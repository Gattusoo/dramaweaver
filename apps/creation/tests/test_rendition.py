"""渠道格式适配测试（openspec change: backend-ffmpeg-quality-distribution，任务组 6-7）。"""
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APITestCase

from dvadmin.utils import media
from apps.distribution import rendition as rd
from apps.distribution.publication import advance_status
from apps.creation.models import (
    Asset, AssetStatus, AssetType, Episode, MemberRole, MemberStatus, Play, PlayMember,
    TaskRecord, TaskType,
)
from dvadmin.system.models import (
    CustomUser, UserRole,
)
from apps.distribution.models import (
    DistributionChannel, Publication, PublicationStatus,
)

MASTER = {"width": 1080, "height": 1920, "fps": 30.0, "duration": 60.0,
          "size_bytes": 45 * 1024 * 1024, "bit_rate_kbps": 6000, "has_audio": True}


class RequirementPureTests(SimpleTestCase):
    """任务 6.2 / 6.3 / 7.1 / 7.2 / 7.3（纯函数）。"""

    def test_validate_requirements(self):
        self.assertEqual(media.validate_requirements(
            {"resolution": "720x1280", "aspect_ratio": "9:16", "max_size_mb": 100, "loudness_lufs": -14}), {})
        errors = media.validate_requirements({"resolution": "abc", "fps": "30", "loudness_lufs": 3})
        self.assertEqual(set(errors), {"resolution", "fps", "loudness_lufs"})
        self.assertIn("aspect_ratio", media.validate_requirements({"resolution": "720x1280", "aspect_ratio": "16:9"}))
        self.assertIn("resolution", media.validate_requirements({"resolution": "721x1280"}))

    def test_evaluate_requirements(self):
        ev = media.evaluate_requirements
        self.assertEqual(ev(MASTER, {})[0], "not_needed")
        self.assertEqual(ev({}, {})[0], "not_needed")
        self.assertEqual(ev(MASTER, {"max_size_mb": 100})[0], "not_needed")
        self.assertEqual(ev(MASTER, {"resolution": "720x1280"})[0], "needed")
        self.assertEqual(ev(MASTER, {"loudness_lufs": -14})[0], "needed")
        self.assertEqual(ev({**MASTER, "duration": 320}, {"max_duration": 300})[0], "failed")
        self.assertEqual(ev({}, {"max_size_mb": 100})[0], "needed")  # 缺元数据

    def test_rendition_plan(self):
        plan = media.rendition_plan(MASTER, {"resolution": "720x1280"})
        self.assertEqual((plan["width"], plan["height"], plan["pad_blur"]), (720, 1280, False))
        wide = media.rendition_plan(MASTER, {"aspect_ratio": "16:9"})
        self.assertEqual((wide["width"], wide["height"], wide["pad_blur"]), (1920, 1080, True))
        sized = media.rendition_plan({**MASTER, "size_bytes": 120 * 1024 * 1024}, {"max_size_mb": 50})
        self.assertEqual(sized["video_kbps_cap"], int(50 * 8192 * 0.92 / 60) - 128)
        with self.assertRaises(ValueError):
            media.rendition_plan({**MASTER, "duration": 300}, {"max_size_mb": 5})

    def test_rendition_filter(self):
        same = media.rendition_filter({"width": 720, "height": 1280, "pad_blur": False, "fps": 25})
        self.assertIn("scale=720:1280:flags=lanczos", same)
        self.assertIn("fps=25", same)
        blur = media.rendition_filter({"width": 1920, "height": 1080, "pad_blur": True, "fps": None})
        for token in ("split=2", "boxblur=", "force_original_aspect_ratio=decrease", "overlay=(W-w)/2:(H-h)/2"):
            self.assertIn(token, blur)

    def test_loudnorm_parse(self):
        stderr = ('[Parsed_loudnorm_0 @ 0x1] \n{\n\t"input_i" : "-23.54",\n\t"input_tp" : "-7.12",\n'
                  '\t"input_lra" : "1.20",\n\t"input_thresh" : "-33.80",\n\t"target_offset" : "0.42"\n}\n')
        m = media.parse_loudnorm_json(stderr)
        second = media.loudnorm_second_pass(-14, m)
        self.assertIn("measured_I=-23.54", second)
        self.assertIn("linear=true", second)
        self.assertEqual(media.parse_ebur128_integrated("  Integrated loudness:\n    I:  -14.2 LUFS\n"), -14.2)


_SEND = "apps.creation.pipeline.dispatcher.send_task"


