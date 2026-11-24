"""成本预测（Phase 1.8.2）：基于历史 CostRecord + AIModel.cost_config 估算任务费用。

- estimate_task_cost(play, task_type, **ctx)：按任务类型估算单次费用
- estimate_generation_cost(play, action, target)：redo_shot/redo_scene/preview_episode
- cost_alert(estimate)：超阈值标记 requires_confirm
- 数据源：优先 AIModel.cost_config 单价；无配置时回退该 play 历史 CostRecord 均价
"""

from __future__ import annotations

import logging
from decimal import Decimal
from typing import Optional

from apps.ai_gateway.models import CostRecord, ProviderType
from ..models import TaskType

logger = logging.getLogger(__name__)

# 成本告警阈值（人民币）：单次预估超过即要求用户确认
ALERT_THRESHOLD = Decimal("1.00")

# 兜底单价（无配置且无历史时）
FALLBACK_UNIT = {
    ProviderType.IMAGE: Decimal("0.05"),
    ProviderType.TTS: Decimal("0.02"),
    ProviderType.VIDEO: Decimal("0.50"),
    ProviderType.LLM: Decimal("0.01"),
}


def _model_for(play, provider_type: str):
    """取该 play agent_config 对应类型的主模型。"""
    cfg = getattr(play, "agent_config", None)
    if cfg is None:
        return None
    field = {ProviderType.IMAGE: "image_model", ProviderType.TTS: "tts_model",
             ProviderType.VIDEO: "video_model", ProviderType.LLM: "assistant_model"}.get(provider_type)
    model = getattr(cfg, field, None)
    if model is None and provider_type == ProviderType.LLM:
        model = cfg.llm_model
    return model


def _unit_cost(play, provider_type: str) -> Decimal:
    """单价优先级：模型 cost_config > 该 play 历史均价 > 兜底。"""
    model = _model_for(play, provider_type)
    if model and model.cost_config:
        cfg = model.cost_config
        key = ("cost_per_image" if provider_type == ProviderType.IMAGE
               else "cost_per_second" if provider_type in (ProviderType.TTS, ProviderType.VIDEO)
               else "input_per_1k")
        if cfg.get(key):
            return Decimal(str(cfg[key]))

    # 历史均价兜底
    avg = (CostRecord.objects.filter(play=play, provider_type=provider_type)
           .order_by("-created_at").values_list("total_cost", flat=True)[:20])
    vals = [Decimal(str(v)) for v in avg if v]
    if vals:
        return sum(vals) / len(vals)
    return FALLBACK_UNIT.get(provider_type, Decimal("0.05"))


# ─── 任务类型估算 ────────────────────────────────────────────────────────

# task_type → (breakdown里的 provider_type 标签, 计价用的 ProviderType)
_TASK_PROVIDER = {
    TaskType.IMAGE_GEN: ("IMAGE", ProviderType.IMAGE),
    TaskType.TTS_GEN: ("TTS", ProviderType.TTS),
    TaskType.VIDEO_COMPOSE: ("VIDEO", ProviderType.VIDEO),
    TaskType.PREVIEW_RENDER: ("PREVIEW", ProviderType.VIDEO),
    TaskType.ASSISTANT_TURN: ("LLM", ProviderType.LLM),
}


# 各任务类型的默认规模（ctx 缺省时使用；phase1 修复 3.2）
_DEFAULT_SCALE = {
    "shot_count": 8,        # 单场景典型镜头数（IMAGE/STORYBOARD 类）
    "duration_s": 60,       # 单视频任务典型时长（PREVIEW_RENDER/VIDEO_COMPOSE）
    "input_tokens": 2000,   # 单轮助手对话典型输入
    "output_tokens": 1000,  # 单轮助手对话典型输出
}

# LLM 按 token 计价的任务类型
_TOKEN_TASKS = {TaskType.ASSISTANT_TURN}
# 按秒计价的任务类型
_PER_SECOND_TASKS = {TaskType.PREVIEW_RENDER, TaskType.VIDEO_COMPOSE, TaskType.TTS_GEN,
                     TaskType.PUBLISH, TaskType.RENDITION}
