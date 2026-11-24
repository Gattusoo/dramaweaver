"""本地持久化执行器测试（工作台 Stage C1，任务 6.1–6.5）。"""
import threading
from unittest.mock import MagicMock, patch

from django.core.management import call_command
from django.test import TestCase, override_settings
from django.utils import timezone

from apps.ai_gateway.models import AIModel, AIProvider
from apps.creation.models import (
    AgentConfig, Asset, AssetStatus, AssetType, AuditMode, ExecutionClaimStatus,
    LocalTaskExecution, Play, TaskRecord, TaskStatus, TaskType,
)
from apps.creation.pipeline.local_backend import (
    PAID, PAID_CHECKPOINT, UnknownTaskError, recovery_class, retry_policy, send_task,
)
from dvadmin.system.models import CustomUser, UserRole


class C1Fixture(TestCase):
    def setUp(self):
        super().setUp()
        self.owner = CustomUser.objects.create_user(
            username="c1o", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.provider = AIProvider.objects.create(
            name="P", provider_type="LLM", adapter_type="deepseek",
            base_url="https://x", api_key_encrypted="", priority=1)
        self.model = AIModel.objects.create(
            provider=self.provider, model_name="m", display_name="M", model_type="LLM")
        self.config = AgentConfig.objects.create(
            name="配方", llm_model=self.model, created_by=self.owner)
        self.play = Play.objects.create(
            title="本地执行剧", agent_config=self.config, created_by=self.owner,
            audit_mode=AuditMode.NONE)

    def mk_record(self, task_type=TaskType.IMAGE_GEN, status=TaskStatus.PENDING):
        return TaskRecord.objects.create(
            play=self.play, task_type=task_type, status=status, input_summary={})


class LocalExecutionModelTests(C1Fixture, TestCase):
    """任务 6.1：模型、迁移兼容、TaskRecord 序列化不变。"""

    def test_execution_fields_and_uuid(self):
        record = self.mk_record()
        ex = LocalTaskExecution.objects.create(
            record=record, task_name="shortplay.tasks.generate_image",
            args=[record.pk], kwargs={})
        self.assertEqual(ex.claim_status, ExecutionClaimStatus.PENDING)
        self.assertEqual(len(str(ex.uuid)), 36)
        self.assertEqual(ex.attempt, 0)

    def test_record_one_to_one(self):
        record = self.mk_record()
        LocalTaskExecution.objects.create(
            record=record, task_name="t", args=[], kwargs={})
        self.assertEqual(record.local_execution, LocalTaskExecution.objects.get())

    def test_recovery_status_choice_registered(self):
        """RECOVERY_REQUIRED 进入 TaskStatus choices（迁移已扩 choices）。"""
        self.assertIn(TaskStatus.RECOVERY_REQUIRED, TaskStatus.values)

    def test_shared_celery_task_id_semantics(self):
        """shared 路径 celery_task_id 语义不变：send 成功即写 id。"""
        record = self.mk_record()
        from apps.creation.pipeline import dispatcher
        with patch("application.celery.app.send_task",
                   return_value=MagicMock(id="celery-123")) as send:
            dispatcher.send_task("shortplay.tasks.generate_image", args=[record.pk])
        send.assert_called_once()
        # celery 路径不产生本地执行行
        self.assertFalse(LocalTaskExecution.objects.exists())


class RegistryTests(C1Fixture, TestCase):
    """任务 6.2：注册表——未注册拒绝、付费分类、ASSISTANT_TURN 归付费。"""

    def test_unknown_task_rejected(self):
        with self.assertRaises(UnknownTaskError):
            recovery_class("shortplay.tasks.not_registered")
        with self.assertRaises(UnknownTaskError):
            retry_policy("nope.task")

    def test_paid_classification(self):
        self.assertEqual(recovery_class("shortplay.tasks.generate_image"), PAID)
        self.assertEqual(recovery_class("shortplay.tasks.generate_tts"), PAID)
        self.assertEqual(recovery_class("shortplay.tasks.compose_video"),
                         "deterministic")
        # ASSISTANT_TURN 单轮含多次模型调用 → 付费类
        self.assertEqual(recovery_class("shortplay.assistant.run_assistant_turn"), PAID)

    @override_settings(CREATION_TASK_BACKEND="local")
    def test_local_send_creates_execution(self):
        record = self.mk_record()
        result = send_task("shortplay.tasks.generate_image", args=[record.pk],
                           kwargs={"k": 1})
        self.assertTrue(result.id)
        ex = LocalTaskExecution.objects.get(record=record)
        self.assertEqual(ex.claim_status, ExecutionClaimStatus.PENDING)
        self.assertEqual(ex.kwargs, {"k": 1})

    @override_settings(CREATION_TASK_BACKEND="local")
    def test_local_send_unregistered_returns_none(self):
        record = self.mk_record()
        result = send_task("shortplay.tasks.not_registered", args=[record.pk])
        self.assertIsNone(result)
        self.assertFalse(LocalTaskExecution.objects.exists())

    @override_settings(CREATION_TASK_BACKEND="celery")
    def test_celery_backend_unchanged(self):
        record = self.mk_record()
        with patch("application.celery.app.send_task",
                   return_value=MagicMock(id="abc")) as send:
            result = send_task("shortplay.tasks.generate_image", args=[record.pk])
        self.assertEqual(result.id, "abc")
        self.assertFalse(LocalTaskExecution.objects.exists())


@override_settings(CREATION_TASK_BACKEND="local")
class WorkerClaimTests(C1Fixture, TestCase):
    """任务 6.3：条件领取、租约互斥、PENDING 可执行、死租约回收。"""

    def _mk_execution(self, task_name="shortplay.tasks.generate_image",
                      checkpoint=""):
        record = self.mk_record()
        send_task(task_name, args=[record.pk])
        ex = LocalTaskExecution.objects.get(record=record)
        if checkpoint:
            ex.last_checkpoint = checkpoint
            ex.save(update_fields=["last_checkpoint"])
        return record, ex

    def test_pending_executed_after_api_exit(self):
        """API 退出（进程崩溃）后 PENDING 可被 worker 领取执行。"""
        record, ex = self._mk_execution()
        executed = {}
        from apps.creation import tasks as tasks_mod

        def fake_image(record_id):
            # 与真实任务同构：_run 生命周期（置 RUNNING → 成功置 SUCCESS）
            tasks_mod._run(record_id, lambda r: (executed.setdefault("record", r.pk), {})[1])
        with patch.dict(tasks_mod._TASK_FUNCS,
                        {"shortplay.tasks.generate_image": fake_image}):
            call_command("run_local_worker", "--once", stdout=self._stdout())
        self.assertEqual(executed["record"], record.pk)
        ex.refresh_from_db()
        self.assertEqual(ex.claim_status, ExecutionClaimStatus.DONE)
        record.refresh_from_db()
        self.assertEqual(record.status, TaskStatus.SUCCESS)

    def _stdout(self):
        import io
        return io.StringIO()

    def test_two_workers_no_double_claim(self):
        """条件领取互斥：同一执行只被领取一次，第二次领取返回 None。

        （SQLite 测试库不支持跨线程并发写——用顺序两次领取验证条件 UPDATE
        的互斥语义；真实并发下同一 UPDATE 谓词保证只有一个成功。）
        """
        record, ex = self._mk_execution()
        from apps.creation.management.commands.run_local_worker import Command
        cmd = Command()
        first = cmd._claim_next("worker-a")
        second = cmd._claim_next("worker-b")
        self.assertIsNotNone(first)
        self.assertIsNone(second)
        ex.refresh_from_db()
        self.assertEqual(ex.claim_status, ExecutionClaimStatus.RUNNING)
        self.assertEqual(ex.lease_worker_id, "worker-a")
        self.assertEqual(ex.attempt, 1)

    def test_dead_lease_paid_recovery(self):
        """死租约 + 付费检查点已置位 → RECOVERY_REQUIRED（不自动重放）。"""
        record, ex = self._mk_execution(checkpoint=PAID_CHECKPOINT)
        ex.claim_status = ExecutionClaimStatus.RUNNING
        ex.lease_expires_at = timezone.now() - timezone.timedelta(seconds=120)
        ex.save(update_fields=["claim_status", "lease_expires_at"])
        call_command("run_local_worker", "--once", stdout=self._stdout())
        record.refresh_from_db()
        ex.refresh_from_db()
        self.assertEqual(record.status, TaskStatus.RECOVERY_REQUIRED)
        self.assertEqual(ex.claim_status, ExecutionClaimStatus.RECOVERY_REQUIRED)

    def test_dead_lease_deterministic_reclaimed(self):
        """死租约 + 确定性任务 → 重新可领取（PENDING）。"""
        record, ex = self._mk_execution(
            task_name="shortplay.tasks.generate_outline")
        ex.claim_status = ExecutionClaimStatus.RUNNING
        ex.lease_expires_at = timezone.now() - timezone.timedelta(seconds=120)
        ex.save(update_fields=["claim_status", "lease_expires_at"])
        call_command("run_local_worker", "--once", stdout=self._stdout())
        ex.refresh_from_db()
        # 启动回收 → PENDING；--once 循环随后领取并执行（文本任务 → SUCCESS）
        self.assertIn(ex.claim_status,
                      (ExecutionClaimStatus.DONE, ExecutionClaimStatus.PENDING))

    def test_transient_failure_local_retry(self):
        """瞬时失败 → 本地退避重试（PENDING + next_attempt_at），不落 FAILED。"""
        record, ex = self._mk_execution(
            task_name="shortplay.tasks.generate_outline")
        from apps.ai_gateway.gateway import AIGatewayError
        calls = {"n": 0}
        from apps.creation import tasks as tasks_mod

        def flaky(record_id):
            calls["n"] += 1
            if calls["n"] == 1:
                raise AIGatewayError("瞬时故障")
        with patch.dict(tasks_mod._TASK_FUNCS,
                        {"shortplay.tasks.generate_outline": flaky}):
            call_command("run_local_worker", "--once", stdout=self._stdout())
        ex.refresh_from_db()
        # 第一次失败 → PENDING 等待退避（next_attempt_at 已设）
        self.assertEqual(ex.claim_status, ExecutionClaimStatus.PENDING)
        self.assertIsNotNone(ex.next_attempt_at)
        record.refresh_from_db()
        self.assertNotEqual(record.status, TaskStatus.FAILED)

    def test_cancel_not_resurrected(self):
        """取消的记录不复活：领取执行但 _run 跳过（CANCELLED 保持）。"""
        record, ex = self._mk_execution()
        TaskRecord.objects.filter(pk=record.pk).update(status=TaskStatus.CANCELLED)
        from apps.creation import tasks as tasks_mod
        ran = []
        # 与真实任务同构：_run 的取消保护在 _mark_running（已取消返回 None）
        def fake_image(record_id):
            tasks_mod._run(record_id, lambda r: ran.append(r.pk))
        with patch.dict(tasks_mod._TASK_FUNCS,
                        {"shortplay.tasks.generate_image": fake_image}):
            call_command("run_local_worker", "--once", stdout=self._stdout())
        self.assertEqual(ran, [])  # 未执行
        record.refresh_from_db()
        self.assertEqual(record.status, TaskStatus.CANCELLED)


class PaidCheckpointTests(C1Fixture, TestCase):
    """任务 6.4：供应商调用前置位 paid_request_started。"""

    @override_settings(CREATION_TASK_BACKEND="local")
    def test_gateway_call_marks_checkpoint(self):
        record = self.mk_record()
        send_task("shortplay.tasks.generate_image", args=[record.pk])
        inner = MagicMock()
        inner.call.return_value = {"url": "/media/x.png"}
        from apps.creation import tasks as tasks_mod
        g = tasks_mod._PaidCheckpointGateway(inner)
        g.call("IMAGE", self.config, play=self.play, task_record=record)
        ex = LocalTaskExecution.objects.get(record=record)
        self.assertEqual(ex.last_checkpoint, PAID_CHECKPOINT)
        inner.call.assert_called_once()

    @override_settings(CREATION_TASK_BACKEND="local")
    def test_chat_and_vision_mark_checkpoint(self):
        record = self.mk_record()
        send_task("shortplay.assistant.run_assistant_turn", args=[record.pk])
        inner = MagicMock()
        from apps.creation import tasks as tasks_mod
        g = tasks_mod._PaidCheckpointGateway(inner)
        g.chat(self.config, messages=[], task_record=record)
        ex = LocalTaskExecution.objects.get(record=record)
        self.assertEqual(ex.last_checkpoint, PAID_CHECKPOINT)

    @override_settings(CREATION_TASK_BACKEND="celery")
    def test_celery_backend_no_checkpoint_overhead(self):
        from apps.creation import tasks as tasks_mod
        from apps.ai_gateway.gateway import AIGateway
        gw = tasks_mod._gateway(self.play)
        self.assertIsInstance(gw, AIGateway)  # shared 路径不包装


class BillingLocalDisableTests(C1Fixture, TestCase):
    """任务 6.5：local 模式禁用 billing 调度路径（无预扣/钱包副作用）。"""

    @override_settings(IS_LOCAL_MODE=True)
    def test_local_gate_noop(self):
        from apps.billing.services.gate import dispatch_gate
        result = dispatch_gate(self.owner, ("test", "1"), 100, slot_kind="ai")
        self.assertIsNone(result)  # 不占槽位、不预扣

    @override_settings(IS_LOCAL_MODE=False, ENABLE_BILLING_HOLD=True)
    def test_shared_gate_unchanged(self):
        """shared 的 gate 行为不被 local 分支影响（有 hold 时照常）。"""
        from apps.billing.services import gate as gate_mod
        fake_hold = MagicMock()
        with patch.object(gate_mod, "hold_for_task",
                          return_value=fake_hold) as hold, \
             patch.object(gate_mod, "acquire_slot", return_value=True):
            result = gate_mod.dispatch_gate(self.owner, ("test", "2"), 100,
                                            slot_kind="ai")
        hold.assert_called_once()
        self.assertIs(result, fake_hold)
