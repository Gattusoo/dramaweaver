# -*- coding: utf-8 -*-
"""实时事件通道测试（openspec change: backend-realtime-events）。

覆盖：SQLiteStreams 语义（ID 单调/续读/截断/TTL/跨实例）、后端三态选择、
指令队列（运行中提交/队列满/注入/消费幂等/取消轮次不进新轮次）、
interrupt 复用取消语义、开关关闭零回归。
"""
import json
import time
from unittest import mock

from django.test import TestCase, override_settings

from apps.creation.assistant import events
from apps.creation.assistant import runner as R
from apps.creation.assistant.models import (
    AssistantEvent, AssistantMessage, AssistantSession, MessageRole,
)
from apps.creation.models import TaskRecord, TaskStatus, TaskType

from .test_assistant import AssistantFixtureMixin, FakeChatGateway, _say, _use

# ── SQLiteStreams（真实 DB）─────────────────────────────────────────────────


class SQLiteStreamsTests(TestCase):
    def setUp(self):
        self.be = events.SQLiteStreams()
        self.key = "assistant:session:999"
        AssistantEvent.objects.all().delete()

    def tearDown(self):
        AssistantEvent.objects.all().delete()

    def test_id_monotonic(self):
        a = self.be.append(self.key, {"type": "message"})
        b = self.be.append(self.key, {"type": "tool_call"})
        c = self.be.append(self.key, {"type": "message"})
        self.assertLess(a, b)
        self.assertLess(b, c)
        # 同毫秒进位：强制同毫秒（ID 解析后 ms 相同则 seq 递增）
        ms_a = int(a.split("-")[0])
        ms_c = int(c.split("-")[0])
        self.assertGreaterEqual(ms_c, ms_a)

    def test_read_after_cursor(self):
        s1 = self.be.append(self.key, {"type": "a"})
        s2 = self.be.append(self.key, {"type": "b"})
        s3 = self.be.append(self.key, {"type": "c"})
        items = self.be.read(self.key, s1)
        self.assertEqual([i[0] for i in items], [s2, s3])
        items = self.be.read(self.key, s3)
        self.assertEqual(items, [])

    def test_invalid_cursor_reads_all(self):
        self.be.append(self.key, {"type": "a"})
        items = self.be.read(self.key, "0-0")
        self.assertEqual(len(items), 1)

    def test_maxlen_truncate(self):
        for i in range(events.MAX_LEN + 10):
            self.be.append(self.key, {"type": "message", "i": i})
        self.assertEqual(AssistantEvent.objects.filter(session_id=999).count(), events.MAX_LEN)
        # 最新事件仍可读：从 0-0 按 cursor 分批续读到末尾，末条 = last_id
        last_id = self.be.last_id(self.key)
        seen, cursor = [], "0-0"
        while True:
            items = self.be.read(self.key, cursor)
            if not items:
                break
            seen.extend(items)
            cursor = items[-1][0]
        self.assertEqual(len(seen), events.MAX_LEN)
        self.assertEqual(seen[-1][0], last_id)
        self.assertEqual(seen[0][1]["i"], 10)  # 最旧 10 条已被截断
        # 读完游标后不再返回（续读）
        items = self.be.read(self.key, last_id)
        self.assertEqual(items, [])

    def test_isolation_between_sessions(self):
        self.be.append("assistant:session:999", {"type": "a"})
        self.be.append("assistant:session:998", {"type": "b"})
        self.assertEqual(len(self.be.read("assistant:session:999", "0-0")), 1)
        self.assertEqual(len(self.be.read("assistant:session:998", "0-0")), 1)
        self.assertEqual(self.be.last_id("assistant:session:997"), "0-0")

    def test_cross_instance_visibility(self):
        """模拟双进程：两个 backend 实例同库读写。"""
        be2 = events.SQLiteStreams()
        sid = self.be.append(self.key, {"type": "from_worker"})
        items = be2.read(self.key, "0-0")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0][0], sid)
        self.assertEqual(items[0][1]["type"], "from_worker")

    def test_read_with_block_ms(self):
        self.be.append(self.key, {"type": "instant"})
        t0 = time.time()
        items = self.be.read(self.key, "0-0", block_ms=200)
        self.assertEqual(len(items), 1)
        self.assertLess(time.time() - t0, 1.0)  # 有事件立即返回

    def test_ttl_cleanup_on_append(self):
        """超过 24h 的事件在下一次 append 时清理。"""
        from datetime import timedelta
        from django.utils import timezone
        self.be.append(self.key, {"type": "old"})
        AssistantEvent.objects.filter(session_id=999).update(
            created_at=timezone.now() - timedelta(seconds=events.TTL_SECONDS + 60))
        self.be.append(self.key, {"type": "new"})
        remaining = AssistantEvent.objects.filter(session_id=999)
        self.assertEqual(remaining.count(), 1)
        self.assertEqual(remaining.first().data["type"], "new")


