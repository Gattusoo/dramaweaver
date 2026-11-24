"""上下文压缩测试（Phase 1.3）：学习Pi Agent。"""
import json
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.creation.assistant.compaction import (
    CompactionConfig,
    Compactor,
    SUMMARY_PREFIX,
    compact_session,
    compacted_history_blocks,
    effective_context_entries,
    estimate_context_tokens,
    estimate_entry_tokens,
    estimate_tokens,
    heuristic_summary,
    llm_summary,
    should_compact,
)
from apps.creation.assistant.models import AssistantSession
from apps.creation.assistant.session_tree import SessionStorage, SessionTree, SessionTreeManager
from apps.creation.models import AgentConfig, Play, PlayStatus
from dvadmin.system.models import CustomUser


def _big_text(n):
    return {"blocks": [{"type": "text", "text": "字" * n}]}


class TokenEstimateTests(TestCase):
    def test_estimate_tokens(self):
        self.assertEqual(estimate_tokens("abcd"), 1)
        self.assertEqual(estimate_tokens("a" * 400), 100)

    def test_estimate_entry_tokens(self):
        tree = SessionTree(session_id="s", play_id="p")
        e = tree.append("user_message", _big_text(400))
        self.assertGreaterEqual(estimate_entry_tokens(e), 100)


class ShouldCompactTests(TestCase):
    def setUp(self):
        self.tree = SessionTree(session_id="s", play_id="p")

    def test_below_threshold(self):
        cfg = CompactionConfig(context_window=1000, reserve_tokens=100, keep_recent_tokens=500)
        self.tree.append("user_message", _big_text(400))  # ~100 tokens
        self.assertFalse(should_compact(self.tree, cfg))

    def test_above_threshold(self):
        cfg = CompactionConfig(context_window=1000, reserve_tokens=100, keep_recent_tokens=500)
        for _ in range(10):
            self.tree.append("user_message", _big_text(400))  # ~1000 tokens
        self.assertTrue(should_compact(self.tree, cfg))

    def test_auto_compact_disabled(self):
        cfg = CompactionConfig(context_window=100, reserve_tokens=10, auto_compact=False)
        for _ in range(10):
            self.tree.append("user_message", _big_text(400))
        self.assertFalse(should_compact(self.tree, cfg))

    def test_tokens_drop_after_compaction(self):
        cfg = CompactionConfig(context_window=5000, reserve_tokens=500, keep_recent_tokens=900)
        for i in range(10):
            self.tree.append("user_message", _big_text(400))
        before = estimate_context_tokens(self.tree)
        Compactor(cfg).compact(self.tree)
        after = estimate_context_tokens(self.tree)
        self.assertLess(after, before)


class FindCutoffTests(TestCase):
    def test_cutoff_keeps_recent(self):
        tree = SessionTree(session_id="s", play_id="p")
        cfg = CompactionConfig(keep_recent_tokens=900)
        # 每条 ~110 tokens（含JSON开销），保留最近2条
        entries = [tree.append("user_message", _big_text(400)) for _ in range(6)]
        cutoff = Compactor(cfg).find_cutoff_point(entries)
        # cutoff之前被压缩，之后保留：保留部分token应<=300 + 一条的余量
        kept_tokens = sum(estimate_entry_tokens(e) for e in entries[cutoff:])
        self.assertLessEqual(kept_tokens, 900)
        self.assertGreater(cutoff, 0)

    def test_cutoff_zero_when_all_fit(self):
        tree = SessionTree(session_id="s", play_id="p")
        cfg = CompactionConfig(keep_recent_tokens=100000)
        entries = [tree.append("user_message", _big_text(100)) for _ in range(3)]
        # phase1 修复 1.3：无可压缩内容返回 None（0 曾兼作「压缩到头部」哨兵）
        self.assertIsNone(Compactor(cfg).find_cutoff_point(entries))


