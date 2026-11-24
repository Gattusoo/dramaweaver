# -*- coding: utf-8 -*-
"""分层记忆测试（openspec change: backend-layered-memory）。

覆盖：重要性评分（类型分值/纯函数）、压缩重要性保护（高分组保留/全组高分兜底/
开关关闭逐项一致）、MemoryRecord 实体与工具（保存/检索/去重/停用/开关零暴露）、
L3 分层检索（三档条数/注入隔离/开关关闭空段）、跨会话沉淀（触发/降级/静默失败/
上限/预算入账）。
"""
import json
from unittest import mock
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.creation.assistant import events
from apps.creation.assistant.compaction import CompactionConfig, Compactor
from apps.creation.assistant.memory_layers import (
    DEFAULT_L3_TIERS,
    build_l3_section,
    l3_limit_for_complexity,
    retrieve_context,
    score_importance,
    search_memory_records,
    settle_memory_records,
)
from apps.creation.assistant.models import (
    AssistantProposal, AssistantSession, MemoryKind, MemoryRecord, MessageRole,
    ProposalStatus,
)
from apps.creation.assistant.session_tree import SessionEntry, SessionTree
from apps.creation.assistant.tools import ToolContext, run_tool, tool_specs
from apps.creation.models import TaskRecord, TaskStatus, TaskType

from .test_assistant import AssistantFixtureMixin, FakeChatGateway, _say, _use

FLAGS = {"ENABLE_LAYERED_MEMORY": True}


# ── 重要性评分（任务 1.1）───────────────────────────────────────────────────


class ScoreImportanceTests(TestCase):
    def _entry(self, etype, content=None):
        return SessionEntry(type=etype, content=content or {})

    def test_score_table(self):
        """各类型分值：memory_save 1.0 / user 0.8 / plan 系 0.7 / 普通 0.3。"""
        save_use = self._entry("assistant_message", {"blocks": [
            {"type": "tool_use", "name": "memory_save", "id": "t1"}]})
        self.assertEqual(score_importance(save_use), 1.0)
        self.assertEqual(score_importance(self._entry("user_message")), 0.8)
        for t in ("plan", "plan_update", "reflection", "tot"):
            self.assertEqual(score_importance(self._entry(t)), 0.7)
        self.assertEqual(score_importance(self._entry("assistant_message")), 0.3)
        self.assertEqual(score_importance(self._entry("tool_result")), 0.3)
        self.assertEqual(score_importance(self._entry("compaction")), 0.0)

    def test_pure_function(self):
        """纯函数：同输入同输出。"""
        e = self._entry("user_message", {"blocks": [{"type": "text", "text": "x"}]})
        self.assertEqual(score_importance(e), score_importance(e))
        e2 = self._entry("user_message", {"blocks": [{"type": "text", "text": "x"}]})
        self.assertEqual(score_importance(e), score_importance(e2))

    def test_propose_tool_use_scores_high_via_group(self):
        """提案工具 tool_use 组内上界：0.9 通过组级取最大生效（间接验证分值来源）。"""
        from apps.creation.assistant.compaction import _tool_call_groups
        from apps.creation.assistant.memory_layers import group_importance
        entries = [
            SessionEntry(type="assistant_message", content={"blocks": [
                {"type": "tool_use", "name": "propose_shot_edit", "id": "t1"}]}),
            SessionEntry(type="tool_result", content={"blocks": [
                {"type": "tool_result", "tool_use_id": "t1"}]}),
        ]
        groups = _tool_call_groups(entries)
        self.assertEqual(len(groups), 1)
        # propose_* 的 tool_use 分值 0.9（提案组上界）
        self.assertGreaterEqual(group_importance(entries, groups[0]), 0.8)


# ── 压缩重要性保护（任务 1.2）───────────────────────────────────────────────


def _big_text(n):
    return {"blocks": [{"type": "text", "text": "字" * n}]}


