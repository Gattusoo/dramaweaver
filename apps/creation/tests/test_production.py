"""生产链路验证测试（openspec change: backend-production-steps）。"""
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.ai_gateway.models import (
    AIModel, AIProvider,
)
from apps.creation.models import (
    AgentConfig, Asset, AssetStatus, AssetType, AssetLibrary, AuditMode, Character, Episode, EpisodeStatus, Play, PlayStatus, Scene, Shot, TaskRecord, TaskStatus, TaskType, VoiceProfile,
)
from apps.core.system.models import (
    CustomUser, UserRole,
)
from apps.distribution.models import (
    DistributionChannel, SubtitleSourceType, SubtitleStatus, SubtitleTrack,
)
from apps.creation.tasks import (
    compose_video, generate_character_ref, generate_episode_script,
    generate_image, generate_outline, generate_storyboard, generate_subtitle, generate_tts,
)


class FakeGateway:
    """模拟 AIGateway，按 provider_type 返回固定结果。"""

    def __init__(self, results=None):
        self.results = results or {}
        self.calls = []

    def call(self, provider_type, config, **payload):
        self.calls.append((provider_type, payload))
        result = {
            "text": "{}", "usage": {"input_tokens": 100, "output_tokens": 50},
            "model": "m", "url": "https://example.com/img.png", "image_bytes": b"IMG",
            "audio_bytes": b"AUDIO", "duration_seconds": 2.0, "cost": 0,
        }
        result.update(self.results.get(provider_type, {}))
        return result


class BaseProdTest(TestCase):
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
            prompt_templates={"outline_system": "你是编剧", "episode_script_system": "你是编剧"},
        )
        self.play = Play.objects.create(
            title="短剧", total_episodes=1, agent_config=self.config,
            created_by=self.producer, audit_mode=AuditMode.NONE,
        )
        self.episode = Episode.objects.create(play=self.play, episode_number=1)
        self.scene = Scene.objects.create(episode=self.episode, scene_number=1)
        self.character = Character.objects.create(
            play=self.play, name="霸总", role="PROTAGONIST", appearance_prompt="西装霸总",
        )
        self.shot = Shot.objects.create(scene=self.scene, shot_number=1, visual_prompt="近景")

    def record(self, task_type, **kw):
        return TaskRecord.objects.create(play=self.play, task_type=task_type, **kw)


class OutlineTest(BaseProdTest):
    def test_generate_outline_creates_asset(self):
        with patch("apps.creation.tasks._gateway", return_value=FakeGateway()) as gw:
            r = self.record(TaskType.OUTLINE)
            with patch("apps.creation.pipeline.dispatcher.send_task", return_value=MagicMock(id="x")):
                generate_outline(r.pk)
        asset = Asset.objects.get(play=self.play, asset_type=AssetType.OUTLINE)
        self.assertEqual(asset.status, AssetStatus.READY)
        r.refresh_from_db()
        self.assertEqual(r.status, TaskStatus.SUCCESS)


class EpisodeScriptTest(BaseProdTest):
    def test_episode_script_creates_scenes(self):
        gw = FakeGateway({
            "LLM": {"text": '{"scenes": [{"scene_number": 1, "description": "办公室", "mood": "紧张"}, '
                             '{"scene_number": 2, "description": "天台"}]}'}
        })
        with patch("apps.creation.tasks._gateway", return_value=gw):
            r = self.record(TaskType.EPISODE_SCRIPT, episode=self.episode)
            generate_episode_script(r.pk)
        self.assertEqual(Scene.objects.filter(episode=self.episode).count(), 2)
        asset = Asset.objects.get(play=self.play, episode=self.episode,
                                  asset_type=AssetType.EPISODE_SCRIPT)
        self.assertEqual(asset.status, AssetStatus.READY)


class CharacterRefTest(BaseProdTest):
    def test_character_ref_updates_fk(self):
        gw = FakeGateway({"IMAGE": {"url": "https://x/ref.png", "image_bytes": b"X"}})
        with patch("apps.creation.tasks._gateway", return_value=gw):
            r = self.record(TaskType.CHARACTER_REF, input_summary={"character": self.character.pk})
            generate_character_ref(r.pk)
        asset = Asset.objects.get(play=self.play, character=self.character,
                                  asset_type=AssetType.CHARACTER_REF)
        self.character.refresh_from_db()
        self.assertEqual(self.character.reference_image, asset)
        self.assertEqual(asset.status, AssetStatus.READY)