class CompactorTests(TestCase):
    def setUp(self):
        self.tree = SessionTree(session_id="s", play_id="p")

    def _fill(self, n, size=400):
        for i in range(n):
            self.tree.append("user_message", {"blocks": [{"type": "text", "text": f"消息{i}" + "字" * size}]})

    def test_compact_creates_entry(self):
        cfg = CompactionConfig(keep_recent_tokens=900)
        self._fill(6)
        result = Compactor(cfg).compact(self.tree)
        self.assertTrue(result.compacted)
        compactions = [e for e in self.tree.entries if e.type == "compaction"]
        self.assertEqual(len(compactions), 1)
        self.assertIn("summary", compactions[0].content)
        self.assertEqual(compactions[0].content["first_kept_entry_id"], result.first_kept_entry_id)

    def test_compact_preserves_originals(self):
        cfg = CompactionConfig(keep_recent_tokens=900)
        self._fill(6)
        n_before = len(self.tree.entries)
        Compactor(cfg).compact(self.tree)
        # 原始条目保留，只新增1个compaction
        self.assertEqual(len(self.tree.entries), n_before + 1)

    def test_no_compact_when_nothing(self):
        result = Compactor(CompactionConfig()).compact(self.tree)
        self.assertFalse(result.compacted)

    def test_no_compact_under_threshold(self):
        cfg = CompactionConfig(keep_recent_tokens=100000)
        self._fill(3, size=10)
        result = Compactor(cfg).compact(self.tree)
        self.assertFalse(result.compacted)

    def test_iterative_compaction_uses_previous_summary(self):
        cfg = CompactionConfig(keep_recent_tokens=900)
        seen = {}

        def spy(history, prev, instr):
            seen["prev"] = prev
            return "摘要X"

        self._fill(6)
        Compactor(cfg, summarizer=spy).compact(self.tree)
        self.assertEqual(seen["prev"], "")

        self._fill(6)
        Compactor(cfg, summarizer=spy).compact(self.tree)
        self.assertEqual(seen["prev"], "摘要X")  # 第二次传入第一次的摘要

    def test_custom_summarizer_output_stored(self):
        cfg = CompactionConfig(keep_recent_tokens=900)
        self._fill(6)
        result = Compactor(cfg, summarizer=lambda h, p, i: "定制摘要").compact(self.tree)
        self.assertEqual(result.summary, "定制摘要")

    def test_instructions_passed(self):
        cfg = CompactionConfig(keep_recent_tokens=900)
        seen = {}
        self._fill(6)
        Compactor(cfg, summarizer=lambda h, p, i: seen.setdefault("i", i) or "s").compact(
            self.tree, instructions="保留角色决策")
        self.assertEqual(seen["i"], "保留角色决策")


class EffectiveContextTests(TestCase):
    def test_no_compaction_returns_all(self):
        tree = SessionTree(session_id="s", play_id="p")
        e1 = tree.append("user_message", _big_text(10))
        e2 = tree.append("assistant_message", _big_text(10))
        entries = effective_context_entries(tree.get_active_branch())
        self.assertEqual([e.id for e in entries], [e1.id, e2.id])

    def test_compaction_replaces_old(self):
        tree = SessionTree(session_id="s", play_id="p")
        cfg = CompactionConfig(keep_recent_tokens=900)
        for i in range(6):
            tree.append("user_message", _big_text(400))
        Compactor(cfg, summarizer=lambda h, p, i: "S").compact(tree)
        entries = effective_context_entries(tree.get_active_branch())
        # 摘要 + 保留的最近条目
        self.assertEqual(entries[0].type, "compaction")
        self.assertLess(len(entries), 7)
        self.assertEqual(entries[0].content["summary"], "S")


class HeuristicSummaryTests(TestCase):
    def test_extracts_tools_and_lines(self):
        history = '[tool_call] {"name": "propose_edit"}\n[user_message] {"text": "改结局"}'
        s = heuristic_summary(history, "旧摘要", "聚焦角色")
        self.assertIn("propose_edit", s)
        self.assertIn("旧摘要", s)
        self.assertIn("聚焦角色", s)


