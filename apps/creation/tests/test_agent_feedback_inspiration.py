# -*- coding: utf-8 -*-
"""决策解释/反馈收集/灵感库/协作审查测试（2026-11-21-backend-agent-feedback-inspiration）。

覆盖：择优决策解释（结构/兜底/非竞争 None）、反馈端点（upsert/非属主 404/404/注入段）、
灵感服务与工具（加权排序/推荐计数/开关零暴露）、REST 权限矩阵、审查反馈
（提交/权重注入/趋势/报告附带）、导出不含反馈。
"""
import json
from types import SimpleNamespace

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from apps.creation.assistant import inspiration as I
from apps.creation.assistant import multi_agent as MA
from apps.creation.assistant import runner as RN
from apps.creation.assistant import tools as T
from apps.creation.assistant.models import (AgentFeedback, AssistantSession, Inspiration,
                                            InspirationCategory, ReviewFeedback)
from apps.creation.models import (Episode, Play, PlayMember, TaskRecord, TaskStatus,
                                  TaskType)


def _user(name, admin=False):
    User = get_user_model()
    return User.objects.create_user(username=name, password="pw-12345",
                                    is_platform_admin=admin)


def _play(owner, title="测试剧"):
    """建剧 + 属主 ACTIVE 成员行（get_accessible_play_ids 只认成员表）。"""
    play = Play.objects.create(title=title, created_by=owner)
    PlayMember.objects.create(play=play, user=owner, role="owner", status="active")
    return play


def _collab_record(play, session_id=1):
    return TaskRecord.objects.create(
        play=play, task_type=TaskType.ASSISTANT_TURN, status=TaskStatus.SUCCESS,
        input_summary={"session_id": session_id, "mode": "collaborate"})


def _session(user, play):
    return AssistantSession.objects.create(user=user, play=play, agent_config=None)


# ── 决策解释（tasks 2.1/2.2）────────────────────────────────────────────────

class DecisionExplanationTests(TestCase):
    def _cand(self, index, style, score, output="x"):
        return {"index": index, "style": style, "output": output,
                "score": score, "feedback": None}

    def test_explanation_with_scores(self):
        cands = [self._cand(1, "快节奏", 78), self._cand(2, "慢热", 85),
                 self._cand(3, "喜剧", 70)]
        d = MA.CoordinatorAgent._decision_explanation(cands[1], cands)
        self.assertEqual((d["selected"], d["style"], d["score"]), (2, "慢热", 85))
        self.assertEqual(len(d["comparison"]), 3)
        self.assertEqual(d["comparison"][0], {"index": 1, "style": "快节奏", "score": 78})
        self.assertIn("总分最高", d["reason"])
        self.assertIn("85", d["reason"])
        self.assertIn("快节奏", d["reason"])

    def test_explanation_no_scores_fallback(self):
        cands = [self._cand(1, "快节奏", None), self._cand(2, "慢热", None)]
        d = MA.CoordinatorAgent._decision_explanation(cands[0], cands)
        self.assertIn("审稿未产出有效分数", d["reason"])
        self.assertIn("快节奏", d["reason"])

    def test_director_task_contains_explain_instruction(self):
        task = MA.CoordinatorAgent._director_task(
            "目标", "草稿", {"overall": 80, "suggestions": []},
            decision={"reason": "风格「慢热」总分最高（85 分）"})
        self.assertIn("说明方案选择理由", task)
        task_plain = MA.CoordinatorAgent._director_task("目标", "草稿", None, decision=None)
        self.assertNotIn("说明方案选择理由", task_plain)


# ── 反馈端点与注入（tasks 3.1/3.2/3.3）──────────────────────────────────────

