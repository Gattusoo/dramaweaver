"""受控任务注册表与 local send_task 适配（工作台 Stage C1 任务 6.2，design D1/D2）。

- 注册表：每个 Celery 任务名声明「恢复分类」（deterministic / paid）与重试策略，
  未注册任务拒绝（不静默落库后无人执行）。
- ASSISTANT_TURN 归付费类：单轮含多次模型调用，paid_request_started 置位后
  整轮 RECOVERY_REQUIRED，不自动重放（design D2 明确）。
- send_task 适配：CREATION_TASK_BACKEND=local 时把 record_id/kwargs 持久化为
  LocalTaskExecution（PENDING，事务提交后可领取），返回带真实 UUID 的轻量结果，
  保持 dispatcher 的兼容形状；celery（缺省）时走原 Celery send_task。
"""
import logging

from django.conf import settings

logger = logging.getLogger(__name__)

# ── 恢复分类 ──────────────────────────────────────────────────────────────

PAID = "paid"                # 付费类：受理后崩溃 → RECOVERY_REQUIRED，不自动重放
DETERMINISTIC = "deterministic"  # 确定性：崩溃可安全重领（幂等或无副作用）

# 与 tasks.py 的 _TASK_RETRY_PAID 同口径：生成类不自动重试；纯文本 LLM 可重试。
# 本地重试策略由 worker 映射（attempt/next_attempt_at），不依赖 Celery self.retry。
_REGISTRY = {
    "shortplay.tasks.generate_outline": {
        "recovery": DETERMINISTIC, "max_attempts": 4, "backoff_seconds": 8},
    "shortplay.tasks.generate_episode_script": {
        "recovery": DETERMINISTIC, "max_attempts": 4, "backoff_seconds": 8},
    "shortplay.tasks.generate_storyboard": {
        "recovery": DETERMINISTIC, "max_attempts": 4, "backoff_seconds": 8},
    "shortplay.tasks.generate_character_ref": {
        "recovery": PAID, "max_attempts": 1, "backoff_seconds": 0},
    "shortplay.tasks.generate_image": {
        "recovery": PAID, "max_attempts": 1, "backoff_seconds": 0},
    "shortplay.tasks.generate_tts": {
        "recovery": PAID, "max_attempts": 1, "backoff_seconds": 0},
    "shortplay.tasks.select_bgm": {
        "recovery": DETERMINISTIC, "max_attempts": 3, "backoff_seconds": 5},
    "shortplay.tasks.compose_video": {
        "recovery": DETERMINISTIC, "max_attempts": 2, "backoff_seconds": 30},
    "shortplay.tasks.trigger_publish": {
        "recovery": DETERMINISTIC, "max_attempts": 3, "backoff_seconds": 10},
    "shortplay.tasks.render_channel_version": {
        "recovery": DETERMINISTIC, "max_attempts": 2, "backoff_seconds": 30},
    "shortplay.tasks.render_preview": {
        "recovery": DETERMINISTIC, "max_attempts": 2, "backoff_seconds": 15},
    "shortplay.tasks.generate_derivatives": {
        "recovery": DETERMINISTIC, "max_attempts": 3, "backoff_seconds": 10},
    "shortplay.assistant.run_assistant_turn": {
        # 单轮混杂多次付费 LLM 调用与确定性工具调用 → 付费类：
        # checkpoint 未置位（未调任何供应商）崩溃可重领；置位后整轮 RECOVERY_REQUIRED。
        "recovery": PAID, "max_attempts": 1, "backoff_seconds": 0},
    "shortplay.tasks.generate_subtitle": {
        "recovery": DETERMINISTIC, "max_attempts": 3, "backoff_seconds": 10},
}

# 付费检查点（design D2）：供应商调用前置位
PAID_CHECKPOINT = "paid_request_started"


class UnknownTaskError(Exception):
    """任务名不在受控注册表中——本地后端拒绝执行未声明任务。"""


def task_registry():
    """注册表视图（只读）。"""
    return dict(_REGISTRY)


def recovery_class(task_name: str) -> str:
    entry = _REGISTRY.get(task_name)
    if entry is None:
        raise UnknownTaskError(f"任务未在本地注册表声明: {task_name}")
    return entry["recovery"]


def retry_policy(task_name: str) -> dict:
    entry = _REGISTRY.get(task_name)
    if entry is None:
        raise UnknownTaskError(f"任务未在本地注册表声明: {task_name}")
    return {"max_attempts": entry["max_attempts"],
            "backoff_seconds": entry["backoff_seconds"]}


# ── send_task 适配（dispatcher 形状兼容）──────────────────────────────────

class LocalSendResult:
    """轻量结果对象：镜像 Celery AsyncResult 的 .id 形状（dispatcher 只取 id）。"""

    def __init__(self, execution_uuid):
        self.id = str(execution_uuid)


def _backend() -> str:
    explicit = getattr(settings, "CREATION_TASK_BACKEND", "") or ""
    if explicit:
        return explicit
    return "local" if getattr(settings, "IS_LOCAL_MODE", False) else "celery"


def send_task(task_name, args=None, kwargs=None):
    """按 CREATION_TASK_BACKEND 分派：local 持久化执行记录；celery 原样发送。"""
    if _backend() == "local":
        return _send_local(task_name, args or [], kwargs or {})
    from application.celery import app as celery_app
    try:
        return celery_app.send_task(task_name, args=args or [], kwargs=kwargs or {})
    except Exception as exc:  # noqa: BLE001
        logger.error("发送 Celery 任务 %s 失败: %s", task_name, exc)
        return None


def _send_local(task_name, args, kwargs):
    """本地入队：TaskRecord(pk 在 args[0]) → LocalTaskExecution(PENDING)。

    立即持久化（调用方 dispatch_task 的 defer_send 已保证事务提交后才发送，
    worker 读到的 TaskRecord 一定已提交）；写失败返回 None（dispatcher 的
    _send_record 会把 TaskRecord 标 FAILED，状态可重派）。
    """
    from ..models import ExecutionClaimStatus, LocalTaskExecution
    try:
        recovery_class(task_name)  # 未注册拒绝
    except UnknownTaskError:
        logger.error("本地后端拒绝未注册任务: %s", task_name)
        return None
    record_id = args[0] if args else None
    if record_id is None:
        logger.error("本地入队缺少 record_id: %s", task_name)
        return None
    try:
        execution, _created = LocalTaskExecution.objects.update_or_create(
            record_id=record_id,
            defaults={"task_name": task_name, "args": list(args), "kwargs": kwargs,
                      "claim_status": ExecutionClaimStatus.PENDING,
                      "next_attempt_at": None})
    except Exception as exc:  # noqa: BLE001
        logger.error("本地入队失败 %s: %s", task_name, exc)
        return None
    return LocalSendResult(execution.uuid)
