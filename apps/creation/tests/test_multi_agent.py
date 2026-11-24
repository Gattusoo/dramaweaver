"""多 Agent 协作测试（openspec change: 2026-11-14-backend-multi-agent / Phase 2.1）。"""
import time
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from apps.ai_gateway.gateway import AIGatewayError
from apps.creation.assistant import events
from apps.creation.assistant.models import (
    AgentCommunication, AgentRole, AssistantSession, CommunicationKind, MessageRole,
)
from apps.creation.assistant.multi_agent import (
    Agent, CollaborationCancelled, MultiAgentUnavailable, parse_critic_feedback, roles_ready,
    run_collaboration, task_cost,
)
from apps.creation.assistant.runner import _save, run_assistant_turn
from apps.creation.models import TaskRecord, TaskStatus, TaskType

from .test_assistant import AssistantFixtureMixin, FakeChatGateway, _say, _use

COMPLEX_TEXT = "重写第3集全部场景并优化所有对白，保持角色语言一致，同时加强反转和节奏"


def _boom(_record=None):
    raise AIGatewayError("模拟模型不可用")


def _cost_say(text, cost):
    step = _say(text)
    step["_cost"] = Decimal(str(cost))
    return step


class AgentRoleSeedTests(TestCase):
    def test_presets(self):
        roles = {r.name: r for r in AgentRole.objects.filter(is_active=True)}
        self.assertEqual(set(roles), {"writer", "critic", "director", "coordinator"})
        for name in ("writer", "critic", "director"):
            self.assertTrue(roles[name].system_prompt)
            self.assertTrue(roles[name].tools)
        self.assertIn("review_episode", roles["critic"].tools)
        self.assertEqual(roles["coordinator"].tools, [])
        self.assertTrue(roles_ready())

    def test_roles_ready_requires_writer_and_director(self):
        AgentRole.objects.filter(name="director").update(is_active=False)
        self.assertFalse(roles_ready())


class AgentRunTests(AssistantFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.role = AgentRole.objects.get(name="writer")
        self.record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                                input_summary={"session_id": self.session.pk})

    def test_agent_calls_allowed_tool_then_returns_text(self):
        gw = FakeChatGateway([
            _use("t1", "get_episode_script", {"episode_number": 3}),
            _say("初稿：她亮出录音。"),
        ])
        result = Agent(self.role, self.session, self.record, gateway=gw).run("改写第3集")
        self.assertEqual(result.output, "初稿：她亮出录音。")
        self.assertEqual(result.tool_calls, [{"name": "get_episode_script", "is_error": False}])
        self.assertEqual(gw.calls[0][0]["role"], "user")
        # 第二次调用带上了工具结果
        self.assertEqual(gw.calls[1][-1]["content"][0]["tool_use_id"], "t1")
        self.assertIn("第3集剧本", gw.calls[1][-1]["content"][0]["content"])

    def test_role_tools_filtered_to_read_only(self):
        self.role.tools = ["get_episode_script", "propose_scene_edit", "ask_user", "get_scene"]
        self.role.save()
        names = {t["name"] for t in Agent(self.role, self.session, self.record).tool_specs()}
        self.assertEqual(names, {"get_episode_script", "get_scene"})

    def test_llm_error_returns_degraded_result(self):
        result = Agent(self.role, self.session, self.record,
                       gateway=FakeChatGateway([_boom])).run("改写")
        self.assertEqual(result.output, "")
        self.assertIn("模拟模型不可用", result.error)


