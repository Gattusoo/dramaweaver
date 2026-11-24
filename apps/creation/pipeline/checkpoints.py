"""审核检查点：按 audit_mode 在关键步骤暂停流水线，等待人工审核。

- NONE：无暂停，全自动
- KEY_CHECKPOINTS：大纲 / 分集剧本 / 素材 3 个检查点暂停
- EVERY_STEP：每步暂停
"""
from django.utils import timezone

from ..models import (
    Asset, AssetStatus, AssetType, AuditMode, EpisodeStatus, Play, PlayStatus,
    ReviewDecision, ReviewLog, ReviewPoint,
)
from . import state_machine

# review_point -> (暂停态, 恢复后的 Play 状态)
_POINT_TO_PAUSE = {
    ReviewPoint.OUTLINE: (PlayStatus.PAUSED_AT_OUTLINE, PlayStatus.SCRIPTING),
    ReviewPoint.EPISODE_SCRIPT: (PlayStatus.PAUSED_AT_SCRIPT, PlayStatus.STORYBOARDING),
    ReviewPoint.ASSETS: (PlayStatus.PAUSED_AT_ASSETS, PlayStatus.COMPOSING),
}

_KEY_POINTS = {ReviewPoint.OUTLINE, ReviewPoint.EPISODE_SCRIPT, ReviewPoint.ASSETS}


def should_pause(play, review_point):
    """Whether the pipeline should pause at this review point."""
    mode = play.audit_mode
    if mode == AuditMode.NONE:
        return False
    if mode == AuditMode.EVERY_STEP:
        return True
    return review_point in _KEY_POINTS


_PAUSE_PREDECESSOR = {
    ReviewPoint.OUTLINE: PlayStatus.OUTLINING,
    ReviewPoint.EPISODE_SCRIPT: PlayStatus.SCRIPTING,
    ReviewPoint.ASSETS: PlayStatus.ASSET_GEN,
}


def pause_state(play, review_point):
    """该审核点相对 Play 当前位置：'paused'（已停在此点）/ 'before' / 'after'。"""
    pause_status, resume_status = _POINT_TO_PAUSE[review_point]
    if play.status == pause_status:
        return "paused"
    linear = state_machine._PLAY_LINEAR
    if play.status not in linear:
        # 停在别的审核点：比较该审核点与本审核点的先后
        order = [PlayStatus.PAUSED_AT_OUTLINE, PlayStatus.PAUSED_AT_SCRIPT, PlayStatus.PAUSED_AT_ASSETS]
        return "before" if order.index(play.status) < order.index(pause_status) else "after"
    return "after" if linear.index(play.status) >= linear.index(resume_status) else "before"


def apply_pause(play, review_point):
    """把 Play 停到对应 PAUSED_AT_*：先沿主线推进到暂停前一状态，已暂停则幂等返回。

    Play 已越过该审核点（如分集重做后剧本再次就绪）时不暂停，返回 play 不变。
    """
    pause_status, _ = _POINT_TO_PAUSE.get(review_point, (None, None))
    if pause_status is None:
        return play
    play.refresh_from_db(fields=["status"])
    if play.status == pause_status or pause_state(play, review_point) == "after":
        return play
    if play.status not in state_machine._PLAY_LINEAR:
        # 仍停在更早的审核点：等那个审核点通过后再说，不在这里越级暂停
        return play
    state_machine.advance_play_to(play, _PAUSE_PREDECESSOR[review_point])
    updated = Play.objects.filter(pk=play.pk, status=_PAUSE_PREDECESSOR[review_point]).update(
        status=pause_status, updated_at=timezone.now(),
    )
    play.refresh_from_db(fields=["status"])
    if not updated and play.status != pause_status:
        raise state_machine.InvalidTransition("Play", play.status, pause_status)
    return play


class CheckpointConflict(Exception):
    """短剧当前不处于该审核点对应的暂停态（重复通过或状态已变化）。"""


