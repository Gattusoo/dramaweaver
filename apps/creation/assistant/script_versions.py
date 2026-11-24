"""剧本版本分支（Phase 2.4.3）。

- 版本快照：剧本正文 + 场景字段（content），带分支名与父版本
- 分支：从某版本创建新分支，继承父版本内容
- 对比：结构化 diff（正文行级 + 场景字段级）
- 合并：三分支合并，冲突以字段/行为单位返回，支持按策略解决
- 角色版本：记录角色设定变化与受影响资产

不做：自动合并冲突（必须显式指定解决方式）；跨剧合并。
"""
from __future__ import annotations

import difflib
import logging

from django.db import transaction

from ..models import Asset, AssetStatus, AssetType, Scene
from .models import CharacterVersion, ScriptVersion
from .proposals import latest_text_asset

logger = logging.getLogger(__name__)

SCENE_FIELDS = ("description", "mood", "location", "time_of_day")
CHARACTER_FIELDS = ("name", "role", "description", "appearance_prompt")


class VersionError(Exception):
    status_code = 400


class VersionConflict(VersionError):
    status_code = 409


# ── 快照 ───────────────────────────────────────────────────────────────────

def snapshot_episode(play, episode=None) -> dict:
    """当前剧本状态：正文 + 场景字段。"""
    asset = latest_text_asset(play, AssetType.EPISODE_SCRIPT, episode)
    content = {"text": ((asset.metadata or {}).get("text") if asset else "") or "", "scenes": {}}
    if episode is not None:
        for scene in Scene.objects.filter(episode=episode).order_by("scene_number"):
            content["scenes"][str(scene.scene_number)] = {f: getattr(scene, f) for f in SCENE_FIELDS}
    return content


def _next_version(play, episode, branch_name) -> int:
    last = (ScriptVersion.objects.filter(play=play, episode=episode, branch_name=branch_name)
            .order_by("-version").values_list("version", flat=True).first())
    return (last or 0) + 1


def create_version(play, *, episode=None, branch_name="main", summary="", user=None,
                   content=None, parent=None) -> ScriptVersion:
    """创建版本快照（幂等调用方需自行判重；同分支版本号自增）。"""
    branch_name = (branch_name or "main").strip()[:100]
    if not branch_name:
        raise VersionError("分支名不能为空")
    if parent is not None and parent.play_id != play.pk:
        raise VersionError("父版本不属于该短剧")
    with transaction.atomic():
        version = ScriptVersion.objects.create(
            play=play, episode=episode, version=_next_version(play, episode, branch_name),
            branch_name=branch_name, parent=parent,
            content=content if content is not None else snapshot_episode(play, episode),
            summary=(summary or "")[:500], created_by=user,
        )
    return version


def create_branch(play, *, episode=None, from_version, branch_name, user=None, summary="") -> ScriptVersion:
    """从已有版本创建新分支：内容继承父版本。"""
    parent = ScriptVersion.objects.filter(pk=getattr(from_version, "pk", from_version), play=play).first()
    if parent is None:
        raise VersionError("父版本不存在")
    if parent.branch_name == branch_name:
        raise VersionError("分支名与父版本相同，无需创建分支")
    return create_version(play, episode=parent.episode, branch_name=branch_name, summary=summary,
                          user=user, content=parent.content, parent=parent)


def branches(play, episode=None) -> dict:
    """分支 → 最新版本列表。"""
    qs = ScriptVersion.objects.filter(play=play, episode=episode)
    out = {}
    for version in qs.order_by("branch_name", "-version"):
        out.setdefault(version.branch_name, version)
    return out


# ── 对比 ───────────────────────────────────────────────────────────────────

def script_diff(v1, v2) -> dict:
    """结构化差异：正文行级 diff + 场景字段级变更。"""
    c1, c2 = v1.content or {}, v2.content or {}
    lines1 = (c1.get("text") or "").splitlines()
    lines2 = (c2.get("text") or "").splitlines()
    text_changes = []
    for line in difflib.unified_diff(lines1, lines2, fromfile=f"v{v1.version}", tofile=f"v{v2.version}",
                                     lineterm="", n=0):
        if line.startswith(("---", "+++")):
            continue
        kind = {"+": "add", "-": "remove", " ": "context"}.get(line[:1], "context")
        if kind != "context":
            text_changes.append({"kind": kind, "line": line[1:]})
    scenes = {}
    keys = set((c1.get("scenes") or {})) | set((c2.get("scenes") or {}))
    for number in sorted(keys, key=lambda x: int(x) if str(x).isdigit() else 0):
        s1 = (c1.get("scenes") or {}).get(number) or {}
        s2 = (c2.get("scenes") or {}).get(number) or {}
        fields = {}
        for field in set(s1) | set(s2):
            if s1.get(field) != s2.get(field):
                fields[field] = {"from": s1.get(field), "to": s2.get(field)}
        if not s1 or not s2:
            fields["__scene__"] = {"from": "exists" if s1 else None, "to": "exists" if s2 else None}
        if fields:
            scenes[number] = fields
    return {
        "from": {"id": v1.pk, "branch": v1.branch_name, "version": v1.version},
        "to": {"id": v2.pk, "branch": v2.branch_name, "version": v2.version},
        "text_changes": text_changes,
        "scene_changes": scenes,
        "identical": not text_changes and not scenes,
    }


# ── 合并 ───────────────────────────────────────────────────────────────────