class AgentFeedbackTests(APITestCase):
    def setUp(self):
        self.alice = _user("fb_alice")
        self.bob = _user("fb_bob")
        self.play = _play(self.alice)
        self.session = _session(self.alice, self.play)
        self.record = _collab_record(self.play, self.session.pk)

    def _post(self, user, body, session=None):
        self.client.force_authenticate(user)
        return self.client.post(
            f"/api/assistant/sessions/{(session or self.session).pk}/feedback/",
            body, format="json")

    def test_submit_and_upsert(self):
        resp = self._post(self.alice, {"rating": 2, "comment": "节奏太慢"})
        self.assertEqual(resp.status_code, 200, resp.content)
        resp = self._post(self.alice, {"rating": 4, "comment": "改后不错"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AgentFeedback.objects.count(), 1)
        fb = AgentFeedback.objects.get()
        self.assertEqual((fb.rating, fb.comment), (4, "改后不错"))

    def test_non_owner_404(self):
        # 会话按属主隔离：非属主与既有会话端点一致返回 404（spec scenario 同步）
        resp = self._post(self.bob, {"rating": 3})
        self.assertEqual(resp.status_code, 404)
        self.assertEqual(AgentFeedback.objects.count(), 0)

    def test_no_collab_turn_404(self):
        session2 = _session(self.alice, self.play)
        resp = self._post(self.alice, {"rating": 3}, session=session2)
        self.assertEqual(resp.status_code, 404)

    def test_explicit_turn(self):
        resp = self._post(self.alice, {"turn": self.record.pk, "rating": 1})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(AgentFeedback.objects.get().turn_id, self.record.pk)

    def test_prompt_injection_and_empty(self):
        AgentFeedback.objects.create(session=self.session, user=self.alice,
                                     turn=self.record, rating=1, comment="别用慢热")
        prompt = RN.system_prompt(self.session)
        self.assertIn("用户对近期协作的反馈", prompt)
        self.assertIn("别用慢热", prompt)
        # 无反馈会话：空段（prompt 无反馈字样）
        session2 = _session(self.alice, self.play)
        self.assertNotIn("用户对近期协作的反馈", RN.system_prompt(session2))

    def test_feedback_not_in_export(self):
        from apps.creation.workspace import manifest as M
        AgentFeedback.objects.create(session=self.session, user=self.alice,
                                     turn=self.record, rating=1, comment="x")
        dumped = json.dumps(M.build_manifest(self.play), default=str)
        self.assertNotIn("agent_feedback", dumped)
        self.assertNotIn("AgentFeedback", dumped)


# ── 灵感服务与工具（tasks 4.1/4.2/4.4）──────────────────────────────────────

class InspirationServiceTests(TestCase):
    def setUp(self):
        self.alice = _user("ins_alice")
        self.play = _play(self.alice)

    def test_save_and_search_weighted(self):
        I.save_inspiration(play=self.play, user=self.alice,
                           category=InspirationCategory.DIALOGUE,
                           content="反派临死前才说出真相", rating=3)
        I.save_inspiration(play=self.play, user=self.alice,
                           category=InspirationCategory.DIALOGUE,
                           content="真相在葬礼上被揭开", rating=5)
        rows = I.search_inspirations(user=self.alice, play=self.play,
                                     keywords="真相")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0].rating, 5)  # 同命中数下高分在前

    def test_search_global_merged(self):
        I.save_inspiration(play=None, user=self.alice,
                           category=InspirationCategory.PLOT_TWIST,
                           content="全局灵感：双胞胎互换身份")
        rows = I.search_inspirations(user=self.alice, play=self.play,
                                     keywords="双胞胎")
        self.assertEqual(len(rows), 1)
        self.assertIsNone(rows[0].play)

    def test_recommend_marks_used(self):
        I.save_inspiration(play=self.play, user=self.alice,
                           category=InspirationCategory.SCENE,
                           content="雨夜天台对峙场景")
        # 2-4 字贪婪分块：本场/天台对峙 等块，"天台对峙" 命中内容
        rows = I.recommend_inspirations(play=self.play, text="这场雨夜天台对峙的戏",
                                        mark_used=True)
        self.assertEqual(len(rows), 1)
        self.assertEqual(Inspiration.objects.get().used_count, 1)
        # 无命中不推荐
        rows = I.recommend_inspirations(play=self.play, text="完全无关的内容")
        self.assertEqual(rows, [])

    def test_save_validation(self):
        with self.assertRaises(I.InspirationError):
            I.save_inspiration(play=self.play, user=self.alice,
                               category="bad_cat", content="x")
        with self.assertRaises(I.InspirationError):
            I.save_inspiration(play=self.play, user=self.alice,
                               category=InspirationCategory.OTHER, content="")
        with self.assertRaises(I.InspirationError):
            I.save_inspiration(play=self.play, user=self.alice,
                               category=InspirationCategory.OTHER, content="x",
                               rating=9)


