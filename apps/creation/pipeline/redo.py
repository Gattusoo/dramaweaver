"""重做机制：SHOT / SCENE / EPISODE 三种粒度（工作台 Stage A3 扩展选择性重做）。

重做通过将目标 Asset 标 SUPERSEDED（级联失效下游）并重新派发任务实现，
Play status 的回退由 summary_play_status 重新计算。

Stage A3（design D5）：
- SHOT/SCENE 粒度支持 asset_types 选择性重做（[IMAGE]/[AUDIO]/两者）；
  缺省 IMAGE+AUDIO 保持旧行为不变；
- 显式 redo 派发的任务带 input_summary.redo=True（跳过指纹复用，强制新版本）；
- 预检后目标变化返回 409（preview_version 校验，RedoStaleError）。
"""
import logging

from ..models import (
    Asset, AssetStatus, AssetType, EpisodeStatus, PlayStatus, RedoGranularity,
    Scene, Shot, TaskType,
)
from . import state_machine
from .dispatcher import dispatch_episode_script, dispatch_image_and_tts, dispatch_task

logger = logging.getLogger(__name__)


class RedoError(Exception):
    pass


class RedoStaleError(Exception):
    """预检版本过期：redo 执行时目标已被他人/其他操作改变，需重新预检。"""


_REDOABLE_TYPES = (AssetType.IMAGE, AssetType.AUDIO)


def _normalize_asset_types(asset_types):
    """asset_types 归一：None → 缺省 IMAGE+AUDIO（旧行为）；非法值 RedoError。"""
    if asset_types is None:
        return [AssetType.IMAGE, AssetType.AUDIO]
    if isinstance(asset_types, str):
        asset_types = [asset_types]
    types = []
    for t in asset_types:
        val = getattr(t, "value", t)
        if val not in _REDOABLE_TYPES:
            raise RedoError(f"asset_types 仅支持 IMAGE/AUDIO，当前值：{val}")
        types.append(val)
    if not types:
        raise RedoError("asset_types 不能为空")
    return types


def supersede_assets(queryset):
    """将一组 Asset 标 SUPERSEDED 并级联失效下游。"""
    for asset in queryset:
        if asset.status != AssetStatus.SUPERSEDED:
            asset.mark_superseded_recursive()


def _shot_ready_assets(shot, asset_types):
    return Asset.objects.filter(
        shot=shot, asset_type__in=asset_types,
        status=AssetStatus.READY,
    )


def _dispatch_materials(shot, asset_types):
    """按选择类型派发素材生成（显式 redo：input_summary.redo=True 跳过指纹复用）。"""
    from .dispatcher import _material_pending
    play = shot.scene.episode.play
    episode = shot.scene.episode
    records = []
    for asset_type, task_type in ((AssetType.IMAGE, TaskType.IMAGE_GEN),
                                  (AssetType.AUDIO, TaskType.TTS_GEN)):
        if asset_type not in asset_types:
            continue
        has_ready = Asset.objects.filter(
            play=play, shot=shot, asset_type=asset_type, status=AssetStatus.READY,
        ).exists()
        if has_ready:
            continue
        from .dispatcher import _ACTIVE_TASK_STATUSES, _MATERIAL_TASK_TYPE
        if _material_pending(play, shot, asset_type):
            continue
        records.append(dispatch_task(
            task_type, play=play, episode=episode, scene=shot.scene, shot=shot,
            input_summary={"redo": True}))
    return records


def redo_shot(shot, asset_types=None):
    """单镜头重做：缺省 IMAGE+AUDIO（旧行为）；AUDIO-only 不派图片任务。"""
    types = _normalize_asset_types(asset_types)
    supersede_assets(_shot_ready_assets(shot, types))
    _dispatch_materials(shot, types)


def redo_scene(scene, asset_types=None):
    """场景重做：该场景全部镜头按选择类型重做。"""
    types = _normalize_asset_types(asset_types)
    for shot in Shot.objects.filter(scene=scene):
        supersede_assets(_shot_ready_assets(shot, types))
        _dispatch_materials(shot, types)


def redo_episode(episode):
    # 失效该集产出物（保留 VIDEO 以便对比，下游级联会处理）
    assets = Asset.objects.filter(
        play=episode.play, episode=episode,
    ).exclude(asset_type=AssetType.VIDEO)
    supersede_assets(assets)
    episode.status = EpisodeStatus.SCRIPTING
    episode.save(update_fields=["status", "updated_at"])
    dispatch_episode_script(episode)
    state_machine.summary_play_status(episode.play)


def redo(play, granularity, target_id, asset_types=None, preview_version=None):
    """执行重做。granularity: SHOT/SCENE/EPISODE；target_id 为目标实体 id。

    asset_types：SHOT/SCENE 粒度的选择性重做（IMAGE/AUDIO）；缺省两者（旧行为）。
    preview_version：携带预检版本时执行前校验，目标已变化抛 RedoStaleError（409）。
    """
    if preview_version:
        from .preview_redo import PreviewRedoError, preview_redo
        try:
            # verify 用与预检一致的 asset_types（缺省 IMAGE+AUDIO）——否则
            # 失效集合不同导致版本戳必然不一致，正常请求也会被误判 409
            current = preview_redo(play, granularity, target_id, asset_types=asset_types)
            if current["preview_version"] != (preview_version or ""):
                raise RedoStaleError("预检后目标已变化，请重新预检确认")
        except PreviewRedoError:
            raise  # 目标本身非法仍走 400
    if granularity == RedoGranularity.SHOT:
        shot = Shot.objects.filter(pk=target_id, scene__episode__play=play).first()
        if shot is None:
            raise RedoError("目标 Shot 不存在或不属于该短剧")
        redo_shot(shot, asset_types)
    elif granularity == RedoGranularity.SCENE:
        scene = Scene.objects.filter(pk=target_id, episode__play=play).first()
        if scene is None:
            raise RedoError("目标 Scene 不存在或不属于该短剧")
        redo_scene(scene, asset_types)
    elif granularity == RedoGranularity.EPISODE:
        from ..models import Episode
        episode = Episode.objects.filter(pk=target_id, play=play).first()
        if episode is None:
            raise RedoError("目标 Episode 不存在或不属于该短剧")
        redo_episode(episode)
    else:
        raise RedoError(f"无效重做粒度: {granularity}")
    return play
