"""多方案生成（Phase 2.4.2）。

一次调用产出 N 个风格不同的剧本方案，落成同一 alternative_group 的提案：
- 风格参数：pacing（节奏）/ tone（基调）/ structure（结构）/ ending（结尾）
- 多样性保证：方案间文本相似度需低于阈值；不达标时用风格指令重试一次，
  仍不达标则丢弃重复方案（宁可少给，不给假多样）
- 成本有界：N ≤ 3，每次生成都是一次计费调用；单轮总费用受会话预算约束

开关：ENABLE_MULTI_PROPOSAL（默认 false）。
"""
from __future__ import annotations

import difflib
import logging
import uuid

from ..models import AssetType
from .llm_text import chat_text
from .models import ProposalKind
from .proposals import create_proposal, latest_text_asset

logger = logging.getLogger(__name__)

MAX_VARIANTS = 3
SIMILARITY_THRESHOLD = 0.7      # 相似度 ≥ 0.7 视为重复（要求差异 > 30%）
MAX_STYLE_CHARS = 40

STYLE_AXES = {
    "pacing": ("节奏", ["紧凑快切", "舒缓铺陈", "张弛交替"]),
    "tone": ("基调", ["冷峻悬疑", "情感细腻", "爽感直给"]),
    "structure": ("结构", ["单线推进", "双线交叉", "倒叙插叙"]),
    "ending": ("结尾", ["反转收束", "悬念留钩", "情绪爆发"]),
}

VARIANT_PROMPT = (
    "你是短剧编剧。根据下面的现有剧本与风格要求，改写这一集剧本正文。\n"
    "要求：保持人物与既有剧情事实一致；只输出剧本正文，不要解释、不要标题。\n"
    "风格要求：__STYLE__\n\n现有剧本：\n__SCRIPT__"
)

DEFAULT_STYLES = (
    {"pacing": "紧凑快切", "tone": "冷峻悬疑", "ending": "反转收束"},
    {"pacing": "舒缓铺陈", "tone": "情感细腻", "ending": "情绪爆发"},
    {"pacing": "张弛交替", "tone": "爽感直给", "ending": "悬念留钩"},
)


def enabled() -> bool:
    from django.conf import settings
    return bool(getattr(settings, "ENABLE_MULTI_PROPOSAL", False))


def normalize_styles(styles) -> list[dict]:
    """校验风格参数：只接受已知轴与值；未给的轴用默认方案补齐。"""
    out = []
    for i, item in enumerate(styles or []):
        if not isinstance(item, dict):
            continue
        style = {}
        for axis, (_, values) in STYLE_AXES.items():
            value = item.get(axis)
            if isinstance(value, str) and value in values:
                style[axis] = value
        if not style:
            style = dict(DEFAULT_STYLES[i % len(DEFAULT_STYLES)])
        out.append(style)
        if len(out) >= MAX_VARIANTS:
            break
    return out or [dict(s) for s in DEFAULT_STYLES[:MAX_VARIANTS]]


def style_label(style: dict) -> str:
    parts = [f"{STYLE_AXES[k][0]}·{v}" for k, v in style.items() if k in STYLE_AXES]
    return " / ".join(parts)[:MAX_STYLE_CHARS]


def similarity(a: str, b: str) -> float:
    return round(difflib.SequenceMatcher(None, a or "", b or "").ratio(), 4)


def _style_text(style: dict) -> str:
    return "；".join(f"{STYLE_AXES[k][0]}：{v}" for k, v in style.items() if k in STYLE_AXES)


def generate_variant(session, script_text, style, *, gateway=None, record=None) -> str:
    """一次风格化改写；失败返回空串（调用方丢弃该方案）。"""
    if gateway is None:
        from apps.ai_gateway.gateway import AIGateway
        gateway = AIGateway()
    prompt = VARIANT_PROMPT.replace("__STYLE__", _style_text(style)).replace(
        "__SCRIPT__", (script_text or "")[:8000])
    try:
        resp = gateway.chat(session.agent_config,
                            messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
                            play=session.play, task_record=record)
        return chat_text(resp).strip()
    except Exception:  # noqa: BLE001 — 单个方案失败不阻断其余方案
        logger.exception("多方案生成失败 style=%s", style)
        return ""


def generate_variants(session, script_text, styles, *, gateway=None, record=None) -> list[dict]:
    """生成 N 个方案并保证多样性；返回 [{style,label,text,similarity}]。"""
    kept = []
    for style in styles:
        text = generate_variant(session, script_text, style, gateway=gateway, record=record)
        if not text:
            continue
        label = style_label(style)
        max_sim = max((similarity(text, k["text"]) for k in kept), default=0.0)
        if max_sim >= SIMILARITY_THRESHOLD:
            # 与已保留方案过于相似：换更强的差异指令重试一次
            retry_style = {**style, "structure": "双线交叉" if style.get("structure") != "双线交叉" else "倒叙插叙"}
            retry = generate_variant(session, script_text, retry_style, gateway=gateway, record=record)
            retry_sim = max((similarity(retry, k["text"]) for k in kept), default=0.0) if retry else 1.0
            if retry and retry_sim < SIMILARITY_THRESHOLD:
                text, label, max_sim = retry, style_label(retry_style), retry_sim
            else:
                logger.info("丢弃重复方案 %s（相似度 %.2f）", label, max_sim)
                continue
        kept.append({"style": style, "label": label, "text": text, "similarity": max_sim})
    return kept


