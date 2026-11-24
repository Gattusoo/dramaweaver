"""Play / Episode 状态机。

定义合法状态转换（仅单向前进，审核暂停态例外），提供转换校验与
Play status 汇总逻辑。重做通过 Asset SUPERSEDED + 重新派发实现，
不改变 Play status 的前进方向（回退由 redo 显式处理）。
"""
from ..models import EpisodeStatus, PlayStatus


class InvalidTransition(Exception):
    def __init__(self, obj_type, current, target):
        self.obj_type = obj_type
        self.current = current
        self.target = target
        super().__init__(f"非法状态转换: {obj_type} {current} -> {target}")


# 合法转换表（含审核暂停态）
PLAY_TRANSITIONS = {
    PlayStatus.DRAFT: {PlayStatus.OUTLINING},
    PlayStatus.OUTLINING: {PlayStatus.SCRIPTING, PlayStatus.PAUSED_AT_OUTLINE},
    PlayStatus.PAUSED_AT_OUTLINE: {PlayStatus.SCRIPTING},
    PlayStatus.SCRIPTING: {PlayStatus.STORYBOARDING, PlayStatus.PAUSED_AT_SCRIPT},
    PlayStatus.PAUSED_AT_SCRIPT: {PlayStatus.STORYBOARDING},
    PlayStatus.STORYBOARDING: {PlayStatus.ASSET_GEN},
    PlayStatus.ASSET_GEN: {PlayStatus.COMPOSING, PlayStatus.PAUSED_AT_ASSETS},
    PlayStatus.PAUSED_AT_ASSETS: {PlayStatus.COMPOSING},
    PlayStatus.COMPOSING: {PlayStatus.COMPLETED},
    PlayStatus.COMPLETED: {PlayStatus.PUBLISHED},
    PlayStatus.PUBLISHED: set(),
}

EPISODE_TRANSITIONS = {
    EpisodeStatus.PENDING: {EpisodeStatus.SCRIPTING},
    EpisodeStatus.SCRIPTING: {EpisodeStatus.STORYBOARDING},
    EpisodeStatus.STORYBOARDING: {EpisodeStatus.ASSET_GEN},
    EpisodeStatus.ASSET_GEN: {EpisodeStatus.COMPOSING},
    EpisodeStatus.COMPOSING: {EpisodeStatus.COMPLETED},
    EpisodeStatus.COMPLETED: set(),
}


def can_transition(obj, new_status):
    """Whether obj may transition to new_status."""
    if isinstance(obj.status, str):
        table = PLAY_TRANSITIONS if hasattr(obj, "episodes") else EPISODE_TRANSITIONS
    else:
        table = PLAY_TRANSITIONS
    allowed = table.get(obj.status, set())
    return new_status in allowed


def validate_transition(obj, new_status):
    """Raise InvalidTransition if the transition is illegal."""
    if not can_transition(obj, new_status):
        raise InvalidTransition(obj.__class__.__name__, obj.status, new_status)


def transition(obj, new_status, save=True):
    """Validate and apply a state transition. Returns obj."""
    validate_transition(obj, new_status)
    obj.status = new_status
    if save:
        obj.save(update_fields=["status", "updated_at"])
    return obj


_PLAY_LINEAR = [
    PlayStatus.DRAFT, PlayStatus.OUTLINING, PlayStatus.SCRIPTING, PlayStatus.STORYBOARDING,
    PlayStatus.ASSET_GEN, PlayStatus.COMPOSING, PlayStatus.COMPLETED, PlayStatus.PUBLISHED,
]
_EPISODE_LINEAR = [
    EpisodeStatus.PENDING, EpisodeStatus.SCRIPTING, EpisodeStatus.STORYBOARDING,
    EpisodeStatus.ASSET_GEN, EpisodeStatus.COMPOSING, EpisodeStatus.COMPLETED,
]


