"""事件驱动的 Task Dispatcher。

监听实体状态变化（Asset 变为 READY / Play 进入 OUTLINING），检查下游
依赖就绪后派发对应的 Celery 任务并创建 TaskRecord。并行编排通过逐任务
send_task 扇出实现（等效 group）；"某集素材全部就绪" 的汇总通过
handle_material_ready 的检查实现（等效 chord）。
"""
import logging

from django.db.models.signals import post_save, pre_save

from ..models import (
    Asset, AssetStatus, AssetType, EpisodeStatus, Play, PlayStatus, ReviewPoint,
    Shot, TaskRecord, TaskStatus, TaskType,
)
from . import state_machine

logger = logging.getLogger(__name__)


class PlayFrozenError(Exception):
    """短剧被平台管理员冻结，生成/发布类任务拒绝派发（业务码 4003）。"""

    def __init__(self, play):
        self.play = play
        super().__init__(f"短剧已被冻结: {play.frozen_reason}")

# TaskType -> Celery 任务名
TASK_NAME_MAP = {
    TaskType.OUTLINE: "shortplay.tasks.generate_outline",
    TaskType.EPISODE_SCRIPT: "shortplay.tasks.generate_episode_script",
    TaskType.CHARACTER_REF: "shortplay.tasks.generate_character_ref",
    TaskType.STORYBOARD: "shortplay.tasks.generate_storyboard",
    TaskType.IMAGE_GEN: "shortplay.tasks.generate_image",
    TaskType.TTS_GEN: "shortplay.tasks.generate_tts",
    TaskType.BGM_SELECT: "shortplay.tasks.select_bgm",
    TaskType.VIDEO_COMPOSE: "shortplay.tasks.compose_video",
    TaskType.TIMELINE_EXPORT: "shortplay.tasks.export_composition",
    TaskType.PUBLISH: "shortplay.tasks.trigger_publish",
    TaskType.RENDITION: "shortplay.tasks.render_channel_version",
    TaskType.PREVIEW_RENDER: "shortplay.tasks.render_preview",
    TaskType.DERIVATIVES: "shortplay.tasks.generate_derivatives",
    TaskType.ASSISTANT_TURN: "shortplay.assistant.run_assistant_turn",
    TaskType.SUBTITLE_GEN: "shortplay.tasks.generate_subtitle",
    TaskType.NOVEL_ADAPT: "shortplay.tasks.adapt_novel",
}


def send_task(task_name, args=None, kwargs=None):
    """按任务后端发送（工作台 Stage C1：local/celery 双轨适配）。

    local：查受控注册表并持久化执行记录（真实 UUID），由 run_local_worker 领取。
    celery（缺省）：原 Celery send_task。形状兼容——均返回带 .id 的轻量结果。
    """
    from .local_backend import send_task as _send
    return _send(task_name, args=args, kwargs=kwargs)


# ─── 计费派发闸（billing 变更接线）────────────────────────────────────────
# 槽位类别：渲染类占 render（默认并发 2），其余生成/助手任务占 ai（默认并发 10）；
# PUBLISH 不占槽（外部动作非算力消费）。类别决定 quotas.acquire_slot 的限额键。
_SLOT_KINDS = {
    TaskType.VIDEO_COMPOSE: "render",
    TaskType.PREVIEW_RENDER: "render",
    TaskType.RENDITION: "render",
    TaskType.TIMELINE_EXPORT: "render",
    TaskType.OUTLINE: "ai",
    TaskType.EPISODE_SCRIPT: "ai",
    TaskType.STORYBOARD: "ai",
    TaskType.CHARACTER_REF: "ai",
    TaskType.IMAGE_GEN: "ai",
    TaskType.TTS_GEN: "ai",
    TaskType.BGM_SELECT: "ai",
    TaskType.DERIVATIVES: "ai",
    TaskType.ASSISTANT_TURN: "ai",
    TaskType.NOVEL_ADAPT: "ai",
    TaskType.SUBTITLE_GEN: "ai",
}

# 每日渲染时长累计的渲染类（与 _SLOT_KINDS 的 render 对齐）
_RENDER_TASK_TYPES = frozenset(
    t for t, kind in _SLOT_KINDS.items() if kind == "render")


