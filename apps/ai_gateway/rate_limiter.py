"""Redis 限流：RPM（每分钟请求数）与并发数控制。

- RPM: INCR rl:{id}:rpm + EXPIRE 60s，超 max_rpm 拒绝
- 并发: ZSET 租约（成员=每次 acquire 的唯一 token，score=过期时间戳），
  acquire 时先清理过期成员——进程崩溃泄漏的槽位到租约期自动回收，
  不依赖请求流量维持 TTL；release 按 token 精确删除（幂等）。
- acquire 支持短时排队等待，超时后返回 None（由调用方降级）
"""
import time
import uuid

from django.conf import settings

from .redis_client import get_store


class RateLimiter:
    def __init__(self, store=None):
        self.store = store or get_store()

    @staticmethod
    def _rpm_key(provider_id):
        return f"rl:{provider_id}:rpm"

    @staticmethod
    def _conc_key(provider_id):
        return f"rl:{provider_id}:conc"

    @staticmethod
    def _lease_seconds() -> int:
        try:
            lease = int(getattr(settings, "RATE_LIMIT_LEASE_SECONDS", 600))
        except (TypeError, ValueError):
            lease = 600
        return lease if lease > 0 else 600

    def current_rpm(self, provider_id):
        return int(self.store.get(self._rpm_key(provider_id)) or 0)

    def current_concurrent(self, provider_id):
        """当前并发租约数（含未过期的成员）。"""
        return self.store.zcard(self._conc_key(provider_id))

    def try_acquire(self, provider_id, max_rpm=0, max_concurrent=0):
        """Try to reserve a slot.

        Returns lease token on success (falsy "" 表示不限并发的成功)，
        None 表示拒绝。先占并发租约、后占 RPM：并发被拒时不消耗 RPM 名额；
        RPM 被拒时归还并发租约。调用方持 token 调 release。
        """
        conc_key = self._conc_key(provider_id)
        rpm_key = self._rpm_key(provider_id)
        token = None
        if max_concurrent:
            now = time.time()
            # 清理已过期成员（含崩溃泄漏的租约）后再计数
            self.store.zremrangebyscore(conc_key, 0, now)
            if self.store.zcard(conc_key) >= max_concurrent:
                return None
            token = f"{provider_id}:{uuid.uuid4().hex}"
            self.store.zadd(conc_key, token, now + self._lease_seconds())
        if max_rpm:
            rpm = self.store.incr(rpm_key)
            if rpm == 1 or self.store.ttl(rpm_key) < 0:
                # 固定 60s 窗口：只在窗口起点设置过期
                self.store.expire(rpm_key, 60)
            if rpm > max_rpm:
                self.store.decr(rpm_key)
                if token is not None:
                    self.store.zrem(conc_key, token)
                return None
        return token or ""

    def acquire(self, provider_id, max_rpm=0, max_concurrent=0, wait_seconds=5):
        """Acquire a slot, waiting up to wait_seconds. Returns lease token or None."""
        deadline = time.time() + wait_seconds
        while True:
            token = self.try_acquire(provider_id, max_rpm=max_rpm, max_concurrent=max_concurrent)
            if token is not None:
                return token
            if time.time() >= deadline:
                return None
            time.sleep(0.1)

    def release(self, provider_id, token):
        """释放并发租约（幂等：成员不存在即 no-op，不误减新持有者）。"""
        if not token:
            return
        self.store.zrem(self._conc_key(provider_id), token)
