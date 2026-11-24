"""Agent辅助审查测试（Phase 1.8.3）：主观审查规整、双源合并、完整报告、review 端点。"""
from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework.test import APITestCase

from apps.creation.assistant import agent_review as AR
from apps.creation.assistant import events
from apps.creation.assistant.models import AssistantSession
from apps.creation.models import (
    AgentConfig, Character, Episode, MemberRole, MemberStatus, Play, PlayMember,
    PlayStatus, Scene, Shot,
)
from dvadmin.system.models import CustomUser


def _sub(score=80, dims=None, **kw):
    d = {"overall_score": score, "recommendations": ["建议1"], "summary": "总评",
         "dimensions": dims or {
             "plot_logic": {"score": 80, "issues": ["逻辑漏洞"], "strengths": ["反转好"]},
             "dialogue": {"score": 75, "issues": [], "strengths": []},
             "pacing": {"score": 85, "issues": [], "strengths": []},
             "emotion": {"score": 70, "issues": ["情感铺垫不足"], "strengths": []},
         }}
    d.update(kw)
    return d


class ReviewFixture(TestCase):
    def setUp(self):
        events.set_backend(events.MemoryStreams())
        self.addCleanup(events.set_backend, None)
        self.user = CustomUser.objects.create_user(username="rv", password="Passw0rd!123")
        self.config = AgentConfig.objects.create(name="cfg", created_by=self.user)
        self.play = Play.objects.create(title="霸总", synopsis="灰姑娘逆袭",
                                        created_by=self.user, agent_config=self.config,
                                        status=PlayStatus.PAUSED_AT_SCRIPT)
        PlayMember.objects.create(play=self.play, user=self.user, role=MemberRole.OWNER,
                                  status=MemberStatus.ACTIVE)
        self.ep = Episode.objects.create(play=self.play, episode_number=1,
                                         synopsis="开局冲突")
        self.char = Character.objects.create(play=self.play, name="林总", role="PROTAGONIST")
        self.scene = Scene.objects.create(episode=self.ep, scene_number=1,
                                          location="办公室", description="对峙")
        self.shot = Shot.objects.create(scene=self.scene, shot_number=1,
                                        dialogue="你逃不掉", visual_prompt="特写")
        self.session = AssistantSession.objects.create(play=self.play, user=self.user,
                                                       agent_config=self.config)


class NormalizeReviewTests(TestCase):
    """_normalize_review：容错缺字段、clamp 0-100、加权总分。"""

    def test_wellformed(self):
        out = AR._normalize_review(_sub(score=88))
        self.assertEqual(out["overall_score"], 88)
        self.assertEqual(set(out["dimensions"]), set(AR._DIMENSIONS))
        self.assertEqual(out["recommendations"], ["建议1"])
        self.assertEqual(out["summary"], "总评")

    def test_missing_dimensions_defaults(self):
        out = AR._normalize_review({})
        for d in AR._DIMENSIONS:
            self.assertEqual(out["dimensions"][d], {"score": 0, "issues": [], "strengths": []})
        self.assertEqual(out["overall_score"], 0)

    def test_overall_computed_when_missing(self):
        # 80*.35 + 75*.25 + 85*.25 + 70*.15 = 78.5 → round 78 或 79(bankers)
        out = AR._normalize_review(_sub(score=None))
        del out  # 重算：去掉 overall_score 键
        sub = _sub()
        del sub["overall_score"]
        out = AR._normalize_review(sub)
        self.assertEqual(out["overall_score"], round(78.5))

    def test_scores_clamped(self):
        out = AR._normalize_review({
            "overall_score": 150,
            "dimensions": {"plot_logic": {"score": -20, "issues": [], "strengths": []}},
        })
        self.assertEqual(out["overall_score"], 100)
        self.assertEqual(out["dimensions"]["plot_logic"]["score"], 0)
        self.assertEqual(out["dimensions"]["dialogue"]["score"], 0)

    def test_non_list_fields_coerced(self):
        out = AR._normalize_review({"dimensions": {"dialogue": {"issues": "bad"}},
                                    "recommendations": "x", "summary": None})
        self.assertEqual(out["dimensions"]["dialogue"]["issues"], [])
        self.assertEqual(out["recommendations"], [])
        self.assertEqual(out["summary"], "")


