"""Pipeline Engine 验证测试（openspec change: backend-pipeline-engine）。"""
from unittest.mock import MagicMock, patch

from django.test import TestCase

from apps.creation.models import (
    AgentConfig, Asset, AssetDependency, AssetStatus, AssetType, AuditMode, Episode,
    EpisodeStatus, MemberRole, MemberStatus, Play, PlayMember, PlayStatus,
    RedoGranularity, RelationType, Scene, Shot,
)
from apps.ai_gateway.models import (
    AIModel, AIProvider,
)
from apps.core.system.models import (
    CustomUser, UserRole,
)
from apps.creation.pipeline.checkpoints import approve, reject, should_pause
from apps.creation.pipeline.redo import redo
from apps.creation.pipeline.state_machine import (
    InvalidTransition, summary_play_status, transition, validate_transition,
)


def _mock_send_task(*args, **kwargs):
    return MagicMock(id="celery-123")


class BasePipelineTest(TestCase):
    def setUp(self):
        self.producer = CustomUser.objects.create_user(
            username="pp", password="Passw0rd!123", role=UserRole.PRODUCER
        )
        self.provider = AIProvider.objects.create(
            name="LLM", provider_type="LLM", adapter_type="deepseek",
            base_url="https://x", api_key_encrypted="", priority=1,
        )
        self.model = AIModel.objects.create(
            provider=self.provider, model_name="m", display_name="M", model_type="LLM",
        )
        self.config = AgentConfig.objects.create(
            name="配方", llm_model=self.model, created_by=self.producer,
        )
        self.play = Play.objects.create(
            title="短剧", total_episodes=2, agent_config=self.config,
            created_by=self.producer, audit_mode=AuditMode.NONE,
        )
        PlayMember.objects.create(
            play=self.play, user=self.producer, role=MemberRole.OWNER,
            status=MemberStatus.ACTIVE,
        )
        self.ep1 = Episode.objects.create(play=self.play, episode_number=1)
        self.ep2 = Episode.objects.create(play=self.play, episode_number=2)
        self.scene = Scene.objects.create(episode=self.ep1, scene_number=1)
        self.shot = Shot.objects.create(scene=self.scene, shot_number=1)

    def make_asset(self, asset_type, *, shot=None, episode=None, status=AssetStatus.READY):
        return Asset.objects.create(
            play=self.play, episode=episode, scene=self.scene,
            shot=shot, asset_type=asset_type, status=status,
        )


class StateMachineTests(BasePipelineTest):
    """任务 7.1 状态机合法/非法转换。"""

    def test_legal_play_transition(self):
        self.play.status = PlayStatus.OUTLINING
        transition(self.play, PlayStatus.SCRIPTING)
        self.assertEqual(self.play.status, PlayStatus.SCRIPTING)

    def test_illegal_play_transition(self):
        self.play.status = PlayStatus.OUTLINING
        with self.assertRaises(InvalidTransition):
            validate_transition(self.play, PlayStatus.DRAFT)

    def test_illegal_episode_transition(self):
        self.ep1.status = EpisodeStatus.PENDING
        with self.assertRaises(InvalidTransition):
            validate_transition(self.ep1, EpisodeStatus.COMPLETED)

    def test_play_summary_all_completed(self):
        self.ep1.status = EpisodeStatus.COMPLETED
        self.ep1.save()
        self.ep2.status = EpisodeStatus.COMPLETED
        self.ep2.save()
        summary_play_status(self.play)
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.COMPLETED)


class DispatcherTests(BasePipelineTest):
    """任务 7.2 Task Dispatcher 事件派发。"""

    @patch("apps.creation.pipeline.dispatcher.send_task", side_effect=_mock_send_task)
    def test_play_outlining_dispatches_outline(self, mock_send):
        self.play.status = PlayStatus.OUTLINING
        self.play.save()
        names = [c.args[0] for c in mock_send.call_args_list]
        self.assertIn("shortplay.tasks.generate_outline", names)

    @patch("apps.creation.pipeline.dispatcher.send_task", side_effect=_mock_send_task)
    def test_outline_ready_no_pause_dispatches_scripts(self, mock_send):
        self.play.status = PlayStatus.OUTLINING
        self.play.save()
        self.make_asset(AssetType.OUTLINE)  # READY -> dispatch episode_script x2
        names = [c.args[0] for c in mock_send.call_args_list]
        self.assertEqual(names.count("shortplay.tasks.generate_episode_script"), 2)

    @patch("apps.creation.pipeline.dispatcher.send_task", side_effect=_mock_send_task)
    def test_storyboard_ready_dispatches_image_tts(self, mock_send):
        # 让 shot 有 storyboard asset READY
        Asset.objects.create(
            play=self.play, episode=self.ep1, scene=self.scene, shot=self.shot,
            asset_type=AssetType.STORYBOARD, status=AssetStatus.READY,
        )
        names = [c.args[0] for c in mock_send.call_args_list]
        self.assertIn("shortplay.tasks.generate_image", names)
        self.assertIn("shortplay.tasks.generate_tts", names)