# 按镜头数计价的任务类型
_PER_SHOT_TASKS = {TaskType.IMAGE_GEN, TaskType.STORYBOARD, TaskType.DERIVATIVES}


def _int_ctx(ctx: dict, key: str):
    """从 ctx 取正数规模，缺省/非法回落默认规模；返回 (值, 是否缺省)。"""
    v = ctx.get(key)
    if v is None:
        return _DEFAULT_SCALE[key], True
    try:
        v = float(v)
    except (TypeError, ValueError):
        return _DEFAULT_SCALE[key], True
    if v <= 0:
        return _DEFAULT_SCALE[key], True
    return v, False


def estimate_task_cost(play, task_type: str, **ctx) -> dict:
    """按任务类型估算单次费用（phase1 修复 3.2：实际使用 ctx 规模计价）。

    ctx: shot_count（镜头数）、has_dialogue（是否有对白）、duration_s（视频时长秒）、
    input_tokens/output_tokens（LLM token 量）
    - PREVIEW_RENDER / VIDEO_COMPOSE 等：按 duration_s 秒计价（缺省 60s 整集规模），
      不再按 1 个平铺单价（旧实现把整集预览按 1 秒视频计价）
    - ASSISTANT_TURN：按 (input_tokens + output_tokens)/1000 千 token 计价
      （旧实现按 1k tokens 计，低报一个数量级）
    - IMAGE_GEN / STORYBOARD 等：按 shot_count 个计价
    - 其余：按 1 次计价
    Returns {"task_type", "estimated_cost", "breakdown", "requires_confirm"}
    """
    # 边界归一：枚举成员或原始字符串都可入，统一为 TaskType 值
    tt = getattr(task_type, "value", task_type)
    label, provider = _TASK_PROVIDER.get(tt, ("DEFAULT", ProviderType.IMAGE))
    unit = _unit_cost(play, provider)
    if tt in _TOKEN_TASKS:
        in_tok, in_def = _int_ctx(ctx, "input_tokens")
        out_tok, out_def = _int_ctx(ctx, "output_tokens")
        k_tokens = (in_tok + out_tok) / 1000.0
        subtotal = unit * Decimal(str(k_tokens))
        items = [{"provider_type": label, "unit": str(unit),
                  "count": round(k_tokens, 3), "count_unit": "1k_tokens",
                  "input_tokens": int(in_tok), "output_tokens": int(out_tok),
                  "scale": "default" if (in_def and out_def) else "provided",
                  "subtotal": str(subtotal)}]
    elif tt in _PER_SECOND_TASKS:
        seconds, used_default = _int_ctx(ctx, "duration_s")
        subtotal = unit * Decimal(str(seconds))
        items = [{"provider_type": label, "unit": str(unit), "unit_per": "second",
                  "count": seconds, "count_unit": "seconds",
                  "scale": "default" if used_default else "provided",
                  "subtotal": str(subtotal)}]
    elif tt in _PER_SHOT_TASKS:
        shots, used_default = _int_ctx(ctx, "shot_count")
        subtotal = unit * Decimal(str(shots))
        items = [{"provider_type": label, "unit": str(unit), "count": int(shots),
                  "count_unit": "shots",
                  "scale": "default" if used_default else "provided",
                  "subtotal": str(subtotal)}]
    else:
        subtotal = unit
        items = [{"provider_type": label, "unit": str(unit), "count": 1,
                  "count_unit": "calls", "subtotal": str(subtotal)}]
    total = Decimal(items[0]["subtotal"])
    return {
        "task_type": tt,
        "estimated_cost": str(total.quantize(Decimal("0.0001"))),
        "breakdown": items,
        "requires_confirm": total >= ALERT_THRESHOLD,
    }


# ─── 生成请求估算 ────────────────────────────────────────────────────────

