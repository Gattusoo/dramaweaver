"""多模态视觉理解测试（openspec change: 2026-11-14-backend-multimodal / Phase 2.3）。"""
import base64
import json
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.ai_gateway.gateway import AIGateway, AIGatewayError
from apps.ai_gateway.models import AIModel, AIProvider
from apps.ai_gateway.redis_client import MemoryStore
from apps.ai_gateway.router import build_vision_chain
from apps.creation.assistant import tools, vision
from apps.creation.assistant.vision import (
    CONSISTENCY_THRESHOLD, VisionUnavailable, analyze_image, compare_with_script, review_asset,
    suggest_improvements,
)
from apps.creation.models import (
    AgentConfig, Asset, AssetStatus, AssetType, Episode, Play, PlayStatus, Scene, Shot,
    TaskRecord, TaskType,
)
from dvadmin.system.models import CustomUser

VISION_ON = {"ENABLE_MULTIMODAL": True, "ENABLE_VISION_UNDERSTANDING": True}


class FakeVisionGateway:
    """按顺序返回 vision() 的 JSON 文本；记录调用参数。"""

    def __init__(self, payloads):
        self.payloads = list(payloads)
        self.calls = []

    def vision(self, agent_config, *, image_url, prompt, play=None, task_record=None, **kw):
        self.calls.append({"image_url": image_url, "prompt": prompt})
        data = self.payloads.pop(0)
        if isinstance(data, Exception):
            raise data
        return {"content_blocks": [{"type": "text", "text": json.dumps(data, ensure_ascii=False)}],
                "stop_reason": "end_turn", "usage": {"input_tokens": 100, "output_tokens": 30},
                "model": "vision"}


class VisionFixtureMixin:
    def setUp(self):
        super().setUp()
        self.user = CustomUser.objects.create_user(username="v", password="Passw0rd!123")
        self.config = AgentConfig.objects.create(name="cfg", created_by=self.user)
        self.play = Play.objects.create(title="霸总", created_by=self.user, agent_config=self.config,
                                        status=PlayStatus.PAUSED_AT_SCRIPT)
        self.episode = Episode.objects.create(play=self.play, episode_number=1)
        self.scene = Scene.objects.create(episode=self.episode, scene_number=1, description="办公室")
        self.shot = Shot.objects.create(scene=self.scene, shot_number=1, visual_prompt="总裁站在落地窗前")
        self.asset = Asset.objects.create(play=self.play, episode=self.episode, scene=self.scene,
                                          shot=self.shot, asset_type=AssetType.IMAGE,
                                          status=AssetStatus.READY, artifact_url="/media/images/a.png")
        from apps.creation.assistant.models import AssistantSession
        self.session = AssistantSession.objects.create(play=self.play, user=self.user,
                                                       agent_config=self.config)


class VisionChainTests(VisionFixtureMixin, TestCase):
    def make_model(self, name, vision=False, tools_ok=True):
        provider = AIProvider.objects.create(name=f"p{name}", provider_type="LLM", adapter_type="deepseek",
                                             base_url="https://x", api_key_encrypted="", priority=1)
        return AIModel.objects.create(provider=provider, model_name=name, display_name=name,
                                      model_type="LLM",
                                      capabilities={"supports_vision": vision, "supports_tools": tools_ok})

    def test_only_vision_models_in_chain(self):
        text_only = self.make_model("text", vision=False)
        self.config.llm_model = text_only
        self.config.save()
        self.assertEqual(build_vision_chain(self.config), [])
        seeing = self.make_model("see", vision=True, tools_ok=False)
        self.config.assistant_model = seeing
        self.config.save()
        self.assertEqual([m.model_name for m, _ in build_vision_chain(self.config)], ["see"])

    def test_vision_raises_without_candidates(self):
        with self.assertRaises(AIGatewayError):
            AIGateway(store=MemoryStore()).vision(self.config, image_url="https://x/a.png", prompt="hi")


