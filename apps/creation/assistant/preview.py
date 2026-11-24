"""提案预览（Phase 1.8.1）：before/after 对比 + 结构化 diff + 成本估算。

- snapshot_current(proposal)：目标对象当前值快照
- compute_diff(before, payload)：字段级 old→new diff
- build_after(before, payload)：模拟应用后状态（不落库）
- cost_estimate(proposal)：impacted_asset_ids × 各类型单价（查 AIModel.cost_config）
- proposal_preview(proposal_id)：汇总 {summary, before, after, diff, cost_estimate, stale_reason}
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from .models import AssistantProposal, ProposalKind
from .proposals import (
    CHARACTER_FIELDS,
    SCENE_FIELDS,
    SHOT_FIELDS,
    latest_text_asset,
    scene_ids,
    shot_ids,
    snapshot,
)

logger = logging.getLogger(__name__)

K = ProposalKind

# 可预览字段 = 提案可修改字段（与 apply 路径一致）+ 预览额外展示的只读字段
_SHOT_FIELDS = SHOT_FIELDS
_SCENE_FIELDS = SCENE_FIELDS
_CHARACTER_FIELDS = CHARACTER_FIELDS + ("voice_profile_id",)


# ─── before 快照 ────────────────────────────────────────────────────────────

def snapshot_current(proposal: AssistantProposal) -> dict:
    """目标当前状态。GENERATION/CREATE 类目标返回结构信息而非字段值。"""
    p = proposal
    k = p.kind

    if k == K.OUTLINE:
        asset = latest_text_asset(p.play, _at("OUTLINE"))
        return {"version": asset.version if asset else None,
                "text": (asset.metadata or {}).get("text", "") if asset else ""}

    if k == K.EPISODE_SCRIPT:
        asset = latest_text_asset(p.play, _at("EPISODE_SCRIPT"), p.target_episode)
        scenes = {
            str(s.scene_number): snapshot(s, (p.payload.get("scenes") or {}).get(
                str(s.scene_number), {}).keys() or _SCENE_FIELDS)
            for s in (p.target_episode.scenes.all() if p.target_episode else [])
        }
        return {"version": asset.version if asset else None,
                "text": (asset.metadata or {}).get("text", "") if asset else "",
                "scenes": scenes}

    if k == K.SCENE and p.target_scene:
        return snapshot(p.target_scene, _SCENE_FIELDS)

    if k in (K.SHOT, K.SHOT_DELETE) and p.target_shot:
        return snapshot(p.target_shot, _SHOT_FIELDS)

    if k == K.SHOT_CREATE:
        return {"scene_id": p.target_scene_id,
                "existing_shot_numbers": [s.shot_number for s in
                                        (p.target_scene.shots.order_by("shot_number") if p.target_scene else [])]}

    if k == K.SCENE_CREATE:
        return {"episode_id": p.target_episode_id,
                "existing_scene_numbers": [s.scene_number for s in
                                          (p.target_episode.scenes.order_by("scene_number") if p.target_episode else [])]}

    if k == K.CHARACTER_EDIT and p.target_character:
        return snapshot(p.target_character, _CHARACTER_FIELDS)

    if k == K.CHARACTER_CREATE:
        return {"existing_names": list(p.play.characters.values_list("name", flat=True))}

    if k == K.GENERATION:
        return {"action": p.payload.get("action"), "target_id": p.payload.get("target_id")}

    return {}


def _at(name):
    from ..models import AssetType
    return getattr(AssetType, name)


# ─── after / diff ───────────────────────────────────────────────────────────

def build_after(before: dict, proposal: AssistantProposal) -> dict:
    """模拟应用后的状态（不触及数据库）。删除类返回删除标记。"""
    p, k, payload = proposal, proposal.kind, proposal.payload or {}

    if k in (K.OUTLINE, K.EPISODE_SCRIPT):
        after = dict(before)
        if "text" in payload:
            after["text"] = payload["text"]
        if k == K.EPISODE_SCRIPT and payload.get("scenes"):
            scenes = dict(after.get("scenes") or {})
            for num, fields in payload["scenes"].items():
                scenes[num] = {**(scenes.get(num) or {}), **fields}
            after["scenes"] = scenes
        return after

    if k in (K.SCENE, K.SHOT, K.CHARACTER_EDIT):
        return {**before, **payload}

    if k == K.SHOT_DELETE:
        return {"_deleted": True, "shot_id": p.target_shot_id}

    if k == K.SHOT_CREATE:
        return {"_new": True, "shot": payload,
                "insert_after": payload.get("after_shot_number")}

    if k == K.SCENE_CREATE:
        return {"_new": True, "scene": payload}

    if k == K.CHARACTER_CREATE:
        return {"_new": True, "character": payload}

    if k == K.GENERATION:
        return {"action": payload.get("action"), "target_id": payload.get("target_id"),
                "note": "将派发重新生成任务"}

    return dict(before)


def compute_diff(before: dict, after: dict) -> list:
    """字段级 diff：[{field, old, new, change}]，change ∈ added|removed|modified|unchanged。
    嵌套dict（如scenes）递归展平为 field.subfield。"""
    diffs = []
    keys = list(dict.fromkeys(list(before.keys()) + list(after.keys())))
    for key in keys:
        old, new = before.get(key), after.get(key)
        if isinstance(old, dict) and isinstance(new, dict):
            for sub in compute_diff(old, new):
                sub["field"] = f"{key}.{sub['field']}"
                diffs.append(sub)
            continue
        if key not in before:
            change = "added"
        elif key not in after:
            change = "removed"
        elif old != new:
            change = "modified"
        else:
            continue  # unchanged 不列入diff
        diffs.append({"field": key, "old": old, "new": new, "change": change})
    return diffs


# ─── 成本估算（1.8.2 基础：按资产类型×单价）──────────────────────────────

# 资产类型 → 供应商类型（无对应供应商的按 IMAGE 兜底）
_ASSET_TO_PROVIDER = {
    "AUDIO": "TTS",
    "VIDEO": "VIDEO",
    "PREVIEW": "VIDEO",
    "TRAILER": "VIDEO",
    "OUTLINE": "LLM",
    "EPISODE_SCRIPT": "LLM",
}
_ASSET_FALLBACK = "IMAGE"  # IMAGE/STORYBOARD/CHARACTER_REF/BGM 等


def _unit_cost_for(asset_type: str, play=None) -> Decimal:
    """资产类型单价：复用 cost_estimator 的优先级链（模型cost_config > 历史均价 > 兜底）。"""
    from .cost_estimator import _unit_cost
    return _unit_cost(play, _ASSET_TO_PROVIDER.get(asset_type, _ASSET_FALLBACK))


# 按秒计价的资产类型（对应 cost_per_second 单价）：TTS/VIDEO；其余按个数（cost_per_image 等）
_PER_SECOND_TYPES = {"AUDIO", "VIDEO", "PREVIEW", "TRAILER"}


def _asset_seconds(asset) -> Decimal:
    """资产时长（秒）：优先入库实测 metadata.duration_seconds；AUDIO 兜底对白字数/语速。

    无任何时长信息兜底 1 秒/资产（与旧按个数计价一致的保守值）。"""
    from ..models import AssetType
    meta = asset.metadata or {}
    d = meta.get("duration_seconds") or (meta.get("media") or {}).get("duration") or 0
    if d:
        return Decimal(str(d))
    if asset.asset_type == AssetType.AUDIO and asset.shot_id and asset.shot and asset.shot.dialogue:
        from .cost_estimator import _strip_punct
        return Decimal(str(max(0.5, len(_strip_punct(asset.shot.dialogue)) / 4.0)))
    return Decimal("1")


def _estimate_pending_generation(proposal) -> dict:
    """按提案类型与目标结构推算待生成资产（phase1 修复 3.1）。

    impacted_asset_ids 只统计 READY 资产——全新短剧没有资产时预览估 0 元，
    最贵的操作被显示为免费、绕过 requires_confirm 确认门。这里对
    GENERATION 类按目标场景/分集的镜头数与对白情况推算待生成集合；
    纯文本字段修改类返回零费用并标记 no_generation_cost。"""
    from decimal import Decimal as D
    from ..models import Shot
    from .cost_estimator import ALERT_THRESHOLD, _strip_punct, _unit_cost
    from apps.ai_gateway.models import ProviderType
    from ..models import AssetType
    from .models import ProposalKind as K

    def _result(counts, total, breakdown, no_generation=False):
        return {"assets": counts, "estimated_cost": str(D("0") if no_generation else
                                                        total.quantize(D("0.0001"))),
                "breakdown": breakdown, "requires_confirm": (not no_generation)
                and total >= ALERT_THRESHOLD,
                **({"basis": "estimated"} if not no_generation else {}),
                **({"no_generation_cost": True} if no_generation else {})}

    # 纯文本字段修改类：不触发生成，零费用但明示
    if proposal.kind not in (K.GENERATION,):
        return _result({}, D("0"), [], no_generation=True)

    payload = proposal.payload or {}
    action = payload.get("action") or ""

    # 按目标层级收集待生成镜头
    if proposal.target_shot_id:
        shots = list(Shot.objects.filter(pk=proposal.target_shot_id))
    elif proposal.target_scene_id:
        shots = list(Shot.objects.filter(scene_id=proposal.target_scene_id))
    elif proposal.target_episode_id:
        shots = list(Shot.objects.filter(scene__episode_id=proposal.target_episode_id))
    else:
        shots = []

    if action == "preview_episode" or (not shots and action == ""):
        # 整集预览：按整集规模计价
        from .cost_estimator import estimate_task_cost
        from ..models import TaskType
        est = estimate_task_cost(proposal.play, TaskType.PREVIEW_RENDER)
        return {"assets": {"PREVIEW": 1}, "estimated_cost": est["estimated_cost"],
                "breakdown": est["breakdown"], "requires_confirm": est["requires_confirm"],
                "basis": "estimated"}

    if not shots:
        # 找不到镜头结构（大纲阶段）：按默认规模估一次图片生成
        image_unit = _unit_cost(proposal.play, ProviderType.IMAGE)
        total = image_unit * D(str(max(1, len(shots) or 1)))
        return _result({"IMAGE": max(1, len(shots) or 1)}, total,
                       [{"asset_type": "IMAGE", "count": max(1, len(shots) or 1),
                         "unit_cost": str(image_unit), "subtotal": str(total)}])

    # redo_shot / redo_scene：图片按镜头数 + TTS 按对白语音秒数
    image_unit = _unit_cost(proposal.play, ProviderType.IMAGE)
    tts_unit = _unit_cost(proposal.play, ProviderType.TTS)
    dialogue_shots = [s for s in shots if (s.dialogue or "").strip()]
    speech_seconds = sum(max(0.5, len(_strip_punct(s.dialogue)) / 4.0)
                         for s in dialogue_shots)
    breakdown = [{"asset_type": "IMAGE", "count": len(shots),
                  "unit_cost": str(image_unit),
                  "subtotal": str(image_unit * len(shots))}]
    total = image_unit * len(shots)
    counts = {"IMAGE": len(shots)}
    if speech_seconds:
        tts_sub = tts_unit * D(str(round(speech_seconds, 2)))
        breakdown.append({"asset_type": "AUDIO", "count": len(dialogue_shots),
                          "seconds": round(speech_seconds, 2),
                          "unit_cost": str(tts_unit), "subtotal": str(tts_sub)})
        total += tts_sub
        counts["AUDIO"] = len(dialogue_shots)
    return _result(counts, total, breakdown)


def cost_estimate(proposal: AssistantProposal) -> dict:
    """按 impacted_asset_ids 分类统计重生成费用。

    按秒计价的类型（AUDIO/VIDEO/PREVIEW/TRAILER）用资产实测时长计价，
    不按个数——否则会系统性低估并压掉 requires_confirm 安全阈值。
    impacted_asset_ids 为空（全新短剧/无 READY 资产）时按提案类型与目标
    结构推算待生成资产，返回非零费用与 basis: "estimated"（phase1 修复 3.1）。
    Returns:
        {"assets": {"IMAGE": 3, "AUDIO": 2}, "estimated_cost": "0.19",
         "breakdown": [...], "requires_confirm": bool}
    """
    from ..models import Asset
    ids = proposal.impacted_asset_ids or []
    if not ids:
        return _estimate_pending_generation(proposal)
    assets = list(Asset.objects.filter(pk__in=ids).select_related("shot"))
    counts = {}
    for a in assets:
        counts[a.asset_type] = counts.get(a.asset_type, 0) + 1

    total = Decimal("0")
    breakdown = []
    for at, n in sorted(counts.items()):
        unit = _unit_cost_for(at, proposal.play)
        if at in _PER_SECOND_TYPES:
            seconds = sum((_asset_seconds(a) for a in assets if a.asset_type == at),
                          Decimal("0"))
            subtotal = unit * seconds
            breakdown.append({"asset_type": at, "count": n,
                              "seconds": str(seconds),
                              "unit_cost": str(unit), "subtotal": str(subtotal)})
        else:
            subtotal = unit * n
            breakdown.append({"asset_type": at, "count": n,
                              "unit_cost": str(unit), "subtotal": str(subtotal)})
        total += subtotal
    # 单次预估超阈值需要用户额外确认
    from .cost_estimator import ALERT_THRESHOLD
    requires_confirm = total >= ALERT_THRESHOLD
    return {"assets": counts, "estimated_cost": str(total.quantize(Decimal("0.0001"))),
            "breakdown": breakdown, "requires_confirm": requires_confirm}


# ─── 汇总 ────────────────────────────────────────────────────────────────

def proposal_preview(proposal_id: int, user=None) -> Optional[dict]:
    """完整预览：{id, kind, summary, before, after, diff, cost_estimate, stale_reason, status}。"""
    from .proposals import stale_reason
    p = AssistantProposal.objects.filter(pk=proposal_id).select_related(
        "target_episode", "target_scene", "target_shot", "target_character", "play").first()
    if p is None:
        return None
    before = snapshot_current(p)
    after = build_after(before, p)
    return {
        "id": p.pk, "kind": p.kind, "summary": p.summary, "status": p.status,
        "before": before, "after": after,
        "diff": compute_diff(before, after),
        "cost_estimate": cost_estimate(p),
        "stale_reason": stale_reason(p),
        "impacted_count": len(p.impacted_asset_ids or []),
    }