@override_settings(ENABLE_INSPIRATION_LIBRARY=True)
class InspirationToolTests(TestCase):
    def setUp(self):
        self.alice = _user("it_alice")
        self.play = _play(self.alice)
        self.session = _session(self.alice, self.play)
        self.ctx = SimpleNamespace(play=self.play, session=self.session,
                                   user=self.alice)

    def test_tool_specs_exposure(self):
        names = [s["name"] for s in T.tool_specs()]
        for n in ("inspiration_save", "inspiration_search", "inspiration_recommend"):
            self.assertIn(n, names)

    def test_off_zero_exposure(self):
        with override_settings(ENABLE_INSPIRATION_LIBRARY=False):
            names = [s["name"] for s in T.tool_specs()]
            for n in ("inspiration_save", "inspiration_search", "inspiration_recommend"):
                self.assertNotIn(n, names)

    def test_save_requires_play(self):
        ctx = SimpleNamespace(play=None, session=self.session, user=self.alice)
        result, err = T.run_tool(ctx, "inspiration_save",
                                {"category": "dialogue", "content": "x"})
        self.assertTrue(err)
        self.assertIn("本剧会话", result["error"])

    def test_save_search_roundtrip(self):
        result, err = T.run_tool(self.ctx, "inspiration_save",
                                 {"category": "plot_twist", "content": "主角其实是反派",
                                  "rating": 5})
        self.assertFalse(err, result)
        result, err = T.run_tool(self.ctx, "inspiration_search",
                                {"keywords": "反派"})
        self.assertFalse(err)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["content"], "主角其实是反派")
        self.assertEqual(Inspiration.objects.get().source, "agent")

    def test_recommend_default_text_from_last_user_message(self):
        from apps.creation.assistant.models import AssistantMessage, MessageRole
        T.run_tool(self.ctx, "inspiration_save",
                   {"category": "scene", "content": "雨夜天台对峙场景"})
        AssistantMessage.objects.create(
            session=self.session, seq=1, role=MessageRole.USER,
            content=[{"type": "text", "text": "这场雨夜天台对峙的戏怎么写"}])
        result, err = T.run_tool(self.ctx, "inspiration_recommend", {})
        self.assertFalse(err, result)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["recommendations"][0]["content"], "雨夜天台对峙场景")


# ── 灵感 REST（tasks 4.3）────────────────────────────────────────────────────