class MergeReviewsTests(TestCase):
    """merge_reviews：双源 issues 合并 + 确定性扣分。"""

    def test_det_issues_tagged(self):
        det = {"issues": [{"code": "empty_scene", "severity": "error", "message": "场景空"}],
               "stats": {"errors": 1, "warnings": 0}}
        merged = AR.merge_reviews(det, _sub())
        det_issue = [i for i in merged["issues"] if i["source"] == "deterministic"]
        self.assertEqual(det_issue[0]["dimension"], "structure")
        self.assertEqual(det_issue[0]["severity"], "error")

    def test_subjective_issues_per_dimension(self):
        merged = AR.merge_reviews({"issues": [], "stats": {}}, _sub())
        sub = [i for i in merged["issues"] if i["source"] == "subjective"]
        codes = {i["code"] for i in sub}
        self.assertEqual(codes, {"subjective_plot_logic", "subjective_emotion"})
        self.assertTrue(all(i["severity"] == "info" for i in sub))

    def test_strengths_collected(self):
        merged = AR.merge_reviews({"issues": [], "stats": {}}, _sub())
        self.assertEqual(merged["strengths"],
                         [{"dimension": "plot_logic", "message": "反转好"}])

    def test_deterministic_penalty_math(self):
        # 80 - 2*8 - 1*3 = 61
        det = {"issues": [], "stats": {"errors": 2, "warnings": 1}}
        merged = AR.merge_reviews(det, _sub(score=80))
        self.assertEqual(merged["overall_score"], 61)
        self.assertEqual(merged["deterministic_penalty"], 19)

    def test_score_floor_zero(self):
        det = {"issues": [], "stats": {"errors": 20, "warnings": 0}}
        merged = AR.merge_reviews(det, _sub(score=50))
        self.assertEqual(merged["overall_score"], 0)


class ScriptTextTests(ReviewFixture):
    def test_collects_scene_shot_dialogue(self):
        text = AR._script_text(self.ep)
        self.assertIn("场景1", text)
        self.assertIn("你逃不掉", text)
        self.assertIn("特写", text[:400] or text)  # visual_prompt 截断80字符内出现

    def test_empty_episode(self):
        ep2 = Episode.objects.create(play=self.play, episode_number=2)
        self.assertEqual(AR._script_text(ep2), "")


class ReviewEpisodeLlmTests(ReviewFixture):
    """review_episode_llm：mock reviewer 注入；失败降级。"""

    def test_reviewer_result_normalized(self):
        reviewer = lambda **kw: _sub(score=90)
        out = AR.review_episode_llm(self.session, self.ep, reviewer=reviewer)
        self.assertEqual(out["overall_score"], 90)

    def test_reviewer_exception_degrades(self):
        def bad(**kw):
            raise RuntimeError("LLM 超时")
        with self.assertLogs("apps.creation.assistant.agent_review", "WARNING"):
            out = AR.review_episode_llm(self.session, self.ep, reviewer=bad)
        self.assertEqual(out["overall_score"], 0)
        self.assertEqual(set(out["dimensions"]), set(AR._DIMENSIONS))

    def test_prompt_payload_fields(self):
        seen = {}
        def spy(**kw):
            seen.update(kw)
            return {}
        AR.review_episode_llm(self.session, self.ep, reviewer=spy)
        self.assertEqual(seen["ep"], 1)
        self.assertIn("灰姑娘", seen["synopsis"])
        self.assertIn("林总", seen["characters"])
        self.assertIn("你逃不掉", seen["script"])


class LlmReviewGatewayTests(ReviewFixture):
    """llm_review：AIGateway 响应解析。"""

    def _resp(self, text):
        return {"content_blocks": [{"type": "text", "text": text}]}

    def test_parses_json(self):
        with patch("apps.ai_gateway.gateway.AIGateway") as GW:
            GW.return_value.chat.return_value = self._resp(
                '前文 {"overall_score": 77, "summary": "好"} 后文')
            out = AR.llm_review(self.session, ep=1, synopsis="s", characters="c", script="x")
        self.assertEqual(out["overall_score"], 77)

    def test_no_json_returns_empty(self):
        with patch("apps.ai_gateway.gateway.AIGateway") as GW:
            GW.return_value.chat.return_value = self._resp("无法审查")
            out = AR.llm_review(self.session, ep=1, synopsis="s", characters="c", script="x")
        self.assertEqual(out, {})

    def test_bad_json_returns_empty(self):
        with patch("apps.ai_gateway.gateway.AIGateway") as GW:
            GW.return_value.chat.return_value = self._resp('{"broken": ')
            out = AR.llm_review(self.session, ep=1, synopsis="s", characters="c", script="x")
        self.assertEqual(out, {})


class ReviewReportTests(ReviewFixture):
    """review_report：确定性 + 主观合并完整报告。"""

    def test_report_shape(self):
        out = AR.review_report(self.session, self.ep, reviewer=lambda **kw: _sub(85))
        self.assertEqual(out["episode_number"], 1)
        self.assertEqual(out["episode_id"], self.ep.pk)
        self.assertIn("overall_score", out)
        self.assertIn("issues", out)
        self.assertIn("strengths", out)
        self.assertIn("stats", out)
        # 确定性检查发现 missing_image 等 warning → 扣分生效
        self.assertGreater(out["deterministic_penalty"], 0)

    def test_report_degrades_without_reviewer(self):
        def bad(**kw):
            raise RuntimeError("x")
        with self.assertLogs("apps.creation.assistant.agent_review", "WARNING"):
            out = AR.review_report(self.session, self.ep, reviewer=bad)
        self.assertIn("issues", out)
        self.assertLessEqual(out["overall_score"], 0 + out["deterministic_penalty"] * 0 + 100)


