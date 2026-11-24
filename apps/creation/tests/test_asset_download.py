"""成片/预览鉴权下载测试（工作台 Stage D1，任务 8.1–8.4）。"""
import shutil
import tempfile

from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from apps.ai_gateway.models import AIModel, AIProvider
from apps.creation.models import (
    AgentConfig, Asset, AssetStatus, AssetType, AuditMode, Character, Episode,
    MemberRole, MemberStatus, Play, PlayMember, Scene, Shot,
)
from apps.creation.workspace.storage import project_dir, save_asset_bytes
from apps.core.system.models import CustomUser, UserRole


def _mk_member(play, user, role):
    PlayMember.objects.create(play=play, user=user, role=role, status=MemberStatus.ACTIVE)


class D1Fixture:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_tmp = tempfile.mkdtemp(prefix="d1-media-")
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
        self.owner = CustomUser.objects.create_user(
            username="d1o", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.viewer = CustomUser.objects.create_user(
            username="d1v", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.outsider = CustomUser.objects.create_user(
            username="d1x", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.provider = AIProvider.objects.create(
            name="P", provider_type="LLM", adapter_type="deepseek",
            base_url="https://x", api_key_encrypted="", priority=1)
        self.model = AIModel.objects.create(
            provider=self.provider, model_name="m", display_name="M", model_type="LLM")
        self.config = AgentConfig.objects.create(
            name="配方", llm_model=self.model, created_by=self.owner)
        self.play = Play.objects.create(
            title="下载剧", agent_config=self.config, created_by=self.owner,
            audit_mode=AuditMode.NONE)
        _mk_member(self.play, self.owner, MemberRole.OWNER)
        _mk_member(self.play, self.viewer, MemberRole.VIEWER)
        self.ep = Episode.objects.create(play=self.play, episode_number=1)
        self.scene = Scene.objects.create(episode=self.ep, scene_number=1)
        self.shot = Shot.objects.create(scene=self.scene, shot_number=1)

    def mk_video(self, data=b"fake-mp4-bytes", asset_type=AssetType.VIDEO,
                 status=AssetStatus.READY, **kw):
        """带 storage_key 的本地成片/预览资产。"""
        key, url, digest = save_asset_bytes(self.play, data, asset_type)
        return Asset.objects.create(
            play=self.play, episode=self.ep, asset_type=asset_type, status=status,
            storage_key=key, artifact_url=url, content_sha256=digest, **kw)


class DownloadApiTests(D1Fixture, APITestCase):
    """任务 8.1/8.2：鉴权、类型限制、FileResponse、X-Artifact-Kind。"""

    def test_video_download_file_response(self):
        video = self.mk_video()
        self.client.force_authenticate(self.viewer)  # 读权限可下载
        resp = self.client.get(f"/api/assets/{video.pk}/download/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "video/mp4")
        self.assertNotIn("application/json", resp["Content-Type"])  # 不包 JSON 信封
        self.assertIn("attachment", resp["Content-Disposition"])
        self.assertEqual(bytes(resp.getvalue() if hasattr(resp, "getvalue") else resp.content), b"fake-mp4-bytes")

    def test_anonymous_and_cross_play_denied(self):
        video = self.mk_video()
        # 匿名 401
        resp = self.client.get(f"/api/assets/{video.pk}/download/")
        self.assertEqual(resp.status_code, 401)
        # 非成员 404（归属隔离，不泄露存在性）
        self.client.force_authenticate(self.outsider)
        resp = self.client.get(f"/api/assets/{video.pk}/download/")
        self.assertEqual(resp.status_code, 404)

    def test_non_video_rejected(self):
        img = self.mk_video(asset_type=AssetType.IMAGE)
        self.client.force_authenticate(self.owner)
        resp = self.client.get(f"/api/assets/{img.pk}/download/")
        self.assertEqual(resp.status_code, 400)

    def test_not_ready_returns_410(self):
        video = self.mk_video(status=AssetStatus.SUPERSEDED)
        self.client.force_authenticate(self.owner)
        resp = self.client.get(f"/api/assets/{video.pk}/download/")
        self.assertEqual(resp.status_code, 410)
        self.assertIn("not_ready", resp.content.decode())

    def test_preview_header_distinguishes_video(self):
        """PREVIEW 带 X-Artifact-Kind: preview（不冒充正式 VIDEO）。"""
        preview = self.mk_video(asset_type=AssetType.PREVIEW)
        self.client.force_authenticate(self.owner)
        resp = self.client.get(f"/api/assets/{preview.pk}/download/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["X-Artifact-Kind"], "preview")
        # VIDEO 不带该头
        video = self.mk_video(data=b"real-video")
        resp2 = self.client.get(f"/api/assets/{video.pk}/download/")
        self.assertNotIn("X-Artifact-Kind", resp2)

    def test_historical_version_explicit_no_redirect(self):
        """历史 VIDEO：?version=<n> 显式取（含 SUPERSEDED 可回查）；
        不带 version 的旧资产 410 未就绪（不自动跳转最新）。"""
        v1 = self.mk_video(data=b"v1-bytes")  # version=1
        v2 = self.mk_video(data=b"v2-bytes")  # version=2（v1 被 supersede）
        v1.refresh_from_db()
        self.client.force_authenticate(self.owner)
        # v1 已被 v2 取代：直接下载 410（不自动跳转到 v2 的字节）
        resp = self.client.get(f"/api/assets/{v1.pk}/download/")
        self.assertEqual(resp.status_code, 410)
        # 当前版本 v2 正常
        resp_cur = self.client.get(f"/api/assets/{v2.pk}/download/")
        self.assertEqual(resp_cur.status_code, 200)
        self.assertEqual(
            bytes(resp_cur.getvalue() if hasattr(resp_cur, "getvalue")
                  else resp_cur.content), b"v2-bytes")
        # 显式 version=1 → 历史 v1 字节（SUPERSEDED 可回查）
        resp1 = self.client.get(f"/api/assets/{v1.pk}/download/?version=1")
        self.assertEqual(resp1.status_code, 200)
        self.assertEqual(
            bytes(resp1.getvalue() if hasattr(resp1, "getvalue")
                  else resp1.content), b"v1-bytes")
        # 不存在的历史版本 404；非法 version 400
        resp3 = self.client.get(f"/api/assets/{v1.pk}/download/?version=99")
        self.assertEqual(resp3.status_code, 404)
        resp4 = self.client.get(f"/api/assets/{v1.pk}/download/?version=abc")
        self.assertEqual(resp4.status_code, 400)

    def test_version_param_only_for_video(self):
        preview = self.mk_video(asset_type=AssetType.PREVIEW)
        self.client.force_authenticate(self.owner)
        resp = self.client.get(f"/api/assets/{preview.pk}/download/?version=1")
        self.assertEqual(resp.status_code, 400)

    def test_path_traversal_and_missing_file_no_leak(self):
        """路径穿越存储键/缺失文件：410/400，不泄露服务器路径。"""
        video = self.mk_video()
        video.storage_key = "projects/../../etc/passwd"
        video.save(update_fields=["storage_key"])
        self.client.force_authenticate(self.owner)
        resp = self.client.get(f"/api/assets/{video.pk}/download/")
        self.assertIn(resp.status_code, (400, 410))
        self.assertNotIn("D:\\", resp.content.decode())
        self.assertNotIn("/etc/", resp.content.decode())
        # 缺失文件
        video2 = self.mk_video()
        (project_dir(self.play) / "assets" / "sha256").rmdir() if False else None
        key_path = video2.storage_key
        import pathlib
        p = pathlib.Path(self._media_tmp) / key_path
        if p.exists():
            p.unlink()
        resp2 = self.client.get(f"/api/assets/{video2.pk}/download/")
        self.assertEqual(resp2.status_code, 410)
        self.assertNotIn("D:\\", resp2.content.decode())

    def test_remote_asset_without_object_410(self):
        """REMOTE 无受控对象：410 明确不可下载（不静默替换、不代理外部 URL）。"""
        remote = Asset.objects.create(
            play=self.play, episode=self.ep, asset_type=AssetType.VIDEO,
            status=AssetStatus.READY,
            artifact_url="https://vendor.example.com/hosted.mp4")
        self.client.force_authenticate(self.owner)
        resp = self.client.get(f"/api/assets/{remote.pk}/download/")
        self.assertEqual(resp.status_code, 410)
        self.assertIn("remote_no_object", resp.content.decode())
        # 绝不把外部 URL 回显为跳转
        self.assertNotIn("vendor.example.com",
                         getattr(resp, "headers", {}).get("Location", ""))


class S3DownloadTests(D1Fixture, TestCase):
    """任务 8.3：shared 私有 S3 签名 URL（受控 SDK，不拉外部 URL）。"""

    @override_settings(STORAGE_BACKEND="s3")
    def test_s3_signed_url_via_sdk(self):
        from apps.creation.workspace.download import build_download
        video = self.mk_video()
        calls = {}

        class FakeClient:
            def generate_presigned_url(self, op, Params=None, ExpiresIn=None):
                calls.update(op=op, key=Params.get("Key"), bucket=Params.get("Bucket"),
                             ttl=ExpiresIn)
                return "https://signed.example.com/limited"

        import sys
        import types
        import unittest.mock as mock
        fake_boto3 = types.ModuleType("boto3")
        fake_boto3.client = lambda *a, **k: FakeClient()
        with mock.patch.dict(sys.modules, {"boto3": fake_boto3}):
            from django.test import RequestFactory
            req = RequestFactory().get("/x/")
            resp = build_download(req, video)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(resp["Location"], "https://signed.example.com/limited")
        # 签名参数受控：本项目存储键 + 只读 + 短期
        self.assertEqual(calls["op"], "get_object")
        self.assertEqual(calls["key"], video.storage_key)
        self.assertEqual(calls["ttl"], 300)

    @override_settings(STORAGE_BACKEND="s3")
    def test_s3_remote_url_not_proxied(self):
        """S3 模式下 artifact_url 为外部 URL（无 storage_key）仍 410，不代理。"""
        remote = Asset.objects.create(
            play=self.play, episode=self.ep, asset_type=AssetType.VIDEO,
            status=AssetStatus.READY,
            artifact_url="https://evil.example.com/payload")
        from apps.creation.workspace.download import DownloadError, build_download
        with self.assertRaises(DownloadError):
            from django.test import RequestFactory
            build_download(RequestFactory().get("/x/"), remote)


class SubtitleExportConsistencyTests(D1Fixture, APITestCase):
    """任务 8.3：字幕 SRT/VTT 导出保持纯文本且权限一致（回归保障）。"""

    def test_subtitle_export_unchanged(self):
        """字幕导出仍为纯文本响应，读权限可访问（回归保障）。"""
        from apps.distribution.models import (
            SubtitleSourceType, SubtitleTrack, SubtitleStatus,
        )
        track = SubtitleTrack.objects.create(
            episode=self.ep, language="zh",
            source_type=SubtitleSourceType.AUTO, status=SubtitleStatus.READY,
            subtitle_data=[{"index": 1, "start": 1.0, "end": 2.0, "text": "你好"}])
        self.client.force_authenticate(self.viewer)
        resp = self.client.get(f"/api/subtitle-tracks/{track.pk}/export/srt/")
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn("application/json", resp["Content-Type"])
        self.assertIn("你好", resp.content.decode("utf-8"))
