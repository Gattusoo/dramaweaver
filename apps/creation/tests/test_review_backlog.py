"""code-review 遗留问题回归测试：流水线状态、驳回、合成去重、依赖边、SSRF、取消、工具异常。"""
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.creation import tasks
from apps.creation.assistant.tools import ToolContext, run_tool, validate, ToolError
from apps.creation.models import (
    AgentConfig, Asset, AssetDependency, AssetStatus, AssetType, AuditMode, Episode, EpisodeStatus, Play, PlayStatus, Scene, Shot, TaskRecord, TaskStatus, TaskType,
)
from apps.core.system.models import (
    CustomUser, UserRole,
)
from apps.creation.pipeline import dispatcher
from apps.creation.pipeline.checkpoints import CheckpointConflict, approve, reject
from apps.creation.pipeline.state_machine import summary_play_status
from apps.core.utils.url_safety import UnsafeURLError, check_peer, check_url
from apps.distribution.models import SubtitleSourceType, SubtitleStatus, SubtitleTrack


class _Base(TestCase):
    def setUp(self):
        p = patch("apps.creation.pipeline.dispatcher.send_task", return_value=MagicMock(id="x"))
        self.send = p.start()
        self.addCleanup(p.stop)
        self.owner = CustomUser.objects.create_user(username="o", password="Passw0rd!123",
                                                    role=UserRole.PRODUCER)
        # 计费派发闸接线后渲染类预扣按 60s×单价（约数千分）——补测试余额防闸拒绝
        from apps.billing.services.wallet import grant
        grant(self.owner, 1_000_000, ref_kind="test", ref_id=f"rb-{id(self)}")
        self.config = AgentConfig.objects.create(name="c", created_by=self.owner)
        self.play = Play.objects.create(title="剧", created_by=self.owner, agent_config=self.config,
                                        audit_mode=AuditMode.KEY_CHECKPOINTS)
        self.ep1 = Episode.objects.create(play=self.play, episode_number=1)
        self.ep2 = Episode.objects.create(play=self.play, episode_number=2)
        self.scene = Scene.objects.create(episode=self.ep1, scene_number=1)
        self.shot = Shot.objects.create(scene=self.scene, shot_number=1, dialogue="台词",
                                        visual_prompt="画面")
        # 素材齐备后直接合成所需的 zh-CN 字幕轨（视频合成烧录字幕）；
        # "无字幕轨先派字幕生成、字幕后合成"的编排由 SubtitleOrchestrationTests 覆盖
        SubtitleTrack.objects.create(
            episode=self.ep1, language="zh-CN", source_type=SubtitleSourceType.AUTO,
            status=SubtitleStatus.READY,
            subtitle_data=[{"start": 0, "end": 1.5, "text": "甲"}],
        )

    def asset(self, asset_type, **kw):
        kw.setdefault("status", AssetStatus.READY)
        return Asset.objects.create(play=self.play, asset_type=asset_type, **kw)

    def records(self, task_type):
        return TaskRecord.objects.filter(task_type=task_type).count()

    def materials(self, shot=None, episode=None):
        shot = shot or self.shot
        episode = episode or self.ep1
        img = self.asset(AssetType.IMAGE, episode=episode, scene=shot.scene, shot=shot, artifact_url="/media/a.png")
        aud = self.asset(AssetType.AUDIO, episode=episode, scene=shot.scene, shot=shot, artifact_url="/media/a.mp3")
        return img, aud


