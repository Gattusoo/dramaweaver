"""树状会话历史测试（Phase 1.2）：学习Pi Agent。"""
import json
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from apps.creation.assistant.models import AssistantSession
from apps.creation.assistant.session_tree import (
    BRANCH_SUMMARY_INJECT_PREFIX,
    BranchSummarizer,
    SessionEntry,
    SessionStorage,
    SessionTree,
    SessionTreeManager,
    branch_summary_inject_messages,
    collect_branch_summaries_for_active,
)
from apps.creation.models import AgentConfig, Play, PlayStatus
from apps.creation.tests.test_assistant import AssistantFixtureMixin
from apps.core.system.models import CustomUser


class SessionTreeTests(TestCase):
    """树状会话测试。"""

    def setUp(self):
        self.user = CustomUser.objects.create_user(username="testuser", password="test")
        self.play = Play.objects.create(
            title="测试短剧",
            status=PlayStatus.DRAFT,
            created_by=self.user,
        )
        self.tree = SessionTree(
            session_id="test-session-1",
            play_id=str(self.play.pk),
        )

    def test_append_entry(self):
        """添加条目。"""
        entry = self.tree.append("user_message", {"text": "hello"})
        self.assertIsNotNone(entry.id)
        self.assertEqual(entry.type, "user_message")
        self.assertEqual(entry.content["text"], "hello")
        self.assertEqual(entry.parent_id, None)
        self.assertEqual(self.tree.active_entry_id, entry.id)

    def test_append_with_parent(self):
        """添加子条目。"""
        e1 = self.tree.append("user_message", {"text": "第一"})
        e2 = self.tree.append("assistant_message", {"text": "第二"}, parent_id=e1.id)
        self.assertEqual(e2.parent_id, e1.id)
        self.assertEqual(self.tree.active_entry_id, e2.id)

    def test_get_active_branch(self):
        """获取活动分支。"""
        e1 = self.tree.append("user_message", {"text": "第一"})
        e2 = self.tree.append("assistant_message", {"text": "第二"})
        e3 = self.tree.append("user_message", {"text": "第三"})

        branch = self.tree.get_active_branch()
        self.assertEqual(len(branch), 3)
        self.assertEqual([e.id for e in branch], [e1.id, e2.id, e3.id])

    def test_get_active_branch_empty(self):
        """空树返回空分支。"""
        self.assertEqual(self.tree.get_active_branch(), [])

    def test_navigate_to(self):
        """导航到某个条目。"""
        e1 = self.tree.append("user_message", {"text": "第一"})
        e2 = self.tree.append("assistant_message", {"text": "第二"})
        e3 = self.tree.append("user_message", {"text": "第三"})

        self.assertTrue(self.tree.navigate_to(e1.id))
        self.assertEqual(self.tree.active_entry_id, e1.id)

        # 活动分支应该只包含e1
        branch = self.tree.get_active_branch()
        self.assertEqual(len(branch), 1)
        self.assertEqual(branch[0].id, e1.id)

    def test_navigate_to_invalid(self):
        """导航到不存在的条目。"""
        self.assertFalse(self.tree.navigate_to("non-existent"))

    def test_create_branch(self):
        """创建分支。"""
        e1 = self.tree.append("user_message", {"text": "第一"})
        e2 = self.tree.append("assistant_message", {"text": "第二"})

        # 从e1创建分支
        e3 = self.tree.create_branch(e1.id, {"text": "分支"})
        self.assertIsNotNone(e3)
        self.assertEqual(e3.parent_id, e1.id)
        self.assertEqual(self.tree.active_entry_id, e3.id)

        # 活动分支应该是 e1 -> e3
        branch = self.tree.get_active_branch()
        self.assertEqual([e.id for e in branch], [e1.id, e3.id])

    def test_get_children(self):
        """获取子条目。"""
        e1 = self.tree.append("user_message", {"text": "第一"})
        e2 = self.tree.append("assistant_message", {"text": "第二"}, parent_id=e1.id)
        e3 = self.tree.append("user_message", {"text": "第三"}, parent_id=e1.id)

        children = self.tree.get_children(e1.id)
        self.assertEqual(len(children), 2)
        self.assertIn(e2, children)
        self.assertIn(e3, children)

    def test_get_parent(self):
        """获取父条目。"""
        e1 = self.tree.append("user_message", {"text": "第一"})
        e2 = self.tree.append("assistant_message", {"text": "第二"}, parent_id=e1.id)

        parent = self.tree.get_parent(e2.id)
        self.assertEqual(parent.id, e1.id)

        # 根节点没有父条目
        self.assertIsNone(self.tree.get_parent(e1.id))

    def test_get_branch_path(self):
        """获取到某个条目的路径。"""
        e1 = self.tree.append("user_message", {"text": "第一"})
        e2 = self.tree.append("assistant_message", {"text": "第二"}, parent_id=e1.id)
        e3 = self.tree.append("user_message", {"text": "第三"}, parent_id=e2.id)

        path = self.tree.get_branch_path(e3.id)
        self.assertEqual([e.id for e in path], [e1.id, e2.id, e3.id])

    def test_fork(self):
        """Fork：获取完整分支路径。"""
        e1 = self.tree.append("user_message", {"text": "第一"})
        e2 = self.tree.append("assistant_message", {"text": "第二"}, parent_id=e1.id)

        forked = self.tree.fork(e2.id)
        self.assertEqual([e.id for e in forked], [e1.id, e2.id])

    def test_active_branch(self):
        """活动分支即根到当前活动条目的路径。"""
        e1 = self.tree.append("user_message", {"text": "第一"})
        e2 = self.tree.append("assistant_message", {"text": "第二"}, parent_id=e1.id)

        branch = self.tree.get_active_branch()
        self.assertEqual([e.id for e in branch], [e1.id, e2.id])

    def test_get_tree_structure(self):
        """获取树的结构化表示。"""
        e1 = self.tree.append("user_message", {"text": "第一"})
        e2 = self.tree.append("assistant_message", {"text": "第二"}, parent_id=e1.id)
        e3 = self.tree.append("user_message", {"text": "第三"}, parent_id=e2.id)

        structure = self.tree.get_tree_structure()
        self.assertIsNotNone(structure)
        self.assertEqual(structure["type"], "user_message")
        self.assertEqual(len(structure["children"]), 1)
        self.assertEqual(structure["children"][0]["type"], "assistant_message")

    def test_get_tree_structure_empty(self):
        """空树返回虚拟根。"""
        structure = self.tree.get_tree_structure()
        self.assertEqual(structure["type"], "empty")

    def test_get_tree_structure_max_depth(self):
        """限制深度。"""
        e1 = self.tree.append("user_message", {"text": "第一"})
        e2 = self.tree.append("assistant_message", {"text": "第二"}, parent_id=e1.id)
        e3 = self.tree.append("user_message", {"text": "第三"}, parent_id=e2.id)

        structure = self.tree.get_tree_structure(max_depth=1)
        # 深度1应该只显示根和第一层子节点
        self.assertIsNotNone(structure)

    def test_to_jsonl(self):
        """序列化为JSONL。"""
        e1 = self.tree.append("user_message", {"text": "第一"})
        e2 = self.tree.append("assistant_message", {"text": "第二"})

        jsonl = self.tree.to_jsonl()
        lines = jsonl.strip().split("\n")
        self.assertEqual(len(lines), 2)

        # 每行应该是有效的JSON
        for line in lines:
            data = json.loads(line)
            self.assertIn("id", data)
            self.assertIn("type", data)

    def test_from_jsonl(self):
        """从JSONL反序列化。"""
        e1 = self.tree.append("user_message", {"text": "第一"})
        e2 = self.tree.append("assistant_message", {"text": "第二"})

        jsonl = self.tree.to_jsonl()
        tree2 = SessionTree.from_jsonl(jsonl, "test-1", "play-1")

        self.assertEqual(len(tree2.entries), 2)
        self.assertEqual(tree2.active_entry_id, e2.id)

    def test_entry_types(self):
        """支持多种条目类型。"""
        types = ["user_message", "assistant_message", "tool_call", "tool_result", "plan", "reflection"]
        for t in types:
            entry = self.tree.append(t, {"data": "test"})
            self.assertEqual(entry.type, t)