def merge_versions(base, ours, theirs, *, resolutions=None) -> dict:
    """三方合并（Phase 2.4.3.4）。

    规则：仅一方改动 → 采用该方；双方改动相同 → 采用；双方改动不同 → 冲突，
    需 resolutions 指定 {field_path: "ours"|"theirs"|值}，未指定则全部列入 conflicts。
    返回 {"content": 合并结果, "conflicts": [...], "resolved": [...], "clean": bool}
    """
    base_c, ours_c, theirs_c = (base.content or {}), (ours.content or {}), (theirs.content or {})
    resolutions = resolutions or {}
    conflicts, resolved = [], []

    def pick(path, b, o, t):
        if o == t:
            return o, None
        if o == b:
            return t, None
        if t == b:
            return o, None
        if path in resolutions:
            choice = resolutions[path]
            if choice == "ours":
                resolved.append({"path": path, "chosen": "ours"})
                return o, None
            if choice == "theirs":
                resolved.append({"path": path, "chosen": "theirs"})
                return t, None
            resolved.append({"path": path, "chosen": "value"})
            return choice, None
        conflicts.append({"path": path, "base": b, "ours": o, "theirs": t})
        return o, path

    merged = {"text": ours_c.get("text", ""), "scenes": {}}
    text, conflict = pick("text", base_c.get("text"), ours_c.get("text"), theirs_c.get("text"))
    merged["text"] = text
    numbers = set(base_c.get("scenes") or {}) | set(ours_c.get("scenes") or {}) | set(theirs_c.get("scenes") or {})
    for number in sorted(numbers, key=lambda x: int(x) if str(x).isdigit() else 0):
        b = (base_c.get("scenes") or {}).get(number)
        o = (ours_c.get("scenes") or {}).get(number)
        t = (theirs_c.get("scenes") or {}).get(number)
        path = f"scenes.{number}"
        # 场景存在性先于字段合并：一方删除（场景 dict 缺失）另一方未动 → 删除生效。
        # 不能走字段级 pick——场景 dict 缺失与字段值为 None 不可区分，会把删除
        # 静默降级成"场景保留、字段全 None"的假场景
        if o is None or t is None:
            if o is None and t is None:
                continue  # 双方一致删除（b 有则删、b 无则双方都没提），删除生效
            deleted, kept = ("ours", t) if o is None else ("theirs", o)
            if b is None or b == kept:
                # 基准没有该场景（双方都新增，一方为空按空场景合并）或另一方未改动 → 删除方生效
                if b is None:
                    merged["scenes"][number] = kept or {}
                continue
            # 一方删除、另一方改过内容 → 冲突；可经 resolutions 显式解决
            choice = resolutions.get(path)
            if choice == deleted:
                resolved.append({"path": path, "chosen": deleted})
                continue
            if choice in ("ours", "theirs"):
                resolved.append({"path": path, "chosen": choice})
                merged["scenes"][number] = kept
                continue
            if isinstance(choice, dict):
                resolved.append({"path": path, "chosen": "value"})
                merged["scenes"][number] = choice
                continue
            conflicts.append({"path": path, "base": b, "ours": o, "theirs": t})
            continue
        b, o, t = b or {}, o or {}, t or {}
        merged_scene = {}
        for field in set(b) | set(o) | set(t):
            value, conflict = pick(f"scenes.{number}.{field}", b.get(field), o.get(field), t.get(field))
            merged_scene[field] = value
        merged["scenes"][number] = merged_scene
    return {"content": merged, "conflicts": conflicts, "resolved": resolved, "clean": not conflicts}


def merge_to_new_version(play, base, ours, theirs, *, branch_name, user=None, resolutions=None) -> ScriptVersion:
    """合并结果落为新版本（未解决冲突时拒绝落库）。"""
    result = merge_versions(base, ours, theirs, resolutions=resolutions)
    if not result["clean"]:
        raise VersionConflict(f"存在 {len(result['conflicts'])} 处未解决冲突")
    return create_version(play, episode=ours.episode, branch_name=branch_name, user=user,
                          content=result["content"],
                          summary=f"合并 {ours.branch_name}@{ours.version} + {theirs.branch_name}@{theirs.version}",
                          parent=ours)


# ── 角色版本 ───────────────────────────────────────────────────────────────

def record_character_version(character, *, changed_fields=None, user=None) -> CharacterVersion:
    """记录角色设定快照与受影响资产（定妆照 + 对白镜头图片）。"""
    snapshot = {f: getattr(character, f) for f in CHARACTER_FIELDS}
    impacted = list(Asset.objects.filter(
        play_id=character.play_id, status=AssetStatus.READY,
    ).filter(
        models_q_character(character)
    ).values_list("pk", flat=True)[:500])
    last = CharacterVersion.objects.filter(character=character).order_by("-version").first()
    return CharacterVersion.objects.create(
        character=character, version=(last.version if last else 0) + 1, snapshot=snapshot,
        changed_fields=list(changed_fields or []), impacted_asset_ids=impacted, created_by=user)


def models_q_character(character):
    from django.db.models import Q
    return (Q(character=character, asset_type=AssetType.CHARACTER_REF)
            | Q(shot__dialogue_character=character, asset_type=AssetType.IMAGE))


def character_version_diff(v1, v2) -> dict:
    s1, s2 = v1.snapshot or {}, v2.snapshot or {}
    return {field: {"from": s1.get(field), "to": s2.get(field)}
            for field in set(s1) | set(s2) if s1.get(field) != s2.get(field)}
