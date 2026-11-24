"""Redis 存储抽象。

熔断器与限流需要跨 Celery worker 共享状态，因此存储于 Redis。
若 Redis 不可用（本地开发 / 单测），自动降级为进程内内存存储，
保证功能可用且便于测试。
"""
import logging
import time
import uuid

logger = logging.getLogger(__name__)

_DEFAULT_URL = "redis://127.0.0.1:6379/0"


class MemoryStore:
    """进程内存储，接口与 RedisStore 对齐。"""

    def __init__(self):
        self._data = {}
        self._zsets = {}  # key -> {member: score}

    def get(self, key):
        item = self._data.get(key)
        if item is None:
            return None
        value, expires_at = item
        if expires_at is not None and expires_at < time.time():
            del self._data[key]
            return None
        return value

    def set(self, key, value, ex=None):
        expires_at = time.time() + ex if ex else None
        self._data[key] = (str(value), expires_at)

    def incr(self, key):
        current = int(self.get(key) or 0) + 1
        item = self._data.get(key)
        expires_at = item[1] if item else None
        self._data[key] = (str(current), expires_at)
        return current

    def decr(self, key):
        current = int(self.get(key) or 0) - 1
        if current < 0:
            current = 0
        item = self._data.get(key)
        expires_at = item[1] if item else None
        self._data[key] = (str(current), expires_at)
        return current

    def expire(self, key, seconds):
        item = self._data.get(key)
        if item is None:
            return False
        self._data[key] = (item[0], time.time() + seconds)
        return True

    def delete(self, key):
        self._data.pop(key, None)

    def exists(self, key):
        return self.get(key) is not None

    def set_nx(self, key, value, ex=None):
        """仅当 key 不存在时写入，返回是否写入成功。"""
        if self.get(key) is not None:
            return False
        self.set(key, value, ex=ex)
        return True

    def ttl(self, key):
        item = self._data.get(key)
        if item is None:
            return -2
        if item[1] is None:
            return -1
        return max(0, int(item[1] - time.time()))

    # ── ZSET（并发限流租约）──────────────────────────────────────────────
    # 语义与 redis-py 对齐：score 为过期时间戳，范围删按 score 比较。

    def _zset(self, key, create=False):
        z = self._zsets.get(key)
        if create and z is None:
            z = {}
            self._zsets[key] = z
        return z

    def zadd(self, key, member, score):
        """添加/覆盖成员；key 不存在则创建。返回是否新增（1=新增 0=覆盖）。"""
        z = self._zset(key, create=True)
        added = member not in z
        z[member] = float(score)
        return 1 if added else 0

    def zcard(self, key):
        return len(self._zset(key) or {})

    def zremrangebyscore(self, key, min_score, max_score):
        """删除 score ∈ [min, max] 的成员，返回删除数。"""
        z = self._zset(key)
        if not z:
            return 0
        stale = [m for m, s in z.items() if min_score <= s <= max_score]
        for m in stale:
            del z[m]
        return len(stale)

    def zrem(self, key, member):
        """删除成员，返回是否删除（0/1）。"""
        z = self._zset(key)
        if not z or member not in z:
            return 0
        del z[member]
        return 1