def _billing_gate(record, task_type, play):
    """计费派发闸：槽位-预扣顺序（spec billing/quotas）。

    通过 → 返回 HoldResult|None（写 input_summary["billing_hold"]）并置位已发凭证；
    拒绝 → record 置 FAILED（error_message 记原因），返回 None。
    估算失败（无单价）以 0 预扣照常派发；ENABLE_BILLING_HOLD=false / local 短路。
    """
    from apps.billing.services.gate import (
        InsufficientPoints, QuotaExceeded, dispatch_gate, release_after_dispatch,
    )
    from apps.billing.services.quotas import QuotaExceeded as _QE, check_daily_render
    from apps.billing.services.wallet import yuan_to_points
    from ..assistant.cost_estimator import CostEstimateError, estimate_task_cost

    slot_kind = _SLOT_KINDS.get(task_type)
    if slot_kind == "render":
        try:
            check_daily_render(play.created_by, duration_seconds_default(play, task_type))
        except _QE as exc:
            record.status = TaskStatus.FAILED
            record.error_message = f"每日渲染时长超限：{exc}"
            record.save(update_fields=["status", "error_message"])
            return None

    estimate = 0
    try:
        est = estimate_task_cost(play, task_type)
        estimate = yuan_to_points(est["estimated_cost"])
    except CostEstimateError:
        logger.warning("TaskRecord#%s 计价数据缺失，以 0 预扣派发", record.pk)
    except Exception:  # noqa: BLE001 — 估算异常不阻断创作（0 预扣只占槽位）
        logger.warning("TaskRecord#%s 预扣估算失败，以 0 预扣派发", record.pk, exc_info=True)

    try:
        hold = dispatch_gate(play.created_by, ("task_record", record.pk), estimate,
                             slot_kind=slot_kind)
    except QuotaExceeded as exc:
        record.status = TaskStatus.FAILED
        record.error_message = f"并发槽位已满：{exc}"
        record.save(update_fields=["status", "error_message"])
        return None
    except InsufficientPoints as exc:
        record.status = TaskStatus.FAILED
        record.error_message = f"钱包余额不足：{exc}"
        record.save(update_fields=["status", "error_message"])
        return None

    if hold is not None:
        # hold 结果写 input_summary（零迁移）：终态对账读 entry_id/amount；
        # settle/refund 内部幂等，记录丢失时按 no-op 处理（见 _billing_settle）
        input_summary = dict(record.input_summary or {})
        input_summary["billing_hold"] = {"entry_id": hold.entry.pk, "amount": hold.entry.amount}
        record.input_summary = input_summary
        record.save(update_fields=["input_summary"])
    if slot_kind:
        # 槽位约束派发并发而非任务时长（design D5）：消息发出后立即释放
        release_after_dispatch(play.created_by, hold, slot_kind)
    return True


def duration_seconds_default(play, task_type):
    """渲染类任务的日限预检时长（与 estimate_task_cost 的默认规模一致：60s）。"""
    return 60


def _billing_settle(record):
    """任务终态对账：成功按实际费用结算差额，失败/取消整额退回。

    hold 信息从 input_summary["billing_hold"] 读（dispatch 侧写入）；
    缺失（闸旁路/估算 0/记录被外部创建）时 no-op。settle/refund 幂等：
    hold 已终态时重复调用不产生新流水。
    """
    info = (record.input_summary or {}).get("billing_hold")
    if not info or not info.get("entry_id"):
        return
    from apps.billing.models import WalletEntry
    from apps.billing.services.gate import TaskOutcome
    from apps.billing.services.wallet import yuan_to_points
    try:
        entry = WalletEntry.objects.get(pk=info["entry_id"], type="hold")
    except WalletEntry.DoesNotExist:
        logger.warning("TaskRecord#%s billing_hold entry %s 不存在，跳过对账",
                       record.pk, info["entry_id"])
        return
    try:
        if record.status == TaskStatus.SUCCESS:
            actual = yuan_to_points(record.cost or 0)
            TaskOutcome.success(_HoldShim(entry), actual, note=f"task_record:{record.pk}")
            if record.task_type in _RENDER_TASK_TYPES:
                # 每日渲染时长按实际累计（spec billing/quotas）：output 的
                # duration_seconds 是成片实际时长；缺省 0 不累计
                duration = int((record.output_summary or {}).get("duration_seconds") or 0)
                if duration > 0:
                    from apps.billing.services.quotas import consume_daily_render
                    consume_daily_render(record.play.created_by_id, duration)
        else:
            TaskOutcome.failure(_HoldShim(entry),
                                reason=f"task_record:{record.pk} {record.status}")
    except Exception:  # noqa: BLE001 — 对账失败不影响任务终态（可重放，幂等兜底）
        logger.exception("TaskRecord#%s 计费对账失败（hold entry=%s）",
                          record.pk, info["entry_id"])


