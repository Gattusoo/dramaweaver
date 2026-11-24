"""提案的创建、过期判定、应用与丢弃（assistant/proposal，D4 / D6 / D8）。

agent 只能经 create_proposal 生成提案；真正落库只发生在用户调用 apply_proposal，
这条路径与 LLM 无关。
"""
import json
import logging

from django.db import transaction
from django.utils import timezone

from ..models import (
    Asset, AssetDependency, AssetStatus, AssetType, Character, Episode, Play, PlayStatus,
    ReviewDecision, ReviewLog, ReviewPoint, Scene, Shot, TimeOfDay,
)
from ..permissions import can_write_play
from .models import AssistantProposal, ProposalKind, ProposalStatus

logger = logging.getLogger(__name__)

SCENE_FIELDS = ("description", "mood", "location", "time_of_day")
SHOT_FIELDS = ("dialogue", "visual_prompt", "action_note", "emotion_tag",
               "dialogue_character_id", "shot_type", "duration_estimate", "sound_effects")
CHARACTER_FIELDS = ("name", "role", "description", "appearance_prompt")
TIME_OF_DAY = tuple(TimeOfDay.values)
GENERATION_ACTIONS = ("redo_shot", "redo_scene", "preview_episode")

APPLICABLE_STATUSES = {
    PlayStatus.PAUSED_AT_OUTLINE, PlayStatus.PAUSED_AT_SCRIPT, PlayStatus.PAUSED_AT_ASSETS,
    PlayStatus.COMPLETED,
}
K = ProposalKind
_REVIEW_POINT = {
    K.OUTLINE: ReviewPoint.OUTLINE,
    K.EPISODE_SCRIPT: ReviewPoint.EPISODE_SCRIPT,
    K.SCENE: ReviewPoint.EPISODE_SCRIPT,
    K.SCENE_CREATE: ReviewPoint.EPISODE_SCRIPT,
    K.SHOT: ReviewPoint.ASSETS,
    K.SHOT_CREATE: ReviewPoint.ASSETS,
    K.SHOT_DELETE: ReviewPoint.ASSETS,
    K.CHARACTER_EDIT: ReviewPoint.CHARACTER_REF,
    K.CHARACTER_CREATE: ReviewPoint.CHARACTER_REF,
    # 写设定不改流水线资产，但要求作者确认（走剧本检查点）
    K.STORY_ASSET: ReviewPoint.EPISODE_SCRIPT,
}
# 不适用 redo 的提案：大纲（会覆盖全部剧本编辑）、删除镜头（无可重做对象）、生成请求（本身即生成）
_NO_REDO = {K.OUTLINE: "大纲提案不支持同时重做：重新生成全部剧本会覆盖已有编辑",
            K.SHOT_DELETE: "删除镜头的提案不支持重做",
            K.GENERATION: "生成请求本身即会派发任务，不需要 redo"}
MAX_IMPACTED = 500


def shot_ids(scene):
    return list(Shot.objects.filter(scene=scene).order_by("shot_number").values_list("pk", flat=True))


def scene_ids(episode):
    return list(Scene.objects.filter(episode=episode).order_by("scene_number").values_list("pk", flat=True))


def estimate_tasks(action, target):
    """生成请求的预计任务数：每个镜头图片 1 + 有对白时配音 1；预览 1。"""
    if action == "preview_episode":
        return 1
    shots = [target] if action == "redo_shot" else list(Shot.objects.filter(scene=target))
    return sum(1 + (1 if s.dialogue else 0) for s in shots)


class ProposalError(Exception):
    status_code = 409


class ProposalForbidden(ProposalError):
    status_code = 403


class ProposalInvalid(ProposalError):
    status_code = 400


# ── 目标当前状态 ────────────────────────────────────────────────────────────

def latest_text_asset(play, asset_type, episode=None):
    return Asset.objects.filter(
        play=play, asset_type=asset_type, episode=episode, status=AssetStatus.READY,
    ).order_by("-version").first()


def snapshot(obj, fields):
    return {f: getattr(obj, f) for f in fields}


def _scene_snapshots(episode, scene_edits):
    """剧本提案中各场景被改字段的当前值 {scene_number: {field: value}}。

    编辑过的场景已被删除时返回 None：表示提案目标结构已变化、必然过期
    （调用方据此返回"场景已被删除"而非抛 KeyError 变 500）。
    """
    by_number = {s.scene_number: s for s in Scene.objects.filter(episode=episode)}
    out = {}
    for number, edit in scene_edits.items():
        scene = by_number.get(int(number))
        if scene is None:
            return None
        out[str(number)] = snapshot(scene, edit.keys())
    return out