class SessionStorageTests(TestCase):
    """JSONL持久化测试。"""

    def setUp(self):
        self.user = CustomUser.objects.create_user(username="testuser2", password="test")
        self.play = Play.objects.create(
            title="测试短剧2",
            status=PlayStatus.DRAFT,
            created_by=self.user,
        )

    def test_get_session_file(self):
        """获取会话文件路径。"""
        from django.conf import settings
        file_path = SessionStorage.get_session_file("play-123", "session-456")
        self.assertTrue(str(file_path).endswith("session-456.jsonl"))
        self.assertIn("play-123", str(file_path))

    def test_save_and_load(self):
        """保存和加载会话树。"""
        tree = SessionTree(
            session_id="test-save",
            play_id=str(self.play.pk),
        )
        tree.append("user_message", {"text": "第一"})
        tree.append("assistant_message", {"text": "第二"})

        # 保存
        file_path = SessionStorage.save(tree)
        self.assertTrue(file_path.exists())

        # 加载
        loaded = SessionStorage.load(str(self.play.pk), "test-save")
        self.assertIsNotNone(loaded)
        self.assertEqual(len(loaded.entries), 2)
        self.assertEqual(loaded.active_entry_id, tree.active_entry_id)

    def test_load_nonexistent(self):
        """加载不存在的会话。"""
        loaded = SessionStorage.load(str(self.play.pk), "nonexistent")
        self.assertIsNone(loaded)


class SessionTreeManagerTests(TestCase):
    """会话树管理器测试。"""

    def setUp(self):
        self.user = CustomUser.objects.create_user(username="testuser3", password="test")
        self.play = Play.objects.create(
            title="测试短剧3",
            status=PlayStatus.DRAFT,
            created_by=self.user,
        )
        self.config = AgentConfig.objects.create(name="test", created_by=self.user)
        self.session = AssistantSession.objects.create(
            play=self.play,
            user=self.user,
            agent_config=self.config,
        )

    def test_get_or_create_new(self):
        """从数据库重建新会话树。"""
        tree = SessionTreeManager.get_or_create(self.session)
        self.assertIsNotNone(tree)
        self.assertEqual(tree.session_id, str(self.session.pk))
        self.assertEqual(tree.play_id, str(self.session.play_id))

    def test_get_or_create_cached(self):
        """从JSONL加载已存在的会话树。"""
        # 先保存一个空树（没有条目）
        tree1 = SessionTreeManager.get_or_create(self.session)
        initial_count = len(tree1.entries)
        tree1.append("user_message", {"text": "test"})
        SessionStorage.save(tree1)

        # 再次加载应该从JSONL读取（包含新增的条目）
        tree2 = SessionTreeManager.get_or_create(self.session)
        self.assertEqual(len(tree2.entries), initial_count + 1)
        # 验证最后一条是新添加的
        self.assertEqual(tree2.entries[-1].content["text"], "test")

    def test_append_message(self):
        """添加消息到会话树。"""
        entry = SessionTreeManager.append_message(
            self.session,
            "user",
            [{"type": "text", "text": "hello"}],
        )
        self.assertIsNotNone(entry)
        self.assertEqual(entry.type, "user_message")


