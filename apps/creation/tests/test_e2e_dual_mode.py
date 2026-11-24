"""双模式端到端验收（工作台 Stage D2 任务 9.2）。

同一 FakeGateway 下，local（本地 worker 领取执行）与 shared（直调任务函数）
两条路径各自独立建剧跑通 大纲→剧本→分镜→素材→导出 全链，断言两种模式
的产物契约一致；付费崩溃点需确认（既有套件）、重复生成复用资产。
"""
import io
import json
import shutil
import tempfile

from django.core.management import call_command
from django.test import TestCase, override_settings
from unittest.mock import MagicMock, patch

from apps.ai_gateway.models import AIModel, AIProvider
from apps.creation.models import (
    AgentConfig, Asset, AssetStatus, AssetType, AuditMode, Episode, Play, PlayStatus,
    Scene, TaskRecord, TaskStatus, TaskType,
)
from apps.creation.tests.test_production import FakeGateway
from apps.creation.workspace.export_service import build_export_zip
from apps.creation.workspace.import_precheck import precheck_import
from apps.creation.workspace.import_service import confirm_import
from apps.core.system.models import CustomUser, UserRole


def _gateway():
    """全链 FakeGateway：大纲/剧本/分镜文本 + 图片/配音字节。"""
    return FakeGateway({
        "LLM": {"text": json.dumps({
            "outline": "三幕大纲",
            "scenes": [{"scene_number": 1, "description": "办公室", "mood": "紧张"}],
            "shots": [{"shot_number": 1, "visual_prompt": "近景", "dialogue": "台词",
                       "shot_type": "MEDIUM"}],
        })},
        "IMAGE": {"image_bytes": b"E2E-IMG", "url": ""},
        "TTS": {"audio_bytes": b"E2E-AUDIO", "url": ""},
    })


class _media_env:
    """E2E 媒体环境：跳过 ffprobe 解码（假字节），保留真实内容寻址存储。

    与 test_generation_spec 同口径——媒体校验单元化覆盖，E2E 关注链路契约。
    """

    def __enter__(self):
        from contextlib import ExitStack
        from apps.creation import tasks as t
        self._stack = ExitStack()
        self._stack.enter_context(patch.object(
            t, "_validate_media_bytes",
            return_value={"duration": 2.0, "has_audio": True,
                          "width": 1080, "height": 1920, "vcodec": "h264"}))
        return self

    def __exit__(self, *exc):
        self._stack.close()


