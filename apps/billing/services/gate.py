"""派发闸：槽位-预扣顺序铁律的落地（design D3）。

任务派发路径统一插入：
  1. 并发槽位闸：拿不到 → 抛 QuotaExceeded（调用方决定排队/拒绝），不预扣
  2. hold_for_task：余额不足 → 抛 InsufficientPoints（402/4001），槽位已释放
  3. 派发成功：hold 挂 ACTIVE，任务结束 settle/refund

ENABLE_BILLING_HOLD=False：跳过槽位与预扣直接派发（应急开关）。
"""
from __future__ import annotations

import logging

from django.conf import settings

from .quotas import (
    QuotaExceeded, acquire_slot, check_daily_render_exhausted, release_slot,
    slot_available,
)
from .wallet import InsufficientPoints, HoldResult, hold_for_task

logger = logging.getLogger(__name__)


def dispatch_gate(owner, task_ref: tuple[str, str], estimate_points: int, *,
                  slot_kind: str | None = None, note: str = "") -> HoldResult | None:
    """派发闸：先槽位、后预扣；预扣失败释放槽位再抛出。

    参数：
      owner        属主（计费主体，决策 3）
      task_ref     (ref_kind, ref_id) 幂等键
      estimate_points 预扣额（分）；0/None = 免费任务（跳过 hold）
      slot_kind    并发槽位类别：None=不占槽位 / "render" / "ai"

    返回 HoldResult | None（无预扣）；QuotaExceeded=槽位满；InsufficientPoints=402。
    任务成功路径调用方应：release_slot + settle；
    失败路径：release_slot + refund_hold（见 TaskOutcome 助手）。
    """
    if not getattr(settings, "ENABLE_BILLING_HOLD", True):
        return None  # 应急开关：直接派发（不占槽位不预扣）

    # 工作台 Stage C1（任务 6.5）：local 模式禁用 billing 调度路径——
    # 不产生预扣/钱包副作用（个人本机创作无计费主体）；模块保留，shared 接线不变。
    if getattr(settings, "IS_LOCAL_MODE", False):
        return None

    slot_acquired = False
    try:
        if slot_kind:
            if not acquire_slot(owner, slot_kind):
                raise QuotaExceeded(
                    f"{slot_kind}_slots", f"并发 {slot_kind} 槽位已满，请稍后重试")
            slot_acquired = True
        if estimate_points and estimate_points > 0:
            result = hold_for_task(owner, task_ref, estimate_points, note=note)
            if result is None:
                # 同 ref 的既有 hold 已终态：任务重复派发，不再扣费
                logger.warning("hold_for_task 幂等终止 ref=%s", task_ref)
            return result
        return None
    except InsufficientPoints:
        if slot_acquired:
            release_slot(owner, slot_kind)  # 预扣失败：立即归还槽位
        raise
    except QuotaExceeded:
        raise  # 槽位未拿到，无需归还


def release_after_dispatch(owner, result: HoldResult | None, slot_kind: str | None):
    """派发完成后的槽位释放（hold 保持冻结直到任务结束）。"""
    if slot_kind and getattr(settings, "ENABLE_BILLING_HOLD", True):
        release_slot(owner, slot_kind)


def preflight(owner, estimate_points: int, *, slot_kind: str | None = None,
              daily_seconds: int | None = None,
              daily_exhausted_only: bool = False) -> None:
    """同步启动预检（Play start 用）：只读，不写 WalletEntry、不改余额终态。

    顺序与 dispatch_gate 一致：每日渲染时长 → 并发槽位 → 余额。
    每日额度有两种只读语义：
      - daily_seconds > 0：按未来消耗预留检查（used + seconds > limit）；
      - daily_exhausted_only=True：只拒绝「已经用完」（used >= limit），
        供 OUTLINE 等非渲染任务使用（其异步派发本就不查日限）。
    槽位用只读计数比较（slot_available），不再 acquire+release 写入探针。
    拒绝时抛 QuotaExceeded（每日/槽位）或 InsufficientPoints（余额），
    与派发闸同款异常供视图映射 402/4001、402/4029、409/4090。

    Design D2 竞态登记：预检通过后、实际派发前余额/槽位可能被并发消耗，
    此时仍可能留下 FAILED TaskRecord + OUTLINING；不引入状态回退协议。
    """
    if not getattr(settings, "ENABLE_BILLING_HOLD", True):
        return
    if getattr(settings, "IS_LOCAL_MODE", False):
        return
    if owner is None:  # created_by 可空（历史/后台建剧）：与 dispatcher 一致跳过
        return

    if daily_exhausted_only:
        check_daily_render_exhausted(owner)
    elif daily_seconds:
        from .quotas import check_daily_render
        check_daily_render(owner, daily_seconds)

    if slot_kind and not slot_available(owner, slot_kind):
        raise QuotaExceeded(
            f"{slot_kind}_slots", f"并发 {slot_kind} 槽位已满，请稍后重试")

    if estimate_points and estimate_points > 0:
        from ..models import Wallet
        wallet = Wallet.objects.filter(user=owner).first()
        balance = wallet.balance if wallet is not None else 0
        if balance < estimate_points:
            raise InsufficientPoints(estimate_points, balance)


class TaskOutcome:
    """任务结束的两态处理助手（成功 settle / 失败 refund）。

    入参为 dispatch_gate 的返回值；幂等由 settle_task/refund_hold 内部保证
    （hold 已终态时重复调用 no-op）。
    """

    @staticmethod
    def success(hold_result: HoldResult | None, actual_points: int, note: str = ""):
        if hold_result is None:
            return None
        from .wallet import settle_task
        return settle_task(hold_result.entry, actual_points, note=note)

    @staticmethod
    def failure(hold_result: HoldResult | None, reason: str = ""):
        if hold_result is None:
            return None
        from .wallet import refund_hold
        return refund_hold(hold_result.entry, reason=reason)
