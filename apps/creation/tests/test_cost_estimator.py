"""成本预测测试（Phase 1.8.2）：单价优先级链、生成动作估算、预算余额、estimate 端点。"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework.test import APITestCase

from apps.ai_gateway.models import AIModel, AIProvider, CostRecord, ProviderType
from apps.creation.assistant import cost_estimator as CE
from apps.creation.assistant.models import AssistantSession
from apps.creation.models import (
    AgentConfig, Episode, MemberRole, MemberStatus, Play, PlayMember, PlayStatus,
    Scene, Shot, TaskRecord, TaskType,
)
from apps.core.system.models import CustomUser


def _model(name, ptype, cost_config=None, priority=1):
    prov = AIProvider.objects.create(name=name, provider_type=ptype, adapter_type="x",
                                     base_url="https://x", api_key_encrypted="", priority=priority)
    return AIModel.objects.create(provider=prov, model_name=name, display_name=name,
                                  model_type=ptype, cost_config=cost_config or {})


class CostFixture(TestCase):
    def setUp(self):
        self.user = CustomUser.objects.create_user(username="ce", password="Passw0rd!123")
        self.config = AgentConfig.objects.create(name="cfg", created_by=self.user)
        self.play = Play.objects.create(title="剧", created_by=self.user,
                                        agent_config=self.config, status=PlayStatus.PAUSED_AT_ASSETS)
        PlayMember.objects.create(play=self.play, user=self.user, role=MemberRole.OWNER,
                                  status=MemberStatus.ACTIVE)
        self.ep = Episode.objects.create(play=self.play, episode_number=1)
        self.scene = Scene.objects.create(episode=self.ep, scene_number=1)
        self.s1 = Shot.objects.create(scene=self.scene, shot_number=1, dialogue="台词一")
        self.s2 = Shot.objects.create(scene=self.scene, shot_number=2, dialogue="")
        self.s3 = Shot.objects.create(scene=self.scene, shot_number=3, dialogue="台词三")

    def history_cost(self, ptype, total):
        prov = AIProvider.objects.create(name=f"h{ptype}{total}", provider_type=ptype,
                                         adapter_type="x", base_url="https://x", priority=1)
        tr = TaskRecord.objects.create(play=self.play, task_type=TaskType.IMAGE_GEN)
        return CostRecord.objects.create(play=self.play, task_record=tr, provider=prov,
                                         model="m", provider_type=ptype,
                                         usage={}, unit_cost=total, total_cost=total)


class UnitCostTests(CostFixture):
    """_unit_cost 优先级：模型 cost_config > 历史均价 > 兜底。"""

    def test_fallback_when_no_config_no_history(self):
        self.assertEqual(CE._unit_cost(self.play, ProviderType.IMAGE), Decimal("0.05"))
        self.assertEqual(CE._unit_cost(self.play, ProviderType.TTS), Decimal("0.02"))
        self.assertEqual(CE._unit_cost(self.play, ProviderType.VIDEO), Decimal("0.50"))
        self.assertEqual(CE._unit_cost(self.play, ProviderType.LLM), Decimal("0.01"))

    def test_history_average_used_when_no_model_config(self):
        self.history_cost(ProviderType.IMAGE, Decimal("0.10"))
        self.history_cost(ProviderType.IMAGE, Decimal("0.20"))
        unit = CE._unit_cost(self.play, ProviderType.IMAGE)
        self.assertEqual(unit, Decimal("0.15"))

    def test_model_cost_config_beats_history(self):
        self.history_cost(ProviderType.IMAGE, Decimal("0.10"))
        m = _model("img", ProviderType.IMAGE, {"cost_per_image": 0.30})
        self.config.image_model = m
        self.config.save(update_fields=["image_model"])
        self.play.refresh_from_db()
        unit = CE._unit_cost(self.play, ProviderType.IMAGE)
        self.assertEqual(unit, Decimal("0.3"))

    def test_llm_uses_assistant_model_then_llm_model(self):
        m = _model("llm", ProviderType.LLM, {"input_per_1k": 0.008})
        self.config.llm_model = m
        self.config.save(update_fields=["llm_model"])
        self.play.refresh_from_db()
        self.assertEqual(CE._unit_cost(self.play, ProviderType.LLM), Decimal("0.008"))

    def test_play_without_agent_config_uses_fallback(self):
        other = Play.objects.create(title="无配置", created_by=self.user)
        self.assertEqual(CE._unit_cost(other, ProviderType.IMAGE), Decimal("0.05"))


class TaskCostTests(CostFixture):
    """estimate_task_cost：各任务类型。"""

    def test_each_task_type(self):
        # phase1 修复 3.2：按任务类型实际规模计价（默认规模：8 镜头/60 秒/3k tokens）
        for tt, expected_pt, expected_cost in (
                (TaskType.IMAGE_GEN, "IMAGE", "0.4000"),      # 0.05 × 8 shots
                (TaskType.TTS_GEN, "TTS", "1.2000"),          # 0.02 × 60s
                (TaskType.VIDEO_COMPOSE, "VIDEO", "30.0000"),   # 0.50 × 60s
                (TaskType.PREVIEW_RENDER, "PREVIEW", "30.0000"),  # 0.50 × 60s
                (TaskType.ASSISTANT_TURN, "LLM", "0.0300")):  # 0.01 × 3k tokens
            est = CE.estimate_task_cost(self.play, tt)
            self.assertEqual(est["task_type"], tt)
            self.assertEqual(est["breakdown"][0]["provider_type"], expected_pt)
            self.assertEqual(est["estimated_cost"], expected_cost)
        # 按秒计价的任务（60s 默认 × 0.02~0.05）超过 ¥1 阈值 → requires_confirm
        self.assertTrue(CE.estimate_task_cost(self.play, TaskType.TTS_GEN)["requires_confirm"])
        # 图片任务默认规模不超阈值
        self.assertFalse(CE.estimate_task_cost(self.play, TaskType.IMAGE_GEN)["requires_confirm"])

    def test_string_task_type_accepted(self):
        # 0.05 × 8 镜头（默认规模）
        est = CE.estimate_task_cost(self.play, "IMAGE_GEN")
        self.assertEqual(est["estimated_cost"], "0.4000")

    def test_unknown_task_type_uses_image_fallback(self):
        est = CE.estimate_task_cost(self.play, "SOMETHING_ELSE")
        self.assertEqual(est["breakdown"][0]["provider_type"], "DEFAULT")
        self.assertEqual(est["estimated_cost"], "0.0500")

    def test_requires_confirm_when_expensive(self):
        m = _model("vid", ProviderType.VIDEO, {"cost_per_second": 1.50})
        self.config.video_model = m
        self.config.save(update_fields=["video_model"])
        self.play.refresh_from_db()
        # 1.50/秒 × 60s 默认时长 = 90（phase1 修复 3.2：按整集规模而非 1 秒）
        est = CE.estimate_task_cost(self.play, TaskType.VIDEO_COMPOSE)
        self.assertTrue(est["requires_confirm"])
        self.assertEqual(est["estimated_cost"], "90.0000")


class GenerationCostTests(CostFixture):
    """estimate_generation_cost：redo_shot / redo_scene / preview_episode。"""

    def test_redo_shot_with_dialogue(self):
        est = CE.estimate_generation_cost(self.play, "redo_shot", self.s1.pk)
        # IMAGE 0.05×1 + TTS 0.02×0.75s（"台词一"3字/4字每秒）= 0.065
        self.assertEqual(est["shot_count"], 1)
        self.assertEqual(est["dialogue_shots"], 1)
        self.assertEqual(est["speech_seconds"], 0.75)
        self.assertEqual(est["estimated_cost"], "0.0650")
        self.assertEqual(len(est["breakdown"]), 2)
        self.assertEqual(est["breakdown"][1]["seconds"], 0.75)

    def test_redo_shot_without_dialogue(self):
        est = CE.estimate_generation_cost(self.play, "redo_shot", self.s2.pk)
        self.assertEqual(est["shot_count"], 1)
        self.assertEqual(est["dialogue_shots"], 0)
        self.assertEqual(est["speech_seconds"], 0)
        self.assertEqual(est["estimated_cost"], "0.0500")
        self.assertEqual(len(est["breakdown"]), 1)

    def test_redo_scene_counts_all_shots(self):
        est = CE.estimate_generation_cost(self.play, "redo_scene", self.scene.pk)
        # 3 shots: 3×0.05 + TTS 0.02×1.5s（"台词一"+"台词三"各0.75s）= 0.18
        self.assertEqual(est["shot_count"], 3)
        self.assertEqual(est["dialogue_shots"], 2)
        self.assertEqual(est["speech_seconds"], 1.5)
        self.assertEqual(est["estimated_cost"], "0.1800")

    def test_long_dialogue_triggers_confirm(self):
        """TTS 按秒计价：多条对白不再按个数低估，≥¥1.00 触发确认。"""
        long_shot = Shot.objects.create(
            scene=self.scene, shot_number=9, dialogue="字" * 40)  # 40字 → 10s
        for n in range(4):
            Shot.objects.create(scene=self.scene, shot_number=10 + n, dialogue="字" * 40)
        est = CE.estimate_generation_cost(self.play, "redo_shot", long_shot.pk)
        self.assertEqual(est["speech_seconds"], 10.0)
        # 单镜头：IMAGE 0.05 + TTS 0.02×10s = 0.25，未过阈值
        self.assertFalse(est["requires_confirm"])
        scene_est = CE.estimate_generation_cost(self.play, "redo_scene", self.scene.pk)
        # 全场景 TTS = 0.02×(5×10s + 0.75 + 0.75) ≈ 1.03，加 IMAGE 8×0.05 → 过阈值
        self.assertTrue(scene_est["requires_confirm"])

    def test_preview_episode(self):
        est = CE.estimate_generation_cost(self.play, "preview_episode", self.ep.pk)
        self.assertEqual(est["action"], "preview_episode")
        # phase1 修复 3.2：整集预览按 60s 默认时长计价（0.50/秒 × 60 = 30），
        # 不再按 1 秒平铺单价（旧值 0.5）
        self.assertEqual(est["estimated_cost"], "30.0000")

    def test_invalid_target_and_action(self):
        # phase1 修复 10.2：业务非法抛 CostEstimateError（视图映射 4xx）
        with self.assertRaises(CE.CostEstimateError):
            CE.estimate_generation_cost(self.play, "redo_shot", 999999)
        with self.assertRaises(CE.CostEstimateError):
            CE.estimate_generation_cost(self.play, "bad_action", 1)

    def test_foreign_shot_rejected(self):
        other_owner = CustomUser.objects.create_user(username="cex", password="Passw0rd!123")
        other = Play.objects.create(title="别人", created_by=other_owner)
        ep = Episode.objects.create(play=other, episode_number=1)
        sc = Scene.objects.create(episode=ep, scene_number=1)
        foreign = Shot.objects.create(scene=sc, shot_number=1, dialogue="机密")
        with self.assertRaises(CE.CostEstimateError):
            CE.estimate_generation_cost(self.play, "redo_shot", foreign.pk)

    def test_cross_play_preview_rejected(self):
        other_owner = CustomUser.objects.create_user(username="cex2", password="Passw0rd!123")
        other = Play.objects.create(title="外剧", created_by=other_owner)
        ep = Episode.objects.create(play=other, episode_number=1)
        with self.assertRaises(CE.CostEstimateError):
            CE.estimate_generation_cost(self.play, "preview_episode", ep.pk)


class BudgetTests(CostFixture):
    def test_no_limit_returns_none(self):
        sess = AssistantSession.objects.create(play=self.play, user=self.user,
                                               agent_config=self.config)
        self.assertIsNone(CE.budget_remaining(sess))


class EstimateApiTests(CostFixture, APITestCase):
    """POST /api/assistant/cost/estimate/"""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.user)

    def _data(self, resp):
        return resp.json().get("data", resp.json())

    def test_estimate_by_action(self):
        resp = self.client.post("/api/assistant/cost/", {
            "play_id": self.play.pk, "action": "redo_scene", "target_id": self.scene.pk})
        self.assertEqual(resp.status_code, 200)
        data = self._data(resp)
        # IMAGE 3×0.05 + TTS 0.02×1.5s（"台词一"+"台词三"各 0.75s）= 0.18
        self.assertEqual(data["estimated_cost"], "0.1800")
        self.assertEqual(data["shot_count"], 3)
        self.assertEqual(data["speech_seconds"], 1.5)

    def test_estimate_by_task_type(self):
        resp = self.client.post("/api/assistant/cost/", {
            "play_id": self.play.pk, "task_type": "TTS_GEN"})
        self.assertEqual(resp.status_code, 200)
        # phase1 修复 3.2：TTS 按秒计价，默认 60s × 0.02 = 1.2（不再是单次 0.02）
        self.assertEqual(self._data(resp)["estimated_cost"], "1.2000")

    def test_missing_params_validated(self):
        resp = self.client.post("/api/assistant/cost/", {"play_id": self.play.pk})
        self.assertEqual(resp.status_code, 400)
        resp2 = self.client.post("/api/assistant/cost/",
                                 {"action": "redo_shot", "target_id": 1})
        self.assertEqual(resp2.status_code, 400)

    def test_foreign_play_404(self):
        other_owner = CustomUser.objects.create_user(username="ceo", password="Passw0rd!123")
        other = Play.objects.create(title="别人", created_by=other_owner)
        resp = self.client.post("/api/assistant/cost/", {
            "play_id": other.pk, "task_type": "IMAGE_GEN"})
        self.assertEqual(resp.status_code, 404)

    def test_budget_remaining_attached(self):
        sess = AssistantSession.objects.create(play=self.play, user=self.user,
                                               agent_config=self.config)
        resp = self.client.post("/api/assistant/cost/", {
            "play_id": self.play.pk, "task_type": "IMAGE_GEN", "session_id": sess.pk})
        data = self._data(resp)
        self.assertIn("budget_remaining", data)
        self.assertIsNone(data["budget_remaining"])  # 无上限


# ── phase1 max 审查修复 3.2：estimate_task_cost 使用 ctx 规模 ────────────

class Phase1TaskScaleTests(CostFixture):
    """estimate_task_cost 按 ctx 实际规模计价，缺省用默认规模。"""

    def test_preview_render_duration_priced(self):
        """PREVIEW_RENDER 提供时长时按秒计价而非 1 秒。"""
        est = CE.estimate_task_cost(self.play, TaskType.PREVIEW_RENDER, duration_s=120)
        # 0.50 × 120s = 60
        self.assertEqual(est["estimated_cost"], "60.0000")
        b = est["breakdown"][0]
        self.assertEqual(b["count"], 120)
        self.assertEqual(b["count_unit"], "seconds")
        self.assertEqual(b["scale"], "provided")

    def test_assistant_turn_tokens_priced(self):
        """ASSISTANT_TURN 提供 16k tokens 时估算约为 1k 的 16 倍。"""
        est_1k = CE.estimate_task_cost(self.play, TaskType.ASSISTANT_TURN,
                                       input_tokens=500, output_tokens=500)
        est_16k = CE.estimate_task_cost(self.play, TaskType.ASSISTANT_TURN,
                                        input_tokens=8000, output_tokens=8000)
        self.assertEqual(est_1k["estimated_cost"], "0.0100")   # 0.01 × 1k
        self.assertEqual(est_16k["estimated_cost"], "0.1600")  # 0.01 × 16k
        b = est_16k["breakdown"][0]
        self.assertEqual(b["count_unit"], "1k_tokens")
        self.assertEqual(b["input_tokens"], 8000)
        self.assertEqual(b["output_tokens"], 8000)

    def test_default_scale_used_when_ctx_missing(self):
        """未提供规模时使用默认规模且 breakdown 标明。"""
        est = CE.estimate_task_cost(self.play, TaskType.IMAGE_GEN)
        b = est["breakdown"][0]
        self.assertEqual(b["count"], 8)
        self.assertEqual(b["count_unit"], "shots")
        self.assertEqual(b["scale"], "default")
        self.assertEqual(est["estimated_cost"], "0.4000")

    def test_invalid_ctx_falls_back_to_default(self):
        """ctx 非法值（负数/字符串）回落默认规模。"""
        est = CE.estimate_task_cost(self.play, TaskType.IMAGE_GEN, shot_count=-3)
        self.assertEqual(est["breakdown"][0]["count"], 8)
        est2 = CE.estimate_task_cost(self.play, TaskType.IMAGE_GEN, shot_count="abc")
        self.assertEqual(est2["breakdown"][0]["count"], 8)

    def test_shot_count_priced(self):
        est = CE.estimate_task_cost(self.play, TaskType.IMAGE_GEN, shot_count=20)
        # 0.05 × 20 = 1.0 → 达阈值
        self.assertEqual(est["estimated_cost"], "1.0000")
        self.assertTrue(est["requires_confirm"])


# ── phase1 max 审查修复 10.2：估算非法请求 4xx ───────────────────────────

class Phase1EstimateErrorApiTests(CostFixture, APITestCase):
    """不存在的 target / 跨剧 target / 未知 action → 4xx 且无 estimated_cost。"""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.user)

    def _post(self, payload):
        return self.client.post("/api/assistant/cost/", payload)

    def test_missing_target_4xx(self):
        resp = self._post({"play_id": self.play.pk, "action": "redo_shot",
                           "target_id": 999999})
        self.assertEqual(resp.status_code, 400)
        body = resp.json().get("data") or resp.json()
        self.assertNotIn("estimated_cost", body)

    def test_cross_play_4xx(self):
        other_owner = CustomUser.objects.create_user(username="p1cx", password="Passw0rd!123")
        other = Play.objects.create(title="外剧", created_by=other_owner)
        ep = Episode.objects.create(play=other, episode_number=1)
        sc = Scene.objects.create(episode=ep, scene_number=1)
        foreign = Shot.objects.create(scene=sc, shot_number=1)
        resp = self._post({"play_id": self.play.pk, "action": "redo_shot",
                           "target_id": foreign.pk})
        self.assertEqual(resp.status_code, 400)
        body = resp.json().get("data") or resp.json()
        self.assertNotIn("estimated_cost", body)

    def test_unknown_action_4xx(self):
        resp = self._post({"play_id": self.play.pk, "action": "bad_action",
                           "target_id": self.s1.pk})
        self.assertEqual(resp.status_code, 400)
        body = resp.json().get("data") or resp.json()
        self.assertNotIn("estimated_cost", body)
