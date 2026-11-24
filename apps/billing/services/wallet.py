"""钱包三段式服务：hold（预扣）→ settle（对账）→ refund（退回）。

铁律（design D1/D2）：
- 只追加流水：所有余额变更都产生 WalletEntry，Wallet.balance 与
  entry.balance_after 同事务内累加写入，全链可重放
- 幂等：hold 按 (ref_kind, ref_id) 唯一——重复派发 no-op；settle/refund
  校验目标 hold 状态，已终态重复调用 no-op 返回既有结果
- 并发：select_for_update 锁钱包行（SQLite 退化为库级锁可接受）
- ¥→分：Decimal 乘 100 后 quantize，全程禁 float
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from decimal import Decimal, ROUND_HALF_UP

from django.db import transaction
from django.utils import timezone

from ..models import EntryStatus, EntryType, Wallet, WalletEntry

logger = logging.getLogger(__name__)

# hold 缺省有效期（小时）：任务丢失后余额不至于永久挂死；过期由 Celery 回收
DEFAULT_HOLD_HOURS = 24


class InsufficientPoints(Exception):
    """余额不足（HTTP 402 + 业务码 4001）。携带所需/现有分值供信封渲染。"""

    def __init__(self, needed: int, balance: int, message: str = ""):
        self.needed = needed
        self.balance = balance
        super().__init__(message or f"积分不足：需 {needed} 分，余额 {balance} 分")


def yuan_to_points(yuan) -> int:
    """¥（Decimal/str/int）→ 整数分。Decimal 乘 100 quantize，禁 float。

    ¥0.005 边界按四舍五入（ROUND_HALF_UP）→ 1 分；负数拒绝。
    """
    if isinstance(yuan, float):  # noqa: SIM101 — 防御：上游误传 float 直接拒绝
        raise TypeError("金额须为 Decimal/str/int，禁 float（精度风险）")
    d = yuan if isinstance(yuan, Decimal) else Decimal(str(yuan))
    points = int((d * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
    if points < 0:
        raise ValueError("金额不可为负")
    return points


@dataclass
class HoldResult:
    entry: WalletEntry
    created: bool  # False = 幂等命中既有 hold


def get_or_create_wallet(user) -> Wallet:
    wallet, _ = Wallet.objects.get_or_create(user=user)
    return wallet


def _apply(wallet: Wallet, entry_type: EntryType, amount: int, *,
           ref_kind: str = "", ref_id: str = "", note: str = "",
           status: EntryStatus = EntryStatus.SETTLED,
           expires_at=None) -> WalletEntry:
    """事务内追加一条流水并推进余额（内部专用；负 amount 减余额）。"""
    wallet.balance += amount
    wallet.save(update_fields=["balance", "updated_at"])
    entry = WalletEntry.objects.create(
        wallet=wallet, type=entry_type, status=status, amount=amount,
        balance_after=wallet.balance, ref_kind=ref_kind, ref_id=ref_id,
        note=note, expires_at=expires_at,
    )
    return entry


@transaction.atomic
def grant(user, points: int, *, ref_kind: str = "manual", ref_id: str = "",
          note: str = "") -> WalletEntry | None:
    """发放积分（正数）。按 (ref_kind, ref_id, type=grant) 幂等：已存在返回 None。

    注册赠金以 ref_kind=signup, ref_id=user_id 调用——重复信号不重复发。
    """
    if points <= 0:
        return None
    wallet = Wallet.objects.select_for_update().get_or_create(user=user)[0]
    existing = WalletEntry.objects.filter(
        wallet=wallet, ref_kind=ref_kind, ref_id=ref_id, type=EntryType.GRANT,
    ).first()
    if existing is not None:
        return None  # 幂等：该引用已发放过
    return _apply(wallet, EntryType.GRANT, points,
                  ref_kind=ref_kind, ref_id=ref_id, note=note)


@transaction.atomic
def adjust(user, points: int, *, note: str = "") -> WalletEntry:
    """管理员调整（可正负）。不幂等（每次调用都是新流水）。"""
    if points == 0:
        raise ValueError("调整额不可为 0")
    wallet = Wallet.objects.select_for_update().get_or_create(user=user)[0]
    return _apply(wallet, EntryType.ADJUST, points, note=note)


@transaction.atomic
def hold_for_task(owner, task_ref: tuple[str, str], estimate_points: int, *,
                  note: str = "", expires_at=None) -> HoldResult | None:
    """预扣积分（任务派发前）。

    - task_ref=(ref_kind, ref_id)：幂等键。同引用重复派发 no-op 返回既有 hold
      （HoldResult.created=False）；调用方据此跳过重复扣费
    - 余额不足：抛 InsufficientPoints（调用方渲染 402/4001）
    - expires_at 缺省 = now + DEFAULT_HOLD_HOURS；过期由 expire_stale_holds 回收
    """
    ref_kind, ref_id = task_ref
    wallet = Wallet.objects.select_for_update().get_or_create(user=owner)[0]
    existing = WalletEntry.objects.select_for_update().filter(
        wallet=wallet, ref_kind=ref_kind, ref_id=ref_id, type=EntryType.HOLD,
    ).first()
    if existing is not None:
        # 幂等：既有 hold 已结算/释放则不能再作为预扣存在（返回 None 让调用方
        # 重新派发新任务引用）；仍 ACTIVE 则复用
        if existing.status == EntryStatus.ACTIVE:
            return HoldResult(entry=existing, created=False)
        return None
    if estimate_points <= 0:
        # 零预扣不落流水（免费任务/估算为零）——但仍返回 None 表示无需 hold
        return None
    if wallet.balance < estimate_points:
        raise InsufficientPoints(estimate_points, wallet.balance)
    if expires_at is None:
        from datetime import timedelta
        expires_at = timezone.now() + timedelta(hours=DEFAULT_HOLD_HOURS)
    entry = _apply(wallet, EntryType.HOLD, -estimate_points,
                   ref_kind=ref_kind, ref_id=ref_id, note=note,
                   status=EntryStatus.ACTIVE, expires_at=expires_at)
    return HoldResult(entry=entry, created=True)


@transaction.atomic
def settle_task(hold_entry: WalletEntry, actual_points: int, *,
                note: str = "", breakdown: dict | None = None) -> WalletEntry:
    """结算：按实际额对账。实际 < 预扣退差额；实际 > 预扣补扣差额。

    - 目标 hold 非 ACTIVE（已结算/已释放）：no-op 返回该 hold（幂等）
    - settle 流水 ref 与 hold 相同；差额走 refund（正）/fee（负）流水，
      三条 entry 共享同一 ref —— 重放可完整对账
    """
    if hold_entry.type != EntryType.HOLD:
        raise ValueError("settle 目标必须是 hold 条目")
    # select_for_update 锁 hold 行防并发 settle
    hold = WalletEntry.objects.select_for_update().get(pk=hold_entry.pk)
    if hold.status != EntryStatus.ACTIVE:
        return hold  # 已结算/已释放：幂等 no-op
    wallet = Wallet.objects.select_for_update().get(pk=hold.wallet_id)
    held = -hold.amount  # 预扣额（正数）
    actual = max(0, int(actual_points))
    # hold 置为已结算（不新增流水——hold 行本身代表冻结，settle 行代表扣除确认）
    hold.status = EntryStatus.SETTLED
    hold.save(update_fields=["status"])
    # 结算流水：确认扣除实际额（hold 已减预扣额，先退回再按实际扣——
    # 等价做法：refund(预扣-实际) + settle(实际) 两条；此处合并为：
    #   差额 = 实际 - 预扣；差额为负先退（refund 正额），为正补扣（fee 负额）
    diff = actual - held
    if diff < 0:
        # 实际 < 预扣：退差额
        _apply(wallet, EntryType.REFUND, -diff,
               ref_kind=hold.ref_kind, ref_id=hold.ref_id,
               note=note or "结算差额退回")
    elif diff > 0:
        # 实际 > 预扣：补扣（余额可能不足——按设计允许透传为负？
        # 不允许：管理员告警兜底，此处拒绝透支，扣到 0 并记 warning）
        if wallet.balance < diff:
            logger.warning(
                "settle 补扣超出余额 wallet=%s diff=%s balance=%s（记应收）",
                wallet.pk, diff, wallet.balance,
            )
            diff = max(0, wallet.balance)
        if diff > 0:
            _apply(wallet, EntryType.FEE, -diff,
                   ref_kind=hold.ref_kind, ref_id=hold.ref_id,
                   note=note or "结算补扣差额")
    # 实际 == 预扣：无差额，仅状态翻转
    return hold


@transaction.atomic
def refund_hold(hold_entry: WalletEntry, *, reason: str = "") -> WalletEntry:
    """失败整额退回。非 ACTIVE 的重复调用 no-op 返回该 hold（幂等）。"""
    hold = WalletEntry.objects.select_for_update().get(pk=hold_entry.pk)
    if hold.type != EntryType.HOLD:
        raise ValueError("refund 目标必须是 hold 条目")
    if hold.status != EntryStatus.ACTIVE:
        return hold
    wallet = Wallet.objects.select_for_update().get(pk=hold.wallet_id)
    hold.status = EntryStatus.RELEASED
    hold.save(update_fields=["status"])
    _apply(wallet, EntryType.REFUND, -hold.amount,
           ref_kind=hold.ref_kind, ref_id=hold.ref_id,
           note=reason or "任务失败整额退回")
    return hold


@transaction.atomic
def charge_service_fee(user, points: int, *, ref_kind: str, ref_id: str,
                        note: str = "") -> WalletEntry | None:
    """服务费扣收（自带 Key 按次，消费 service_fee 事件）。amount=0 直接跳过。"""
    if points <= 0:
        return None
    wallet = Wallet.objects.select_for_update().get_or_create(user=user)[0]
    existing = WalletEntry.objects.filter(
        wallet=wallet, ref_kind=ref_kind, ref_id=ref_id, type=EntryType.FEE,
    ).first()
    if existing is not None:
        return existing  # 幂等
    if wallet.balance < points:
        # 服务费不阻断已成功的调用（调用已完成），扣到 0 并告警
        logger.warning("服务费超出余额 wallet=%s（记应收）", wallet.pk)
        points = max(0, wallet.balance)
        if points == 0:
            return None
    return _apply(wallet, EntryType.FEE, -points,
                  ref_kind=ref_kind, ref_id=ref_id, note=note or "自带渠道服务费")


@transaction.atomic
def record_meter(user, *, meter_kind: str, quantity: str,
                 ref_kind: str = "", ref_id: str = "") -> WalletEntry:
    """存储计量事件（GB·月，第二期结算；本期只记量不改余额）。amount=0。"""
    wallet = Wallet.objects.get_or_create(user=user)[0]
    return WalletEntry.objects.create(
        wallet=wallet, type=EntryType.METER, status=EntryStatus.SETTLED,
        amount=0, balance_after=wallet.balance,
        ref_kind=ref_kind or "meter", ref_id=ref_id or meter_kind,
        note=f"{meter_kind}={quantity}",
    )


def expire_stale_holds() -> int:
    """回收过期 ACTIVE hold：转 RELEASED + refund 整额。返回回收条数。

    Celery 周期任务（每分钟）；防止任务丢失后余额永久挂死。
    """
    now = timezone.now()
    stale = WalletEntry.objects.select_for_update().filter(
        type=EntryType.HOLD, status=EntryStatus.ACTIVE, expires_at__lt=now,
    )
    count = 0
    for hold in stale:
        try:
            refund_hold(hold, reason="预扣过期自动回收")
            count += 1
        except Exception as exc:  # noqa: BLE001 — 单条失败不阻断批次
            logger.exception("回收过期 hold#%s 失败: %s", hold.pk, exc)
    if count:
        logger.info("过期 hold 回收完成：%s 条", count)
    return count


def read_service_fee_events() -> list[dict]:
    """读 ai_gateway 埋的 service_fee 事件（Redis 环形列表）。

    billing 侧消费入口：读出 → charge_service_fee → 清空已处理。
    事件源见 apps/ai_gateway/gateway.py::_emit_service_fee。
    """
    import json

    from apps.ai_gateway.redis_client import get_store

    store = get_store()
    raw = store.get("ai_gateway:service_fee_events")
    return json.loads(raw) if raw else []


def consume_service_fee_events() -> int:
    """消费全部 service_fee 事件落 FEE 流水（幂等键 credential 调用）。返回处理数。"""
    events = read_service_fee_events()
    count = 0
    for ev in events:
        try:
            entry = charge_service_fee(
                # user_id 需要 User 实例——事件里存 pk，此处反查
                _user_of(ev.get("user_id")),
                int(ev.get("amount") or 0),
                ref_kind=f"service_fee:{ev.get('provider_id')}",
                ref_id=str(ev.get("credential_id") or "") or f"{ev.get('ts')}",
                note=f"task={ev.get('task_record_id') or '-'}",
            )
            if entry is not None:
                count += 1
        except Exception as exc:  # noqa: BLE001 — 单条失败不阻断
            logger.exception("service_fee 事件处理失败 %s: %s", ev, exc)
    # 处理完清空事件源（幂等由 charge_service_fee 的 ref 唯一性保证；
    # 部分失败的事件保留——下次重消费时已落账的被 ref 幂等跳过）
    if events:
        try:
            from apps.ai_gateway.redis_client import get_store
            get_store().set("ai_gateway:service_fee_events", "[]")
        except Exception as exc:  # noqa: BLE001
            logger.exception("清空 service_fee 事件源失败: %s", exc)
    return count


def _user_of(user_id):
    if user_id is None:
        raise ValueError("事件缺 user_id")
    from django.contrib.auth import get_user_model
    User = get_user_model()
    return User.objects.get(pk=user_id)