class KeyCheckpointFlowTests(_Base):
    """默认审核模式能从大纲一路走到合成：素材审核点不再崩溃。"""

    def test_full_key_checkpoint_flow(self):
        self.play.status = PlayStatus.OUTLINING
        self.play.save()
        self.asset(AssetType.OUTLINE)
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.PAUSED_AT_OUTLINE)
        approve(self.play, "OUTLINE", self.owner)

        # 只有 ep1 剧本就绪：不暂停、也不提前派分镜（等全部就绪）
        self.asset(AssetType.EPISODE_SCRIPT, episode=self.ep1)
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.SCRIPTING)
        self.assertEqual(self.records(TaskType.STORYBOARD), 0)
        self.asset(AssetType.EPISODE_SCRIPT, episode=self.ep2)
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.PAUSED_AT_SCRIPT)
        approve(self.play, "EPISODE_SCRIPT", self.owner)
        self.assertEqual(self.records(TaskType.STORYBOARD), 1)

        # ep1 素材齐：STORYBOARDING -> ASSET_GEN -> PAUSED_AT_ASSETS（此前抛 InvalidTransition）
        self.materials()
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.PAUSED_AT_ASSETS)
        self.assertEqual(self.records(TaskType.VIDEO_COMPOSE), 0)
        approve(self.play, "ASSETS", self.owner)
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.COMPOSING)
        # 只合成素材已齐的 ep1；ep2 还没有镜头
        self.assertEqual(TaskRecord.objects.filter(task_type=TaskType.VIDEO_COMPOSE, episode=self.ep1).count(), 1)
        self.assertEqual(TaskRecord.objects.filter(task_type=TaskType.VIDEO_COMPOSE, episode=self.ep2).count(), 0)
        self.ep1.refresh_from_db()
        self.assertEqual(self.ep1.status, EpisodeStatus.COMPOSING)


class SummaryRegressionTests(_Base):
    def test_unstarted_episode_does_not_drop_play_to_draft(self):
        Play.objects.filter(pk=self.play.pk).update(status=PlayStatus.COMPOSING)
        self.play.refresh_from_db()
        Episode.objects.filter(pk=self.ep1.pk).update(status=EpisodeStatus.COMPLETED)
        summary_play_status(self.play)  # ep2 仍 PENDING
        self.play.refresh_from_db()
        self.assertNotEqual(self.play.status, PlayStatus.DRAFT)


class RejectTests(_Base):
    def test_outline_reject_dispatches_once_and_is_gated(self):
        Play.objects.filter(pk=self.play.pk).update(status=PlayStatus.PAUSED_AT_OUTLINE)
        self.play.refresh_from_db()
        self.asset(AssetType.OUTLINE)
        before = self.records(TaskType.OUTLINE)
        reject(self.play, "OUTLINE", self.owner, "重写")
        self.assertEqual(self.records(TaskType.OUTLINE) - before, 1)
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.OUTLINING)
        with self.assertRaises(CheckpointConflict):
            reject(self.play, "OUTLINE", self.owner, "再驳回")

    def test_reject_rejected_on_completed_play(self):
        Play.objects.filter(pk=self.play.pk).update(status=PlayStatus.COMPLETED)
        self.play.refresh_from_db()
        with self.assertRaises(CheckpointConflict):
            reject(self.play, "ASSETS", self.owner)

    def test_script_reject_then_regenerated_script_does_not_crash(self):
        Play.objects.filter(pk=self.play.pk).update(status=PlayStatus.PAUSED_AT_SCRIPT)
        self.play.refresh_from_db()
        self.asset(AssetType.EPISODE_SCRIPT, episode=self.ep1)
        self.asset(AssetType.EPISODE_SCRIPT, episode=self.ep2)
        reject(self.play, "EPISODE_SCRIPT", self.owner, episode=self.ep1)
        self.asset(AssetType.EPISODE_SCRIPT, episode=self.ep1)  # 重新生成：再暂停应幂等
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.PAUSED_AT_SCRIPT)


class ComposeDedupTests(_Base):
    def setUp(self):
        super().setUp()
        self.play.audit_mode = AuditMode.NONE
        self.play.save(update_fields=["audit_mode"])
        Play.objects.filter(pk=self.play.pk).update(status=PlayStatus.STORYBOARDING)
        self.play.refresh_from_db()

    def test_materials_ready_twice_composes_once(self):
        self.materials()
        dispatcher._after_material_ready(self.play, self.ep1)  # 并发的第二个就绪事件
        self.assertEqual(self.records(TaskType.VIDEO_COMPOSE), 1)

    def test_scene_without_shots_blocks_compose(self):
        Scene.objects.create(episode=self.ep1, scene_number=2)  # 分镜还没出来
        self.materials()
        self.assertEqual(self.records(TaskType.VIDEO_COMPOSE), 0)


