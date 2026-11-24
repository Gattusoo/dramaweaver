"""助手事件流（assistant/session，D5 / D8）。

事件 {type, data, ts}，type ∈ message | tool_call | tool_result | proposal | error |
budget_exceeded | cancelled | done。cursor 为 Redis Stream ID（"毫秒-序号"）。

后端：配置使用 Redis（settings.AI_GATEWAY_STORE=redis）时用 Redis Stream（XADD / XREAD BLOCK，
MAXLEN≈1000，key TTL 24h）；否则用进程内实现。无 Redis 时 Celery 只能 eager 同进程执行，
进程内实现与之语义一致。Stream 只用于实时推送，持久数据以 AssistantMessage 为准，
因此推送失败只记录日志，不影响 agent 循环。
"""
import json
import logging
import re
import threading
import time
from datetime import timedelta

from django.utils import timezone

logger = logging.getLogger(__name__)

TTL_SECONDS = 24 * 3600
MAX_LEN = 1000
MAX_WAIT = 25
_ID_RE = re.compile(r"^\d+-\d+$")


def stream_key(session_id):
    return f"assistant:session:{session_id}"


def _parse_id(sid):
    ms, seq = sid.split("-")
    return int(ms), int(seq)


def _format_id(ms, seq):
    """ID 定宽格式化（seq 零填充 6 位）：字符串排序 == 数值排序。

    Redis Stream ID "ms-seq" 无零填充但按二进制数值比较；SQLite 后端用
    字符串列存 ID（order_by/stream_id__gt 按字典序），无填充时 "X-9" >
    "X-10"——进位后 last 取错位、游标比较漏事件、唯一约束撞车。定宽后
    字典序与数值序一致。_parse_id 用 int() 解析，兼容存量无填充行。
    """
    return f"{ms}-{seq:06d}"


class MemoryStreams:
    """进程内 Stream：ID 单调递增，条件变量支持阻塞读。"""

    def __init__(self):
        self._data = {}
        self._last = (0, 0)
        self._cond = threading.Condition()

    def append(self, key, event):
        with self._cond:
            ms = int(time.time() * 1000)
            last_ms, last_seq = self._last
            self._last = (ms, 0) if ms > last_ms else (last_ms, last_seq + 1)
            sid = f"{self._last[0]}-{self._last[1]}"
            self._data.setdefault(key, []).append((sid, event))
            del self._data[key][:-MAX_LEN]
            self._cond.notify_all()
            return sid

    def _after(self, key, cursor):
        c = _parse_id(cursor)
        return [(sid, ev) for sid, ev in self._data.get(key, []) if _parse_id(sid) > c]

    def read(self, key, cursor, block_ms=0, count=100):
        deadline = time.time() + block_ms / 1000.0
        with self._cond:
            items = self._after(key, cursor)
            while not items and block_ms and time.time() < deadline:
                self._cond.wait(max(0.0, deadline - time.time()))
                items = self._after(key, cursor)
            return items[:count]

    def last_id(self, key):
        with self._cond:
            items = self._data.get(key) or []
            return items[-1][0] if items else "0-0"


class RedisStreams:
    def __init__(self, client):
        self._redis = client

    def append(self, key, event):
        sid = self._redis.xadd(key, {"e": json.dumps(event, ensure_ascii=False)}, maxlen=MAX_LEN, approximate=True)
        self._redis.expire(key, TTL_SECONDS)
        return sid.decode() if isinstance(sid, bytes) else sid

    def read(self, key, cursor, block_ms=0, count=100):
        resp = self._redis.xread({key: cursor}, count=count, block=block_ms or None)
        out = []
        for _, entries in resp or []:
            for sid, fields in entries:
                sid = sid.decode() if isinstance(sid, bytes) else sid
                raw = fields.get("e") or fields.get(b"e") or "{}"
                out.append((sid, json.loads(raw)))
        return out

    def last_id(self, key):
        items = self._redis.xrevrange(key, count=1)
        if not items:
            return "0-0"
        sid = items[0][0]
        return sid.decode() if isinstance(sid, bytes) else sid