class IntegrationTests(TestCase):
    """runner/会话树/压缩联动。"""

    def setUp(self):
        self.user = CustomUser.objects.create_user(username="cu", password="t")
        self.play = Play.objects.create(title="t", status=PlayStatus.DRAFT, created_by=self.user)
        self.config = AgentConfig.objects.create(name="c", created_by=self.user,
                                                 assistant_compaction={"keep_recent_tokens": 900})
        self.session = AssistantSession.objects.create(
            play=self.play, user=self.user, agent_config=self.config)

    def _tree_with_history(self):
        tree = SessionTreeManager.get_or_create(self.session)
        for i in range(6):
            tree.append("user_message", _big_text(400))
        SessionStorage.save(tree)
        return tree

    @override_settings(ENABLE_CONTEXT_COMPACTION=True)
    def test_compacted_history_blocks(self):
        tree = self._tree_with_history()
        Compactor(CompactionConfig(keep_recent_tokens=900),
                  summarizer=lambda h, p, i: "压缩摘要内容").compact(tree)
        SessionStorage.save(tree)
        msgs = compacted_history_blocks(self.session)
        self.assertIsNotNone(msgs)
        self.assertEqual(msgs[0]["role"], "user")
        self.assertIn(SUMMARY_PREFIX, msgs[0]["content"][0]["text"])
        self.assertIn("压缩摘要内容", msgs[0]["content"][0]["text"])

    def test_compacted_history_none_without_compaction(self):
        # 用全新session确保JSONL路径未被本类其他测试写过（pk复用会撞文件）
        session2 = AssistantSession.objects.create(
            play=self.play, user=self.user, agent_config=self.config)
        tree = SessionTreeManager.get_or_create(session2)
        for i in range(6):
            tree.append("user_message", _big_text(400))
        SessionStorage.save(tree)
        self.assertIsNone(compacted_history_blocks(session2))

    @override_settings(ENABLE_CONTEXT_COMPACTION=True)
    def test_compact_session_manual(self):
        self._tree_with_history()
        with patch("apps.creation.assistant.compaction.llm_summary",
                   return_value="LLM摘要") as mock_llm:
            result = compact_session(self.session, "保留主线")
        self.assertTrue(result.compacted)
        self.assertEqual(result.summary, "LLM摘要")
        # 验证写入了JSONL
        tree = SessionStorage.load(str(self.play.pk), str(self.session.pk))
        self.assertTrue(any(e.type == "compaction" for e in tree.entries))

    @override_settings(ENABLE_CONTEXT_COMPACTION=True)
    def test_compact_session_llm_fallback(self):
        """LLM摘要失败时降级为启发式，不抛错。"""
        self._tree_with_history()
        with patch("apps.creation.assistant.compaction.llm_summary",
                   side_effect=RuntimeError("provider down")):
            result = compact_session(self.session)
        self.assertTrue(result.compacted)
        self.assertTrue(result.summary)  # 启发式摘要兜底

    @override_settings(ENABLE_CONTEXT_COMPACTION=False)
    def test_flag_off_no_auto(self):
        from apps.creation.assistant.compaction import maybe_auto_compact
        self._tree_with_history()
        self.assertIsNone(maybe_auto_compact(self.session))


