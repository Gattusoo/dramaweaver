"""多模态视觉理解（Phase 2.3 / P1）。

- analyze_image：图片 → 结构化描述（场景/角色/构图/文字/问题）
- compare_with_script：图片 vs 剧本要求 → 一致性分数 + 差异
- suggest_improvements：低分时给出可执行的 prompt 改进方向
- review_asset：资产入库后的自动质检（低分标记 + 建议重新生成）
- 视觉反馈循环：分数低于阈值时可带改进建议重新生成一次（成本有界）

开关：ENABLE_MULTIMODAL（总开关）/ ENABLE_VISION_UNDERSTANDING（视觉分析）。
预算：assistant_budget.max_vision_cost 限定单次视觉分析费用上限；超限降级为不做视觉分析。
"""
from __future__ import annotations

import logging
from decimal import Decimal

from .llm_text import chat_text, extract_json_object

logger = logging.getLogger(__name__)

CONSISTENCY_THRESHOLD = 0.8      # 低于此分数视为不一致（触发重生成）
MAX_IMAGE_CHARS = 4000

ANALYZE_PROMPT = (
    "分析这张短剧镜头画面，只输出 JSON：{\"scene\":\"场景描述\",\"characters\":[\"人物与外貌/服饰\"],"
    "\"composition\":\"构图与机位\",\"mood\":\"情绪氛围\",\"text\":\"画面中可见文字\","
    "\"issues\":[\"明显问题，如变形、多手、错字、风格不符\"]}。不要输出 JSON 以外的文字。"
)

COMPARE_PROMPT = (
    "你是短剧分镜审片。根据剧本要求判断画面是否符合，只输出 JSON："
    "{\"consistency\":0.0到1.0的小数,\"matches\":[\"符合点\"],\"mismatches\":[\"不符合点\"],"
    "\"reason\":\"一句话结论\"}。剧本要求：__EXPECTED__\n不要输出 JSON 以外的文字。"
)

SUGGEST_PROMPT = (
    "根据画面分析结果与不符合点，给出可执行的画面改进建议，只输出 JSON："
    "{\"suggestions\":[\"具体改进，如镜头/服饰/光线/构图\"],\"prompt_patch\":\"可直接追加到生图 prompt 的一句话\"}。"
    "不要输出 JSON 以外的文字。"
)


class VisionUnavailable(Exception):
    """视觉能力未启用/无视觉模型/预算不足：调用方降级为不使用视觉分析。"""


def enabled() -> bool:
    from django.conf import settings
    return (bool(getattr(settings, "ENABLE_MULTIMODAL", False))
            and bool(getattr(settings, "ENABLE_VISION_UNDERSTANDING", False)))


def _vision_budget(session) -> Decimal | None:
    cfg = (session.agent_config.assistant_budget or {}) if session and session.agent_config else {}
    limit = cfg.get("max_vision_cost")
    return None if limit is None else Decimal(str(limit))


def _vision_spent(session, record) -> Decimal:
    """当前时点 LLM 类（含视觉）累计费用，供质检内对账增量。

    CostRecord 在网关成功后落库（视觉走 CHAT、计费归 LLM）。质检开始时记录基准，
    每步调用后重查，二者之差即本次质检已花的视觉费用。
    """
    from apps.ai_gateway.models import CostRecord
    from django.db.models import Sum
    if record is not None and getattr(record, "pk", None) is not None:
        total = (CostRecord.objects.filter(task_record=record, provider_type="LLM")
                 .aggregate(s=Sum("total_cost"))["s"])
    else:
        total = (CostRecord.objects.filter(play=session.play, provider_type="LLM")
                 .aggregate(s=Sum("total_cost"))["s"])
    return total or Decimal("0")


def _gateway(gateway=None):
    if gateway is not None:
        return gateway
    from apps.ai_gateway.gateway import AIGateway
    return AIGateway()


def _ask(session, image_url, prompt, *, gateway=None, record=None, play=None):
    """一次视觉调用 → (JSON dict, resp)。失败抛 VisionUnavailable。"""
    gw = _gateway(gateway)
    try:
        resp = gw.vision(session.agent_config, image_url=image_url, prompt=prompt,
                         play=play or session.play, task_record=record)
    except Exception as exc:  # noqa: BLE001 — 视觉失败统一降级
        raise VisionUnavailable(str(exc)) from exc
    return extract_json_object(chat_text(resp)), resp


def analyze_image(session, image_url, *, gateway=None, record=None) -> dict:
    """图片 → 结构化描述。未启用/失败抛 VisionUnavailable。"""
    if not enabled():
        raise VisionUnavailable("视觉理解未启用")
    if not image_url:
        raise VisionUnavailable("缺少图片地址")
    data, _ = _ask(session, image_url, ANALYZE_PROMPT, gateway=gateway, record=record)
    if not data:
        raise VisionUnavailable("视觉模型未返回可解析结果")
    issues = data.get("issues") if isinstance(data.get("issues"), list) else []
    return {
        "scene": str(data.get("scene") or "")[:MAX_IMAGE_CHARS],
        "characters": [str(c)[:200] for c in (data.get("characters") or []) if str(c).strip()][:10],
        "composition": str(data.get("composition") or "")[:500],
        "mood": str(data.get("mood") or "")[:200],
        "text": str(data.get("text") or "")[:500],
        "issues": [str(i)[:200] for i in issues][:10],
    }