_EPISODE_OUTPUTS = [AssetType.VIDEO, AssetType.TRAILER, AssetType.PREVIEW]


def impacted_assets(play, kind, *, episode=None, scene=None, shot=None, character=None):
    """受影响的 READY 资产 id：层级关系（大纲 → 全部剧本与下游；剧本/场景/镜头 → 其下游；
    角色 → 定妆照与其对白镜头的图片；结构变化 → 该集成片）加 AssetDependency 显式依赖边。"""
    qs = Asset.objects.filter(play=play, status=AssetStatus.READY)
    outputs = qs.filter(episode=episode, asset_type__in=_EPISODE_OUTPUTS) if episode else qs.none()
    if kind == K.OUTLINE:
        qs = qs.exclude(asset_type__in=[AssetType.OUTLINE, AssetType.BGM])
    elif kind == K.EPISODE_SCRIPT:
        qs = qs.filter(episode=episode).exclude(asset_type__in=[AssetType.EPISODE_SCRIPT, AssetType.BGM])
    elif kind in (K.SCENE, K.SHOT_CREATE, K.SCENE_CREATE) or (kind == K.GENERATION and shot is None):
        qs = (qs.filter(scene=scene) if scene else qs.none()) | outputs
    elif kind in (K.SHOT, K.SHOT_DELETE, K.GENERATION):
        qs = qs.filter(shot=shot) | outputs
    elif kind in (K.CHARACTER_EDIT, K.CHARACTER_CREATE):
        qs = (qs.filter(character=character, asset_type=AssetType.CHARACTER_REF)
              | qs.filter(shot__dialogue_character=character, asset_type=AssetType.IMAGE)) if character else qs.none()
    else:
        qs = qs.none()
    ids = set(qs.values_list("pk", flat=True)[:MAX_IMPACTED])
    frontier = list(ids)
    while frontier and len(ids) < MAX_IMPACTED:
        nxt = set(AssetDependency.objects.filter(upstream_id__in=frontier).values_list(
            "downstream_id", flat=True)) - ids
        nxt = set(Asset.objects.filter(pk__in=nxt, status=AssetStatus.READY).values_list("pk", flat=True))
        ids |= nxt
        frontier = list(nxt)
    return sorted(ids)[:MAX_IMPACTED]


# ── 创建 ────────────────────────────────────────────────────────────────────

def create_proposal(session, message, kind, *, payload, summary, base_version=None, base_snapshot=None,
                    target_asset=None, episode=None, scene=None, shot=None, character=None,
                    parent=None, alternative_group=None, alternative_label="", stale_siblings=True):
    """创建 PENDING 提案；同一目标已有的 PENDING 提案标记为 STALE。

    Phase 2.4：alternative_group 相同表示同一组备选方案（多方案生成）；
    stale_siblings=False 时保留同组其它 PENDING 备选，使多方案可以并存待选。
    """
    play = session.play
    proposal = AssistantProposal(
        session=session, message=message, play=play, kind=kind, payload=payload, summary=summary,
        base_version=base_version, base_snapshot=base_snapshot or {}, target_asset=target_asset,
        target_episode=episode, target_scene=scene, target_shot=shot, target_character=character,
        impacted_asset_ids=impacted_assets(play, kind, episode=episode, scene=scene, shot=shot,
                                           character=character),
        parent=parent, alternative_group=alternative_group, alternative_label=alternative_label,
    )
    with transaction.atomic():
        pending = AssistantProposal.objects.filter(
            play=play, status=ProposalStatus.PENDING, **proposal.target_key(),
        )
        if not stale_siblings and alternative_group is not None:
            # 同组备选互相保留；组外的旧提案仍失效
            pending = pending.exclude(alternative_group=alternative_group)
        pending.update(status=ProposalStatus.STALE)
        proposal.save()
    # 渐进自主（主计划 3.2.6 / design D3）：高信任用户的高置信度非删除类提案
    # 创建即自动应用（复用 apply_proposal 全部校验；失败降级 PENDING）。
    # 开关关闭时 should_auto_approve 恒 False，行为与建设前一致。
    try:
        from .trust import should_auto_approve
        if should_auto_approve(session.user, proposal):
            try:
                return apply_proposal(proposal.pk, session.user, auto_applied=True)
            except ProposalError:
                # 状态门禁/过期拒绝：降级 PENDING 待用户确认，不丢提案
                pass
    except Exception:  # noqa: BLE001 — 自动批准失败不影响提案创建
        pass
    return proposal