class BranchSummarizerTests(TestCase):
    """分支摘要测试（Phase 1.4）。"""

    def setUp(self):
        self.tree = SessionTree(session_id="test", play_id="play-1")

    def test_divergent_segment(self):
        """分叉段 = from_branch 中 fork_point 之后。"""
        e1 = self.tree.append("user_message", {"text": "根"})
        e2 = self.tree.append("assistant_message", {"text": "回复"})
        e3 = self.tree.append("assistant_message", {"blocks": [
            {"type": "tool_use", "id": "tu-d", "name": "get_episode", "input": {}},
        ]})
        e4 = self.tree.append("tool_result", {"x": 1})
        abandoned, fork = BranchSummarizer.divergent_segment(
            [e1, e2, e3, e4], [e1, e2])
        self.assertEqual([e.id for e in abandoned], [e3.id, e4.id])
        self.assertEqual(fork.id, e2.id)

    def test_divergent_segment_no_common(self):
        e1 = self.tree.append("user_message", {"text": "a"})
        e2 = self.tree.append("user_message", {"text": "b"})
        abandoned, fork = BranchSummarizer.divergent_segment([e1, e2], [e1])
        self.assertEqual([e.id for e in abandoned], [e2.id])
        self.assertEqual(fork.id, e1.id)

    def test_should_summarize_with_work(self):
        """放弃段有工具调用时需要摘要（生产形状：tool_use 块）。"""
        e1 = self.tree.append("user_message", {"blocks": [{"type": "text", "text": "任务"}]})
        e2 = self.tree.append("assistant_message", {"blocks": [
            {"type": "tool_use", "id": "tu-1", "name": "propose_edit", "input": {}},
        ]})

        from_branch = [e1, e2]
        to_branch = [e1]

        self.assertTrue(BranchSummarizer.should_summarize(self.tree, from_branch, to_branch))

    def test_should_summarize_no_work(self):
        """放弃段只有消息时不需要摘要。"""
        e1 = self.tree.append("user_message", {"blocks": [{"type": "text", "text": "你好"}]})
        e2 = self.tree.append("assistant_message", {"blocks": [{"type": "text", "text": "你好"}]})

        from_branch = [e1, e2]
        to_branch = [e1]

        self.assertFalse(BranchSummarizer.should_summarize(self.tree, from_branch, to_branch))

    def test_should_summarize_work_before_fork(self):
        """工具在公共段（未放弃）→ 不摘要。"""
        e1 = self.tree.append("assistant_message", {"blocks": [
            {"type": "tool_use", "id": "tu-0", "name": "get_play_overview", "input": {}},
        ]})
        e2 = self.tree.append("user_message", {"blocks": [{"type": "text", "text": "继续"}]})
        # 导航目标包含e1，被放弃的只有e2（纯消息）
        self.assertFalse(BranchSummarizer.should_summarize(self.tree, [e1, e2], [e1]))

    def test_collect_cumulative(self):
        """累积文件跟踪：读/写目标。"""
        def _use(name, args):
            return self.tree.append("assistant_message", {"blocks": [
                {"type": "tool_use", "id": f"tu-{name}", "name": name, "input": args},
            ]})
        e1 = _use("get_episode_script", {"episode_number": 3})
        e2 = _use("propose_episode_script_edit", {"episode_number": 3})
        e3 = _use("get_shot", {"shot_id": 7})
        cum = BranchSummarizer.collect_cumulative([e1, e2, e3])
        self.assertIn("3", cum["read_files"])
        self.assertIn("3", cum["modified_files"])
        self.assertIn("7", cum["read_files"])
        self.assertEqual(set(cum["tools"]), {"get_episode_script", "propose_episode_script_edit", "get_shot"})

    def test_summarize_appends_entry_with_cumulative(self):
        """摘要条目含 read_files/modified_files/summarized_entry_ids。"""
        e1 = self.tree.append("user_message", {"blocks": [{"type": "text", "text": "根"}]})
        e2 = self.tree.append("assistant_message", {"blocks": [
            {"type": "tool_use", "id": "tu-2", "name": "propose_episode_script_edit",
             "input": {"episode_number": 5}},
        ]}, parent_id=e1.id)
        summary = BranchSummarizer.summarize_branch(self.tree, [e1, e2], [e1])
        self.assertIn("propose_episode_script_edit", summary)
        entry = self.tree.entries[-1]
        self.assertEqual(entry.type, "branch_summary")
        self.assertEqual(entry.parent_id, e1.id)  # 挂在分叉点下
        self.assertIn("5", entry.content["modified_files"])
        self.assertEqual(entry.content["summarized_entry_ids"], [e2.id])

    def test_summarize_uses_llm_summarizer(self):
        """注入的summarizer生效。"""
        e1 = self.tree.append("assistant_message", {"blocks": [
            {"type": "tool_use", "id": "tu-a", "name": "get_play_overview", "input": {}},
        ]})
        s = BranchSummarizer.summarize_branch(
            self.tree, [e1], None, summarizer=lambda h, c: "LLM分支摘要")
        self.assertEqual(s, "LLM分支摘要")

    def test_summarize_llm_fallback(self):
        """LLM摘要失败降级启发式。"""
        e1 = self.tree.append("assistant_message", {"blocks": [
            {"type": "tool_use", "id": "tu-b", "name": "get_play", "input": {}},
        ]})
        def boom(h, c):
            raise RuntimeError("down")
        s = BranchSummarizer.summarize_branch(self.tree, [e1], None, summarizer=boom)
        self.assertIn("get_play", s)

    def test_summary_visible_on_new_branch(self):
        """1.4.4：摘要挂分叉点→新分支路径可见。"""
        e1 = self.tree.append("user_message", {"blocks": [{"type": "text", "text": "根"}]})
        e2 = self.tree.append("assistant_message", {"blocks": [
            {"type": "tool_use", "id": "tu-c", "name": "propose_edit", "input": {}},
        ]}, parent_id=e1.id)
        e3 = self.tree.append("tool_result", {"ok": True}, parent_id=e2.id)
        # 模拟导航回e1：摘要挂到e1下
        BranchSummarizer.summarize_branch(self.tree, [e1, e2, e3], [e1])
        self.tree.navigate_to(e1.id)
        # 新分支从e1延伸
        e_new = self.tree.append("user_message", {"text": "换个方案"}, parent_id=e1.id)
        # 摘要条目是e1的子节点之一（与e_new并列）
        children_types = {c.type for c in self.tree.get_children(e1.id)}
        self.assertIn("branch_summary", children_types)

    def test_on_navigate_no_switch(self):
        """导航到当前活动条目：无摘要。"""
        user = CustomUser.objects.create_user(username="nav", password="t")
        play = Play.objects.create(title="t", status=PlayStatus.DRAFT, created_by=user)
        config = AgentConfig.objects.create(name="c", created_by=user)
        session = AssistantSession.objects.create(play=play, user=user, agent_config=config)
        e1 = self.tree.append("user_message", {"text": "hi"})
        self.assertIsNone(BranchSummarizer.on_navigate(session, self.tree, e1.id))


