"""Agent辅助审查（Phase 1.8.3）：LLM 主观审查 + 确定性审查合并。

审查维度（对齐 spec）：剧情逻辑 / 对白质量 / 节奏 / 情感。
- review_episode_llm(session, episode)：LLM 对剧本做四维度主观审查
- merge_reviews(deterministic, subjective)：合并两类结果为统一报告
- review_report(session, episode)：完整报告 {overall_score, issues, strengths, recommendations}
"""

from __future__ import annotations

import logging
from typing import Optional

from .llm_text import chat_text, extract_json_object
from .review import review_episode as deterministic_review

logger = logging.getLogger(__name__)

REVIEW_SYSTEM = (
    "你是短剧剧本审查专家，从剧情逻辑、对白质量、节奏、情感四维度审查剧本，"
    "输出严格 JSON。审查要具体、可操作，不要空泛评价。"
)

REVIEW_PROMPT = """请审查以下短剧剧本（第{ep}集），输出 JSON：
{{
  "overall_score": 0-100,
  "dimensions": {{
    "plot_logic": {{"score": 0-100, "issues": ["..."], "strengths": ["..."]}},
    "dialogue": {{"score": 0-100, "issues": ["..."], "strengths": ["..."]}},
    "pacing": {{"score": 0-100, "issues": ["..."], "strengths": ["..."]}},
    "emotion": {{"score": 0-100, "issues": ["..."], "strengths": ["..."]}}
  }},
  "recommendations": ["按优先级排列的3-5条改进建议"],
  "summary": "一段总体评价"
}}
{feedback_hint}
剧本概要：{synopsis}
角色：{characters}
剧本内容：
{script}

请确保输出是纯 JSON，不要其他文字。"""

_DIMENSIONS = ("plot_logic", "dialogue", "pacing", "emotion")
_WEIGHTS = {"plot_logic": 0.35, "dialogue": 0.25, "pacing": 0.25, "emotion": 0.15}


def _script_text(episode) -> str:
    """汇集分集的场景/镜头/对白为审查文本。"""
    lines = []
    for scene in episode.scenes.order_by("scene_number").prefetch_related("shots__dialogue_character"):
        lines.append(f"\n【场景{scene.scene_number}】{scene.location or scene.description[:30] or ''}")
        for shot in sorted(scene.shots.all(), key=lambda s: s.shot_number):
            parts = [f"  镜头{shot.shot_number}"]
            if shot.dialogue_character_id:
                try:
                    name = shot.dialogue_character.name
                except Exception:  # noqa: BLE001
                    name = str(shot.dialogue_character_id)
                parts.append(f"[{name}]")
            if shot.dialogue:
                parts.append(f"「{shot.dialogue}」")
            if shot.visual_prompt:
                parts.append(f"(画面: {shot.visual_prompt[:80]})")
            lines.append(" ".join(parts))
    return "\n".join(lines)[:12000]


def _characters_text(play) -> str:
    chars = play.characters.all()[:10]
    return "; ".join(f"{c.name}({c.role or '角色'})" for c in chars) or "无"


