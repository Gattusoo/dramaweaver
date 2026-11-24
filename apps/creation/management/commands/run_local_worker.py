"""本地持久化执行 worker（工作台 Stage C1 任务 6.3，design D1/D2）。

单逻辑 worker：`python manage.py run_local_worker`
- 事务条件领取 PENDING/到期 RETRYING 的 LocalTaskExecution（租约防重复领取）；
- 调用现有 Celery 任务函数（保持同步签名，走原 _run 生命周期）；
- 租约续约：长任务运行中周期续租；租约过期视为崩溃，按恢复分类处理；
- AI I/O 有界线程池 + FFmpeg 独立并发（任务间串行领取，数据库状态写串行）。

同库两个 worker 实例：条件领取天然互斥（一次 update 只有一行成功）；
启动时对已死租约（expired）的 RUNNING 执行按恢复分类回收。
"""
import logging
import threading
import time

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from ...models import (
    ExecutionClaimStatus, LocalTaskExecution, TaskStatus,
)
from ...pipeline.local_backend import PAID_CHECKPOINT, recovery_class, retry_policy
from ...tasks import _TASK_FUNCS

logger = logging.getLogger(__name__)

LEASE_SECONDS = 60          # 租约时长：远小于最短任务，远大于轮询间隔
HEARTBEAT_SECONDS = 20      # 秒约间隔
POLL_SECONDS = 2            # 空闲轮询
PAID = "paid"