class RenditionStateMixin:
    def setUp(self):
        super().setUp()
        p = patch(_SEND, return_value=MagicMock(id="celery-1"))
        self.send = p.start()
        self.addCleanup(p.stop)
        self.producer = CustomUser.objects.create_user(username="rp", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.viewer = CustomUser.objects.create_user(username="rv", password="Passw0rd!123", role=UserRole.VIEWER)
        self.play = Play.objects.create(title="剧", created_by=self.producer)
        PlayMember.objects.get_or_create(play=self.play, user=self.producer,
                                         defaults={"role": MemberRole.OWNER, "status": MemberStatus.ACTIVE})
        PlayMember.objects.get_or_create(play=self.play, user=self.producer,
                                         defaults={"role": MemberRole.OWNER, "status": MemberStatus.ACTIVE})
        PlayMember.objects.create(play=self.play, user=self.viewer, role=MemberRole.VIEWER,
                                  status=MemberStatus.ACTIVE)
        self.ep = Episode.objects.create(play=self.play, episode_number=1)
        self.video = Asset.objects.create(play=self.play, episode=self.ep, asset_type=AssetType.VIDEO,
                                          status=AssetStatus.READY, artifact_url="/media/videos/ep1.mp4",
                                          metadata={"media": MASTER})

    def pub(self, req):
        channel = DistributionChannel.objects.create(name="渠道", platform="x", format_requirements=req)
        return Publication.objects.create(episode=self.ep, channel=channel, video_asset=self.video)


class RenditionStateTests(RenditionStateMixin, TestCase):
    """任务 6.3 / 6.4 / 6.6。"""

    def test_not_needed_uses_master(self):
        pub = self.pub({"max_size_mb": 100})
        r = rd.ensure_rendition(pub)
        self.assertEqual((r["status"], r["url"]), (rd.NOT_NEEDED, "/media/videos/ep1.mp4"))
        self.send.assert_not_called()
        advance_status(pub, PublicationStatus.UPLOADING)

    def test_needed_dispatches_once_and_blocks_upload(self):
        pub = self.pub({"resolution": "720x1280"})
        with self.captureOnCommitCallbacks(execute=True):
            r = rd.ensure_rendition(pub)
        self.assertEqual(r["status"], rd.PENDING)
        # 消息在事务提交后才发出，且发出时 task_record_id 已落库
        self.assertEqual(self.send.call_count, 1)
        pub.refresh_from_db()
        self.assertEqual(pub.rendition["task_record_id"], r["task_record_id"])
        self.assertEqual(self.send.call_args.args[0], "shortplay.tasks.render_channel_version")
        rd.ensure_rendition(pub)  # 未失效时不重复派发
        self.assertEqual(self.send.call_count, 1)
        with self.assertRaises(rd.RenditionNotReady):
            advance_status(pub, PublicationStatus.UPLOADING)
        self.assertTrue(rd.claim(pub.pk, r["task_record_id"]))
        with self.assertRaises(rd.RenditionBusy):
            rd.ensure_rendition(pub, force=True)

    def test_stale_task_is_skipped(self):
        pub = self.pub({"resolution": "720x1280"})
        first = rd.ensure_rendition(pub)["task_record_id"]
        second = rd.ensure_rendition(pub, force=True)["task_record_id"]
        self.assertNotEqual(first, second)
        self.assertIsNone(rd.claim(pub.pk, first))
        self.assertIsNotNone(rd.claim(pub.pk, second))

    def test_too_long_fails_without_dispatch(self):
        pub = self.pub({"max_duration": 30})
        self.assertEqual(rd.ensure_rendition(pub)["status"], rd.FAILED)
        self.send.assert_not_called()

    def test_legacy_publication_evaluated_on_upload(self):
        pub = self.pub({})
        self.assertEqual(pub.rendition, {})
        advance_status(pub, PublicationStatus.UPLOADING)
        pub.refresh_from_db()
        self.assertEqual(pub.rendition["status"], rd.NOT_NEEDED)

    def test_requirement_change_invalidates(self):
        pub = self.pub({})
        rd.ensure_rendition(pub)
        pub.channel.format_requirements = {"resolution": "720x1280"}
        pub.channel.save()
        pub.refresh_from_db()
        with self.assertRaises(rd.RenditionNotReady):
            advance_status(pub, PublicationStatus.UPLOADING)


class RenditionApiTests(RenditionStateMixin, APITestCase):
    """任务 6.2 / 6.5 / 6.6 / 6.7。"""

    def test_channel_requirements_validated(self):
        admin = CustomUser.objects.create_user(username="ra", password="Passw0rd!123", is_platform_admin=True)
        self.client.force_authenticate(admin)
        bad = self.client.post("/api/distribution-channels/", {"name": "c", "platform": "p",
                               "format_requirements": {"resolution": "abc"}}, format="json")
        self.assertEqual(bad.status_code, 400)
        self.assertIn("resolution", str(bad.data))
        ok = self.client.post("/api/distribution-channels/", {"name": "c", "platform": "p",
                              "format_requirements": {"resolution": "720x1280"}}, format="json")
        self.assertEqual(ok.status_code, 201)

    def test_create_triggers_rendition_and_upload_gated(self):
        channel = DistributionChannel.objects.create(name="快手", platform="ks",
                                                     format_requirements={"resolution": "720x1280"})
        self.client.force_authenticate(self.producer)
        resp = self.client.post("/api/publications/", {"episode": self.ep.pk, "channel": channel.pk}, format="json")
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.data["rendition"]["status"], rd.PENDING)
        pub_id = resp.data["id"]
        sync = self.client.post(f"/api/publications/{pub_id}/sync/", {"status": "uploading"}, format="json")
        self.assertEqual(sync.status_code, 400)
        self.assertIn("渠道版本未就绪", sync.data["detail"])
        rd.finish(pub_id, url="/media/renditions/x.mp4", media={"width": 720})
        sync = self.client.post(f"/api/publications/{pub_id}/sync/", {"status": "uploading"}, format="json")
        self.assertEqual(sync.status_code, 200)

    def test_regenerate_endpoint_permissions(self):
        pub = self.pub({"resolution": "720x1280"})
        url = f"/api/publications/{pub.pk}/rendition/"
        self.client.force_authenticate(self.viewer)
        self.assertEqual(self.client.post(url).status_code, 403)
        self.client.force_authenticate(self.producer)
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["rendition"]["status"], rd.PENDING)
        rd.claim(pub.pk, resp.data["rendition"]["task_record_id"])
        self.assertEqual(self.client.post(url).status_code, 400)
        # 客户端不可直接写 rendition
        self.client.patch(f"/api/publications/{pub.pk}/", {"rendition": {"status": "ready"}}, format="json")
        pub.refresh_from_db()
        self.assertEqual(pub.rendition["status"], rd.PROCESSING)