def feedback_weight_hint(play) -> str:
    """Phase 3.5.2 / design D4：本剧近期审查反馈的维度权重提示段。

    统计近 REVIEW_FEEDBACK_WINDOW_DAYS 天 ReviewFeedback 的 issues_feedback，
    disagree ≥ agree 的维度生成"仅在明确时报告"提示；无反馈返回空串
    （prompt 与建设前逐字节一致——REVIEW_PROMPT 的 {feedback_hint} 占位
    在空串时产生一个空行差异，故空时返回 "" 且模板占位行以 {feedback_hint}
    独立成行、空串时该行为空行，审查行为无实质变化）。
    """
    try:
        from datetime import timedelta

        from django.conf import settings
        from django.utils import timezone as dj_tz

        from .models import ReviewFeedback

        window = int(getattr(settings, "REVIEW_FEEDBACK_WINDOW_DAYS", 30))
        since = dj_tz.now() - timedelta(days=window)
        rows = ReviewFeedback.objects.filter(episode__play=play,
                                             created_at__gte=since)
        dims = {}
        for fb in rows[:200]:
            for item in (fb.issues_feedback or []):
                dim = str(item.get("issue_key", "")).split("-")[0]
                if not dim:
                    continue
                agg = dims.setdefault(dim, {"agree": 0, "disagree": 0})
                if item.get("agree") is True:
                    agg["agree"] += 1
                elif item.get("agree") is False:
                    agg["disagree"] += 1
        lines = []
        for dim, agg in sorted(dims.items()):
            if agg["disagree"] >= agg["agree"] and agg["disagree"] > 0:
                lines.append(f"- 「{dim}」维度：用户近期 {agg['disagree']} 次不认同 / "
                             f"{agg['agree']} 次认同，该维度问题仅在明确时报告")
        if not lines:
            return ""
        return ("用户对近期审查的反馈（请据此调整审查尺度）：\n" + "\n".join(lines))
    except Exception:  # noqa: BLE001 — 反馈统计失败不影响审查本身
        logger.exception("审查反馈统计失败 play=%s", getattr(play, "pk", None))
        return ""


def review_episode_llm(session, episode, reviewer=None, task_record=None) -> dict:
    """LLM 主观审查。reviewer(prompt_kwargs)->dict；None 走 AIGateway。

    task_record：归因 LLM 费用到本轮记录（入账/预算）；/review/ 端点无记录时为 None。"""
    payload = {
        "ep": episode.episode_number,
        "synopsis": (episode.play.synopsis or "")[:300],
        "characters": _characters_text(episode.play),
        "script": _script_text(episode),
        "feedback_hint": feedback_weight_hint(episode.play),
    }
    if reviewer is None:
        reviewer = lambda **kw: llm_review(session, task_record=task_record, **kw)
    degraded = False
    try:
        data = reviewer(**payload)
    except Exception as exc:  # noqa: BLE001
        logger.warning("LLM审查失败 ep=%s: %s", episode.episode_number, exc)
        data = None
        degraded = True
    # phase1 修复 5.2：模型输出完全不可解析（None/非 dict/空 dict）视为降级，
    # 结果带 degraded: true，与"评分为 0 的正常结果"可区分
    if not isinstance(data, dict) or not data:
        degraded = True
        data = {}
    result = _normalize_review(data)
    result["degraded"] = degraded
    return result


def llm_review(session, task_record=None, **kw) -> dict:
    """经 AIGateway 生成审查 JSON。task_record：归因费用到本轮记录。

    feedback_hint 缺省补空串（既有调用方/测试不带该 key 时 prompt 与
    建设前逐字节一致——占位行成空行，无实质审查行为变化）。"""
    from apps.ai_gateway.gateway import AIGateway
    kw.setdefault("feedback_hint", "")
    resp = AIGateway().chat(
        session.agent_config,
        messages=[{"role": "user", "content": [{"type": "text", "text": REVIEW_PROMPT.format(**kw)}]}],
        system=REVIEW_SYSTEM,
        play=session.play,
        task_record=task_record,
    )
    return extract_json_object(chat_text(resp))


def _normalize_review(data: dict) -> dict:
    """规整 LLM 输出为标准结构（容错缺字段/类型错）。

    phase1 修复 5.2：对 data/dimensions 做类型守卫——非 dict 按空处理，
    不再因 dimensions 为 null/数组/字符串而抛 AttributeError（500）。"""
    if not isinstance(data, dict):
        data = {}
    dims_raw = data.get("dimensions")
    if not isinstance(dims_raw, dict):
        dims_raw = {}
    dims = {}
    for d in _DIMENSIONS:
        v = dims_raw.get(d) or {}
        if not isinstance(v, dict):
            v = {}
        dims[d] = {
            "score": _clamp(v.get("score")),
            "issues": _str_list(v.get("issues")),
            "strengths": _str_list(v.get("strengths")),
        }
    overall = data.get("overall_score")
    if overall is None:
        overall = sum(dims[d]["score"] * _WEIGHTS[d] for d in _DIMENSIONS)
    return {
        "overall_score": round(_clamp(overall)),
        "dimensions": dims,
        "recommendations": _str_list(data.get("recommendations")),
        "summary": str(data.get("summary") or ""),
    }