def advance_play_to(play, target):
    """把 Play 沿主线前进到 target（只进不退）。

    暂停态不会被自动离开（只能由审核通过离开）；已在 target 或之后则不动。
    用条件 UPDATE，并发事件不会把状态往回写。不经 save()，不触发 post_save 派发。
    """
    from django.utils import timezone
    from ..models import Play
    if play.status not in _PLAY_LINEAR or target not in _PLAY_LINEAR:
        return play
    if _PLAY_LINEAR.index(target) <= _PLAY_LINEAR.index(play.status):
        return play
    Play.objects.filter(pk=play.pk, status=play.status).update(status=target, updated_at=timezone.now())
    play.refresh_from_db(fields=["status"])
    return play


def advance_episode_to(episode, target):
    """把 Episode 沿主线前进到 target（只进不退；回退由驳回/重做显式处理）。"""
    from django.utils import timezone
    from ..models import Episode
    if episode.status not in _EPISODE_LINEAR:
        return episode
    if _EPISODE_LINEAR.index(target) <= _EPISODE_LINEAR.index(episode.status):
        return episode
    Episode.objects.filter(pk=episode.pk, status=episode.status).update(
        status=target, updated_at=timezone.now())
    episode.refresh_from_db(fields=["status"])
    return episode


def advance(play):
    """Advance a Play one stage forward based on audit mode (no pause)."""
    order = [
        PlayStatus.DRAFT, PlayStatus.OUTLINING, PlayStatus.SCRIPTING,
        PlayStatus.STORYBOARDING, PlayStatus.ASSET_GEN, PlayStatus.COMPOSING,
        PlayStatus.COMPLETED, PlayStatus.PUBLISHED,
    ]
    idx = order.index(play.status)
    if idx < len(order) - 1:
        return transition(play, order[idx + 1])
    return play


def summary_play_status(play):
    """Recalculate Play status from Episode statuses.

    所有 Episode COMPLETED -> Play COMPLETED；否则取未完成分集中的最高进度。
    重做回退：任一 Episode 从 COMPLETED 回退，Play 也随之回退。
    """
    episodes = list(play.episodes.all())
    if not episodes:
        return play
    # PUBLISHED 为终态，不因 Episode 重做/再渲染回退为 COMPLETED
    if play.status == PlayStatus.PUBLISHED:
        return play
    if all(ep.status == EpisodeStatus.COMPLETED for ep in episodes):
        play.status = PlayStatus.COMPLETED
        play.save(update_fields=["status", "updated_at"])
        return play

    # 进度排名
    rank = {
        EpisodeStatus.PENDING: 0,
        EpisodeStatus.SCRIPTING: 1,
        EpisodeStatus.STORYBOARDING: 2,
        EpisodeStatus.ASSET_GEN: 3,
        EpisodeStatus.COMPOSING: 4,
        EpisodeStatus.COMPLETED: 5,
    }
    # 取未完成分集中的最高进度：已完成分集不能把 Play 顶到 COMPLETED，
    # 这样任一分集重做回退时 Play 会随之回退（上面已排除全部完成的情况）
    max_rank = max(
        rank.get(ep.status, 0) for ep in episodes if ep.status != EpisodeStatus.COMPLETED
    )
    mapping = {
        # 汇总只在大纲完成之后发生（成片就绪 / 发布）；尚未开始的分集不能把 Play 拉回 DRAFT，
        # 否则后续从 DRAFT 出发的转换/暂停都会 InvalidTransition
        0: PlayStatus.SCRIPTING,
        1: PlayStatus.SCRIPTING,
        2: PlayStatus.STORYBOARDING,
        3: PlayStatus.ASSET_GEN,
        4: PlayStatus.COMPOSING,
        5: PlayStatus.COMPLETED,
    }
    # 暂停态不覆盖
    if play.status in {
        PlayStatus.PAUSED_AT_OUTLINE,
        PlayStatus.PAUSED_AT_SCRIPT,
        PlayStatus.PAUSED_AT_ASSETS,
    }:
        return play
    play.status = mapping.get(max_rank, PlayStatus.DRAFT)
    play.save(update_fields=["status", "updated_at"])
    return play