# ── 后端选择 ────────────────────────────────────────────────────────────────


class BackendSelectionTests(TestCase):
    def tearDown(self):
        events.set_backend(None)

    def test_local_with_flag_uses_sqlite(self):
        with override_settings(DEPLOYMENT_MODE="local", ENABLE_REALTIME_EVENTS=True,
                               AI_GATEWAY_STORE="memory"):
            be = events._build_backend()
        self.assertIsInstance(be, events.SQLiteStreams)

    def test_flag_off_uses_memory(self):
        with override_settings(DEPLOYMENT_MODE="local", ENABLE_REALTIME_EVENTS=False,
                               AI_GATEWAY_STORE="memory"):
            be = events._build_backend()
        self.assertIsInstance(be, events.MemoryStreams)

    def test_shared_no_redis_uses_memory(self):
        with override_settings(DEPLOYMENT_MODE="shared", ENABLE_REALTIME_EVENTS=True,
                               AI_GATEWAY_STORE="memory"):
            be = events._build_backend()
        self.assertIsInstance(be, events.MemoryStreams)

    def test_redis_wins_when_configured(self):
        # Redis 配置但连接失败 → 降级 memory（local+flag 也不接管：Redis 分支已处理）
        with override_settings(AI_GATEWAY_STORE="redis", REDIS_URL="redis://invalid:6379/0",
                               DEPLOYMENT_MODE="local", ENABLE_REALTIME_EVENTS=True):
            be = events._build_backend()
        self.assertIsInstance(be, events.MemoryStreams)


# ── 配置接线（任务 3.3：settings/env 模板静态验证）────────────────────────────


class FeatureFlagWiringTests(TestCase):
    """开关默认关闭 + env 模板含键（与 test_gen_params 模板测试同一模式）。"""

    def test_settings_defaults(self):
        """默认 False（关闭时零行为变化）；TURN_QUEUE_MAX 默认 3。"""
        from django.conf import settings
        self.assertIs(settings.ENABLE_REALTIME_EVENTS, False)
        self.assertEqual(settings.TURN_QUEUE_MAX, 3)

    def test_env_template_has_realtime_keys(self):
        """conf/env.example.py 可加载且含实时通道配置键。"""
        import conf
        from pathlib import Path
        template = Path(conf.__file__).parent / "env.example.py"
        self.assertTrue(template.is_file())
        import importlib.util
        spec = importlib.util.spec_from_file_location("_tpl_env_realtime", template)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.ENABLE_REALTIME_EVENTS, "false")
        self.assertEqual(mod.TURN_QUEUE_MAX, "3")


# ── 指令队列（视图分支 + runner 注入）───────────────────────────────────────

TURN_QUEUE_FLAGS = {"ENABLE_REALTIME_EVENTS": True}


class QueuedMessageTests(AssistantFixtureMixin, TestCase):
    """复用 assistant fixture：会话/播放/成员/权限齐全。"""

    def _start_turn(self):
        record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                           input_summary={"session_id": self.session.pk})
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        return record

    def _post(self, text):
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.producer)
        return client.post(f"/api/assistant/sessions/{self.session.pk}/messages/",
                           {"text": text}, format="json")

    @override_settings(**TURN_QUEUE_FLAGS)
    def test_queue_message_when_running(self):
        record = self._start_turn()
        resp = self._post("把节奏加快")
        self.assertEqual(resp.status_code, 202, resp.data)
        self.assertTrue(resp.data["queued"])
        self.assertEqual(resp.data["task_record_id"], record.pk)
        msg = AssistantMessage.objects.get(seq=resp.data["seq"])
        self.assertEqual(msg.role, MessageRole.USER)
        self.assertEqual(msg.queued_on_id, record.pk)

    def test_running_rejects_when_flag_off(self):
        record = self._start_turn()
        resp = self._post("把节奏加快")
        self.assertEqual(resp.status_code, 409)
        self.assertIn("上一轮", resp.data["detail"])

    @override_settings(**TURN_QUEUE_FLAGS)
    def test_queue_full_conflict(self):
        self._start_turn()
        for i in range(3):
            resp = self._post(f"指令{i}")
            self.assertEqual(resp.status_code, 202, resp.data)
        resp = self._post("第4条")
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("reason"), "queue_full")

    @override_settings(**TURN_QUEUE_FLAGS)
    def test_consumed_free_queue_slots(self):
        """消费（queued_on 置空）后队列槽位释放。"""
        record = self._start_turn()
        for i in range(3):
            self._post(f"指令{i}")
        AssistantMessage.objects.filter(queued_on=record).update(queued_on=None)
        resp = self._post("新指令")
        self.assertEqual(resp.status_code, 202, resp.data)


