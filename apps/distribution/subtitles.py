"""字幕生成与翻译逻辑（不依赖 Celery，供 tasks 与 API 复用）。"""
import json
import logging
import re

from apps.creation.models import Asset, AssetStatus, AssetType, Shot
from .models import SubtitleSourceType, SubtitleStatus, SubtitleTrack

logger = logging.getLogger(__name__)

# 行首编号："1." / "1、" / "1:" / "1）" 等
_NUMBERED_LINE = re.compile(r"^(\d+)[.、:：)）]\s*(.*)$")


def build_auto_subtitles(episode):
    """从 Episode 的所有 Shot 的 AUDIO Asset 生成时间轴字幕条目。"""
    shots = Shot.objects.filter(scene__episode=episode).order_by(
        "scene__scene_number", "shot_number"
    )
    entries = []
    cursor = 0.0
    for shot in shots:
        if not shot.dialogue:
            # 无对白镜头不出字幕，但仍占用画面时长，游标需前移（与 compose 片段时长一致）
            cursor += float(shot.duration_estimate or 3)
            continue
        audio = Asset.objects.filter(
            play=episode.play, shot=shot, asset_type=AssetType.AUDIO,
            status=AssetStatus.READY,
        ).order_by("-version").first()
        duration = 0.0
        if audio:
            duration = float((audio.metadata or {}).get("duration_seconds", 0) or 0)
        if duration <= 0:
            duration = max(1.0, len(shot.dialogue) / 4.0)  # 估算
        entries.append({
            "start": round(cursor, 3),
            "end": round(cursor + duration, 3),
            "text": shot.dialogue,
        })
        cursor += duration
    return entries


def generate_subtitle_track(episode, language="zh-CN"):
    """为 Episode 生成 AUTO 字幕轨（存在则复用）。"""
    entries = build_auto_subtitles(episode)
    track, created = SubtitleTrack.objects.update_or_create(
        episode=episode, language=language,
        defaults={
            "source_type": SubtitleSourceType.AUTO,
            "status": SubtitleStatus.READY,
            "subtitle_data": entries,
        },
    )
    return track, created


def translate_subtitle_track(source_track, target_language, gateway, agent_config, play):
    """通过 LLM 翻译字幕数据，创建目标语言字幕轨。"""
    entries = source_track.subtitle_data or []
    texts = [e.get("text", "") for e in entries]
    joined = "\n".join(f"{i + 1}. {t}" for i, t in enumerate(texts))
    system = "你是字幕翻译，将中文字幕翻译为目标语言，保持编号，仅输出翻译结果。"
    result = gateway.call(
        "LLM", agent_config, play=play,
        prompt=f"翻译为 {target_language} 的字幕：\n{joined}",
        system_prompt=system,
    )
    raw = result["text"]
    # 按编号匹配（LLM 被要求保持编号），不再纯位置映射：
    # 前言/尾注等多余行不会污染 cue 1，缺失条目回退原文且其余不偏移
    numbered, unnumbered = {}, []
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        m = _NUMBERED_LINE.match(line)
        if m and m.group(2):
            numbered[int(m.group(1))] = m.group(2).strip()
        else:
            unnumbered.append(line)
    # 无编号行按序兜底给缺编号条目；多于条目数时视为前言/尾注，取尾部对齐（前言更常见于开头）
    if len(unnumbered) > len(entries):
        logger.warning("翻译无编号行 %d 多于条目 %d，丢弃前 %d 行",
                       len(unnumbered), len(entries), len(unnumbered) - len(entries))
        unnumbered = unnumbered[-len(entries):]
    fallback = iter(unnumbered)
    translated, matched = [], 0
    for i, entry in enumerate(entries, start=1):
        if numbered.get(i):
            text, matched = numbered[i], matched + 1
        else:
            text = next(fallback, None) or entry.get("text", "")  # 缺失回退原文
        translated.append({**entry, "text": text})
    if matched < len(entries):
        logger.warning("翻译编号匹配 %d/%d 条，未匹配条目按序兜底或回退原文",
                       matched, len(entries))
    track, _ = SubtitleTrack.objects.update_or_create(
        episode=source_track.episode, language=target_language,
        defaults={
            "source_type": SubtitleSourceType.TRANSLATION,
            "status": SubtitleStatus.READY,
            "subtitle_data": translated,
        },
    )
    return track


def format_srt(entries):
    """转换为 SRT 文本。"""
    lines = []
    for i, e in enumerate(entries, start=1):
        start = _srt_time(e.get("start", 0))
        end = _srt_time(e.get("end", 0))
        lines.append(f"{i}\n{start} --> {end}\n{e.get('text', '')}\n")
    return "\n".join(lines)


def format_vtt(entries):
    """转换为 WebVTT 文本（头部与各 cue 之间以空行分隔）。"""
    blocks = ["WEBVTT"]
    for e in entries:
        start = _vtt_time(e.get("start", 0))
        end = _vtt_time(e.get("end", 0))
        blocks.append(f"{start} --> {end}\n{e.get('text', '')}")
    return "\n\n".join(blocks) + "\n"


def _split_ms(seconds):
    # 整体四舍五入到毫秒再拆分，避免浮点截断（2.3 -> 2.299）与进位丢失
    total_ms = int(round(max(0.0, float(seconds)) * 1000))
    h, rem = divmod(total_ms, 3600000)
    m, rem = divmod(rem, 60000)
    s, ms = divmod(rem, 1000)
    return h, m, s, ms


def _srt_time(seconds):
    h, m, s, ms = _split_ms(seconds)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _vtt_time(seconds):
    h, m, s, ms = _split_ms(seconds)
    return f"{h:02d}:{m:02d}:{s:02d}.{ms:03d}"