# ── 过期判定 ────────────────────────────────────────────────────────────────

def stale_reason(proposal):
    """目标在提案生成后被修改则返回原因，否则 None。"""
    kind = proposal.kind
    if kind in (ProposalKind.OUTLINE, ProposalKind.EPISODE_SCRIPT):
        # 改编提案（adapt payload）：不是对既有文本的编辑，无目标文本可比对；
        # 已有剧集保护在 _apply_adapt（confirm_overwrite 两档）。
        if kind == ProposalKind.EPISODE_SCRIPT and "adapt" in (proposal.payload or {}):
            return None
        asset_type = AssetType.OUTLINE if kind == ProposalKind.OUTLINE else AssetType.EPISODE_SCRIPT
        episode = proposal.target_episode if kind == ProposalKind.EPISODE_SCRIPT else None
        current = latest_text_asset(proposal.play, asset_type, episode)
        if (current.version if current else None) != proposal.base_version:
            return "目标文本已被更新"
        if kind == ProposalKind.EPISODE_SCRIPT:
            edits = proposal.payload.get("scenes") or {}
            if edits:
                snapshots = _scene_snapshots(proposal.target_episode, edits)
                if snapshots is None:
                    return "编辑的目标场景已被删除"
                if snapshots != (proposal.base_snapshot.get("scenes") or {}):
                    return "场景字段已被更新"
        return None
    snap = proposal.base_snapshot
    if kind == K.SHOT_CREATE:
        if proposal.target_scene_id is None:
            return "目标场景已被删除"
        return "场景内镜头已变化" if shot_ids(proposal.target_scene) != snap.get("shot_ids") else None
    if kind == K.SCENE_CREATE:
        return "分集内场景已变化" if scene_ids(proposal.target_episode) != snap.get("scene_ids") else None
    if kind == K.CHARACTER_CREATE:
        exists = proposal.play.characters.filter(name=proposal.payload.get("name")).exists()
        return "已存在同名角色" if exists else None
    if kind == K.STORY_ASSET:
        pay = proposal.payload
        if pay.get("action") == "update":
            from apps.creation.models import StoryAsset
            if not StoryAsset.objects.filter(pk=pay.get("story_asset_id")).exists():
                return "目标设定已被删除"
        else:
            from apps.creation.models import StoryAsset
            exists = StoryAsset.objects.filter(
                play=proposal.play, type=pay.get("type"), name=pay.get("name")).exists()
            return "已存在同名设定" if exists else None
        return None
    if kind == K.GENERATION:
        model = Shot if proposal.payload.get("action") == "redo_shot" else (
            Scene if proposal.payload.get("action") == "redo_scene" else None)
        if model is not None and not model.objects.filter(pk=proposal.payload.get("target_id")).exists():
            return "目标已被删除"
        return None
    if kind == K.SCENE:
        obj = Scene.objects.filter(pk=proposal.target_scene_id).first()
    elif kind == K.CHARACTER_EDIT:
        obj = proposal.play.characters.filter(pk=proposal.target_character_id).first()
    else:  # SHOT / SHOT_DELETE
        obj = Shot.objects.filter(pk=proposal.target_shot_id).first()
    if obj is None:
        return "目标已被删除"
    if kind == K.SHOT_DELETE:
        if shot_ids(obj.scene) != snap.get("shot_ids") or snapshot(obj, snap.get("fields", {})) != snap.get("fields"):
            return "镜头或场景结构已变化"
        return None
    if snapshot(obj, snap.keys()) != snap:
        return "目标字段已被更新"
    return None


# ── 应用 / 丢弃 ─────────────────────────────────────────────────────────────

def _new_text_asset(proposal, asset_type, episode, metadata):
    """新版本文本资产：version 由 Asset._bump_version 递增，旧版本由 save 标记 SUPERSEDED；
    _skip_pipeline_dispatch 阻止 post_save 触发下游重新生成（D8）。"""
    asset = Asset(play=proposal.play, episode=episode, asset_type=asset_type, status=AssetStatus.READY,
                  metadata={**metadata, "source": "assistant", "proposal_id": proposal.pk})
    asset._skip_pipeline_dispatch = True
    asset.save()
    return asset


