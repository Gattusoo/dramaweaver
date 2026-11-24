"""智能批量生成编排（主计划 3.5.3.7 / design D5）。

按场景或分集批量派发 IMAGE/AUDIO 素材生成——本质是既有派发守卫链
（已就绪跳过 / 在跑去重 / 无对白不派配音 / 计费闸逐任务判定）的批量
编排层，一次请求聚合呈现三桶结果。不做跨请求合并（IMAGE/TTS 受理即
计费，合并语义只在文本提案层成立——与 assistant/batching.py 边界
一致）；不做批量 redo（v1 明示排除，避免一次批量把整集旧图全作废）。
"""
import logging

from ..models import Asset, AssetStatus, AssetType, Scene, Shot, TaskType

logger = logging.getLogger(__name__)


class BatchGenerateError(ValueError):
    """批量请求本身非法（scope/target 不存在/跨剧/asset_types 非法）。"""


def _collect_shots(play, scope, target_id):
    if scope == "scene":
        scene = Scene.objects.filter(pk=target_id, episode__play=play).first()
        if scene is None:
            raise BatchGenerateError(f"场景 {target_id} 不存在或不属于该剧")
        return list(Shot.objects.filter(scene=scene).order_by("shot_number"))
    if scope == "episode":
        from ..models import Episode
        ep = Episode.objects.filter(pk=target_id, play=play).first()
        if ep is None:
            raise BatchGenerateError(f"分集 {target_id} 不存在或不属于该剧")
        return list(Shot.objects.filter(scene__episode=ep)
                    .order_by("scene__scene_number", "shot_number"))
    raise BatchGenerateError(f"未知 scope: {scope}（支持 scene | episode）")


def _normalize_asset_types(asset_types):
    """asset_types 归一：None → 缺省 [IMAGE, AUDIO]；非法值 BatchGenerateError。"""
    if asset_types is None:
        return [AssetType.IMAGE, AssetType.AUDIO]
    if isinstance(asset_types, str):
        asset_types = [asset_types]
    valid = {AssetType.IMAGE, AssetType.AUDIO}
    unknown = [t for t in asset_types if t not in valid]
    if unknown:
        raise BatchGenerateError(f"asset_types 仅支持 IMAGE/AUDIO，收到: {unknown}")
    types = [t for t in (AssetType.IMAGE, AssetType.AUDIO) if t in asset_types]
    if not types:
        raise BatchGenerateError("asset_types 不能为空")
    return types


def batch_generate(play, scope, target_id, asset_types=None):
    """批量派发素材生成，返回三桶聚合结果。

    - dispatched：守卫全通过、已派发（含 TaskRecord id；计费闸拒绝的记录
      会落为 FAILED TaskRecord——按既有语义，闸拒绝即任务终态，归入
      skipped 桶并带 reason=billing_gate 与 record id 供前端展示）；
    - reused：已有 READY 资产无需生成（与 dispatch_image_and_tts 的
      has_ready 跳过语义一致——批量默认不带 redo，不作废既有就绪资产；
      三桶口径中的"已就绪跳过"即此桶）；
    - skipped：守卫拦截明细（in_progress / no_dialogue / billing_gate）。

    逐镜头逐类型走既有守卫（dispatcher._material_pending /
    dispatch_task 内的计费闸与冻结剧拒绝），本层只聚合呈现不新增守卫语义。
    """
    from .dispatcher import _material_pending, dispatch_task

    shots = _collect_shots(play, scope, target_id)
    types = _normalize_asset_types(asset_types)
    task_type_of = {AssetType.IMAGE: TaskType.IMAGE_GEN, AssetType.AUDIO: TaskType.TTS_GEN}

    dispatched, reused, skipped = [], [], []
    for shot in shots:
        episode = shot.scene.episode
        for asset_type in types:
            entry = {"shot_id": shot.pk, "asset_type": asset_type}
            has_ready = Asset.objects.filter(
                play=play, shot=shot, asset_type=asset_type,
                status=AssetStatus.READY,
            ).exists()
            if has_ready:
                reused.append({**entry, "reason": "already_ready"})
                continue
            if asset_type == AssetType.AUDIO and not (shot.dialogue or "").strip():
                skipped.append({**entry, "reason": "no_dialogue"})
                continue
            if _material_pending(play, shot, asset_type):
                skipped.append({**entry, "reason": "in_progress"})
                continue
            record = dispatch_task(
                task_type_of[asset_type], play=play, episode=episode,
                scene=shot.scene, shot=shot,
            )
            if record.status == "FAILED":
                # 计费闸拒绝（冻结剧拒绝在 dispatch_task 更早抛 PlayFrozenError；
                # 计费闸置 FAILED 落审计）：归 skipped 桶，不吞错
                skipped.append({**entry, "reason": "billing_gate",
                                "task_record_id": record.pk,
                                "error": record.error_message})
            else:
                dispatched.append({**entry, "task_record_id": record.pk})
    return {
        "dispatched": dispatched,
        "reused": reused,
        "skipped": skipped,
        "summary": {
            "dispatched": len(dispatched),
            "reused": len(reused),
            "skipped": len(skipped),
            "shot_count": len(shots),
        },
    }