class StoryboardTest(BaseProdTest):
    def test_storyboard_creates_shots(self):
        gw = FakeGateway({
            "LLM": {"text": '{"shots": [{"shot_number": 1, "shot_type": "CLOSE_UP", '
                            '"visual_prompt": "近景", "dialogue": "你逃不了"}]}'}
        })
        with patch("apps.creation.tasks._gateway", return_value=gw):
            r = self.record(TaskType.STORYBOARD, episode=self.episode, scene=self.scene)
            generate_storyboard(r.pk)
        self.assertEqual(Shot.objects.filter(scene=self.scene).count(), 1)
        asset = Asset.objects.filter(asset_type=AssetType.STORYBOARD).first()
        self.assertEqual(asset.status, AssetStatus.READY)

    def test_dialogue_change_supersedes_auto_subtitle(self):
        """对白变化的 redo 应作废该集自动字幕，避免旧字幕烧进新视频。"""
        Shot.objects.filter(pk=self.shot.pk).update(dialogue="旧台词")
        SubtitleTrack.objects.create(
            episode=self.episode, language="zh-CN", source_type=SubtitleSourceType.AUTO,
            status=SubtitleStatus.READY,
            subtitle_data=[{"start": 0, "end": 1, "text": "旧台词"}],
        )
        gw = FakeGateway({
            "LLM": {"text": '{"shots": [{"shot_number": 1, "shot_type": "CLOSE_UP", '
                            '"visual_prompt": "近景", "dialogue": "新台词"}]}'}
        })
        with patch("apps.creation.tasks._gateway", return_value=gw):
            r = self.record(TaskType.STORYBOARD, episode=self.episode, scene=self.scene)
            generate_storyboard(r.pk)
        track = SubtitleTrack.objects.get(episode=self.episode, language="zh-CN")
        self.assertEqual(track.status, SubtitleStatus.SUPERSEDED)


class SubtitleTest(BaseProdTest):
    def test_generate_subtitle_rolls_back_track_on_callback_failure(self):
        """合成编排回调失败时回滚新建字幕轨，避免 READY 字幕但无合成派发的 stall。"""
        with patch("apps.creation.pipeline.dispatcher.maybe_dispatch_video_compose",
                   side_effect=RuntimeError("boom")):
            r = self.record(TaskType.SUBTITLE_GEN, episode=self.episode)
            with self.assertRaises(RuntimeError):
                generate_subtitle(r.pk)
        self.assertFalse(SubtitleTrack.objects.filter(
            episode=self.episode, language="zh-CN").exists())


class ImageTest(BaseProdTest):
    def test_generate_image_uses_reference(self):
        ref = Asset.objects.create(
            play=self.play, character=self.character, asset_type=AssetType.CHARACTER_REF,
            status=AssetStatus.READY, artifact_url="https://x/ref.png",
        )
        self.character.reference_image = ref
        self.character.save(update_fields=["reference_image"])
        # 关联对白角色，任务才能读取其参考图
        self.shot.dialogue_character = self.character
        self.shot.save(update_fields=["dialogue_character"])
        gw = FakeGateway({"IMAGE": {"url": "https://x/img.png", "image_bytes": b"I"}})
        with patch("apps.creation.tasks._gateway", return_value=gw):
            r = self.record(TaskType.IMAGE_GEN, episode=self.episode, scene=self.scene, shot=self.shot)
            generate_image(r.pk)
        asset = Asset.objects.get(play=self.play, shot=self.shot, asset_type=AssetType.IMAGE)
        self.assertEqual(asset.status, AssetStatus.READY)
        _, payload = gw.calls[0]
        self.assertIn("https://x/ref.png", payload.get("reference_images", []))


class TTSTest(BaseProdTest):
    def test_generate_tts_skips_empty_dialogue(self):
        self.shot.dialogue = ""
        self.shot.save(update_fields=["dialogue"])
        r = self.record(TaskType.TTS_GEN, episode=self.episode, scene=self.scene, shot=self.shot)
        with patch("apps.creation.tasks._gateway", return_value=FakeGateway()):
            generate_tts(r.pk)
        self.assertFalse(Asset.objects.filter(asset_type=AssetType.AUDIO).exists())
        r.refresh_from_db()
        self.assertEqual(r.status, TaskStatus.SUCCESS)