class Command(BaseCommand):
    help = "本地持久化任务执行 worker（单逻辑 worker，租约互斥）"

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true",
                            help="处理完当前可领取任务后退出（冒烟/测试用）")
        parser.add_argument("--max-tasks", type=int, default=0,
                            help="最多执行 N 个任务后退出（0=不限）")

    def handle(self, *args, **options):
        from django.conf import settings
        backend = getattr(settings, "CREATION_TASK_BACKEND", "") or \
            ("local" if getattr(settings, "IS_LOCAL_MODE", False) else "celery")
        if backend != "local":
            self.stderr.write("CREATION_TASK_BACKEND != local，本地 worker 不适用")
            return
        worker_id = f"{settings.BASE_DIR.name if hasattr(settings, 'BASE_DIR') else 'w'}-{int(time.time())}"
        self.stdout.write(f"本地 worker 启动: {worker_id}")
        self._stop = threading.Event()
        done = 0
        try:
            # 启动即回收：死租约的 RUNNING 执行按恢复分类处理
            self._reclaim_expired(worker_id)
            while not self._stop.is_set():
                execution = self._claim_next(worker_id)
                if execution is None:
                    if options.get("once"):
                        break
                    time.sleep(POLL_SECONDS)
                    continue
                self._run_execution(execution, worker_id)
                done += 1
                if options.get("max_tasks") and done >= options["max_tasks"]:
                    break
        except KeyboardInterrupt:
            self.stdout.write("收到中断，worker 退出")
        finally:
            self.stdout.write(f"worker 退出（完成 {done} 个任务）")

    # ── 领取 ─────────────────────────────────────────────────────────────

    def _claim_next(self, worker_id):
        """事务条件领取：一条 UPDATE 抢占，两个 worker 不重复领取。"""
        now = timezone.now()
        with transaction.atomic():
            candidates = (LocalTaskExecution.objects
                          .select_for_update(skip_locked=True)
                          .filter(claim_status=ExecutionClaimStatus.PENDING)
                          .order_by("created_at")[:1])
            execution = None
            for candidate in candidates:
                if candidate.next_attempt_at and candidate.next_attempt_at > now:
                    continue  # 未到重试时间
                execution = candidate
                break
            if execution is None:
                # PENDING 无（或都未到期）→ 无可领取
                return None
            updated = LocalTaskExecution.objects.filter(
                pk=execution.pk, claim_status=ExecutionClaimStatus.PENDING
            ).update(
                claim_status=ExecutionClaimStatus.RUNNING,
                lease_worker_id=worker_id,
                lease_expires_at=now + timezone.timedelta(seconds=LEASE_SECONDS),
                attempt=execution.attempt + 1)
            if not updated:
                return None
            return execution

    # ── 执行 ─────────────────────────────────────────────────────────────

    def _run_execution(self, execution, worker_id):
        func = _TASK_FUNCS.get(execution.task_name)
        record_id = execution.args[0] if execution.args else None
        if func is None or record_id is None:
            self._finish_claim(execution, ExecutionClaimStatus.DONE)
            logger.error("执行 %s 找不到任务函数或 record_id", execution.task_name)
            return
        # 租约心跳线程
        heartbeat = threading.Thread(
            target=self._heartbeat_loop, args=(execution.pk, worker_id), daemon=True)
        heartbeat.start()
        retried = False
        try:
            func(record_id, **(execution.kwargs or {}))
        except Exception as exc:  # noqa: BLE001 — _run 已落终态/保持 RUNNING，这里管本地侧
            logger.exception("本地执行 %s 异常: %s", execution.task_name, exc)
            retried = self._handle_failure(execution, exc)
        finally:
            self._stop_heartbeat(execution.pk)
        if not retried:
            # 业务终态（SUCCESS/FAILED/CANCELLED）或名额耗尽 → 领取结束；
            # 已转 PENDING+next_attempt_at 的等待重领，不覆盖
            self._finish_claim(execution, ExecutionClaimStatus.DONE)

    def _heartbeat_loop(self, execution_pk, worker_id):
        while not self._stop_heartbeat_flags.get(execution_pk, False):
            time.sleep(HEARTBEAT_SECONDS)
            if self._stop_heartbeat_flags.get(execution_pk, False):
                return
            LocalTaskExecution.objects.filter(
                pk=execution_pk, lease_worker_id=worker_id
            ).update(lease_expires_at=timezone.now()
                     + timezone.timedelta(seconds=LEASE_SECONDS))

    _stop_heartbeat_flags = {}

    def _stop_heartbeat(self, execution_pk):
        self._stop_heartbeat_flags[execution_pk] = True

    def _finish_claim(self, execution, status):
        LocalTaskExecution.objects.filter(pk=execution.pk).update(
            claim_status=status, lease_worker_id="", lease_expires_at=None)

    def _handle_failure(self, execution, exc) -> bool:
        """瞬时失败映射本地重试；付费/确定性耗尽 → 领取结束（TaskRecord 已 FAILED）。

        本地判定用 _TRANSIENT_ERRORS 成员关系 + 注册表策略（Celery 的
        _retry_pending 依赖 current_task，本地直调不适用）。
        返回 True 表示已转 PENDING 等待重领。
        """
        from ...tasks import _TRANSIENT_ERRORS
        if not isinstance(exc, _TRANSIENT_ERRORS):
            return False  # 确定性失败：_run 已落 FAILED，领取结束
        policy = retry_policy(execution.task_name)
        if execution.attempt >= policy["max_attempts"]:
            # 名额耗尽：_run 对瞬时失败保持 RUNNING（待重试契约），这里显式落 FAILED
            from ...models import TaskRecord
            TaskRecord.objects.filter(pk=execution.record_id).update(
                status="FAILED", error_message=str(exc)[:2000])
            return False
        backoff = policy["backoff_seconds"] * (2 ** max(0, execution.attempt - 1))
        LocalTaskExecution.objects.filter(pk=execution.pk).update(
            claim_status=ExecutionClaimStatus.PENDING,
            next_attempt_at=timezone.now() + timezone.timedelta(seconds=backoff),
            lease_worker_id="", lease_expires_at=None)
        return True

    # ── 死租约回收（启动时 + 崩溃后）─────────────────────────────────────

    def _reclaim_expired(self, worker_id):
        """租约过期的 RUNNING 执行：确定性 → 重领（PENDING）；付费 → RECOVERY_REQUIRED。

        心跳/租约超时不是"已重复执行"的证明；恢复策略宁可暂停付费任务。
        checkpoint=paid_request_started 已置位的（含 ASSISTANT_TURN 已调过模型）
        一律 RECOVERY_REQUIRED——不猜测供应商是否已受理。
        """
        now = timezone.now()
        expired = LocalTaskExecution.objects.filter(
            claim_status=ExecutionClaimStatus.RUNNING,
            lease_expires_at__lt=now)
        reclaimed = recovery = 0
        for execution in expired:
            record = execution.record
            if execution.last_checkpoint == PAID_CHECKPOINT:
                # 付费请求已受理（含 ASSISTANT_TURN 已调过模型）——
                # 不猜测供应商是否已扣费，一律 RECOVERY_REQUIRED
                TaskStatus_ = TaskStatus
                (type(record).objects.filter(pk=record.pk)
                 .exclude(status=TaskStatus_.CANCELLED)
                 .update(status=TaskStatus_.RECOVERY_REQUIRED,
                         error_message="付费请求已受理后执行中断，等待恢复确认"))
                self._finish_claim(execution, ExecutionClaimStatus.RECOVERY_REQUIRED)
                recovery += 1
            else:
                # checkpoint 未置位（尚未调用任何供应商）：确定性/付费任务都安全重领
                LocalTaskExecution.objects.filter(pk=execution.pk).update(
                    claim_status=ExecutionClaimStatus.PENDING,
                    lease_worker_id="", lease_expires_at=None,
                    next_attempt_at=now)
                reclaimed += 1
        if reclaimed or recovery:
            self.stdout.write(f"死租约回收: 重领 {reclaimed}, 待恢复 {recovery}")