class RedisStore:
    """包装 redis.Redis 客户端，接口与 MemoryStore 对齐。

    运行中 Redis 断开时，单次操作降级到进程内 MemoryStore 并记录告警，
    避免熔断/限流的存储故障直接导致 AI 调用失败（fail-open 到本地状态）。
    `client` 可注入（如 fakeredis），便于在无 Redis 环境下验证真实语义。
    """

    def __init__(self, url=None, client=None):
        if client is None:
            import redis as redis_lib

            client = redis_lib.Redis.from_url(
                url or _DEFAULT_URL, decode_responses=True,
                socket_connect_timeout=1, socket_timeout=1, retry_on_timeout=False,
            )
        self._redis = client
        self._fallback = MemoryStore()

    def _call(self, op, *args, **kwargs):
        try:
            return getattr(self._redis, op)(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 — redis.RedisError / 连接错误
            logger.warning("Redis %s 失败，本次降级为内存存储: %s", op, exc)
            return _MISSING

    def ping(self):
        """启动探活：直接访问 Redis，失败抛异常（不走降级）。"""
        self._redis.ping()

    def get(self, key):
        value = self._call("get", key)
        return self._fallback.get(key) if value is _MISSING else value

    def set(self, key, value, ex=None):
        if self._call("set", key, value, ex=ex) is _MISSING:
            self._fallback.set(key, value, ex=ex)

    def incr(self, key):
        value = self._call("incr", key)
        return self._fallback.incr(key) if value is _MISSING else int(value)

    def decr(self, key):
        value = self._call("decr", key)
        return self._fallback.decr(key) if value is _MISSING else int(value)

    def expire(self, key, seconds):
        value = self._call("expire", key, seconds)
        return self._fallback.expire(key, seconds) if value is _MISSING else bool(value)

    def delete(self, key):
        self._fallback.delete(key)
        self._call("delete", key)

    def exists(self, key):
        value = self._call("exists", key)
        return self._fallback.exists(key) if value is _MISSING else bool(value)

    def set_nx(self, key, value, ex=None):
        result = self._call("set", key, value, ex=ex, nx=True)
        return self._fallback.set_nx(key, value, ex=ex) if result is _MISSING else bool(result)

    def ttl(self, key):
        value = self._call("ttl", key)
        return self._fallback.ttl(key) if value is _MISSING else int(value)

    # ── ZSET（并发限流租约）──────────────────────────────────────────────

    def zadd(self, key, member, score):
        value = self._call("zadd", key, {member: score})
        return self._fallback.zadd(key, member, score) if value is _MISSING else int(value)

    def zcard(self, key):
        value = self._call("zcard", key)
        return self._fallback.zcard(key) if value is _MISSING else int(value)

    def zremrangebyscore(self, key, min_score, max_score):
        value = self._call("zremrangebyscore", key, min_score, max_score)
        return self._fallback.zremrangebyscore(key, min_score, max_score) if value is _MISSING else int(value)

    def zrem(self, key, member):
        value = self._call("zrem", key, member)
        return self._fallback.zrem(key, member) if value is _MISSING else int(value)


_MISSING = object()


_store = None


def get_store(url=None):
    """Return a shared store, preferring Redis and falling back to memory.

    本地无 Redis 时避免反复重连：settings.AI_GATEWAY_STORE 为
      - "memory": 直接用内存，不尝试 Redis
      - "redis":  强制 Redis（不可用则内存）
      - "auto":   尝试 Redis（短超时），失败则内存并缓存该决定
    """
    global _store
    if _store is not None:
        return _store
    try:
        from django.conf import settings as _settings
        mode = getattr(_settings, "AI_GATEWAY_STORE", "auto").lower()
    except Exception:  # noqa: BLE001
        mode = "auto"
    if mode == "memory":
        logger.info("AI Gateway 使用内存存储（AI_GATEWAY_STORE=memory）")
        _store = MemoryStore()
        return _store
    redis_url = url
    if not redis_url:
        try:
            from django.conf import settings as _settings
            redis_url = getattr(_settings, "REDIS_URL", None)
        except Exception:  # noqa: BLE001
            redis_url = None
    try:
        store = RedisStore(redis_url)
        store.ping()  # RedisStore 操作自带降级，探活必须直连才能判断可用性
        logger.info("AI Gateway 使用 Redis 存储")
        _store = store
    except Exception as exc:  # noqa: BLE001
        logger.warning("Redis 不可用，降级为内存存储: %s", exc)
        _store = MemoryStore()
    return _store


def reset_store():
    """Reset the cached store (mainly for tests)."""
    global _store
    _store = None
