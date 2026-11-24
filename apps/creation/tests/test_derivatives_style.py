"""衍生品与风格化测试（openspec change: backend-ffmpeg-derivatives-style）。"""
import json
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.conf import settings
from django.test import SimpleTestCase, TestCase
from rest_framework.test import APITestCase

from apps.core.utils import media
from apps.creation import tasks
from apps.creation.models import (
    Asset, AssetDependency, AssetLibrary, AssetStatus, AssetType, Episode, EpisodeStatus,
    MemberRole, MemberStatus, Play, PlayMember, Scene, Shot, TaskRecord, TaskStatus,
    TaskType,
)
from apps.core.system.models import (
    CustomUser, UserRole,
)
from apps.distribution.models import (
    SubtitleSourceType, SubtitleStatus, SubtitleTrack,
)
from .test_ffmpeg_quality import ComposeFixtureMixin, _ff, _ffmpeg_ok, _graph, _seg


class PureFunctionTests(SimpleTestCase):
    """任务 4.1 / 5.1 / 6.1-6.3 / 7.1 / 9.1 / 9.2。"""

    def test_normalize_sound_effects(self):
        out = media.normalize_sound_effects([
            {"library_id": 3, "offset": -1, "volume": 9},
            {"url": "fx://a", "offset": 0.5, "volume": 0.8, "duration": 1},
            {"volume": 1}, "bad", {"library_id": True},
        ])
        self.assertEqual(len(out), 2)
        self.assertEqual((out[0]["offset"], out[0]["volume"], out[0]["duration"]), (0.0, 2.0, None))
        self.assertEqual((out[1]["url"], out[1]["duration"]), ("fx://a", 1.0))
        self.assertEqual(media.normalize_sound_effects(None), [])

    def test_color_grade(self):
        self.assertIsNone(media.parse_color_grade(None))
        self.assertIsNone(media.parse_color_grade({}))
        spec = media.parse_color_grade({"saturation": 10, "vignette": 0.5, "grain": 10})
        self.assertEqual(spec["saturation"], 3.0)
        full = media.parse_color_grade({"lut": "x.cube"})
        g = media.color_grade_graph(full, "L", "a", "b", 0)
        self.assertIn("lut3d=file=L", g)
        self.assertNotIn("split", g)
        half = media.parse_color_grade({"lut": "x.cube", "intensity": 0.5})
        g = media.color_grade_graph(half, "L", "a", "b", 0)
        self.assertIn("blend=all_expr='A*0.500+B*0.500'", g)
        # LUT 不可用且无其他效果 -> 无调色
        self.assertIsNone(media.color_grade_graph(full, None, "a", "b", 0))
        g = media.color_grade_graph(spec, None, "a", "b", 0)
        self.assertTrue(g.startswith("[a]eq=") and "vignette=" in g and "noise=alls=10" in g)

    def test_render_ass_default_unchanged(self):
        ass = media.render_ass([{"start": 0, "end": 1, "text": "a\nb"}], {}, "SimSun", 1080, 1920)
        self.assertIn("Style: Default,SimSun,48,&H00FFFFFF,&H000000FF,&H80000000,&H80000000,"
                      "0,0,0,0,100,100,0,0,1,1,0,2,40,40,80,1", ass)
        self.assertIn(",,a\\Nb", ass)

    def test_render_ass_new_styles(self):
        style = {"subtitle_style": {"box": True, "box_color": "#112233", "shadow": 2, "bold": True,
                                    "outline_color": "#FF0000"}}
        ass = media.render_ass([], style, "F", 1080, 1920)
        line = next(l for l in ass.splitlines() if l.startswith("Style:"))
        f = line.split(",")
        # OutlineColour/BackColour 均为底框色（BGR + 半透明），Bold=-1，BorderStyle=3，Shadow=2
        self.assertEqual((f[5], f[6], f[7], f[15], f[17]), ("&H80332211", "&H80332211", "-1", "3", "2"))
        ass = media.render_ass([], {"subtitle_style": {"outline_color": "#FF0000"}}, "F", 1, 1)
        self.assertIn("&H000000FF,&H000000FF,&H80000000", ass)  # Secondary, Outline(红), Back

    def test_karaoke_and_braces(self):
        self.assertEqual(media.karaoke_text("你好吗", 1.5), "{\\k50}你{\\k50}好{\\k50}吗")
        self.assertEqual(media.karaoke_text("ab", 0.25), "{\\k12}a{\\k13}b")
        self.assertEqual(media.karaoke_text("a\nb", 1.0), "{\\k50}a\\N{\\k50}b")
        self.assertEqual(media.ass_text("{内心}我不信"), "｛内心｝我不信")
        ass = media.render_ass([{"start": 0, "end": 1.5, "text": "你好吗"}],
                               {"subtitle_style": {"karaoke": True, "color": "#FFFFFF"}}, "F", 1, 1)
        self.assertIn("{\\k50}你", ass)
        self.assertIn("F,48,&H0000D7FF,&H00FFFFFF", ass)  # 已唱=高亮色、未唱=原色

    def test_build_timeline(self):
        metas = [{"role": "intro"}, {"role": "body", "shot_id": 7, "emotion": "震惊", "dialogue": "啊"},
                 {"role": "body", "shot_id": 8}]
        durs = [3.0, 4.0, 3.0]
        offsets, total, starts = tasks._xfade_timeline(durs, 0.5)
        tl = media.build_timeline(metas, starts, durs, 0.5, total)
        self.assertEqual([(e["start"], e["end"]) for e in tl], [(0.0, 2.5), (3.0, 6.0), (6.5, 9.0)])
        self.assertEqual((tl[1]["shot_id"], tl[1]["emotion"]), (7, "震惊"))
        self.assertEqual(media.keyframe_args(2), ["-force_key_frames", "expr:gte(t,n_forced*2)"])

    def test_select_trailer_clips(self):
        tl = [{"index": i, "role": "body", "shot_id": i, "start": i * 5.0, "end": i * 5 + 5.0,
               "emotion": "震惊" if i == 3 else "", "dialogue": "真的？" if i == 5 else ""} for i in range(10)]
        tl.insert(0, {"index": -1, "role": "intro", "shot_id": None, "start": 0, "end": 3})
        clips = media.select_trailer_clips(tl, 15, 30, 6)
        self.assertEqual(clips[0]["shot_id"], 3)  # 钩子 = 最高分
        rest = [c["start"] for c in clips[1:]]
        self.assertEqual(rest, sorted(rest))
        self.assertIn(5, [c["shot_id"] for c in clips])
        self.assertTrue(15 <= sum(c["duration"] for c in clips) <= 30)
        self.assertTrue(all(c["duration"] <= 6 for c in clips))
        short = [{"index": 0, "role": "body", "shot_id": 1, "start": 0, "end": 4},
                 {"index": 1, "role": "body", "shot_id": 2, "start": 4, "end": 10, "emotion": "愤怒"}]
        self.assertEqual({c["shot_id"] for c in media.select_trailer_clips(short, 15, 30, 6)}, {1, 2})
        self.assertEqual(media.select_trailer_clips([], 15, 30), [])

    def test_poster_candidates(self):
        tl = [{"index": i, "role": "body", "shot_id": i, "start": float(i), "end": i + 1.0,
               "emotion": "哭" if i in (2, 4) else ""} for i in range(6)]
        picked = media.pick_poster_candidates(tl, 4)
        self.assertEqual(len(picked), 4)
        self.assertTrue({2, 4} <= {p["shot_id"] for p in picked})
        self.assertEqual([p["start"] for p in picked], sorted(p["start"] for p in picked))
        uniform = media.pick_poster_candidates([], 4, 8.0)
        self.assertEqual([p["start"] for p in uniform], [0.0, 2.0, 4.0, 6.0])

    def test_gif_hls_preview_size(self):
        self.assertIn("palettegen", media.gif_filter())
        args = media.hls_args(4, "seg_%03d.ts", "index.m3u8")
        self.assertEqual(args[:2], ["-c", "copy"])
        self.assertIn("vod", args)
        self.assertEqual(media.preview_size(1080, 1920, None)[:2], (360, 640))
        self.assertEqual(media.preview_size(180, 320, 0.5)[:2], (90, 160))