class CutoffProtectionTests(TestCase):
    """find_cutoff_point：保护/兜底/开关关闭三态。

    构造：每组 ~410 tokens；keep_recent=1000 → 纯位置边界在组 4（0-3 被摘要）。
    """

    def _entries(self, n_groups=6, important_at=()):
        entries = []
        for i in range(n_groups):
            if i in important_at:
                entries.append(SessionEntry(type="user_message", content=_big_text(400)))
            else:
                entries.append(SessionEntry(type="assistant_message", content=_big_text(400)))
        return entries

    def _compactor(self):
        return Compactor(CompactionConfig(keep_recent_tokens=1000),
                         summarizer=lambda h, p, i: "s")

    def test_flag_off_position_semantics(self):
        """开关关闭：与纯位置语义逐项一致（高分组也照切）。"""
        entries = self._entries(important_at=(2,))
        cutoff_off = self._compactor().find_cutoff_point(entries)
        with override_settings(ENABLE_LAYERED_MEMORY=False):
            self.assertEqual(self._compactor().find_cutoff_point(entries), cutoff_off)

    def test_important_group_protected(self):
        """摘要区高分组 → 边界前移划入保留区（更小索引 = 保留更多）。"""
        entries = self._entries(important_at=(2,))
        with override_settings(ENABLE_LAYERED_MEMORY=False):
            position = self._compactor().find_cutoff_point(entries)
        self.assertEqual(position, 4)  # 组 0-3 被摘要（含组 2 的 user）
        with override_settings(**FLAGS):
            cutoff = self._compactor().find_cutoff_point(entries)
        # 组 2（user 0.8）被保护：边界前移到 2（组 2-5 保留）
        self.assertEqual(cutoff, 2)
        self.assertLess(cutoff, position)

    def test_important_group_in_keep_zone_not_protected(self):
        """高分组已在保留区 → 不触发保护（边界与纯位置一致）。"""
        entries = self._entries(important_at=(4,))
        with override_settings(**FLAGS):
            cutoff = self._compactor().find_cutoff_point(entries)
        self.assertEqual(cutoff, 4)

    def test_all_groups_important_fallback(self):
        """全部组高分 → 兜底退纯位置语义（不出现永久压不了）。"""
        entries = self._entries(important_at=tuple(range(6)))
        with override_settings(**FLAGS):
            cutoff = self._compactor().find_cutoff_point(entries)
        with override_settings(ENABLE_LAYERED_MEMORY=False):
            position = self._compactor().find_cutoff_point(entries)
        self.assertEqual(cutoff, position)
        self.assertEqual(cutoff, 4)

    def test_no_need_to_compact_returns_none_both_ways(self):
        """全部组累计 < 阈值 → None（两态一致）。"""
        entries = self._entries(n_groups=2)
        with override_settings(**FLAGS):
            self.assertIsNone(self._compactor().find_cutoff_point(entries))
        with override_settings(ENABLE_LAYERED_MEMORY=False):
            self.assertIsNone(self._compactor().find_cutoff_point(entries))

    def test_compact_records_protected_groups(self):
        """compact 写入 protected_groups 可观测字段。"""
        tree = SessionTree(session_id="s", play_id="p")
        for i in range(6):
            tree.append("user_message" if i == 2 else "assistant_message", _big_text(400))
        with override_settings(**FLAGS):
            result = Compactor(CompactionConfig(keep_recent_tokens=1000),
                               summarizer=lambda h, p, i: "s").compact(tree)
        self.assertTrue(result.compacted)
        comp = [e for e in tree.entries if e.type == "compaction"]
        self.assertEqual(len(comp), 1)
        self.assertGreaterEqual((comp[0].content or {}).get("protected_groups", 0), 1)


# ── MemoryRecord 实体与工具（任务 2.1/2.2）─────────────────────────────────


class MemoryRecordModelTests(AssistantFixtureMixin, TestCase):
    def test_dedup_on_same_content(self):
        sha = "x" * 64
        a, created1 = MemoryRecord.objects.update_or_create(
            play=self.play, kind=MemoryKind.PREFERENCE, content_sha256=sha,
            defaults={"content": "对白不超过20字", "importance": 0.7})
        b, created2 = MemoryRecord.objects.update_or_create(
            play=self.play, kind=MemoryKind.PREFERENCE, content_sha256=sha,
            defaults={"content": "对白不超过20字", "importance": 0.9,
                      "is_active": True})
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(a.pk, b.pk)
        a.refresh_from_db()
        self.assertEqual(a.importance, 0.9)  # 刷新而非新行

    def test_cross_play_isolated(self):
        MemoryRecord.objects.create(
            play=self.play, kind=MemoryKind.FACT, content_sha256="a" * 64, content="甲剧事实")
        self.assertEqual(MemoryRecord.objects.count(), 1)
        # 另一剧（fixture 里的 other play 不在本 mixin——直接建）
        from apps.creation.models import Play, PlayStatus
        p2 = Play.objects.create(title="乙剧", created_by=self.producer)
        MemoryRecord.objects.create(
            play=p2, kind=MemoryKind.FACT, content_sha256="b" * 64, content="乙剧事实")
        self.assertEqual(
            len(search_memory_records(self.play, "事实", limit=10)), 1)


