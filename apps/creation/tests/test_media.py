"""media.py 纯函数单测（openspec change: backend-ffmpeg-quality-distribution）。"""
import random

from django.test import SimpleTestCase

from apps.core.utils import media
from apps.creation.tasks import _xfade_timeline


class ProbeParseTests(SimpleTestCase):
    def test_parse_probe(self):
        data = {
            "streams": [
                {"codec_type": "video", "codec_name": "h264", "width": 1080, "height": 1920,
                 "avg_frame_rate": "30000/1001"},
                {"codec_type": "audio", "codec_name": "aac"},
            ],
            "format": {"duration": "12.345", "size": "2048000", "bit_rate": "1327000"},
        }
        m = media.parse_probe(data)
        self.assertEqual((m["width"], m["height"], m["vcodec"], m["acodec"]), (1080, 1920, "h264", "aac"))
        self.assertAlmostEqual(m["fps"], 29.97, places=2)
        self.assertEqual((m["duration"], m["size_bytes"], m["bit_rate_kbps"]), (12.345, 2048000, 1327))
        self.assertTrue(m["has_audio"])

    def test_parse_probe_audio_only(self):
        m = media.parse_probe({"streams": [{"codec_type": "audio", "duration": "2.5"}], "format": {}})
        self.assertEqual(m["duration"], 2.5)
        self.assertFalse(m["has_video"])


class SegmentDurationTests(SimpleTestCase):
    """任务 2.2 / 2.6。"""

    def test_spec_example_first_shot(self):
        # 首段：预估 3s、配音 5.2s、fade 0.5s、留白 0.3s -> 0 + 5.2 + 0.5 + 0.3 = 6.0
        out = media.plan_segment_durations([3, 3], [5.2, 0], [False, False], 0.5, 0.3)
        self.assertEqual(out[0], 6.0)
        self.assertEqual(out[1], 3.0)

    def test_middle_shot_reserves_both_overlaps(self):
        out = media.plan_segment_durations([3, 3, 3], [0, 5.2, 0], [False] * 3, 0.5, 0.3)
        self.assertEqual(out[1], 6.5)

    def test_cut_and_fixed(self):
        out = media.plan_segment_durations([2, 3, 4], [0, 5.0, 0], [True, False, True], 0.0, 0.3)
        self.assertEqual(out, [2.0, 5.3, 4.0])

    def test_estimate_wins_when_longer(self):
        self.assertEqual(media.plan_segment_durations([8], [2.0], [False], 0.5, 0.3), [8.0])

    def test_voice_never_overlaps_next_transition_or_voice(self):
        rnd = random.Random(42)
        for _ in range(300):
            n = rnd.randint(1, 6)
            td = rnd.choice([0.0, 0.3, 0.5, 1.0])
            fixed = [rnd.random() < 0.2 for _ in range(n)]
            bases = [rnd.uniform(1.5, 6) if f else rnd.choice([0, 1, 3, 5]) for f in fixed]
            voices = [0.0 if f else rnd.choice([0.0, rnd.uniform(0.5, 9)]) for f in fixed]
            durs = media.plan_segment_durations(bases, voices, fixed, td if n > 1 else 0.0, 0.3)
            eff_td = td if n > 1 else 0.0
            offsets, _, starts = _xfade_timeline(durs, eff_td)
            for i in range(n):
                if fixed[i] or not voices[i]:
                    continue
                voice_end = starts[i] + voices[i]
                if i < n - 1:
                    # 出场转场开始于 offsets[i]；下一配音开始于 starts[i+1]
                    self.assertLessEqual(voice_end, offsets[i] + 1e-6, (durs, voices, td))
                    self.assertLessEqual(voice_end, starts[i + 1] + 1e-6)
                else:
                    self.assertLessEqual(voice_end, sum(durs) - (n - 1) * eff_td + 1e-6)


class EncoderArgsTests(SimpleTestCase):
    """任务 4.1。"""

    def test_libx264(self):
        self.assertEqual(media.encoder_args("libx264", 23, "medium"),
                         ["-c:v", "libx264", "-crf", "23", "-preset", "medium"])

    def test_nvenc_has_no_crf(self):
        args = media.encoder_args("h264_nvenc", 23, "medium")
        self.assertNotIn("-crf", args)
        self.assertEqual(args[args.index("-cq") + 1], "23")
        self.assertEqual(args[args.index("-preset") + 1], "p5")
        self.assertIn("vbr", args)

    def test_qsv(self):
        args = media.encoder_args("h264_qsv", 25, "ultrafast")
        self.assertNotIn("-crf", args)
        self.assertEqual(args[args.index("-global_quality") + 1], "25")
        self.assertEqual(args[args.index("-preset") + 1], "veryfast")


class PosterAndWatermarkTests(SimpleTestCase):
    """任务 4.3 / 5.1。"""

    def test_poster_skips_intro(self):
        # 片头 3s（starts[0]=0），首个正片起点 3s、时长 5s -> 3 + 1 = 4.0
        self.assertEqual(media.poster_timestamp([0.0, 3.0], [3.0, 5.0], [1]), 4.0)
        self.assertEqual(media.poster_timestamp([0.0], [1.0], [0]), 0.5)
        self.assertEqual(media.poster_timestamp([0.0], [1.0], []), 0.0)

    def test_parse_watermark_defaults_and_invalid(self):
        self.assertIsNone(media.parse_watermark(None))
        self.assertIsNone(media.parse_watermark({"type": "image"}))
        self.assertIsNone(media.parse_watermark({"type": "text", "text": "  "}))
        spec = media.parse_watermark({"type": "text", "text": "出品", "position": "middle",
                                      "opacity": 5, "margin": "x", "color": "red"})
        self.assertEqual((spec["position"], spec["opacity"], spec["margin"], spec["color"]),
                         ("top_right", 1.0, 40, "#FFFFFF"))

    def test_watermark_corners(self):
        xy = lambda p: media.watermark_xy(p, 40, "W", "H", "w", "h", 128)
        self.assertEqual(xy("top_left"), ("40", "40"))
        self.assertEqual(xy("top_right"), ("W-w-40", "40"))
        self.assertEqual(xy("bottom_left"), ("40", "H-h-168"))
        self.assertEqual(xy("bottom_right"), ("W-w-40", "H-h-168"))