class SubtitleOrchestrationTests(_Base):
    """素材齐备但无字幕轨时：先派字幕生成，字幕就绪后再派合成（合成烧录字幕）。"""

    def _no_subtitle(self):
        SubtitleTrack.objects.filter(episode=self.ep1).delete()
        # 关闭审核点，避免素材齐备后在素材审核点暂停、走不到合成编排
        self.play.audit_mode = AuditMode.NONE
        self.play.save(update_fields=["audit_mode"])
        Play.objects.filter(pk=self.play.pk).update(status=PlayStatus.STORYBOARDING)
        self.play.refresh_from_db()

    def test_materials_ready_without_subtitle_dispatches_subtitle_first(self):
        self._no_subtitle()
        self.materials()
        dispatcher._after_material_ready(self.play, self.ep1)
        self.assertEqual(self.records(TaskType.SUBTITLE_GEN), 1)
        self.assertEqual(self.records(TaskType.VIDEO_COMPOSE), 0)

    def test_subtitle_ready_then_compose_dispatched(self):
        self._no_subtitle()
        self.materials()
        dispatcher._after_material_ready(self.play, self.ep1)
        self.assertEqual(self.records(TaskType.VIDEO_COMPOSE), 0)
        # 模拟 generate_subtitle 任务成功：字幕轨就绪后回调继续合成
        SubtitleTrack.objects.create(
            episode=self.ep1, language="zh-CN", source_type=SubtitleSourceType.AUTO,
            status=SubtitleStatus.READY,
            subtitle_data=[{"start": 0, "end": 1, "text": "甲"}],
        )
        dispatcher.maybe_dispatch_video_compose(self.ep1)
        self.assertEqual(self.records(TaskType.VIDEO_COMPOSE), 1)

    def test_subtitle_gen_not_dispatched_twice(self):
        self._no_subtitle()
        self.materials()
        dispatcher._after_material_ready(self.play, self.ep1)
        dispatcher._after_material_ready(self.play, self.ep1)  # 并发的第二个就绪事件
        self.assertEqual(self.records(TaskType.SUBTITLE_GEN), 1)

    def test_send_failure_marks_record_failed(self):
        """broker 不可用（send_task 返回 None）时记录标 FAILED 而非停留 PENDING。"""
        rec = TaskRecord.objects.create(play=self.play, task_type=TaskType.SUBTITLE_GEN)
        self.send.return_value = None
        dispatcher._send_record(rec, "shortplay.tasks.generate_subtitle", {})
        rec.refresh_from_db()
        self.assertEqual(rec.status, TaskStatus.FAILED)
        self.assertIn("broker", rec.error_message)

    def test_failed_send_record_does_not_block_redispatch(self):
        """FAILED（消息未发出）记录不阻塞重派：不再被当作进行中任务。"""
        self._no_subtitle()
        self.materials()
        # 模拟 broker 挂：_send_record 把消息未发出的记录标 FAILED
        TaskRecord.objects.filter(task_type=TaskType.SUBTITLE_GEN).update(status=TaskStatus.FAILED)
        # broker 恢复后重试：FAILED 不阻塞，重新派发
        dispatcher.maybe_dispatch_video_compose(self.ep1)
        self.assertEqual(self.records(TaskType.SUBTITLE_GEN), 2)
        newest = TaskRecord.objects.filter(task_type=TaskType.SUBTITLE_GEN).order_by("-pk").first()
        self.assertEqual(newest.status, TaskStatus.PENDING)


class DependencyEdgeTests(_Base):
    def test_redo_shot_supersedes_composed_video(self):
        img, aud = self.materials()
        video = self.asset(AssetType.VIDEO, episode=self.ep1, artifact_url="/media/v.mp4")
        # 合成任务落的依赖边（这里直接造边，_compose 端到端由 ffmpeg 测试覆盖）
        AssetDependency.objects.create(upstream=img, downstream=video, relation_type="IMAGE_TO_VIDEO")
        img.mark_superseded_recursive()
        video.refresh_from_db()
        self.ep1.refresh_from_db()
        self.assertEqual(video.status, AssetStatus.SUPERSEDED)
        self.assertIsNone(self.ep1.current_video_asset)