class SummaryModelTests(TestCase):
    """summary_model：摘要走更便宜的小模型（gateway chat 收到 model= 覆盖）。"""

    def setUp(self):
        from apps.ai_gateway.models import AIModel, AIProvider
        self.AIModel, self.AIProvider = AIModel, AIProvider
        self.user = CustomUser.objects.create_user(username="sm", password="t")
        self.play = Play.objects.create(title="t", status=PlayStatus.DRAFT, created_by=self.user)
        self.prov = AIProvider.objects.create(name="p", provider_type="LLM", adapter_type="deepseek",
                                              base_url="https://x", api_key_encrypted="", priority=1,
                                              retry_count=0)
        self.small = AIModel.objects.create(provider=self.prov, model_name="small", display_name="small",
                                            model_type="LLM", capabilities={"supports_tools": False},
                                            cost_config={"input_per_1k": 1, "output_per_1k": 2})
        self.config = AgentConfig.objects.create(name="c", created_by=self.user)
        self.session = AssistantSession.objects.create(
            play=self.play, user=self.user, agent_config=self.config)

    def _chat(self):
        return patch("apps.ai_gateway.gateway.AIGateway")

    def test_summary_model_passed_to_chat(self):
        """配置 summary_model=pk → chat 收到 model=该模型实例。"""
        self.config.assistant_compaction = {"summary_model": self.small.pk}
        self.config.save()
        with self._chat() as mock_cls:
            llm_summary(self.session, "历史", "", "")
        kwargs = mock_cls.return_value.chat.call_args.kwargs
        self.assertEqual(kwargs["model"], self.small)

    def test_no_summary_model_passes_none(self):
        """未配置 → chat 收到 model=None（走默认 assistant 候选链）。"""
        with self._chat() as mock_cls:
            llm_summary(self.session, "历史", "", "")
        kwargs = mock_cls.return_value.chat.call_args.kwargs
        self.assertIsNone(kwargs["model"])

    def test_invalid_summary_model_silent_fallback(self):
        """summary_model 无效 pk → 静默降级为 None，不抛错。"""
        self.config.assistant_compaction = {"summary_model": 999999}
        self.config.save()
        with self._chat() as mock_cls:
            llm_summary(self.session, "历史", "", "")
        kwargs = mock_cls.return_value.chat.call_args.kwargs
        self.assertIsNone(kwargs["model"])

    def test_inactive_summary_model_silent_fallback(self):
        """summary_model 已停用（is_active=False）→ 静默降级为 None。"""
        self.small.is_active = False
        self.small.save()
        self.config.assistant_compaction = {"summary_model": self.small.pk}
        self.config.save()
        with self._chat() as mock_cls:
            llm_summary(self.session, "历史", "", "")
        kwargs = mock_cls.return_value.chat.call_args.kwargs
        self.assertIsNone(kwargs["model"])


