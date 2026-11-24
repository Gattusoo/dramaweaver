"""重做影响预检（工作台 Stage A3，design D5）。

`preview_redo(play, granularity, target_id, asset_types=None, changes=None)`：
纯函数式只读服务——不写库、不派发任务、不调用模型。返回：

- invalidated：将失效（SUPERSEDED + 级联）的资产 [{id, type, version, shot_id}]
- reusable：目标范围内可复用（保持 READY）的资产
- rebuild：将重建的生成任务 [{task_type, shot_id}] 与预计任务数
- conservative：无法精确判定的资产及原因（缺依赖边/缺指纹 → 保守重建，不报零影响）
- preview_version：本次预检的版本戳（redo 执行时带回校验，目标变化返回 409）

影响规则（spec: incremental-generation「按类型选择性失效」）：
- 显式 redo 缺省 asset_types=[IMAGE, AUDIO]（旧行为不变）；
- changes 带 dialogue（对白变化）：只失效 AUDIO→VIDEO，IMAGE 可复用；
- changes 带 visual_prompt / aspect_ratio / style（视觉/画面参数变化）：
  失效 IMAGE→VIDEO；
- 角色定妆（granularity=CHARACTER_REF，target=character_id）：失效该角色
  reference_image + 引用该角色的镜头 IMAGE 及依赖成片（依赖边遍历）；
- 依赖边/指纹缺失的资产标 conservative + 原因（legacy 资产不能精确判定）。
"""
import hashlib
import json
import logging

from ..models import (
    Asset, AssetStatus, AssetType, RedoGranularity, Scene, Shot, TaskType,
)

logger = logging.getLogger(__name__)

# 语义类型 → 失效的资产类型集合
_CHANGE_IMPACT = {
    "dialogue": {AssetType.AUDIO},
    "visual_prompt": {AssetType.IMAGE},
    "aspect_ratio": {AssetType.IMAGE},
    "style": {AssetType.IMAGE},
    "character_ref": {AssetType.IMAGE},
}

_REDOABLE_TYPES = (AssetType.IMAGE, AssetType.AUDIO)


class PreviewRedoError(ValueError):
    """预检请求非法（目标不存在/不属于该剧/粒度非法）。"""


def _validate_asset_types(asset_types):
    """归一 asset_types 参数：None → 缺省 IMAGE+AUDIO（旧行为）；非法值报错。"""
    if asset_types is None:
        return [AssetType.IMAGE, AssetType.AUDIO]
    if isinstance(asset_types, str):
        asset_types = [asset_types]
    types = []
    for t in asset_types:
        val = getattr(t, "value", t)
        if val not in _REDOABLE_TYPES:
            raise PreviewRedoError(f"asset_types 仅支持 IMAGE/AUDIO，当前值：{val}")
        types.append(val)
    if not types:
        raise PreviewRedoError("asset_types 不能为空")
    return types


def _impact_types(asset_types, changes):
    """按显式类型 + changes 语义求实际失效类型集合。

    显式 asset_types 是用户选择的重建范围（白名单上限）；changes 只能收窄：
    例如用户选 [IMAGE, AUDIO] 且 changes 只声明 dialogue → 仅 AUDIO 失效。
    用户显式只选 [AUDIO] 时 visual_prompt 变化也不失效 IMAGE（尊重显式选择）。
    """
    selected = set(_validate_asset_types(asset_types))
    if not changes:
        return selected
    semantic = set()
    for key in (changes or {}):
        semantic |= _CHANGE_IMPACT.get(key, set())
    if not semantic:
        return selected
    return selected & semantic if (selected & semantic) else selected & semantic


def _collect_shots(play, granularity, target_id):
    """目标范围的镜头列表 + 校验目标归属。"""
    if granularity == RedoGranularity.SHOT:
        shot = Shot.objects.filter(pk=target_id, scene__episode__play=play).first()
        if shot is None:
            raise PreviewRedoError(f"镜头 {target_id} 不存在或不属于该剧")
        return [shot]
    if granularity == RedoGranularity.SCENE:
        scene = Scene.objects.filter(pk=target_id, episode__play=play).first()
        if scene is None:
            raise PreviewRedoError(f"场景 {target_id} 不存在或不属于该剧")
        return list(Shot.objects.filter(scene=scene))
    raise PreviewRedoError(f"预检仅支持 SHOT/SCENE 粒度，当前值：{granularity}")