class CostEstimateError(ValueError):
    """估算请求本身非法（目标不存在/跨剧/未知动作）——业务异常，
    视图映射 4xx（phase1 修复 10.2，不再返回 200 + error 字典零费用）。"""


def estimate_generation_cost(play, action: str, target_id: int) -> dict:
    """估算 request_generation 三类动作的费用。

    action ∈ {redo_shot, redo_scene, preview_episode}
    TTS/VIDEO 单价为 cost_per_second——按估算时长（秒）计价，不按镜头个数，
    否则会系统性低估并压掉 requires_confirm 安全阈值。
    phase1 修复 10.2：目标不存在/跨剧/未知动作抛 CostEstimateError（业务异常），
    调用方（视图/工具）以 4xx 呈现，不再返回 200 零费用误导用户。
    Returns {"action", "target_id", "estimated_cost", "shot_count",
             "breakdown", "requires_confirm"}
    """
    from ..models import Scene, Shot

    if action == "redo_shot":
        shot = Shot.objects.filter(pk=target_id, scene__episode__play=play).first()
        if shot is None:
            raise CostEstimateError(f"镜头 {target_id} 不存在或不属于该剧")
        shots = [shot]
    elif action == "redo_scene":
        scene = Scene.objects.filter(pk=target_id, episode__play=play).first()
        if scene is None:
            raise CostEstimateError(f"场景 {target_id} 不存在或不属于该剧")
        shots = list(Shot.objects.filter(scene=scene))
    elif action == "preview_episode":
        from ..models import Episode
        ep = Episode.objects.filter(pk=target_id, play=play).first()
        if ep is None:
            raise CostEstimateError(f"分集 {target_id} 不存在或不属于该剧")
        # 预览按一集一个视频任务估算
        est = estimate_task_cost(play, TaskType.PREVIEW_RENDER)
        return {"action": action, "target_id": target_id, "shot_count": 0,
                "estimated_cost": est["estimated_cost"],
                "breakdown": est["breakdown"],
                "requires_confirm": est["requires_confirm"]}
    else:
        raise CostEstimateError(f"未知动作 {action}")

    image_unit = _unit_cost(play, ProviderType.IMAGE)
    tts_unit = _unit_cost(play, ProviderType.TTS)
    dialogue_shots = [s for s in shots if (s.dialogue or "").strip()]
    # TTS 按语音时长（秒）计价：中文语速约 4 字/秒，标点不计；
    # 每条对白保底 0.5s（TTS 按实际音频秒数计费，非按次）
    speech_seconds = sum(
        max(0.5, len(_strip_punct(s.dialogue)) / 4.0) for s in dialogue_shots)

    breakdown = [
        {"provider_type": "IMAGE", "unit": str(image_unit), "count": len(shots),
         "subtotal": str(image_unit * len(shots))},
    ]
    if speech_seconds:
        breakdown.append({"provider_type": "TTS", "unit": str(tts_unit),
                         "seconds": round(speech_seconds, 2),
                         "subtotal": str(tts_unit * Decimal(str(round(speech_seconds, 2))))})
    total = sum(Decimal(b["subtotal"]) for b in breakdown)
    return {
        "action": action, "target_id": target_id, "shot_count": len(shots),
        "dialogue_shots": len(dialogue_shots),
        "speech_seconds": round(speech_seconds, 2),
        "estimated_cost": str(total.quantize(Decimal("0.0001"))),
        "breakdown": breakdown,
        "requires_confirm": total >= ALERT_THRESHOLD,
    }


def _strip_punct(text: str) -> str:
    """去掉标点与空白——TTS 不为标点发声，计时不计。"""
    import re
    return re.sub(r"[\s，。！？、；：…—,.!?;:\"'“”‘’（）()\[\]【】]+", "", text or "")


def budget_remaining(session) -> Optional[Decimal]:
    """会话预算余额：max_cost - 已花；无上限返回 None。"""
    from .runner import budget_of, session_cost
    limit = (budget_of(session) or {}).get("max_cost")
    if limit is None:
        return None
    return Decimal(str(limit)) - session_cost(session)
