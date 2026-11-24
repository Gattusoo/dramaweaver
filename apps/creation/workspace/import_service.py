"""项目导入恢复（工作台 Stage B2 任务 5.3/5.4，design D3）。

确认导入 = 新建 Play（默认导入为新项目，不覆盖）：
- 同 UUID 项目已存在 → 冲突（409），不默认覆盖；
- 预检报告驱动恢复：结构（Play/Episode/Scene/Shot/Character）按清单
  白名单字段重建，资产本地文件按 storage_key 落新项目目录并校验摘要；
- FK / current 指针 / 依赖边按 UUID 映射重建；
- 剧本/角色版本随资产 metadata 恢复（正文在 EPISODE_SCRIPT 资产 metadata）；
- DB 事务失败 → 只清理新建目录（不留半项目文件）；
- 远程 URL 资产（无 storage_key）仅保存受控元数据（5.4），不请求任何
  归档中的 URL——缺外部资产以 remote 资产形式存在并明示，不生成空文件。
"""
import json
import logging
import shutil

from django.db import transaction

from ..models import (
    Asset, AssetDependency, AssetStatus, AssetType, Character, Episode,
    Play, PlayStatus, RelationType, Scene, Shot,
)
from .export_service import CHECKSUM_NAME, PROJECT_JSON
from .import_precheck import ImportError_, precheck_import
from .manifest import (
    _EPISODE_FIELDS, _SCENE_FIELDS, _SHOT_FIELDS, _scrub,
)
from .storage import project_dir, save_asset_bytes, _root

logger = logging.getLogger(__name__)

# 清单角色字段（manifest 构建器同口径）
_CHARACTER_FIELDS = ("name", "role", "description", "appearance_prompt")


def _read_zip(fileobj):
    import io
    import zipfile
    try:
        return zipfile.ZipFile(io.BytesIO(fileobj.read()))
    except (zipfile.BadZipFile, OSError) as exc:
        raise ImportError_(f"不是合法的 ZIP 归档: {exc}")


def confirm_import(fileobj, user, *, report=None):
    """确认导入：预检（如未传入报告）→ 新建项目恢复。

    返回 (play, summary)；同 UUID 已存在 → 冲突（409），v1 不做覆盖恢复
    （design D3：导入默认新建，不承诺旧项目无损覆盖）。
    """
    if report is None:
        report = precheck_import(fileobj)
        fileobj.seek(0)
    uuid_ = report["project_uuid"]
    if Play.objects.filter(uuid=uuid_).exists():
        conflict = ImportError_(f"已存在同 UUID 项目（{uuid_}），导入不覆盖既有项目")
        conflict.status_code = 409
        raise conflict

    zf = _read_zip(fileobj)
    with zf:
        manifest = json.loads(zf.read(PROJECT_JSON).decode("utf-8"))
        project = manifest["project"]
        # 资产文件字节缓存（导入包不大时内存可承受；超大包由预检限额挡住）
        asset_files = {}
        for name in zf.namelist():
            if name in (PROJECT_JSON, CHECKSUM_NAME):
                continue
            asset_files[name] = zf.read(name)

    new_dir = None
    try:
        with transaction.atomic():
            play = _restore_play(project, user)
            new_dir = project_dir(play)
            char_map = _restore_characters(play, manifest.get("characters") or [])
            ep_map, scene_map, shot_map, script_contents = _restore_structure(
                play, manifest.get("episodes") or [], char_map)
            asset_map, remote_count = _restore_assets(
                play, manifest, asset_files,
                ep_map, scene_map, shot_map, char_map)
            _restore_dependencies(manifest.get("dependencies") or [], asset_map)
            _restore_versions(play, manifest, script_contents, char_map, user)
    except Exception:
        # DB 失败：只清理新建目录（既有项目目录不动）
        if new_dir is not None:
            shutil.rmtree(new_dir, ignore_errors=True)
        raise

    summary = {
        "play_id": play.pk,
        "project_uuid": str(play.uuid),
        "title": play.title,
        "episodes": len(ep_map),
        "characters": len(char_map),
        "assets_local": len(asset_files),
        "assets_remote": remote_count,
    }
    return play, summary


def _restore_play(project, user) -> Play:
    return Play.objects.create(
        uuid=project["uuid"],
        title=project.get("title") or "导入项目",
        synopsis=project.get("synopsis") or "",
        genre=project.get("genre") or "",
        total_episodes=project.get("total_episodes") or 10,
        status=PlayStatus.DRAFT,  # 新项目从 DRAFT 起（不继承导出时状态）
        audit_mode=project.get("audit_mode") or "KEY_CHECKPOINTS",
        redo_granularity=project.get("redo_granularity") or "SHOT",
        consistency_mode=project.get("consistency_mode") or "NONE",
        gen_params=_scrub(project.get("gen_params") or {}),
        created_by=user,
    )


def _restore_characters(play, characters) -> dict:
    """char-<pk> → 新 Character（dialogue_character 清单存 "<pk>"，兼容两种引用）。"""
    out = {}
    for c in characters:
        created = Character.objects.create(
            play=play, **{f: c.get(f) or "" for f in _CHARACTER_FIELDS})
        out[c["uuid"]] = created
        # 兼容 _shot_payload 的纯 pk 引用（"2" ↔ "char-2"）
        if str(c["uuid"]).startswith("char-"):
            out[str(c["uuid"])[len("char-"):]] = created
    return out