class _HoldShim:
    """把 WalletEntry 适配成 TaskOutcome 需要的 HoldResult 形状（entry 属性）。"""

    def __init__(self, entry):
        self.entry = entry


def dispatch_task(task_type, *, play, episode=None, scene=None, shot=None,
                  asset=None, input_summary=None, defer_send=False, **kwargs):
    """创建 TaskRecord(PENDING) 并发送 Celery 任务。返回 TaskRecord。

    defer_send=True：TaskRecord 立即创建（调用方可拿到 id），Celery 消息等事务提交后再发，
    避免 worker 读到未提交的记录。
    冻结剧拒绝派发（业务码 4003）：属主可见冻结原因（spec: auth/object-permissions 冻结场景）。

    生成快照（工作台 Stage A2，design D4）：图片/配音/视频等生成类任务在派发时把
    生成所需的配置快照（模型、有效创作参数）写入 input_summary["generation"]，
    worker 从快照构建规格——排队期间用户改 Play/配方不会改变已派发请求的输入；
    快照只含模型标识与参数选择，不含 API Key/凭证（TaskRecord 不落任何凭证）。
    """
    if play is not None and play.frozen_reason:
        raise PlayFrozenError(play)
    input_summary = dict(input_summary or {})
    if task_type in _GENERATION_TASK_TYPES:
        input_summary["generation"] = _generation_snapshot(
            task_type, play, episode=episode, scene=scene, shot=shot)
    record = TaskRecord.objects.create(
        play=play, episode=episode, scene=scene, shot=shot, asset=asset,
        task_type=task_type, status=TaskStatus.PENDING,
        input_summary=input_summary,
    )
    # 计费派发闸（billing 变更接线）：槽位满/余额不足置 FAILED 落审计，
    # 消息不发出（调用方拿到 FAILED 记录继续流程，事件流可见）。
    if play is not None and play.created_by is not None and _billing_gate(record, task_type, play) is None:
        return record
    task_name = TASK_NAME_MAP[task_type]
    if defer_send:
        from django.db import transaction
        transaction.on_commit(lambda: _send_record(record, task_name, kwargs))
        return record
    _send_record(record, task_name, kwargs)
    logger.info("已派发 %s TaskRecord#%s", task_type, record.pk)
    return record


# 生成类任务：派发时保存不可变生成快照（design D4「模型与参数在派发前生成不可变任务快照」）
_GENERATION_TASK_TYPES = {
    TaskType.IMAGE_GEN, TaskType.TTS_GEN, TaskType.VIDEO_COMPOSE,
    TaskType.PREVIEW_RENDER, TaskType.CHARACTER_REF, TaskType.BGM_SELECT,
    TaskType.RENDITION, TaskType.DERIVATIVES, TaskType.SUBTITLE_GEN,
}