class CoordinatorTests(AssistantFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                                input_summary={"session_id": self.session.pk})
        self.budget = {"max_cost": None, "max_collab_cost": None}

    def run_collab(self, gw, **kwargs):
        return run_collaboration(self.session, self.record, kwargs.pop("goal", COMPLEX_TEXT),
                                 budget=self.budget, gateway=gw, **kwargs)

    def comms(self):
        return list(AgentCommunication.objects.filter(session=self.session).order_by("id"))

    def test_three_role_flow_and_communications(self):
        gw = FakeChatGateway([
            _say("初稿内容"),
            _say('{"scores":{"logic":8,"dialogue":7},"overall":8.5,"suggestions":["加强钩子"]}'),
            _say("最终整合文本"),
        ])
        started = time.monotonic()
        result = self.run_collab(gw)
        elapsed = time.monotonic() - started
        self.assertLess(elapsed, 30)  # 2.1.12：假模型下流程本身远低于 30s
        self.assertEqual(result["output"], "最终整合文本")
        self.assertEqual(result["outcome"], "done")
        self.assertEqual(result["feedback"]["overall"], 8.5)

        comms = self.comms()
        kinds = {c.kind for c in comms}
        self.assertEqual(kinds, {"TASK", "RESULT", "FEEDBACK", "COORDINATION"})
        senders = {c.sender for c in comms}
        self.assertEqual(senders, {"coordinator", "writer", "critic", "director"})
        self.assertTrue(any(c.sender == "writer" and c.kind == "RESULT" and "初稿内容" in c.content["output"]
                            for c in comms))
        self.assertTrue(any(c.sender == "critic" and c.kind == "FEEDBACK" and "加强钩子" in
                            str(c.content["suggestions"]) for c in comms))
        # 事件流：通信 + 汇总
        types = [e["type"] for e in events.read(self.session.pk)[0]]
        self.assertIn("agent_communication", types)
        self.assertIn("collaboration", types)
        self.assertEqual(len(gw.calls), 3)
        # 成本累加到本轮记录
        self.assertEqual(task_cost(self.record), Decimal("0"))

    def test_critic_failure_degrades_and_director_still_runs(self):
        gw = FakeChatGateway([_say("初稿"), _boom, _say("导演整合")])
        with self.assertLogs("apps.creation.assistant.multi_agent", "ERROR"):
            result = self.run_collab(gw)
        self.assertEqual(result["output"], "导演整合")
        self.assertEqual(result["outcome"], "done")
        self.assertFalse(result["degraded"])
        self.assertFalse(any(c.kind == "FEEDBACK" for c in self.comms()))

    def test_writer_failure_returns_fallback_text(self):
        with self.assertLogs("apps.creation.assistant.multi_agent", "ERROR"):
            result = self.run_collab(FakeChatGateway([_boom]))
        self.assertTrue(result["degraded"])
        self.assertIn("未产生有效结果", result["output"])
        self.assertEqual(result["outcome"], "done")

    def test_cost_cap_stops_before_more_agents(self):
        self.budget = {"max_cost": None, "max_collab_cost": 0.01}
        gw = FakeChatGateway([_cost_say("初稿", 0.02), _say("不应被调用")])
        result = self.run_collab(gw)
        self.assertEqual(result["outcome"], "budget_exceeded")
        self.assertEqual(len(gw.calls), 1)  # 只有编剧被调用
        self.assertFalse(any(c.sender == "critic" for c in self.comms()))

    def test_competition_scores_all_and_picks_best(self):
        gw = FakeChatGateway([
            _say("候选A"), _say("候选B"), _say("候选C"),
            _say('{"overall":6}'), _say('{"overall":9}'), _say('{"overall":7}'),
            _say("最终B"),
        ])
        result = self.run_collab(gw, competitive=True, styles=["紧凑", "细腻", "反转"])
        self.assertEqual(result["output"], "最终B")
        self.assertEqual([c["score"] for c in result["candidates"]], [6.0, 9.0, 7.0])
        comms = self.comms()
        writer_results = [c for c in comms if c.sender == "writer" and c.kind == "RESULT"]
        self.assertEqual(len(writer_results), 3)  # 所有候选都落库
        self.assertEqual(len([c for c in comms if c.kind == "FEEDBACK"]), 3)
        selected = [c for c in comms if c.sender == "coordinator"
                    and c.recipient == "coordinator" and "selected_candidate" in c.content]
        self.assertEqual(selected[-1].content["selected_candidate"], 2)

    def test_unparsable_critic_output_keeps_none_score(self):
        feedback = parse_critic_feedback("这不是 JSON")
        self.assertIsNone(feedback["overall"])
        self.assertFalse(feedback["parsed"])

    def test_unavailable_roles_raise(self):
        AgentRole.objects.filter(name="writer").update(is_active=False)
        with self.assertRaises(MultiAgentUnavailable):
            self.run_collab(FakeChatGateway([_say("x")]))

    def test_cancelled_round_stops(self):
        TaskRecord.objects.filter(pk=self.record.pk).update(status=TaskStatus.CANCELLED)
        with self.assertRaises(CollaborationCancelled):
            self.run_collab(FakeChatGateway([_say("x")]))