class VisionBlockTests(TestCase):
    def test_http_url_passthrough(self):
        from apps.ai_gateway.gateway import _vision_block
        self.assertEqual(_vision_block("https://x/a.png"), {"url": "https://x/a.png"})

    def test_data_uri_split(self):
        from apps.ai_gateway.gateway import _vision_block
        block = _vision_block("data:image/png;base64,AAA")
        self.assertEqual(block["source"], {"type": "base64", "media_type": "image/png", "data": "AAA"})

    def test_local_media_file_read(self):
        import tempfile
        from pathlib import Path
        from django.test import override_settings
        from apps.ai_gateway.gateway import _vision_block
        with tempfile.TemporaryDirectory() as tmp:
            Path(tmp, "images").mkdir()
            Path(tmp, "images", "x.png").write_bytes(b"PNGDATA")
            with override_settings(MEDIA_ROOT=tmp, MEDIA_URL="/media/"):
                block = _vision_block("/media/images/x.png")
        self.assertEqual(base64.b64decode(block["source"]["data"]), b"PNGDATA")
        self.assertEqual(block["source"]["media_type"], "image/png")

    def test_missing_local_file_raises(self):
        from apps.ai_gateway.gateway import _vision_block
        with self.assertRaises(AIGatewayError):
            _vision_block("/media/images/nope.png")


