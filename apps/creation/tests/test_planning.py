"""规划引擎测试（Phase 1.6）：复杂度评估、计划分解、动态更新、反思。"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.creation.assistant.models import AssistantSession, Plan, PlanRevision, PlanStatus
from apps.creation.assistant.planning import (
    COMPLEXITY_PLAN_THRESHOLD,
    COMPLEXITY_TOT_THRESHOLD,
    FailureTracker,
    PlanningEngine,
    _extract_json_array,
    _heuristic_candidates,
    _heuristic_steps,
    _normalize_candidates,
    _revise_steps,
    advance_plan_for_tool,
    estimate_task_complexity,
    maybe_plan,
    maybe_tot,
    reflect_on_failure,
    tot_explore,
)
from apps.creation.models import AgentConfig, Play, PlayStatus, TaskStatus
from apps.creation.tests.test_assistant import AssistantFixtureMixin, FakeChatGateway, _say, _use
from dvadmin.system.models import CustomUser


def _mk_session(user=None):
    user = user or CustomUser.objects.create_user(username="pl", password="t")
    play = Play.objects.create(title="t", status=PlayStatus.DRAFT, created_by=user)
    config = AgentConfig.objects.create(name="c", created_by=user)
    return AssistantSession.objects.create(play=play, user=user, agent_config=config)


class ComplexityTests(TestCase):
    def test_simple_task_low(self):
        self.assertLess(estimate_task_complexity("读取第3集剧本"), COMPLEXITY_PLAN_THRESHOLD)

    def test_complex_task_high(self):
        score = estimate_task_complexity("修改第3集剧本节奏并优化全部对白")
        self.assertGreater(score, COMPLEXITY_PLAN_THRESHOLD)

    def test_multi_goal_increases(self):
        simple = estimate_task_complexity("改第3集")
        multi = estimate_task_complexity("改第3集和第5集，同时优化节奏并重写大纲")
        self.assertGreater(multi, simple)

    def test_empty_text(self):
        self.assertEqual(estimate_task_complexity(""), 0.0)

    def test_many_targets(self):
        text = "检查第1集、第2集、第3集、第4集的节奏"
        self.assertGreaterEqual(estimate_task_complexity(text), 0.3)


class PlanCreationTests(TestCase):
    def setUp(self):
        self.session = _mk_session()

    def test_create_with_llm_steps(self):
        steps = [{"id": 1, "description": "读取剧本", "depends_on": []},
                 {"id": 2, "description": "分析节奏", "depends_on": [1]},
                 {"id": 3, "description": "提交修改提案", "depends_on": [2]}]
        engine = PlanningEngine(self.session, planner=lambda g, c, t: steps)
        plan = engine.create_plan("修改第3集节奏")
        self.assertIsNotNone(plan.pk)
        self.assertEqual(len(plan.steps), 3)
        self.assertEqual(plan.steps[1]["depends_on"], [1])
        self.assertEqual(plan.steps[0]["status"], "PENDING")
        self.assertEqual(plan.status, PlanStatus.ACTIVE)

    def test_create_fallback_heuristic(self):
        engine = PlanningEngine(self.session, planner=lambda g, c, t: [])
        plan = engine.create_plan("目标X")
        self.assertGreaterEqual(len(plan.steps), 3)  # 启发式4步模板

    def test_create_llm_failure_fallback(self):
        def boom(g, c, t):
            raise RuntimeError("provider down")
        engine = PlanningEngine(self.session, planner=boom)
        plan = engine.create_plan("目标Y")
        self.assertGreaterEqual(len(plan.steps), 3)

    def test_max_active_plans(self):
        for _ in range(10):
            PlanningEngine(self.session, planner=lambda g, c, t: []).create_plan("g")
        plan = PlanningEngine(self.session, planner=lambda g, c, t: []).create_plan("g11")
        self.assertIsNone(plan)

    def test_progress(self):
        plan = Plan.objects.create(session=self.session, goal="g", steps=[
            {"id": 1, "description": "a", "status": "COMPLETED"},
            {"id": 2, "description": "b", "status": "PENDING"},
        ])
        self.assertAlmostEqual(plan.get_progress(), 0.5)


class StepStatusTests(TestCase):
    def setUp(self):
        self.session = _mk_session()
        self.plan = Plan.objects.create(session=self.session, goal="g", steps=[
            {"id": 1, "description": "a", "status": "PENDING"},
            {"id": 2, "description": "b", "status": "PENDING"},
        ])

    def test_mark_step(self):
        self.assertTrue(PlanningEngine.mark_step(self.plan, 1, "IN_PROGRESS"))
        self.assertTrue(PlanningEngine.mark_step(self.plan, 1, "COMPLETED"))
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.steps[0]["status"], "COMPLETED")

    def test_mark_step_invalid_status(self):
        self.assertFalse(PlanningEngine.mark_step(self.plan, 1, "PENDING"))
        self.assertFalse(PlanningEngine.mark_step(self.plan, 1, "BOGUS"))

    def test_finish_plan(self):
        PlanningEngine.finish_plan(self.plan, PlanStatus.COMPLETED)
        self.plan.refresh_from_db()
        self.assertEqual(self.plan.status, PlanStatus.COMPLETED)

    def test_finish_plan_invalid(self):
        with self.assertRaises(ValueError):
            PlanningEngine.finish_plan(self.plan, "RUNNING")


class UpdatePlanTests(TestCase):
    def setUp(self):
        self.session = _mk_session()
        self.engine = PlanningEngine(self.session, planner=lambda g, c, t: [])

    def _plan(self, statuses):
        return Plan.objects.create(session=self.session, goal="g", steps=[
            {"id": i + 1, "description": f"s{i+1}", "status": st}
            for i, st in enumerate(statuses)])

    def test_should_update_half_done(self):
        plan = self._plan(["COMPLETED", "COMPLETED", "COMPLETED", "PENDING", "PENDING"])
        self.assertTrue(PlanningEngine.should_update_plan(plan))

    def test_should_update_blocked(self):
        plan = self._plan(["PENDING", "PENDING"])
        self.assertTrue(PlanningEngine.should_update_plan(plan, blocked=True))

    def test_should_update_failed(self):
        plan = self._plan(["COMPLETED", "FAILED", "PENDING"])
        self.assertTrue(PlanningEngine.should_update_plan(plan))

    def test_should_not_update_early(self):
        plan = self._plan(["COMPLETED", "PENDING", "PENDING", "PENDING"])
        self.assertFalse(PlanningEngine.should_update_plan(plan))

    def test_should_not_update_finished(self):
        plan = self._plan(["COMPLETED", "COMPLETED"])
        plan.status = PlanStatus.COMPLETED
        self.assertFalse(PlanningEngine.should_update_plan(plan))

    def test_update_saves_revision(self):
        plan = self._plan(["COMPLETED", "FAILED", "PENDING"])
        plan = self.engine.update_plan(plan, reason="步骤2失败")
        self.assertEqual(plan.revisions.count(), 1)
        # FAILED 重置为 PENDING
        self.assertEqual(plan.steps[1]["status"], "PENDING")
        self.assertIn("重试", plan.steps[1].get("note", ""))

    def test_update_no_change_no_revision(self):
        plan = self._plan(["COMPLETED", "PENDING"])
        plan = self.engine.update_plan(plan, reason="无变化")
        self.assertEqual(plan.revisions.count(), 0)


class FailureTrackerTests(TestCase):
    def test_consecutive_fails(self):
        t = FailureTracker()
        self.assertEqual(t.record("propose_edit", True), 1)
        self.assertEqual(t.record("propose_edit", True), 2)
        self.assertEqual(t.record("propose_edit", False), 0)
        self.assertEqual(t.record("propose_edit", True), 1)

    def test_worst(self):
        t = FailureTracker()
        t.record("a", True); t.record("a", True); t.record("b", True)
        name, count = t.worst()
        self.assertEqual((name, count), ("a", 2))


class ReflectionTests(TestCase):
    def setUp(self):
        self.session = _mk_session()

    def test_reflect_fields(self):
        r = reflect_on_failure(self.session, "propose_edit",
                               [{"error": "第3集不存在"}], plan=None,
                               reflector=lambda **kw: {
                                   "root_cause": "集数不存在",
                                   "lessons_learned": "先列集数",
                                   "strategy_adjustment": {"prefer": "list_episodes"}})
        self.assertEqual(r.root_cause, "集数不存在")
        self.assertEqual(r.lessons_learned, "先列集数")
        self.assertEqual(r.strategy_adjustment["prefer"], "list_episodes")

    def test_reflect_heuristic_fallback(self):
        r = reflect_on_failure(self.session, "get_episode",
                               [{"error": "第99集不存在"}], reflector=lambda **kw: {})
        self.assertIn("不存在", r.root_cause)
        self.assertTrue(r.lessons_learned)

    def test_reflect_writes_tree(self):
        from apps.creation.assistant.session_tree import SessionTreeManager
        # 独立 play+session 避免 JSONL 文件与同 pk 的先前测试冲突
        me = self.session.user
        play2 = Play.objects.create(title="t2", status=PlayStatus.DRAFT, created_by=me)
        session2 = AssistantSession.objects.create(
            play=play2, user=me, agent_config=self.session.agent_config)
        reflect_on_failure(session2, "x", [{"e": 1}, {"e": 2}], reflector=lambda **kw: {})
        tree = SessionTreeManager.get_or_create(session2)
        refs = [e for e in tree.entries if e.type == "reflection"]
        self.assertTrue(any(r.content.get("tool") == "x" for r in refs))
        self.assertEqual(refs[-1].content["tool"], "x")


class MaybePlanTests(TestCase):
    def setUp(self):
        self.session = _mk_session()

    @override_settings(ENABLE_PLANNING=False)
    def test_flag_off(self):
        self.assertIsNone(maybe_plan(self.session, "改全部集节奏并重写大纲", []))

    @override_settings(ENABLE_PLANNING=True)
    def test_simple_task_skips(self):
        self.assertIsNone(maybe_plan(self.session, "读取第3集", []))

    @override_settings(ENABLE_PLANNING=True)
    def test_complex_creates(self):
        with patch("apps.creation.assistant.planning.llm_plan", return_value=[
                {"id": 1, "description": "a"}, {"id": 2, "description": "b"}]):
            plan = maybe_plan(self.session, "修改第3集节奏并优化全部对白与悬念", [])
        self.assertIsNotNone(plan)
        self.assertEqual(len(plan.steps), 2)

    @override_settings(ENABLE_PLANNING=True)
    def test_no_duplicate_active(self):
        Plan.objects.create(session=self.session, goal="已有", steps=[], status=PlanStatus.ACTIVE)
        self.assertIsNone(maybe_plan(self.session, "修改第3集节奏并重写全部", []))


@override_settings(ENABLE_PLANNING=True)
class AdvancePlanTests(TestCase):
    """Phase A：工具调用驱动活跃计划步骤推进（映射逻辑单测，flag 开启）。"""

    def setUp(self):
        from apps.creation.assistant import events
        self.events = events
        events.set_backend(events.MemoryStreams())
        self.addCleanup(events.set_backend, None)
        self.session = _mk_session()
        self.plan = Plan.objects.create(
            session=self.session, goal="改第3集", status=PlanStatus.ACTIVE,
            steps=[
                {"id": 1, "description": "读取剧本", "depends_on": [], "status": "PENDING"},
                {"id": 2, "description": "分析节奏", "depends_on": [1], "status": "PENDING"},
                {"id": 3, "description": "提交提案", "depends_on": [2], "status": "PENDING"},
            ])

    def _step(self, sid):
        return next(s for s in self.plan.steps if s["id"] == sid)

    def _reload(self):
        self.plan.refresh_from_db(fields=["steps", "status"])

    def test_no_active_plan_noop(self):
        Plan.objects.filter(pk=self.plan.pk).update(status=PlanStatus.ABANDONED)
        self.assertIsNone(advance_plan_for_tool(self.session, "get_scene", False))

    def test_read_tool_starts_first_runnable(self):
        advance_plan_for_tool(self.session, "get_episode_script", False)
        self._reload()
        self.assertEqual(self._step(1)["status"], "IN_PROGRESS")
        self.assertEqual(self._step(2)["status"], "PENDING")  # 依赖未满足不推进

    def test_read_tool_respects_dependencies(self):
        # 步骤1已在进行，只读工具不应跳到步骤2（依赖1未完成）
        Plan.objects.filter(pk=self.plan.pk).update(steps=[
            {"id": 1, "description": "读取剧本", "depends_on": [], "status": "IN_PROGRESS"},
            {"id": 2, "description": "分析节奏", "depends_on": [1], "status": "PENDING"}])
        advance_plan_for_tool(self.session, "get_outline", False)
        self._reload()
        self.assertEqual(self._step(2)["status"], "PENDING")

    def test_write_tool_completes_current(self):
        Plan.objects.filter(pk=self.plan.pk).update(steps=[
            {"id": 1, "description": "读取剧本", "depends_on": [], "status": "IN_PROGRESS"},
            {"id": 2, "description": "提交提案", "depends_on": [1], "status": "PENDING"}])
        advance_plan_for_tool(self.session, "propose_scene_edit", False)
        self._reload()
        self.assertEqual(self._step(1)["status"], "COMPLETED")

    def test_error_records_note_not_failed(self):
        Plan.objects.filter(pk=self.plan.pk).update(steps=[
            {"id": 1, "description": "读取剧本", "depends_on": [], "status": "IN_PROGRESS"}])
        advance_plan_for_tool(self.session, "get_scene", True)
        self._reload()
        self.assertEqual(self._step(1)["status"], "IN_PROGRESS")  # 不轻易置 FAILED
        self.assertIn("失败", self._step(1).get("note", ""))

    def test_all_done_finishes_plan(self):
        Plan.objects.filter(pk=self.plan.pk).update(steps=[
            {"id": 1, "description": "读取剧本", "depends_on": [], "status": "IN_PROGRESS"}])
        advance_plan_for_tool(self.session, "propose_outline_edit", False)
        self.plan.refresh_from_db(fields=["status"])
        self.assertEqual(self.plan.status, PlanStatus.COMPLETED)

    def test_progress_event_emitted(self):
        advance_plan_for_tool(self.session, "get_episode_script", False)
        evs = [e["type"] for e in self.events.read(self.session.pk)[0]]
        self.assertIn("plan_progress", evs)

    def test_no_change_no_event(self):
        # 纯闲聊工具（不在读/写集合）且无 IN_PROGRESS → 不变更不发事件
        advance_plan_for_tool(self.session, "ask_user", False)
        evs = [e["type"] for e in self.events.read(self.session.pk)[0]]
        self.assertNotIn("plan_progress", evs)

    def test_revision_triggered_on_failed_step(self):
        # 出现 FAILED 步骤 → should_update_plan 为真，默认启发式重置 FAILED→PENDING → 创建 PlanRevision
        Plan.objects.filter(pk=self.plan.pk).update(steps=[
            {"id": 1, "description": "a", "depends_on": [], "status": "COMPLETED"},
            {"id": 2, "description": "b", "depends_on": [], "status": "FAILED"},
            {"id": 3, "description": "c", "depends_on": [2], "status": "PENDING"}])
        advance_plan_for_tool(self.session, "propose_scene_edit", False)  # 无 IN_PROGRESS，不推进
        # FAILED 触发修订（FAILED→PENDING），产生 PlanRevision
        self.assertTrue(PlanRevision.objects.filter(plan=self.plan).exists())

    def test_exception_does_not_propagate(self):
        with patch("apps.creation.assistant.planning.PlanningEngine.active_plan",
                   side_effect=RuntimeError("boom")):
            # advance_plan_for_tool 内部不吞异常，由 runner 兜底；此处验证调用方 try/except 语义
            with self.assertRaises(RuntimeError):
                advance_plan_for_tool(self.session, "get_scene", False)


class RunnerPlanAdvanceTests(AssistantFixtureMixin, TestCase):
    """Phase A 集成：runner 循环内工具调用确实推进计划步骤（ENABLE_PLANNING 门控）。

    复用 AssistantFixtureMixin 的真实 fixtures（第3集/场景/剧本），工具才不会因数据缺失而报错。"""

    def setUp(self):
        super().setUp()
        self.plan = Plan.objects.create(
            session=self.session, goal="改第3集节奏", status=PlanStatus.ACTIVE,
            steps=[
                {"id": 1, "description": "读取剧本", "depends_on": [], "status": "PENDING"},
                {"id": 2, "description": "提交提案", "depends_on": [1], "status": "PENDING"},
            ])

    def _run_turn(self, script):
        from apps.creation.models import TaskRecord, TaskType
        from apps.creation.assistant.runner import _save, run_assistant_turn
        from apps.creation.tests.test_assistant import FakeChatGateway
        record = TaskRecord.objects.create(play=self.play,
                                           task_type=TaskType.ASSISTANT_TURN,
                                           input_summary={"session_id": self.session.pk})
        _save(self.session, "user", [{"type": "text", "text": "改第3集节奏"}], record)
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        with patch("apps.ai_gateway.gateway.AIGateway", return_value=FakeChatGateway(script)):
            run_assistant_turn(record.pk)
        self.plan.refresh_from_db(fields=["steps", "status"])
        return self.plan

    @override_settings(ENABLE_PLANNING=True)
    def test_turn_advances_read_then_write(self):
        plan = self._run_turn([
            _use("t1", "get_episode_script", {"episode_number": 3}),
            _use("t2", "propose_scene_edit", {"scene_id": self.scene.pk, "summary": "s",
                                              "description": "她亮出录音"}),
            _say("已生成提案"),
        ])
        step1 = next(s for s in plan.steps if s["id"] == 1)
        self.assertEqual(step1["status"], "COMPLETED")  # 读→IN_PROGRESS，提案→COMPLETED
        self.assertEqual(plan.get_progress(), 0.5)

    @override_settings(ENABLE_PLANNING=False)
    def test_flag_off_no_advance(self):
        plan = self._run_turn([
            _use("t1", "get_episode_script", {"episode_number": 3}),
            _say("ok"),
        ])
        step1 = next(s for s in plan.steps if s["id"] == 1)
        self.assertEqual(step1["status"], "PENDING")  # flag 关闭不推进


class JsonExtractTests(TestCase):
    def test_plain(self):
        self.assertEqual(_extract_json_array('[{"id":1,"description":"x"}]'),
                         [{"id": 1, "description": "x"}])

    def test_fenced(self):
        text = '分析如下：\n```json\n[{"id":1,"description":"x"}]\n```'
        self.assertEqual(len(_extract_json_array(text)), 1)

    def test_no_json(self):
        self.assertEqual(_extract_json_array("没有JSON"), [])
        self.assertEqual(_extract_json_array(""), [])


class PlanApiTests(TestCase):
    def setUp(self):
        from dvadmin.system.models import UserRole
        self.user = CustomUser.objects.create_user(username="plapi", password="t")
        self.user.role = UserRole.PRODUCER
        self.user.save()
        self.session = _mk_session(self.user)

    def test_plan_endpoint_empty(self):
        from rest_framework.test import APIClient
        c = APIClient(); c.force_authenticate(self.user)
        resp = c.get(f"/api/assistant/sessions/{self.session.pk}/plan/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json().get("data", resp.json())
        self.assertIsNone(data["plan"])

    def test_plan_endpoint_with_plan(self):
        Plan.objects.create(session=self.session, goal="改节奏", steps=[
            {"id": 1, "description": "a", "status": "COMPLETED"},
            {"id": 2, "description": "b", "status": "IN_PROGRESS"}])
        from rest_framework.test import APIClient
        c = APIClient(); c.force_authenticate(self.user)
        resp = c.get(f"/api/assistant/sessions/{self.session.pk}/plan/")
        data = resp.json().get("data", resp.json())
        self.assertEqual(data["plan"]["goal"], "改节奏")
        self.assertAlmostEqual(data["plan"]["progress"], 0.5)

    def test_plan_cancel(self):
        plan = Plan.objects.create(session=self.session, goal="g", steps=[
            {"id": 1, "description": "a", "status": "COMPLETED"},
            {"id": 2, "description": "b", "status": "PENDING"}])
        from rest_framework.test import APIClient
        c = APIClient(); c.force_authenticate(self.user)
        resp = c.post(f"/api/assistant/sessions/{self.session.pk}/plan/cancel/")
        self.assertEqual(resp.status_code, 200)
        plan.refresh_from_db()
        self.assertEqual(plan.status, PlanStatus.ABANDONED)

    def test_plan_cancel_none(self):
        from rest_framework.test import APIClient
        c = APIClient(); c.force_authenticate(self.user)
        resp = c.post(f"/api/assistant/sessions/{self.session.pk}/plan/cancel/")
        self.assertEqual(resp.status_code, 404)


class NormalizeCandidatesTests(TestCase):
    def test_normalizes_and_caps(self):
        raw = {"candidates": [
            {"id": "A", "description": "a", "rationale": "r", "score": 0.9},
            {"id": "B", "description": "b", "score": 1.5},   # 分数截断到 1.0
            {"id": "C", "description": "c", "score": -0.2},  # 分数截断到 0.0
        ]}
        out = _normalize_candidates(raw, 3)
        self.assertEqual(len(out), 3)
        self.assertEqual(out[1].score, 1.0)
        self.assertEqual(out[2].score, 0.0)

    def test_drops_missing_description(self):
        raw = {"candidates": [{"id": "A"}, {"id": "B", "description": "ok"}]}
        out = _normalize_candidates(raw, 3)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0].cid, "B")

    def test_caps_to_n(self):
        raw = {"candidates": [{"id": i, "description": "d"} for i in range(10)]}
        self.assertEqual(len(_normalize_candidates(raw, 3)), 3)

    def test_non_dict_returns_empty(self):
        self.assertEqual(_normalize_candidates(None, 3), [])
        self.assertEqual(_normalize_candidates({"candidates": "x"}, 3), [])


class HeuristicCandidatesTests(TestCase):
    def test_three_differentiated(self):
        out = _heuristic_candidates("改第3集节奏", 3)
        self.assertEqual(len(out), 3)
        self.assertEqual([c.cid for c in out], ["A", "B", "C"])
        self.assertEqual(len({c.description for c in out}), 3)  # 描述互不相同

    def test_caps_to_n(self):
        self.assertEqual(len(_heuristic_candidates("目标", 2)), 2)


class ToTExploreTests(TestCase):
    """tot_explore：生成候选→选最高分→返回全部。"""

    def setUp(self):
        self.session = _mk_session()

    def test_picks_highest_score(self):
        cands = [
            {"id": "A", "description": "a", "score": 0.85},
            {"id": "B", "description": "b", "score": 0.70},
            {"id": "C", "description": "c", "score": 0.60},
        ]
        result = tot_explore(self.session, "决策", generator=lambda d, c, n: _normalize_candidates({"candidates": cands}, n))
        self.assertEqual(result.best.cid, "A")
        self.assertEqual(len(result.candidates), 3)

    def test_llm_failure_falls_back_heuristic(self):
        def gen(d, c, n):
            raise RuntimeError("down")
        result = tot_explore(self.session, "决策", generator=gen)
        self.assertIsNotNone(result.best)  # 启发式兜底，不阻塞

    def test_empty_candidates_heuristic_fallback(self):
        result = tot_explore(self.session, "决策", generator=lambda d, c, n: [])
        self.assertIsNotNone(result.best)

    def test_result_dict_shape(self):
        cands = [{"id": "A", "description": "a", "score": 0.9}]
        result = tot_explore(self.session, "决策", complexity=0.8, generator=lambda d, c, n: _normalize_candidates({"candidates": cands}, n))
        d = result.to_dict()
        self.assertEqual(d["best_id"], "A")
        self.assertEqual(d["complexity"], 0.8)
        self.assertEqual(d["decision"], "决策")


@override_settings(ENABLE_PLANNING=True)
class MaybeTotTests(TestCase):
    """maybe_tot：复杂度>0.7 且 ENABLE_PLANNING_TOT 启用时触发，写会话树。"""

    def setUp(self):
        self.session = _mk_session()

    def test_flag_off_returns_none(self):
        with override_settings(ENABLE_PLANNING_TOT=False):
            self.assertIsNone(maybe_tot(self.session, "改第3集和第5集节奏并重写全部大纲", []))

    def test_below_threshold_returns_none(self):
        with override_settings(ENABLE_PLANNING_TOT=True):
            self.assertIsNone(maybe_tot(self.session, "改第3集", []))  # 低复杂度

    @override_settings(ENABLE_PLANNING_TOT=True)
    def test_above_threshold_explores_and_writes_tree(self):
        from apps.creation.assistant.session_tree import SessionStorage, SessionTreeManager
        text = "重写第3集和第5集大纲，同时优化节奏并调整人物弧光与全部反转"
        with patch("apps.creation.assistant.planning.llm_tot",
                   return_value=_normalize_candidates({"candidates": [
                       {"id": "A", "description": "a", "score": 0.9},
                       {"id": "B", "description": "b", "score": 0.6},
                   ]}, 3)) as mock_tot:
            result = maybe_tot(self.session, text, [])
        self.assertIsNotNone(result)
        self.assertEqual(result.best.cid, "A")
        mock_tot.assert_called_once()
        # 写入会话树 tot 条目
        tree = SessionTreeManager.get_or_create(self.session)
        self.assertTrue(any(e.type == "tot" for e in tree.entries))
        SessionStorage.save(tree)

    @override_settings(ENABLE_PLANNING_TOT=True)
    def test_exception_silent(self):
        with patch("apps.creation.assistant.planning.estimate_task_complexity",
                   side_effect=RuntimeError("boom")):
            self.assertIsNone(maybe_tot(self.session, "重写第3集大纲并优化节奏", []))


class RunnerTotIntegrationTests(AssistantFixtureMixin, TestCase):
    """runner 集成：复杂度>0.7 且开关启用时 bootstrap 发 tot_explored 事件。"""

    def setUp(self):
        super().setUp()
        from apps.creation.assistant.events import set_backend, MemoryStreams
        set_backend(MemoryStreams())
        from apps.creation.assistant import events as ev
        self.events = ev

    def _run_turn(self):
        from apps.creation.models import TaskRecord, TaskType
        from apps.creation.assistant.runner import _save, run_assistant_turn
        from apps.creation.tests.test_assistant import FakeChatGateway
        record = TaskRecord.objects.create(play=self.play,
                                           task_type=TaskType.ASSISTANT_TURN,
                                           input_summary={"session_id": self.session.pk})
        _save(self.session, "user", [{"type": "text", "text": "改第3集节奏"}], record)
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        with patch("apps.ai_gateway.gateway.AIGateway",
                   return_value=FakeChatGateway([_say("ok")])):
            run_assistant_turn(record.pk)

    @override_settings(ENABLE_PLANNING=True, ENABLE_PLANNING_TOT=True)
    def test_bootstrap_emits_tot_event(self):
        from apps.creation.assistant.planning import ToTCandidate, ToTResult
        with patch("apps.creation.assistant.planning.maybe_tot") as mock_tot:
            mock_tot.return_value = ToTResult(
                decision="d", complexity=0.8, best=ToTCandidate("A", "a", score=0.9),
                candidates=[ToTCandidate("A", "a", score=0.9)])
            self._run_turn()
        types = [e["type"] for e in self.events.read(self.session.pk)[0]]
        self.assertIn("tot_explored", types)


# ── Phase 0 max 审查缺陷修复（C 组）────────────────────────────────────────

class PlanningFixTests(TestCase):
    """修订不回退进行中步骤 / 规整健壮性 / 活跃计划停滞兜底。"""

    def setUp(self):
        self.session = _mk_session()
        self.engine = PlanningEngine(self.session, planner=lambda g, c, t: [])

    def _plan(self, statuses):
        return Plan.objects.create(session=self.session, goal="g", status=PlanStatus.ACTIVE,
                                   steps=[{"id": i + 1, "description": f"s{i+1}",
                                           "depends_on": [], "status": st}
                                          for i, st in enumerate(statuses)])

    def test_revise_keeps_in_progress(self):
        """3.1 修订保留 IN_PROGRESS（不再回退 PENDING）。"""
        steps = _revise_steps([
            {"id": 1, "description": "a", "status": "IN_PROGRESS"},
            {"id": 2, "description": "b", "status": "PENDING"},
        ])
        self.assertEqual(steps[0]["status"], "IN_PROGRESS")

    def test_revise_resets_failed(self):
        """3.1 FAILED 仍重置 PENDING（保留原语义）。"""
        steps = _revise_steps([{"id": 1, "description": "a", "status": "FAILED"}])
        self.assertEqual(steps[0]["status"], "PENDING")
        self.assertIn("重试", steps[0].get("note", ""))

    def test_half_done_revision_keeps_in_progress(self):
        """3.1+3.2 完成过半触发修订时，IN_PROGRESS 步骤不被重置。"""
        plan = self._plan(["COMPLETED", "COMPLETED", "IN_PROGRESS", "PENDING"])
        plan = self.engine.update_plan(plan, reason="执行中动态修订")
        self.assertEqual(plan.steps[2]["status"], "IN_PROGRESS",
                         "修订不得把刚推进的步骤打回 PENDING")
        # 修订后无状态变化 → 不创建 PlanRevision（节流）
        self.assertEqual(plan.revisions.count(), 0)

    def test_revision_throttled_when_no_change(self):
        """3.2 完成过半后连续多次修订触发不再堆积 PlanRevision。"""
        plan = self._plan(["COMPLETED", "COMPLETED", "PENDING", "PENDING"])
        before = PlanRevision.objects.count()
        for _ in range(10):
            if PlanningEngine.should_update_plan(plan):
                self.engine.update_plan(plan, reason="执行中动态修订")
        self.assertEqual(PlanRevision.objects.count(), before,
                         "修订无状态变化时不得重复创建 PlanRevision")

    def test_plan_can_reach_completed(self):
        """3.1+3.2 端到端：交替读写工具后计划可推进到 COMPLETED。"""
        # 2/4 完成后（完成过半），后续每个工具调用都触发修订检查
        plan = self._plan(["COMPLETED", "COMPLETED", "PENDING", "PENDING"])
        for _ in range(2):
            # 模拟：只读工具 → IN_PROGRESS；写工具 → COMPLETED；每步都过修订检查
            s = next(s for s in plan.steps if s["status"] == "PENDING")
            if PlanningEngine.should_update_plan(plan):
                self.engine.update_plan(plan, reason="执行中动态修订")
            PlanningEngine.mark_step(plan, s["id"], "IN_PROGRESS")
            # 修订检查不得把它打回去
            if PlanningEngine.should_update_plan(plan):
                self.engine.update_plan(plan, reason="执行中动态修订")
            self.assertEqual(
                next(x for x in plan.steps if x["id"] == s["id"])["status"],
                "IN_PROGRESS", "修订后步骤必须仍为 IN_PROGRESS")
            PlanningEngine.mark_step(plan, s["id"], "COMPLETED")
        statuses = [s["status"] for s in plan.steps]
        self.assertEqual(statuses, ["COMPLETED"] * 4)
        PlanningEngine.finish_plan(plan, PlanStatus.COMPLETED)

    # ── 3.3 规整健壮性 ──

    def test_normalize_depends_on_single_value(self):
        """3.3 depends_on 为单个数字不抛 TypeError。"""
        from apps.creation.assistant.planning import _normalize_steps
        norm = _normalize_steps([{"id": 1, "description": "a", "depends_on": 2},
                                 {"id": 2, "description": "b"}])
        self.assertEqual(norm[0]["depends_on"], [2])

    def test_normalize_depends_on_dangling_dropped(self):
        """phase1 4.4：指向不存在 id 的依赖被丢弃。"""
        from apps.creation.assistant.planning import _normalize_steps
        norm = _normalize_steps([{"id": 1, "description": "a", "depends_on": [2, 9]}])
        self.assertEqual(norm[0]["depends_on"], [])

    def test_normalize_string_id(self):
        """3.3 id 为字符串不抛 ValueError。"""
        from apps.creation.assistant.planning import _normalize_steps
        norm = _normalize_steps([{"id": "step-1", "description": "a"}])
        self.assertEqual(len(norm), 1)
        self.assertIsInstance(norm[0]["id"], int)

    def test_normalize_duplicate_ids(self):
        """3.3 重复 id 去重，mark_step 不再串步（phase1 4.4：最小未占用正整数）。"""
        from apps.creation.assistant.planning import _normalize_steps
        norm = _normalize_steps([
            {"id": 1, "description": "read"},
            {"id": 1, "description": "propose"},
        ])
        self.assertEqual([s["id"] for s in norm], [1, 2])

    def test_normalize_duplicate_ids_min_unused(self):
        """phase1 4.4：ids [1,1,101,102] 时重复者得到既非 101 也非 102 的 id，
        102 的依赖仍指向原 101。"""
        from apps.creation.assistant.planning import _normalize_steps
        norm = _normalize_steps([
            {"id": 1, "description": "a"},
            {"id": 1, "description": "a2"},
            {"id": 101, "description": "b"},
            {"id": 102, "description": "c", "depends_on": [101]},
        ])
        ids = [s["id"] for s in norm]
        self.assertEqual(len(set(ids)), 4, "全部 id 唯一")
        self.assertEqual(ids[0], 1)
        self.assertNotIn(101, (ids[0], ids[1]))
        self.assertNotIn(102, (ids[0], ids[1]))
        self.assertEqual(ids[2], 101)
        self.assertEqual(ids[3], 102)
        self.assertEqual(norm[3]["depends_on"], [101], "依赖仍指向原 101 步骤")
        all_ids = set(ids)
        self.assertTrue(all(d in all_ids for s in norm for d in s["depends_on"]),
                        "规整后所有依赖指向存在的 id")

    def test_duplicate_id_no_status_regression(self):
        """3.3 重复 id 脏数据下推进不使终态步骤倒退（状态来回抖动）。"""
        plan = Plan.objects.create(session=self.session, goal="g", status=PlanStatus.ACTIVE,
                                   steps=[
                                       {"id": 1, "description": "read", "status": "COMPLETED"},
                                       {"id": 1, "description": "propose", "status": "PENDING"},
                                   ])
        # 只读工具再次命中 id=1（首个匹配）：不得把已 COMPLETED 的步骤打回 IN_PROGRESS
        changed = PlanningEngine.mark_step(plan, 1, "IN_PROGRESS")
        self.assertFalse(changed, "终态步骤不得倒退")
        plan.refresh_from_db(fields=["steps"])
        self.assertEqual([s["status"] for s in plan.steps], ["COMPLETED", "PENDING"])

    def test_llm_malformed_steps_do_not_crash_create(self):
        """3.3 create_plan 对畸形 LLM 输出不崩、降级启发式。"""
        engine = PlanningEngine(self.session, planner=lambda g, c, t: [
            {"id": 1, "description": "ok", "depends_on": [2]},
            {"description": "无id", "depends_on": None},
        ])
        plan = engine.create_plan("改第3集", tools=[])
        self.assertTrue(plan.steps, "畸形输入不得使计划创建失败")

    # ── 3.4 活跃计划停滞兜底 ──

    @override_settings(ENABLE_PLANNING=True)
    def test_stalled_active_plan_abandoned_and_new_plan_created(self):
        """3.4 长期无进度的活跃计划被放弃，新目标能创建新计划 + plan_abandoned 事件。"""
        from apps.creation.assistant import events
        events.set_backend(events.MemoryStreams())
        self.addCleanup(events.set_backend, None)
        from datetime import timedelta
        from django.utils import timezone
        plan = self._plan(["PENDING"] * 4)
        # 模拟长期无进度：updated_at 早于阈值
        Plan.objects.filter(pk=plan.pk).update(
            updated_at=timezone.now() - timedelta(minutes=999))
        # 高复杂度文本
        text = "重写全部大纲与前三集剧本，重排角色关系，新增两条剧情线并出提案"
        new_plan = maybe_plan(self.session, text, tools=[])
        self.assertIsNotNone(new_plan, "停滞计划应被放弃且允许新建")
        plan.refresh_from_db(fields=["status"])
        self.assertEqual(plan.status, PlanStatus.ABANDONED)
        types = [e["type"] for e in events.read(self.session.pk)[0]]
        self.assertIn("plan_abandoned", types)

    @override_settings(ENABLE_PLANNING=True)
    def test_fresh_active_plan_blocks_new_plan(self):
        """3.4 正常（未停滞）活跃计划仍抑制新建。"""
        plan = self._plan(["PENDING"] * 4)
        text = "重写全部大纲与前三集剧本，重排角色关系，新增两条剧情线并出提案"
        self.assertIsNone(maybe_plan(self.session, text, tools=[]))
        plan.refresh_from_db(fields=["status"])
        self.assertEqual(plan.status, PlanStatus.ACTIVE)


# ── Phase 0 max 审查缺陷修复（E 组：开关与阈值接线）──────────────────────────

class ThresholdWiringTests(TestCase):
    """5.2 阈值读配置而非模块字面量。"""

    def test_thresholds_follow_settings(self):
        from apps.creation.assistant.planning import (
            complexity_plan_threshold, complexity_tot_threshold, reflect_fail_threshold,
            tot_num_candidates,
        )
        with override_settings(AGENT_REFLECTION_FAILURE_THRESHOLD=5,
                               AGENT_PLANNING_COMPLEXITY_THRESHOLD=0.9,
                               AGENT_EXPLORATION_THRESHOLD=0.95,
                               TOT_NUM_CANDIDATES=7):
            self.assertEqual(reflect_fail_threshold(), 5)
            self.assertEqual(complexity_plan_threshold(), 0.9)
            self.assertEqual(complexity_tot_threshold(), 0.95)
            self.assertEqual(tot_num_candidates(), 7)

    def test_tot_candidate_cap_uses_config(self):
        """5.2 ToT 候选数受 TOT_NUM_CANDIDATES 约束（配置低于硬上限时生效）。"""
        session = _mk_session()
        seen = {}

        def gen(decision, context, n):
            seen["n"] = n
            return _normalize_candidates({"candidates": [
                {"id": "A", "description": "a", "score": 0.9},
                {"id": "B", "description": "b", "score": 0.5},
            ]}, n)

        with override_settings(TOT_NUM_CANDIDATES=1):
            result = tot_explore(session, "决策", generator=gen)
        self.assertEqual(seen["n"], 1)
        self.assertEqual(len(result.candidates), 1)

    def test_tot_candidate_cap_never_exceeds_hard_limit(self):
        """配置高于硬上限时仍受 TOT_NUM_CANDIDATES 模块常量封顶（成本有界）。"""
        from apps.creation.assistant.planning import TOT_NUM_CANDIDATES as HARD_CAP
        session = _mk_session()
        seen = {}

        def gen(decision, context, n):
            seen["n"] = n
            return []

        with override_settings(TOT_NUM_CANDIDATES=99):
            tot_explore(session, "决策", generator=gen)
        self.assertEqual(seen["n"], HARD_CAP)


class ReflectionFlagTests(AssistantFixtureMixin, TestCase):
    """5.2 反思由 ENABLE_REFLEXION 独立控制（不再误用 ENABLE_PLANNING）。"""

    def setUp(self):
        super().setUp()
        from apps.creation.assistant import events as ev
        ev.set_backend(ev.MemoryStreams())
        self.addCleanup(ev.set_backend, None)
        self.events = ev

    def _run_two_failures(self):
        from apps.creation.assistant.runner import _save, run_assistant_turn
        from apps.creation.models import TaskRecord, TaskType
        record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                           input_summary={"session_id": self.session.pk})
        _save(self.session, "user", [{"type": "text", "text": "看看第3集"}], record)
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        gw = FakeChatGateway([
            _use("t1", "get_scene", {"scene_id": 999999}),
            _use("t2", "get_scene", {"scene_id": 999999}),
            _say("放弃了"),
        ])
        with patch("apps.ai_gateway.gateway.AIGateway", return_value=gw):
            run_assistant_turn(record.pk)
        record.refresh_from_db()
        return record, [e["type"] for e in self.events.read(self.session.pk)[0]]

    @override_settings(ENABLE_PLANNING=True, ENABLE_REFLEXION=False)
    def test_no_reflection_when_reflexion_off(self):
        """规划开着、反思关着 → 连续失败达阈值也不产生反思与付费调用。"""
        with patch("apps.creation.assistant.planning.llm_reflect") as mock_reflect:
            record, types = self._run_two_failures()
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        self.assertNotIn("reflection", types)
        mock_reflect.assert_not_called()

    @override_settings(ENABLE_PLANNING=False, ENABLE_REFLEXION=True)
    def test_reflection_when_reflexion_on_alone(self):
        """反思开关单独开启即可生效（不依赖规划开关）。"""
        with patch("apps.creation.assistant.planning.llm_reflect",
                   return_value={"root_cause": "对象不存在"}) as mock_reflect:
            record, types = self._run_two_failures()
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        self.assertIn("reflection", types)
        mock_reflect.assert_called_once()

    @override_settings(ENABLE_PLANNING=True, ENABLE_REFLEXION=True,
                       AGENT_REFLECTION_FAILURE_THRESHOLD=99)
    def test_threshold_from_settings_suppresses_reflection(self):
        """阈值调高到 99 后两次失败不触发（阈值确实参与判定）。"""
        with patch("apps.creation.assistant.planning.llm_reflect") as mock_reflect:
            _, types = self._run_two_failures()
        self.assertNotIn("reflection", types)
        mock_reflect.assert_not_called()


# ── phase1 max 审查修复（4.1–4.4）────────────────────────────────────────

class Phase1PlanLifecycleTests(TestCase):
    """规划生命周期修复：写优先可达终态 / 停滞时钟 / 阈值禁用。"""

    def setUp(self):
        self.session = _mk_session()
        self.engine = PlanningEngine(self.session, planner=lambda g, c, t: [])

    @override_settings(ENABLE_PLANNING=True)
    def test_write_first_plan_reaches_completion(self):
        """4.1：首步为改写类工作的计划，连续 propose_* 成功后进度非 0。"""
        from apps.creation.assistant.planning import advance_plan_for_tool
        plan = Plan.objects.create(session=self.session, goal="改写第一集",
                                   status=PlanStatus.ACTIVE,
                                   steps=[{"id": 1, "description": "提出剧本修改提案",
                                           "depends_on": [], "status": "PENDING"},
                                          {"id": 2, "description": "确认提案生效",
                                           "depends_on": [1], "status": "PENDING"}])
        for _ in range(3):
            advance_plan_for_tool(self.session, "propose_episode_script_edit", False)
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]["status"], "COMPLETED", "写优先计划首步应可达终态")
        self.assertGreater(plan.get_progress(), 0)

    @override_settings(ENABLE_PLANNING=True)
    def test_write_tool_completes_first_runnable_with_note(self):
        """4.1：无 IN_PROGRESS 时写工具直接完成首个可执行步骤并带 note。"""
        from apps.creation.assistant.planning import advance_plan_for_tool
        plan = Plan.objects.create(session=self.session, goal="g", status=PlanStatus.ACTIVE,
                                   steps=[{"id": 1, "description": "a", "depends_on": [],
                                           "status": "PENDING"}])
        advance_plan_for_tool(self.session, "request_generation", False)
        plan.refresh_from_db()
        self.assertEqual(plan.steps[0]["status"], "COMPLETED")
        self.assertIn("request_generation", plan.steps[0].get("note", ""))

    @override_settings(ENABLE_PLANNING=True)
    def test_note_only_change_does_not_refresh_stall_clock(self):
        """4.2：长时间无状态变化、其间多次失败工具写 note，仍判停滞。"""
        from datetime import timedelta
        from django.utils import timezone
        from apps.creation.assistant.planning import _plan_stalled, advance_plan_for_tool
        plan = Plan.objects.create(session=self.session, goal="g", status=PlanStatus.ACTIVE,
                                   steps=[{"id": 1, "description": "a", "depends_on": [],
                                           "status": "IN_PROGRESS"}])
        # 把 updated_at 拉到很久以前（超过阈值）
        old = timezone.now() - timedelta(minutes=100)
        Plan.objects.filter(pk=plan.pk).update(updated_at=old)
        plan.refresh_from_db()
        # 多次失败工具写 note（不改变状态）
        for _ in range(5):
            advance_plan_for_tool(self.session, "get_scene", True)
        plan.refresh_from_db()
        self.assertIn("失败", plan.steps[0].get("note", ""), "note 已写入")
        self.assertTrue(_plan_stalled(plan),
                        "note-only 写入不应刷新停滞时钟，计划应判停滞")

    @override_settings(AGENT_PLAN_STALL_TURNS=0)
    def test_stall_turns_zero_disables(self):
        """4.3：AGENT_PLAN_STALL_TURNS=0 时长时间无进度的计划不判停滞。"""
        from datetime import timedelta
        from django.utils import timezone
        from apps.creation.assistant.planning import _plan_stalled
        plan = self._plan(["IN_PROGRESS"])
        Plan.objects.filter(pk=plan.pk).update(
            updated_at=timezone.now() - timedelta(days=7))
        plan.refresh_from_db()
        self.assertFalse(_plan_stalled(plan), "配置 0 应禁用停滞判定")

    def _plan(self, statuses):
        return Plan.objects.create(session=self.session, goal="g", status=PlanStatus.ACTIVE,
                                   steps=[{"id": i + 1, "description": f"s{i+1}",
                                           "depends_on": [], "status": st}
                                          for i, st in enumerate(statuses)])
