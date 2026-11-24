"""视频合成质量修复测试（openspec change: backend-ffmpeg-quality-distribution，任务组 1-5）。"""
import re
import shutil
import unittest
import subprocess
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase

from apps.creation import tasks
from apps.creation.models import (
    AgentConfig, Asset, AssetLibrary, AssetStatus, AssetType, AuditMode, Episode, Play, Scene, Shot, TaskRecord, TaskStatus, TaskType,
)
from apps.ai_gateway.models import (
    AIModel, AIProvider,
)
from apps.core.system.models import (
    CustomUser, UserRole,
)
from apps.distribution.models import (
    SubtitleSourceType, SubtitleStatus, SubtitleTrack,
)


def _seg(role, dur, has_audio=False, kind="video"):
    return {"kind": kind, "role": role, "duration": dur, "has_audio": has_audio,
            "input_args": ["-i", f"{role}.mp4"], "filter": "null"}


def _graph(segments, voice_map=None, bgm=None, subtitle=None, watermark=None, td=0.5, style=None):
    durs = [s["duration"] for s in segments]
    td = td if len(segments) > 1 else 0.0
    offsets, total, starts = tasks._xfade_timeline(durs, td)
    return tasks._build_filtergraph(segments, subtitle, voice_map or {}, bgm, style or {}, "",
                                    td, "fade", offsets, total, starts, watermark=watermark)


class FiltergraphAudioTests(SimpleTestCase):
    """任务 3.1-3.4。"""

    def test_three_buses(self):
        segs = [_seg("intro", 3.0, True), _seg("body", 4.0, True), _seg("body", 3.0, kind="image"),
                _seg("outro", 2.0, True)]
        script, args, has_audio, _ = _graph(segs, voice_map={1: "v1.mp3"}, bgm="bgm.mp3")
        self.assertTrue(has_audio)
        # bed（BGM + 正片环境声）为被压缩主路，voice 为 sidechain
        self.assertIn("[ca1][bgm]amix=inputs=2:duration=longest:dropout_transition=0:normalize=0[bed]", script)
        self.assertIn("[bed][voice_sc]sidechaincompress", script)
        # 片头片尾原声直接进最终混音，不经 sidechaincompress
        final = re.search(r"\[voice_mix\]\[bed_duck\]\[ca0\]\[ca3\]amix", script)
        self.assertIsNotNone(final, script)
        self.assertIn("alimiter=limit=0.95", script)
        self.assertIn("-stream_loop", args)

    def test_bgm_only_covers_body(self):
        segs = [_seg("intro", 3.0), _seg("body", 5.0, kind="image"), _seg("outro", 2.0)]
        script, *_ = _graph(segs, bgm="bgm.mp3")
        # 正片：3.0 -> 3.0-0.5+5.0-0.5 = 7.0，时长 4.0
        self.assertIn("atrim=0:4.000", script)
        self.assertIn("adelay=3000:all=1[bgm]", script)

    def test_clip_audio_aligned_to_segment_local_zero(self):
        segs = [_seg("intro", 3.0, True), _seg("body", 4.0, True)]
        script, *_ = _graph(segs)
        # 第二段本地 0 点 = starts[1] - td = 3.0 - 0.5 = 2.5
        self.assertIn("adelay=2500:all=1[ca1]", script)

    def test_silent_clip_and_no_audio(self):
        segs = [_seg("intro", 2.0, False), _seg("body", 3.0, kind="image")]
        script, _, has_audio, _ = _graph(segs)
        self.assertFalse(has_audio)
        self.assertNotIn("[0:a]", script)
        self.assertNotIn("aout", script)