def _generation_snapshot(task_type, play, episode=None, scene=None, shot=None):
    """构建派发时的生成配置快照（不含凭证）。

    内容：任务类型、模型标识（配方各链当前选择的主模型 id/name）、有效创作参数
    （resolve_gen_params 的 effective——aspect_ratio/style 等在派发时定格）。
    worker 侧从快照读模型与参数（快照缺失时回落实时配置，兼容存量任务）。
    """
    from .gen_params import resolve_gen_params
    config = getattr(play, "agent_config", None)
    snapshot = {"task_type": str(task_type)}
    for label, field in (("llm_model", "llm_model"), ("tts_model", "tts_model"),
                         ("image_model", "image_model"), ("video_model", "video_model")):
        m = getattr(config, field, None) if config else None
        if m is not None:
            snapshot[label] = {"id": m.pk, "name": m.model_name}
    try:
        resolved = resolve_gen_params(play, config)
        if resolved["effective"]:
            snapshot["effective_params"] = resolved["effective"]
    except Exception:  # noqa: BLE001 — 参数解析失败不阻断派发（worker 回落实时配置）
        pass
    return snapshot


def _send_record(record, task_name, kwargs):
    result = send_task(task_name, args=[record.pk], kwargs=kwargs)
    if result is not None:
        record.celery_task_id = getattr(result, "id", "") or ""
        TaskRecord.objects.filter(pk=record.pk).update(celery_task_id=record.celery_task_id)
    else:
        # broker 不可用：消息未发出。标记 FAILED（保留审计），使 dedup 不再把
        # "PENDING + 空 celery_task_id" 当作进行中任务而永久阻塞后续派发。
        TaskRecord.objects.filter(pk=record.pk).update(
            status=TaskStatus.FAILED,
            error_message="发送 Celery 消息失败（broker 不可用）",
        )


# ─── 具体派发动作 ──────────────────────────────────────────────────────────

def dispatch_outline(play):
    return dispatch_task(TaskType.OUTLINE, play=play)


def dispatch_episode_script(episode):
    state_machine.advance_episode_to(episode, EpisodeStatus.SCRIPTING)
    return dispatch_task(
        TaskType.EPISODE_SCRIPT, play=episode.play, episode=episode,
        input_summary={"episode_number": episode.episode_number},
    )


def dispatch_character_ref(play):
    records = []
    for character in play.characters.all():
        has_ready = Asset.objects.filter(
            play=play, character=character, asset_type=AssetType.CHARACTER_REF,
            status=AssetStatus.READY,
        ).exists()
        if has_ready:
            continue
        # 多集并行就绪时同一角色会被重复派发；按 input_summary.character 去重在跑任务
        pending = TaskRecord.objects.filter(
            play=play, task_type=TaskType.CHARACTER_REF,
            status__in=_ACTIVE_TASK_STATUSES,
            input_summary__character=character.pk,
        ).exists()
        if pending:
            continue
        records.append(
            dispatch_task(TaskType.CHARACTER_REF, play=play, asset=None,
                          input_summary={"character": character.pk})
        )
    return records


def dispatch_storyboard(scene):
    return dispatch_task(
        TaskType.STORYBOARD, play=scene.episode.play, episode=scene.episode,
        scene=scene, input_summary={"scene_number": scene.scene_number},
    )


_ACTIVE_TASK_STATUSES = (TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRYING)
_MATERIAL_TASK_TYPE = {AssetType.IMAGE: TaskType.IMAGE_GEN, AssetType.AUDIO: TaskType.TTS_GEN}


def _material_pending(play, shot, asset_type):
    """该镜头是否已有在跑的素材生成任务（防止并发/双击重复提交付费生成）。"""
    return TaskRecord.objects.filter(
        play=play, shot=shot, task_type=_MATERIAL_TASK_TYPE[asset_type],
        status__in=_ACTIVE_TASK_STATUSES,
    ).exists()


def dispatch_image_and_tts(shot):
    records = []
    play = shot.scene.episode.play
    episode = shot.scene.episode
    for asset_type, task_type in ((AssetType.IMAGE, TaskType.IMAGE_GEN),
                                  (AssetType.AUDIO, TaskType.TTS_GEN)):
        has_ready = Asset.objects.filter(
            play=play, shot=shot, asset_type=asset_type, status=AssetStatus.READY,
        ).exists()
        if has_ready:
            # 已有 READY 资产：但若正有进行中的任务（重做已作废旧资产后又派发过一次），
            # 不再重复派；无进行中任务时直接跳过。
            continue
        if _material_pending(play, shot, asset_type):
            continue
        records.append(dispatch_task(task_type, play=play, episode=episode,
                                     scene=shot.scene, shot=shot))
    return records