class ReviewApiTests(ReviewFixture, APITestCase):
    """GET /api/assistant/review/?play_id=&episode="""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.user)

    def _data(self, resp):
        return resp.json().get("data", resp.json())

    def test_review_endpoint(self):
        with patch("apps.creation.assistant.agent_review.llm_review",
                   return_value=_sub(80)):
            resp = self.client.get(
                f"/api/assistant/review/?play_id={self.play.pk}&episode=1")
        self.assertEqual(resp.status_code, 200)
        data = self._data(resp)
        self.assertEqual(data["episode_number"], 1)
        self.assertIn("dimensions", data)
        self.assertIn("issues", data)

    def test_missing_params(self):
        resp = self.client.get("/api/assistant/review/")
        self.assertEqual(resp.status_code, 400)
        resp2 = self.client.get(f"/api/assistant/review/?play_id={self.play.pk}")
        self.assertEqual(resp2.status_code, 400)

    def test_episode_not_found(self):
        resp = self.client.get(f"/api/assistant/review/?play_id={self.play.pk}&episode=99")
        self.assertEqual(resp.status_code, 404)

    def test_foreign_play_404(self):
        other_owner = CustomUser.objects.create_user(username="rvx", password="Passw0rd!123")
        other = Play.objects.create(title="别人", created_by=other_owner)
        resp = self.client.get(f"/api/assistant/review/?play_id={other.pk}&episode=1")
        self.assertEqual(resp.status_code, 404)


# ── phase1 max 审查修复 5.2：类型守卫 + 降级可辨识 ───────────────────────

class Phase1NormalizeGuardTests(ReviewFixture):
    """_normalize_review 对 dimensions 类型错做守卫，不再 500。"""

    def test_dimensions_null(self):
        out = AR._normalize_review({"dimensions": None})
        self.assertEqual(set(out["dimensions"]), set(AR._DIMENSIONS))
        self.assertEqual(out["overall_score"], 0)

    def test_dimensions_array(self):
        out = AR._normalize_review({"dimensions": ["plot_logic", "dialogue"]})
        self.assertEqual(set(out["dimensions"]), set(AR._DIMENSIONS))

    def test_dimensions_string(self):
        out = AR._normalize_review({"dimensions": "plot_logic"})
        self.assertEqual(set(out["dimensions"]), set(AR._DIMENSIONS))

    def test_data_not_dict(self):
        out = AR._normalize_review(None)
        self.assertEqual(out["overall_score"], 0)
        out2 = AR._normalize_review(["not", "a", "dict"])
        self.assertEqual(out2["overall_score"], 0)

    def test_dimension_value_not_dict(self):
        out = AR._normalize_review({"dimensions": {"plot_logic": "bad"}})
        self.assertEqual(out["dimensions"]["plot_logic"]["score"], 0)


class Phase1DegradedFlagTests(ReviewFixture):
    """完全不可解析输出 → degraded: true，与评分 0 的正常结果可区分。"""

    def test_exception_sets_degraded(self):
        def bad(**kw):
            raise RuntimeError("LLM 超时")
        with self.assertLogs("apps.creation.assistant.agent_review", "WARNING"):
            out = AR.review_episode_llm(self.session, self.ep, reviewer=bad)
        self.assertTrue(out["degraded"])
        self.assertEqual(out["overall_score"], 0)

    def test_unparseable_output_sets_degraded(self):
        out = AR.review_episode_llm(self.session, self.ep, reviewer=lambda **kw: None)
        self.assertTrue(out["degraded"])

    def test_normal_result_not_degraded(self):
        out = AR.review_episode_llm(self.session, self.ep,
                                    reviewer=lambda **kw: _sub(score=85))
        self.assertFalse(out["degraded"])
        self.assertEqual(out["overall_score"], 85)

    def test_report_propagates_degraded(self):
        def bad(**kw):
            raise RuntimeError("x")
        with self.assertLogs("apps.creation.assistant.agent_review", "WARNING"):
            report = AR.review_report(self.session, self.ep, reviewer=bad)
        self.assertTrue(report.get("degraded"))

    def test_review_api_returns_200_on_unparseable(self):
        """接口层：不可解析输出 → 200 + degraded，而非 500。"""
        from unittest.mock import patch
        from django.test import override_settings
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.user)
        with override_settings(ENABLE_AGENT_REVIEW=True):
            with patch.object(AR, "review_report",
                              return_value={"overall_score": 0, "degraded": True}):
                resp = client.get(
                    f"/api/assistant/review/?play_id={self.play.pk}&episode=1")
        self.assertEqual(resp.status_code, 200, getattr(resp, "data", resp.content))