class FiltergraphWatermarkTests(SimpleTestCase):
    """任务 5.2-5.3。"""

    def _spec(self, **kw):
        from apps.core.utils.media import parse_watermark
        return parse_watermark({"type": "image", "url": "u", **kw})

    def test_image_overlay_after_subtitles_only_in_body(self):
        segs = [_seg("intro", 3.0), _seg("body", 5.0, kind="image"), _seg("outro", 2.0)]
        wm = {"spec": self._spec(position="top_right", opacity=0.6, scale=0.15), "path": "wm.png"}
        script, args, _, vlabel = _graph(segs, subtitle="sub.ass", watermark=wm)
        self.assertEqual(vlabel, "vwm")
        self.assertLess(script.index("subtitles="), script.index("overlay="))
        self.assertIn("enable='between(t,3.000,7.000)'", script)
        self.assertIn("colorchannelmixer=aa=0.600", script)
        self.assertIn("x=main_w-overlay_w-40:y=40", script)
        self.assertEqual(args[-4:], ["-loop", "1", "-i", "wm.png"])

    def test_text_watermark_bottom_avoids_subtitles(self):
        from apps.core.utils.media import parse_watermark
        spec = parse_watermark({"type": "text", "text": "出品", "position": "bottom_left", "margin": 20})
        wm = {"spec": spec, "textfile": "C:/t/wm.txt", "fontfile": "C:/Windows/Fonts/msyh.ttc"}
        script, *_ = _graph([_seg("body", 3.0, kind="image")], subtitle="sub.ass", watermark=wm,
                            style={"subtitle_style": {"margin_v": 80, "font_size": 48}})
        self.assertIn(r"textfile=C\\:/t/wm.txt", script)  # 两层转义：: -> \: -> \\:
        self.assertIn("alpha=0.600", script)
        self.assertIn("y=h-text_h-148", script)  # 20 + 80 + 48

    def test_missing_watermark_asset_skipped(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch("apps.creation.tasks._download", side_effect=OSError("404")):
            self.assertIsNone(tasks._resolve_watermark(Path(tmp), {"watermark": {"type": "image", "url": "x"}}))


class SegmentAndEncodeTests(SimpleTestCase):
    """任务 2.1 / 2.4 / 2.5 / 4.2。"""

    def test_voice_duration_fallbacks(self):
        asset = MagicMock(metadata={"duration_seconds": 2.5})
        with patch("apps.creation.tasks._probe_duration", return_value=3.1):
            self.assertEqual(tasks._voice_duration("v.mp3", asset), 3.1)
        with patch("apps.creation.tasks._probe_duration", side_effect=ValueError("bad")):
            with self.assertLogs("apps.creation.tasks", "WARNING"):
                self.assertEqual(tasks._voice_duration("v.mp3", asset), 2.5)
            self.assertEqual(tasks._voice_duration("v.mp3", MagicMock(metadata={})), 0.0)

    def test_video_segment_padded_to_planned(self):
        info = {"duration": 3.0, "has_audio": True}
        with patch("apps.creation.tasks._probe_media", return_value=info):
            seg = tasks._video_input_segment("i2v.mp4", "body", planned=4.5)
        self.assertIn("tpad=stop_mode=clone:stop_duration=4.500", seg["filter"])
        self.assertIn("trim=duration=4.500", seg["filter"])
        self.assertTrue(seg["has_audio"])

    def test_subtitles_follow_voice_duration(self):
        track = MagicMock(subtitle_data=[{"start": 0, "end": 1.0, "text": "甲"}])
        shot = MagicMock(pk=7, dialogue="甲")
        out = tasks._align_subtitles(track, [shot], [3.0], {7: 0}, {7: 4.2})
        self.assertEqual(out, [{"start": 3.0, "end": 7.2, "text": "甲"}])

    def test_subtitles_chain_when_shot_segment_missing(self):
        """B.3：缺段对白镜头接前一条目末尾补位，其余条目仍按各自段落起点对齐（不整体漂移）。"""
        track = MagicMock(subtitle_data=[
            {"start": 0, "end": 1.0, "text": "甲"},
            {"start": 1.0, "end": 2.0, "text": "乙"},
            {"start": 2.0, "end": 3.0, "text": "丙"},
        ])
        s1 = MagicMock(pk=1, dialogue="a")
        s2 = MagicMock(pk=2, dialogue="b")   # 该镜头缺视频片段（seg_of_shot 无 2）
        s3 = MagicMock(pk=3, dialogue="c")
        out = tasks._align_subtitles(track, [s1, s2, s3], [5.0, 9.0, 13.0], {1: 0, 3: 2}, {})
        self.assertEqual(out[0], {"start": 5.0, "end": 6.0, "text": "甲"})
        self.assertEqual(out[1], {"start": 6.0, "end": 7.0, "text": "乙"})   # 接甲末尾
        self.assertEqual(out[2], {"start": 13.0, "end": 14.0, "text": "丙"})  # 按自身段起点

    def test_subtitles_scaled_when_counts_mismatch(self):
        """B.3：条目数≠对白镜头数（分镜重做）→ 按比例缩放到合成时间线，吸收 xfade 压缩。"""
        track = MagicMock(subtitle_data=[
            {"start": 0, "end": 1.0, "text": "甲"},
            {"start": 1.0, "end": 4.0, "text": "乙"},
        ])
        shot = MagicMock(pk=7, dialogue="甲")
        # 1 个对白镜头 vs 2 条目；时间线 [2.0, 8.0]，raw_max=4 → scale=1.5
        out = tasks._align_subtitles(track, [shot], [2.0], {7: 0}, {}, total=8.0)
        self.assertEqual(out[0]["start"], 2.0)
        self.assertAlmostEqual(out[0]["end"], 3.5)
        self.assertAlmostEqual(out[1]["start"], 3.5)
        self.assertAlmostEqual(out[1]["end"], 8.0)

    def test_hw_encode_fallback(self):
        calls = []

        def fake(args, **kw):
            calls.append(args)
            if "h264_nvenc" in args:
                raise subprocess.CalledProcessError(1, "ffmpeg", output="No NVENC capable devices")

        with patch("apps.creation.tasks.run_ffmpeg", side_effect=fake):
            with self.assertLogs("apps.creation.tasks", "WARNING"):
                codec = tasks._encode_with_fallback(["-y"], "out.mp4", "h264_nvenc", 23, "medium", 5, None)
        self.assertEqual(codec, "libx264")
        self.assertEqual(len(calls), 2)
        self.assertIn("-crf", calls[1])

    def test_cancel_and_timeout_do_not_retry(self):
        for exc in (tasks.RenderCancelled("x"), subprocess.TimeoutExpired("ffmpeg", 1)):
            with patch("apps.creation.tasks.run_ffmpeg", side_effect=exc) as run:
                with self.assertRaises(type(exc)):
                    tasks._encode_with_fallback(["-y"], "o.mp4", "h264_nvenc", 23, "medium", 5, None)
            self.assertEqual(run.call_count, 1)


def _ffmpeg_ok():
    try:
        return subprocess.run([settings.FFMPEG_BIN, "-version"], capture_output=True, timeout=20).returncode == 0
    except Exception:  # noqa: BLE001
        return False


def _ff(*args):
    subprocess.run([settings.FFMPEG_BIN, "-v", "error", "-y", *args], check=True, timeout=120)


def _mean_volume(path, start, dur):
    proc = subprocess.run(
        [settings.FFMPEG_BIN, "-hide_banner", "-ss", str(start), "-t", str(dur), "-i", str(path),
         "-af", "volumedetect", "-vn", "-f", "null", "-"],
        capture_output=True, text=True, errors="replace", timeout=60,
    )
    m = re.search(r"mean_volume:\s*(-?[\d.]+|-inf) dB", proc.stderr)
    return float(m.group(1)) if m and m.group(1) != "-inf" else -999.0


class ComposeFixtureMixin:
    """真实 ffmpeg 素材；画面缩小到 180x320 避免测试过慢。"""

    W, H = 180, 320
    FILES = {"img": "img.png", "voice": "voice.mp3", "bgm": "bgm.mp3", "intro": "intro.mp4", "wm": "wm.png"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not _ffmpeg_ok():
            raise unittest.SkipTest("ffmpeg 不可用")
        cls.fx = Path(tempfile.mkdtemp(prefix="sp_fx_"))
        _ff("-f", "lavfi", "-i", "color=c=red:s=360x640", "-frames:v", "1", str(cls.fx / "img.png"))
        _ff("-f", "lavfi", "-i", "sine=frequency=440:duration=1.5", "-c:a", "libmp3lame", str(cls.fx / "voice.mp3"))
        _ff("-f", "lavfi", "-i", "sine=frequency=220:duration=2", "-c:a", "libmp3lame", str(cls.fx / "bgm.mp3"))
        _ff("-f", "lavfi", "-i", "color=c=blue:s=360x640:d=1", "-f", "lavfi", "-i",
            "sine=frequency=660:duration=1", "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
            "-shortest", str(cls.fx / "intro.mp4"))
        _ff("-f", "lavfi", "-i", "color=c=white:s=64x32", "-frames:v", "1", str(cls.fx / "wm.png"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "fx", ""), ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.out_dir = Path(tempfile.mkdtemp(prefix="sp_out_"))
        self.addCleanup(shutil.rmtree, self.out_dir, True)

        def fake_download(url, path, play_pk=None):
            shutil.copyfile(self.fx / self.FILES[url.split("://", 1)[1]], path)
            return path

        def fake_save(data, filename, subdir=""):
            (self.out_dir / filename).write_bytes(data)
            return f"/media/{subdir}/{filename}"

        cover = (f"scale={self.W}:{self.H}:force_original_aspect_ratio=increase,"
                 f"crop={self.W}:{self.H},setsar=1,fps=30,format=yuv420p")
        for p in (patch("apps.creation.tasks._download", side_effect=fake_download),
                  patch("apps.core.utils.storage.save_artifact", side_effect=fake_save),
                  patch("apps.creation.pipeline.dispatcher.send_task", return_value=MagicMock(id="x")),
                  patch("apps.creation.tasks.W", self.W), patch("apps.creation.tasks.H", self.H),
                  patch("apps.creation.tasks._COVER", cover)):
            p.start()
            self.addCleanup(p.stop)
        owner = CustomUser.objects.create_user(username="fx", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.config = AgentConfig.objects.create(name="c", created_by=owner, style_params={})
        self.play = Play.objects.create(title="剧", created_by=owner, agent_config=self.config,
                                        audit_mode=AuditMode.NONE)
        self.ep = Episode.objects.create(play=self.play, episode_number=1)
        self.scene = Scene.objects.create(episode=self.ep, scene_number=1)

    def add_shot(self, n, dialogue="", est=1, voice=False):
        shot = Shot.objects.create(scene=self.scene, shot_number=n, dialogue=dialogue, duration_estimate=est)
        kw = dict(play=self.play, episode=self.ep, scene=self.scene, shot=shot, status=AssetStatus.READY)
        Asset.objects.create(asset_type=AssetType.IMAGE, artifact_url="fx://img", **kw)
        if voice:
            # 元数据故意写错，验证实际时长以 ffprobe 为准
            Asset.objects.create(asset_type=AssetType.AUDIO, artifact_url="fx://voice",
                                 metadata={"duration_seconds": 9.9}, **kw)
        return shot

    def compose(self):
        rec = TaskRecord.objects.create(play=self.play, episode=self.ep, task_type=TaskType.VIDEO_COMPOSE)
        tasks.compose_video(rec.pk)
        rec.refresh_from_db()
        self.assertEqual(rec.status, TaskStatus.SUCCESS, rec.error_message)
        return Asset.objects.get(episode=self.ep, asset_type=AssetType.VIDEO)


class ComposeSmokeTests(ComposeFixtureMixin, TestCase):
    """任务 3.5 / 4.4 / 5：真实 ffmpeg 端到端。"""

    def test_full_compose(self):
        AssetLibrary.objects.create(name="片头", asset_type=AssetType.INTRO, file_url="fx://intro", is_public=True)
        self.add_shot(1, "你好", est=1, voice=True)
        self.add_shot(2, est=1)
        Asset.objects.create(play=self.play, episode=self.ep, asset_type=AssetType.BGM,
                             status=AssetStatus.READY, artifact_url="fx://bgm")
        SubtitleTrack.objects.create(episode=self.ep, language="zh-CN", source_type=SubtitleSourceType.AUTO,
                                     status=SubtitleStatus.READY,
                                     subtitle_data=[{"start": 0, "end": 1, "text": "你好"}])
        self.config.style_params = {"watermark": {"type": "text", "text": "测试出品", "position": "bottom_left",
                                                  "font_size": 12, "margin": 4}}
        self.config.save()
        asset = self.compose()
        m = asset.metadata["media"]
        # 片头 1.0 | 镜头1 = 0.5 + 1.5 + 0.5 + 0.3 = 2.8 | 镜头2 = 1.0；总长 4.8 - 2*0.5 = 3.8
        self.assertAlmostEqual(m["duration"], 3.8, delta=0.3)
        self.assertEqual((m["width"], m["height"]), (self.W, self.H))
        self.assertTrue(m["has_audio"])
        self.assertGreater(m["size_bytes"], 0)
        self.assertEqual(asset.metadata["video_encoder"], "libx264")
        self.assertTrue((self.out_dir / f"play{self.play.pk}_ep1_poster.jpg").exists())
        video = self.out_dir / f"play{self.play.pk}_ep1.mp4"
        # 片头原声（0-0.5s）、对白（1.0-2.0s）、对白后的 BGM（3.0-3.5s）都必须可闻
        for start, dur in ((0.0, 0.5), (1.0, 1.0), (3.0, 0.5)):
            self.assertGreater(_mean_volume(video, start, dur), -40.0, f"{start}s 起音频静音")

    def test_image_watermark_cut_no_audio(self):
        self.add_shot(1, est=1)
        self.add_shot(2, est=1)
        self.config.style_params = {"transition_style": "cut",
                                    "watermark": {"type": "image", "url": "fx://wm", "scale": 0.3}}
        self.config.save()
        m = self.compose().metadata["media"]
        self.assertAlmostEqual(m["duration"], 2.0, delta=0.3)
        self.assertFalse(m["has_audio"])

    def test_i2v_clip_shorter_than_voice_is_padded_and_keeps_audio(self):
        provider = AIProvider.objects.create(name="V", provider_type="VIDEO", adapter_type="kling",
                                             base_url="https://v", api_key_encrypted="", priority=1)
        self.config.video_model = AIModel.objects.create(provider=provider, model_name="v",
                                                         display_name="V", model_type="VIDEO")
        self.config.save()
        self.add_shot(1, "台词", est=1, voice=True)
        gateway = MagicMock()
        gateway.call.return_value = {"url": "fx://intro", "duration_seconds": 5}  # 实际 1s，返回值不可信
        with patch("apps.creation.tasks._gateway", return_value=gateway):
            m = self.compose().metadata["media"]
        # 单段：1.5(配音) + 0.3 = 1.8；1s 片段冻结末帧补齐
        self.assertAlmostEqual(m["duration"], 1.8, delta=0.2)
        self.assertTrue(m["has_audio"])

    def test_probe_media(self):
        bad = self.out_dir / "bad.mp4"
        bad.write_bytes(b"not a video")
        with self.assertRaises(ValueError):
            tasks._probe_media(bad)
        info = tasks._probe_media(self.fx / "intro.mp4")
        self.assertTrue(info["has_audio"])
        self.assertAlmostEqual(info["duration"], 1.0, delta=0.1)


class ComposePlanTests(ComposeFixtureMixin, TestCase):
    """任务 2.3 / 4.3：片段时长取计划值、封面时间点（mock ffmpeg 检查参数）。"""

    def test_segment_length_follows_voice(self):
        # _probe_duration 按固定序列 mock，关闭去静音避免额外探测调用
        self.config.style_params = {"voice_trim_silence": False}
        self.config.save()
        self.add_shot(1, "台词", est=1, voice=True)
        scripts, calls = [], []

        def fake_ffmpeg(args, **kw):
            calls.append(list(args))
            opt = next((o for o in ("-/filter_complex", "-filter_complex_script") if o in args), None)
            if opt:
                scripts.append(Path(args[args.index(opt) + 1]).read_text(encoding="utf-8"))
            Path(args[-1]).write_bytes(b"x")

        with patch("apps.creation.tasks.run_ffmpeg", side_effect=fake_ffmpeg), \
                patch("apps.creation.tasks._probe_duration", side_effect=[4.0, 4.3]), \
                patch("apps.creation.tasks._decode_check"), \
                patch("apps.creation.tasks._probe_media", return_value={"duration": 4.3, "width": 1}):
            asset = self.compose()
        main, poster = calls[0], calls[1]
        # 单段：0 + 4.0(配音) + 0 + 0.3 = 4.3；zoompan d=1 逐帧输出，帧数由 -t 决定
        self.assertEqual(main[main.index("-t") + 1], "4.300")
        self.assertIn("zoompan=", scripts[0])
        self.assertEqual(asset.metadata["media"], {"duration": 4.3, "width": 1})
        self.assertEqual(poster[poster.index("-ss") + 1], "1.000")
        self.assertTrue(scripts)