class BranchSummaryInjectTests(TestCase):
    """Phase B：分支摘要注入模型上下文（挂分叉点下的兄弟叶节点须被主动收集）。"""

    def setUp(self):
        self.tree = SessionTree(session_id="s-inject", play_id="p1")

    def test_collect_no_summary(self):
        """无分支摘要 → 空列表。"""
        self.tree.append("user_message", {"text": "hi"})
        self.assertEqual(collect_branch_summaries_for_active(self.tree), [])

    def test_collect_summary_off_active_path(self):
        """挂分叉点下的摘要（不在活动路径上）→ 被收集到。"""
        e1 = self.tree.append("user_message", {"text": "根"})
        e2 = self.tree.append("assistant_message", {"blocks": [
            {"type": "tool_use", "id": "tu-i", "name": "get_scene", "input": {}},
        ]}, parent_id=e1.id)
        e3 = self.tree.append("tool_result", {"ok": True}, parent_id=e2.id)
        # 生成摘要（挂 e1 下），随后导航回 e1 走新分支
        BranchSummarizer.summarize_branch(self.tree, [e1, e2, e3], [e1])
        self.tree.navigate_to(e1.id)
        self.tree.append("user_message", {"text": "换方案"}, parent_id=e1.id)
        # 摘要不在活动路径（e1→新user）上，但应被收集
        summaries = collect_branch_summaries_for_active(self.tree)
        self.assertEqual(len(summaries), 1)
        self.assertTrue(summaries[0])

    def test_inject_message_has_source_prefix(self):
        """注入消息标注来源前缀，避免模型误认为当前分支历史。"""
        e1 = self.tree.append("user_message", {"text": "根"})
        e2 = self.tree.append("assistant_message", {"blocks": [
            {"type": "tool_use", "id": "tu-i", "name": "get_scene", "input": {}},
        ]}, parent_id=e1.id)
        BranchSummarizer.summarize_branch(self.tree, [e1, e2], [e1])
        self.tree.navigate_to(e1.id)
        msgs = branch_summary_inject_messages(self.tree)
        self.assertEqual(len(msgs), 1)
        self.assertEqual(msgs[0]["role"], "user")
        text = msgs[0]["content"][0]["text"]
        self.assertIn(BRANCH_SUMMARY_INJECT_PREFIX, text)
        self.assertIn("不是当前对话历史", text)

    def test_inject_empty_when_no_summary(self):
        """无摘要 → 不注入任何消息。"""
        self.tree.append("user_message", {"text": "hi"})
        self.assertEqual(branch_summary_inject_messages(self.tree), [])

    def test_inject_multiple_summaries_deduped(self):
        """多个分叉点的摘要都收集；同一摘要不重复。"""
        e1 = self.tree.append("user_message", {"text": "根"})
        e2 = self.tree.append("assistant_message", {"blocks": [
            {"type": "tool_use", "id": "tu-i", "name": "get_scene", "input": {}},
        ]}, parent_id=e1.id)
        BranchSummarizer.summarize_branch(self.tree, [e1, e2], [e1])  # 摘要A挂e1
        self.tree.navigate_to(e1.id)
        e3 = self.tree.append("assistant_message", {"text": "新分支"}, parent_id=e1.id)
        e4 = self.tree.append("assistant_message", {"blocks": [
            {"type": "tool_use", "id": "tu-j", "name": "get_shot", "input": {}},
        ]}, parent_id=e3.id)
        BranchSummarizer.summarize_branch(self.tree, [e1, e3, e4], [e3])  # 摘要B挂e3
        self.tree.navigate_to(e3.id)
        summaries = collect_branch_summaries_for_active(self.tree)
        self.assertEqual(len(summaries), 2)


class BranchSummaryInjectRunnerTests(AssistantFixtureMixin, TestCase):
    """Phase B 集成：runner 构建模型请求时确实把分支摘要注入到对话首部。"""

    def setUp(self):
        super().setUp()
        # 本类会向 media/sessions/{play}/{session}.jsonl 落盘 branch_summary，
        # 而 AssistantFixtureMixin 用确定性低 pk，文件会跨测试类泄漏（污染 RunnerTests）。
        # 测试后删除本会话 JSONL，避免 pk 复用撞文件。
        self.addCleanup(self._cleanup_session_file)

    def _cleanup_session_file(self):
        from apps.creation.assistant.session_tree import SessionStorage
        try:
            SessionStorage.get_session_file(str(self.play.pk), str(self.session.pk)).unlink()
        except OSError:
            pass

    def _seed_branch_summary(self):
        """在会话树里造一个挂分叉点下的 branch_summary（不在活动路径上）并落盘。"""
        tree = SessionTreeManager.get_or_create(self.session)
        e1 = tree.append("user_message", {"text": "最初目标"})
        e2 = tree.append("assistant_message", {"blocks": [
            {"type": "tool_use", "id": "tu-k", "name": "get_scene", "input": {}},
        ]}, parent_id=e1.id)
        e3 = tree.append("tool_result", {"ok": True}, parent_id=e2.id)
        BranchSummarizer.summarize_branch(tree, [e1, e2, e3], [e1])
        tree.navigate_to(e1.id)
        SessionStorage.save(tree)
        return tree

    @override_settings(ENABLE_CONTEXT_COMPACTION=False)
    def test_branch_summary_reaches_model(self):
        from apps.creation.assistant.runner import run_assistant_turn
        from apps.creation.tests.test_assistant import FakeChatGateway, _say
        self._seed_branch_summary()
        gw = FakeChatGateway([_say("好的，我了解了之前的探索")])
        record = self._make_record()
        with patch("apps.ai_gateway.gateway.AIGateway", return_value=gw):
            run_assistant_turn(record.pk)
        # 模型收到的首条消息应含分支摘要来源前缀
        first = gw.calls[0][0]
        self.assertEqual(first["role"], "user")
        self.assertIn(BRANCH_SUMMARY_INJECT_PREFIX, first["content"][0]["text"])

    @override_settings(ENABLE_BRANCH_SUMMARY=False)
    def test_flag_off_no_injection(self):
        from apps.creation.assistant.runner import run_assistant_turn
        from apps.creation.tests.test_assistant import FakeChatGateway, _say
        self._seed_branch_summary()
        gw = FakeChatGateway([_say("好的")])
        record = self._make_record()
        with patch("apps.ai_gateway.gateway.AIGateway", return_value=gw):
            run_assistant_turn(record.pk)
        first = gw.calls[0][0]
        self.assertNotIn(BRANCH_SUMMARY_INJECT_PREFIX, first["content"][0]["text"])

    def _make_record(self):
        from apps.creation.models import TaskRecord, TaskType
        from apps.creation.assistant.runner import _save
        record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                           input_summary={"session_id": self.session.pk})
        _save(self.session, "user", [{"type": "text", "text": "继续"}], record)
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        return record