class FiltergraphStyleTests(SimpleTestCase):
    """任务 2.2 / 4.3 / 5.2。"""

    def test_sfx_bus_not_ducked(self):
        segs = [_seg("body", 4.0, kind="image"), _seg("body", 4.5, kind="image")]
        sfx = {1: [{"path": "door.mp3", "offset": 0.5, "volume": 0.8, "duration": None}]}
        durs = [4.0, 4.5]
        offsets, total, starts = tasks._xfade_timeline(durs, 0.5)
        script, args, has_audio, _ = tasks._build_filtergraph(
            segs, None, {0: "v.mp3"}, "bgm.mp3", {}, "", 0.5, "fade", offsets, total, starts, sfx_map=sfx)
        # Shot 2 可见起点 starts[1]=4.0s + 0.5 偏移 = 4.5s；可见区间末尾为总时长 8.0 -> 裁剪 3.5s
        self.assertIn("volume=0.800,atrim=0:3.500,asetpts=PTS-STARTPTS,adelay=4500:all=1[fx", script)
        self.assertIn("door.mp3", args)
        sc = next(p for p in script.split(";") if "sidechaincompress" in p)
        self.assertNotIn("[fx", sc)
        self.assertRegex(script, r"\[voice_mix\]\[bed_duck\]\[fx\d+\]amix")

    def test_sfx_beyond_visible_skipped(self):
        segs = [_seg("body", 2.0, kind="image")]
        sfx = {0: [{"path": "x.mp3", "offset": 5.0, "volume": 1.0, "duration": None}]}
        with self.assertLogs("apps.creation.tasks", "WARNING"):
            script, args, has_audio, _ = tasks._build_filtergraph(
                segs, None, {}, None, {}, "", 0.0, "fade", [], 2.0, [0.0], sfx_map=sfx)
        self.assertNotIn("x.mp3", args)
        self.assertFalse(has_audio)

    def test_grade_body_only(self):
        segs = [_seg("intro", 3.0), _seg("body", 4.0, kind="image"), _seg("outro", 2.0)]
        durs = [3.0, 4.0, 2.0]
        offsets, total, starts = tasks._xfade_timeline(durs, 0.5)
        grade = {"spec": media.parse_color_grade({"lut": "x", "saturation": 1.2}), "lut": "C:/t/grade.cube"}
        script, *_ = tasks._build_filtergraph(segs, None, {}, None, {}, "", 0.5, "fade",
                                              offsets, total, starts, grade=grade)
        self.assertIn("[1:v]null[g1]", script)
        self.assertIn("[g1]lut3d=file=C\\\\:/t/grade.cube", script)  # 与 subtitles 同样两层转义
        self.assertIn("[0:v]null[v0]", script)
        self.assertIn("[2:v]null[v2]", script)
        no_lut = {"spec": grade["spec"], "lut": None}
        script, *_ = tasks._build_filtergraph(segs, None, {}, None, {}, "", 0.5, "fade",
                                              offsets, total, starts, grade=no_lut)
        self.assertIn("[g1]eq=", script)
        self.assertNotIn("lut3d", script)

    def test_watermark_scaled_on_preview_canvas(self):
        segs = [_seg("body", 3.0, kind="image")]
        wm = {"spec": media.parse_watermark({"type": "image", "url": "u", "scale": 0.15, "margin": 30}),
              "path": "wm.png"}
        canvas = {"w": 360, "h": 640, "fps": 30, "scale": 1 / 3, "full": False}
        script, *_ = tasks._build_filtergraph(segs, None, {}, None, {}, "", 0.0, "fade", [], 3.0, [0.0],
                                              watermark=wm, canvas=canvas)
        self.assertIn("scale=54:-2", script)  # 360 * 0.15
        self.assertIn("overlay=x=main_w-overlay_w-10:y=10", script)  # 边距 30/3
        full, *_ = _graph(segs, watermark=wm, td=0.0)
        self.assertIn("scale=162:-2", full)  # 1080 * 0.15

    def test_canvas(self):
        self.assertEqual(tasks._canvas()["w"], tasks.W)
        self.assertEqual(tasks._cover(None), tasks._COVER)
        c = tasks._canvas({}, preview=True)
        self.assertEqual((c["w"], c["h"]), (360, 640))
        self.assertIn("scale=360:640", tasks._cover(c))
        self.assertIn("s=360x640", tasks._kenburns_zoompan({}, 0, 30, c))


