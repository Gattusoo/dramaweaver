"""FFmpeg 相关纯函数（无 IO，便于单测）。

openspec change: backend-ffmpeg-quality-distribution。
探测/运行 ffmpeg 等 IO 逻辑留在 tasks.py，此处只做计算与参数/滤镜生成。
"""
import json
import re
from fractions import Fraction

# ── 媒体元数据（D8）────────────────────────────────────────────────────────


def parse_probe(data):
    """ffprobe `-show_streams -show_format -of json` 输出 -> 统一媒体元数据。"""
    streams = data.get("streams") or []
    fmt = data.get("format") or {}
    video = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
    duration = _to_float(fmt.get("duration")) or _to_float((video or {}).get("duration"))
    if not duration:
        duration = _to_float((audio or {}).get("duration"))
    media = {
        "duration": round(duration or 0.0, 3),
        "size_bytes": int(_to_float(fmt.get("size")) or 0),
        "bit_rate_kbps": int((_to_float(fmt.get("bit_rate")) or 0) / 1000),
        "has_audio": audio is not None,
        "acodec": (audio or {}).get("codec_name", ""),
        "has_video": video is not None,
        "vcodec": "", "width": 0, "height": 0, "fps": 0.0,
    }
    if video:
        media.update(
            vcodec=video.get("codec_name", ""),
            width=int(video.get("width") or 0),
            height=int(video.get("height") or 0),
            fps=round(_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")), 3),
        )
    return media


def _to_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _rate(text):
    try:
        value = Fraction(str(text))
        return float(value) if value.denominator else 0.0
    except (ValueError, ZeroDivisionError):
        return 0.0


# ── 配音驱动的片段时长（D1）─────────────────────────────────────────────────


def plan_segment_durations(bases, voices, fixed, td, tail):
    """计算各片段计划时长。

    bases: 基础时长（Shot 预估 / 片头片尾探测值）；voices: 配音实际时长（无为 0）；
    fixed: 是否固定时长（片头片尾不随配音变化）；td: 转场重叠（cut 为 0）；tail: 尾部留白。
    规则：duration = max(base, td_prev + voice + td_next + tail)；首段 td_prev=0、末段 td_next=0。
    配音放在入场转场结束处（xfade 时间轴 starts[i]），故需同时预留入场与出场重叠。
    """
    n = len(bases)
    out = []
    for i in range(n):
        base = float(bases[i])
        if fixed[i]:
            out.append(base)
            continue
        td_prev = td if i > 0 else 0.0
        td_next = td if i < n - 1 else 0.0
        voice = float(voices[i] or 0.0)
        required = td_prev + voice + td_next + tail if voice > 0 else 0.0
        # xfade 要求片段长于转场，留 0.1s 余量
        out.append(round(max(base, required, td + 0.1 if n > 1 else 0.1), 3))
    return out


# ── 编码器参数映射（D5）─────────────────────────────────────────────────────

_NVENC_PRESETS = {
    "ultrafast": "p1", "superfast": "p2", "veryfast": "p3", "faster": "p4",
    "fast": "p4", "medium": "p5", "slow": "p6", "slower": "p7", "veryslow": "p7",
}
_QSV_PRESETS = {"ultrafast": "veryfast", "superfast": "veryfast"}


def encoder_args(vcodec, crf, preset):
    """同一质量档位映射为各编码器实际生效的参数。"""
    crf = int(crf)
    preset = str(preset or "medium")
    if vcodec == "h264_nvenc":
        return ["-c:v", vcodec, "-rc", "vbr", "-cq", str(crf), "-b:v", "0",
                "-preset", _NVENC_PRESETS.get(preset, "p5")]
    if vcodec == "h264_qsv":
        return ["-c:v", vcodec, "-global_quality", str(crf),
                "-preset", _QSV_PRESETS.get(preset, preset)]
    return ["-c:v", "libx264", "-crf", str(crf), "-preset", preset]


# ── 封面时间点（D6）─────────────────────────────────────────────────────────


def poster_timestamp(starts, durations, body_indices):
    """取首个正片片段内部一帧，避开片头与入场转场。"""
    if not body_indices:
        return 0.0
    i = body_indices[0]
    return round(starts[i] + min(1.0, durations[i] / 2.0), 3)


# ── 水印（D7）───────────────────────────────────────────────────────────────

_POSITIONS = {"top_left", "top_right", "bottom_left", "bottom_right"}


def parse_watermark(cfg):
    """规范化 style_params.watermark；未配置或不可用返回 None。非法值回退默认。"""
    if not isinstance(cfg, dict):
        return None
    kind = cfg.get("type")
    if kind == "image" and not cfg.get("url"):
        return None
    if kind == "text" and not str(cfg.get("text") or "").strip():
        return None
    if kind not in ("image", "text"):
        return None
    position = cfg.get("position") if cfg.get("position") in _POSITIONS else "top_right"
    return {
        "type": kind,
        "url": cfg.get("url", ""),
        "text": str(cfg.get("text") or ""),
        "position": position,
        "margin": _clamp_int(cfg.get("margin"), 40, 0, 400),
        "opacity": _clamp_float(cfg.get("opacity"), 0.6, 0.0, 1.0),
        "scale": _clamp_float(cfg.get("scale"), 0.15, 0.01, 1.0),
        "font_size": _clamp_int(cfg.get("font_size"), 36, 8, 300),
        "color": cfg.get("color") if re.fullmatch(r"#[0-9A-Fa-f]{6}", str(cfg.get("color") or "")) else "#FFFFFF",
    }


def watermark_xy(position, margin, main_w, main_h, mark_w, mark_h, bottom_extra=0):
    """返回 (x, y) 表达式；底部位置额外上移 bottom_extra 以避开字幕区。"""
    x = f"{margin}" if position.endswith("left") else f"{main_w}-{mark_w}-{margin}"
    if position.startswith("top"):
        y = f"{margin}"
    else:
        y = f"{main_h}-{mark_h}-{margin + bottom_extra}"
    return x, y


# ── 渠道格式要求（format-adaptation）──────────────────────────────────────

_RES_RE = re.compile(r"^(\d{2,5})x(\d{2,5})$")
_AR_RE = re.compile(r"^(\d{1,3}):(\d{1,3})$")
AUDIO_KBPS = 128
MIN_VIDEO_KBPS = 500


def validate_requirements(req):
    """校验 format_requirements；返回 {字段: 错误信息}，空字典表示合法。"""
    errors = {}
    if not isinstance(req, dict):
        return {"format_requirements": "应为对象"}
    res = req.get("resolution")
    if res is not None:
        m = _RES_RE.match(str(res))
        if not m or int(m.group(1)) % 2 or int(m.group(2)) % 2:
            errors["resolution"] = "格式应为 宽x高，且宽高为偶数，如 720x1280"
    ar = req.get("aspect_ratio")
    if ar is not None:
        m = _AR_RE.match(str(ar))
        if not m or not int(m.group(1)) or not int(m.group(2)):
            errors["aspect_ratio"] = "格式应为 宽:高，如 9:16"
    if res is not None and ar is not None and "resolution" not in errors and "aspect_ratio" not in errors:
        w, h = _parse_res(res)
        if abs(w / h - _parse_ar(ar)) > 0.01:
            errors["aspect_ratio"] = "与 resolution 的宽高比不一致"
    for key, lo, hi in (("fps", 1, 120), ("max_bitrate_kbps", 100, 200000),
                        ("max_size_mb", 1, 100000), ("max_duration", 1, 36000)):
        if req.get(key) is not None and not _in_range(req.get(key), lo, hi):
            errors[key] = f"应为 {lo}~{hi} 的数字"
    if req.get("loudness_lufs") is not None and not _in_range(req.get("loudness_lufs"), -70, -5):
        errors["loudness_lufs"] = "应为 -70~-5 的数字"
    return errors


def _in_range(value, lo, hi):
    return not isinstance(value, bool) and isinstance(value, (int, float)) and lo <= value <= hi


def _parse_res(text):
    m = _RES_RE.match(str(text))
    return int(m.group(1)), int(m.group(2))


def _parse_ar(text):
    m = _AR_RE.match(str(text))
    return int(m.group(1)) / int(m.group(2))


_MEDIA_KEYS = ("resolution", "aspect_ratio", "fps", "max_bitrate_kbps", "max_size_mb",
               "loudness_lufs", "max_duration")


def has_media_limits(req):
    return any((req or {}).get(k) is not None for k in _MEDIA_KEYS)


def evaluate_requirements(media, req):
    """适配判定 -> ("not_needed"|"needed"|"failed", 原因)。

    响度无法从元数据得知，指定 loudness_lufs 即需转码。
    """
    req = req or {}
    if not has_media_limits(req):
        return "not_needed", ""
    if not media:
        return "needed", "母版缺少媒体元数据"
    duration = float(media.get("duration") or 0)
    if req.get("max_duration") and duration > req["max_duration"]:
        return "failed", f"视频时长 {duration:.1f}s 超过渠道限制 {req['max_duration']}s"
    reasons = []
    if req.get("resolution") and (media.get("width"), media.get("height")) != _parse_res(req["resolution"]):
        reasons.append("分辨率")
    if req.get("aspect_ratio") and media.get("height") and \
            abs(media["width"] / media["height"] - _parse_ar(req["aspect_ratio"])) > 0.01:
        reasons.append("宽高比")
    if req.get("fps") and abs(float(media.get("fps") or 0) - req["fps"]) > 0.5:
        reasons.append("帧率")
    if req.get("max_bitrate_kbps") and media.get("bit_rate_kbps", 0) > req["max_bitrate_kbps"]:
        reasons.append("码率")
    if req.get("max_size_mb") and media.get("size_bytes", 0) > req["max_size_mb"] * 1024 * 1024:
        reasons.append("文件大小")
    if req.get("loudness_lufs") is not None:
        reasons.append("响度")
    return ("needed", "、".join(reasons) + "不符合渠道要求") if reasons else ("not_needed", "")


def rendition_plan(media, req):
    """计算渠道版本转码目标；无法满足时抛 ValueError。"""
    req = req or {}
    src_w, src_h = int(media["width"]), int(media["height"])
    duration = float(media["duration"])
    if req.get("max_duration") and duration > req["max_duration"]:
        raise ValueError(f"视频时长 {duration:.1f}s 超过渠道限制 {req['max_duration']}s")
    if req.get("resolution"):
        w, h = _parse_res(req["resolution"])
    elif req.get("aspect_ratio"):
        ratio = _parse_ar(req["aspect_ratio"])
        long_edge = max(src_w, src_h)
        w, h = (long_edge, long_edge / ratio) if ratio >= 1 else (long_edge * ratio, long_edge)
        w, h = _even(w), _even(h)
    else:
        w, h = src_w, src_h
    cap = None
    if req.get("max_bitrate_kbps"):
        cap = int(req["max_bitrate_kbps"]) - AUDIO_KBPS
    if req.get("max_size_mb") and duration > 0:
        by_size = int(req["max_size_mb"] * 8192 * 0.92 / duration) - AUDIO_KBPS
        cap = by_size if cap is None else min(cap, by_size)
    if cap is not None and cap < MIN_VIDEO_KBPS:
        raise ValueError(f"满足体积/码率上限所需视频码率 {cap}kbps 过低（下限 {MIN_VIDEO_KBPS}kbps）")
    return {
        "width": w, "height": h,
        "pad_blur": abs(w / h - src_w / src_h) > 0.01,
        "fps": req.get("fps"),
        "video_kbps_cap": cap,
        "loudness": req.get("loudness_lufs"),
        "max_size_bytes": int(req["max_size_mb"] * 1024 * 1024) if req.get("max_size_mb") else None,
        "duration": duration,
    }


def _even(value):
    return max(2, int(round(value / 2.0)) * 2)


def rendition_filter(plan):
    """渠道版本视频滤镜：同比缩放，或异宽高比时完整画面居中 + 模糊填充。"""
    w, h = plan["width"], plan["height"]
    fps = f",fps={plan['fps']}" if plan.get("fps") else ""
    if not plan["pad_blur"]:
        return f"[0:v]scale={w}:{h}:flags=lanczos,setsar=1{fps},format=yuv420p[vout]"
    return (
        f"[0:v]split=2[bgs][fgs];"
        f"[bgs]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},boxblur=20:2[bg];"
        f"[fgs]scale={w}:{h}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2,setsar=1{fps},format=yuv420p[vout]"
    )


def parse_loudnorm_json(stderr):
    """解析 loudnorm print_format=json 输出（stderr 末尾的 JSON 块）。"""
    matches = re.findall(r"\{[^{}]*\"input_i\"[^{}]*\}", stderr or "", re.S)
    if not matches:
        raise ValueError("未找到 loudnorm 测量结果")
    return json.loads(matches[-1])


def loudnorm_second_pass(target, measured):
    m = measured
    return (
        f"loudnorm=I={target}:TP=-1.5:LRA=11:measured_I={m['input_i']}:"
        f"measured_TP={m['input_tp']}:measured_LRA={m['input_lra']}:"
        f"measured_thresh={m['input_thresh']}:offset={m['target_offset']}:linear=true"
    )


def parse_ebur128_integrated(stderr):
    """从 ebur128 summary 中读取 Integrated loudness（LUFS）。"""
    m = re.findall(r"I:\s*(-?\d+(?:\.\d+)?)\s*LUFS", stderr or "")
    if not m:
        raise ValueError("未找到 ebur128 响度结果")
    return float(m[-1])


def _clamp_int(value, default, lo, hi):
    try:
        return max(lo, min(hi, int(value)))
    except (TypeError, ValueError):
        return default


def _clamp_float(value, default, lo, hi):
    try:
        return max(lo, min(hi, float(value)))
    except (TypeError, ValueError):
        return default


# ═══ openspec change: backend-ffmpeg-derivatives-style ═══════════════════════

# ── 镜头音效（D3）───────────────────────────────────────────────────────────


def normalize_sound_effects(raw):
    """规范化 Shot.sound_effects；非法条目丢弃。"""
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        lib = item.get("library_id")
        url = str(item.get("url") or "").strip()
        lib = lib if isinstance(lib, int) and not isinstance(lib, bool) and lib > 0 else None
        if lib is None and not url:
            continue
        duration = _clamp_float(item.get("duration"), 0.0, 0.0, 600.0)
        out.append({
            "library_id": lib, "url": url if lib is None else "",
            "offset": _clamp_float(item.get("offset"), 0.0, 0.0, 600.0),
            "volume": _clamp_float(item.get("volume"), 1.0, 0.0, 2.0),
            "duration": duration or None,
        })
    return out


# ── 调色（D4）───────────────────────────────────────────────────────────────


def parse_color_grade(cfg):
    """规范化 style_params.color_grade；未配置或全为默认值返回 None。越界钳制。"""
    if not isinstance(cfg, dict):
        return None
    spec = {
        "lut": str(cfg.get("lut") or "").strip(),
        "intensity": _clamp_float(cfg.get("intensity"), 1.0, 0.0, 1.0),
        "brightness": _clamp_float(cfg.get("brightness"), 0.0, -1.0, 1.0),
        "contrast": _clamp_float(cfg.get("contrast"), 1.0, 0.0, 3.0),
        "saturation": _clamp_float(cfg.get("saturation"), 1.0, 0.0, 3.0),
        "vignette": _clamp_float(cfg.get("vignette"), 0.0, 0.0, 1.0),
        "grain": _clamp_int(cfg.get("grain"), 0, 0, 100),
    }
    if not (spec["lut"] and spec["intensity"] > 0) and not _grade_has_effects(spec):
        return None
    return spec


def _grade_has_effects(spec):
    return (spec["brightness"] != 0.0 or spec["contrast"] != 1.0 or spec["saturation"] != 1.0
            or spec["vignette"] > 0 or spec["grain"] > 0)


def color_grade_graph(spec, lut_arg, inp, out, tag):
    """调色子图：[inp] -> [out]。lut_arg 为已转义的 LUT 路径（None 表示不用 LUT）。

    intensity<1 时 split + blend 混合原画与 LUT 结果；两路统一 gbrp 保证 blend 输入格式一致。
    未产生任何效果时返回 None。
    """
    use_lut = bool(lut_arg) and spec["intensity"] > 0
    if not use_lut and not _grade_has_effects(spec):
        return None
    tail = []
    if spec["brightness"] != 0.0 or spec["contrast"] != 1.0 or spec["saturation"] != 1.0:
        tail.append(f"eq=brightness={spec['brightness']:.3f}:contrast={spec['contrast']:.3f}:"
                    f"saturation={spec['saturation']:.3f}")
    if spec["vignette"] > 0:
        tail.append(f"vignette=angle={spec['vignette'] * 1.5708:.4f}")
    if spec["grain"] > 0:
        tail.append(f"noise=alls={spec['grain']}:allf=t")
    tail.append("format=yuv420p")
    chain = ",".join(tail)
    if not use_lut:
        return f"[{inp}]{chain}[{out}]"
    lut = f"lut3d=file={lut_arg}:interp=tetrahedral"
    if spec["intensity"] >= 1.0:
        return f"[{inp}]{lut},{chain}[{out}]"
    i = spec["intensity"]
    return (f"[{inp}]format=gbrp,split=2[ga{tag}][gb{tag}];[gb{tag}]{lut}[gl{tag}];"
            f"[ga{tag}][gl{tag}]blend=all_expr='A*{1 - i:.3f}+B*{i:.3f}',{chain}[{out}]")


# ── ASS 字幕（D5）───────────────────────────────────────────────────────────


def ass_color(hexcolor, alpha=0, default="&H00FFFFFF"):
    h = str(hexcolor or "").lstrip("#")
    if not re.fullmatch(r"[0-9A-Fa-f]{6}", h):
        return default
    return f"&H{alpha:02X}{h[4:6]}{h[2:4]}{h[0:2]}".upper()


def ass_time(seconds):
    # 先整体四舍五入到厘秒再拆分，进位自然传递到秒/分/时（1.996 -> 0:00:02.00）
    total_cs = int(round(max(0.0, float(seconds)) * 100))
    h, rem = divmod(total_cs, 360000)
    m, rem = divmod(rem, 6000)
    sec, cs = divmod(rem, 100)
    return f"{h}:{m:02d}:{sec:02d}.{cs:02d}"


def ass_text(text):
    """普通文本 -> ASS 文本：花括号转全角（避免被当作覆盖标签），换行转 ASS 硬换行。"""
    t = str(text or "").replace("{", "｛").replace("}", "｝")
    return t.replace("\r\n", "\n").replace("\n", "\\N")


def karaoke_text(text, duration):
    """逐字高亮：条目时长按厘秒平均分配到每个字，余数补到末字；换行不计时。"""
    t = str(text or "").replace("{", "｛").replace("}", "｝").replace("\r\n", "\n")
    lines = t.split("\n")
    n = sum(len(line) for line in lines)
    if n == 0:
        return ""
    total = max(n, int(round(max(0.0, float(duration)) * 100)))
    per, rem = divmod(total, n)
    out, k = [], 0
    for line in lines:
        chunk = []
        for ch in line:
            k += 1
            chunk.append(f"{{\\k{per + (rem if k == n else 0)}}}{ch}")
        out.append("".join(chunk))
    return "\\N".join(out)


def render_ass(entries, style, font_family, play_w, play_h):
    """渲染 ASS 字幕。未配置新样式项时输出与此前版本一致。"""
    ss = (style or {}).get("subtitle_style") or {}
    size = _clamp_int(ss.get("font_size"), 48, 8, 300)
    base = ass_color(ss.get("color", "#FFFFFF"))
    karaoke = bool(ss.get("karaoke"))
    primary, secondary = base, "&H000000FF"
    if karaoke:
        primary, secondary = ass_color(ss.get("highlight_color", "#FFD700"), default="&H0000D7FF"), base
    outline_c = ass_color(ss.get("outline_color"), default="&H80000000")
    back_c = "&H80000000"
    border_style = 1
    if ss.get("box"):
        border_style = 3
        outline_c = back_c = ass_color(ss.get("box_color", "#000000"), alpha=0x80, default="&H80000000")
    outline = _clamp_int(ss.get("outline", 1), 1, 0, 20)
    shadow = _clamp_int(ss.get("shadow", 0), 0, 0, 20)
    bold = -1 if ss.get("bold") else 0
    marginv = _clamp_int(ss.get("margin_v", 80), 80, 0, 2000)
    lines = [
        "[Script Info]", "ScriptType: v4.00+", f"PlayResX: {play_w}", f"PlayResY: {play_h}", "",
        "[V4+ Styles]",
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
        f"Style: Default,{font_family},{size},{primary},{secondary},{outline_c},{back_c},{bold},0,0,0,100,100,0,0,{border_style},{outline},{shadow},2,40,40,{marginv},1",
        "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text",
    ]
    for e in entries:
        start, end = float(e.get("start", 0)), float(e.get("end", 0))
        text = karaoke_text(e.get("text", ""), end - start) if karaoke else ass_text(e.get("text", ""))
        lines.append(f"Dialogue: 0,{ass_time(start)},{ass_time(end)},Default,,0,0,0,,{text}")
    return "\n".join(lines)


# ── 合成时间轴（D6）─────────────────────────────────────────────────────────


def build_timeline(metas, starts, durations, td, total):
    """各片段在成片中的可见区间。metas: [{"role", "shot_id", "emotion", "dialogue"}]。

    start = 入场转场结束（starts[i]）；end = 出场转场开始（末段为总时长）。
    """
    n = len(metas)
    out = []
    for i, meta in enumerate(metas):
        td_prev = td if i > 0 else 0.0
        td_next = td if i < n - 1 else 0.0
        end = total if i == n - 1 else starts[i] - td_prev + durations[i] - td_next
        out.append({
            "index": i, "role": meta.get("role", "body"), "shot_id": meta.get("shot_id"),
            "start": round(starts[i], 3), "end": round(max(starts[i], end), 3),
            "emotion": meta.get("emotion", ""), "dialogue": meta.get("dialogue", ""),
        })
    return out


def keyframe_args(gop_seconds):
    g = _clamp_float(gop_seconds, 2.0, 0.5, 10.0)
    return ["-force_key_frames", f"expr:gte(t,n_forced*{g:g})"]


# ── 预告片挑选（D7）─────────────────────────────────────────────────────────

_STRONG_EMOTIONS = ("愤怒", "怒", "震惊", "惊", "反转", "哭", "泪", "吼", "打", "吻", "恐惧", "绝望",
                    "崩溃", "狂喜", "心碎", "紧张", "悬疑", "高潮", "angry", "shock", "twist", "cry")


def score_clip(entry):
    emotion = str(entry.get("emotion") or "").lower()
    dialogue = str(entry.get("dialogue") or "")
    score = 3 if any(k in emotion for k in _STRONG_EMOTIONS) else 0
    if dialogue.strip():
        score += 1
        if any(c in dialogue for c in "！!？?"):
            score += 1
    return score


def _body_entries(timeline):
    return [e for e in timeline or [] if e.get("role", "body") == "body" and e["end"] > e["start"]]


def select_trailer_clips(timeline, target_min=15.0, target_max=30.0, max_clip=6.0):
    """挑选预告片段 -> [{"start", "duration", "shot_id", "score"}]，首段为钩子（最高分）。"""
    body = _body_entries(timeline)
    if not body:
        return []
    clips = [{"start": e["start"], "duration": round(min(e["end"] - e["start"], max_clip), 3),
              "shot_id": e.get("shot_id"), "score": score_clip(e)} for e in body]
    if sum(c["duration"] for c in clips) <= target_min:
        chosen = clips
    else:
        chosen, total = [], 0.0
        for c in sorted(clips, key=lambda c: (-c["score"], c["start"])):
            if total >= target_min:
                break
            if total + c["duration"] > target_max:
                remain = target_max - total
                if remain < 1.0:
                    continue
                c = {**c, "duration": round(remain, 3)}
            chosen.append(c)
            total += c["duration"]
    hook = max(chosen, key=lambda c: (c["score"], -c["start"]))
    rest = sorted((c for c in chosen if c is not hook), key=lambda c: c["start"])
    return [hook] + rest


def pick_poster_candidates(timeline, k=4, total=0.0):
    """封面候选窗口 -> [{"start", "end", "shot_id"}]，按时间排序。

    有时间轴时取评分前 k 个不同镜头；无时间轴时在 [0, total] 均匀取 k 个窗口。
    """
    body = _body_entries(timeline)
    if body:
        seen, picked = set(), []
        for e in sorted(body, key=lambda e: (-score_clip(e), e["start"])):
            key = e.get("shot_id") or ("idx", e["index"])
            if key in seen:
                continue
            seen.add(key)
            picked.append({"start": e["start"], "end": e["end"], "shot_id": e.get("shot_id")})
            if len(picked) >= k:
                break
        return sorted(picked, key=lambda c: c["start"])
    if total <= 0:
        return []
    step = total / k
    return [{"start": round(i * step, 3), "end": round((i + 1) * step, 3), "shot_id": None} for i in range(k)]


def gif_filter(width=320, fps=10):
    return (f"fps={fps},scale={width}:-2:flags=lanczos,split[ga][gb];"
            f"[ga]palettegen=stats_mode=diff[gp];[gb][gp]paletteuse=dither=bayer:bayer_scale=4")


def hls_args(segment_seconds, segment_pattern, playlist):
    return ["-c", "copy", "-f", "hls", "-hls_time", f"{_clamp_float(segment_seconds, 4.0, 1.0, 30.0):g}",
            "-hls_playlist_type", "vod", "-hls_segment_filename", str(segment_pattern), str(playlist)]


def preview_size(w, h, scale):
    s = _clamp_float(scale, 1 / 3, 0.1, 1.0)
    return _even(w * s), _even(h * s), s