def _update_scene(scene_id, fields):
    Scene.objects.filter(pk=scene_id).update(**fields, updated_at=timezone.now())


def _apply_changes(proposal):
    """按类型落库，返回新建的资产（Scene / Shot 提案可能为 None）。"""
    p, payload = proposal, proposal.payload
    if p.kind == ProposalKind.OUTLINE:
        return _new_text_asset(p, AssetType.OUTLINE, None, {"text": payload["text"]})
    if p.kind == ProposalKind.EPISODE_SCRIPT:
        episode = p.target_episode
        if "adapt" in payload:
            return _apply_adapt(p, payload)
        current = latest_text_asset(p.play, AssetType.EPISODE_SCRIPT, episode)
        meta = dict((current.metadata or {}) if current else {})
        meta.pop("usage", None)
        if "text" in payload:
            meta["text"] = payload["text"]
        by_number = {s.scene_number: s.pk for s in Scene.objects.filter(episode=episode)}
        for number, fields in (payload.get("scenes") or {}).items():
            scene_pk = by_number.get(int(number))
            if scene_pk is None:
                raise ProposalError(f"场景 {number} 已被删除，提案已过期")
            _update_scene(scene_pk, fields)
        meta["scenes"] = sorted(by_number.values())
        return _new_text_asset(p, AssetType.EPISODE_SCRIPT, episode, meta)
    if p.kind == K.SCENE:
        _update_scene(p.target_scene_id, payload)
        return None
    if p.kind == K.SHOT:
        # 同步结构化字段；有分镜资产时一并写入新版本，保持两边一致
        shot = p.target_shot
        Shot.objects.filter(pk=shot.pk).update(**payload, updated_at=timezone.now())
        board = Asset.objects.filter(shot=shot, asset_type=AssetType.STORYBOARD,
                                     status=AssetStatus.READY).order_by("-version").first()
        if board is None:
            return None
        try:
            data = json.loads((board.metadata or {}).get("text") or "{}")
        except ValueError:
            data = {}
        return _storyboard_asset(p, shot, {**(data if isinstance(data, dict) else {}), **payload})
    if p.kind == K.SHOT_CREATE:
        return _insert_shot(p)
    if p.kind == K.SHOT_DELETE:
        _delete_shot(p)
        return None
    if p.kind == K.SCENE_CREATE:
        episode = p.target_episode
        number = (Scene.objects.filter(episode=episode).order_by("-scene_number")
                  .values_list("scene_number", flat=True).first() or 0) + 1
        p.target_scene = Scene.objects.create(episode=episode, scene_number=number, **payload)
        p.result = {"scene_id": p.target_scene.pk, "scene_number": number}
        return None
    if p.kind == K.CHARACTER_EDIT:
        Character.objects.filter(pk=p.target_character_id).update(**payload, updated_at=timezone.now())
        # Phase 2.4.3.5：角色设定变化留版本 + 标记受影响资产（失败不影响提案应用）
        try:
            from .script_versions import record_character_version
            character = Character.objects.filter(pk=p.target_character_id).first()
            if character is not None:
                record_character_version(character, changed_fields=list(payload), user=p.applied_by)
        except Exception:  # noqa: BLE001
            logger.exception("记录角色版本失败 character=%s", p.target_character_id)
        return None
    if p.kind == K.CHARACTER_CREATE:
        p.target_character = Character.objects.create(play=p.play, **payload)
        p.result = {"character_id": p.target_character.pk}
        try:
            from .script_versions import record_character_version
            record_character_version(p.target_character, changed_fields=list(payload), user=p.applied_by)
        except Exception:  # noqa: BLE001
            logger.exception("记录角色版本失败 character=%s", p.target_character.pk)
        return None
    if p.kind == K.STORY_ASSET:
        return _apply_story_asset(p, payload)
    return None  # GENERATION：不改数据，提交后派发任务（_dispatch_generation）