class DeterministicRebuildTests(AssistantFixtureMixin, APITestCase):
    """Phase C：/tree 重建 ID 稳定化（确定性 ID msg-{pk}，修复 flag-off 下 navigate 404）。"""

    def _seed_messages(self):
        """造几条 AssistantMessage（否则空会话重建无条目）。"""
        from apps.creation.assistant.runner import _save
        _save(self.session, "user", [{"type": "text", "text": "q1"}], None)
        _save(self.session, "assistant", [{"type": "text", "text": "a1"}], None)
        _save(self.session, "user", [{"type": "text", "text": "q2"}], None)

    def test_rebuild_ids_deterministic(self):
        """同一会话两次从 DB 重建得到相同 ID 集合。"""
        self._seed_messages()
        tree1 = SessionTreeManager._rebuild_from_db(self.session)
        tree2 = SessionTreeManager._rebuild_from_db(self.session)
        ids1 = {e.id for e in tree1.entries}
        ids2 = {e.id for e in tree2.entries}
        self.assertTrue(ids1)
        self.assertEqual(ids1, ids2)
        # ID 形如 msg-{pk}
        self.assertTrue(all(i.startswith("msg-") for i in ids1))

    def test_append_still_uses_uuid(self):
        """compaction 开启时真实追加的条目仍用 uuid（确定性 ID 仅 DB 重建路径）。"""
        self._seed_messages()
        tree = SessionTreeManager._rebuild_from_db(self.session)
        entry = tree.append("user_message", {"text": "新"})
        import uuid as _uuid
        try:
            _uuid.UUID(entry.id)
        except ValueError:
            self.fail(f"新追加条目应为 uuid 格式，实际 {entry.id}")

    @override_settings(ENABLE_CONTEXT_COMPACTION=False)
    def test_tree_endpoint_ids_stable_across_requests(self):
        """flag-off：GET /tree/ 两次返回相同 entry id 集合与 active_entry_id。"""
        self._seed_messages()
        self.client.force_authenticate(self.producer)
        url = f"/api/assistant/sessions/{self.session.pk}/tree/"
        resp1 = self.client.get(url)
        self.assertEqual(resp1.status_code, 200, resp1.data)
        resp2 = self.client.get(url)
        self.assertEqual(resp2.status_code, 200, resp2.data)
        self.assertEqual(resp1.data["active_entry_id"], resp2.data["active_entry_id"])

        def collect_ids(node, acc):
            acc.add(node["id"])
            for c in node.get("children", []):
                collect_ids(c, acc)
        ids1, ids2 = set(), set()
        collect_ids(resp1.data["tree"], ids1)
        collect_ids(resp2.data["tree"], ids2)
        self.assertTrue(ids1)
        self.assertEqual(ids1, ids2)

    @override_settings(ENABLE_CONTEXT_COMPACTION=False)
    def test_navigate_with_rebuilt_id_succeeds(self):
        """flag-off：用 GET /tree/ 返回的确定性 entry_id 导航成功（不再 404）。"""
        self._seed_messages()
        self.client.force_authenticate(self.producer)
        tree_url = f"/api/assistant/sessions/{self.session.pk}/tree/"
        tree_resp = self.client.get(tree_url).data

        def first_id(node):
            if node.get("children"):
                return first_id(node["children"][0])
            return node["id"]
        entry_id = first_id(tree_resp["tree"])

        nav_url = f"/api/assistant/sessions/{self.session.pk}/tree/navigate/"
        nav_resp = self.client.post(nav_url, {"entry_id": entry_id}, format="json")
        self.assertEqual(nav_resp.status_code, 200, nav_resp.data)
        self.assertEqual(nav_resp.data["active_entry_id"], entry_id)


# ── Phase 0 max 审查缺陷修复（D 组：导航与快照）──────────────────────────────

