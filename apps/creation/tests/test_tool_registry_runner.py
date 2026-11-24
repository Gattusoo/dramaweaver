# -*- coding: utf-8 -*-
"""工具注册表 runner 端到端集成测试（openspec change: backend-tool-registry-sandbox）。

任务 3.3 验收：FakeChatGateway 轮次内模型调用注册表工具，经 run_tool 回退
→ 沙箱执行 → 结果回填 → 下一轮模型可见。覆盖：
- 注册表工具并入工具面（tools 参数含注册表工具 spec，开关关闭时不含）
- chain_tools 计入预算（链 + 各步）
"""
from unittest.mock import patch

import json

from django.test import TestCase, override_settings

from apps.creation.assistant import events, registry as R
from apps.creation.assistant.models import MessageRole, ToolRegistry, ToolType
from apps.creation.assistant.runner import _save, run_assistant_turn
from apps.creation.models import TaskRecord, TaskStatus, TaskType

from .test_assistant import AssistantFixtureMixin, FakeChatGateway, _say, _use


@override_settings(ENABLE_TOOL_REGISTRY=True)
class RunnerRegistryIntegrationTests(AssistantFixtureMixin, TestCase):
    """端到端：模型在轮次内调用注册表工具（FakeChatGateway + 真实沙箱）。"""

    def setUp(self):
        super().setUp()
        self.alice = self.producer  # fixture 的会话用户
        R._cache._entries.clear()
        self.tool = R.create_tool(
            tool_name="word_count", tool_type=ToolType.CUSTOM,
            category="text", description="统计文本字数",
            input_schema={"type": "object",
                          "properties": {"text": {"type": "string", "maxLength": 2000}},
                          "required": ["text"]},
            handler_code="def run(args):\n    return {\"count\": len(args.get(\"text\", \"\"))}",
            author=self.alice)
        ToolRegistry.objects.filter(pk=self.tool.pk).update(is_active=True)

    def turn(self, gw, *, text="数一下这段话"):
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.ASSISTANT_TURN,
            input_summary={"session_id": self.session.pk})
        _save(self.session, MessageRole.USER, [{"type": "text", "text": text}], record)
        from apps.creation.assistant.models import AssistantSession
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        with patch("apps.ai_gateway.gateway.AIGateway", return_value=gw):
            run_assistant_turn(record.pk)
        record.refresh_from_db()
        return record

    def test_model_calls_registry_tool_in_turn(self):
        # 第 1 轮模型调用注册表工具；第 2 轮拿到结果后收尾
        seen_tools = []
        orig_init = FakeChatGateway.__init__

        def _rec_init(gw_self, script):
            orig_init(gw_self, script)
        gw = FakeChatGateway([
            _use("t1", "word_count", {"text": "你好世界"}),
            _say("共 4 字"),
        ])
        # 记录 tools：包一层 chat 代理
        orig_chat = gw.chat

        def chat_spy(agent_config, *, messages, system="", tools=None, **kw):
            seen_tools.append([t["name"] for t in (tools or [])])
            return orig_chat(agent_config, messages=messages, system=system, tools=tools, **kw)
        gw.chat = chat_spy
        record = self.turn(gw)
        self.assertEqual(record.status, TaskStatus.SUCCESS, record.output_summary)
        # 模型确实收到了注册表工具 spec（含 chain_tools，因为开关开）
        self.assertTrue(seen_tools)
        tool_names = set(seen_tools[0])
        self.assertIn("word_count", tool_names)
        self.assertIn("chain_tools", tool_names)
        # 工具结果进入下一轮模型消息
        second_call = gw.calls[1]
        blocks = [b for m in second_call for b in m.get("content", [])
                  if isinstance(b, dict)]
        tool_results = [b for b in blocks if b.get("type") == "tool_result"]
        self.assertTrue(tool_results, "第二轮模型消息应含 tool_result")
        payload = json.loads(tool_results[0]["content"])
        self.assertEqual(payload["count"], 4)
        # 收尾文本落库
        last = self.session.messages.filter(role=MessageRole.ASSISTANT).order_by("-seq").first()
        self.assertEqual(last.content[0]["text"], "共 4 字")

    def test_registry_tool_hidden_when_flag_off(self):
        gw = FakeChatGateway([_say("好")])
        seen_tools = []
        orig_chat = gw.chat

        def chat_spy(agent_config, *, messages, system="", tools=None, **kw):
            seen_tools.append([t["name"] for t in (tools or [])])
            return orig_chat(agent_config, messages=messages, system=system, tools=tools, **kw)
        gw.chat = chat_spy
        with override_settings(ENABLE_TOOL_REGISTRY=False):
            record = self.turn(gw)
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        self.assertTrue(seen_tools)
        tool_names = set(seen_tools[0])
        self.assertNotIn("word_count", tool_names)
        self.assertNotIn("chain_tools", tool_names)

    def test_chain_steps_counted_against_budget(self):
        # 预算只剩 3 次工具调用：链（1 次）+ 两步（各 1 次）恰好耗尽
        for name, code in [
            ("s_first", "def run(args):\n    return {\"n\": 1}"),
            ("s_second", "def run(args):\n    return {\"n\": args[\"prev\"][\"n\"] + 1}"),
        ]:
            rec = R.create_tool(tool_name=name, tool_type=ToolType.CUSTOM,
                                category="c", description=name,
                                input_schema={}, handler_code=code,
                                author=self.alice)
            ToolRegistry.objects.filter(pk=rec.pk).update(is_active=True)
        gw = FakeChatGateway([
            _use("t1", "chain_tools",
                 {"chain": [{"tool": "s_first"}, {"tool": "s_second"}]}),
            _say("完成"),
        ])
        # 限预算：max_tool_calls=3（链 1 + 两步 2 恰好耗尽，第 2 轮模型仍可回复）
        record = self.turn(gw)
        self.assertEqual(record.status, TaskStatus.SUCCESS, record.output_summary)
        blocks = [b for m in gw.calls[1] for b in m.get("content", [])
                  if isinstance(b, dict)]
        tool_results = [b for b in blocks if b.get("type") == "tool_result"]
        payload = json.loads(tool_results[0]["content"])
        self.assertEqual(payload["completed"], [{"n": 1}, {"n": 2}])
        self.assertEqual(payload["steps"], 2)