class InspirationAPITests(APITestCase):
    def setUp(self):
        self.alice = _user("ia_alice")
        self.bob = _user("ia_bob")
        self.padmin = _user("ia_padmin", admin=True)
        self.play = _play(self.alice)
        PlayMember.objects.create(play=self.play, user=self.bob, role="viewer",
                                  status="active")

    def _list(self, user, query=""):
        self.client.force_authenticate(user)
        body = self.client.get(f"/api/inspirations/{query}").json()
        # 渲染器信封：分页 {code,msg,page,limit,total,data:[...]}；非分页 {code,msg,data}
        return body["data"]

    def test_create_play_scoped(self):
        self.client.force_authenticate(self.alice)
        resp = self.client.post("/api/inspirations/",
                                {"play": self.play.pk, "category": "scene",
                                 "content": "废弃医院场景"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(Inspiration.objects.get().source, "manual")

    def test_viewer_cannot_create(self):
        self.client.force_authenticate(self.bob)
        resp = self.client.post("/api/inspirations/",
                                {"play": self.play.pk, "category": "scene",
                                 "content": "x"}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_global_requires_admin(self):
        self.client.force_authenticate(self.alice)
        resp = self.client.post("/api/inspirations/",
                                {"category": "other", "content": "x"}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.client.force_authenticate(self.padmin)
        resp = self.client.post("/api/inspirations/",
                                {"category": "other", "content": "全局"}, format="json")
        self.assertEqual(resp.status_code, 201)

    def test_member_reads_play_scoped(self):
        Inspiration.objects.create(play=self.play, user=self.alice,
                                   category="other", content="本剧灵感")
        rows = self._list(self.bob, f"?play={self.play.pk}")
        self.assertIn("本剧灵感", [r["content"] for r in rows])

    def test_non_member_cannot_read_play_scoped(self):
        outsider = _user("ia_out")
        Inspiration.objects.create(play=self.play, user=self.alice,
                                   category="other", content="本剧灵感")
        rows = self._list(outsider, f"?play={self.play.pk}")
        self.assertEqual(rows, [])

    def test_global_readable_by_all(self):
        Inspiration.objects.create(play=None, user=self.padmin,
                                   category="other", content="全局灵感")
        outsider = _user("ia_out2")
        rows = self._list(outsider)
        self.assertIn("全局灵感", [r["content"] for r in rows])

    def test_owner_can_delete_own(self):
        rec = Inspiration.objects.create(play=self.play, user=self.alice,
                                         category="other", content="x")
        self.client.force_authenticate(self.alice)
        resp = self.client.delete(f"/api/inspirations/{rec.pk}/")
        self.assertEqual(resp.status_code, 204, resp.content)
        self.assertFalse(Inspiration.objects.filter(pk=rec.pk).exists())

    def test_viewer_cannot_delete_others(self):
        rec = Inspiration.objects.create(play=self.play, user=self.alice,
                                         category="other", content="x")
        self.client.force_authenticate(self.bob)
        resp = self.client.delete(f"/api/inspirations/{rec.pk}/")
        self.assertEqual(resp.status_code, 403)


# ── 审查反馈（tasks 5.1/5.2/5.3）────────────────────────────────────────────

@override_settings(ENABLE_AGENT_REVIEW=True)
class ReviewFeedbackTests(APITestCase):
    def setUp(self):
        self.alice = _user("rf_alice")
        self.bob = _user("rf_bob")
        self.play = _play(self.alice)
        PlayMember.objects.create(play=self.play, user=self.bob, role="viewer",
                                  status="active")
        self.episode = Episode.objects.create(play=self.play, episode_number=1)

    def _post(self, user, body):
        self.client.force_authenticate(user)
        return self.client.post("/api/assistant/review/feedback/", body,
                                format="json")

    def test_submit_and_upsert(self):
        body = {"episode": self.episode.pk, "rating": 2,
                "issues_feedback": [
                    {"issue_key": "dialogue-0", "agree": False, "suggestion": "这条对白没问题"}]}
        resp = self._post(self.alice, body)
        self.assertEqual(resp.status_code, 201, resp.content)
        resp = self._post(self.alice, {**body, "rating": 4})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(ReviewFeedback.objects.count(), 1)
        self.assertEqual(ReviewFeedback.objects.get().rating, 4)

    def test_non_member_403(self):
        outsider = _user("rf_out")
        resp = self._post(outsider, {"episode": self.episode.pk, "rating": 3})
        self.assertEqual(resp.status_code, 403)

    def test_review_off_404(self):
        with override_settings(ENABLE_AGENT_REVIEW=False):
            resp = self._post(self.alice, {"episode": self.episode.pk, "rating": 3})
            self.assertEqual(resp.status_code, 404)

    def test_weight_hint_injection(self):
        from apps.creation.assistant import agent_review as AR
        ReviewFeedback.objects.create(
            episode=self.episode, user=self.alice, rating=2,
            issues_feedback=[{"issue_key": "dialogue-0", "agree": False},
                              {"issue_key": "dialogue-1", "agree": False},
                              {"issue_key": "plot_logic-0", "agree": True}])
        hint = AR.feedback_weight_hint(self.play)
        self.assertIn("dialogue", hint)
        self.assertIn("仅在明确时报告", hint)
        self.assertNotIn("plot_logic", hint)  # agree 多的维度不注入
        # 无反馈空串
        play2 = _play(self.alice, "另一剧")
        self.assertEqual(AR.feedback_weight_hint(play2), "")

    def test_trends_endpoint(self):
        ReviewFeedback.objects.create(
            episode=self.episode, user=self.alice, rating=2,
            issues_feedback=[{"issue_key": "dialogue-0", "agree": False}])
        self.client.force_authenticate(self.bob)
        resp = self.client.get(f"/api/assistant/review/feedback/trends/?play_id={self.play.pk}")
        self.assertEqual(resp.status_code, 200, resp.content)
        data = resp.json()["data"]
        self.assertEqual(data["dimensions"]["dialogue"]["disagree"], 1)
        self.assertEqual(len(data["recent"]), 1)
        # 非成员 404（_play_or_404 成员过滤）
        outsider = _user("rf_out2")
        self.client.force_authenticate(outsider)
        resp = self.client.get(f"/api/assistant/review/feedback/trends/?play_id={self.play.pk}")
        self.assertEqual(resp.status_code, 404)

    def test_report_contains_trends(self):
        from apps.creation.assistant import agent_review as AR
        ReviewFeedback.objects.create(
            episode=self.episode, user=self.alice, rating=2,
            issues_feedback=[{"issue_key": "dialogue-0", "agree": False}])
        session = _session(self.alice, self.play)
        report = AR.review_report(session, self.episode,
                                  reviewer=lambda **kw: {"overall_score": 80})
        self.assertIn("feedback_trends", report)
        self.assertEqual(report["feedback_trends"]["dimensions"]["dialogue"]["disagree"], 1)


# ── 配置接线（tasks 4.4：默认关 + env 模板含键）──────────────────────────────

class InspirationFlagWiringTests(TestCase):
    """开关默认关闭 + env 模板含键（与 FeatureFlagWiringTests 同一模式）。"""

    def test_settings_defaults(self):
        from django.conf import settings
        self.assertIs(settings.ENABLE_INSPIRATION_LIBRARY, False)
        self.assertEqual(settings.INSPIRATION_SEARCH_LIMIT, 20)
        self.assertEqual(settings.INSPIRATION_RECOMMEND_LIMIT, 5)
        self.assertEqual(settings.FEEDBACK_PROMPT_RECENT, 3)
        self.assertEqual(settings.REVIEW_FEEDBACK_WINDOW_DAYS, 30)

    def test_env_template_has_keys(self):
        from pathlib import Path
        import conf
        template = Path(conf.__file__).parent / "env.example.py"
        self.assertTrue(template.is_file())
        text = template.read_text(encoding="utf-8")
        for key in ("ENABLE_INSPIRATION_LIBRARY", "INSPIRATION_SEARCH_LIMIT",
                    "INSPIRATION_RECOMMEND_LIMIT", "FEEDBACK_PROMPT_RECENT",
                    "REVIEW_FEEDBACK_WINDOW_DAYS"):
            self.assertIn(key, text)


# ── runner E2E：FakeChatGateway 轮内调用灵感工具（tasks 4.2 验收）────────────

@override_settings(ENABLE_INSPIRATION_LIBRARY=True)
class InspirationRunnerE2ETests(TestCase):
    """runner 端到端：模型 tool_use 调 inspiration_save → 落库 source=agent。"""

    def test_turn_saves_inspiration(self):
        import apps.creation.assistant.events as events
        from unittest.mock import patch
        from apps.creation.assistant import runner as runner_mod
        from apps.creation.assistant.models import MessageRole
        from apps.creation.tests.test_assistant import FakeChatGateway, _say, _use

        alice = _user("e2e_alice")
        play = _play(alice)
        session = _session(alice, play)
        record = TaskRecord.objects.create(
            play=play, task_type=TaskType.ASSISTANT_TURN,
            input_summary={"session_id": session.pk})
        runner_mod._save(session, "user",
                         [{"type": "text", "text": "帮我记个灵感"}], record)
        AssistantSession.objects.filter(pk=session.pk).update(running_task=record)
        events.set_backend(events.MemoryStreams())
        self.addCleanup(events.set_backend, None)

        gw = FakeChatGateway([
            _use("t1", "inspiration_save", {"category": "plot_twist",
                                            "content": "主角其实是反派",
                                            "rating": 5}),
            _say("已保存灵感"),
        ])
        with patch("apps.ai_gateway.gateway.AIGateway", return_value=gw):
            runner_mod.run_assistant_turn(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        rec = Inspiration.objects.get()
        self.assertEqual((rec.content, rec.source, rec.rating),
                         ("主角其实是反派", "agent", 5))
        self.assertEqual(rec.play_id, play.pk)
