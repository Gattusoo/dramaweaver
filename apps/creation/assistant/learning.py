"""经验学习机制（Phase 2.2 / P1）。

- 经验提取：从成功轮次（outcome=done 且产生提案）提取结构化技能（LLM + 启发式降级）
- 技能库：成功/失败计数累加，置信度用 Laplace 平滑 (s+1)/(s+f+2)
- 检索：按用户文本匹配触发条件打分，取前 5 注入 system prompt
- 策略推荐：样本量达阈值后按成功率推荐，样本不足返回空
- A/B 实验：权重分桶（稳定哈希）+ 结果记录 + 报告

开关：ENABLE_LEARNING（总开关）/ ENABLE_SKILL_LIBRARY（技能库注入与沉淀）。
"""
from __future__ import annotations

import hashlib
import logging
from decimal import Decimal
from typing import Iterable, Optional

from django.db.models import Sum

from .llm_text import chat_text, extract_json_object
from .models import (
    AssistantProposal, ExperimentStatus, SkillLibrary, SkillScope, SkillType, StrategyExperiment,
)

logger = logging.getLogger(__name__)

MAX_SEARCH_RESULTS = 5
CONFIDENCE_PRIORITY = 0.8          # 置信度高于此值的技能优先排序
MIN_SAMPLES_FOR_RECOMMEND = 3      # 策略推荐的最小样本量
MAX_TRIGGERS = 8
MAX_STEPS = 8
MAX_NAME_CHARS = 200
MAX_EXTRACT_CHARS = 4000

EXTRACT_PROMPT = (
    "你是短剧创作经验提炼器。阅读下面这次成功任务的对话摘要，提炼可复用的经验，"
    "只输出一个 JSON 对象：{\"name\":\"不超过40字的技能名\",\"skill_type\":"
    "\"STYLE|STRUCTURE|TOOL_SEQUENCE|RECOVERY|OTHER\",\"trigger_conditions\":[\"用户请求中出现的关键词\"],"
    "\"execution_steps\":[\"可执行的步骤\"],\"description\":\"一句话说明适用场景\"}。\n"
    "trigger_conditions 与 execution_steps 各 1-5 条，务必简短。不要输出 JSON 以外的文字。"
)

# 启发式兜底：按关键词归类（trigger -> (type, name, steps)）
_HEURISTICS = (
    (("反转", "钩子", "悬念"), SkillType.STRUCTURE, "反转钩子强化",
     ["读取当前集剧本", "定位结尾钩子", "提出反转提案"]),
    (("对白", "台词", "口语"), SkillType.STYLE, "对白口语化润色",
     ["读取目标场景", "统一角色语言风格", "提交对白修改提案"]),
    (("节奏", "拖沓", "加快"), SkillType.STYLE, "节奏压缩",
     ["读取分集结构", "标注冗余段落", "提交删改提案"]),
    (("角色", "人设", "一致性"), SkillType.STRUCTURE, "角色一致性校验",
     ["读取角色设定", "对照剧本检查", "提交设定修正提案"]),
    (("重试", "失败", "报错"), SkillType.RECOVERY, "失败后换工具重试",
     ["读取错误信息", "改用等价只读工具", "汇报结论"]),
)


def enabled() -> bool:
    from django.conf import settings
    return bool(getattr(settings, "ENABLE_LEARNING", False))


def library_enabled() -> bool:
    from django.conf import settings
    return enabled() and bool(getattr(settings, "ENABLE_SKILL_LIBRARY", False))


# ── 置信度 ─────────────────────────────────────────────────────────────────

def confidence_of(success: int, failure: int) -> float:
    """Laplace 平滑：无样本时 0.5；9 成功 1 失败 → 0.8333。"""
    s, f = max(int(success), 0), max(int(failure), 0)
    return round((s + 1) / (s + f + 2), 4)


def _refresh(skill: SkillLibrary, *, save=True):
    skill.confidence = confidence_of(skill.success_count, skill.failure_count)
    if save:
        skill.save(update_fields=["confidence", "updated_at"])
    return skill.confidence


# ── 提取 ───────────────────────────────────────────────────────────────────

def _transcript(session, limit=MAX_EXTRACT_CHARS) -> str:
    """最近几条消息的文本摘要，供提取器使用。"""
    rows = session.messages.order_by("-seq").values_list("role", "content")[:8]
    parts = []
    for role, content in reversed(list(rows)):
        text = "".join(b.get("text", "") for b in (content or []) if isinstance(b, dict))
        if text:
            parts.append(f"{role}: {text}")
    return "\n".join(parts)[:limit]