def _apply_adapt(p, payload):
    """应用小说改编提案（novel-adaptation 组 2）：payload.adapt 结构化剧集落库。

    已有剧集保护：目标剧集存在场景且 payload 未带 confirm_overwrite=True 时
    拒绝（作者须显式确认覆盖）。产出：每集按 episode_start 落 Episode + 场景 +
    镜头（含对白/动作指示），剧集号从 episode_start 连续排。
    """
    from ..models import Episode, EpisodeStatus, Scene, Shot
    adapt = payload.get("adapt") or {}
    play = p.play
    overwrite = bool(adapt.get("confirm_overwrite"))
    episode_start = int(adapt.get("episode_start") or 1)
    # 已有剧集保护：episode_start..N 范围内任一已有场景且未确认覆盖 → 拒绝
    if not overwrite:
        numbers = [episode_start + i for i in range(len(adapt.get("episodes") or []))]
        existing = Scene.objects.filter(
            episode__play=play, episode__episode_number__in=numbers).exists()
        if existing:
            raise ProposalError(
                f"改编范围（第 {episode_start} 集起）已有剧本内容，"
                "需在提案上确认覆盖后重发（confirm_overwrite）")
    created_eps = []
    for i, ep_data in enumerate(adapt.get("episodes") or []):
        number = episode_start + i
        episode, _ = Episode.objects.update_or_create(
            play=play, episode_number=number,
            defaults={"title": ep_data.get("title", "")})
        episode.scenes.all().delete()  # 覆盖路径：清旧场景重建
        for j, sc in enumerate(ep_data.get("scenes") or [], start=1):
            scene = Scene.objects.create(
                episode=episode, scene_number=sc.get("scene_number", j),
                location=sc.get("location", ""), time_of_day=sc.get("time_of_day", "DAY"),
                description=sc.get("description", ""), mood=sc.get("mood", ""))
            for k, sh in enumerate(sc.get("shots") or [], start=1):
                Shot.objects.create(
                    scene=scene, shot_number=sh.get("shot_number", k),
                    visual_prompt=sh.get("description", ""),
                    dialogue=sh.get("dialogue", ""),
                    action_note=sh.get("action", ""))
        created_eps.append(episode.pk)
    p.result = {"episode_ids": created_eps, "adapt": True}
    return None


def _apply_story_asset(p, payload):
    """应用写设定提案：action=new 建资产 / action=update 更新既有设定。"""
    from apps.creation.models import Character, StoryAsset
    if payload.get("action") == "update":
        asset = StoryAsset.objects.filter(pk=payload.get("story_asset_id")).first()
        if asset is None:
            raise ProposalError("目标设定已被删除，提案已过期")
        if "name" in payload:
            asset.name = payload["name"]
        if "fields" in payload:
            asset.fields = payload["fields"]
        if "character_id" in payload:
            asset.character = (Character.objects.filter(pk=payload["character_id"], play=p.play).first()
                              if payload["character_id"] else None)
        asset.save()
        p.result = {"story_asset_id": asset.pk, "action": "update"}
        return None
    asset = StoryAsset.objects.create(
        play=p.play, type=payload.get("type"), name=payload.get("name"),
        fields=payload.get("fields") or {})
    if payload.get("character_id"):
        asset.character = Character.objects.filter(pk=payload["character_id"], play=p.play).first()
        asset.save()
    chapter_ids = payload.get("chapter_ids") or []
    if chapter_ids:
        from apps.creation.models import NovelChapter
        chapters = NovelChapter.objects.filter(volume__novel__play=p.play, pk__in=chapter_ids)
        asset.chapter_refs.set(chapters)
    p.result = {"story_asset_id": asset.pk, "action": "new"}
    return None


def _storyboard_asset(p, shot, data):
    asset = Asset(play=p.play, episode=shot.scene.episode, scene=shot.scene, shot=shot,
                  asset_type=AssetType.STORYBOARD, status=AssetStatus.READY,
                  metadata={"text": json.dumps(data, ensure_ascii=False), "source": "assistant",
                            "proposal_id": p.pk})
    asset._skip_pipeline_dispatch = True
    asset.save()
    return asset


def _insert_shot(p):
    """插到 after_shot_number 之后：倒序顺延后续编号（避免撞 (scene, shot_number) 唯一约束）。"""
    scene, payload = p.target_scene, dict(p.payload)
    after = payload.pop("after_shot_number")
    for shot in Shot.objects.filter(scene=scene, shot_number__gt=after).order_by("-shot_number"):
        Shot.objects.filter(pk=shot.pk).update(shot_number=shot.shot_number + 1)
    shot = Shot.objects.create(scene=scene, shot_number=after + 1, **payload)
    p.target_shot = shot
    p.result = {"shot_id": shot.pk, "shot_number": shot.shot_number}
    board = {"shot_number": shot.shot_number, **{k: v for k, v in payload.items() if k != "dialogue_character_id"}}
    return _storyboard_asset(p, shot, board)