def scoring_enabled() -> bool:
    """多方案竞争择优打分开关（design D2）：竞争开关开启时对每个方案审稿一次。"""
    from django.conf import settings
    return bool(getattr(settings, "ENABLE_AGENT_COMPETITION", False))


def score_variants(session, variants, *, gateway=None, record=None):
    """对每个方案跑一次 critic 审稿打分，写回 variant["score"]。

    复用 multi_agent 的审稿任务模板（CriticAgent.review_task）与解析
    （parse_critic_feedback）——与竞争协作路径同源同口径。审稿失败或
    无 overall → 分数保持 None（该方案不参与择优）。成本有界：每方案一次。
    """
    if not scoring_enabled() or not variants:
        return variants
    from .multi_agent import CriticAgent, parse_critic_feedback
    if gateway is None:
        from apps.ai_gateway.gateway import AIGateway
        gateway = AIGateway()
    for variant in variants:
        try:
            prompt = CriticAgent.review_task(variant["text"], variant["text"])
            resp = gateway.chat(
                session.agent_config,
                messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
                play=session.play, task_record=record)
            overall = parse_critic_feedback(chat_text(resp)).get("overall")
            if overall is not None:
                variant["score"] = float(overall)
        except Exception:  # noqa: BLE001 — 打分失败不阻断方案落提案
            logger.exception("多方案审稿打分失败 label=%s", variant.get("label"))
    return variants


def create_alternative_proposals(session, message, episode, variants, *, summary_prefix="多方案"):
    """把方案落成同一 alternative_group 的提案，返回提案列表。"""
    current = latest_text_asset(session.play, AssetType.EPISODE_SCRIPT, episode)
    group = uuid.uuid4()
    # 竞争择优（design D2 / 主计划 3.2.6）：组内带分方案中分数最高者独占
    # confidence（/100，供渐进自主判定）。只有最优者能自动应用——避免循环序
    # 早到者抢先应用导致更高分方案因版本已变而过期；其余方案不带分，
    # 保持"多方案对比待选"的产品语义（不会被自动应用打断）。
    scored = [v for v in variants if v.get("score") is not None]
    best = max(scored, key=lambda v: v["score"]) if scored else None
    proposals = []
    for variant in variants:
        payload = {"text": variant["text"]}
        if best is not None and variant is best:
            try:
                payload["confidence"] = max(0.0, min(1.0, float(variant["score"]) / 100.0))
            except (TypeError, ValueError):
                pass
        proposal = create_proposal(
            session, message, ProposalKind.EPISODE_SCRIPT,
            payload=payload,
            summary=f"{summary_prefix}｜{variant['label']}",
            base_version=current.version if current else None,
            target_asset=current, episode=episode,
            alternative_group=group, alternative_label=variant["label"],
            stale_siblings=False,
        )
        proposals.append(proposal)
    return group, proposals


def propose_multiple(session, message, episode_number, *, styles=None, gateway=None, record=None,
                     summary_prefix="多方案") -> dict:
    """工具主入口：生成多方案并建提案。返回 {alternative_group, proposals, discarded}。"""
    from ..models import Episode
    if not enabled():
        raise ValueError("多方案生成未启用（ENABLE_MULTI_PROPOSAL=false）")
    episode = Episode.objects.filter(play=session.play, episode_number=episode_number).first()
    if episode is None:
        raise ValueError(f"第 {episode_number} 集不存在")
    current = latest_text_asset(session.play, AssetType.EPISODE_SCRIPT, episode)
    script_text = ((current.metadata or {}).get("text") if current else "") or ""
    if not script_text.strip():
        raise ValueError("该集尚无剧本文本，无法生成多方案")
    wanted = normalize_styles(styles)
    variants = generate_variants(session, script_text, wanted, gateway=gateway, record=record)
    if not variants:
        raise ValueError("多方案生成失败：未产出有效方案")
    # 竞争择优（主计划 3.2.6 / design D2）：竞争开关开启时为每个方案打分，
    # 最高分者落 payload.confidence（/100）供渐进自动批准；关闭时零行为变化。
    variants = score_variants(session, variants, gateway=gateway, record=record)
    group, proposals = create_alternative_proposals(session, message, episode, variants,
                                                   summary_prefix=summary_prefix)
    return {"alternative_group": str(group),
            "proposals": [{"proposal_id": p.pk, "label": p.alternative_label,
                           "similarity": v["similarity"]} for p, v in zip(proposals, variants)],
            "discarded": len(wanted) - len(variants),
            "note": "已生成多个备选方案，需用户对比后选择其一应用；请勿声称已经修改"}