class StoryboardRerunTests(_Base):
    def test_rerun_removes_extra_shots_and_stales_changed(self):
        Shot.objects.create(scene=self.scene, shot_number=2, visual_prompt="旧2")
        img, aud = self.materials()
        rec = TaskRecord.objects.create(play=self.play, episode=self.ep1, scene=self.scene,
                                        task_type=TaskType.STORYBOARD)
        gw = MagicMock()
        gw.call.return_value = {"text": '{"shots": [{"shot_number": 1, "visual_prompt": "新画面", "dialogue": "台词"}]}'}
        with patch("apps.creation.tasks._gateway", return_value=gw):
            tasks.generate_storyboard(rec.pk)
        self.assertEqual(list(self.scene.shots.values_list("shot_number", flat=True)), [1])
        img.refresh_from_db()
        aud.refresh_from_db()
        self.assertEqual(img.status, AssetStatus.SUPERSEDED)  # 画面变了
        self.assertEqual(aud.status, AssetStatus.READY)  # 对白没变


class DownloadSafetyTests(TestCase):
    def test_rejects_internal_addresses(self):
        for url in ("http://169.254.169.254/latest/meta-data", "http://127.0.0.1:6379/", "http://10.0.0.5/x",
                    "file:///etc/passwd", "http://[::1]/"):
            with self.assertRaises(UnsafeURLError, msg=url):
                check_url(url)

    @override_settings(SAFE_FETCH_ALLOWED_HOSTS=["minio.internal"])
    def test_allowlisted_host_passes(self):
        self.assertEqual(check_url("http://minio.internal/bucket/a.mp3"), "minio.internal")

    def test_local_path_traversal_rejected(self):
        for url in ("/media/../db.sqlite3", "/media/../../etc/passwd", "D:/secret.txt", "../x"):
            with self.assertRaises(ValueError, msg=url):
                tasks._media_path(url)

    def test_check_peer_rejects_internal_peer(self):
        # DNS rebinding：check_url 预校验后连接落到内网，连接后对端校验应拒绝
        with self.assertRaises(UnsafeURLError):
            check_peer("evil.com", ("169.254.169.254", 80))

    @override_settings(SAFE_FETCH_ALLOWED_HOSTS=["minio.internal"])
    def test_check_peer_allowlist_host_passes(self):
        # 白名单主机（内网 MinIO 等）信任，不做公网强制
        check_peer("minio.internal", ("10.0.0.5", 9000))

    def test_check_peer_public_peer_passes(self):
        check_peer("example.com", ("93.184.216.34", 443))

    def test_check_peer_none_peer_passes(self):
        # 拿不到对端信息时不误伤（交由连接前 check_url 把关）
        check_peer("example.com", None)

    def test_download_rebinds_to_internal_rejected(self):
        """_download 连接后读到内网对端（DNS rebinding）时在读响应体前中断。"""
        stream_mock = MagicMock()
        stream_mock.get_extra_info.return_value = ("10.0.0.5", 80)
        resp = MagicMock()
        resp.is_redirect = False
        resp.extensions = {"network_stream": stream_mock}
        resp.url.host = "evil.com"
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=resp)
        ctx.__exit__ = MagicMock(return_value=False)
        with tempfile.TemporaryDirectory() as tmp:
            with patch("apps.core.utils.url_safety.check_url", return_value="evil.com"), \
                 patch("httpx.stream", return_value=ctx):
                with self.assertRaises(UnsafeURLError):
                    tasks._download("http://evil.com/a.mp3", Path(tmp) / "a.mp3")

    def test_download_public_peer_writes_file(self):
        """正常公网对端不误伤，下载成功。"""
        stream_mock = MagicMock()
        stream_mock.get_extra_info.return_value = ("93.184.216.34", 443)
        resp = MagicMock()
        resp.is_redirect = False
        resp.raise_for_status = MagicMock()
        resp.iter_bytes.return_value = [b"DATA"]
        resp.extensions = {"network_stream": stream_mock}
        resp.url.host = "example.com"
        ctx = MagicMock()
        ctx.__enter__ = MagicMock(return_value=resp)
        ctx.__exit__ = MagicMock(return_value=False)
        with tempfile.TemporaryDirectory() as tmp:
            out = Path(tmp) / "a.mp3"
            with patch("apps.core.utils.url_safety.check_url", return_value="example.com"), \
                 patch("httpx.stream", return_value=ctx):
                tasks._download("http://example.com/a.mp3", out)
            self.assertEqual(out.read_bytes(), b"DATA")