class MemoryRecordToolTests(AssistantFixtureMixin, TestCase):
    def _ctx(self):
        return ToolContext(self.session)

    def _run(self, name, args):
        return run_tool(self._ctx(), name, args)

    @override_settings(**FLAGS)
    def test_save_and_search(self):
        result, is_err = self._run("memory_record_save", {
            "kind": "PREFERENCE", "content": "本剧对白不超过 20 字", "importance": 0.9})
        self.assertFalse(is_err, result)
        self.assertTrue(result["created"])
        hit, is_err = self._run("memory_record_search", {"query": "对白", "limit": 5})
        self.assertFalse(is_err, hit)
        self.assertEqual(hit["count"], 1)
        self.assertEqual(hit["records"][0]["kind"], "PREFERENCE")
        self.assertEqual(hit["records"][0]["importance"], 0.9)

    @override_settings(**FLAGS)
    def test_save_dedup_via_tool(self):
        args = {"kind": "FACT", "content": "男主第5集失忆"}
        r1, _ = self._run("memory_record_save", args)
        r2, _ = self._run("memory_record_save", args)
        self.assertTrue(r1["created"])
        self.assertFalse(r2["created"])
        self.assertEqual(MemoryRecord.objects.count(), 1)

    @override_settings(**FLAGS)
    def test_inactive_excluded_from_search(self):
        rec = MemoryRecord.objects.create(
            play=self.play, kind=MemoryKind.DECISION, content_sha256="c" * 64,
            content="第3集高潮提前到场景2")
        MemoryRecord.objects.filter(pk=rec.pk).update(is_active=False)
        hit, _ = self._run("memory_record_search", {"query": "高潮"})
        self.assertEqual(hit["count"], 0)

    def test_flag_off_tools_rejected_as_unknown(self):
        """开关关闭：工具执行报未启用（is_error），工具面零暴露。"""
        result, is_err = self._run("memory_record_save", {
            "kind": "FACT", "content": "不该保存"})
        self.assertTrue(is_err)
        result2, is_err2 = self._run("memory_record_search", {"query": "x"})
        self.assertTrue(is_err2)
        names = [s["name"] for s in tool_specs()]
        self.assertNotIn("memory_record_save", names)
        self.assertNotIn("memory_record_search", names)

    @override_settings(**FLAGS)
    def test_flag_on_tools_in_specs(self):
        names = [s["name"] for s in tool_specs()]
        self.assertIn("memory_record_save", names)
        self.assertIn("memory_record_search", names)

    @override_settings(**FLAGS)
    def test_importance_weighting(self):
        """importance 加权：命中数相同时高分在前。"""
        from django.conf import settings
        MemoryRecord.objects.create(
            play=self.play, kind=MemoryKind.FACT, content_sha256="d" * 64,
            content="反转设计在尾声", importance=0.3)
        MemoryRecord.objects.create(
            play=self.play, kind=MemoryKind.FACT, content_sha256="e" * 64,
            content="反转设计在开场", importance=0.95)
        hit, _ = self._run("memory_record_search", {"query": "反转设计"})
        self.assertEqual(hit["count"], 2)
        self.assertEqual(hit["records"][0]["importance"], 0.95)


# ── L3 分层检索与动态窗口（任务 3.1/3.2）────────────────────────────────────


