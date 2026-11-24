"""三态熔断器：CLOSED / OPEN / HALF_OPEN。

状态存储于 Redis（多 Celery worker 共享），通过 redis_client 抽象。
CLOSED 连续错误数达阈值 -> OPEN；OPEN 冷却后 -> HALF_OPEN（放行 1 个探测）；
探测成功 -> CLOSED；探测失败 -> OPEN（冷却时间翻倍）。
"""
import time

from .redis_client import get_store

CLOSED = "CLOSED"
OPEN = "OPEN"
HALF_OPEN = "HALF_OPEN"
# 探测锁 TTL：持有探测名额的 worker 崩溃时自动释放
PROBE_TTL = 300


class CircuitBreaker:
    def __init__(
        self,
        store=None,
        error_threshold=5,
        cooldown_seconds=30,
        max_cooldown_seconds=300,
    ):
        self.store = store or get_store()
        self.error_threshold = error_threshold
        self.cooldown_seconds = cooldown_seconds
        self.max_cooldown_seconds = max_cooldown_seconds

    # ── keys ──────────────────────────────────────────────────────────────
    def _key(self, provider_id, suffix):
        return f"cb:{provider_id}:{suffix}"

    def state(self, provider_id):
        return self.store.get(self._key(provider_id, "state")) or CLOSED

    def consecutive_errors(self, provider_id):
        return int(self.store.get(self._key(provider_id, "errors")) or 0)

    # ── core API ──────────────────────────────────────────────────────────
    def is_available(self, provider_id):
        """只读判断：当前是否可能放行（不改变状态、不占用探测名额）。

        用于构建候选链；真正发起调用前必须再调用 allow_request。
        """
        state = self.state(provider_id)
        if state == CLOSED:
            return True
        if state == OPEN and not self._cooldown_elapsed(provider_id):
            return False
        return self.store.get(self._key(provider_id, "probe")) is None

    def allow_request(self, provider_id):
        """Whether a request may be sent now. 放行探测时会原子占用唯一名额。

        调用方拿到 True 后必须以 record_success / record_failure / release_probe
        之一收尾；探测锁带 TTL，worker 崩溃时也会自动释放。
        """
        state = self.state(provider_id)
        if state == CLOSED:
            return True
        if state == OPEN and not self._cooldown_elapsed(provider_id):
            return False
        # OPEN 冷却结束 或 HALF_OPEN：SET NX 抢占唯一探测名额，避免并发穿透
        if not self.store.set_nx(self._key(provider_id, "probe"), "1", ex=PROBE_TTL):
            return False
        self._set_state(provider_id, HALF_OPEN)
        return True

    def release_probe(self, provider_id):
        """探测名额已占用但未实际调用（如被限流拒绝）时归还。"""
        if self.state(provider_id) == HALF_OPEN:
            self.store.delete(self._key(provider_id, "probe"))

    def record_success(self, provider_id):
        self.store.set(self._key(provider_id, "errors"), 0)
        self._set_state(provider_id, CLOSED)
        self.store.delete(self._key(provider_id, "probe"))
        self.store.delete(self._key(provider_id, "transition"))
        self.store.delete(self._key(provider_id, "cooldown_until"))
        self.store.delete(self._key(provider_id, "cooldown_len"))

    def record_failure(self, provider_id):
        # 原子 INCR：多 worker 并发失败时不丢计数（读-改-写会互相覆盖）
        errors = self.store.incr(self._key(provider_id, "errors"))
        state = self.state(provider_id)
        if state == HALF_OPEN:
            # 探测失败 -> OPEN，冷却翻倍（探测名额 set_nx 唯一，天然单发）
            self._open(provider_id, double=True)
        elif state == CLOSED and errors >= self.error_threshold:
            # CLOSED->OPEN 跃迁用 set_nx 抢占本周期唯一名额：多个 worker 并发越界
            # （同时 INCR 过阈值、读到同一 CLOSED 态）时只有抢到者执行 _open，
            # 不重复后推 cooldown_until；抢占者中途崩溃名额随 TTL 过期自愈。
            if self.store.set_nx(self._key(provider_id, "transition"), "1",
                                ex=self.cooldown_seconds):
                self._open(provider_id, double=False)
        # 已 OPEN：在途请求的迟到失败不重置冷却，避免抹掉已升级的冷却时长

    def reset(self, provider_id):
        for suffix in ("state", "errors", "cooldown_until", "cooldown_len", "probe",
                       "transition"):
            self.store.delete(self._key(provider_id, suffix))

    # ── internals ─────────────────────────────────────────────────────────
    def _open(self, provider_id, double=False):
        if double:
            current = min(self.current_cooldown(provider_id) * 2, self.max_cooldown_seconds)
        else:
            current = self.cooldown_seconds
        self.store.set(self._key(provider_id, "cooldown_len"), str(current))
        self.store.set(self._key(provider_id, "cooldown_until"), str(time.time() + current))
        self._set_state(provider_id, OPEN)
        self.store.delete(self._key(provider_id, "probe"))

    def current_cooldown(self, provider_id):
        """最近一次打开时使用的冷却时长（秒），未打开过则为基础值。"""
        raw = self.store.get(self._key(provider_id, "cooldown_len"))
        try:
            return float(raw) if raw else float(self.cooldown_seconds)
        except (TypeError, ValueError):
            return float(self.cooldown_seconds)

    def _cooldown_elapsed(self, provider_id):
        until = self.store.get(self._key(provider_id, "cooldown_until"))
        if not until:
            return True
        return time.time() >= float(until)

    def _set_state(self, provider_id, state):
        self.store.set(self._key(provider_id, "state"), state)
