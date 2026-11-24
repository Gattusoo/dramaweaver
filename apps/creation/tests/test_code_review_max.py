"""code-review max（2026-11-13）15 项发现的回归测试。"""
from datetime import timedelta
from unittest.mock import MagicMock, patch

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.ai_gateway.circuit_breaker import CircuitBreaker
from apps.ai_gateway.cost_tracker import calculate
from apps.ai_gateway.gateway import AIGateway, AIGatewayError
from apps.ai_gateway.rate_limiter import RateLimiter
from apps.ai_gateway.redis_client import MemoryStore
from apps.creation.models import (
    Asset, AssetStatus, AssetType, Episode, EpisodeStatus, MemberRole, MemberStatus,
    Play, PlayMember, Scene, Shot, TaskRecord, TaskStatus, TaskType,
)
from apps.ai_gateway.models import AIModel, AIProvider
from apps.distribution.models import DistributionChannel, Publication
from apps.distribution import rendition as rendition_mod
from apps.core.system.models import CustomUser, UserRole
from apps.core.utils.url_safety import UnsafeURLError, check_url


def _user(name, role):
    return CustomUser.objects.create_user(username=name, password="Passw0rd!123", role=role)


_SEND = patch("apps.creation.pipeline.dispatcher.send_task", return_value=MagicMock(id="x"))


class FixtureMixin:
    def setUp(self):
        _SEND.start()
        self.addCleanup(_SEND.stop)
        self.producer = _user("prd", UserRole.PRODUCER)
        self.editor = _user("edt", UserRole.EDITOR)
        self.play = Play.objects.create(title="剧", created_by=self.producer)
        PlayMember.objects.create(play=self.play, user=self.producer, role=MemberRole.OWNER,
                                  status=MemberStatus.ACTIVE)
        PlayMember.objects.create(play=self.play, user=self.editor, role=MemberRole.EDITOR,
                                  status=MemberStatus.ACTIVE)
        self.ep = Episode.objects.create(play=self.play, episode_number=1)
        self.scene = Scene.objects.create(episode=self.ep, scene_number=1)
        self.shot = Shot.objects.create(scene=self.scene, shot_number=1, dialogue="台词")