class FallbackTests(TestCase):
    """任务 3.1 / 4.2 / 5.2：素材问题降级而非失败（mock ffmpeg / 下载）。"""

    def test_trim_failure_falls_back(self):
        err = subprocess.CalledProcessError(1, "ffmpeg")
        with patch("apps.creation.tasks.run_ffmpeg", side_effect=err), self.assertLogs("apps.creation.tasks", "WARNING"):
            self.assertEqual(tasks._trim_voice(Path(tempfile.gettempdir()), "v.mp3"), "v.mp3")

    def test_missing_sfx_library_skipped(self):
        owner = CustomUser.objects.create_user(username="fb", password="Passw0rd!123")
        ep = Episode.objects.create(play=Play.objects.create(title="p", created_by=owner), episode_number=1)
        shot = Shot.objects.create(scene=Scene.objects.create(episode=ep, scene_number=1), shot_number=1,
                                   sound_effects=[{"library_id": 999}])
        with self.assertLogs("apps.creation.tasks", "WARNING"):
            self.assertEqual(tasks._resolve_sound_effects(Path(tempfile.gettempdir()), shot), [])

    def test_lut_failure_keeps_other_grading(self):
        style = {"color_grade": {"lut": "https://x/a.cube", "saturation": 1.2}}
        with patch("apps.creation.tasks._download", side_effect=OSError("404")), \
                self.assertLogs("apps.creation.tasks", "WARNING"):
            grade = tasks._resolve_color_grade(Path(tempfile.gettempdir()), style)
        self.assertIsNone(grade["lut"])
        self.assertEqual(grade["spec"]["saturation"], 1.2)
        self.assertIsNone(tasks._resolve_color_grade(Path(tempfile.gettempdir()), {}))


def _identity_cube(path):
    rows = ["LUT_3D_SIZE 2"]
    for b in (0, 1):
        for g in (0, 1):
            for r in (0, 1):
                rows.append(f"{r}.0 {g}.0 {b}.0")
    Path(path).write_text("\n".join(rows) + "\n", encoding="ascii")