def _clamp(v, lo=0, hi=100):
    try:
        return max(lo, min(hi, float(v)))
    except (TypeError, ValueError):
        return 0.0


def _str_list(v) -> list:
    if not isinstance(v, list):
        return []
    return [str(x)[:300] for x in v if x][:10]


def merge_reviews(deterministic: dict, subjective: dict) -> dict:
    """合并确定性审查（review_episode）与 LLM 主观审查。

    - issues = 确定性问题（标 severity）+ 主观 issues（标 dimension）
    - overall_score = 主观 overall_score（确定性扣分作为 metadata）
    """
    det_issues = []
    for i in (deterministic.get("issues") or []):
        det_issues.append({**i, "source": "deterministic",
                           "dimension": "structure"})
    sub_issues = []
    for dim, v in (subjective.get("dimensions") or {}).items():
        for msg in v.get("issues", []):
            sub_issues.append({"code": f"subjective_{dim}", "severity": "info",
                               "message": msg, "source": "subjective", "dimension": dim})

    strengths = []
    for dim, v in (subjective.get("dimensions") or {}).items():
        for s in v.get("strengths", []):
            strengths.append({"dimension": dim, "message": s})

    det_errors = deterministic.get("stats", {}).get("errors", 0)
    det_warnings = deterministic.get("stats", {}).get("warnings", 0)
    # 确定性错误对总分扣分：每 error -8，每 warning -3（叠加在主观分上，下限0）
    score = max(0, subjective.get("overall_score", 0) - det_errors * 8 - det_warnings * 3)

    return {
        "overall_score": score,
        "summary": subjective.get("summary", ""),
        "dimensions": subjective.get("dimensions", {}),
        "issues": det_issues + sub_issues,
        "strengths": strengths,
        "recommendations": subjective.get("recommendations", []),
        "stats": deterministic.get("stats", {}),
        "deterministic_penalty": det_errors * 8 + det_warnings * 3,
        # phase1 修复 5.2：降级标记穿透合并层，报告层可辨识
        "degraded": bool(subjective.get("degraded")),
    }


def review_report(session, episode, reviewer=None) -> dict:
    """完整审查报告：确定性 + 主观合并 + 反馈趋势段（Phase 3.5.2.4）。"""
    det = deterministic_review(episode)
    sub = review_episode_llm(session, episode, reviewer=reviewer)
    report = merge_reviews(det, sub)
    report["episode_number"] = episode.episode_number
    report["episode_id"] = episode.pk
    # 反馈趋势附报告尾部（无反馈时零计数，与 trends 端点同口径）
    report["feedback_trends"] = _feedback_trends_of(episode.play)
    return report


def _feedback_trends_of(play) -> dict:
    """按维度聚合本剧反馈 agree/disagree（design D4；失败返回空计数不炸报告）。"""
    try:
        from datetime import timedelta

        from django.conf import settings
        from django.utils import timezone as dj_tz

        from .models import ReviewFeedback

        window = int(getattr(settings, "REVIEW_FEEDBACK_WINDOW_DAYS", 30))
        since = dj_tz.now() - timedelta(days=window)
        dims: dict = {}
        for fb in ReviewFeedback.objects.filter(episode__play=play,
                                               created_at__gte=since)[:200]:
            for item in (fb.issues_feedback or []):
                dim = str(item.get("issue_key", "")).split("-")[0]
                if not dim:
                    continue
                agg = dims.setdefault(dim, {"agree": 0, "disagree": 0})
                if item.get("agree") is True:
                    agg["agree"] += 1
                elif item.get("agree") is False:
                    agg["disagree"] += 1
        return {"window_days": window, "dimensions": dims}
    except Exception:  # noqa: BLE001 — 趋势失败不影响报告
        logger.exception("审查反馈趋势聚合失败 play=%s", getattr(play, "pk", None))
        return {"window_days": 30, "dimensions": {}}