class DualModeE2ETests(TestCase):
    """任务 9.2：创建→剧本→分镜→素材→导出 + 重启 worker 恢复。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_tmp = tempfile.mkdtemp(prefix="e2e-media-")
        from django.conf import settings
        cls._orig_media = settings.MEDIA_ROOT
        settings.MEDIA_ROOT = cls._media_tmp

    @classmethod
    def tearDownClass(cls):
        from django.conf import settings
        settings.MEDIA_ROOT = cls._orig_media
        shutil.rmtree(cls._media_tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.user = CustomUser.objects.create_user(
            username="e2e", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.provider = AIProvider.objects.create(
            name="P", provider_type="LLM", adapter_type="deepseek",
            base_url="https://x", api_key_encrypted="", priority=1)
        self.model = AIModel.objects.create(
            provider=self.provider, model_name="m", display_name="M", model_type="LLM")
        self.config = AgentConfig.objects.create(
            name="配方", llm_model=self.model, created_by=self.user,
            prompt_templates={"outline_system": "你是编剧",
                              "episode_script_system": "你是编剧"})

    def _mk_play(self):
        play = Play.objects.create(
            title="E2E 剧", total_episodes=1, agent_config=self.config,
            created_by=self.user, audit_mode=AuditMode.NONE,
            gen_params={"aspect_ratio": "9:16"})
        Episode.objects.create(play=play, episode_number=1)
        return play

    def _mk_shot(self, play):
        """为 IMAGE/TTS 准备镜头（完整链由分镜任务生成；直发素材需要现成镜头）。"""
        from apps.creation.models import Shot
        episode = play.episodes.first()
        scene = Scene.objects.create(episode=episode, scene_number=1)
        return Shot.objects.create(scene=scene, shot_number=1,
                                    visual_prompt="近景", dialogue="台词")

    def _contract(self, play):
        """产物契约：类型集合/内容摘要/任务状态/结构——模式间可比。"""
        ready = Asset.objects.filter(play=play, status=AssetStatus.READY)
        return {
            "types": sorted(ready.values_list("asset_type", flat=True)),
            "digests": sorted(a.content_sha256 for a in ready if a.content_sha256),
            "task_status": sorted(set(TaskRecord.objects.filter(
                play=play).values_list("status", flat=True))),
            "scene_count": Scene.objects.filter(episode__play=play).count(),
        }

    def _run_shared(self):
        """shared：直调任务函数（Celery worker 语义），独立建剧。"""
        from apps.creation import tasks as t
        from apps.creation.pipeline import dispatcher
        play = self._mk_play()
        episode = play.episodes.first()
        gw = _gateway()
        with _media_env(), patch.object(t, "_gateway", return_value=gw), \
             patch("apps.creation.pipeline.dispatcher.send_task",
                   return_value=MagicMock(id="x")):
            for task_type, func in (
                (TaskType.OUTLINE, t.generate_outline),
                (TaskType.EPISODE_SCRIPT, t.generate_episode_script),
            ):
                record = dispatcher.dispatch_task(
                    task_type, play=play, episode=episode)
                func(record.pk)
            # 素材阶段：剧本产物创建场景；分镜逐场景；图片/配音逐镜头
            for scene in Scene.objects.filter(episode=episode).order_by("pk"):
                record = dispatcher.dispatch_task(
                    TaskType.STORYBOARD, play=play, episode=episode, scene=scene)
                t.generate_storyboard(record.pk)
            from apps.creation.models import Shot
            for shot in Shot.objects.filter(scene__episode=episode).order_by("pk"):
                for task_type, func in ((TaskType.IMAGE_GEN, t.generate_image),
                                        (TaskType.TTS_GEN, t.generate_tts)):
                    record = dispatcher.dispatch_task(
                        task_type, play=play, episode=episode, scene=shot.scene,
                        shot=shot)
                    func(record.pk)
        return play, self._contract(play)

    def _drain_worker(self, rounds=12):
        """把队列拉空：worker 反复领取直到无任务可执行（覆盖事件级联派发）。"""
        for _ in range(rounds):
            call_command("run_local_worker", "--once", "--max-tasks", "1",
                         stdout=io.StringIO())
            from apps.creation.models import LocalTaskExecution
            if not LocalTaskExecution.objects.filter(
                    claim_status="PENDING").exists():
                break

    @override_settings(CREATION_TASK_BACKEND="local")
    def _run_local(self):
        """local：dispatch → 本地执行记录 → worker 逐个领取执行，独立建剧。"""
        from apps.creation import tasks as t
        from apps.creation.pipeline import dispatcher
        play = self._mk_play()
        episode = play.episodes.first()
        gw = _gateway()
        with _media_env(), patch.object(t, "_gateway", return_value=gw):
            for task_type in (TaskType.OUTLINE, TaskType.EPISODE_SCRIPT):
                dispatcher.dispatch_task(task_type, play=play, episode=episode)
                self._drain_worker()
            for scene in Scene.objects.filter(episode=episode).order_by("pk"):
                dispatcher.dispatch_task(TaskType.STORYBOARD, play=play,
                                         episode=episode, scene=scene)
                self._drain_worker()
            from apps.creation.models import Shot
            for shot in Shot.objects.filter(scene__episode=episode).order_by("pk"):
                for task_type in (TaskType.IMAGE_GEN, TaskType.TTS_GEN):
                    dispatcher.dispatch_task(task_type, play=play, episode=episode,
                                             scene=shot.scene, shot=shot)
                    self._drain_worker()
        return play, self._contract(play)

    def test_dual_mode_parity(self):
        """同一 FakeGateway 下两种模式产物契约一致。"""
        _, shared = self._run_shared()
        _, local = self._run_local()
        self.assertEqual(shared["types"], local["types"])
        self.assertEqual(shared["digests"], local["digests"])
        self.assertEqual(shared["scene_count"], local["scene_count"])
        # 全链无失败：可能有级联派发留下的未执行重复记录（PENDING），
        # 但绝不能有 FAILED / RECOVERY_REQUIRED
        for statuses in (shared["task_status"], local["task_status"]):
            for st in statuses:
                self.assertNotIn(st, (TaskStatus.FAILED, TaskStatus.RECOVERY_REQUIRED,
                                      TaskStatus.CANCELLED))

    @override_settings(CREATION_TASK_BACKEND="local")
    def test_worker_restart_recovers_pending(self):
        """重启 worker 后恢复：API 派发 → worker 未跑 → 重启领取完成。"""
        from apps.creation import tasks as t
        from apps.creation.pipeline import dispatcher
        play = self._mk_play()
        episode = play.episodes.first()
        with _media_env(), patch.object(t, "_gateway", return_value=_gateway()):
            # 模拟"重启"：先派发（API 存活、worker 不在）
            dispatcher.dispatch_task(TaskType.OUTLINE, play=play, episode=episode)
            from apps.creation.models import LocalTaskExecution
            self.assertEqual(LocalTaskExecution.objects.filter(
                record__play=play, claim_status="PENDING").count(), 1)
            # worker 启动 → 领取并完成
            call_command("run_local_worker", "--once", stdout=io.StringIO())
        # 事件驱动：outline READY 会级联派发后续任务——本批只执行了 outline
        records = TaskRecord.objects.filter(play=play, task_type=TaskType.OUTLINE)
        self.assertTrue(records.exists())
        self.assertTrue(all(r.status == TaskStatus.SUCCESS for r in records))
        record = records.order_by("pk").first()
        self.assertTrue(Asset.objects.filter(
            play=play, asset_type=AssetType.OUTLINE, status=AssetStatus.READY).exists())

    @override_settings(CREATION_TASK_BACKEND="local")
    def test_repeat_generation_reuses_asset(self):
        """普通重复生成复用资产：同指纹 READY 不重复调用模型（不制造新版本）。"""
        from apps.creation import tasks as t
        from apps.creation.pipeline import dispatcher
        from apps.creation.models import LocalTaskExecution
        play = self._mk_play()
        shot = self._mk_shot(play)
        gw = _gateway()
        with _media_env(), patch.object(t, "_gateway", return_value=gw):
            dispatcher.dispatch_task(TaskType.IMAGE_GEN, play=play,
                                     episode=shot.scene.episode, scene=shot.scene,
                                     shot=shot)
            call_command("run_local_worker", "--once", "--max-tasks", "1",
                         stdout=io.StringIO())
            first_calls = len(gw.calls)
            # 同输入再次派发执行：指纹命中复用，网关不再被调
            dispatcher.dispatch_task(TaskType.IMAGE_GEN, play=play,
                                      episode=shot.scene.episode, scene=shot.scene,
                                      shot=shot)
            call_command("run_local_worker", "--once", "--max-tasks", "1",
                         stdout=io.StringIO())
            self.assertEqual(len(gw.calls), first_calls)  # 无新模型调用
        versions = Asset.objects.filter(play=play, asset_type=AssetType.IMAGE,
                                        status=AssetStatus.READY).count()
        self.assertEqual(versions, 1)  # 仍只有一个 READY 版本

    @override_settings(CREATION_TASK_BACKEND="local")
    def test_export_after_local_pipeline(self):
        """local 全链产物可导出为 ZIP（换目录恢复的输入侧）。"""
        from apps.creation import tasks as t
        from apps.creation.pipeline import dispatcher
        play = self._mk_play()
        shot = self._mk_shot(play)
        with _media_env(), patch.object(t, "_gateway", return_value=_gateway()):
            dispatcher.dispatch_task(TaskType.OUTLINE, play=play,
                                     episode=shot.scene.episode)
            self._drain_worker()
            dispatcher.dispatch_task(TaskType.IMAGE_GEN, play=play,
                                     episode=shot.scene.episode, scene=shot.scene,
                                     shot=shot)
            self._drain_worker()
        self.assertTrue(Asset.objects.filter(
            play=play, asset_type=AssetType.IMAGE, status=AssetStatus.READY
        ).exists(), "本地链路应产出 IMAGE 资产")
        data, summary = build_export_zip(play)
        self.assertGreaterEqual(summary["packed_assets"], 1)  # 本地资产进包
        report = precheck_import(io.BytesIO(data))
        self.assertEqual(report["project_uuid"], str(play.uuid))