def _ffmpeg_ok():
    try:
        return subprocess.run([settings.FFMPEG_BIN, "-version"], capture_output=True, timeout=20).returncode == 0
    except Exception:  # noqa: BLE001
        return False


class RenderChannelVersionTests(RenditionStateMixin, TestCase):
    """任务 7.3 / 7.4：真实 ffmpeg 转码。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not _ffmpeg_ok():
            raise unittest.SkipTest("ffmpeg 不可用")
        cls.fx = Path(tempfile.mkdtemp(prefix="sp_rd_"))
        # 2 秒竖屏母版，音量偏低（约 -30 LUFS），用于验证响度归一
        subprocess.run([settings.FFMPEG_BIN, "-v", "error", "-y",
                        "-f", "lavfi", "-i", "testsrc2=s=360x640:r=30:d=2",
                        "-f", "lavfi", "-i", "sine=frequency=440:duration=2,volume=0.03",
                        "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest",
                        str(cls.fx / "master.mp4")], check=True, timeout=120)

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "fx", ""), ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.out = Path(tempfile.mkdtemp(prefix="sp_rdo_"))
        self.addCleanup(shutil.rmtree, self.out, True)

        def fake_download(url, path, play_pk=None):
            shutil.copyfile(self.fx / "master.mp4", path)
            return path

        def fake_save(data, filename, subdir=""):
            (self.out / filename).write_bytes(data)
            return f"/media/{subdir}/{filename}"

        for p in (patch("apps.creation.tasks._download", side_effect=fake_download),
                  patch("dvadmin.utils.storage.save_artifact", side_effect=fake_save)):
            p.start()
            self.addCleanup(p.stop)

    def run_task(self, pub):
        from apps.creation.tasks import render_channel_version
        r = rd.ensure_rendition(pub)
        render_channel_version(r["task_record_id"])
        pub.refresh_from_db()
        return pub.rendition

    def test_scale_and_loudness(self):
        r = self.run_task(self.pub({"resolution": "180x320", "fps": 25, "loudness_lufs": -16}))
        self.assertEqual(r["status"], rd.READY, r.get("error"))
        self.assertEqual((r["media"]["width"], r["media"]["height"]), (180, 320))
        self.assertAlmostEqual(r["media"]["fps"], 25, delta=0.5)
        self.assertAlmostEqual(r["media"]["loudness_lufs"], -16, delta=1.0)
        self.assertTrue(r["url"].startswith("/media/renditions/"))

    def test_blur_pad_to_landscape(self):
        r = self.run_task(self.pub({"resolution": "320x180"}))
        self.assertEqual(r["status"], rd.READY, r.get("error"))
        self.assertEqual((r["media"]["width"], r["media"]["height"]), (320, 180))

    def test_verification_failure_marks_failed(self):
        pub = self.pub({"resolution": "180x320"})
        with patch("dvadmin.utils.media.rendition_filter",
                   return_value="[0:v]scale=200:320,setsar=1,format=yuv420p[vout]"):
            r = self.run_task(pub)
        self.assertEqual(r["status"], rd.FAILED)
        self.assertIn("分辨率", r["error"])
        record = TaskRecord.objects.filter(task_type=TaskType.RENDITION).latest("pk")
        self.assertEqual(record.output_summary["status"], rd.FAILED)