def _delete_shot(p):
    """先把素材标记失效（保留历史、级联依赖边）再删除镜头，后续编号正序前移。"""
    shot = p.target_shot
    scene, number = shot.scene, shot.shot_number
    for asset in Asset.objects.filter(shot=shot, status=AssetStatus.READY):
        asset.mark_superseded_recursive()
    shot.delete()
    p.target_shot = None  # 数据库已 SET_NULL；同步内存对象，避免随后 save 写回已删除的 id
    for later in Shot.objects.filter(scene=scene, shot_number__gt=number).order_by("shot_number"):
        Shot.objects.filter(pk=later.pk).update(shot_number=later.shot_number - 1)
    p.result = {"deleted_shot_number": number}


def _review_episode(proposal):
    """ReviewLog 关联的分集：分集级以下的提案在创建时都记录了 target_episode。"""
    return proposal.target_episode


def _redo(proposal):
    """对受影响的最小粒度重做（会产生费用）。剧本提案按场景重做，不用 redo_episode
    （它会重新生成剧本、覆盖刚应用的内容）。"""
    from ..models import TaskType
    from ..pipeline.dispatcher import dispatch_storyboard, dispatch_task
    from ..pipeline.redo import redo_scene, redo_shot
    k = proposal.kind
    if k in (K.SHOT, K.SHOT_CREATE):
        redo_shot(proposal.target_shot)
    elif k == K.SCENE:
        redo_scene(proposal.target_scene)
    elif k == K.SCENE_CREATE:
        dispatch_storyboard(proposal.target_scene)
    elif k == K.EPISODE_SCRIPT:
        for scene in Scene.objects.filter(episode=proposal.target_episode):
            redo_scene(scene)
    elif k in (K.CHARACTER_EDIT, K.CHARACTER_CREATE):
        character = proposal.target_character
        for asset in Asset.objects.filter(character=character, asset_type=AssetType.CHARACTER_REF,
                                          status=AssetStatus.READY):
            asset.mark_superseded_recursive()
        dispatch_task(TaskType.CHARACTER_REF, play=proposal.play, input_summary={"character": character.pk})


def preview_busy(episode):
    from ..models import TaskRecord, TaskStatus, TaskType
    return TaskRecord.objects.filter(
        task_type=TaskType.PREVIEW_RENDER, episode=episode,
        status__in=[TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRYING],
    ).exists()


def _dispatch_generation(proposal):
    """生成请求：派发任务并把任务 id 写入提案 result。配音只为有对白的镜头派发，与预计任务数一致。"""
    from ..models import TaskType
    from ..pipeline.dispatcher import dispatch_task
    action, target_id = proposal.payload["action"], proposal.payload["target_id"]
    play = proposal.play
    records = []
    # 在 apply 事务内创建 TaskRecord（id 立即可用），Celery 消息等事务提交后再发
    if action == "preview_episode":
        episode = Episode.objects.get(pk=target_id)
        records.append(dispatch_task(TaskType.PREVIEW_RENDER, play=play, episode=episode, defer_send=True))
    else:
        shots = ([Shot.objects.get(pk=target_id)] if action == "redo_shot"
                 else list(Shot.objects.filter(scene_id=target_id).order_by("shot_number")))
        for shot in shots:
            for asset in Asset.objects.filter(shot=shot, asset_type__in=[AssetType.IMAGE, AssetType.AUDIO],
                                              status=AssetStatus.READY):
                asset.mark_superseded_recursive()
            kw = dict(play=play, episode=shot.scene.episode, scene=shot.scene, shot=shot, defer_send=True)
            records.append(dispatch_task(TaskType.IMAGE_GEN, **kw))
            if shot.dialogue:
                records.append(dispatch_task(TaskType.TTS_GEN, **kw))
    proposal.result = {"task_record_ids": [r.pk for r in records]}