class MediaPlayScopingTests(TestCase):
    """媒体跨剧引用防护：带剧前缀的私有产物只允许本剧引用（回归测试）。"""

    def test_same_play_prefixed_url_passes(self):
        p = tasks._media_path("/media/play7_char_3.png", play_pk=7)
        self.assertEqual(p.name, "play7_char_3.png")

    def test_cross_play_prefixed_url_rejected(self):
        with self.assertRaises(ValueError):
            tasks._media_path("/media/play8_shot_3.png", play_pk=7)

    def test_download_rejects_cross_play_prefixed_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                tasks._download("/media/play9_shot_1.mp3", Path(tmp) / "x.mp3", play_pk=7)

    def test_unprefixed_url_compat_no_ownership_check(self):
        # 历史数据 / 共享素材库（BGM/水印/LUT/INTRO）：无剧前缀，不做剧归属校验
        p = tasks._media_path("/media/bgm_shared.mp3", play_pk=7)
        self.assertEqual(p.name, "bgm_shared.mp3")

    def test_no_play_pk_skips_ownership_check(self):
        # play_pk=None（旧调用方 / 共享资源路径）时不校验剧归属
        p = tasks._media_path("/media/play5_shot_1.png", play_pk=None)
        self.assertEqual(p.name, "play5_shot_1.png")


class CancelTests(TestCase):
    def setUp(self):
        owner = CustomUser.objects.create_user(username="c", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.play = Play.objects.create(title="剧", created_by=owner)

    def test_cancelled_before_start_is_not_revived(self):
        rec = TaskRecord.objects.create(play=self.play, task_type=TaskType.VIDEO_COMPOSE,
                                        status=TaskStatus.CANCELLED)
        work = MagicMock()
        tasks._run(rec.pk, work)
        work.assert_not_called()
        rec.refresh_from_db()
        self.assertEqual(rec.status, TaskStatus.CANCELLED)

    def test_cancel_during_render_stays_cancelled(self):
        rec = TaskRecord.objects.create(play=self.play, task_type=TaskType.VIDEO_COMPOSE)
        tasks._run(rec.pk, MagicMock(side_effect=tasks.RenderCancelled("x")))
        rec.refresh_from_db()
        self.assertEqual(rec.status, TaskStatus.CANCELLED)


class ToolRobustnessTests(TestCase):
    def test_huge_id_is_tool_error_not_keyerror(self):
        with self.assertRaises(ToolError):
            validate({"type": "integer", "minimum": 1}, 10 ** 20)

    def test_unexpected_exception_becomes_error_result(self):
        from apps.creation.assistant import tools as T
        T._TOOLS["_boom"] = {"name": "_boom", "description": "", "handler": MagicMock(side_effect=RuntimeError("db")),
                            "input_schema": {"type": "object", "properties": {}}}
        self.addCleanup(T._TOOLS.pop, "_boom")
        result, is_error = run_tool(ToolContext.__new__(ToolContext), "_boom", {})
        self.assertTrue(is_error)
        self.assertNotIn("db", result["error"])  # 不把异常原文给模型


class ShotStrTests(TestCase):
    def test_shot_str(self):
        owner = CustomUser.objects.create_user(username="s", password="Passw0rd!123")
        ep = Episode.objects.create(play=Play.objects.create(title="剧", created_by=owner), episode_number=1)
        shot = Shot.objects.create(scene=Scene.objects.create(episode=ep, scene_number=3), shot_number=2)
        self.assertEqual(str(shot), "Scene 3 - Shot 2")
