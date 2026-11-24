"""Distribution & Subtitle 验证测试（openspec change: backend-distribution-subtitle）。"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APITestCase

from apps.distribution.analytics import (
    cost_by_provider, play_cost_breakdown, production_stats, publication_summary,
)
from apps.distribution.publication import (
    advance_status, create_publication, mark_published, mark_rejected, mark_reviewing,
)
from apps.distribution.subtitles import (
    format_srt, generate_subtitle_track, translate_subtitle_track,
)
from apps.ai_gateway.models import (
    AIModel, AIProvider, CostRecord,
)
from apps.creation.models import (
    AgentConfig, Asset, AssetStatus, AssetType, AuditMode, Episode, Play, PlayStatus, Scene, Shot, TaskRecord, TaskType,
)
from dvadmin.system.models import (
    CustomUser, UserRole,
)
from apps.distribution.models import (
    DistributionChannel, Publication, PublicationStatus, SubtitleSourceType, SubtitleStatus, SubtitleTrack,
)


class BaseDistTest(TestCase):
    def setUp(self):
        self.producer = CustomUser.objects.create_user(
            username="prod", password="Passw0rd!123", role=UserRole.PRODUCER
        )
        self.provider = AIProvider.objects.create(
            name="P", provider_type="LLM", adapter_type="deepseek",
            base_url="https://x", api_key_encrypted="", priority=1,
        )
        self.model = AIModel.objects.create(
            provider=self.provider, model_name="m", display_name="M", model_type="LLM",
        )
        self.config = AgentConfig.objects.create(
            name="配方", llm_model=self.model, created_by=self.producer,
        )
        self.play = Play.objects.create(
            title="短剧", total_episodes=1, agent_config=self.config,
            created_by=self.producer, audit_mode=AuditMode.NONE,
        )
        self.episode = Episode.objects.create(play=self.play, episode_number=1)
        self.scene = Scene.objects.create(episode=self.episode, scene_number=1)
        self.shot = Shot.objects.create(
            scene=self.scene, shot_number=1, dialogue="你以为你还能逃吗",
        )
        self.audio = Asset.objects.create(
            play=self.play, episode=self.episode, scene=self.scene, shot=self.shot,
            asset_type=AssetType.AUDIO, status=AssetStatus.READY,
            metadata={"duration_seconds": 2.0},
        )


class SubtitleGenerationTest(BaseDistTest):
    """任务 7.1 字幕自动生成。"""

    def test_generate_auto_subtitle(self):
        track, created = generate_subtitle_track(self.episode, "zh-CN")
        self.assertTrue(created)
        self.assertEqual(track.language, "zh-CN")
        self.assertEqual(track.source_type, SubtitleSourceType.AUTO)
        self.assertEqual(track.status, SubtitleStatus.READY)
        self.assertTrue(track.subtitle_data)
        self.assertAlmostEqual(track.subtitle_data[0]["start"], 0.0)
        self.assertAlmostEqual(track.subtitle_data[0]["end"], 2.0)
        self.assertEqual(track.subtitle_data[0]["text"], "你以为你还能逃吗")


class SubtitleTranslationTest(BaseDistTest):
    """任务 7.2 多语言翻译 + B.1 行映射不错位。"""

    def test_translate_subtitle(self):
        source, _ = generate_subtitle_track(self.episode, "zh-CN")

        class FakeGW:
            def call(self, *a, **k):
                return {"text": "1. You think you can escape?"}

        translated = translate_subtitle_track(
            source, "en-US", FakeGW(), self.config, self.play,
        )
        self.assertEqual(translated.language, "en-US")
        self.assertEqual(translated.source_type, SubtitleSourceType.TRANSLATION)
        self.assertEqual(translated.status, SubtitleStatus.READY)
        self.assertEqual(translated.subtitle_data[0]["text"], "You think you can escape?")

    def test_translate_preamble_not_shifted(self):
        """LLM 多输出前言 → 前言丢弃，翻译与条目一一对应（不再位置错位）。"""
        source, _ = generate_subtitle_track(self.episode, "zh-CN")

        class FakeGW:
            def call(self, *a, **k):
                return {"text": "好的，以下是您要求的翻译结果：\n1. You think you can escape?"}

        translated = translate_subtitle_track(source, "en-US", FakeGW(), self.config, self.play)
        self.assertEqual(translated.subtitle_data[0]["text"], "You think you can escape?")

    def test_translate_missing_lines_fallback_original(self):
        """LLM 行数不足 → 缺失条目回退原文，其余不偏移。"""
        shot2 = Shot.objects.create(scene=self.scene, shot_number=2, dialogue="放我出去")
        Asset.objects.create(play=self.play, episode=self.episode, scene=self.scene,
                             shot=shot2, asset_type=AssetType.AUDIO, status=AssetStatus.READY,
                             metadata={"duration_seconds": 2.0})
        source, _ = generate_subtitle_track(self.episode, "zh-CN")
        self.assertEqual(len(source.subtitle_data), 2)

        class FakeGW:
            def call(self, *a, **k):
                return {"text": "1. You think you can escape?"}

        translated = translate_subtitle_track(source, "en-US", FakeGW(), self.config, self.play)
        self.assertEqual(translated.subtitle_data[0]["text"], "You think you can escape?")
        self.assertEqual(translated.subtitle_data[1]["text"], "放我出去")  # 回退原文
        self.assertEqual(translated.subtitle_data[1]["start"],
                         source.subtitle_data[1]["start"])  # 时间戳不偏移

    def test_translate_multiple_entries_numbered(self):
        """多条目编号匹配：跳号/乱序也能对上。"""
        shot2 = Shot.objects.create(scene=self.scene, shot_number=2, dialogue="放我出去")
        Asset.objects.create(play=self.play, episode=self.episode, scene=self.scene,
                             shot=shot2, asset_type=AssetType.AUDIO, status=AssetStatus.READY,
                             metadata={"duration_seconds": 2.0})
        source, _ = generate_subtitle_track(self.episode, "zh-CN")

        class FakeGW:
            def call(self, *a, **k):
                return {"text": "2. Let me go\n1. You think you can escape?"}

        translated = translate_subtitle_track(source, "en-US", FakeGW(), self.config, self.play)
        self.assertEqual(translated.subtitle_data[0]["text"], "You think you can escape?")
        self.assertEqual(translated.subtitle_data[1]["text"], "Let me go")


class SubtitleEditExportTest(BaseDistTest):
    """任务 7.3 字幕编辑/导出。"""

    def test_format_srt(self):
        entries = [{"start": 0, "end": 2, "text": "你好"}]
        srt = format_srt(entries)
        self.assertIn("00:00:00,000 --> 00:00:02,000", srt)
        self.assertIn("你好", srt)

    def test_edit_track_review(self):
        track, _ = generate_subtitle_track(self.episode, "zh-CN")
        track.subtitle_data = [{"start": 0, "end": 1, "text": "改"}]
        track.review_count += 1
        track.status = SubtitleStatus.REVIEW
        track.save()
        track.refresh_from_db()
        self.assertEqual(track.status, SubtitleStatus.REVIEW)
        self.assertEqual(track.review_count, 1)


class PublicationFlowTest(BaseDistTest):
    """任务 7.4 发布工作流状态流转。"""

    def setUp(self):
        super().setUp()
        self.channel = DistributionChannel.objects.create(
            name="抖音", platform="douyin",
            format_requirements={"aspect_ratio": "9:16", "max_duration": 300},
        )
        # 母版媒体元数据满足 9:16 / 300s，渠道版本判定为无需转码
        Asset.objects.create(
            play=self.play, episode=self.episode, asset_type=AssetType.VIDEO,
            status=AssetStatus.READY, artifact_url="https://x/video.mp4",
            metadata={"media": {"width": 1080, "height": 1920, "duration": 60.0,
                                "fps": 30.0, "size_bytes": 1, "bit_rate_kbps": 1}},
        )

    def test_create_publication_links_video(self):
        pub, created = create_publication(self.episode, self.channel)
        self.assertTrue(created)
        self.assertEqual(pub.status, PublicationStatus.PENDING)
        self.assertIsNotNone(pub.video_asset)
        self.assertEqual(pub.video_asset.asset_type, AssetType.VIDEO)

    def test_status_flow_to_published(self):
        pub, _ = create_publication(self.episode, self.channel)
        advance_status(pub, PublicationStatus.UPLOADING)
        mark_reviewing(pub, "vid-123")
        mark_published(pub, "https://douyin.com/v/123")
        pub.refresh_from_db()
        self.assertEqual(pub.status, PublicationStatus.PUBLISHED)
        self.assertEqual(pub.platform_video_id, "vid-123")
        self.assertIsNotNone(pub.published_at)

    def test_status_flow_to_rejected(self):
        pub, _ = create_publication(self.episode, self.channel)
        advance_status(pub, PublicationStatus.UPLOADING)
        mark_reviewing(pub, "vid-456")
        mark_rejected(pub, "内容违规")
        pub.refresh_from_db()
        self.assertEqual(pub.status, PublicationStatus.REJECTED)
        self.assertEqual(pub.reject_reason, "内容违规")

    def test_illegal_transition(self):
        pub, _ = create_publication(self.episode, self.channel)
        with self.assertRaises(ValueError):
            advance_status(pub, PublicationStatus.PUBLISHED)


class AnalyticsTest(BaseDistTest):
    """任务 7.5/7.6 费用分析 + 生产统计。"""

    def test_play_cost_breakdown(self):
        CostRecord.objects.create(
            play=self.play, provider=self.provider, model="m", provider_type="LLM",
            usage={}, unit_cost=Decimal("0.001"), total_cost=Decimal("0.5"),
        )
        result = play_cost_breakdown(self.play)
        self.assertEqual(result["total"], 0.5)
        self.assertIn("LLM", result["by_type"])

    def test_production_stats(self):
        self.play.status = PlayStatus.COMPLETED
        self.play.save(update_fields=["status"])
        stats = production_stats()
        self.assertEqual(stats["total_plays"], 1)
        self.assertEqual(stats["completed"], 1)


class AnalyticsAPITests(APITestCase):
    def setUp(self):
        self.admin = CustomUser.objects.create_user(
            username="ad", password="Passw0rd!123", is_platform_admin=True
        )
        self.client.force_authenticate(self.admin)

    def test_cost_by_provider_requires_admin(self):
        resp = self.client.get("/api/analytics/cost/by-provider/")
        self.assertEqual(resp.status_code, 200)

    def test_publication_summary(self):
        resp = self.client.get("/api/analytics/publications/summary/")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("total", resp.data)