def apply_proposal(proposal_id, user, redo=False, keep_alternatives=False, auto_applied=False):
    """D6：权限 → 状态门禁 → 过期判定 → 新版本 + 结构化字段 → 旧提案 STALE → ReviewLog → 可选 redo。

    keep_alternatives=True 时不把同组备选（alternative_group 相同）一并置 STALE
    （Phase 2.4：apply_selected 的 discard_others=False 路径依赖此语义）。
    返回 proposal。错误抛 ProposalForbidden(403) / ProposalInvalid(400) / ProposalError(409)。
    """
    stale = None
    with transaction.atomic():
        proposal = AssistantProposal.objects.select_for_update().select_related(
            "target_episode", "target_scene__episode", "target_shot__scene__episode",
        ).get(pk=proposal_id)
        play = Play.objects.select_for_update().get(pk=proposal.play_id)
        proposal.play = play
        if not can_write_play(user, play):
            raise ProposalForbidden("无权修改该短剧")
        if redo and proposal.kind in _NO_REDO:
            raise ProposalInvalid(_NO_REDO[proposal.kind])
        if proposal.status != ProposalStatus.PENDING:
            raise ProposalError(f"提案状态为 {proposal.status}，不能应用")
        is_preview = proposal.kind == K.GENERATION and proposal.payload.get("action") == "preview_episode"
        # 预览不改变流水线数据，不受状态门禁；但同集只能有一个进行中的预览
        if not is_preview and play.status not in APPLICABLE_STATUSES:
            raise ProposalError(f"短剧当前状态为 {play.status}，需在审核暂停或完成状态下应用")
        if is_preview and preview_busy(proposal.target_episode):
            raise ProposalError("该集已有进行中的预览任务")
        stale = stale_reason(proposal)
        if stale:
            # 先提交 STALE 标记再抛错（在事务内抛错会回滚该标记）
            proposal.status = ProposalStatus.STALE
            proposal.save(update_fields=["status"])
            # 信任分累积（design D1）：提案过期扣分（幂等：终态后不再计）
            try:
                from .trust import record_outcome
                record_outcome(proposal.session.user, "stale", proposal=proposal)
            except Exception:  # noqa: BLE001
                pass
        else:
            asset = _apply_changes(proposal)
            if proposal.kind == K.GENERATION:
                _dispatch_generation(proposal)
            pending = AssistantProposal.objects.filter(
                play=play, status=ProposalStatus.PENDING, **proposal.target_key(),
            ).exclude(pk=proposal.pk)
            if keep_alternatives and proposal.alternative_group is not None:
                # 保留同组其余备选（调用方自行决定去留）
                pending = pending.exclude(alternative_group=proposal.alternative_group)
            pending.update(status=ProposalStatus.STALE)
            proposal.status = ProposalStatus.APPLIED
            proposal.applied_asset = asset
            proposal.applied_by = user
            proposal.applied_at = timezone.now()
            # 渐进自主（design D3）：自动应用标记 payload.auto_applied=true（仅渐进
            # 自动的路径传入；用户显式 apply 不标）。序列化器原样回显 payload，
            # 前端据此展示"已自动应用"角标。
            if auto_applied:
                try:
                    proposal.payload = {**proposal.payload, "auto_applied": True}
                except (TypeError, ValueError):
                    pass
            proposal.save(update_fields=["status", "applied_asset", "applied_by", "applied_at", "result",
                                         "payload", "target_scene", "target_shot", "target_character"])
            # 信任分累积（主计划 3.2.5 / design D1）：PENDING→APPLIED 首次迁移给
            # 提案创建者加分（幂等：终态后重复 apply 不再计）
            try:
                from .trust import record_outcome
                record_outcome(proposal.session.user, "apply", proposal=proposal)
            except Exception:  # noqa: BLE001
                pass
            if proposal.kind != K.GENERATION:
                ReviewLog.objects.create(
                    play=play, review_point=_REVIEW_POINT[proposal.kind], episode=_review_episode(proposal),
                    reviewer=user, decision=ReviewDecision.MODIFIED, comment=proposal.summary,
                    modifications={"proposal_id": proposal.pk, "kind": proposal.kind,
                                   "payload": proposal.payload},
                )
    if stale:
        raise ProposalError(f"提案已过期：{stale}，请重新生成")
    if redo:
        # 重做在提交之后执行（worker 才能读到新内容）；被外层事务包裹时退回 on_commit
        if transaction.get_connection().in_atomic_block:
            transaction.on_commit(lambda: _redo(proposal))
        else:
            _redo(proposal)
    return proposal


# ── 提案树 / 多方案（Phase 2.4.1 / 2.4.2）─────────────────────────────────