def compare_with_script(session, image_url, expected, *, gateway=None, record=None) -> dict:
    """图片 vs 剧本要求 → {consistency, matches, mismatches, reason}。"""
    if not enabled():
        raise VisionUnavailable("视觉理解未启用")
    # 用 replace 而非 str.format：提示词内 JSON 花括号会被 format 当成占位符
    prompt = COMPARE_PROMPT.replace("__EXPECTED__", (expected or "")[:2000])
    data, _ = _ask(session, image_url, prompt, gateway=gateway, record=record)
    try:
        consistency = float(data.get("consistency"))
    except (TypeError, ValueError):
        consistency = None
    if consistency is None:
        raise VisionUnavailable("视觉模型未返回一致性分数")
    return {
        "consistency": max(0.0, min(1.0, consistency)),
        "matches": [str(m)[:200] for m in (data.get("matches") or [])][:10],
        "mismatches": [str(m)[:200] for m in (data.get("mismatches") or [])][:10],
        "reason": str(data.get("reason") or "")[:500],
        "consistent": consistency >= CONSISTENCY_THRESHOLD,
    }


def suggest_improvements(session, image_url, *, expected="", analysis=None,
                         gateway=None, record=None) -> dict:
    """生成改进建议；优先复用已得到的分析结果，避免重复视觉调用。"""
    if not enabled():
        raise VisionUnavailable("视觉理解未启用")
    context = ""
    if analysis:
        context = f"画面分析：{analysis}\n"
    if expected:
        context += f"剧本要求：{expected}\n"
    data, _ = _ask(session, image_url, f"{context}\n{SUGGEST_PROMPT}", gateway=gateway, record=record)
    suggestions = data.get("suggestions") if isinstance(data.get("suggestions"), list) else []
    return {"suggestions": [str(s)[:300] for s in suggestions][:6],
            "prompt_patch": str(data.get("prompt_patch") or "")[:500]}


def review_asset(session, asset, *, expected="", gateway=None, record=None) -> dict:
    """资产质检：分析 + 一致性 + 建议。返回含 needs_regeneration 的结论。

    预算（assistant_budget.max_vision_cost）约束本次质检的累计视觉费用：
    每步视觉调用前对账已花费用，超限即降级为不再继续（skipped=True）。
    """
    cap = _vision_budget(session)
    if cap is not None and cap <= 0:
        return {"skipped": True, "reason": "视觉分析预算为 0", "needs_regeneration": False}

    # 质检内增量对账：基准 + 每步调用后的累计差值即本次质检的视觉费用
    baseline = _vision_spent(session, record)

    def _over_cap() -> bool:
        return cap is not None and _vision_spent(session, record) - baseline >= cap

    url = getattr(asset, "artifact_url", "") or ""
    result = {"asset_id": getattr(asset, "pk", None), "skipped": False,
              "needs_regeneration": False}
    try:
        analysis = analyze_image(session, url, gateway=gateway, record=record)
        result["analysis"] = analysis
        if analysis.get("issues"):
            result["needs_regeneration"] = True
        if expected and not _over_cap():
            comparison = compare_with_script(session, url, expected, gateway=gateway, record=record)
            result["comparison"] = comparison
            if not comparison["consistent"]:
                result["needs_regeneration"] = True
        else:
            if expected and _over_cap():
                result["budget_stopped"] = True
        if result["needs_regeneration"] and not _over_cap():
            result["suggestions"] = suggest_improvements(
                session, url, expected=expected, analysis=analysis, gateway=gateway, record=record)
        elif result["needs_regeneration"]:
            result["budget_stopped"] = True
    except VisionUnavailable as exc:
        result.update({"skipped": True, "reason": str(exc)[:300], "needs_regeneration": False})
    return result


def analyze_generated_image_tool(ctx, asset_id):
    """工具入口：分析本剧已生成的图片资产（只读）。"""
    from ..models import Asset, AssetType
    asset = Asset.objects.filter(pk=asset_id, play=ctx.play, asset_type=AssetType.IMAGE).first()
    if asset is None:
        raise ValueError("图片资产不存在或不属于本剧")
    expected = (asset.shot.visual_prompt if asset.shot else "") or asset.prompt
    record = getattr(getattr(ctx, "message", None), "task_record", None)
    try:
        return review_asset(ctx.session, asset, expected=expected, record=record)
    except VisionUnavailable as exc:
        return {"skipped": True, "reason": str(exc)[:300], "needs_regeneration": False}
