"""崩溃恢复：一次性恢复令牌与确认重试（工作台 Stage C2 任务 7.1/7.2，design D2）。

付费任务 paid_request_started 后崩溃 → TaskRecord RECOVERY_REQUIRED：
- 发现阶段（首次查询）：生成一次性随机恢复令牌（明文只回显一次，
  库里只存 SHA-256 hash + 过期时间）；
- 确认阶段：凭令牌创建**关联新执行**（新 TaskRecord + 新 LocalTaskExecution），
  原记录保留审计（不复用原 TaskRecord）；
- 令牌只回显一次：确认或过期后作废；过期/重复/跨用户使用拒绝。
"""
import hashlib
import secrets
from datetime import timedelta

from django.utils import timezone

from ..models import (
    ExecutionClaimStatus, LocalTaskExecution, TaskRecord, TaskStatus,
)
from .local_backend import PAID, PAID_CHECKPOINT, UnknownTaskError, recovery_class

TOKEN_TTL_SECONDS = 24 * 3600


class RecoveryError(Exception):
    status_code = 400


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _fresh_execution(record: TaskRecord) -> LocalTaskExecution:
    """按 pk 重新取执行行（record 上的关联缓存可能是陈旧对象）。"""
    return LocalTaskExecution.objects.filter(record_id=record.pk).first()


def issue_recovery_token(record: TaskRecord) -> dict:
    """为 RECOVERY_REQUIRED 记录签发一次性恢复令牌（幂等窗口内复用）。"""
    if record.status != TaskStatus.RECOVERY_REQUIRED:
        raise RecoveryError("仅待恢复确认的任务可签发恢复令牌")
    execution = _fresh_execution(record)
    if execution is None:
        raise RecoveryError("该任务无本地执行记录（shared 后端任务不适用）")
    now = timezone.now()
    # 有效期内未使用的令牌复用（不重复回显新令牌造成多活令牌）
    if (execution.recovery_token_hash and execution.recovery_expires_at
            and execution.recovery_expires_at > now):
        raise RecoveryError("恢复令牌已签发且未过期，请使用既有令牌确认")
    token = secrets.token_urlsafe(32)
    LocalTaskExecution.objects.filter(pk=execution.pk).update(
        recovery_token_hash=_hash_token(token),
        recovery_expires_at=now + timedelta(seconds=TOKEN_TTL_SECONDS))
    return {
        "task_record_id": record.pk,
        "recovery_token": token,  # 明文仅此一次
        "expires_at": (now + timedelta(seconds=TOKEN_TTL_SECONDS)).isoformat(),
        "disclosure": _partial_output_disclosure(record),
    }


def _partial_output_disclosure(record) -> list:
    """恢复响应披露该任务已写入的部分产物（design D2：不静默）。

    按任务类型描述中断轮次已落库的产物（对用户可见的口径）：
    - ASSISTANT_TURN：已写入的会话消息/树条目（assistant 侧落库的部分产物）；
    - 生成类：供应商已受理但资产未 READY（无产物落库，仅计费风险披露）。
    """
    summary = record.input_summary or {}
    items = []
    if record.task_type == "ASSISTANT_TURN":
        items.append({
            "kind": "assistant_messages",
            "note": "该轮已写入的部分会话消息/树条目保留为审计痕迹",
            "session_id": summary.get("session_id"),
        })
    else:
        items.append({
            "kind": "paid_request",
            "note": "付费请求已受理但产物未落库；确认重试将创建新执行重新生成",
        })
    return items


def confirm_recovery(record: TaskRecord, token: str, user) -> dict:
    """确认恢复：校验令牌 → 创建关联新执行（原记录保留审计）。

    新执行：新 TaskRecord（FAILED-ish 终态标记原记录为 RECOVERED——保留
    审计链）+ 新 LocalTaskExecution(PENDING)。跨用户/过期/重复拒绝。
    """
    execution = _fresh_execution(record)
    if execution is None:
        raise RecoveryError("该任务无本地执行记录")
    if not token:
        raise RecoveryError("缺少恢复令牌")
    now = timezone.now()
    if (not execution.recovery_token_hash
            or not execution.recovery_expires_at
            or execution.recovery_expires_at <= now):
        raise RecoveryError("恢复令牌缺失或已过期，请重新发起恢复")
    if _hash_token(token) != execution.recovery_token_hash:
        raise RecoveryError("恢复令牌不匹配")
    # 归属校验：确认者必须是该任务所属剧的成员（写权限）
    from ..permissions import can_write_play
    if not can_write_play(user, record.play):
        err = RecoveryError("无权确认该任务的恢复")
        err.status_code = 403
        raise err
    try:
        recovery_class(execution.task_name)
    except UnknownTaskError:
        raise RecoveryError("原任务不在受控注册表中，无法创建关联重试")

    from django.db import transaction
    with transaction.atomic():
        # 令牌立即作废（一次性：确认或失败后都不可重放）
        LocalTaskExecution.objects.filter(pk=execution.pk).update(
            recovery_token_hash="", recovery_expires_at=None)
        # 原记录进入终态（RECOVERED 语义用 output_summary 标注，状态回 FAILED
        # 保持 choices 兼容——RECOVERY_REQUIRED 是中间态，审计链完整可查）
        TaskRecord.objects.filter(pk=record.pk).update(
            status=TaskStatus.FAILED,
            error_message="已确认恢复：创建关联新执行重试（原付费请求已受理）",
            completed_at=now)
        new_record = TaskRecord.objects.create(
            play=record.play, episode=record.episode, scene=record.scene,
            shot=record.shot, task_type=record.task_type,
            status=TaskStatus.PENDING,
            input_summary=dict(record.input_summary or {}, recovery_of=record.pk))
        new_execution = LocalTaskExecution.objects.create(
            record=new_record, task_name=execution.task_name,
            args=[new_record.pk], kwargs=execution.kwargs,
            claim_status=ExecutionClaimStatus.PENDING)
    return {
        "recovered": True,
        "original_record_id": record.pk,
        "new_record_id": new_record.pk,
        "new_execution_uuid": str(new_execution.uuid),
        "disclosure": _partial_output_disclosure(record),
    }