class VersionManagementTests(BasePipelineTest):
    """任务 7.3 版本管理。"""

    @patch("apps.creation.pipeline.dispatcher.send_task", side_effect=_mock_send_task)
    def test_new_version_supersedes_old(self, mock_send):
        v1 = Asset.objects.create(
            play=self.play, episode=self.ep1, scene=self.scene, shot=self.shot,
            asset_type=AssetType.IMAGE, status=AssetStatus.READY,
        )
        self.assertEqual(v1.version, 1)
        v2 = Asset.objects.create(
            play=self.play, episode=self.ep1, scene=self.scene, shot=self.shot,
            asset_type=AssetType.IMAGE, status=AssetStatus.READY,
        )
        self.assertEqual(v2.version, 2)
        v1.refresh_from_db()
        self.assertEqual(v1.status, AssetStatus.SUPERSEDED)
        self.shot.refresh_from_db()
        self.assertEqual(self.shot.current_image_asset, v2)


class CascadeTests(BasePipelineTest):
    """任务 7.4 级联失效。"""

    @patch("apps.creation.pipeline.dispatcher.send_task", side_effect=_mock_send_task)
    def test_cascade_supersedes_downstream(self, mock_send):
        img = Asset.objects.create(
            play=self.play, episode=self.ep1, scene=self.scene, shot=self.shot,
            asset_type=AssetType.IMAGE, status=AssetStatus.READY,
        )
        video = Asset.objects.create(
            play=self.play, episode=self.ep1, scene=self.scene, shot=self.shot,
            asset_type=AssetType.VIDEO, status=AssetStatus.READY,
        )
        AssetDependency.objects.create(
            upstream=img, downstream=video, relation_type=RelationType.IMAGE_TO_VIDEO,
        )
        # 新版本就绪 -> 旧 img SUPERSEDED -> video 级联失效
        Asset.objects.create(
            play=self.play, episode=self.ep1, scene=self.scene, shot=self.shot,
            asset_type=AssetType.IMAGE, status=AssetStatus.READY,
        )
        video.refresh_from_db()
        self.assertEqual(video.status, AssetStatus.SUPERSEDED)


class AuditModeTests(BasePipelineTest):
    """任务 7.5 三种审核模式。"""

    @patch("apps.creation.pipeline.dispatcher.send_task", side_effect=_mock_send_task)
    def test_none_no_pause(self, mock_send):
        self.play.status = PlayStatus.OUTLINING
        self.play.save()
        self.make_asset(AssetType.OUTLINE)
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.SCRIPTING)

    @patch("apps.creation.pipeline.dispatcher.send_task", side_effect=_mock_send_task)
    def test_key_checkpoints_pause_then_approve(self, mock_send):
        self.play.audit_mode = AuditMode.KEY_CHECKPOINTS
        self.play.save()
        self.play.status = PlayStatus.OUTLINING
        self.play.save()
        self.make_asset(AssetType.OUTLINE)
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.PAUSED_AT_OUTLINE)
        # 审核通过 -> 恢复派发 episode_script
        approve(self.play, "OUTLINE", self.producer)
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.SCRIPTING)
        names = [c.args[0] for c in mock_send.call_args_list]
        self.assertIn("shortplay.tasks.generate_episode_script", names)

    @patch("apps.creation.pipeline.dispatcher.send_task", side_effect=_mock_send_task)
    def test_every_step_pauses(self, mock_send):
        self.play.audit_mode = AuditMode.EVERY_STEP
        self.play.save()
        self.play.status = PlayStatus.OUTLINING
        self.play.save()
        self.make_asset(AssetType.OUTLINE)
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.PAUSED_AT_OUTLINE)

    @patch("apps.creation.pipeline.dispatcher.send_task", side_effect=_mock_send_task)
    def test_reject_supersedes_and_redispatches(self, mock_send):
        self.play.audit_mode = AuditMode.KEY_CHECKPOINTS
        self.play.save()
        outline = self.make_asset(AssetType.OUTLINE)
        reject(self.play, "OUTLINE", self.producer, "不行")
        outline.refresh_from_db()
        self.assertEqual(outline.status, AssetStatus.SUPERSEDED)
        names = [c.args[0] for c in mock_send.call_args_list]
        self.assertIn("shortplay.tasks.generate_outline", names)


class RedoTests(BasePipelineTest):
    """任务 7.6 三种重做粒度。"""

    @patch("apps.creation.pipeline.dispatcher.send_task", side_effect=_mock_send_task)
    def test_redo_shot(self, mock_send):
        Asset.objects.create(
            play=self.play, episode=self.ep1, scene=self.scene, shot=self.shot,
            asset_type=AssetType.IMAGE, status=AssetStatus.READY,
        )
        redo(self.play, RedoGranularity.SHOT, self.shot.pk)
        names = [c.args[0] for c in mock_send.call_args_list]
        self.assertIn("shortplay.tasks.generate_image", names)
        self.assertIn("shortplay.tasks.generate_tts", names)

    @patch("apps.creation.pipeline.dispatcher.send_task", side_effect=_mock_send_task)
    def test_redo_episode(self, mock_send):
        Asset.objects.create(
            play=self.play, episode=self.ep1, scene=self.scene, shot=self.shot,
            asset_type=AssetType.IMAGE, status=AssetStatus.READY,
        )
        redo(self.play, RedoGranularity.EPISODE, self.ep1.pk)
        self.ep1.refresh_from_db()
        self.assertEqual(self.ep1.status, EpisodeStatus.SCRIPTING)
        names = [c.args[0] for c in mock_send.call_args_list]
        self.assertIn("shortplay.tasks.generate_episode_script", names)