class TreeSnapshotFollowsDbTests(AssistantFixtureMixin, APITestCase):
    """4.4 flag-off 下 /tree 快照跟随数据库增长（不再永久停留在首次落盘）。"""

    @override_settings(ENABLE_CONTEXT_COMPACTION=False)
    def test_tree_grows_with_new_messages(self):
        from apps.creation.assistant.runner import _save
        from django.core.files.storage import default_storage
        # 干净起点：删除可能存在的旧快照（同 pk 会话复用会撞文件）
        SessionStorage.get_session_file(str(self.session.play_id), str(self.session.pk)).unlink(missing_ok=True)
        self.client.force_authenticate(self.producer)
        url = f"/api/assistant/sessions/{self.session.pk}/tree/"

        _save(self.session, "user", [{"type": "text", "text": "q1"}], None)
        resp1 = self.client.get(url)
        self.assertEqual(resp1.status_code, 200, resp1.data)
        self.assertEqual(resp1.data["total_entries"], 1)

        # 新增 3 条消息（flag-off 不写树）→ 再次 GET /tree/ 应看到 4 条
        _save(self.session, "assistant", [{"type": "text", "text": "a1"}], None)
        _save(self.session, "user", [{"type": "text", "text": "q2"}], None)
        _save(self.session, "assistant", [{"type": "text", "text": "a2"}], None)
        resp2 = self.client.get(url)
        self.assertEqual(resp2.status_code, 200, resp2.data)
        self.assertEqual(resp2.data["total_entries"], 4,
                         "快照须跟随数据库消息增长")

        # 用最新消息的确定性 entry_id 导航成功
        def last_id(node):
            if node.get("children"):
                return last_id(node["children"][-1])
            return node["id"]
        latest = last_id(resp2.data["tree"])
        nav_resp = self.client.post(
            f"/api/assistant/sessions/{self.session.pk}/tree/navigate/",
            {"entry_id": latest}, format="json")
        self.assertEqual(nav_resp.status_code, 200, nav_resp.data)

    @override_settings(ENABLE_CONTEXT_COMPACTION=False)
    def test_navigated_model_history_excludes_abandoned(self):
        """4.3 导航后 _history_for_llm 不含被放弃分支（flag-off 同样生效）。"""
        from apps.creation.assistant.runner import _history_for_llm, _save
        SessionStorage.get_session_file(str(self.session.play_id), str(self.session.pk)).unlink(missing_ok=True)
        # turn1
        m1 = _save(self.session, "user", [{"type": "text", "text": "turn1-start"}], None)
        _save(self.session, "assistant", [{"type": "text", "text": "turn1-answer"}], None)
        # turn2（将被放弃）
        _save(self.session, "user", [{"type": "text", "text": "turn2-abandoned"}], None)
        _save(self.session, "assistant", [{"type": "text", "text": "turn2-answer"}], None)

        # 未导航：历史 = 全量
        hist0 = _history_for_llm(self.session)
        flat0 = json.dumps(hist0, ensure_ascii=False)
        self.assertIn("turn2-abandoned", flat0)

        # 落盘快照（GET /tree/）后导航回第 1 轮
        self.client.force_authenticate(self.producer)
        tree_resp = self.client.get(
            f"/api/assistant/sessions/{self.session.pk}/tree/").data

        def first_id(node):
            # DB 重建为线性链（每条 parent 上一条）：根节点即第一轮；
            # 顶层节点 id 为 "root"（虚拟根）时取其首个孩子
            if node["id"] == "root":
                return node["children"][0]["id"]
            return node["id"]
        first = first_id(tree_resp["tree"])
        nav_resp = self.client.post(
            f"/api/assistant/sessions/{self.session.pk}/tree/navigate/",
            {"entry_id": first}, format="json")
        self.assertEqual(nav_resp.status_code, 200, nav_resp.data)

        hist1 = _history_for_llm(self.session)
        flat1 = json.dumps(hist1, ensure_ascii=False)
        self.assertIn("turn1-start", flat1, "导航目标分支仍在上下文中")
        self.assertNotIn("turn2-abandoned", flat1,
                          "被放弃的 turn2 不得进入模型上下文")

    def test_unnavigated_history_unchanged(self):
        """4.3 从未导航时 _history_for_llm 保持全量（与修复前一致）。"""
        from apps.creation.assistant.runner import _history_for_llm, _save
        SessionStorage.get_session_file(str(self.session.play_id), str(self.session.pk)).unlink(missing_ok=True)
        _save(self.session, "user", [{"type": "text", "text": "u1"}], None)
        _save(self.session, "assistant", [{"type": "text", "text": "a1"}], None)
        hist = _history_for_llm(self.session)
        flat = json.dumps(hist, ensure_ascii=False)
        self.assertIn("u1", flat)
        self.assertIn("a1", flat)


# ── phase1 max 审查修复（2.1–2.5）────────────────────────────────────────

class Phase1BranchSummaryShapeTests(TestCase):
    """2.1：分支摘要对齐真实条目类型。"""

    def setUp(self):
        self.user = CustomUser.objects.create_user(username="p1bs", password="t")
        self.play = Play.objects.create(title="t", status=PlayStatus.DRAFT, created_by=self.user)
        self.config = AgentConfig.objects.create(name="c", created_by=self.user)
        self.session = AssistantSession.objects.create(
            play=self.play, user=self.user, agent_config=self.config)

    def test_abandoned_branch_tools_and_lessons_in_summary(self):
        """被放弃分支含 propose_episode_script_edit 调用与反思时，
        摘要包含工具名与反思教训，不是通用占位文本。"""
        from apps.creation.assistant.session_tree import SessionTree
        tree = SessionTree(session_id=str(self.session.pk), play_id=str(self.play.pk))
        tree.append("user_message", {"blocks": [{"type": "text", "text": "改第三集"}]})
        tree.append("assistant_message", {"blocks": [
            {"type": "text", "text": "好的"},
            {"type": "tool_use", "id": "tu-p1", "name": "propose_episode_script_edit",
             "input": {"episode_number": 3}},
        ]})
        tree.append("tool_result", {"blocks": [
            {"type": "tool_result", "tool_use_id": "tu-p1",
             "content": "提案已创建 proposal ok"}]})
        tree.append("reflection", {"tool": "propose_episode_script_edit",
                                   "lessons_learned": "提案前要先确认分集存在",
                                   "fail_count": 2})
        summary = BranchSummarizer.summarize_branch(tree, tree.entries[:4], tree.entries[:1])
        self.assertIn("propose_episode_script_edit", summary)
        self.assertIn("提案前要先确认分集存在", summary)
        self.assertNotEqual(summary, "在另一个分支中进行了一些探索")