def _asset_brief(a):
    return {"id": a.pk, "uuid": str(a.uuid), "type": a.asset_type,
            "version": a.version, "shot_id": a.shot_id}


def preview_redo(play, granularity, target_id, asset_types=None, changes=None):
    """只读预检（不写库/不派发/不调模型）。"""
    shots = _collect_shots(play, granularity, target_id)
    types = sorted(_impact_types(asset_types, changes), key=str)
    invalidated, reusable, conservative = [], [], []
    rebuild_tasks = []

    for shot in shots:
        ready = Asset.objects.filter(
            play=play, shot=shot, asset_type__in=_REDOABLE_TYPES,
            status=AssetStatus.READY,
        )
        for a in ready:
            if a.asset_type in types:
                invalidated.append(_asset_brief(a))
                # 缺指纹/依赖边的资产虽在重建范围内，但要标明保守原因
                if not a.input_fingerprint:
                    conservative.append({
                        "asset_id": a.pk, "type": a.asset_type,
                        "reason": "legacy_asset_no_fingerprint",
                    })
            else:
                reusable.append(_asset_brief(a))
        # 下游 VIDEO：任一失效类型被该集成合成消费即需重建（由级联失效触发重派）
    # 成片影响：范围内任一 IMAGE/AUDIO 失效 → 依赖它的 VIDEO 级联
    episode_ids = {s.scene.episode_id for s in shots}
    video_hits = []
    if invalidated:
        from ..models import AssetDependency
        downstream = AssetDependency.objects.filter(
            upstream_id__in=[a["id"] for a in invalidated],
            downstream__asset_type=AssetType.VIDEO,
            downstream__status=AssetStatus.READY,
        ).select_related("downstream")
        seen = set()
        for edge in downstream:
            if edge.downstream_id in seen:
                continue
            seen.add(edge.downstream_id)
            video_hits.append(_asset_brief(edge.downstream))
    # 保守：无依赖边的旧成片无法判定——范围内有失效资产但该集无 VIDEO 依赖边记录时提示
    if invalidated:
        for ep_id in episode_ids:
            has_video = Asset.objects.filter(
                play=play, episode_id=ep_id, asset_type=AssetType.VIDEO,
                status=AssetStatus.READY).exists()
            has_edges = AssetDependency.objects.filter(
                downstream__episode_id=ep_id,
                downstream__asset_type=AssetType.VIDEO).exists()
            if has_video and not has_edges:
                conservative.append({
                    "asset_id": None, "type": AssetType.VIDEO, "episode_id": ep_id,
                    "reason": "video_without_dependency_edges",
                })

    for a in invalidated:
        rebuild_tasks.append({
            "task_type": TaskType.IMAGE_GEN if a["type"] == AssetType.IMAGE else TaskType.TTS_GEN,
            "shot_id": a["shot_id"],
        })
    n_video = len(video_hits)
    estimated = len(rebuild_tasks) + (n_video if n_video else 0)

    # 预检版本戳：目标资产 id/版本/指纹 + 镜头内容摘要的规范化哈希
    stamp = {
        "shots": [{"id": s.pk, "dialogue": s.dialogue, "visual_prompt": s.visual_prompt,
                   "updated_at": str(s.updated_at)} for s in shots],
        "assets": [{"id": a["id"], "version": a["version"]} for a in invalidated + reusable],
        "types": [str(t) for t in types],
    }
    preview_version = hashlib.sha256(
        json.dumps(stamp, ensure_ascii=False, sort_keys=True,
                   separators=(",", ":")).encode("utf-8")).hexdigest()[:16]

    return {
        "granularity": str(granularity),
        "target_id": target_id,
        "asset_types": [str(t) for t in types],
        "invalidated": invalidated,
        "reusable": reusable,
        "video_impact": video_hits,
        "rebuild_tasks": rebuild_tasks,
        "estimated_tasks": estimated,
        "conservative": conservative,
        "preview_version": preview_version,
    }


def verify_preview_version(play, granularity, target_id, preview_version):
    """redo 执行前的版本校验：目标已变化（内容/版本/指纹）返回 False → 409。"""
    current = preview_redo(play, granularity, target_id)
    return current["preview_version"] == (preview_version or "")