class AdapterContentTests(TestCase):
    def test_openai_messages_with_image(self):
        from apps.ai_gateway.adapters import to_openai_messages
        out = to_openai_messages([{"role": "user", "content": [
            {"type": "image", "url": "https://x/a.png"},
            {"type": "text", "text": "描述这张图"}]}])
        self.assertEqual(out[0]["content"][0], {"type": "image_url", "image_url": {"url": "https://x/a.png"}})
        self.assertEqual(out[0]["content"][1], {"type": "text", "text": "描述这张图"})

    def test_anthropic_content_source(self):
        from apps.ai_gateway.adapters import _anthropic_content
        out = _anthropic_content([{"type": "image", "url": "https://x/a.png"}])
        self.assertEqual(out[0], {"type": "image", "source": {"type": "url", "url": "https://x/a.png"}})
        kept = [{"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "A"}}]
        self.assertEqual(_anthropic_content(kept), kept)


class AnalyzeTests(VisionFixtureMixin, TestCase):
    @override_settings(**VISION_ON)
    def test_analyze_image(self):
        gw = FakeVisionGateway([{"scene": "落地窗前的总裁", "characters": ["林总"],
                                 "composition": "中景", "mood": "冷峻", "text": "",
                                 "issues": ["领带颜色与设定不符"]}])
        result = analyze_image(self.session, self.asset.artifact_url, gateway=gw)
        self.assertEqual(result["characters"], ["林总"])
        self.assertEqual(result["issues"], ["领带颜色与设定不符"])
        self.assertEqual(gw.calls[0]["image_url"], self.asset.artifact_url)

    @override_settings(**VISION_ON)
    def test_analyze_unparsable_raises(self):
        gw = FakeVisionGateway([])
        gw.vision = MagicMock(return_value={"content_blocks": [{"type": "text", "text": "不是JSON"}]})
        with self.assertRaises(VisionUnavailable):
            analyze_image(self.session, "https://x/a.png", gateway=gw)

    @override_settings(ENABLE_MULTIMODAL=False, ENABLE_VISION_UNDERSTANDING=True)
    def test_disabled_raises(self):
        with self.assertRaises(VisionUnavailable):
            analyze_image(self.session, "https://x/a.png", gateway=FakeVisionGateway([]))

    @override_settings(**VISION_ON)
    def test_compare_consistent(self):
        gw = FakeVisionGateway([{"consistency": 0.9, "matches": ["窗边站位"], "mismatches": [],
                                 "reason": "符合"}])
        result = compare_with_script(self.session, "https://x/a.png", "总裁站在落地窗前", gateway=gw)
        self.assertTrue(result["consistent"])
        self.assertIn("总裁站在落地窗前", gw.calls[0]["prompt"])

    @override_settings(**VISION_ON)
    def test_compare_low_score_inconsistent(self):
        gw = FakeVisionGateway([{"consistency": 0.6, "mismatches": ["缺西装"]}])
        result = compare_with_script(self.session, "https://x/a.png", "西装总裁", gateway=gw)
        self.assertFalse(result["consistent"])
        self.assertLess(result["consistency"], CONSISTENCY_THRESHOLD)

    @override_settings(**VISION_ON)
    def test_compare_missing_score_raises(self):
        gw = FakeVisionGateway([{"reason": "无法判断"}])
        with self.assertRaises(VisionUnavailable):
            compare_with_script(self.session, "https://x/a.png", "x", gateway=gw)

    @override_settings(**VISION_ON)
    def test_suggest_improvements(self):
        gw = FakeVisionGateway([{"suggestions": ["换深色西装", "补侧光"],
                                 "prompt_patch": "dark suit, side light"}])
        result = suggest_improvements(self.session, "https://x/a.png", expected="西装", gateway=gw)
        self.assertEqual(result["suggestions"], ["换深色西装", "补侧光"])
        self.assertEqual(result["prompt_patch"], "dark suit, side light")


class ReviewAssetTests(VisionFixtureMixin, TestCase):
    @override_settings(**VISION_ON)
    def test_review_flags_regeneration(self):
        gw = FakeVisionGateway([
            {"scene": "x", "issues": ["多手"], "characters": []},
            {"consistency": 0.5, "mismatches": ["缺西装"]},
            {"suggestions": ["修正手部"], "prompt_patch": "fix hands"},
        ])
        result = review_asset(self.session, self.asset, expected="西装总裁", gateway=gw)
        self.assertFalse(result["skipped"])
        self.assertTrue(result["needs_regeneration"])
        self.assertEqual(result["suggestions"]["prompt_patch"], "fix hands")
        self.assertEqual(len(gw.calls), 3)

    @override_settings(**VISION_ON)
    def test_review_clean_asset(self):
        gw = FakeVisionGateway([
            {"scene": "ok", "issues": [], "characters": []},
            {"consistency": 0.95, "matches": ["西装"]},
        ])
        result = review_asset(self.session, self.asset, expected="西装", gateway=gw)
        self.assertFalse(result["needs_regeneration"])

    @override_settings(**VISION_ON)
    def test_review_zero_budget_skips(self):
        self.config.assistant_budget = {"max_vision_cost": 0}
        self.config.save()
        gw = FakeVisionGateway([])
        result = review_asset(self.session, self.asset, gateway=gw)
        self.assertTrue(result["skipped"])
        self.assertFalse(result["needs_regeneration"])
        self.assertEqual(gw.calls, [])

    @override_settings(**VISION_ON)
    def test_review_vision_error_skips(self):
        gw = FakeVisionGateway([VisionUnavailable("模型不可用")])
        result = review_asset(self.session, self.asset, gateway=gw)
        self.assertTrue(result["skipped"])
        self.assertIn("模型不可用", result["reason"])

    @override_settings(ENABLE_MULTIMODAL=True, ENABLE_VISION_UNDERSTANDING=False)
    def test_review_disabled_skips(self):
        result = review_asset(self.session, self.asset, gateway=FakeVisionGateway([]))
        self.assertTrue(result["skipped"])


class AnalyzeImageToolTests(VisionFixtureMixin, TestCase):
    def ctx(self):
        return tools.ToolContext(self.session)

    @override_settings(**VISION_ON)
    def test_tool_reviews_own_asset(self):
        gw = FakeVisionGateway([{"scene": "x", "issues": [], "characters": []},
                                {"consistency": 0.9}])
        with patch("apps.ai_gateway.gateway.AIGateway", return_value=gw):
            result, is_error = tools.run_tool(self.ctx(), "analyze_generated_image",
                                              {"asset_id": self.asset.pk})
        self.assertFalse(is_error, result)
        self.assertEqual(result["asset_id"], self.asset.pk)

    @override_settings(**VISION_ON)
    def test_tool_rejects_foreign_asset(self):
        other_user = CustomUser.objects.create_user(username="o", password="Passw0rd!123")
        other_play = Play.objects.create(title="别的", created_by=other_user)
        foreign = Asset.objects.create(play=other_play, asset_type=AssetType.IMAGE,
                                       status=AssetStatus.READY, artifact_url="/media/images/f.png")
        result, is_error = tools.run_tool(self.ctx(), "analyze_generated_image", {"asset_id": foreign.pk})
        self.assertTrue(is_error)
        self.assertNotIn("f.png", str(result))

    @override_settings(ENABLE_MULTIMODAL=False, ENABLE_VISION_UNDERSTANDING=False)
    def test_tool_skips_when_disabled(self):
        result, is_error = tools.run_tool(self.ctx(), "analyze_generated_image", {"asset_id": self.asset.pk})
        self.assertFalse(is_error)
        self.assertTrue(result["skipped"])


class ImageTaskReviewTests(VisionFixtureMixin, TestCase):
    def test_review_written_to_metadata(self):
        from apps.creation.tasks import _maybe_vision_review
        with override_settings(**VISION_ON), patch(
                "apps.creation.assistant.vision.review_asset",
                return_value={"skipped": False, "needs_regeneration": True, "issues": ["多手"]}):
            verdict = _maybe_vision_review(self.play, self.asset, self.shot)
        self.asset.refresh_from_db()
        self.assertTrue(verdict["needs_regeneration"])
        self.assertEqual(self.asset.metadata["quality"]["issues"], ["多手"])
        self.assertEqual(self.asset.status, AssetStatus.READY)

    def test_review_skipped_when_flag_off(self):
        from apps.creation.tasks import _maybe_vision_review
        with override_settings(ENABLE_MULTIMODAL=False, ENABLE_VISION_UNDERSTANDING=False):
            self.assertIsNone(_maybe_vision_review(self.play, self.asset, self.shot))
        self.asset.refresh_from_db()
        self.assertNotIn("quality", self.asset.metadata)

    def test_review_failure_silent(self):
        from apps.creation.tasks import _maybe_vision_review
        with override_settings(**VISION_ON), patch(
                "apps.creation.assistant.vision.review_asset", side_effect=RuntimeError("boom")):
            with self.assertLogs("apps.creation.tasks", "ERROR"):
                self.assertIsNone(_maybe_vision_review(self.play, self.asset, self.shot))
        self.asset.refresh_from_db()
        self.assertEqual(self.asset.status, AssetStatus.READY)


# ── Phase 2 code-review 修复回归 ─────────────────────────────────────────────

class VisionBudgetAccumulationTests(VisionFixtureMixin, TestCase):
    """修复：视觉预算只挡 0，非 0 上限不做任何累计约束。"""

    @override_settings(**VISION_ON)
    def test_budget_stops_subsequent_calls(self):
        from apps.ai_gateway.models import CostRecord
        provider = AIProvider.objects.create(name="p", provider_type="LLM", adapter_type="deepseek",
                                             base_url="https://x", api_key_encrypted="", priority=1)
        record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                           input_summary={"session_id": self.session.pk})
        self.config.assistant_budget = {"max_vision_cost": 0.05}
        self.config.save()

        class CountingGateway:
            def __init__(self):
                self.calls = 0

            def vision(self, agent_config, *, image_url, prompt, play=None, task_record=None, **kw):
                self.calls += 1
                CostRecord.objects.create(play=play, task_record=task_record, provider=provider,
                                           model="v", provider_type="LLM", usage={},
                                           unit_cost="0", total_cost=Decimal("0.04"), currency="CNY")
                # 前两次调用按 analyze/compare 的形状返回（每次 0.04）
                payload = ({"scene": "x", "issues": ["多手"], "characters": []}
                           if self.calls == 1 else
                           {"consistency": 0.5, "mismatches": ["缺西装"], "reason": "不符"})
                return {"content_blocks": [{"type": "text", "text": json.dumps(payload)}],
                        "stop_reason": "end_turn"}

        gw = CountingGateway()
        result = review_asset(self.session, self.asset, expected="西装", gateway=gw, record=record)
        # analyze 花 0.04（< 0.05）→ compare 继续，再花 0.04 → 累计 0.08 超限，建议步被挡
        self.assertEqual(gw.calls, 2)
        self.assertTrue(result["needs_regeneration"])
        self.assertTrue(result.get("budget_stopped"))
        self.assertFalse(result["skipped"])

    @override_settings(**VISION_ON)
    def test_generous_budget_runs_all_steps(self):
        from apps.ai_gateway.models import CostRecord
        provider = AIProvider.objects.create(name="p2", provider_type="LLM", adapter_type="deepseek",
                                             base_url="https://x", api_key_encrypted="", priority=1)
        record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                           input_summary={})
        self.config.assistant_budget = {"max_vision_cost": 10}
        self.config.save()

        class CountingGateway:
            def __init__(self):
                self.calls = 0

            def vision(self, agent_config, *, image_url, prompt, play=None, task_record=None, **kw):
                self.calls += 1
                CostRecord.objects.create(play=play, task_record=task_record, provider=provider,
                                           model="v", provider_type="LLM", usage={},
                                           unit_cost="0", total_cost=Decimal("0.001"), currency="CNY")
                payloads = [
                    {"scene": "x", "issues": ["多手"], "characters": []},
                    {"consistency": 0.5, "mismatches": ["缺西装"]},
                    {"suggestions": ["修正手部"], "prompt_patch": "fix hands"},
                ]
                return {"content_blocks": [{"type": "text", "text": json.dumps(payloads[self.calls - 1])}],
                        "stop_reason": "end_turn"}

        gw = CountingGateway()
        result = review_asset(self.session, self.asset, expected="西装", gateway=gw, record=record)
        self.assertEqual(gw.calls, 3)
        self.assertTrue(result["needs_regeneration"])
        self.assertFalse(result.get("budget_stopped"))