def dispatch_publish(episode):
    return dispatch_task(TaskType.PUBLISH, play=episode.play, episode=episode)


# ─── 资产预览生成（主计划 3.5.3.5 / design D2）────────────────────────────

def preview_pending(shot):
    """该镜头是否已有进行中的预览生成任务（防重复点击重复付费）。"""
    return TaskRecord.objects.filter(
        play=shot.scene.episode.play, task_type=TaskType.IMAGE_GEN,
        status__in=_ACTIVE_TASK_STATUSES,
        input_summary__preview=True, input_summary__shot=shot.pk,
    ).exists()


def dispatch_image_preview(shot):
    """派发低分辨率预览草稿图任务（复用 IMAGE_GEN 通道：计费闸/冻结剧拒绝/
    生成快照全部生效）。同镜头已有进行中预览 → 返回 None（调用方 409）。"""
    play = shot.scene.episode.play
    if preview_pending(shot):
        return None
    return dispatch_task(
        TaskType.IMAGE_GEN, play=play, episode=shot.scene.episode,
        scene=shot.scene, shot=shot,
        input_summary={"preview": True, "shot": shot.pk},
    )


# ─── 事件处理 ──────────────────────────────────────────────────────────────

def handle_asset_ready(asset):
    """Asset 变为 READY 后派发下游。"""
    play = asset.play
    if asset.asset_type == AssetType.OUTLINE:
        _after_outline_ready(play)
    elif asset.asset_type == AssetType.EPISODE_SCRIPT and asset.episode:
        _after_episode_script_ready(play, asset.episode)
    elif asset.asset_type == AssetType.STORYBOARD and asset.shot:
        _after_storyboard_ready(asset.shot)
    elif asset.asset_type in (AssetType.IMAGE, AssetType.AUDIO) and asset.episode:
        _after_material_ready(play, asset.episode)
    elif asset.asset_type == AssetType.VIDEO and asset.episode:
        _after_video_ready(asset.episode)


def _after_outline_ready(play):
    from .checkpoints import apply_pause, should_pause
    if play.status == PlayStatus.DRAFT:
        state_machine.transition(play, PlayStatus.OUTLINING)
    if should_pause(play, ReviewPoint.OUTLINE):
        apply_pause(play, ReviewPoint.OUTLINE)
        return
    state_machine.advance_play_to(play, PlayStatus.SCRIPTING)
    for ep in play.episodes.all():
        dispatch_episode_script(ep)


def _after_episode_script_ready(play, episode):
    from .checkpoints import apply_pause, pause_state, should_pause
    play.refresh_from_db(fields=["status"])
    state_machine.advance_episode_to(episode, EpisodeStatus.STORYBOARDING)
    if should_pause(play, ReviewPoint.EPISODE_SCRIPT) and pause_state(play, ReviewPoint.EPISODE_SCRIPT) != "after":
        # 剧本审核点：等全部分集剧本就绪后统一暂停；审核通过时 resume_dispatch 再派分镜，
        # 这里提前派会和恢复派发重复
        if _all_episodes_script_ready(play):
            apply_pause(play, ReviewPoint.EPISODE_SCRIPT)
        return
    state_machine.advance_play_to(play, PlayStatus.STORYBOARDING)
    dispatch_character_ref(play)
    for scene in episode.scenes.all():
        dispatch_storyboard(scene)


def _after_storyboard_ready(shot):
    dispatch_image_and_tts(shot)


def _after_material_ready(play, episode):
    from .checkpoints import apply_pause, pause_state, should_pause
    if not _episode_materials_ready(episode):
        return
    play.refresh_from_db(fields=["status"])
    state_machine.advance_episode_to(episode, EpisodeStatus.ASSET_GEN)
    if should_pause(play, ReviewPoint.ASSETS) and pause_state(play, ReviewPoint.ASSETS) != "after":
        # 停在素材审核点（先沿主线推进到 ASSET_GEN）；审核通过时 resume_dispatch 统一派合成
        apply_pause(play, ReviewPoint.ASSETS)
        return
    # 素材齐备后先确保字幕轨就绪（合成烧录字幕），再派合成
    maybe_dispatch_video_compose(episode)