class CompactionApiTests(TestCase):
    """POST /sessions/{id}/compact/ 端点。"""

    def setUp(self):
        from dvadmin.system.models import UserRole
        self.user = CustomUser.objects.create_user(username="apiu", password="t")
        self.user.role = UserRole.PRODUCER
        self.user.save()
        self.play = Play.objects.create(title="t", status=PlayStatus.DRAFT, created_by=self.user)
        self.config = AgentConfig.objects.create(name="c", created_by=self.user)
        self.session = AssistantSession.objects.create(
            play=self.play, user=self.user, agent_config=self.config)

    def test_compact_endpoint(self):
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.user)
        with patch("apps.creation.assistant.compaction.llm_summary", return_value="摘要"):
            resp = client.post(f"/api/assistant/sessions/{self.session.pk}/compact/",
                               {"instructions": "重点角色"}, format="json")
        self.assertIn(resp.status_code, (200, 201))
        data = resp.json().get("data", resp.json())
        self.assertIn("summary", data)
        self.assertIn("compacted", data)

    def test_compact_endpoint_validates_instructions(self):
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.user)
        resp = client.post(f"/api/assistant/sessions/{self.session.pk}/compact/",
                           {"instructions": 123}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_compact_endpoint_permission(self):
        from rest_framework.test import APIClient
        other = CustomUser.objects.create_user(username="other", password="t")
        client = APIClient()
        client.force_authenticate(other)
        resp = client.post(f"/api/assistant/sessions/{self.session.pk}/compact/", {}, format="json")
        self.assertEqual(resp.status_code, 404)  # get_queryset过滤自己的会话


# ── Phase 0 max 审查缺陷修复（D 组）────────────────────────────────────────

class PairingGroupTests(TestCase):
    """4.1 压缩切割按配对分组：tool_use → reflection → tool_result 不可分割。"""

    def setUp(self):
        self.tree = SessionTree(session_id="s-pair", play_id="p")

    def _tool_group_entries(self):
        """构造 A(tool_use) R(reflection) T(tool_result) 的真实顺序。"""
        use = self.tree.append("assistant_message", {"blocks": [
            {"type": "text", "text": "查一下"},
            {"type": "tool_use", "id": "tu1", "name": "get_scene", "input": {}},
        ]})
        reflection = self.tree.append("reflection", {"tool": "get_scene", "consecutive_fails": 2})
        result = self.tree.append("tool_result", {
            "tool_use_id": "tu1", "blocks": [{"type": "tool_result", "tool_use_id": "tu1",
                                              "content": "场景数据"}]})
        return use, reflection, result

    def test_group_inseparable(self):
        """4.1 切割点落在 reflection 上时不再产生孤儿 tool_result。"""
        from apps.creation.assistant.compaction import Compactor, CompactionConfig, _tool_call_groups
        use, reflection, result = self._tool_group_entries()
        early = self.tree.append("user_message", _big_text(2000))  # 大头在早期
        entries = self.tree.entries
        groups = _tool_call_groups(entries)
        # tool_use/reflection/tool_result 三条必须在同一组
        ids = {e.id for e in entries}
        target = None
        for g in groups:
            if use.id in {entries[j].id for j in g}:
                target = g
                break
        self.assertIn(entries.index(reflection), target)
        self.assertIn(entries.index(result), target)

        comp = Compactor(CompactionConfig(keep_recent_tokens=50),  # 几乎全压
                         summarizer=lambda h, p, i: "sum")
        # 手动触发：以 keep_recent 极小值找切割点
        cutoff = comp.find_cutoff_point(entries)
        kept_types = [entries[i].type for i in range(cutoff, len(entries))]
        # 保留部分中 tool_use 数量 == tool_result 数量（无孤儿）
        n_use = sum(1 for t, e in zip(kept_types, entries[cutoff:])
                    if t == "assistant_message")
        # 直接断言：切割点不在组中间——tool_use 与其 tool_result 同去同留
        use_idx = entries.index(use)
        res_idx = entries.index(result)
        self.assertTrue((use_idx < cutoff and res_idx < cutoff)
                        or (use_idx >= cutoff and res_idx >= cutoff),
                        "tool_use 与 tool_result 必须同侧保留/压缩")

    def test_compact_no_orphan_tool_result(self):
        """4.1 端到端：压缩后历史无孤儿 tool_result（_pair_tool_results 无需修补）。"""
        from apps.creation.assistant.compaction import Compactor, CompactionConfig
        use, reflection, result = self._tool_group_entries()
        self.tree.append("user_message", _big_text(2000))
        comp = Compactor(CompactionConfig(keep_recent_tokens=60),
                         summarizer=lambda h, p, i: "sum")
        entry = comp.compact(self.tree)
        if entry.compacted:
            # first_kept 之后的条目集合：含 tool_use 则必含对应 tool_result
            kept = [e for e in self.tree.get_active_branch()
                    if e.type != "compaction"]
            fk = entry.first_kept_entry_id
            kept_ids = {e.id for e in kept}
            has_use = any(b.get("type") == "tool_use"
                          for e in kept if e.type == "assistant_message"
                          for b in (e.content or {}).get("blocks", []))
            has_result = any(e.type == "tool_result" for e in kept)
            if has_use and fk:
                self.assertTrue(has_result, "保留 tool_use 则必须保留对应 tool_result")
        else:
            self.fail("压缩不应整体拒绝（配对分组下总能安全切割早期历史）")


class OversizedEntryCompactionTests(TestCase):
    """4.2 单条超大结果不再使压缩永久拒绝。"""

    def test_single_oversized_group_still_compacts_earlier(self):
        """最新一组自身 ≥ keep_recent_tokens 时，其之前的历史仍被压缩。"""
        from apps.creation.assistant.compaction import Compactor, CompactionConfig
        tree = SessionTree(session_id="s-big", play_id="p")
        for _ in range(6):
            tree.append("user_message", _big_text(400))  # 早期 ~660 tokens
        huge = tree.append("user_message", _big_text(80000))  # ~20000+ tokens 单条
        entries = tree.entries
        cfg = CompactionConfig(keep_recent_tokens=20000)
        comp = Compactor(cfg, summarizer=lambda h, p, i: "sum")
        cutoff = comp.find_cutoff_point(entries)
        self.assertGreater(cutoff, 0, "单条超大时切割点不再是『拒绝压缩』哨兵")
        self.assertLessEqual(cutoff, entries.index(huge), "超大条本身保留")
        entry = comp.compact(tree)
        self.assertTrue(entry.compacted, "超大单条不再阻塞压缩")
        self.assertEqual(entry.first_kept_entry_id, huge.id)

    def test_all_fit_returns_zero(self):
        from apps.creation.assistant.compaction import Compactor, CompactionConfig
        tree = SessionTree(session_id="s-fit", play_id="p")
        for _ in range(3):
            tree.append("user_message", _big_text(100))
        entries = tree.entries
        cutoff = Compactor(CompactionConfig(keep_recent_tokens=100000)).find_cutoff_point(entries)
        self.assertIsNone(cutoff)


# ── phase1 max 审查修复（1.1–1.5）────────────────────────────────────────

def _entry_idx(entry, tree):
    return tree.entries.index(entry)


class Phase1TreeSnapshotTests(TestCase):
    """1.1/1.2：/tree/ 权威来源按 ENABLE_CONTEXT_COMPACTION 区分。"""

    def setUp(self):
        self.user = CustomUser.objects.create_user(username="p1tree", password="t")
        self.play = Play.objects.create(title="t", status=PlayStatus.DRAFT, created_by=self.user)
        self.config = AgentConfig.objects.create(name="c", created_by=self.user)
        self.session = AssistantSession.objects.create(
            play=self.play, user=self.user, agent_config=self.config)

    def _api(self):
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.user)
        return client

    def _tree_types(self):
        from apps.creation.assistant.session_tree import SessionStorage
        t = SessionStorage.load(str(self.play.pk), str(self.session.pk))
        return {e.type for e in t.entries}, {e.id for e in t.entries}

    def _seed_tree_with_compaction(self):
        """模拟压缩开启时的落盘树：含 compaction 与 branch_summary 条目。"""
        from apps.creation.assistant.session_tree import SessionStorage
        tree = SessionTreeManager.get_or_create(self.session)
        u = tree.append("user_message", _big_text(50))
        tree.append("assistant_message", _big_text(30))
        tree.append("compaction", {"summary": "压缩摘要", "first_kept_entry_id": None})
        tree.append("branch_summary", {"summary": "分支摘要"}, parent_id=u.id)
        SessionStorage.save(tree)

    @override_settings(ENABLE_CONTEXT_COMPACTION=True)
    def test_compaction_on_no_rebuild(self):
        """1.1：压缩开启时连续两次 GET /tree/ 不重建，压缩/摘要条目保留。"""
        self._seed_tree_with_compaction()
        client = self._api()
        url = f"/api/assistant/sessions/{self.session.pk}/tree/"
        self.assertEqual(client.get(url).status_code, 200)
        self.assertEqual(client.get(url).status_code, 200)
        types, _ = self._tree_types()
        self.assertIn("compaction", types)
        self.assertIn("branch_summary", types)

    @override_settings(ENABLE_CONTEXT_COMPACTION=False)
    def test_compaction_off_assistant_reply_not_stale(self):
        """1.2：compaction 关闭且含 assistant 回复时不恒判过期重建。"""
        from apps.creation.assistant.models import AssistantMessage
        AssistantMessage.objects.create(session=self.session, seq=0, role="user",
                                        content=[{"type": "text", "text": "hi"}])
        AssistantMessage.objects.create(session=self.session, seq=1, role="assistant",
                                        content=[{"type": "text", "text": "hello"}])
        client = self._api()
        url = f"/api/assistant/sessions/{self.session.pk}/tree/"
        self.assertEqual(client.get(url).status_code, 200)
        _, ids1 = self._tree_types()
        self.assertEqual(client.get(url).status_code, 200)
        _, ids2 = self._tree_types()
        self.assertEqual(ids1, ids2, "含 assistant 回复的会话不应每次刷新重建")
        self.assertIn("msg-1", ids2, "assistant 消息应被映射为条目")

    @override_settings(ENABLE_CONTEXT_COMPACTION=False)
    def test_compaction_off_new_message_triggers_rebuild(self):
        """1.2 反向：DB 领先（新消息未入树）仍触发重建。"""
        from apps.creation.assistant.models import AssistantMessage
        client = self._api()
        url = f"/api/assistant/sessions/{self.session.pk}/tree/"
        AssistantMessage.objects.create(session=self.session, seq=0, role="user",
                                        content=[{"type": "text", "text": "hi"}])
        client.get(url)
        AssistantMessage.objects.create(session=self.session, seq=1, role="assistant",
                                        content=[{"type": "text", "text": "new"}])
        client.get(url)
        _, ids = self._tree_types()
        self.assertIn("msg-1", ids)