class RunnerCollaborationTests(AssistantFixtureMixin, TestCase):
    def turn(self, gw, *, mode=None, text="改一下", flags=None):
        summary = {"session_id": self.session.pk}
        if mode:
            summary["mode"] = mode
        record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                           input_summary=summary)
        _save(self.session, MessageRole.USER, [{"type": "text", "text": text}], record)
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        with override_settings(**(flags or {})), patch(
                "apps.ai_gateway.gateway.AIGateway", return_value=gw):
            run_assistant_turn(record.pk)
        record.refresh_from_db()
        return record

    def test_explicit_collaborate_uses_multi_agent(self):
        record = self.turn(FakeChatGateway([_say("初稿"), _say('{"overall":8}'), _say("协作最终")]),
                           mode="collaborate", flags={"ENABLE_MULTI_AGENT": True})
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        last = self.session.messages.filter(role=MessageRole.ASSISTANT).order_by("-seq").first()
        self.assertEqual(last.content[0]["text"], "协作最终")
        self.assertTrue(AgentCommunication.objects.filter(session=self.session).exists())
        types = [e["type"] for e in events.read(self.session.pk)[0]]
        self.assertIn("collaboration", types)
        self.assertEqual(types[-1], "done")

    def test_explicit_collaborate_rejected_when_flag_off(self):
        with self.assertLogs("apps.creation.assistant.runner", "ERROR"):
            record = self.turn(FakeChatGateway([_say("不应调用")]), mode="collaborate",
                               flags={"ENABLE_MULTI_AGENT": False})
        self.assertEqual(record.status, TaskStatus.FAILED)
        self.assertIn("未启用", record.error_message)

    def test_auto_collaborate_for_complex_task(self):
        record = self.turn(FakeChatGateway([_say("初稿"), _say('{"overall":9}'), _say("自动协作")]),
                           text=COMPLEX_TEXT,
                           flags={"ENABLE_MULTI_AGENT": True, "ENABLE_AGENT_COLLABORATION": True})
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        self.assertTrue(AgentCommunication.objects.filter(session=self.session).exists())
        last = self.session.messages.filter(role=MessageRole.ASSISTANT).order_by("-seq").first()
        self.assertEqual(last.content[0]["text"], "自动协作")

    def test_competitive_downgraded_when_competition_flag_off(self):
        """5.3 兜底：投递后竞争开关被关闭 → 按非竞争执行（3 次调用而非 3n+1）。"""
        gw = FakeChatGateway([_say("初稿"), _say('{"overall":8}'), _say("协作最终")])
        summary = {"session_id": self.session.pk, "mode": "collaborate", "competitive": True}
        record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                           input_summary=summary)
        _save(self.session, MessageRole.USER, [{"type": "text", "text": "改一下"}], record)
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        with override_settings(ENABLE_MULTI_AGENT=True, ENABLE_AGENT_COMPETITION=False), patch(
                "apps.ai_gateway.gateway.AIGateway", return_value=gw):
            run_assistant_turn(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.status, TaskStatus.SUCCESS, record.error_message)
        self.assertEqual(len(gw.calls), 3)  # 编剧 + 评论 + 导演，无竞争候选

    def test_auto_disabled_for_simple_task(self):
        record = self.turn(FakeChatGateway([_say("单Agent回复")]), text="你好",
                           flags={"ENABLE_MULTI_AGENT": True, "ENABLE_AGENT_COLLABORATION": True})
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        self.assertFalse(AgentCommunication.objects.filter(session=self.session).exists())
        last = self.session.messages.filter(role=MessageRole.ASSISTANT).order_by("-seq").first()
        self.assertEqual(last.content[0]["text"], "单Agent回复")