class StyleFixtureMixin(ComposeFixtureMixin):
    FILES = {**ComposeFixtureMixin.FILES, "sfx": "sfx.mp3", "lut": "id.cube", "quiet": "quiet.mp3"}

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        _ff("-f", "lavfi", "-i", "sine=frequency=880:duration=0.5", "-c:a", "libmp3lame", str(cls.fx / "sfx.mp3"))
        _identity_cube(cls.fx / "id.cube")
        # 0.6s 静音 + 1.5s 正弦 + 0.8s 静音
        _ff("-f", "lavfi", "-t", "0.6", "-i", "anullsrc=r=48000:cl=mono",
            "-f", "lavfi", "-i", "sine=frequency=440:duration=1.5:sample_rate=48000",
            "-f", "lavfi", "-t", "0.8", "-i", "anullsrc=r=48000:cl=mono",
            "-filter_complex", "[0:a][1:a][2:a]concat=n=3:v=0:a=1", "-c:a", "libmp3lame",
            str(cls.fx / "quiet.mp3"))


def _keyframe_times(path):
    proc = subprocess.run([settings.FFPROBE_BIN, "-v", "error", "-select_streams", "v", "-skip_frame", "nokey",
                           "-show_entries", "frame=pts_time", "-of", "csv=p=0", str(path)],
                          capture_output=True, text=True, timeout=60)
    # csv 行可能带尾随逗号（side data 列），取首列
    return [float(x.split(",")[0]) for x in proc.stdout.split() if x.split(",")[0].strip()]


class StyledComposeTests(StyleFixtureMixin, TestCase):
    """任务 3.2 / 4.4 / 5.3 / 6 / 7.2 / 8.1：真实 ffmpeg。"""

    def test_trim_voice_real(self):
        out = tasks._trim_voice(self.out_dir, self.fx / "quiet.mp3")
        self.assertNotEqual(Path(out), self.fx / "quiet.mp3")
        self.assertTrue(1.4 <= tasks._probe_duration(out) <= 1.7, tasks._probe_duration(out))

    def test_styled_compose(self):
        lib = AssetLibrary.objects.create(name="门铃", asset_type=AssetType.SFX, file_url="fx://sfx", is_public=True)
        s1 = self.add_shot(1, "你好", est=2, voice=True)
        s1.emotion_tag = "震惊"
        s1.save(update_fields=["emotion_tag"])
        s2 = self.add_shot(2, est=2)
        s2.sound_effects = [{"library_id": lib.pk, "offset": 0.2, "volume": 1.0}]
        s2.save(update_fields=["sound_effects"])
        SubtitleTrack.objects.create(episode=self.ep, language="zh-CN", source_type=SubtitleSourceType.AUTO,
                                     status=SubtitleStatus.READY,
                                     subtitle_data=[{"start": 0, "end": 1, "text": "{你}好"}])
        self.config.style_params = {
            "gop_seconds": 1,
            "color_grade": {"lut": "fx://lut", "intensity": 0.5, "vignette": 0.5, "grain": 10},
            "subtitle_style": {"karaoke": True, "box": True, "shadow": 1, "font_size": 12},
        }
        self.config.save()
        asset = self.compose()
        # 镜头1 = 0 + 1.5 + 0.5 + 0.3 = 2.3 | 镜头2 = 2.0；总长 3.8
        # 镜头2 入场转场 1.8-2.3s，可见起点 = 转场结束 2.3s；音效偏移 0.2 -> 2.5-3.0s
        tl = asset.metadata["timeline"]
        self.assertEqual([e["shot_id"] for e in tl], [s1.pk, s2.pk])
        self.assertEqual((tl[0]["emotion"], tl[0]["end"], tl[1]["start"]), ("震惊", 1.8, 2.3))
        video = self.out_dir / f"play{self.play.pk}_ep1.mp4"
        keys = _keyframe_times(video)
        for t in (0.0, 1.0, 2.0, 3.0):
            self.assertTrue(any(abs(k - t) < 0.05 for k in keys), (t, keys))
        from .test_ffmpeg_quality import _mean_volume
        self.assertGreater(_mean_volume(video, 2.55, 0.35), -40.0, "音效不可闻")
        self.assertLess(_mean_volume(video, 1.9, 0.5), -40.0, "音效提前播放")  # 配音 1.5s 已结束

    def test_preview_render(self):
        self.add_shot(1, "你好", est=1, voice=True)
        self.config.style_params = {"preview_scale": 0.5}
        self.config.save()
        status_before = self.ep.status
        rec = TaskRecord.objects.create(play=self.play, episode=self.ep, task_type=TaskType.PREVIEW_RENDER)
        tasks.render_preview(rec.pk)
        rec.refresh_from_db()
        self.assertEqual(rec.status, TaskStatus.SUCCESS, rec.error_message)
        preview = Asset.objects.get(episode=self.ep, asset_type=AssetType.PREVIEW)
        self.assertEqual((preview.metadata["media"]["width"], preview.metadata["media"]["height"]), (90, 160))
        self.assertTrue(preview.artifact_url.startswith("/media/previews/"))
        self.ep.refresh_from_db()
        self.assertEqual((self.ep.status, self.ep.video_url, self.ep.poster_url), (status_before, "", ""))
        self.assertFalse(Asset.objects.filter(episode=self.ep, asset_type=AssetType.VIDEO).exists())

    def test_preview_skips_paid_i2v(self):
        self.add_shot(1, est=1)
        gateway = MagicMock()
        with patch("apps.creation.tasks._gateway", return_value=gateway), \
                patch("apps.creation.tasks._video_segment") as vseg:
            rec = TaskRecord.objects.create(play=self.play, episode=self.ep, task_type=TaskType.PREVIEW_RENDER)
            tasks.render_preview(rec.pk)
        vseg.assert_not_called()