class Phase1InjectAlternationTests(TestCase):
    """2.3：注入摘要与本轮用户消息合并，不产生相邻同角色。"""

    def setUp(self):
        self.user = CustomUser.objects.create_user(username="p1alt", password="t")
        self.play = Play.objects.create(title="t", status=PlayStatus.DRAFT, created_by=self.user)
        self.config = AgentConfig.objects.create(name="c", created_by=self.user)
        self.session = AssistantSession.objects.create(
            play=self.play, user=self.user, agent_config=self.config)
        from apps.creation.assistant.session_tree import SessionStorage
        f = SessionStorage.get_session_file(str(self.play.pk), str(self.session.pk))
        if f.exists():
            f.unlink()

    def test_prefix_merged_with_user_history(self):
        """存在分支摘要注入且本轮有用户消息时，messages 无相邻 user。"""
        from apps.creation.assistant.runner import _history_for_llm, _save
        from apps.creation.assistant.session_tree import SessionTree, SessionStorage
        tree = SessionTree(session_id=str(self.session.pk), play_id=str(self.play.pk))
        u = tree.append("user_message", {"blocks": [{"type": "text", "text": "根"}]})
        tree.append("assistant_message", {"blocks": [
            {"type": "tool_use", "id": "tu-x", "name": "get_scene", "input": {}}]})
        tree.append("branch_summary", {"summary": "探索过场景"}, parent_id=u.id)
        SessionStorage.save(tree)
        # 本轮用户消息（flag-off 不入树）
        _save(self.session, "user", [{"type": "text", "text": "当前请求"}], None)

        hist = _history_for_llm(self.session)
        roles = [m["role"] for m in hist]
        for a, b in zip(roles, roles[1:]):
            self.assertNotEqual(a, b, f"存在相邻同角色: {roles}")

    def test_no_prefix_sequence_unchanged(self):
        """无注入时序列不变（首条就是本轮用户消息）。"""
        from apps.creation.assistant.runner import _history_for_llm, _save
        from apps.creation.assistant.session_tree import SessionStorage
        SessionStorage.get_session_file(str(self.play.pk), str(self.session.pk)).unlink(missing_ok=True)
        _save(self.session, "user", [{"type": "text", "text": "q"}], None)
        _save(self.session, "assistant", [{"type": "text", "text": "a"}], None)
        hist = _history_for_llm(self.session)
        self.assertEqual([m["role"] for m in hist], ["user", "assistant"])


class Phase1NavigatedFlagTests(TestCase):
    """2.4：导航状态显式标记（navigated_at）。"""

    def setUp(self):
        self.user = CustomUser.objects.create_user(username="p1nav", password="t")
        self.play = Play.objects.create(title="t", status=PlayStatus.DRAFT, created_by=self.user)
        self.config = AgentConfig.objects.create(name="c", created_by=self.user)
        self.session = AssistantSession.objects.create(
            play=self.play, user=self.user, agent_config=self.config)

    def test_navigated_flag_survives_append(self):
        """导航后 append 一条消息，_tree_navigated 仍为真。"""
        from apps.creation.assistant.runner import _tree_navigated
        from apps.creation.assistant.session_tree import SessionTree
        tree = SessionTree(session_id=str(self.session.pk), play_id=str(self.play.pk))
        tree.append("user_message", {"blocks": [{"type": "text", "text": "a"}]})
        tree.append("user_message", {"blocks": [{"type": "text", "text": "b"}]})
        tree.navigate_to(tree.entries[0].id)
        tree.append("user_message", {"blocks": [{"type": "text", "text": "c"}]},
                    parent_id=tree.entries[0].id)
        self.assertTrue(_tree_navigated(tree), "导航点成为尾条目后标记仍应为真")

    def test_never_navigated_false(self):
        """从未导航的会话为假。"""
        from apps.creation.assistant.runner import _tree_navigated
        from apps.creation.assistant.session_tree import SessionTree
        tree = SessionTree(session_id=str(self.session.pk), play_id=str(self.play.pk))
        tree.append("user_message", {"blocks": [{"type": "text", "text": "a"}]})
        tree.append("user_message", {"blocks": [{"type": "text", "text": "b"}]})
        self.assertFalse(_tree_navigated(tree))

    def test_old_format_jsonl_means_unnavigated(self):
        """旧格式 JSONL（无 navigated_at 字段）加载后视为未导航。"""
        from apps.creation.assistant.runner import _tree_navigated
        from apps.creation.assistant.session_tree import SessionTree
        tree = SessionTree(session_id=str(self.session.pk), play_id=str(self.play.pk))
        tree.append("user_message", {"blocks": [{"type": "text", "text": "a"}]})
        # 手工构造旧格式：_meta 行只有 id，无 navigated_at
        import json as _json
        lines = [_json.dumps({"_meta": "active", "id": tree.entries[0].id}, ensure_ascii=False)]
        lines += [_json.dumps(e.to_dict(), ensure_ascii=False) for e in tree.entries]
        loaded = SessionTree.from_jsonl("\n".join(lines), str(self.session.pk), str(self.play.pk))
        self.assertFalse(_tree_navigated(loaded))


class Phase1MergeCompletedTurnsTests(AssistantFixtureMixin, APITestCase):
    """2.5：导航后已完成轮次并入模型历史，被放弃轮次不并入。"""

    def setUp(self):
        super().setUp()
        from apps.creation.assistant.session_tree import SessionStorage
        SessionStorage.get_session_file(str(self.play.pk), str(self.session.pk)).unlink(missing_ok=True)
        self.addCleanup(SessionStorage.get_session_file(str(self.play.pk), str(self.session.pk)).unlink,
                        missing_ok=True)

    @override_settings(ENABLE_CONTEXT_COMPACTION=False)
    def test_completed_turn_after_navigation_included(self):
        """导航到第 1 轮后完成第 3 轮：第 4 轮历史含第 3 轮内容、不含第 2 轮。"""
        from apps.creation.assistant.runner import _history_for_llm, _save
        from django.utils import timezone
        import time

        # turn1
        _save(self.session, "user", [{"type": "text", "text": "t1-q"}], None)
        _save(self.session, "assistant", [{"type": "text", "text": "t1-a"}], None)
        # turn2（将被放弃）
        _save(self.session, "user", [{"type": "text", "text": "t2-abandoned"}], None)
        _save(self.session, "assistant", [{"type": "text", "text": "t2-a"}], None)

        # 落盘快照并导航回 turn1
        self.client.force_authenticate(self.producer)
        tree_resp = self.client.get(f"/api/assistant/sessions/{self.session.pk}/tree/").data

        def first_id(node):
            if node["id"] == "root":
                return node["children"][0]["id"]
            return node["id"]
        nav_resp = self.client.post(
            f"/api/assistant/sessions/{self.session.pk}/tree/navigate/",
            {"entry_id": first_id(tree_resp["tree"])}, format="json")
        self.assertEqual(nav_resp.status_code, 200, nav_resp.data)

        # 导航时刻之后完成 turn3（新分支上的轮次）
        time.sleep(0.05)
        nav_time = timezone.now()
        m3u = _save(self.session, "user", [{"type": "text", "text": "t3-q"}], None)
        m3a = _save(self.session, "assistant", [{"type": "text", "text": "t3-a"}], None)
        # created_at 是 auto_now_add，确保 > 导航时刻
        from apps.creation.assistant.models import AssistantMessage
        AssistantMessage.objects.filter(pk__in=[m3u.pk, m3a.pk]).update(created_at=nav_time)

        hist = _history_for_llm(self.session)
        flat = json.dumps(hist, ensure_ascii=False)
        self.assertIn("t1-q", flat, "导航目标分支仍在上下文")
        self.assertNotIn("t2-abandoned", flat, "被放弃的 turn2 不得进入上下文")
        self.assertIn("t3-q", flat, "导航后完成的 turn3 应并入历史")
        self.assertIn("t3-a", flat)
        roles = [m["role"] for m in hist]
        for a, b in zip(roles, roles[1:]):
            self.assertNotEqual(a, b, f"存在相邻同角色: {roles}")


