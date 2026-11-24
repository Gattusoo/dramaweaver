"""AssistantConsumer：助手会话 WebSocket（Phase 3.2 / backend-realtime-events D2–D4）。

- connect：JWT 鉴权（query token）+ 会话归属校验（仅会话创建者），拒绝 403
- 订阅：初始 cursor 续订（非法回 0-0）→ backlog 回放 → 实时轮询推送（1s 批 + 50ms 快连）
- interrupt（D4）：复用既有取消语义（TaskRecord CANCELLED；PENDING 直接收尾），不抢占模型调用
- 读循环统一走 events 三态后端（memory/redis/sqlite），不引入 channel layer
"""
import asyncio
import logging

from channels.generic.websocket import AsyncJsonWebsocketConsumer

logger = logging.getLogger(__name__)

READ_BATCH_WAIT_S = 1.0  # 每批最长等待（有事件时快速连读，空批 1s）
FAST_POLL_S = 0.05      # 批内轮询间隔（与 SQLiteStreams 一致）


class AssistantConsumer(AsyncJsonWebsocketConsumer):
    """单个会话的事件订阅端点。多连接允许（前端刷新场景），读取幂等。"""

    async def connect(self):
        from django.conf import settings
        if not getattr(settings, "ENABLE_REALTIME_EVENTS", False):
            await self.close(code=4404)
            return
        self.session_id = int(self.scope["url_route"]["kwargs"]["session_id"])
        self.cursor = self._normalize_cursor(
            self.scope.get("query_string", b"").decode("utf-8", "ignore"))
        self.user = None
        self._reading = False

        user, error = await self._authenticate()
        if user is None:
            await self.close(code=error or 4403)
            return
        self.user = user
        ok, error = await self._check_session()
        if not ok:
            await self.close(code=error or 4403)
            return
        await self.accept()
        # 读循环在后台推事件（不阻塞 connect 返回）；disconnect 时取消，避免泄漏
        self._read_task = asyncio.create_task(self._read_loop())

    # ── 鉴权与归属（D2）─────────────────────────────────────────────────────

    @staticmethod
    def _query_param(query_string, name):
        from urllib.parse import parse_qs
        params = parse_qs(query_string)
        values = params.get(name) or []
        return values[0] if values else None

    @classmethod
    def _normalize_cursor(cls, query_string):
        import re
        cursor = cls._query_param(query_string, "cursor") or "0-0"
        return cursor if re.match(r"^\d+-\d+$", cursor) else "0-0"

    async def _authenticate(self):
        """JWT（query token）→ (user, error_code)。失败返回 (None, 4403)。"""
        def _auth():
            from rest_framework_simplejwt.authentication import JWTAuthentication
            from django.contrib.auth.models import AnonymousUser
            token = self._query_param(
                self.scope.get("query_string", b"").decode("utf-8", "ignore"), "token")
            if not token:
                return None
            auth = JWTAuthentication()
            try:
                from rest_framework_simplejwt.exceptions import TokenError
                validated = auth.get_validated_token(token.encode())
                return auth.get_user(validated)
            except TokenError:
                return None
            except Exception:  # noqa: BLE001 — 鉴权失败一律匿名拒绝
                logger.exception("WS JWT 鉴权异常")
                return None
        user = await asyncio.to_thread(_auth)
        if user is None or getattr(user, "is_authenticated", False) is not True:
            return None, 4403
        return user, None

    async def _check_session(self):
        """会话存在且属于当前用户。"""
        def _check():
            from .models import AssistantSession
            session = AssistantSession.objects.filter(pk=self.session_id).first()
            if session is None:
                return False, 4404
            if session.user_id != self.user.pk:
                return False, 4403
            return True, None
        return await asyncio.to_thread(_check)

    # ── 读循环（D3）──────────────────────────────────────────────────────────

    async def _read_loop(self):
        try:
            while True:
                batch, self.cursor = await asyncio.to_thread(
                    self._read_batch, self.session_id, self.cursor)
                for event in batch:
                    await self.send_json(event)
                if not batch:
                    await asyncio.sleep(FAST_POLL_S)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — 读循环崩溃记录日志，不断连接重试
            logger.exception("WS 事件读循环异常 session=%s", self.session_id)
            await asyncio.sleep(1.0)
            self._read_task = asyncio.create_task(self._read_loop())

    @staticmethod
    def _read_batch(session_id, cursor):
        """同步读一批事件（在线程池执行，Django ORM/事件后端线程安全）。"""
        from . import events
        return events.read(session_id, cursor=cursor, wait=0)

    async def disconnect(self, code):
        task = getattr(self, "_read_task", None)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass

    # ── 客户端消息（D4）──────────────────────────────────────────────────────

    async def receive_json(self, content, **kwargs):
        msg_type = (content or {}).get("type")
        if msg_type == "interrupt":
            response = await asyncio.to_thread(self._interrupt)
            await self.send_json(response)
        else:
            await self.send_json({"type": "error", "detail": f"未知消息类型 {msg_type}"})

    def _interrupt(self):
        """复用既有取消语义（D4：不造第二套状态机）。

        RUNNING → CANCELLED（循环下迭代点退出）；PENDING → 直接收尾 emit；
        空闲 → 409 语义错误响应。不抢占进行中的模型调用。
        """
        from django.utils import timezone
        from . import events
        from .models import AssistantSession
        from apps.creation.models import TaskRecord, TaskStatus

        session = AssistantSession.objects.filter(pk=self.session_id).first()
        if session is None:
            return {"type": "error", "detail": "会话不存在"}
        task = session.running_task
        _TERMINAL = (TaskStatus.SUCCESS, TaskStatus.FAILED, TaskStatus.CANCELLED)
        if task is None or task.status in _TERMINAL:
            return {"type": "error", "detail": "没有运行中的轮次", "code": 409}
        # 与 POST /cancel/ 同一语义
        updated = TaskRecord.objects.filter(pk=task.pk).exclude(status__in=_TERMINAL).update(
            status=TaskStatus.CANCELLED)
        if updated and task.status == TaskStatus.PENDING:
            TaskRecord.objects.filter(pk=task.pk).update(completed_at=timezone.now())
            AssistantSession.objects.filter(pk=session.pk, running_task=task).update(running_task=None)
            events.emit(session.pk, "cancelled")
            events.emit(session.pk, "done", {"outcome": "cancelled"})
        return {"type": "interrupt_ack", "task_record_id": task.pk,
                "status": TaskStatus.CANCELLED}