def _tl(shot_ids, emotions, seg=3.0):
    return [{"index": i, "role": "body", "shot_id": sid, "start": i * seg, "end": (i + 1) * seg,
             "emotion": emotions[i], "dialogue": ""} for i, sid in enumerate(shot_ids)]


class DerivativeFixtureMixin:
    def setUp(self):
        super().setUp()
        p = patch("apps.creation.pipeline.dispatcher.send_task", return_value=MagicMock(id="x"))
        p.start()
        self.addCleanup(p.stop)
        self.owner = CustomUser.objects.create_user(username="dv", password="Passw0rd!123")
        # 计费派发闸接线后渲染类预扣按 60s×单价（约数千分）——补测试余额防闸拒绝
        from apps.billing.services.wallet import grant
        grant(self.owner, 1_000_000, ref_kind="test", ref_id=f"dv-{id(self)}")
        self.play = Play.objects.create(title="剧", created_by=self.owner)
        PlayMember.objects.get_or_create(play=self.play, user=self.owner,
                                         defaults={"role": MemberRole.OWNER, "status": MemberStatus.ACTIVE})
        self.ep = Episode.objects.create(play=self.play, episode_number=1)
        scene = Scene.objects.create(episode=self.ep, scene_number=1)
        self.shots = [Shot.objects.create(scene=scene, shot_number=i + 1) for i in range(4)]

    def make_video(self, timeline=True, **meta):
        ids = [s.pk for s in self.shots]
        md = {"duration_seconds": 12.0, "media": {"has_audio": True},
              "timeline": _tl(ids, ["", "震惊", "", "愤怒"]) if timeline else [], **meta}
        return Asset.objects.create(play=self.play, episode=self.ep, asset_type=AssetType.VIDEO,
                                    status=AssetStatus.READY, artifact_url="fx://master", metadata=md)

    def run_task(self, video, kinds=None):
        rec = TaskRecord.objects.create(play=self.play, episode=self.ep, asset=video, task_type=TaskType.DERIVATIVES,
                                        input_summary={"video_asset_id": video.pk, "kinds": kinds or []})
        try:
            tasks.generate_derivatives(rec.pk)
        except Exception:  # noqa: BLE001  _run 会重新抛出；状态以 TaskRecord 为准
            pass
        rec.refresh_from_db()
        return rec