class CollaborateApiTests(AssistantFixtureMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.url = f"/api/assistant/sessions/{self.session.pk}/collaborate/"

    def post(self, body, user=None, flags=None):
        self.client.force_authenticate(user or self.producer)
        with override_settings(**(flags or {"ENABLE_MULTI_AGENT": True})):
            with self.captureOnCommitCallbacks(execute=True):
                return self.client.post(self.url, body, format="json")

    def test_creates_async_turn(self):
        resp = self.post({"goal": "重写第3集", "competitive": True, "styles": ["紧凑", "细腻"]},
                         flags={"ENABLE_MULTI_AGENT": True, "ENABLE_AGENT_COMPETITION": True})
        self.assertEqual(resp.status_code, 202)
        self.assertIn("task_record_id", resp.data)
        record = TaskRecord.objects.get(pk=resp.data["task_record_id"])
        self.assertEqual(record.input_summary["mode"], "collaborate")
        self.assertTrue(record.input_summary["competitive"])
        self.assertEqual(record.input_summary["styles"], ["紧凑", "细腻"])
        self.session.refresh_from_db()
        self.assertEqual(self.session.running_task_id, record.pk)
        self.assertTrue(self.session.messages.filter(role=MessageRole.USER, content__0__text="重写第3集").exists())
        self.send.assert_called()

    def test_flag_off_returns_400(self):
        resp = self.post({"goal": "x"}, flags={"ENABLE_MULTI_AGENT": False})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("未启用", str(resp.data))

    # ── 5.3 竞争模式受自身开关约束 ──
    def test_competitive_rejected_when_competition_flag_off(self):
        """ENABLE_AGENT_COMPETITION=false 时要求竞争模式 → 400，且不创建任务。"""
        resp = self.post({"goal": "重写第3集", "competitive": True},
                         flags={"ENABLE_MULTI_AGENT": True, "ENABLE_AGENT_COMPETITION": False})
        self.assertEqual(resp.status_code, 400, resp.data)
        self.assertIn("competitive", str(resp.data))
        self.assertFalse(TaskRecord.objects.filter(
            play=self.play, input_summary__mode="collaborate").exists())

    def test_competitive_allowed_when_competition_flag_on(self):
        resp = self.post({"goal": "重写第3集", "competitive": True},
                         flags={"ENABLE_MULTI_AGENT": True, "ENABLE_AGENT_COMPETITION": True})
        self.assertEqual(resp.status_code, 202, resp.data)
        record = TaskRecord.objects.get(pk=resp.data["task_record_id"])
        self.assertTrue(record.input_summary["competitive"])

    def test_non_competitive_unaffected_by_competition_flag(self):
        """开关只管竞争模式：非竞争协作在开关关闭时照常受理。"""
        resp = self.post({"goal": "重写第3集"},
                         flags={"ENABLE_MULTI_AGENT": True, "ENABLE_AGENT_COMPETITION": False})
        self.assertEqual(resp.status_code, 202, resp.data)

    def test_empty_goal_returns_400(self):
        self.assertEqual(self.post({"goal": "  "}).status_code, 400)

    def test_invalid_styles_returns_400(self):
        self.assertEqual(self.post({"goal": "x", "styles": "紧凑"}).status_code, 400)

    def test_viewer_cannot_collaborate(self):
        session = AssistantSession.objects.create(play=self.play, user=self.viewer,
                                                  agent_config=self.config)
        url = f"/api/assistant/sessions/{session.pk}/collaborate/"
        self.client.force_authenticate(self.viewer)
        with override_settings(ENABLE_MULTI_AGENT=True):
            resp = self.client.post(url, {"goal": "x"}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_conflict_when_round_running(self):
        record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                           status=TaskStatus.PENDING,
                                           input_summary={"session_id": self.session.pk})
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        resp = self.post({"goal": "x"})
        self.assertEqual(resp.status_code, 409)

    def test_budget_exceeded_returns_409(self):
        self.config.assistant_budget = {"max_cost": 0}
        self.config.save()
        resp = self.post({"goal": "x"})
        self.assertEqual(resp.status_code, 409)
        self.assertEqual(resp.data.get("reason"), "budget_exceeded")


# ── Phase 2 code-review 修复回归 ─────────────────────────────────────────────

class AgentToolCapPairingTests(AssistantFixtureMixin, TestCase):
    """修复：多 Agent 工具白名单截断后未补 tool_result，破坏配对约束。"""

    def setUp(self):
        super().setUp()
        self.role = AgentRole.objects.get(name="writer")
        self.record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                                input_summary={"session_id": self.session.pk})

    def test_excess_tool_uses_get_error_results(self):
        # 同一条模型响应返回 8 个 tool_use（上限 6）：前 6 个正常执行，后 2 个补 is_error 结果
        many = {"content_blocks": [
            {"type": "tool_use", "id": f"t{i}", "name": "get_episode_script",
             "input": {"episode_number": 3}} for i in range(8)],
            "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5}}
        gw = FakeChatGateway([many, _say("初稿")])
        result = Agent(self.role, self.session, self.record, gateway=gw).run("改写第3集")
        self.assertEqual(result.output, "初稿", (result.error, result.tool_calls))
        errors = [t for t in result.tool_calls if t["is_error"]]
        self.assertEqual(len(result.tool_calls), 8)
        self.assertEqual(len(errors), 2)
        # 第二次模型调用的 tool_result 覆盖全部 8 个 tool_use（配对完整）
        second_call_results = gw.calls[1][-1]["content"]
        self.assertEqual(len(second_call_results), 8)
        self.assertEqual(sum(1 for r in second_call_results if r.get("is_error")), 2)
        self.assertIn("工具调用上限", second_call_results[-1]["content"])


class CollaborateTreeSyncTests(AssistantFixtureMixin, TestCase):
    """修复：协作轮提前返回，用户 goal 未同步进会话树，压缩历史丢用户消息。"""

    def test_user_goal_synced_before_assistant(self):
        import shutil
        import tempfile
        from apps.creation.assistant.session_tree import SessionStorage

        tmp = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, tmp, True)
        record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                           input_summary={"session_id": self.session.pk,
                                                          "mode": "collaborate"})
        _save(self.session, MessageRole.USER, [{"type": "text", "text": "重写第3集"}], record)
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        gw = FakeChatGateway([_say("初稿"), _say('{"overall":8}'), _say("协作最终")])
        with override_settings(ENABLE_MULTI_AGENT=True, ENABLE_CONTEXT_COMPACTION=True,
                               MEDIA_ROOT=tmp), patch(
                "apps.ai_gateway.gateway.AIGateway", return_value=gw):
            run_assistant_turn(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.status, TaskStatus.SUCCESS)

        with override_settings(MEDIA_ROOT=tmp):
            tree = SessionStorage.load(str(self.play.pk), str(self.session.pk))
        self.assertIsNotNone(tree)
        kinds = [e.type for e in tree.entries]
        texts = [b.get("text") for e in tree.entries
                 for b in (e.content.get("blocks") or []) if isinstance(b, dict)]
        self.assertIn("重写第3集", texts)
        self.assertIn("协作最终", texts)
        self.assertLess(kinds.index("user_message"), kinds.index("assistant_message"))


