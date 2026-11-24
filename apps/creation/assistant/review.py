"""分集确定性审查（assistant/review，D5）：只查询、不改数据、不调用模型。

主观质量（钩子、节奏、反转）由模型基于读取结果自行判断，不做成规则。
"""
import re

from ..models import AssetStatus, AssetType

CHARS_PER_SECOND = 5          # 中文配音约 4-5 字/秒，取上限避免误报
DEFAULT_DURATION_RANGE = (60, 180)
_SPEAKER_RE = re.compile(r"^\s*([^：:\s]{1,12})\s*[：:]")
_PUNCT_RE = re.compile(r"[\s，。！？、；：,.!?;:\"'“”‘’（）()【】\[\]…—\-]")


def spoken_chars(text):
    return len(_PUNCT_RE.sub("", text or ""))


def _issue(code, severity, message, scene=None, shot=None):
    return {"code": code, "severity": severity, "message": message,
            "scene_number": scene.scene_number if scene else None,
            "shot_number": shot.shot_number if shot else None}


def _duration_range(style):
    raw = (style or {}).get("episode_duration_range")
    if isinstance(raw, (list, tuple)) and len(raw) == 2 and all(isinstance(v, (int, float)) for v in raw):
        return float(raw[0]), float(raw[1])
    return DEFAULT_DURATION_RANGE


def review_episode(episode, style=None):
    play = episode.play
    characters = list(play.characters.all())
    names = {c.name for c in characters}
    issues, total, shot_count, speaking = [], 0.0, 0, set()
    ready = {}
    for asset_type, shot_id in play.assets.filter(
            episode=episode, status=AssetStatus.READY, shot__isnull=False,
            asset_type__in=[AssetType.IMAGE, AssetType.AUDIO]).values_list("asset_type", "shot_id"):
        ready.setdefault(shot_id, set()).add(asset_type)

    for scene in episode.scenes.order_by("scene_number").prefetch_related("shots"):
        shots = sorted(scene.shots.all(), key=lambda s: s.shot_number)
        if not shots:
            issues.append(_issue("empty_scene", "error", "场景没有镜头", scene))
        for shot in shots:
            shot_count += 1
            total += float(shot.duration_estimate or 3)
            have = ready.get(shot.pk, set())
            if not (shot.visual_prompt or "").strip():
                issues.append(_issue("missing_visual_prompt", "error", "镜头缺少画面描述", scene, shot))
            if AssetType.IMAGE not in have:
                issues.append(_issue("missing_image", "warning", "镜头没有就绪的图片", scene, shot))
            dialogue = (shot.dialogue or "").strip()
            if not dialogue:
                continue
            if shot.dialogue_character_id:
                speaking.add(shot.dialogue_character_id)
            else:
                issues.append(_issue("dialogue_without_character", "warning", "镜头有对白但未指定对白角色",
                                     scene, shot))
            m = _SPEAKER_RE.match(dialogue)
            if m and m.group(1) not in names and m.group(1) not in ("旁白", "画外音", "OS", "VO"):
                issues.append(_issue("unknown_speaker", "warning", f"对白说话人「{m.group(1)}」不在角色表",
                                     scene, shot))
            need = spoken_chars(dialogue[m.end():] if m else dialogue) / CHARS_PER_SECOND
            if shot.duration_estimate and shot.duration_estimate < need:
                issues.append(_issue("duration_too_short", "warning",
                                     f"预估 {shot.duration_estimate} 秒，对白约需 {need:.1f} 秒", scene, shot))
            if AssetType.AUDIO not in have:
                issues.append(_issue("missing_audio", "warning", "镜头有对白但没有就绪的配音", scene, shot))

    refs = set(play.assets.filter(asset_type=AssetType.CHARACTER_REF, status=AssetStatus.READY)
               .values_list("character_id", flat=True))
    for c in characters:
        if c.pk not in speaking:
            continue
        if c.pk not in refs:
            issues.append(_issue("character_without_ref", "warning", f"角色「{c.name}」缺少定妆照"))
        if not c.voice_profile_id:
            issues.append(_issue("character_without_voice", "warning", f"角色「{c.name}」未绑定声线"))

    lo, hi = _duration_range(style)
    if shot_count and not lo <= total <= hi:
        issues.append(_issue("episode_duration_out_of_range", "warning",
                             f"单集预估 {total:.0f} 秒，不在 {lo:.0f}-{hi:.0f} 秒范围内"))
    return {
        "episode_number": episode.episode_number,
        "issues": issues,
        "stats": {"scenes": episode.scenes.count(), "shots": shot_count, "estimated_seconds": round(total, 1),
                  "errors": sum(i["severity"] == "error" for i in issues),
                  "warnings": sum(i["severity"] == "warning" for i in issues)},
    }