class VideoComposeTest(BaseProdTest):
    def setUp(self):
        super().setUp()
        self.shot.duration_estimate = 5
        self.shot.save(update_fields=["duration_estimate"])

    def test_compose_video_creates_video_asset(self):
        from pathlib import Path as P
        Asset.objects.create(
            play=self.play, episode=self.episode, scene=self.scene, shot=self.shot,
            asset_type=AssetType.IMAGE, status=AssetStatus.READY,
            artifact_url="https://x/img.png",
        )

        def fake_ffmpeg(args, timeout=600, progress_cb=None, cancel_check=None):
            # 单次编码：最后一个参数为输出路径，写伪文件（视频/封面均如此）
            last = args[-1]
            P(last).parent.mkdir(parents=True, exist_ok=True)
            P(last).write_bytes(b"DUMMYMP4")

        r = self.record(TaskType.VIDEO_COMPOSE, episode=self.episode)
        with patch("apps.creation.tasks.run_ffmpeg", side_effect=fake_ffmpeg), \
             patch("apps.creation.tasks._probe_duration", return_value=5.0), \
             patch("apps.creation.tasks._decode_check", return_value=None), \
             patch("apps.creation.tasks._download", return_value="fake.jpg"):
            compose_video(r.pk)
        asset = Asset.objects.get(play=self.play, episode=self.episode, asset_type=AssetType.VIDEO)
        self.assertEqual(asset.status, AssetStatus.READY)
        self.assertEqual(asset.metadata["clip_count"], 1)
        self.episode.refresh_from_db()
        self.assertEqual(self.episode.duration_seconds, 5)
        self.assertTrue(self.episode.poster_url)
        self.assertEqual(self.episode.status, EpisodeStatus.COMPLETED)

    def test_compose_video_no_shots_raises(self):
        r = self.record(TaskType.VIDEO_COMPOSE, episode=self.episode)
        from apps.creation.models import Shot as ShotModel
        ShotModel.objects.all().delete()
        with self.assertRaises(ValueError):
            compose_video(r.pk)
        r.refresh_from_db()
        self.assertEqual(r.status, TaskStatus.FAILED)

    def test_compose_video_subtitle_burn(self):
        from pathlib import Path as P
        Asset.objects.create(
            play=self.play, episode=self.episode, scene=self.scene, shot=self.shot,
            asset_type=AssetType.IMAGE, status=AssetStatus.READY,
            artifact_url="https://x/img.png",
        )
        self.shot.dialogue = "你逃不了"
        self.shot.save(update_fields=["dialogue"])
        SubtitleTrack.objects.create(
            episode=self.episode, language="zh-CN", source_type=SubtitleSourceType.AUTO,
            status=SubtitleStatus.READY,
            subtitle_data=[{"start": 0.0, "end": 2.0, "text": "你逃不了"}],
        )

        calls = []
        scripts = []

        def fake_ffmpeg(args, timeout=600, progress_cb=None, cancel_check=None):
            calls.append(list(args))
            opt = next((o for o in ("-/filter_complex", "-filter_complex_script") if o in args), None)
            if opt:
                p = args[args.index(opt) + 1]
                scripts.append(P(p).read_text(encoding="utf-8"))
            P(args[-1]).parent.mkdir(parents=True, exist_ok=True)
            P(args[-1]).write_bytes(b"DUMMYMP4")

        r = self.record(TaskType.VIDEO_COMPOSE, episode=self.episode)
        with patch("apps.creation.tasks.run_ffmpeg", side_effect=fake_ffmpeg), \
             patch("apps.creation.tasks._probe_duration", return_value=5.0), \
             patch("apps.creation.tasks._decode_check", return_value=None), \
             patch("apps.creation.tasks._download", return_value="fake.jpg"):
            compose_video(r.pk)
        # 主编码脚本应含 subtitles 烧录
        self.assertTrue(scripts, "未捕获 filtergraph 脚本文件参数")
        self.assertIn("subtitles=", scripts[0])
        self.assertTrue(Asset.objects.filter(asset_type=AssetType.VIDEO, status=AssetStatus.READY).exists())