class SQLiteStreams:
    """SQLite 事件表后端（Phase 3.2 / registry D1）：local 双进程跨进程事件。

    ID 为"毫秒-序号"（对齐 Redis Stream ID），同毫秒进位，写入单调递增；
    read 按 block_ms 分片 50ms 轮询（SQLite 无跨进程通知原语）。
    MAXLEN 截断与 TTL 清理在写入时顺带完成（摊销，索引删除）。
    """
    POLL_INTERVAL = 0.05

    def __init__(self):
        from .models import AssistantEvent
        self._model = AssistantEvent

    def _now_ms(self):
        return int(time.time() * 1000)

    def append(self, key, event):
        from django.db import transaction
        sid = self._session_id(key)
        with transaction.atomic():
            last = (self._model.objects.filter(session_id=sid)
                    .order_by("-stream_id").values_list("stream_id", flat=True).first())
            ms = self._now_ms()
            if last:
                last_ms, last_seq = _parse_id(last)
                new_id = _format_id(ms, 0) if ms > last_ms else _format_id(last_ms, last_seq + 1)
            else:
                new_id = _format_id(ms, 0)
            self._model.objects.create(session_id=sid, stream_id=new_id, data=event)
            # MAXLEN 截断 + TTL 清理（同会话摊销删除）
            keep = self._model.objects.filter(session_id=sid).order_by("-stream_id")[:MAX_LEN]
            keep_ids = [k["pk"] for k in keep.values("pk")]
            self._model.objects.filter(session_id=sid).exclude(pk__in=keep_ids).delete()
            cutoff = timezone.now() - timedelta(seconds=TTL_SECONDS)
            self._model.objects.filter(session_id=sid, created_at__lt=cutoff).delete()
        return new_id

    def read(self, key, cursor, block_ms=0, count=100):
        sid = self._session_id(key)
        deadline = time.time() + block_ms / 1000.0
        while True:
            items = list(self._model.objects.filter(session_id=sid, stream_id__gt=cursor)
                         .order_by("stream_id")[:count].values_list("stream_id", "data"))
            if items or not block_ms or time.time() >= deadline:
                return items
            time.sleep(self.POLL_INTERVAL)

    def last_id(self, key):
        sid = self._session_id(key)
        last = (self._model.objects.filter(session_id=sid)
                .order_by("-stream_id").values_list("stream_id", flat=True).first())
        return last or "0-0"

    @staticmethod
    def _session_id(key):
        # stream_key 形如 assistant:session:{id}
        try:
            return int(key.rsplit(":", 1)[-1])
        except ValueError:
            return key


_backend = None
_lock = threading.Lock()


def get_backend():
    global _backend
    if _backend is not None:
        return _backend
    with _lock:
        if _backend is None:
            _backend = _build_backend()
    return _backend


def _build_backend():
    from django.conf import settings
    if str(getattr(settings, "AI_GATEWAY_STORE", "memory")).lower() == "redis":
        try:
            import redis as redis_lib
            client = redis_lib.Redis.from_url(settings.REDIS_URL, decode_responses=True,
                                              socket_connect_timeout=2, socket_timeout=MAX_WAIT + 5)
            client.ping()
            return RedisStreams(client)
        except Exception as exc:  # noqa: BLE001
            logger.warning("助手事件流无法连接 Redis，改用进程内实现（跨进程推送不可用）: %s", exc)
            return MemoryStreams()
    # Phase 3.2（registry D1）：local 双进程跨进程事件缺口——SQLite 事件表后端。
    # 仅 local 模式且实时开关开启时启用；shared 无 Redis 保持进程内实现（现状）。
    if (str(getattr(settings, "DEPLOYMENT_MODE", "shared")).lower() == "local"
            and getattr(settings, "ENABLE_REALTIME_EVENTS", False)):
        return SQLiteStreams()
    return MemoryStreams()


def set_backend(backend):
    """测试注入（fakeredis → RedisStreams(fake)，或 MemoryStreams()）。"""
    global _backend
    _backend = backend


def emit(session_id, event_type, data=None):
    """写入事件，返回事件 ID；失败返回 None 并记录日志（不影响调用方）。"""
    event = {"type": event_type, "data": data if data is not None else {}, "ts": time.time()}
    try:
        return get_backend().append(stream_key(session_id), event)
    except Exception as exc:  # noqa: BLE001
        logger.warning("助手事件推送失败 session=%s type=%s: %s", session_id, event_type, exc)
        return None


def read(session_id, cursor="0-0", wait=0):
    """读 cursor 之后的事件 -> (events, new_cursor)。wait 秒内无事件则阻塞等待（上限 25 秒）。"""
    cursor = cursor if isinstance(cursor, str) and _ID_RE.match(cursor) else "0-0"
    wait = max(0, min(MAX_WAIT, int(wait or 0)))
    items = get_backend().read(stream_key(session_id), cursor, block_ms=wait * 1000)
    events = [{"id": sid, "seq": sid, **ev} for sid, ev in items]
    return events, (items[-1][0] if items else cursor)


def last_cursor(session_id):
    try:
        return get_backend().last_id(stream_key(session_id))
    except Exception as exc:  # noqa: BLE001
        logger.warning("读取事件流位置失败 session=%s: %s", session_id, exc)
        return "0-0"