class CollaborateBoolValidationTests(AssistantFixtureMixin, APITestCase):
    """修复：bool("false") 为 True，字符串布尔把 competitive=false 翻成真。"""

    def setUp(self):
        super().setUp()
        self.url = f"/api/assistant/sessions/{self.session.pk}/collaborate/"

    def post(self, body):
        self.client.force_authenticate(self.producer)
        with override_settings(ENABLE_MULTI_AGENT=True):
            with self.captureOnCommitCallbacks(execute=True):
                return self.client.post(self.url, body, format="json")

    def test_competitive_string_false_stays_false(self):
        resp = self.post({"goal": "重写第3集", "competitive": "false"})
        self.assertEqual(resp.status_code, 202, resp.data)
        record = TaskRecord.objects.get(pk=resp.data["task_record_id"])
        self.assertFalse(record.input_summary["competitive"])

    def test_competitive_invalid_string_rejected(self):
        resp = self.post({"goal": "重写第3集", "competitive": "yes"})
        self.assertEqual(resp.status_code, 400)
        self.assertIn("competitive", str(resp.data))


# ── 2026-11-15-phase2-multi-agent-budget-remediation ──────────────────────────

from apps.creation.assistant.runner import budget_of  # noqa: E402


class BudgetThreeStateTests(AssistantFixtureMixin, TestCase):
    """任务 1.1–1.3：三态语义（缺失=默认 / null=不限制 / 正数=限制）与非金额字段兜底。"""

    def test_missing_key_uses_default(self):
        # 未配置 max_collab_cost：生效值为默认 ¥1.00（自动触发路径的兜底约束）
        self.config.assistant_budget = {}
        self.assertEqual(budget_of(self.session)["max_collab_cost"], Decimal("1.00"))

    def test_explicit_null_means_unlimited(self):
        # 显式 null：不限制，不回落默认值（否则默认值永远关不掉）
        self.config.assistant_budget = {"max_collab_cost": None}
        self.assertIsNone(budget_of(self.session)["max_collab_cost"])

    def test_positive_value_limits(self):
        self.config.assistant_budget = {"max_collab_cost": 0.5}
        self.assertEqual(budget_of(self.session)["max_collab_cost"], 0.5)

    def test_malformed_null_on_integer_field_falls_back(self):
        # 畸形配置：max_iterations=null 兜底回默认值，None 不得漏到下游 int()
        self.config.assistant_budget = {"max_iterations": None, "max_tool_calls": None}
        budget = budget_of(self.session)
        self.assertEqual(budget["max_iterations"], 8)
        self.assertEqual(budget["max_tool_calls"], 20)

    def test_max_cost_stays_unlimited_by_default(self):
        # max_cost 不加默认值：会话级预算由用户显式配置，"没配=不限制"是产品语义
        self.config.assistant_budget = {}
        self.assertIsNone(budget_of(self.session)["max_cost"])