class QueuedInjectionTests(AssistantFixtureMixin, TestCase):
    """runner 端到端：队列指令合并进 user 内容、消费置空、取消轮次不进新轮次。"""

    def _turn(self, gw, *, flags=None, queued_texts=()):
        record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                           input_summary={"session_id": self.session.pk})
        R._save(self.session, MessageRole.USER, [{"type": "text", "text": "改一下"}], record)
        for t in queued_texts:
            m = R._save(self.session, MessageRole.USER, [{"type": "text", "text": t}], record)
            AssistantMessage.objects.filter(pk=m.pk).update(queued_on=record)
        from apps.creation.assistant.models import AssistantSession
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        from unittest.mock import patch
        with override_settings(**(flags or {})), patch(
                "apps.ai_gateway.gateway.AIGateway", return_value=gw):
            R.run_assistant_turn(record.pk)
        record.refresh_from_db()
        return record

    @override_settings(**TURN_QUEUE_FLAGS)
    def test_injected_into_user_content(self):
        gw = FakeChatGateway([_say("收到")])
        record = self._turn(gw, queued_texts=("把节奏加快",))
        self.assertEqual(record.status, TaskStatus.SUCCESS, record.output_summary)
        # 消费标记置空（指令归属本轮，已并入历史）
        queued = AssistantMessage.objects.filter(queued_on=record).count()
        self.assertEqual(queued, 0)
        # 模型历史包含队列文本（相邻 USER 消息合并为连续 user 块）
        first_call = gw.calls[0]
        texts = [b.get("text", "") for m in first_call for b in m.get("content", [])
                 if isinstance(b, dict) and b.get("type") == "text"]
        self.assertTrue(any("把节奏加快" in t for t in texts), texts)
        # 同一 user 消息内含"改一下"和队列文本（相邻合并，非连续 user 块）
        user_messages = [m for m in first_call if m.get("role") == "user"]
        merged = [m for m in user_messages if any(
            isinstance(b, dict) and b.get("type") == "text" and "把节奏加快" in b.get("text", "")
            for b in m.get("content", []))]
        self.assertTrue(merged, "队列指令应并入 user 消息")

    @override_settings(**TURN_QUEUE_FLAGS)
    def test_injection_idempotent(self):
        """同一轮重复消费不再追加（queued_on 已置空）。"""
        gw = FakeChatGateway([_say("收到")])
        record = self._turn(gw, queued_texts=("指令A",))
        # 消费后模型历史中"指令A"出现一次（相邻合并）
        first_call = gw.calls[0]
        texts = [b.get("text", "") for m in first_call for b in m.get("content", [])
                 if isinstance(b, dict) and b.get("type") == "text"]
        self.assertEqual(sum(t.count("指令A") for t in texts), 1)
        # 再次消费：空操作
        R._consume_queued_instructions(self.session, record)
        user_msg = self.session.messages.filter(role=MessageRole.USER, task_record=record).order_by("seq").first()
        self.assertIsNone(user_msg.queued_on_id)

    @override_settings(**TURN_QUEUE_FLAGS)
    def test_cancelled_turn_instructions_not_in_next_turn(self):
        """取消轮次的未消费指令不进新轮次。"""
        # 第 1 轮：队列指令在运行中提交后轮次被取消（模拟：模型调用前 record 置 CANCELLED）
        record1 = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                            input_summary={"session_id": self.session.pk})
        R._save(self.session, MessageRole.USER, [{"type": "text", "text": "第一轮"}], record1)
        m = R._save(self.session, MessageRole.USER, [{"type": "text", "text": "被遗弃指令"}], record1)
        AssistantMessage.objects.filter(pk=m.pk).update(queued_on=record1)
        # 直接取消（不执行循环）
        TaskRecord.objects.filter(pk=record1.pk).update(status=TaskStatus.CANCELLED)
        # 第 2 轮：正常执行
        gw = FakeChatGateway([_say("第二轮")])
        record2 = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                            input_summary={"session_id": self.session.pk})
        R._save(self.session, MessageRole.USER, [{"type": "text", "text": "第二轮"}], record2)
        from apps.creation.assistant.models import AssistantSession
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record2)
        from unittest.mock import patch
        with override_settings(**TURN_QUEUE_FLAGS), patch(
                "apps.ai_gateway.gateway.AIGateway", return_value=gw):
            R.run_assistant_turn(record2.pk)
        # 模型历史不含被遗弃指令
        texts = [b.get("text", "") for msg in gw.calls[0] for b in msg.get("content", [])
                 if isinstance(b, dict) and b.get("type") == "text"]
        self.assertFalse(any("被遗弃指令" in t for t in texts), texts)
        # 指令保留归属标记（审计）
        m.refresh_from_db()
        self.assertEqual(m.queued_on_id, record1.pk)

    def test_no_injection_when_flag_off(self):
        gw = FakeChatGateway([_say("收到")])
        record = self._turn(gw, queued_texts=("不该注入",), flags={"ENABLE_REALTIME_EVENTS": False})
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        # 开关关闭时消费为空操作：queued_on 保持不变（不消费、不注入）
        m = self.session.messages.filter(role=MessageRole.USER, task_record=record).order_by("-seq").first()
        self.assertEqual(m.queued_on_id, record.pk)