class DerivativesTaskTests(DerivativeFixtureMixin, TestCase):
    """任务 9.3：任务骨架（mock 各 builder，不依赖 ffmpeg）。"""

    def setUp(self):
        super().setUp()
        for p in (patch("apps.creation.tasks._download", side_effect=lambda url, path, play_pk=None: path),
                  patch("apps.creation.tasks._probe_duration", return_value=12.0)):
            p.start()
            self.addCleanup(p.stop)

    def test_partial_failure(self):
        video = self.make_video()
        with patch("apps.creation.tasks._build_gif", side_effect=RuntimeError("gif boom")), \
                patch("apps.creation.tasks._build_hls", return_value={"url": "/media/hls/x/index.m3u8"}), \
                self.assertLogs("apps.creation.tasks", "WARNING"):
            rec = self.run_task(video, ["gif", "hls"])
        self.assertEqual(rec.status, TaskStatus.SUCCESS, rec.error_message)
        self.assertIn("gif boom", rec.output_summary["errors"]["gif"])
        video.refresh_from_db()
        self.assertEqual(video.metadata["derivatives"]["hls_url"], "/media/hls/x/index.m3u8")
        self.assertNotIn("gif_url", video.metadata["derivatives"])
        self.assertIn("timeline", video.metadata)  # 合并写入不丢其他键

    def test_all_failed(self):
        video = self.make_video()
        with patch("apps.creation.tasks._build_hls", side_effect=RuntimeError("x")), \
                self.assertLogs("apps.creation.tasks", "WARNING"):
            rec = self.run_task(video, ["hls"])
        self.assertEqual(rec.status, TaskStatus.FAILED)

    def test_superseded_master_discarded(self):
        video = self.make_video()

        def supersede(*a, **k):
            Asset.objects.filter(pk=video.pk).update(status=AssetStatus.SUPERSEDED)
            return {"url": "/media/hls/x/index.m3u8"}

        with patch("apps.creation.tasks._build_hls", side_effect=supersede), \
                self.assertLogs("apps.creation.tasks", "WARNING"):
            rec = self.run_task(video, ["hls"])
        self.assertTrue(rec.output_summary["discarded"])
        video.refresh_from_db()
        self.assertNotIn("derivatives", video.metadata)

    def test_not_ready_skipped(self):
        video = self.make_video()
        Asset.objects.filter(pk=video.pk).update(status=AssetStatus.SUPERSEDED)
        rec = self.run_task(video)
        self.assertEqual(rec.output_summary, {"skipped": True, "reason": "video_not_ready"})

    def test_write_does_not_rerun_video_ready(self):
        video = self.make_video()
        with patch("apps.creation.tasks._build_hls", return_value={"url": "u"}), \
                patch("apps.creation.pipeline.dispatcher._after_video_ready") as after:
            self.run_task(video, ["hls"])
        after.assert_not_called()

    def test_trailer_requires_timeline(self):
        video = self.make_video(timeline=False)
        with patch("apps.creation.tasks._build_hls", return_value={"url": "u"}), \
                self.assertLogs("apps.creation.tasks", "WARNING"):
            rec = self.run_task(video, ["trailer", "hls"])
        self.assertEqual(rec.status, TaskStatus.SUCCESS)
        self.assertIn("重新合成", rec.output_summary["errors"]["trailer"])

    def test_kinds_default(self):
        self.assertEqual(tasks.derivative_kinds({}), list(tasks.DERIVATIVE_KINDS))
        self.assertEqual(tasks.derivative_kinds({"derivatives": ["hls", "gif"]}), ["gif", "hls"])
        self.assertEqual(tasks.derivative_kinds({"derivatives": ["hls"]}, ["posters"]), ["posters"])