def _restore_structure(play, episodes, char_map):
    ep_map, scene_map, shot_map, script_contents = {}, {}, {}, {}
    for ep in episodes:
        episode = Episode.objects.create(
            play=play,
            episode_number=ep["episode_number"],
            **{f: ep.get(f) or ("PENDING" if f == "status" else 0
                                if f == "duration_seconds" else "")
               for f in _EPISODE_FIELDS})
        ep_map[ep["episode_number"]] = episode
        for sc in ep.get("scenes") or []:
            scene = Scene.objects.create(
                episode=episode, scene_number=sc["scene_number"],
                **{f: sc.get(f) or ("PENDING" if f == "status" else "DAY"
                                    if f == "time_of_day" else "")
                   for f in _SCENE_FIELDS})
            scene_map[(episode.pk, sc["scene_number"])] = scene
            for sh in sc.get("shots") or []:
                dialogue_char = char_map.get(sh.get("dialogue_character"))
                shot = Shot.objects.create(
                    scene=scene, shot_number=sh["shot_number"],
                    dialogue_character=dialogue_char,
                    sound_effects=sh.get("sound_effects") or [],
                    **{f: sh.get(f) or ("PENDING" if f == "status" else 0
                                        if f == "duration_estimate" else "")
                       for f in _SHOT_FIELDS})
                shot_map[(scene.pk, sh["shot_number"])] = shot
                if sh.get("dialogue"):
                    script_contents.setdefault(episode, []).append(
                        {"shot_number": sh["shot_number"], "dialogue": sh["dialogue"]})
    return ep_map, scene_map, shot_map, script_contents


def _restore_assets(play, manifest, asset_files, ep_map, scene_map, shot_map, char_map):
    """按清单重建资产行并落本地文件（内容寻址写新项目目录）。"""
    from .storage import asset_ext_for
    asset_map = {}
    remote = 0
    for a in manifest.get("assets") or []:
        asset_type = a.get("type")
        if asset_type not in AssetType.values:
            continue
        key = a.get("storage_key") or ""
        data = asset_files.get(key) if key else None
        if data is None:
            # 远程/缺失资产：只建受控元数据行（不请求 URL，不生成空文件）
            remote += 1
            new_key = ""
            digest = ""
        else:
            digest = __import__("hashlib").sha256(data).hexdigest()
            new_key, _, _ = save_asset_bytes(play, data, asset_type)
        episode = ep_map.get(a.get("episode_number"))
        scene = scene_map.get((episode.pk, a.get("scene_number"))) if episode else None
        shot = shot_map.get((scene.pk, a.get("shot_number"))) if scene else None
        character = char_map.get(a.get("character"))
        asset = Asset.objects.create(
            play=play, episode=episode, scene=scene, shot=shot,
            character=character,
            asset_type=asset_type,
            status=AssetStatus.READY if data is not None else AssetStatus.PENDING,
            storage_key=new_key,
            content_sha256=digest or (a.get("content_sha256") or ""),
            input_fingerprint=a.get("fingerprint") or "",
            generation_spec=_scrub(a.get("generation_spec"))
            if a.get("generation_spec") else None,
            prompt=a.get("prompt") or "",
            parameters=_scrub(a.get("parameters")) if a.get("parameters") else {},
            artifact_url=f"/media/{new_key}" if new_key else (a.get("artifact_url") or ""),
        )
        asset_map[a["uuid"]] = asset
        # current 指针：同 scope 最新 READY 版本
        if data is not None:
            _update_pointers(shot, asset, asset_type)
    return asset_map, remote


def _update_pointers(shot, asset, asset_type):
    if shot is None:
        return
    if asset_type == AssetType.IMAGE and shot.current_image_asset_id is None:
        shot.current_image_asset = asset
        shot.save(update_fields=["current_image_asset", "updated_at"])
    elif asset_type == AssetType.AUDIO and shot.current_audio_asset_id is None:
        shot.current_audio_asset = asset
        shot.save(update_fields=["current_audio_asset", "updated_at"])


def _restore_dependencies(dependencies, asset_map):
    rel_values = set(RelationType.values)
    for dep in dependencies:
        up = asset_map.get(dep.get("upstream"))
        down = asset_map.get(dep.get("downstream"))
        rel = dep.get("relation_type")
        if up is None or down is None or rel not in rel_values or up.pk == down.pk:
            continue
        AssetDependency.objects.get_or_create(
            upstream=up, downstream=down, defaults={"relation_type": rel})


def _restore_versions(play, manifest, script_contents, char_map, user):
    """剧本/角色版本恢复：结构快照建 main 分支 v1。

    对白来自镜头清单字段（EPISODE_SCRIPT 资产 metadata 不在导出白名单内，
    正文以结构快照恢复）；沿用 save_service 的 create_version 语义。
    """
    from ..assistant.script_versions import (
        CHARACTER_FIELDS, CharacterVersion, ScriptVersion, create_version,
    )
    from ..models import Episode as _Episode
    for episode in script_contents:
        content = {
            "text": "\n".join(s["dialogue"] for s in script_contents[episode]),
            "scenes": {
                str(s.scene_number): {f: getattr(s, f) for f in
                                      ("description", "mood", "location", "time_of_day")}
                for s in Scene.objects.filter(episode=episode)
            },
        }
        create_version(play, episode=episode, branch_name="main",
                       summary="导入恢复", user=user, content=content)
    # 角色版本 v1（有变化才记录——对导入项目即首个快照）
    # char_map 含 "char-<pk>" 与 "<pk>" 双键（引用兼容）——按 pk 去重
    seen_char_pks = set()
    for uuid_, character in char_map.items():
        if character.pk in seen_char_pks:
            continue
        seen_char_pks.add(character.pk)
        snapshot = {f: getattr(character, f) for f in CHARACTER_FIELDS}
        CharacterVersion.objects.create(
            character=character, version=1, snapshot=snapshot,
            changed_fields=list(CHARACTER_FIELDS), created_by=user)