class Phase1CutoffSemanticsTests(TestCase):
    """1.3：find_cutoff_point 返回 Optional[int]，单组超阈值可压缩。"""

    def _mk(self, username):
        user = CustomUser.objects.create_user(username=username, password="t")
        play = Play.objects.create(title="t", status=PlayStatus.DRAFT, created_by=user)
        config = AgentConfig.objects.create(name="c", created_by=user)
        session = AssistantSession.objects.create(play=play, user=user, agent_config=config)
        tree = SessionTreeManager.get_or_create(session)
        # 测试间 pk 复用会撞 JSONL 文件（已知坑）：清掉残留
        from apps.creation.assistant.session_tree import SessionStorage
        f = SessionStorage.get_session_file(str(play.pk), str(session.pk))
        if f.exists():
            f.unlink()
        return SessionTreeManager.get_or_create(session)

    def test_single_oversized_group_compacts_earlier_history(self):
        """待压缩区间仅剩一个超阈值工具组时仍产出有效压缩（压缩其之前历史）。"""
        tree = self._mk("p1cut")
        for _ in range(6):
            tree.append("user_message", _big_text(400))
        use = tree.append("assistant_message", {"blocks": [
            {"type": "tool_use", "id": "tu-big", "name": "get_play_overview", "input": {}},
        ]})
        tree.append("tool_result", {"blocks": [
            {"type": "tool_result", "tool_use_id": "tu-big", "content": "x" * 80000},
        ]})
        comp = Compactor(CompactionConfig(keep_recent_tokens=20000),
                         summarizer=lambda h, p, i: "sum")
        entry = comp.compact(tree)
        self.assertTrue(entry.compacted, "单工具组超阈值时应压缩其之前的早期历史")
        kept = [e for e in tree.get_active_branch() if e.type != "compaction"]
        self.assertIn(use, kept, "超阈值的最后一组本身保留")

    def test_all_within_threshold_returns_none_no_compaction_entry(self):
        """全部内容在保留阈值内 → None 且不产出压缩条目。"""
        tree = self._mk("p1cut2")
        for _ in range(3):
            tree.append("user_message", _big_text(50))
        comp = Compactor(CompactionConfig(keep_recent_tokens=100000),
                         summarizer=lambda h, p, i: "sum")
        self.assertIsNone(comp.find_cutoff_point(tree.get_active_branch()))
        entry = comp.compact(tree)
        self.assertFalse(entry.compacted)
        self.assertNotIn("compaction", {e.type for e in tree.entries})