class DerivativesRealTests(DerivativeFixtureMixin, TestCase):
    """任务 9.4-9.6：真实 ffmpeg。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not _ffmpeg_ok():
            raise unittest.SkipTest("ffmpeg 不可用")
        cls.fx = Path(tempfile.mkdtemp(prefix="sp_dv_"))
        _ff("-f", "lavfi", "-i", "testsrc2=s=180x320:r=30:d=12", "-f", "lavfi", "-i", "sine=frequency=440:duration=12",
            "-c:v", "libx264", "-pix_fmt", "yuv420p", "-force_key_frames", "expr:gte(t,n_forced*2)",
            "-c:a", "aac", "-shortest", str(cls.fx / "master.mp4"))

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "fx", ""), ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.out = Path(tempfile.mkdtemp(prefix="sp_dvo_"))
        self.addCleanup(shutil.rmtree, self.out, True)
        self.saved = []

        def fake_save(data, filename, subdir=""):
            d = self.out / subdir
            d.mkdir(parents=True, exist_ok=True)
            (d / filename).write_bytes(data)
            self.saved.append(f"{subdir}/{filename}")
            return f"/media/{subdir}/{filename}"

        for p in (patch("apps.creation.tasks._download",
                        side_effect=lambda url, path, play_pk=None: shutil.copyfile(self.fx / "master.mp4", path) or path),
                  patch("apps.core.utils.storage.save_artifact", side_effect=fake_save)):
            p.start()
            self.addCleanup(p.stop)
        self.play.agent_config = None

    def test_all_derivatives(self):
        video = self.make_video()
        with patch("apps.creation.tasks._style",
                   return_value={"trailer": {"min_seconds": 5, "max_seconds": 8}, "gif_seconds": 2}):
            rec = self.run_task(video)
        self.assertEqual(rec.status, TaskStatus.SUCCESS, rec.error_message)
        self.assertEqual(rec.output_summary["errors"], {})
        trailer = Asset.objects.get(episode=self.ep, asset_type=AssetType.TRAILER)
        clips = trailer.metadata["clips"]
        self.assertEqual(clips[0]["shot_id"], self.shots[1].pk)  # 震惊 / 愤怒 同分，取较早者为钩子
        self.assertIn(self.shots[3].pk, [c["shot_id"] for c in clips])
        self.assertAlmostEqual(trailer.metadata["duration_seconds"], sum(c["duration"] for c in clips), delta=0.2)
        self.assertTrue(trailer.metadata["media"]["has_audio"])
        self.assertTrue(AssetDependency.objects.filter(upstream=video, downstream=trailer).exists())

        video.refresh_from_db()
        d = video.metadata["derivatives"]
        gif = tasks._probe_media(self.out / "previews" / Path(d["gif_url"]).name)
        self.assertLessEqual(gif["width"], 320)
        self.assertEqual(len(d["posters"]), 4)
        self.assertEqual({p["shot_id"] for p in d["posters"]}, {s.pk for s in self.shots})

        playlist = self.out / "hls" / f"play{self.play.pk}_ep1_v{video.pk}" / "index.m3u8"
        text = playlist.read_text(encoding="utf-8")
        self.assertIn("#EXT-X-PLAYLIST-TYPE:VOD", text)
        hls = [s for s in self.saved if s.startswith("hls/")]
        self.assertGreaterEqual(len(hls), 3)  # ≥2 分片 + m3u8
        self.assertEqual({s.rsplit("/", 1)[0] for s in hls}, {f"hls/play{self.play.pk}_ep1_v{video.pk}"})
        self.assertAlmostEqual(tasks._probe_duration(playlist), 12.0, delta=0.5)

        # 母版重做 -> 预告片随之失效
        Asset.objects.create(play=self.play, episode=self.ep, asset_type=AssetType.VIDEO,
                             status=AssetStatus.READY, artifact_url="fx://master2")
        trailer.refresh_from_db()
        self.assertEqual(trailer.status, AssetStatus.SUPERSEDED)


class EpisodeEndpointTests(DerivativeFixtureMixin, APITestCase):
    """任务 8.2 / 9.7。"""

    def setUp(self):
        super().setUp()
        self.viewer = CustomUser.objects.create_user(username="dvv", password="Passw0rd!123", role=UserRole.VIEWER)
        PlayMember.objects.create(play=self.play, user=self.viewer, role=MemberRole.VIEWER,
                                  status=MemberStatus.ACTIVE)

    def post(self, user, action, data=None):
        self.client.force_authenticate(user)
        return self.client.post(f"/api/episodes/{self.ep.pk}/{action}/", data or {}, format="json")

    def test_preview_endpoint(self):
        self.assertEqual(self.post(self.viewer, "preview").status_code, 403)
        resp = self.post(self.owner, "preview")
        self.assertEqual(resp.status_code, 202)
        rec = TaskRecord.objects.get(pk=resp.data["task_record_id"])
        self.assertEqual((rec.task_type, rec.episode_id), (TaskType.PREVIEW_RENDER, self.ep.pk))
        self.assertEqual(self.post(self.owner, "preview").status_code, 400)
        TaskRecord.objects.filter(pk=rec.pk).update(status=TaskStatus.SUCCESS)
        self.assertEqual(self.post(self.owner, "preview").status_code, 202)

    def test_derivatives_endpoint(self):
        self.assertEqual(self.post(self.owner, "derivatives").status_code, 400)  # 无成片
        video = self.make_video()
        self.assertEqual(self.post(self.viewer, "derivatives").status_code, 403)
        self.assertEqual(self.post(self.owner, "derivatives", {"kinds": ["nope"]}).status_code, 400)
        resp = self.post(self.owner, "derivatives", {"kinds": ["hls"]})
        self.assertEqual(resp.status_code, 202)
        rec = TaskRecord.objects.get(pk=resp.data["task_record_id"])
        # Stage A2 派发快照：生成类任务的 input_summary 附带 generation 快照（不可变配置定格）
        self.assertEqual(rec.input_summary["video_asset_id"], video.pk)
        self.assertEqual(rec.input_summary["kinds"], ["hls"])
        self.assertIn("generation", rec.input_summary)
        self.assertEqual(self.post(self.owner, "derivatives").status_code, 400)  # 进行中

    def test_poster_select(self):
        video = self.make_video(derivatives={"posters": [{"url": "/media/posters/a.jpg", "time": 1, "shot_id": 1}]})
        self.assertEqual(self.post(self.viewer, "poster", {"url": "/media/posters/a.jpg"}).status_code, 403)
        bad = self.post(self.owner, "poster", {"url": "https://evil/x.jpg"})
        self.assertEqual(bad.status_code, 400)
        self.ep.refresh_from_db()
        self.assertEqual(self.ep.poster_url, "")
        ok = self.post(self.owner, "poster", {"url": "/media/posters/a.jpg"})
        self.assertEqual(ok.status_code, 200)
        self.ep.refresh_from_db()
        video.refresh_from_db()
        self.assertEqual((self.ep.poster_url, video.metadata["poster_url"]), ("/media/posters/a.jpg",) * 2)

    def test_preview_asset_does_not_touch_pipeline(self):
        status_before = self.ep.status
        with patch("apps.creation.pipeline.dispatcher.dispatch_task") as dispatch:
            Asset.objects.create(play=self.play, episode=self.ep, asset_type=AssetType.PREVIEW,
                                 status=AssetStatus.READY, artifact_url="/media/previews/p.mp4")
            Asset.objects.create(play=self.play, episode=self.ep, asset_type=AssetType.TRAILER,
                                 status=AssetStatus.READY, artifact_url="/media/trailers/t.mp4")
        dispatch.assert_not_called()
        self.ep.refresh_from_db()
        self.assertEqual((self.ep.status, self.ep.current_video_asset_id), (status_before, None))
        self.assertNotEqual(self.ep.status, EpisodeStatus.COMPLETED)


class _GW:
    def __init__(self, result):
        self.result = result

    def call(self, *a, **k):
        return dict(self.result)


class IngestValidationTests(TestCase):
    """任务 10.1 / 10.2。"""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not _ffmpeg_ok():
            raise unittest.SkipTest("ffmpeg 不可用")
        cls.fx = Path(tempfile.mkdtemp(prefix="sp_in_"))
        _ff("-f", "lavfi", "-i", "sine=frequency=440:duration=2", "-c:a", "libmp3lame", str(cls.fx / "a.mp3"))
        _ff("-f", "lavfi", "-i", "color=c=red:s=64x32", "-frames:v", "1", str(cls.fx / "i.png"))
        cls.mp3 = (cls.fx / "a.mp3").read_bytes()
        cls.png = (cls.fx / "i.png").read_bytes()

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(getattr(cls, "fx", ""), ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        for p in (patch("apps.creation.pipeline.dispatcher.send_task", return_value=MagicMock(id="x")),
                  patch("apps.core.utils.storage.save_artifact", side_effect=lambda d, f, s="": f"/media/{s}/{f}")):
            p.start()
            self.addCleanup(p.stop)
        owner = CustomUser.objects.create_user(username="in", password="Passw0rd!123")
        self.play = Play.objects.create(title="p", created_by=owner)
        self.ep = Episode.objects.create(play=self.play, episode_number=1)
        self.scene = Scene.objects.create(episode=self.ep, scene_number=1)
        self.shot = Shot.objects.create(scene=self.scene, shot_number=1, dialogue="台词")

    def run_gen(self, task, task_type, result):
        rec = TaskRecord.objects.create(play=self.play, episode=self.ep, scene=self.scene, shot=self.shot,
                                        task_type=task_type)
        with patch("apps.creation.tasks._gateway", return_value=_GW(result)):
            try:
                task(rec.pk)
            except Exception:  # noqa: BLE001
                pass
        rec.refresh_from_db()
        return rec

    def test_validate_media_bytes(self):
        self.assertTrue(tasks._validate_media_bytes(self.mp3, "audio")["has_audio"])
        self.assertEqual(tasks._validate_media_bytes(self.png, "image")["width"], 64)
        for data, kind in ((b"AUDIO", "audio"), (self.png, "audio"), (b"IMG", "image")):
            with self.assertRaises(ValueError):
                tasks._validate_media_bytes(data, kind)

    def test_tts_duration_from_probe(self):
        rec = self.run_gen(tasks.generate_tts, TaskType.TTS_GEN, {"audio_bytes": self.mp3, "duration_seconds": 5})
        self.assertEqual(rec.status, TaskStatus.SUCCESS, rec.error_message)
        asset = Asset.objects.get(shot=self.shot, asset_type=AssetType.AUDIO)
        self.assertAlmostEqual(asset.metadata["duration_seconds"], 2.0, delta=0.1)
        self.assertTrue(asset.metadata["media_validated"])

    def test_tts_bad_bytes_fails(self):
        with self.assertLogs("apps.creation.tasks", "ERROR"):
            rec = self.run_gen(tasks.generate_tts, TaskType.TTS_GEN, {"audio_bytes": b"AUDIO", "duration_seconds": 2})
        self.assertEqual(rec.status, TaskStatus.FAILED)
        self.assertIn("无法解码", rec.error_message)
        self.assertFalse(Asset.objects.filter(asset_type=AssetType.AUDIO).exists())

    def test_tts_url_only_not_validated(self):
        rec = self.run_gen(tasks.generate_tts, TaskType.TTS_GEN, {"url": "https://x/a.mp3", "duration_seconds": 2})
        self.assertEqual(rec.status, TaskStatus.SUCCESS)
        asset = Asset.objects.get(shot=self.shot, asset_type=AssetType.AUDIO)
        self.assertEqual((asset.metadata["duration_seconds"], asset.metadata["media_validated"]), (2, False))

    def test_image_bytes(self):
        rec = self.run_gen(tasks.generate_image, TaskType.IMAGE_GEN, {"url": "", "image_bytes": self.png})
        self.assertEqual(rec.status, TaskStatus.SUCCESS, rec.error_message)
        asset = Asset.objects.get(shot=self.shot, asset_type=AssetType.IMAGE)
        self.assertEqual(asset.metadata["media"]["width"], 64)
        Asset.objects.all().delete()
        with self.assertLogs("apps.creation.tasks", "ERROR"):
            rec = self.run_gen(tasks.generate_image, TaskType.IMAGE_GEN, {"url": "", "image_bytes": b"IMG"})
        self.assertEqual(rec.status, TaskStatus.FAILED)
        self.assertFalse(Asset.objects.filter(asset_type=AssetType.IMAGE).exists())