def _episode_has_subtitle(episode):
    """该集是否已有 READY 的 zh-CN 字幕轨（视频合成烧录字幕需要）。"""
    from apps.distribution.models import SubtitleStatus, SubtitleTrack
    return SubtitleTrack.objects.filter(
        episode=episode, language="zh-CN", status=SubtitleStatus.READY,
    ).exists()


def dispatch_subtitle_gen(episode):
    """同一集只派一个字幕生成任务（并发素材齐备时不重复创建）。

    与 dispatch_video_compose_once 一致用行锁防并发重复（最后一张图与最后一段
    配音同时就绪时不会创建两个字幕任务）。defer_send 下 on_commit 前 celery_task_id
    为空属正常进行中状态，不据此排除。
    """
    from django.db import transaction
    from ..models import Episode
    with transaction.atomic():
        Episode.objects.select_for_update().get(pk=episode.pk)
        if TaskRecord.objects.filter(
            episode=episode, task_type=TaskType.SUBTITLE_GEN,
            status__in=(TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRYING),
        ).exists():
            return None
        return dispatch_task(TaskType.SUBTITLE_GEN, play=episode.play, episode=episode,
                             defer_send=True)


def maybe_dispatch_video_compose(episode):
    """素材齐备后的合成编排：无字幕轨则先派字幕生成，字幕完成后回调本函数继续。

    确保视频合成时 zh-CN 字幕轨已就绪（否则合成会不带字幕）。
    返回派发的 TaskRecord 或 None。
    """
    play = episode.play
    if not _episode_materials_ready(episode):
        return None
    if not _episode_has_subtitle(episode):
        return dispatch_subtitle_gen(episode)
    if TaskRecord.objects.filter(
        episode=episode, task_type=TaskType.VIDEO_COMPOSE,
        status__in=(TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRYING),
    ).exists():
        return None
    state_machine.advance_play_to(play, PlayStatus.COMPOSING)
    return dispatch_video_compose_once(episode)


def dispatch_video_compose_once(episode):
    """同一集同时只派一个合成任务：最后一张图和最后一段配音同时就绪时不会合成两次。"""
    from django.db import transaction
    from ..models import Episode
    with transaction.atomic():
        Episode.objects.select_for_update().get(pk=episode.pk)
        if TaskRecord.objects.filter(
            episode=episode, task_type=TaskType.VIDEO_COMPOSE,
            status__in=(TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRYING),
        ).exists():
            return None
        state_machine.advance_episode_to(episode, EpisodeStatus.COMPOSING)
        return dispatch_task(TaskType.VIDEO_COMPOSE, play=episode.play, episode=episode,
                             defer_send=True)


def _after_video_ready(episode):
    play = episode.play
    state_machine.advance_episode_to(episode, EpisodeStatus.COMPLETED)
    video_asset = Asset.objects.filter(
        play=play, episode=episode, asset_type=AssetType.VIDEO, status=AssetStatus.READY,
    ).order_by("-version").first()
    if video_asset and video_asset.artifact_url:
        episode.video_url = video_asset.artifact_url
        episode.save(update_fields=["video_url"])
    state_machine.summary_play_status(play)
    # 合成期间发生的 redo：成片用的素材已不是当前版本。此时素材侧事件早已发生在
    # RUNNING 合成期间被 maybe_dispatch_video_compose 挡掉，此后不会再有任何触发，
    # 旧成片会被当作最终成片发布。这里检查一次，必要时补派合成。
    if video_asset is not None and _composed_from_stale_materials(video_asset):
        logger.warning("Episode#%s 成片素材已完成更新，补派合成", episode.pk)
        maybe_dispatch_video_compose(episode)


def _composed_from_stale_materials(video_asset):
    """成片是否由已失效/被换新的素材合成。"""
    sources = (video_asset.metadata or {}).get("source_assets") or []
    if not sources:
        return False
    from ..models import Asset as _Asset, AssetStatus as _Status
    return _Asset.objects.filter(pk__in=sources).exclude(status=_Status.READY).exists()