class Phase1GroupingShapeTests(TestCase):
    """1.4/1.5：_tool_call_groups 按生产写入形状分组。"""

    def setUp(self):
        self.user = CustomUser.objects.create_user(username="p1grp", password="t")
        self.play = Play.objects.create(title="t", status=PlayStatus.DRAFT, created_by=self.user)
        self.config = AgentConfig.objects.create(name="c", created_by=self.user)
        self.session = AssistantSession.objects.create(
            play=self.play, user=self.user, agent_config=self.config)
        # 测试间 pk 复用会撞 JSONL 文件（已知坑）：清掉残留
        from apps.creation.assistant.session_tree import SessionStorage
        f = SessionStorage.get_session_file(str(self.play.pk), str(self.session.pk))
        if f.exists():
            f.unlink()

    def test_production_shape_groups(self):
        """生产形状（append_message 写入）：use→reflection→result 同组，用户消息独立。"""
        from apps.creation.assistant.compaction import _tool_call_groups
        tree = SessionTreeManager.get_or_create(self.session)
        SessionTreeManager.append_message(self.session, "user",
                                          [{"type": "text", "text": "查场景"}], tree=tree, save=False)
        SessionTreeManager.append_message(self.session, "assistant", [
            {"type": "text", "text": "好的"},
            {"type": "tool_use", "id": "tu-9", "name": "get_scene", "input": {}},
        ], tree=tree, save=False)
        tree.append("reflection", {"tool": "get_scene", "consecutive_fails": 2,
                                   "lessons_learned": "参数错了"})
        SessionTreeManager.append_message(self.session, "tool", [
            {"type": "tool_result", "tool_use_id": "tu-9", "content": "场景数据"},
        ], tree=tree, save=False)
        SessionTreeManager.append_message(self.session, "user",
                                          [{"type": "text", "text": "下一个请求"}], tree=tree, save=False)
        groups = _tool_call_groups(tree.entries)
        # 期望：[user] [assistant(use)+reflection+tool_result] [user]
        self.assertEqual(len(groups), 3, f"应为3组，实际 {groups}")
        self.assertEqual(len(groups[1]), 3, "工具组应含 use+reflection+result 三条")
        self.assertEqual(groups[2], [4], "最后的用户消息应独立成组")

    def test_legacy_top_level_shape_still_pairs(self):
        """顶层旧形状（content.tool_use_id）仍能配对。"""
        from apps.creation.assistant.compaction import _tool_call_groups
        tree = SessionTree(session_id="s-legacy", play_id="p")
        use = tree.append("assistant_message", {"blocks": [
            {"type": "tool_use", "id": "tu-L", "name": "get_scene", "input": {}}]})
        result = tree.append("tool_result", {"tool_use_id": "tu-L",
                                             "blocks": [{"type": "tool_result",
                                                         "tool_use_id": "tu-L", "content": "r"}]})
        after = tree.append("user_message", {"blocks": [{"type": "text", "text": "next"}]})
        groups = _tool_call_groups(tree.entries)
        self.assertEqual(len(groups), 2)
        self.assertIn(_entry_idx(use, tree), groups[0])
        self.assertIn(_entry_idx(result, tree), groups[0])
        self.assertEqual(groups[1], [_entry_idx(after, tree)])

    def test_unmatched_tool_result_glues_only_previous(self):
        """无归属的 tool_result 并入紧邻前一条目，不粘合后续条目。"""
        from apps.creation.assistant.compaction import _tool_call_groups
        tree = SessionTree(session_id="s-orphan", play_id="p")
        tree.append("user_message", {"blocks": [{"type": "text", "text": "a"}]})
        tree.append("tool_result", {"blocks": [{"type": "tool_result",
                                                "tool_use_id": "ghost", "content": "r"}]})
        after = tree.append("user_message", {"blocks": [{"type": "text", "text": "b"}]})
        groups = _tool_call_groups(tree.entries)
        self.assertEqual(len(groups), 2)
        self.assertEqual(groups[1], [_entry_idx(after, tree)])