class AssetSerializerReadOnlyTests(FixtureMixin, APITestCase):
    """① 客户端不能经 API 伪造 READY 视频资产绕过生成/审核。"""

    def test_cannot_create_ready_video_with_artifact_url(self):
        self.client.force_authenticate(self.editor)
        resp = self.client.post("/api/assets/", {
            "play": self.play.pk, "episode": self.ep.pk, "asset_type": AssetType.VIDEO,
            "status": AssetStatus.READY,
            "artifact_url": "https://attacker.example/fake.mp4",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        asset = Asset.objects.get(pk=resp.data["id"])
        self.assertEqual(asset.status, AssetStatus.PENDING)
        self.assertEqual(asset.artifact_url, "")
        self.ep.refresh_from_db()
        self.assertIsNone(self.ep.current_video_asset)
        self.assertNotEqual(self.ep.status, EpisodeStatus.COMPLETED)

    def test_cannot_patch_asset_to_ready(self):
        asset = Asset.objects.create(play=self.play, episode=self.ep, asset_type=AssetType.VIDEO)
        self.client.force_authenticate(self.editor)
        resp = self.client.patch(f"/api/assets/{asset.pk}/", {
            "status": AssetStatus.READY,
            "artifact_url": "https://attacker.example/fake.mp4",
            "version": 99, "cost": 0, "ai_model": "fake",
        }, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        asset.refresh_from_db()
        self.assertEqual(asset.status, AssetStatus.PENDING)
        self.assertEqual(asset.artifact_url, "")
        self.assertEqual(asset.version, 1)

    def test_metadata_still_writable(self):
        asset = Asset.objects.create(play=self.play, asset_type=AssetType.IMAGE)
        self.client.force_authenticate(self.editor)
        resp = self.client.patch(f"/api/assets/{asset.pk}/", {"metadata": {"k": 1}}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        asset.refresh_from_db()
        self.assertEqual(asset.metadata, {"k": 1})


class RetryPolicyTests(FixtureMixin, TestCase):
    """③ 付费生成任务不自动重试；④ 重试判定 off-by-one。"""

    def test_paid_tasks_do_not_autoretry(self):
        from apps.creation import tasks as T
        self.assertEqual(T.generate_image.autoretry_for, ())
        self.assertEqual(T.generate_tts.autoretry_for, ())
        self.assertEqual(T.generate_character_ref.autoretry_for, ())
        self.assertEqual(T.compose_video.autoretry_for, ())
        self.assertEqual(T.render_channel_version.autoretry_for, ())

    def test_llm_tasks_still_autoretry(self):
        from apps.creation import tasks as T
        self.assertTrue(T.generate_outline.autoretry_for)
        self.assertTrue(T.generate_episode_script.autoretry_for)

    @staticmethod
    def _task(called_directly, retries, max_retries=3):
        from apps.creation import tasks as T
        task = MagicMock()
        task.autoretry_for = T._TRANSIENT_ERRORS
        task.max_retries = max_retries
        task.request = MagicMock(called_directly=called_directly, retries=retries)
        return task

    def test_retry_boundary_matches_celery(self):
        """Celery：retries=request.retries+1，> max_retries 时放弃。
        所以 request.retries == max_retries 时本次已不再重试（原实现误判为待重试）。"""
        from apps.creation.tasks import _retry_pending
        for retries, expected in ((0, True), (1, True), (2, True), (3, False), (4, False)):
            with patch("celery.current_task", self._task(False, retries)):
                self.assertIs(_retry_pending(AIGatewayError("x")), expected, f"retries={retries}")

    def test_last_failure_marks_failed(self):
        """名额耗尽那次（request.retries==max_retries）必须落 FAILED，不能永挂 RUNNING。"""
        import httpx
        from apps.creation import tasks as T
        record = TaskRecord.objects.create(play=self.play, task_type=TaskType.IMAGE_GEN)
        with patch("celery.current_task", self._task(False, 3)):
            with self.assertRaises(httpx.ReadTimeout):
                T._run(record.pk, MagicMock(side_effect=httpx.ReadTimeout("t")))
        record.refresh_from_db()
        self.assertEqual(record.status, TaskStatus.FAILED)


class GatewayNonIdempotent429Tests(TestCase):
    """⑤ 非幂等生成在 429 时不得整段重跑（轮询阶段 429 时供应商已受理计费）。"""

    def setUp(self):
        self.store = MemoryStore()
        self.provider = AIProvider.objects.create(
            name="p", provider_type="VIDEO", base_url="https://api.example.com",
            api_key_encrypted="k", is_active=True, retry_count=0,
        )
        self.model = AIModel.objects.create(
            provider=self.provider, display_name="m", model_name="v1",
            model_type="VIDEO", is_active=True, cost_config={"cost_per_second": "0.1"},
        )
        self.gateway = AIGateway(store=self.store, breaker=CircuitBreaker(self.store),
                                 limiter=RateLimiter(self.store))

    def _http_error(self, code):
        import httpx
        req = httpx.Request("GET", "https://api.example.com")
        return httpx.HTTPStatusError("err", request=req,
                                     response=httpx.Response(code, request=req))

    def test_video_429_not_retried(self):
        adapter = MagicMock()
        adapter.image_to_video.side_effect = self._http_error(429)
        self.gateway.adapter_factory = lambda p: adapter
        with patch.object(self.gateway, "_record_cost"):
            result, error = self.gateway._try_provider(
                "VIDEO", self.model, self.provider, {"image_url": "https://x/i.png"})
        self.assertIsNone(result)
        self.assertIsNotNone(error)
        self.assertEqual(adapter.image_to_video.call_count, 1, "非幂等生成不得重试")

    def test_llm_429_still_retried(self):
        provider = AIProvider.objects.create(
            name="lp", provider_type="LLM", base_url="https://api.example.com", api_key_encrypted="k", retry_count=3)
        model = AIModel.objects.create(provider=provider, display_name="lm", model_name="l1",
                                       model_type="LLM", is_active=True)
        adapter = MagicMock()
        adapter.generate.side_effect = self._http_error(429)
        self.gateway.adapter_factory = lambda p: adapter
        with patch("time.sleep"), patch.object(self.gateway, "_record_cost"):
            self.gateway._try_provider("LLM", model, provider, {"prompt": "hi"})
        self.assertGreater(adapter.generate.call_count, 1, "幂等调用应继续重试")


class VideoCostTests(TestCase):
    """⑬ 视频成本按顶层 duration_seconds 计价，不能恒为 0。"""

    def test_video_cost_uses_duration_seconds(self):
        from apps.ai_gateway import gateway as gw_mod
        recorded = {}

        def fake_record_cost(**kwargs):
            recorded.update(kwargs)
            return MagicMock()

        provider = AIProvider.objects.create(
            name="vp", provider_type="VIDEO", base_url="https://api.example.com", api_key_encrypted="k", retry_count=0)
        model = AIModel.objects.create(provider=provider, display_name="vm", model_name="v1",
                                       model_type="VIDEO", cost_config={"cost_per_second": "0.2"})
        play = Play.objects.create(title="剧", created_by=_user("p", UserRole.PRODUCER))
        gateway = AIGateway(store=MemoryStore())
        with patch.object(gw_mod, "record_cost", side_effect=fake_record_cost):
            gateway._record_cost(model, provider, "VIDEO",
                                 {"url": "https://x/v.mp4", "duration_seconds": 5},
                                 play=play)
        self.assertEqual(recorded["usage"].get("video_seconds"), 5)
        self.assertEqual(calculate("VIDEO", recorded["usage"], {"cost_per_second": "0.2"}), 1.0)


class EmptyLLMListTests(FixtureMixin, TestCase):
    """⑥ LLM 返回空 scenes/shots 时不得删光既有场景/镜头。"""

    def test_empty_scenes_keeps_existing(self):
        from apps.creation import tasks as T
        self.scene.description = "原场景"
        self.scene.save()
        record = TaskRecord.objects.create(
            play=self.play, episode=self.ep, task_type=TaskType.EPISODE_SCRIPT,
        )
        with patch.object(T, "_gateway") as gw:
            gw.return_value.call.return_value = {"text": '{"scenes": []}', "cost": 0}
            with self.assertRaises(ValueError):
                T.generate_episode_script(record.pk)
        self.assertTrue(Scene.objects.filter(pk=self.scene.pk).exists(), "既有场景不得被删")
        self.assertTrue(Shot.objects.filter(pk=self.shot.pk).exists(), "既有镜头不得被删")

    def test_empty_shots_keeps_existing(self):
        from apps.creation import tasks as T
        record = TaskRecord.objects.create(
            play=self.play, episode=self.ep, scene=self.scene, task_type=TaskType.STORYBOARD,
        )
        with patch.object(T, "_gateway") as gw:
            gw.return_value.call.return_value = {"text": '{"shots": []}', "cost": 0}
            with self.assertRaises(ValueError):
                T.generate_storyboard(record.pk)
        self.assertTrue(Shot.objects.filter(pk=self.shot.pk).exists())


class DispatchDedupeTests(FixtureMixin, TestCase):
    """⑪ 素材/角色派发去重：已在跑的任务不重复提交付费生成。"""

    def test_image_not_dispatched_twice(self):
        from apps.creation.pipeline.dispatcher import dispatch_image_and_tts
        dispatch_image_and_tts(self.shot)
        n_first = TaskRecord.objects.filter(
            shot=self.shot, task_type=TaskType.IMAGE_GEN).count()
        dispatch_image_and_tts(self.shot)
        n_second = TaskRecord.objects.filter(
            shot=self.shot, task_type=TaskType.IMAGE_GEN).count()
        self.assertEqual(n_first, 1)
        self.assertEqual(n_second, 1, "进行中的素材任务不得重复派发")

    def test_character_ref_not_dispatched_twice(self):
        from apps.creation.models import Character
        from apps.creation.pipeline.dispatcher import dispatch_character_ref
        Character.objects.create(play=self.play, name="甲")
        dispatch_character_ref(self.play)
        dispatch_character_ref(self.play)
        self.assertEqual(
            TaskRecord.objects.filter(play=self.play,
                                      task_type=TaskType.CHARACTER_REF).count(), 1)


class PublishResyncTests(FixtureMixin, APITestCase):
    """⑮ 重新发布要同步最新成片到既有 Publication，不能沿用旧渠道版本。"""

    def test_republish_syncs_new_video_asset(self):
        channel = DistributionChannel.objects.create(name="抖音", platform="douyin")
        v1 = Asset.objects.create(play=self.play, episode=self.ep, asset_type=AssetType.VIDEO,
                                  status=AssetStatus.READY, artifact_url="/media/v1.mp4")
        self.ep.status = EpisodeStatus.COMPLETED
        self.ep.current_video_asset = v1
        self.ep.save(update_fields=["status", "current_video_asset"])
        pub = Publication.objects.create(episode=self.ep, channel=channel,
                                         title="t", video_asset=v1)
        v1.status = AssetStatus.SUPERSEDED
        v1.save(update_fields=["status"])
        v2 = Asset.objects.create(play=self.play, episode=self.ep, asset_type=AssetType.VIDEO,
                                  status=AssetStatus.READY, artifact_url="/media/v2.mp4")
        self.ep.current_video_asset = v2
        self.ep.save(update_fields=["current_video_asset"])

        self.client.force_authenticate(self.producer)
        with patch("apps.distribution.rendition.ensure_rendition"):
            resp = self.client.post(f"/api/plays/{self.play.pk}/publish/",
                                    {"channel": channel.pk}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        pub.refresh_from_db()
        self.assertEqual(pub.video_asset_id, v2.pk)


class RenditionTimeoutTests(FixtureMixin, TestCase):
    """⑫ processing 超时后允许重新派发，不能永久卡死。"""

    def _pub(self, status):
        channel = DistributionChannel.objects.create(name="抖音", platform="douyin")
        video = Asset.objects.create(play=self.play, episode=self.ep, asset_type=AssetType.VIDEO,
                                     status=AssetStatus.READY, artifact_url="/media/v.mp4",
                                     metadata={"media": {"width": 1920, "height": 1080}})
        return Publication.objects.create(episode=self.ep, channel=channel, title="t",
                                          video_asset=video,
                                          rendition={"status": status, "video_asset_id": video.pk,
                                                     "requirements": {}})

    def test_fresh_processing_raises_busy(self):
        pub = self._pub(rendition_mod.PROCESSING)
        Publication.objects.filter(pk=pub.pk).update(updated_at=timezone.now())
        pub.refresh_from_db()
        with self.assertRaises(rendition_mod.RenditionBusy):
            rendition_mod.ensure_rendition(pub, force=True)

    def test_stale_processing_is_recovered(self):
        pub = self._pub(rendition_mod.PROCESSING)
        Publication.objects.filter(pk=pub.pk).update(
            updated_at=timezone.now() - rendition_mod.PROCESSING_TIMEOUT - timedelta(minutes=1))
        pub.refresh_from_db()
        with patch("apps.creation.pipeline.dispatcher.dispatch_task") as dispatch:
            dispatch.return_value = MagicMock(pk=1)
            result = rendition_mod.ensure_rendition(pub, force=True)
        self.assertNotEqual(result.get("status"), rendition_mod.PROCESSING)


class UrlSafetyTests(TestCase):
    """⑩ 共享地址与 site-local 地址视为内网。"""

    def test_cgnat_metadata_blocked(self):
        from apps.core.utils.url_safety import _is_public_ip
        import ipaddress
        self.assertFalse(_is_public_ip(ipaddress.ip_address("100.100.100.200")))
        self.assertFalse(_is_public_ip(ipaddress.ip_address("100.64.0.1")))
        self.assertFalse(_is_public_ip(ipaddress.ip_address("fec0::1")))
        self.assertTrue(_is_public_ip(ipaddress.ip_address("8.8.8.8")))