# ── WebSocket Consumer（Channels Communicator 集成）───────────────────────────

from channels.db import database_sync_to_async  # noqa: E402
from channels.testing.websocket import WebsocketCommunicator  # noqa: E402
from django.test import TransactionTestCase  # noqa: E402
from rest_framework_simplejwt.tokens import RefreshToken  # noqa: E402


def _ws_app():
    """URLRouter + consumer（与生产 asgi 一致：注入 url_route kwargs）。"""
    from channels.routing import URLRouter
    from django.urls import path
    from apps.creation.assistant.consumers import AssistantConsumer
    return URLRouter([
        path("api/assistant/sessions/<int:session_id>/ws/", AssistantConsumer.as_asgi()),
    ])


class ConsumerTests(AssistantFixtureMixin, TransactionTestCase):
    """WS 端点：JWT 鉴权/归属、cursor 回放与实时推送、断线重连、interrupt 三态。

    TransactionTestCase：asyncio.to_thread 中 ORM 需要真实提交可见。
    """

    @database_sync_to_async
    def _url(self, user=None, cursor=None, session_id=None):
        token = str(RefreshToken.for_user(user or self.producer).access_token)
        qs = f"token={token}"
        if cursor:
            qs += f"&cursor={cursor}"
        return f"/api/assistant/sessions/{session_id or self.session.pk}/ws/?{qs}"

    async def _connect(self, url):
        communicator = WebsocketCommunicator(_ws_app(), url)
        with override_settings(ENABLE_REALTIME_EVENTS=True):
            connected, code = await communicator.connect()
        return communicator, connected, code

    @database_sync_to_async
    def _emit(self, event_type, data=None):
        return events.emit(self.session.pk, event_type, data or {})

    # ── 鉴权与归属 ────────────────────────────────────────────────────────────

    async def test_connect_ok_and_flag_off_rejected(self):
        comm, connected, _ = await self._connect(await self._url())
        self.assertTrue(connected)
        await comm.disconnect()
        # 开关关闭 → 4404
        comm2 = WebsocketCommunicator(_ws_app(), await self._url())
        with override_settings(ENABLE_REALTIME_EVENTS=False):
            connected, code = await comm2.connect()
        self.assertFalse(connected)
        self.assertEqual(code, 4404)

    async def test_invalid_token_and_cross_user_rejected(self):
        comm, connected, code = await self._connect(
            f"/api/assistant/sessions/{self.session.pk}/ws/?token=bad")
        self.assertFalse(connected)
        self.assertEqual(code, 4403)
        # 其他用户（viewer 非会话创建者）→ 4403
        comm2, connected, code = await self._connect(await self._url(user=self.viewer))
        self.assertFalse(connected)
        self.assertEqual(code, 4403)
        # 不存在的会话 → 4404
        comm3, connected, code = await self._connect(await self._url(session_id=999999))
        self.assertFalse(connected)
        self.assertEqual(code, 4404)

    # ── 事件推送与断线重连 ───────────────────────────────────────────────────

    async def test_backlog_replay_and_live_events(self):
        await self._emit("message", {"text": "历史1"})
        await self._emit("message", {"text": "历史2"})
        comm, connected, _ = await self._connect(await self._url())
        self.assertTrue(connected)
        e1 = await comm.receive_json_from(timeout=3)
        e2 = await comm.receive_json_from(timeout=3)
        self.assertEqual([e1["data"]["text"], e2["data"]["text"]], ["历史1", "历史2"])
        # 实时事件
        await self._emit("tool_call", {"name": "x"})
        e3 = await comm.receive_json_from(timeout=3)
        self.assertEqual(e3["type"], "tool_call")
        await comm.disconnect()

    async def test_cursor_resume_no_loss_no_dup(self):
        s1 = await self._emit("message", {"text": "一"})
        await self._emit("message", {"text": "二"})
        comm, connected, _ = await self._connect(await self._url(cursor=s1))
        self.assertTrue(connected)
        # cursor=s1：只回放 s1 之后的事件（不重复 s1）
        e = await comm.receive_json_from(timeout=3)
        self.assertEqual(e["data"]["text"], "二")
        self.assertTrue(await comm.receive_nothing(timeout=0.3))
        await comm.disconnect()
        # 断线重连：用已收事件 id 续订 → 不丢不重
        last_id = e["id"]
        await self._emit("message", {"text": "三"})
        comm2, connected, _ = await self._connect(await self._url(cursor=last_id))
        self.assertTrue(connected)
        e2 = await comm2.receive_json_from(timeout=3)
        self.assertEqual(e2["data"]["text"], "三")
        self.assertTrue(await comm2.receive_nothing(timeout=0.3))
        await comm2.disconnect()

    async def test_invalid_cursor_falls_back_to_start(self):
        await self._emit("message", {"text": "全部"})
        comm, connected, _ = await self._connect(await self._url(cursor="garbage"))
        self.assertTrue(connected)
        e = await comm.receive_json_from(timeout=3)
        self.assertEqual(e["data"]["text"], "全部")
        await comm.disconnect()

    # ── interrupt（D4：复用取消语义）─────────────────────────────────────────

    @database_sync_to_async
    def _start_running(self, status=TaskStatus.RUNNING):
        record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                           status=status, input_summary={"session_id": self.session.pk})
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        return record

    @database_sync_to_async
    def _refresh(self, *objs):
        for obj in objs:
            obj.refresh_from_db()

    async def test_interrupt_running(self):
        record = await self._start_running()
        comm, connected, _ = await self._connect(await self._url())
        self.assertTrue(connected)
        await comm.send_json_to({"type": "interrupt"})
        ack = await comm.receive_json_from(timeout=3)
        self.assertEqual(ack["type"], "interrupt_ack")
        self.assertEqual(ack["task_record_id"], record.pk)
        await self._refresh(record)
        self.assertEqual(record.status, TaskStatus.CANCELLED)
        await comm.disconnect()

    async def test_interrupt_pending_finishes_immediately(self):
        record = await self._start_running(status=TaskStatus.PENDING)
        comm, connected, _ = await self._connect(await self._url())
        self.assertTrue(connected)
        await comm.send_json_to({"type": "interrupt"})
        ack = await comm.receive_json_from(timeout=3)
        self.assertEqual(ack["type"], "interrupt_ack")
        await self._refresh(record, self.session)
        self.assertEqual(record.status, TaskStatus.CANCELLED)
        self.assertIsNotNone(record.completed_at)
        self.assertIsNone(self.session.running_task_id)
        # PENDING 直接收尾 emit cancelled + done
        seen = []
        while True:
            try:
                ev = await comm.receive_json_from(timeout=1)
                seen.append(ev["type"])
            except TimeoutError:
                break
            if "done" in seen:
                break
        self.assertIn("cancelled", seen)
        self.assertIn("done", seen)
        await comm.disconnect()

    async def test_interrupt_idle_and_unknown_type(self):
        comm, connected, _ = await self._connect(await self._url())
        self.assertTrue(connected)
        await comm.send_json_to({"type": "interrupt"})
        err = await comm.receive_json_from(timeout=3)
        self.assertEqual(err["type"], "error")
        self.assertEqual(err["code"], 409)
        await comm.send_json_to({"type": "whatever"})
        err2 = await comm.receive_json_from(timeout=3)
        self.assertEqual(err2["type"], "error")
        await comm.disconnect()