def _clean_list(values, limit) -> list[str]:
    out = []
    for v in values if isinstance(values, (list, tuple)) else []:
        if isinstance(v, str) and v.strip():
            out.append(v.strip()[:100])
        if len(out) >= limit:
            break
    return out


def _normalize_experience(data: dict) -> dict:
    name = (data.get("name") or "").strip()[:MAX_NAME_CHARS]
    skill_type = (data.get("skill_type") or "").strip().upper()
    if skill_type not in SkillType.values:
        skill_type = SkillType.OTHER
    return {
        "name": name,
        "skill_type": skill_type,
        "trigger_conditions": _clean_list(data.get("trigger_conditions"), MAX_TRIGGERS),
        "execution_steps": _clean_list(data.get("execution_steps"), MAX_STEPS),
        "description": (data.get("description") or "").strip()[:500],
    }


def heuristic_experience(text: str) -> dict:
    """关键词启发式：命中则归类，未命中给通用技能。

    未命中时不把用户文本切片做触发条件——单字符会在检索打分中对任意后续
    消息大量误命中（hits*1.0 占主导），压过关键词精确匹配的真技能。
    """
    for keywords, skill_type, name, steps in _HEURISTICS:
        hits = [k for k in keywords if k in (text or "")]
        if hits:
            return {"name": name, "skill_type": skill_type, "trigger_conditions": hits,
                    "execution_steps": list(steps), "description": f"命中关键词：{'、'.join(hits)}"}
    return {"name": "通用创作任务流程", "skill_type": SkillType.OTHER,
            "trigger_conditions": [],
            "execution_steps": ["读取相关剧本", "生成提案", "等待用户确认"],
            "description": "无明确关键词时的通用流程"}


def extract_experience(session, record=None, *, gateway=None) -> dict:
    """从会话最近内容提取经验；LLM 失败降级启发式。返回规范化经验 dict。"""
    text = _transcript(session)
    data = {}
    try:
        if gateway is None:
            from apps.ai_gateway.gateway import AIGateway
            gateway = AIGateway()
        resp = gateway.chat(session.agent_config,
                            messages=[{"role": "user", "content": [
                                {"type": "text", "text": f"{EXTRACT_PROMPT}\n\n任务记录：\n{text}"}]}],
                            play=session.play, task_record=record)
        data = extract_json_object(chat_text(resp))
    except Exception:  # noqa: BLE001 — 提取失败不影响轮次
        logger.exception("经验提取 LLM 调用失败，降级启发式")
    experience = _normalize_experience(data) if data.get("name") else heuristic_experience(text)
    if not experience["trigger_conditions"]:
        experience["trigger_conditions"] = _clean_list([experience["name"]], MAX_TRIGGERS)
    return experience


# ── 技能库写入 ─────────────────────────────────────────────────────────────

def upsert_skill(session, experience: dict, *, record=None, succeeded=True) -> SkillLibrary:
    """按（作用域=PLAY, play, name）写入或累加；返回技能。"""
    name = (experience.get("name") or "").strip()[:MAX_NAME_CHARS] or "未命名经验"
    skill, _created = SkillLibrary.objects.get_or_create(
        scope=SkillScope.PLAY, play=session.play, name=name,
        defaults={
            "skill_type": experience.get("skill_type") or SkillType.OTHER,
            "description": experience.get("description") or "",
            "trigger_conditions": experience.get("trigger_conditions") or [],
            "execution_steps": experience.get("execution_steps") or [],
            "source_turn": record,
        })
    if succeeded:
        skill.success_count += 1
    else:
        skill.failure_count += 1
    # 合并触发条件（去重保序，上限）
    merged = list(dict.fromkeys(list(skill.trigger_conditions or []) +
                                list(experience.get("trigger_conditions") or [])))[:MAX_TRIGGERS]
    skill.trigger_conditions = merged
    if not skill.execution_steps and experience.get("execution_steps"):
        skill.execution_steps = experience["execution_steps"]
    _refresh(skill, save=False)
    skill.save(update_fields=["success_count", "failure_count", "trigger_conditions",
                              "execution_steps", "confidence", "updated_at"])
    return skill