def _claim_resume(play, review_point):
    """原子地把 PAUSED_AT_* 切到恢复态；不在对应暂停态时抛 CheckpointConflict。

    用条件 UPDATE 而非先读后写，两个并发 approve 只有一个能成功，
    从而保证下游任务只派发一次。无暂停映射的审核点（如 CHARACTER_REF）不做门禁。
    """
    pause_status, resume_status = _POINT_TO_PAUSE.get(review_point, (None, None))
    if pause_status is None:
        return
    updated = Play.objects.filter(pk=play.pk, status=pause_status).update(
        status=resume_status, updated_at=timezone.now(),
    )
    if not updated:
        play.refresh_from_db(fields=["status"])
        raise CheckpointConflict(
            f"短剧当前状态为 {play.status}，不在 {review_point} 审核点，无法通过"
        )
    play.status = resume_status


def approve(play, review_point, reviewer, comment=""):
    """审核通过：清除暂停态，创建 ReviewLog，恢复并派发下游。"""
    _claim_resume(play, review_point)
    ReviewLog.objects.create(
        play=play, review_point=review_point, reviewer=reviewer,
        decision=ReviewDecision.APPROVED, comment=comment,
    )
    from .dispatcher import resume_dispatch
    resume_dispatch(play, review_point)
    return play


def reject(play, review_point, reviewer, comment="", episode=None):
    """审核驳回：仅在对应暂停态可用；创建 ReviewLog，失效对应产出物，回退并重派一次。

    行锁串行化：并发驳回只有一个通过门禁，其余抛 CheckpointConflict。
    """
    from django.db import transaction
    pause_status, _ = _POINT_TO_PAUSE.get(review_point, (None, None))
    if pause_status is None:
        raise CheckpointConflict(f"审核点 {review_point} 不支持驳回")
    with transaction.atomic():
        locked = Play.objects.select_for_update().get(pk=play.pk)
        if locked.status != pause_status:
            play.status = locked.status
            raise CheckpointConflict(
                f"短剧当前状态为 {locked.status}，不在 {review_point} 审核点，无法驳回"
            )
        ReviewLog.objects.create(
            play=play, review_point=review_point, reviewer=reviewer, episode=episode,
            decision=ReviewDecision.REJECTED, comment=comment,
        )
        _supersede_for_reject(play, review_point, episode)
    from .dispatcher import reject_dispatch
    reject_dispatch(play, review_point, episode)
    return play


def approve_modified(play, review_point, reviewer, comment="", modifications=None):
    """审核修改：创建 ReviewLog(MODIFIED)，继续派发（内容已由前端更新）。"""
    _claim_resume(play, review_point)
    ReviewLog.objects.create(
        play=play, review_point=review_point, reviewer=reviewer,
        decision=ReviewDecision.MODIFIED, comment=comment,
        modifications=modifications or {},
    )
    from .dispatcher import resume_dispatch
    resume_dispatch(play, review_point)
    return play


def _supersede_for_reject(play, review_point, episode=None):
    """按审核点失效对应产出物。"""
    target = None
    if review_point == ReviewPoint.OUTLINE:
        target = Asset.objects.filter(
            play=play, asset_type=AssetType.OUTLINE
        )
        # 用 QuerySet.update 回到 OUTLINING：save() 会触发 _play_post_save 再派一次大纲，
        # 而 reject_dispatch 还会派一次，导致两个大纲任务
        Play.objects.filter(pk=play.pk).update(status=PlayStatus.OUTLINING, updated_at=timezone.now())
        play.status = PlayStatus.OUTLINING
    elif review_point == ReviewPoint.EPISODE_SCRIPT:
        ep = episode or play.episodes.first()
        if ep:
            target = Asset.objects.filter(play=play, episode=ep, asset_type=AssetType.EPISODE_SCRIPT)
            ep.status = EpisodeStatus.SCRIPTING
            ep.save(update_fields=["status", "updated_at"])
    elif review_point == ReviewPoint.ASSETS:
        ep = episode or play.episodes.first()
        if ep:
            target = Asset.objects.filter(play=play, episode=ep).exclude(
                asset_type__in=[AssetType.VIDEO, AssetType.BGM]
            )
            ep.status = EpisodeStatus.ASSET_GEN
            ep.save(update_fields=["status", "updated_at"])
    if target is not None:
        for asset in target:
            if asset.status != AssetStatus.SUPERSEDED:
                asset.mark_superseded_recursive()