def proposal_tree(proposal_id, *, max_depth=20):
    """提案的完整树结构：父链（向上）与子提案（向下）。"""
    proposal = AssistantProposal.objects.select_related("target_episode").get(pk=proposal_id)
    ancestors, node, seen = [], proposal.parent, {proposal.pk}
    while node is not None and len(ancestors) < max_depth and node.pk not in seen:
        seen.add(node.pk)
        ancestors.append(node)
        node = node.parent
    return {"proposal": proposal, "ancestors": list(reversed(ancestors)),
            "children": list(proposal.children.order_by("created_at")),
            "alternatives": list(alternatives_of(proposal))}


def alternatives_of(proposal):
    """同组备选（排除自身），按创建顺序。"""
    if proposal.alternative_group is None:
        return AssistantProposal.objects.none()
    return AssistantProposal.objects.filter(
        play=proposal.play, alternative_group=proposal.alternative_group).exclude(pk=proposal.pk).order_by("id")


def compare_proposals(proposal_ids):
    """多提案并排对比：目标、摘要、payload 差异、受影响资产、成本估算。"""
    from .preview import proposal_preview
    by_id = {p.pk: p for p in AssistantProposal.objects.filter(pk__in=proposal_ids).select_related(
        "play", "target_episode", "target_scene", "target_shot", "target_character")}
    # 保持调用方给定的顺序（前端对比列顺序 = 用户选择顺序）
    proposals = [by_id[i] for i in proposal_ids if i in by_id]
    if len(proposals) != len(set(proposal_ids)):
        missing = set(proposal_ids) - {p.pk for p in proposals}
        raise ProposalInvalid(f"提案不存在：{sorted(missing)}")
    plays = {p.play_id for p in proposals}
    if len(plays) > 1:
        raise ProposalInvalid("只能对比同一短剧的提案")
    rows = []
    for p in proposals:
        row = {"id": p.pk, "kind": p.kind, "status": p.status, "summary": p.summary,
               "label": p.alternative_label, "payload": p.payload,
               "base_snapshot": p.base_snapshot,
               "impacted_asset_count": len(p.impacted_asset_ids or []),
               "alternative_group": str(p.alternative_group) if p.alternative_group else None}
        try:
            # proposal_preview 按主键查询（select_related 已带目标对象），必须传 id 而非对象
            preview = proposal_preview(p.pk)
            row["cost_estimate"] = preview.get("cost_estimate")
            row["diff"] = preview.get("diff")
        except Exception:  # noqa: BLE001 — 预览失败不影响对比主体
            logger.exception("对比时预览失败 proposal=%s", p.pk)
            row["cost_estimate"], row["diff"] = None, None
        rows.append(row)
    common = set(proposals[0].payload or {}) if proposals else set()
    for p in proposals[1:]:
        common &= set(p.payload or {})
    differences = {}
    for key in sorted(common):
        values = [json.dumps((p.payload or {}).get(key), ensure_ascii=False, sort_keys=True) for p in proposals]
        if len(set(values)) > 1:
            differences[key] = [json.loads(v) for v in values]
    return {"play_id": proposals[0].play_id if proposals else None, "proposals": rows,
            "differences": differences}


def apply_selected(proposal_id, user, *, discard_others=True, redo=False):
    """从对比组中选择一个应用；同组其余 PENDING 备选按 discard_others 处置（Phase 2.4.1.4）。

    discard_others=False 时同组备选保持 PENDING（继续待选），组外同目标的旧提案仍失效。
    """
    proposal = AssistantProposal.objects.get(pk=proposal_id)
    result = apply_proposal(proposal_id, user, redo=redo,
                            keep_alternatives=not discard_others)
    if discard_others and proposal.alternative_group is not None:
        AssistantProposal.objects.filter(
            play_id=proposal.play_id, alternative_group=proposal.alternative_group,
            status=ProposalStatus.PENDING).exclude(pk=proposal.pk).update(status=ProposalStatus.STALE)
    return result


def discard_proposal(proposal_id, user):
    with transaction.atomic():
        proposal = AssistantProposal.objects.select_for_update().select_related("play").get(pk=proposal_id)
        if not can_write_play(user, proposal.play):
            raise ProposalForbidden("无权修改该短剧")
        if proposal.status != ProposalStatus.PENDING:
            raise ProposalError(f"提案状态为 {proposal.status}，不能丢弃")
        proposal.status = ProposalStatus.DISCARDED
        proposal.save(update_fields=["status"])
        # 信任分累积（design D1）：丢弃扣分（幂等：终态后不再计）
        try:
            from .trust import record_outcome
            record_outcome(proposal.session.user, "discard", proposal=proposal)
        except Exception:  # noqa: BLE001
            pass
    return proposal
