"""资源限额闸（决策 7 / design D3）。

槽位-预扣顺序铁律：先并发槽位、再预扣积分——拿不到槽位不产生预扣；
预扣失败立即释放槽位（finally），杜绝互相卡死。

槽位计数（Redis）：key 带任务级 TTL（过期兜底）+ 结束原子释放（双保险）；
**不复用限流层的滑动清零逻辑**（已知缺陷：600s 空闲清零泄漏）。

每日渲染时长：属主日累计（Redis INCR + 当日 key），超限当日 4029。
"""
from __future__ import annotations

import logging
from datetime import date

from django.conf import settings

from apps.ai_gateway.redis_client import get_store

from ..models import UserQuotaOverride

logger = logging.getLogger(__name__)

# 槽位 key 前缀与 TTL 兜底（秒）：任务异常退出未释放时由过期兜底
_SLOT_KEY = "billing:slots:{kind}:{user_id}"
_SLOT_TTL_SECONDS = 3600
# 每日渲染累计 key（当日日期后缀，随日期自然滚动；TTL 48h 兜底清残留）
_DAILY_KEY = "billing:daily_render:{user_id}:{yyyymmdd}"
_DAILY_TTL_SECONDS = 48 * 3600


class QuotaExceeded(Exception):
    """限额超限。kind ∈ {"render_slots", "ai_slots", "daily_render"}。"""

    def __init__(self, kind: str, message: str = ""):
        self.kind = kind
        super().__init__(message or f"资源限额超限：{kind}")


def _override_of(user):
    """取用户的限额覆盖；未启用/不存在返回 None。"""
    o = getattr(user, "quota_override", None)
    if o is not None and o.override_enabled:
        return o
    return None


def effective_limits(user) -> dict:
    """解析生效限额（覆盖表优先，字段空回退全局默认）。"""
    o = _override_of(user)
    g = settings
    if o is None:
        return {
            "max_concurrent_render": getattr(g, "MAX_CONCURRENT_RENDER", 2),
            "max_concurrent_ai": getattr(g, "MAX_CONCURRENT_AI", 10),
            "daily_render_seconds": getattr(g, "DAILY_RENDER_SECONDS_LIMIT", 3600),
        }
    return {
        "max_concurrent_render": o.max_concurrent_render
            if o.max_concurrent_render is not None else getattr(g, "MAX_CONCURRENT_RENDER", 2),
        "max_concurrent_ai": o.max_concurrent_ai
            if o.max_concurrent_ai is not None else getattr(g, "MAX_CONCURRENT_AI", 10),
        "daily_render_seconds": o.daily_render_seconds
            if o.daily_render_seconds is not None else getattr(g, "DAILY_RENDER_SECONDS_LIMIT", 3600),
    }


def _slot_count(store, kind, user_id) -> int:
    raw = store.get(_SLOT_KEY.format(kind=kind, user_id=user_id))
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def acquire_slot(user, kind: str) -> bool:
    """尝试占用一个并发槽位；满则 False（不排队——由 Celery 延迟重投表达排队）。

    kind ∈ {"render", "ai"}；限额 0 表示不限制。
    """
    limits = effective_limits(user)
    limit = limits.get(f"max_concurrent_{kind}", 0)
    if not limit:  # 0 = 不限制
        return True
    store = get_store()
    key = _SLOT_KEY.format(kind=kind, user_id=user.pk)
    current = _slot_count(store, kind, user.pk)
    if current >= limit:
        return False
    store.set(key, str(current + 1), ex=_SLOT_TTL_SECONDS)
    return True


def release_slot(user, kind: str) -> None:
    """释放一个槽位（幂等：减到 0 为止不越界）。"""
    store = get_store()
    key = _SLOT_KEY.format(kind=kind, user_id=user.pk)
    current = _slot_count(store, kind, user.pk)
    store.set(key, str(max(0, current - 1)), ex=_SLOT_TTL_SECONDS)


def slot_available(user, kind: str) -> bool:
    """只读探测：当前是否还有可用并发槽位（不写计数）。

    预检专用——避免 acquire+release 探针在非原子 get/set 计数上产生
    瞬时占用/覆盖竞态（见 2026-11-25 审查发现）。真正的占位仍由
    acquire_slot 在派发闸完成；预检与派发之间的 TOCTOU 由 design D2 登记。
    """
    limits = effective_limits(user)
    limit = limits.get(f"max_concurrent_{kind}", 0)
    if not limit:  # 0 = 不限制
        return True
    return _slot_count(get_store(), kind, user.pk) < limit


def daily_render_used(user_id) -> int:
    """当日已渲染秒数。"""
    store = get_store()
    key = _DAILY_KEY.format(user_id=user_id, yyyymmdd=date.today().strftime("%Y%m%d"))
    try:
        return int(store.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def consume_daily_render(user_id, seconds: int) -> int:
    """累计当日渲染时长（读改写，单写者场景），返回累计值。"""
    if seconds <= 0:
        return daily_render_used(user_id)
    store = get_store()
    key = _DAILY_KEY.format(user_id=user_id, yyyymmdd=date.today().strftime("%Y%m%d"))
    used = daily_render_used(user_id)
    store.set(key, str(used + seconds), ex=_DAILY_TTL_SECONDS)
    return used + seconds


def check_daily_render(user, seconds_to_consume: int) -> None:
    """渲染派发前检查日累计；超限抛 QuotaExceeded("daily_render")（业务码 4029）。"""
    limits = effective_limits(user)
    limit = limits.get("daily_render_seconds", 0)
    if not limit:
        return
    used = daily_render_used(user.pk)
    if used + seconds_to_consume > limit:
        raise QuotaExceeded(
            "daily_render",
            f"当日渲染时长已达上限（已用 {used}s / 限 {limit}s）",
        )


def check_daily_render_exhausted(user) -> None:
    """只读检查当日渲染额度是否**已经用完**（不预留未来时长）。

    用于 Play start 这类非渲染任务（OUTLINE 为 ai 槽位、零渲染秒）的同步
    预检：只有 used >= limit 才拒绝，避免「还有余额但不足 60s」被误判为
    额度用完（异步派发闸对非 render 任务本就不查日限）。
    """
    limits = effective_limits(user)
    limit = limits.get("daily_render_seconds", 0)
    if not limit:
        return
    used = daily_render_used(user.pk)
    if used >= limit:
        raise QuotaExceeded(
            "daily_render",
            f"当日渲染时长已达上限（已用 {used}s / 限 {limit}s）",
        )