def update_skill_library(session, record=None, *, succeeded=True, gateway=None) -> Optional[SkillLibrary]:
    """成功轮次沉淀经验；失败轮次只对已有同名技能累加失败计数。开关关闭返回 None。"""
    if not library_enabled():
        return None
    try:
        experience = extract_experience(session, record, gateway=gateway)
        return upsert_skill(session, experience, record=record, succeeded=succeeded)
    except Exception:  # noqa: BLE001 — 学习失败绝不影响轮次
        logger.exception("技能库更新失败 session=%s", getattr(session, "pk", None))
        return None


def should_learn(session, record) -> bool:
    """成功且有提案的轮次才值得沉淀（避免把闲聊写进技能库）。"""
    outcome = ((record.output_summary or {}).get("outcome") if record else None)
    if outcome not in ("done",):
        return False
    return AssistantProposal.objects.filter(session=session).exists()


# ── 检索 ───────────────────────────────────────────────────────────────────

def _match_score(skill: SkillLibrary, text: str) -> float:
    text = text or ""
    hits = sum(1 for t in (skill.trigger_conditions or []) if t and t in text)
    if not hits:
        return 0.0
    score = hits * 1.0 + skill.confidence
    if skill.confidence >= CONFIDENCE_PRIORITY:
        score += 0.5
    return score


def search_skills(text: str, *, play=None, limit=MAX_SEARCH_RESULTS) -> list[SkillLibrary]:
    """匹配触发条件打分，返回前 limit 条（全局 + 本剧作用域）。"""
    from django.db.models import Q
    scope_q = Q(scope=SkillScope.GLOBAL)
    if play is not None:
        scope_q |= Q(scope=SkillScope.PLAY, play=play)
    skills = SkillLibrary.objects.filter(is_active=True).filter(scope_q)
    scored = [(s, _match_score(s, text)) for s in skills]
    scored = [(s, sc) for s, sc in scored if sc > 0]
    scored.sort(key=lambda pair: (-pair[1], -pair[0].confidence, -pair[0].success_count))
    return [s for s, _ in scored[:max(int(limit), 0)]]


def build_learning_prompt_section(skills: Iterable[SkillLibrary]) -> str:
    skills = list(skills)
    if not skills:
        return ""
    lines = ["\n\n## 相关经验（可能适用）",
             "以下是本剧历史成功任务沉淀的策略，仅在确实相关时参考，不必照搬："]
    for s in skills:
        triggers = "、".join(s.trigger_conditions or [])
        lines.append(f"- {s.name}（置信度 {s.confidence:.2f}，触发：{triggers}）：{s.description}")
    return "\n".join(lines)


# ── 策略推荐 ───────────────────────────────────────────────────────────────

def optimize_strategy(*, play=None, min_samples=MIN_SAMPLES_FOR_RECOMMEND) -> Optional[dict]:
    """按成功率推荐技能；样本不足返回 None。"""
    from django.db.models import Q
    scope_q = Q(scope=SkillScope.GLOBAL)
    if play is not None:
        scope_q |= Q(scope=SkillScope.PLAY, play=play)
    candidates = [s for s in SkillLibrary.objects.filter(is_active=True).filter(scope_q)
                  if s.samples >= min_samples]
    if not candidates:
        return None
    best = max(candidates, key=lambda s: (s.confidence, s.samples))
    rate = best.success_count / best.samples
    return {"skill_id": best.pk, "name": best.name, "confidence": best.confidence,
            "success_rate": round(rate, 4), "samples": best.samples}


# ── A/B 实验 ───────────────────────────────────────────────────────────────

def _bucket(key: str, total_weight: int) -> int:
    digest = hashlib.sha256(str(key).encode("utf-8")).hexdigest()
    return int(digest[:8], 16) % max(total_weight, 1)


def normalize_variants(variants) -> list[dict]:
    out = []
    for i, v in enumerate(variants or []):
        if not isinstance(v, dict):
            continue
        key = str(v.get("key") or chr(ord("A") + i))[:20]
        try:
            weight = max(int(v.get("weight", 1)), 1)
        except (TypeError, ValueError):
            weight = 1
        out.append({"key": key, "name": str(v.get("name") or key)[:100], "weight": weight,
                    "success": int(v.get("success", 0) or 0), "failure": int(v.get("failure", 0) or 0)})
    return out