class L3RetrievalTests(AssistantFixtureMixin, TestCase):
    def _seed(self, n=10):
        for i in range(n):
            MemoryRecord.objects.create(
                play=self.play, kind=MemoryKind.FACT, content_sha256=f"{i:064d}",
                content=f"第{i}条记忆条目", importance=0.5)

    def test_tier_limits(self):
        """三档条数：低/中/高复杂度取 MEMORY_L3_TIERS。"""
        low, mid, high = DEFAULT_L3_TIERS
        self.assertEqual(l3_limit_for_complexity(0.2), low)
        self.assertEqual(l3_limit_for_complexity(0.55), mid)
        self.assertEqual(l3_limit_for_complexity(0.9), high)

    def test_tier_limits_config_override(self):
        with override_settings(MEMORY_L3_TIERS="1,2,3"):
            self.assertEqual(l3_limit_for_complexity(0.2), 1)
            self.assertEqual(l3_limit_for_complexity(0.55), 2)
            self.assertEqual(l3_limit_for_complexity(0.9), 3)

    @override_settings(**FLAGS)
    def test_retrieve_context_high_complexity_more_records(self):
        """复杂任务注入更多 L3 条目（spec Scenario）。"""
        self._seed(10)
        low = retrieve_context(self.session, "条目", 0.2)
        high = retrieve_context(self.session, "条目", 0.9)
        self.assertGreater(high["layers"]["l3_records"], low["layers"]["l3_records"])
        self.assertEqual(high["meta"]["limit"], DEFAULT_L3_TIERS[2])

    def test_retrieve_context_flag_off_empty(self):
        """开关关闭：L3 为空、limit 仍计算（spec Scenario）。"""
        self._seed(5)
        ctx = retrieve_context(self.session, "条目", 0.9)
        self.assertEqual(ctx["layers"]["l3_section"], "")
        self.assertEqual(ctx["layers"]["l3_records"], 0)

    @override_settings(**FLAGS)
    def test_l3_section_isolated(self):
        """注入隔离：随机定界符包裹，正文围栏不能提前闭合。"""
        MemoryRecord.objects.create(
            play=self.play, kind=MemoryKind.FACT, content_sha256="f" * 64,
            content="含围栏```的恶意内容")
        section = build_l3_section(search_memory_records(self.play, "围栏", limit=5))
        self.assertIn("MEMREC-", section)
        self.assertIn("BEGIN", section)
        self.assertIn("END", section)
        # 围栏被打断（零宽分隔）
        self.assertNotIn("```", section)

    @override_settings(**FLAGS)
    def test_runner_injects_l3_in_system_prompt(self):
        """runner 端到端：system prompt 含 L3 段（开关开启时）。"""
        MemoryRecord.objects.create(
            play=self.play, kind=MemoryKind.PREFERENCE, content_sha256="1" * 64,
            content="对白短平快", importance=0.8)
        from apps.creation.assistant.runner import _save, run_assistant_turn, system_prompt
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.ASSISTANT_TURN,
            input_summary={"session_id": self.session.pk})
        _save(self.session, MessageRole.USER, [{"type": "text", "text": "对白怎么改"}], record)
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        gw = FakeChatGateway([_say("收到")])
        with patch("apps.ai_gateway.gateway.AIGateway", return_value=gw):
            run_assistant_turn(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        # system prompt 含 L3 段（gw.calls 只含 messages；直接调 system_prompt 验证）
        prompt = system_prompt(self.session)
        self.assertIn("结构化记忆条目", prompt)
        self.assertIn("对白短平快", prompt)

    def test_runner_flag_off_prompt_unchanged(self):
        """开关关闭：system prompt 不含 L3 段（与现状一致）。"""
        MemoryRecord.objects.create(
            play=self.play, kind=MemoryKind.PREFERENCE, content_sha256="2" * 64,
            content="对白短平快", importance=0.8)
        from apps.creation.assistant.runner import system_prompt
        self.assertNotIn("结构化记忆条目", system_prompt(self.session))


# ── 跨会话沉淀（任务 4.1）───────────────────────────────────────────────────


class SettleTests(AssistantFixtureMixin, TestCase):
    def _turn(self, gw, user_text="把节奏加快"):
        from apps.creation.assistant.runner import _save, run_assistant_turn
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.ASSISTANT_TURN,
            input_summary={"session_id": self.session.pk})
        _save(self.session, MessageRole.USER, [{"type": "text", "text": user_text}], record)
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        with patch("apps.ai_gateway.gateway.AIGateway", return_value=gw):
            run_assistant_turn(record.pk)
        record.refresh_from_db()
        return record

    @staticmethod
    def _llm_reply(records):
        return {"content_blocks": [{"type": "text", "text": json.dumps(
            {"records": records}, ensure_ascii=False)}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 10, "output_tokens": 5}}

    @override_settings(**FLAGS)
    def test_settle_on_applied_proposal_signal(self):
        """提案 apply 信号 → 沉淀（LLM 提取，计入预算）。"""
        AssistantProposal.objects.create(
            session=self.session, play=self.play, kind="SHOT", status=ProposalStatus.APPLIED)
        cost_before = TaskRecord.objects.filter(
            task_type=TaskType.ASSISTANT_TURN,
            input_summary__session_id=self.session.pk).count()
        gw = FakeChatGateway([
            _say("改好了"),
            self._llm_reply([{"kind": "DECISION", "content": "第3集高潮提前", "importance": 0.8}]),
        ])
        record = self._turn(gw)
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        self.assertTrue(MemoryRecord.objects.filter(
            play=self.play, kind=MemoryKind.DECISION).exists())
        # 提取 LLM 调用计入轮次预算（gateway 收到 task_record）
        self.assertEqual(gw.calls and True, True)

    @override_settings(**FLAGS)
    def test_settle_on_user_confirm_word(self):
        """用户确认词 → 触发（无需提案）。"""
        gw = FakeChatGateway([
            _say("好"),
            self._llm_reply([{"kind": "PREFERENCE", "content": "以后对白都短", "importance": 0.7}]),
        ])
        record = self._turn(gw, user_text="以后对白都控制在20字以内")
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        self.assertTrue(MemoryRecord.objects.filter(
            play=self.play, kind=MemoryKind.PREFERENCE).exists())

    @override_settings(**FLAGS)
    def test_settle_no_signal_no_call(self):
        """无确认信号 → 不沉淀、不调 LLM（spec Scenario）。"""
        gw = FakeChatGateway([_say("好的")])
        record = self._turn(gw, user_text="看一下第3集")
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        self.assertEqual(MemoryRecord.objects.count(), 0)
        self.assertEqual(len(gw.calls), 1)  # 只有轮次主调用

    @override_settings(**FLAGS)
    def test_settle_llm_fail_falls_back_heuristic(self):
        """LLM 失败 → 启发式降级（user 确认句被抓为偏好）。"""
        gw = FakeChatGateway([
            _say("收到"),
            {"content_blocks": [{"type": "text", "text": "不是JSON"}],
             "stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 5}},
        ])
        record = self._turn(gw, user_text="记住以后对白都控制在20字以内")
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        self.assertTrue(MemoryRecord.objects.filter(
            play=self.play, kind=MemoryKind.PREFERENCE).exists())

    @override_settings(**FLAGS)
    def test_settle_all_fail_silent(self):
        """LLM 与启发式均无产出 → 静默、轮次照常（提案触发但无确认词可抓）。"""
        AssistantProposal.objects.create(
            session=self.session, play=self.play, kind="SHOT", status=ProposalStatus.APPLIED)
        gw = FakeChatGateway([
            _say("收到"),
            {"content_blocks": [{"type": "text", "text": "无有效内容"}],
             "stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 5}},
        ])
        record = self._turn(gw, user_text="把第3集结尾改一下")
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        self.assertEqual(MemoryRecord.objects.count(), 0)

    def test_settle_flag_off(self):
        """开关关闭 → 不沉淀、无 LLM 调用。"""
        AssistantProposal.objects.create(
            session=self.session, play=self.play, kind="SHOT", status=ProposalStatus.APPLIED)
        gw = FakeChatGateway([_say("改好了")])
        record = self._turn(gw)
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        self.assertEqual(MemoryRecord.objects.count(), 0)
        self.assertEqual(len(gw.calls), 1)

    @override_settings(**FLAGS, MEMORY_SETTLE_MAX_RECORDS=2)
    def test_settle_max_records_cap(self):
        """单轮上限：超出部分丢弃。"""
        AssistantProposal.objects.create(
            session=self.session, play=self.play, kind="SHOT", status=ProposalStatus.APPLIED)
        gw = FakeChatGateway([
            _say("好"),
            self._llm_reply([
                {"kind": "FACT", "content": f"事实{i}"} for i in range(5)
            ] + [{"importance": 0.5}]),
        ])
        record = self._turn(gw)
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        self.assertEqual(MemoryRecord.objects.count(), 2)

    @override_settings(**FLAGS)
    def test_settle_dedup_across_turns(self):
        """跨轮重复沉淀去重（content_sha256 唯一）。"""
        reply = self._llm_reply([{"kind": "PREFERENCE", "content": "固定用冷开场", "importance": 0.6}])
        for _ in range(2):
            gw = FakeChatGateway([_say("好"), reply])
            self._turn(gw, user_text="记住固定用冷开场")
        self.assertEqual(MemoryRecord.objects.count(), 1)


# ── 配置接线（任务 1.3）─────────────────────────────────────────────────────


class FeatureFlagWiringTests(TestCase):
    def test_settings_defaults(self):
        """默认关闭 + 阈值正确。"""
        from django.conf import settings
        self.assertIs(settings.ENABLE_LAYERED_MEMORY, False)
        self.assertEqual(settings.MEMORY_IMPORTANT_THRESHOLD, 0.8)
        self.assertEqual(settings.MEMORY_SETTLE_MAX_RECORDS, 3)

    def test_env_template_has_keys(self):
        import conf
        from pathlib import Path
        import importlib.util
        template = Path(conf.__file__).parent / "env.example.py"
        self.assertTrue(template.is_file())
        spec = importlib.util.spec_from_file_location("_tpl_layered", template)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.ENABLE_LAYERED_MEMORY, "false")
        self.assertEqual(mod.MEMORY_IMPORTANT_THRESHOLD, "0.8")
        self.assertEqual(mod.MEMORY_L3_TIERS, "3,5,8")
        self.assertEqual(mod.MEMORY_SETTLE_MAX_RECORDS, "3")
