"""信任分级与渐进自主（主计划 3.2.5 / 3.2.6 / design D1-D3）。

- trust_level_of(user)：读取或初始化（TRUST_INITIAL_SCORE）
- record_outcome(user, event)：apply → +TRUST_STEP_APPLY / discard|stale → -TRUST_STEP_DISCARD
  幂等：仅提案 PENDING→终态的首次迁移计一次（调用方在状态迁移后传 proposal 判定，
  已终态的重复事件直接跳过）；夹紧 [0,1]
- should_auto_approve(user, proposal)：四条件（design D3）
    ① ENABLE_PROGRESSIVE_AUTONOMY 开启
    ② payload.confidence ≥ AUTO_APPROVE_MIN_CONFIDENCE
    ③ trust_score ≥ auto_approve_threshold（阈值：行值 > settings.TRUST_DEFAULT_THRESHOLD）
    ④ kind != SHOT_DELETE（不可逆删除不自动化）
"""
import logging

from .models import ProposalKind, ProposalStatus

logger = logging.getLogger(__name__)


def trust_level_of(user):
    """读取或初始化信任分级行（不落库——首次写事件时 update_or_create 一并建行）。"""
    from django.conf import settings

    from .models import TrustLevel
    if user is None or not getattr(user, "pk", None):
        return None
    row = TrustLevel.objects.filter(user=user).first()
    if row is None:
        row = TrustLevel(user=user, trust_score=float(
            getattr(settings, "TRUST_INITIAL_SCORE", 0.5)),
            auto_approve_threshold=None)
    return row


def effective_threshold(row):
    """行级阈值优先，空取 settings 默认。"""
    from django.conf import settings
    if row is None:
        return float(getattr(settings, "TRUST_DEFAULT_THRESHOLD", 0.8))
    return (row.auto_approve_threshold
            if row.auto_approve_threshold is not None
            else float(getattr(settings, "TRUST_DEFAULT_THRESHOLD", 0.8)))


def trust_score_of(user) -> float:
    row = trust_level_of(user)
    if row is None:
        return 0.0
    return float(row.trust_score)


def level_label(score: float) -> str:
    """展示分档（design D1）：cautious / standard / trusted，无行为差异。"""
    if score < 0.3:
        return "cautious"
    if score <= 0.7:
        return "standard"
    return "trusted"


def record_outcome(user, event: str, *, proposal=None):
    """信任分累积（design D1）。event ∈ apply / discard / stale。

    幂等：调用方在状态迁移后传入 proposal；其 status 非 APPLIED/DISCARDED/STALE
    （即尚未迁移）或已非 PENDING 起点时跳过——只计 PENDING→终态的首次迁移。
    失败只记日志不阻断提案操作。
    """
    from django.conf import settings

    from .models import TrustLevel
    if user is None or not getattr(user, "pk", None):
        return None
    if event not in ("apply", "discard", "stale"):
        return None
    if proposal is not None:
        # 幂等闸：仅终态合法（迁移已发生）时计分
        if proposal.status not in (ProposalStatus.APPLIED, ProposalStatus.DISCARDED,
                                   ProposalStatus.STALE):
            return None
    step = float(getattr(settings, "TRUST_STEP_APPLY" if event == "apply"
                         else "TRUST_STEP_DISCARD", 0.02 if event == "apply" else 0.01))
    delta = abs(step) if event == "apply" else -abs(step)
    try:
        row, _ = TrustLevel.objects.update_or_create(
            user=user,
            defaults={"trust_score": _clamped(trust_score_of(user) + delta)})
        return row
    except Exception:  # noqa: BLE001 — 计分失败不影响提案操作
        logger.exception("信任分累积失败 user=%s event=%s", getattr(user, "pk", None), event)
        return None


def _clamped(v: float, lo=0.0, hi=1.0) -> float:
    return max(lo, min(hi, float(v)))


def proposal_confidence(proposal) -> float:
    """提案置信度（design D2）：payload.confidence 0-1；无 → 0.0（不自动批准）。"""
    try:
        v = (proposal.payload or {}).get("confidence")
        return _clamped(float(v)) if v is not None else 0.0
    except (TypeError, ValueError):
        return 0.0


def should_auto_approve(user, proposal) -> bool:
    """渐进自动批准判定（design D3 四条件）。任一不满足即 False。"""
    from django.conf import settings
    try:
        if not getattr(settings, "ENABLE_PROGRESSIVE_AUTONOMY", False):
            return False
        if proposal is None or proposal.pk is None:
            return False
        if proposal.kind == ProposalKind.SHOT_DELETE:
            return False  # 不可逆删除不自动化
        if proposal_confidence(proposal) < float(getattr(
                settings, "AUTO_APPROVE_MIN_CONFIDENCE", 0.8)):
            return False
        row = trust_level_of(user)
        if row is None:
            return False
        return _clamped(row.trust_score) >= effective_threshold(row)
    except Exception:  # noqa: BLE001 — 判定失败按不自动批准
        logger.exception("自动批准判定失败 proposal=%s", getattr(proposal, "pk", None))
        return False