class CapSemanticsTests(AssistantFixtureMixin, TestCase):
    """任务 3.1–3.3：上限口径统一为整轮累计。"""

    def setUp(self):
        super().setUp()
        self.role = AgentRole.objects.get(name="writer")
        self.record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                                input_summary={"session_id": self.session.pk})

    def test_role_cannot_exceed_round_cap(self):
        # 轮次已消耗 ¥0.06，cap=¥0.10：角色第一次调用（工具轮）把轮次累计置到
        # ¥0.10 后，第二次迭代入口的整轮累计检查即截断——单角色不拥有从 0
        # 起算的完整额度（旧量纲错配下本可再花满 ¥0.10）
        TaskRecord.objects.filter(pk=self.record.pk).update(cost=Decimal("0.06"))
        gw = FakeChatGateway([
            {"content_blocks": [{"type": "tool_use", "id": "t1", "name": "get_episode_script",
                                 "input": {"episode_number": 3}}],
             "stop_reason": "tool_use", "usage": {"input_tokens": 10, "output_tokens": 5},
             "_cost": Decimal("0.10")},
            _say("不应被调用"),
        ])
        result = Agent(self.role, self.session, self.record, gateway=gw).run("改写", cap=Decimal("0.10"))
        self.assertTrue(result.truncated)
        self.assertEqual(len(gw.calls), 1)
        # AgentResult.cost 仍表达本角色花费（轮末累计 − 轮初累计）
        self.assertEqual(result.cost, Decimal("0.04"))

    def test_three_roles_consecutive_no_cumulative_overrun(self):
        # 三个角色依次执行，编剧把轮次累计抬到 ¥0.04、导演抬到 ¥0.10（触顶）：
        # 全部结束后轮次总费用不超过上限，outcome 为 budget_exceeded 而非继续越界
        budget = {"max_cost": None, "max_collab_cost": "0.10"}
        gw = FakeChatGateway([
            _cost_say("初稿", 0.04),
            _say('{"overall":8}'),
            _cost_say("最终", 0.10),
        ])
        result = run_collaboration(self.session, self.record, "改写", budget=budget, gateway=gw)
        self.assertEqual(result["outcome"], "budget_exceeded")
        self.assertLessEqual(task_cost(self.record), Decimal("0.10"))

    def test_role_stops_before_overspending_round_cap(self):
        # 角色开始前轮次已达到上限：任何调用都会越限，角色应在调用前截断
        TaskRecord.objects.filter(pk=self.record.pk).update(cost=Decimal("0.10"))
        gw = FakeChatGateway([_say("不应被调用")])
        result = Agent(self.role, self.session, self.record, gateway=gw).run("改写", cap=Decimal("0.10"))
        self.assertTrue(result.truncated)
        self.assertEqual(len(gw.calls), 0)


class CollaborationTokenTests(AssistantFixtureMixin, TestCase):
    """任务 4.1–4.2：协作轮 token 计入会话统计。"""

    def _turn(self, gw):
        record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                           input_summary={"session_id": self.session.pk, "mode": "collaborate"})
        _save(self.session, MessageRole.USER, [{"type": "text", "text": COMPLEX_TEXT}], record)
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        with override_settings(ENABLE_MULTI_AGENT=True), patch(
                "apps.ai_gateway.gateway.AIGateway", return_value=gw):
            run_assistant_turn(record.pk)
        record.refresh_from_db()
        self.session.refresh_from_db()
        return record

    def test_collaboration_tokens_counted(self):
        # 三次调用 × 每次 10+5 tokens = 45 计入会话统计；协作路径不再是统计豁免
        gw = FakeChatGateway([_say("初稿"), _say('{"overall":8}'), _say("协作最终")])
        before = self.session.total_tokens
        record = self._turn(gw)
        self.assertEqual(record.status, TaskStatus.SUCCESS, record.error_message)
        self.assertEqual(self.session.total_tokens - before, 45)