def _all_episodes_script_ready(play):
    episodes = list(play.episodes.all())
    if not episodes:
        return False
    for ep in episodes:
        if not Asset.objects.filter(
            play=play, episode=ep, asset_type=AssetType.EPISODE_SCRIPT,
            status=AssetStatus.READY,
        ).exists():
            return False
    return True


def _episode_materials_ready(episode):
    shots = Shot.objects.filter(scene__episode=episode)
    if not shots.exists():
        return False
    # 还有场景没出分镜（尚无镜头）时不算就绪，否则场景 1 素材先齐就会只合成场景 1、
    # 整集被标记完成。分镜重做时旧镜头的素材会被失效（generate_storyboard），同样不会误判就绪。
    if episode.scenes.filter(shots__isnull=True).exists():
        return False
    for shot in shots:
        if not Asset.objects.filter(
            play=episode.play, shot=shot, asset_type=AssetType.IMAGE, status=AssetStatus.READY,
        ).exists():
            return False
        # 无对白镜头 generate_tts 会跳过、不产出 AUDIO，只要求有对白的镜头具备配音
        if shot.dialogue and not Asset.objects.filter(
            play=episode.play, shot=shot, asset_type=AssetType.AUDIO, status=AssetStatus.READY,
        ).exists():
            return False
    return True


# ─── 审核恢复 / 驳回派发 ──────────────────────────────────────────────────

def resume_dispatch(play, review_point):
    """审核通过后恢复派发下游任务。"""
    if review_point == ReviewPoint.OUTLINE:
        for ep in play.episodes.all():
            dispatch_episode_script(ep)
    elif review_point == ReviewPoint.EPISODE_SCRIPT:
        dispatch_character_ref(play)
        for ep in play.episodes.all():
            state_machine.advance_episode_to(ep, EpisodeStatus.STORYBOARDING)
            for scene in ep.scenes.all():
                dispatch_storyboard(scene)
    elif review_point == ReviewPoint.ASSETS:
        # 素材已齐的分集：先补字幕再合成；其余分集素材齐后由 _after_material_ready 自行派发
        for ep in play.episodes.all():
            maybe_dispatch_video_compose(ep)


def reject_dispatch(play, review_point, episode=None):
    """驳回后重新派发对应步骤任务。"""
    if review_point == ReviewPoint.OUTLINE:
        dispatch_outline(play)
    elif review_point == ReviewPoint.EPISODE_SCRIPT:
        ep = episode or play.episodes.first()
        if ep:
            dispatch_episode_script(ep)
    elif review_point == ReviewPoint.ASSETS:
        ep = episode or play.episodes.first()
        if ep:
            for shot in Shot.objects.filter(scene__episode=ep):
                dispatch_image_and_tts(shot)


# ─── 信号 ──────────────────────────────────────────────────────────────────

def _asset_pre_save(sender, instance, **kwargs):
    old = None
    if instance.pk:
        old = sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    instance._pipeline_pre_status = old


def _asset_post_save(sender, instance, **kwargs):
    old = getattr(instance, "_pipeline_pre_status", None)
    # 助手应用提案写入的新版本只落库，不触发下游重新生成（assistant/proposal）
    if getattr(instance, "_skip_pipeline_dispatch", False):
        return
    if instance.status == AssetStatus.READY and old != AssetStatus.READY:
        handle_asset_ready(instance)


def _play_pre_save(sender, instance, **kwargs):
    old = None
    if instance.pk:
        old = sender.objects.filter(pk=instance.pk).values_list("status", flat=True).first()
    instance._pipeline_pre_status = old


def _play_post_save(sender, instance, **kwargs):
    old = getattr(instance, "_pipeline_pre_status", None)
    if instance.status == PlayStatus.OUTLINING and old != PlayStatus.OUTLINING:
        dispatch_outline(instance)


def connect_signals():
    pre_save.connect(_asset_pre_save, sender=Asset, dispatch_uid="asset_pre")
    post_save.connect(_asset_post_save, sender=Asset, dispatch_uid="asset_post")
    pre_save.connect(_play_pre_save, sender=Play, dispatch_uid="play_pre")
    post_save.connect(_play_post_save, sender=Play, dispatch_uid="play_post")