# ── phase1 max 审查修复 7.1/7.2：导航端点权限与预算 ─────────────────────

class Phase1NavigateGuardTests(AssistantFixtureMixin, APITestCase):
    """7.1：只读 403、运行中 409；7.2：摘要调用计入预算 + 限流。"""

    def setUp(self):
        super().setUp()
        SessionStorage.get_session_file(str(self.session.play_id), str(self.session.pk)).unlink(missing_ok=True)
        self.addCleanup(SessionStorage.get_session_file(
            str(self.session.play_id), str(self.session.pk)).unlink, missing_ok=True)
        # 造树：含工具调用的分支（可摘要）+ 多条消息
        from apps.creation.assistant.runner import _save
        _save(self.session, "user", [{"type": "text", "text": "t1"}], None)
        _save(self.session, "assistant", [{"type": "text", "text": "a1"}], None)
        _save(self.session, "user", [{"type": "text", "text": "t2"}], None)
        _save(self.session, "assistant", [{"type": "text", "text": "a2"}], None)
        self.client.force_authenticate(self.producer)
        tree_resp = self.client.get(
            f"/api/assistant/sessions/{self.session.pk}/tree/").data

        def first_id(node):
            if node["id"] == "root":
                return node["children"][0]["id"]
            return node["id"]
        self.first_entry = first_id(tree_resp["tree"])
        self.nav_url = f"/api/assistant/sessions/{self.session.pk}/tree/navigate/"

    @override_settings(ENABLE_CONTEXT_COMPACTION=False)
    def test_viewer_403_tree_unchanged(self):
        """7.1：只读成员（自己的会话、只读的剧）导航 403，树不变、无模型调用。"""
        from unittest.mock import patch as _patch
        from apps.creation.assistant.session_tree import SessionTreeManager
        # 会话属于 viewer（queryset 按 user 过滤），viewer 对剧是 VIEWER 成员
        v_session = AssistantSession.objects.create(
            play=self.play, user=self.viewer, agent_config=self.config)
        nav_url = f"/api/assistant/sessions/{v_session.pk}/tree/navigate/"
        before = SessionTreeManager.get_or_create(v_session)
        self.client.force_authenticate(self.viewer)
        with _patch("apps.ai_gateway.gateway.AIGateway") as gw:
            resp = self.client.post(nav_url, {"entry_id": "any-entry"},
                                    format="json")
        self.assertEqual(resp.status_code, 403, resp.data)
        gw.assert_not_called()
        after = SessionTreeManager.get_or_create(v_session)
        self.assertEqual(after.active_entry_id, before.active_entry_id, "树不变")

    @override_settings(ENABLE_CONTEXT_COMPACTION=False)
    def test_running_task_409(self):
        """7.1：轮次运行中导航 409。"""
        from apps.creation.models import TaskRecord, TaskStatus, TaskType
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.ASSISTANT_TURN, status=TaskStatus.PENDING,
            input_summary={"session_id": self.session.pk})
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        self.session.refresh_from_db()
        resp = self.client.post(self.nav_url, {"entry_id": self.first_entry},
                                format="json")
        self.assertEqual(resp.status_code, 409, resp.data)

    @override_settings(ENABLE_CONTEXT_COMPACTION=False, ENABLE_BRANCH_SUMMARY=True)
    def test_summary_cost_attributed(self):
        """7.2：导航产生分支摘要时调用写入 CostRecord 并关联会话费用。"""
        from unittest.mock import patch as _patch
        from apps.ai_gateway.cost_tracker import record_cost
        from apps.creation.assistant.session_tree import SessionTree
        # 树里放真实工具调用形状（触发摘要）+ 导航回早期条目
        tree = SessionTreeManager.get_or_create(self.session)
        u = tree.append("user_message", {"blocks": [{"type": "text", "text": "查"}]})
        tree.append("assistant_message", {"blocks": [
            {"type": "tool_use", "id": "tu-nav", "name": "get_scene", "input": {}}]})
        tree.append("tool_result", {"blocks": [
            {"type": "tool_result", "tool_use_id": "tu-nav", "content": "ok"}]})
        SessionStorage.save(tree)
        with _patch("apps.ai_gateway.gateway.AIGateway") as gw:
            gw.return_value.chat.return_value = {
                "content_blocks": [{"type": "text", "text": "分支摘要文本"}],
                "usage": {"input_tokens": 100, "output_tokens": 50}}
            resp = self.client.post(self.nav_url, {"entry_id": u.id}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        gw.return_value.chat.assert_called_once()
        # chat 收到 task_record（费用归因链路接通）
        self.assertIsNotNone(gw.return_value.chat.call_args.kwargs.get("task_record"))

    @override_settings(ENABLE_CONTEXT_COMPACTION=False)
    def test_throttle_429(self):
        """7.2：超过限流阈值返回 429。"""
        from apps.ai_gateway.rate_limiter import RateLimiter
        # try_acquire 返回 None=拒绝（租约化后 None 是拒绝语义，空串/token 是成功）
        with patch.object(RateLimiter, "try_acquire", return_value=None):
            resp = self.client.post(self.nav_url, {"entry_id": self.first_entry},
                                    format="json")
        self.assertEqual(resp.status_code, 429, resp.data)
