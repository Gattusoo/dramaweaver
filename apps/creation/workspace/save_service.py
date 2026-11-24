"""手动保存服务（工作台 Stage B1 任务 4.3，design D3）。

保存 = 剧本/角色版本快照（复用 assistant.script_versions，内容摘要判重，
不制造重复版本）+ project.json 原子写入（延迟刷新在此收敛）。
"""
import hashlib
import json
import logging

from ..models import Character

from .manifest import manifest_digest, refresh_manifest, write_manifest

logger = logging.getLogger(__name__)


class SaveError(Exception):
    status_code = 400


def _content_digest(content: dict) -> str:
    """规范化内容摘要（判重依据）：sort_keys + 紧凑分隔符的 SHA-256。"""
    data = json.dumps(content or {}, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def _save_episode_version(play, episode, user):
    """每集调 snapshot_episode + 内容摘要判重；有变化才 create_version。"""
    from ..assistant.script_versions import ScriptVersion, create_version, snapshot_episode

    content = snapshot_episode(play, episode)
    digest = _content_digest(content)
    last = (ScriptVersion.objects.filter(play=play, episode=episode)
            .order_by("-version").first())
    if last is not None and _content_digest(last.content) == digest:
        return last, False  # 内容未变：复用既有版本，不制造重复
    version = create_version(
        play, episode=episode, branch_name="main",
        summary="手动保存", user=user, content=content,
        parent=last)
    return version, True


def _save_character_versions(play, user):
    """角色版本：复用 record_character_version 的既有判重口径。

    script_versions.record_character_version 每次自增——这里先比对最近
    快照内容，有变化才记录（不制造重复版本）。
    """
    from ..assistant.script_versions import (
        CHARACTER_FIELDS, CharacterVersion, record_character_version,
    )

    created = []
    for character in Character.objects.filter(play=play):
        snapshot = {f: getattr(character, f) for f in CHARACTER_FIELDS}
        last = (CharacterVersion.objects.filter(character=character)
                .order_by("-version").first())
        if last is not None and _content_digest(last.snapshot) == _content_digest(snapshot):
            continue
        created.append(record_character_version(character, user=user))
    return created


def save_play(play, user=None) -> dict:
    """手动保存：版本快照（判重）+ 清单强制刷新。

    返回 {saved, manifest_digest, episode_versions, character_versions}。
    """
    from ..models import Episode

    episode_results = []
    for episode in Episode.objects.filter(play=play).order_by("episode_number"):
        version, created = _save_episode_version(play, episode, user)
        episode_results.append({
            "episode_number": episode.episode_number,
            "version": version.version,
            "created": created,
        })
    character_created = _save_character_versions(play, user)
    write_manifest(play)
    # 保存后清除脏标记（write_manifest 已落盘最新状态）
    from .manifest import _dirty, _dirty_guard
    with _dirty_guard:
        _dirty.discard(str(play.uuid))
    return {
        "saved": True,
        "manifest_digest": manifest_digest(play),
        "episode_versions": episode_results,
        "character_versions": len(character_created),
    }