def assign_variant(experiment: StrategyExperiment, bucket_key: str) -> Optional[dict]:
    """按权重确定性分桶：同一 bucket_key 永远得到同一变体。"""
    variants = normalize_variants(experiment.variants)
    if not variants:
        return None
    total = sum(v["weight"] for v in variants)
    point = _bucket(bucket_key, total)
    cursor = 0
    for v in variants:
        cursor += v["weight"]
        if point < cursor:
            return v
    return variants[-1]


def record_outcome(experiment: StrategyExperiment, variant_key: str, *, success: bool) -> dict:
    """记录某变体的成功/失败，返回更新后的该变体。"""
    variants = normalize_variants(experiment.variants)
    target = next((v for v in variants if v["key"] == variant_key), None)
    if target is None:
        raise KeyError(f"变体 {variant_key} 不存在")
    target["success" if success else "failure"] += 1
    experiment.variants = variants
    experiment.save(update_fields=["variants", "updated_at"])
    return target


def experiment_report(experiment: StrategyExperiment) -> dict:
    variants = normalize_variants(experiment.variants)
    rows = []
    for v in variants:
        samples = v["success"] + v["failure"]
        rows.append({
            "key": v["key"], "name": v["name"], "weight": v["weight"],
            "success": v["success"], "failure": v["failure"], "samples": samples,
            "success_rate": round(v["success"] / samples, 4) if samples else None,
            "confidence": confidence_of(v["success"], v["failure"]),
        })
    eligible = [r for r in rows if r["samples"] >= MIN_SAMPLES_FOR_RECOMMEND]
    best = max(eligible, key=lambda r: (r["success_rate"], r["samples"])) if eligible else None
    return {
        "id": experiment.pk, "name": experiment.name, "status": experiment.status,
        "variants": rows, "best_variant": best["key"] if best else None,
        "winner": best,
    }


def cost_comparison_report(experiment: StrategyExperiment) -> dict:
    """成本对比实验报告（主计划 3.3.11 / design D5）。

    variant 带 "cost_record_ids"（CostRecord pk 列表）时输出两组费用均值对比；
    任一组无记录 → 样本不足明示，不给出结论。复用 experiment_report 的变体
    骨架（成功率/置信度照常输出）。
    """
    from decimal import Decimal

    from apps.ai_gateway.models import CostRecord

    base = experiment_report(experiment)
    variants = base.pop("variants", [])
    # cost_record_ids 是非规范字段：normalize_variants 会剥掉，从原始 variants 取
    raw_ids = {str(v.get("key")): (v.get("cost_record_ids") or [])
               for v in (experiment.variants or []) if isinstance(v, dict)}
    rows = []
    for v in variants:
        ids = raw_ids.get(str(v["key"])) or []
        if ids:
            qs = CostRecord.objects.filter(pk__in=ids)
            total = qs.aggregate(t=Sum("total_cost"))["t"] or Decimal("0")
            n = qs.count()
            v["cost_total"] = float(total)
            v["cost_avg"] = round(float(total) / n, 6) if n else None
            v["cost_samples"] = n
        else:
            v["cost_total"] = 0.0
            v["cost_avg"] = None
            v["cost_samples"] = 0
        rows.append(v)
    sampled = [r for r in rows if r["cost_samples"] > 0]
    conclusion = None
    if len(sampled) >= 2:
        a, b = sampled[0], sampled[1]
        diff = round(a["cost_avg"] - b["cost_avg"], 6)
        cheaper = b["key"] if diff > 0 else a["key"]
        conclusion = {
            "variant_a": a["key"], "variant_b": b["key"],
            "avg_diff": diff, "cheaper_variant": cheaper,
        }
    base["variants"] = rows
    base["cost_conclusion"] = conclusion
    base["cost_samples_sufficient"] = len(sampled) >= 2
    return base


def finish_if_conclusive(experiment: StrategyExperiment, *, min_samples=MIN_SAMPLES_FOR_RECOMMEND):
    """所有变体样本量达标且最优变体成功率显著领先时标记 COMPLETED。"""
    report = experiment_report(experiment)
    rows = report["variants"]
    if not rows or any(r["samples"] < min_samples for r in rows):
        return report
    rates = sorted((r["success_rate"] for r in rows), reverse=True)
    if len(rates) >= 2 and rates[0] - rates[1] >= 0.1 and experiment.status == ExperimentStatus.RUNNING:
        experiment.status = ExperimentStatus.COMPLETED
        experiment.save(update_fields=["status", "updated_at"])
        report["status"] = experiment.status
    return report
