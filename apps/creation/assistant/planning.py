"""规划引擎（Phase 1.6）：复杂任务分解 + 动态调整 + Reflexion 反思。

设计：
- estimate_task_complexity：启发式评估 0-1，>0.5 触发规划，>0.7 可启用多路径
- PlanningEngine.create_plan：LLM 分解目标为 3-7 步（失败降级启发式模板）
- update_plan / should_update_plan：完成过半、意外结果、阻塞时修订，留 PlanRevision
- Reflexion：同一工具连续失败 >=2 次 → reflect_on_failure 生成
  {root_cause, lessons_learned, strategy_adjustment}，作为 "reflection" 条目写会话树
- 步骤状态机：PENDING | IN_PROGRESS | COMPLETED | FAILED | SKIPPED
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Optional

from django.utils import timezone

from .llm_text import chat_text, extract_json_array, extract_json_object
from . import events
from .models import AssistantSession, Plan, PlanRevision, PlanStatus

logger = logging.getLogger(__name__)

STEP_STATUSES = {"PENDING", "IN_PROGRESS", "COMPLETED", "FAILED", "SKIPPED"}
MAX_ACTIVE_PLANS = 10
# 以下为模块级回退默认值（settings 未定义时使用）；Phase 0 max 审查修复后
# 实际判定统一走 settings（AGENT_* / TOT_NUM_CANDIDATES），环境变量可调。
REFLECT_FAIL_THRESHOLD = 2      # 同一工具连续失败次数触发反思
COMPLEXITY_PLAN_THRESHOLD = 0.5
COMPLEXITY_TOT_THRESHOLD = 0.7  # 多路径探索门槛（成本高，仅高复杂度启用）
TOT_NUM_CANDIDATES = 3          # 候选方案数上限（成本有界）


def _threshold(name: str, fallback):
    """读配置阈值（settings 定义优先，回退模块字面量）。"""
    from django.conf import settings
    try:
        return getattr(settings, name, fallback)
    except Exception:  # noqa: BLE001 — settings 未就绪（如单测早期导入）
        return fallback


def reflect_fail_threshold() -> int:
    return int(_threshold("AGENT_REFLECTION_FAILURE_THRESHOLD", REFLECT_FAIL_THRESHOLD))


def complexity_plan_threshold() -> float:
    return float(_threshold("AGENT_PLANNING_COMPLEXITY_THRESHOLD", COMPLEXITY_PLAN_THRESHOLD))


def complexity_tot_threshold() -> float:
    return float(_threshold("AGENT_EXPLORATION_THRESHOLD", COMPLEXITY_TOT_THRESHOLD))


def tot_num_candidates() -> int:
    return int(_threshold("TOT_NUM_CANDIDATES", TOT_NUM_CANDIDATES))

PLAN_SYSTEM = (
    "你是短剧创作助手 planner。将用户的创作目标分解为 3-7 个可执行子任务，"
    "输出纯 JSON 数组，每项 {\"id\": 序号, \"description\": \"子任务描述\", "
    "\"depends_on\": [前置id...]}。只输出 JSON，不要其他文字。"
)

PLAN_PROMPT = """目标：{goal}

已知信息：{context}

可用工具：{tools}

请分解为 3-7 个顺序/依赖明确的子任务，输出 JSON 数组：
[{{"id": 1, "description": "...", "depends_on": []}}, ...]"""

REFLECT_SYSTEM = "你是失败分析器：分析创作助手工具调用失败的根因并给出策略调整建议，输出纯 JSON。"

REFLECT_PROMPT = """以下工具调用连续失败，请分析并输出 JSON：
{{
  "root_cause": "失败根本原因（一句话）",
  "lessons_learned": "经验教训（一句话）",
  "strategy_adjustment": {{"avoid": "应避免的做法", "prefer": "建议的做法"}}
}}

工具：{tool_name}
近期失败：
{failures}

当前计划：{plan}"""


# ─── 多路径探索（Tree of Thoughts，复杂度>0.7 且开关启用时）─────────────────

TOT_SYSTEM = (
    "你是短剧创作策略生成器：针对关键决策生成多个差异化候选方案并自评分，"
    "输出纯 JSON 对象，不要其他文字。"
)

TOT_PROMPT = """创作决策：{decision}

已知信息：{context}

请生成 {n} 个差异化候选方案（策略方向要彼此不同），并对每个方案自评其预期效果。
输出 JSON 对象：
{{
  "candidates": [
    {{"id": "A", "description": "方案描述", "rationale": "为何可行", "score": 0.0-1.0 的预期效果分}},
    ...
  ]
}}
score 越高越好（综合可行性、对创作目标的贡献、风险）。只输出 JSON。"""


@dataclass
class ToTCandidate:
    """单个候选方案。"""
    cid: str
    description: str
    rationale: str = ""
    score: float = 0.0

    def to_dict(self) -> dict:
        return {"id": self.cid, "description": self.description,
                "rationale": self.rationale, "score": round(self.score, 3)}


@dataclass
class ToTResult:
    """多路径探索结果：候选集 + 选中的最佳方案。"""
    decision: str
    candidates: list  # list[ToTCandidate]
    best: Optional[ToTCandidate]
    complexity: float = 0.0

    def to_dict(self) -> dict:
        return {
            "decision": self.decision,
            "candidates": [c.to_dict() for c in self.candidates],
            "best_id": self.best.cid if self.best else None,
            "complexity": round(self.complexity, 3),
        }


# ─── 复杂度评估 ─────────────────────────────────────────────────────────

# 多目标信号词：每命中一类 +0.15
_MULTI_GOAL = ["并", "和", "以及", "同时", "优化", "修改.*重写|重写.*修改", "全部", "每一集|各集"]
# 高复杂度领域词：涉及结构性大改
_HEAVY = ["节奏", "结构", "大纲", "全剧", "多集", "重排", "反转设计", "人物弧光", "重写"]
# 目标对象数量（第N集、场景、镜头编号出现次数越多越复杂）
_TARGET_RE = re.compile(r"第\s*\d+\s*[集场]|场景\s*\d+|镜头\s*\d+|sc\d+|shot\d+", re.I)


def estimate_task_complexity(text: str, context_entries: int = 0) -> float:
    """启发式任务复杂度评分（0-1）。

    因子：文本长度、多目标信号、重领域词、目标数量、上下文规模。
    """
    if not text:
        return 0.0
    score = 0.0
    n = len(text)
    # 长度：>200字明显复杂
    score += min(0.2, n / 1000)
    # 多目标信号
    hits = sum(1 for p in _MULTI_GOAL if re.search(p, text))
    score += min(0.35, hits * 0.15)
    # 重领域词
    heavy = sum(1 for w in _HEAVY if w in text)
    score += min(0.3, heavy * 0.15)
    # 目标数量：>=3 个编号目标说明批量操作
    targets = len(set(_TARGET_RE.findall(text)))
    score += min(0.15, targets * 0.05)
    # 长会话上下文略增复杂度（信息整合成本）
    if context_entries > 20:
        score += 0.05
    return round(min(1.0, score), 3)


# ─── 规划引擎 ───────────────────────────────────────────────────────────

class PlanningEngine:
    """Plan-and-Solve 规划器。"""

    def __init__(self, session: AssistantSession, planner=None, task_record=None):
        self.session = session
        self.task_record = task_record  # 辅助 LLM 调用归因到本轮 TaskRecord（入账/预算）
        # planner(goal_text, context, tools) -> list[step-dict]；None 走 LLM→启发式
        self.planner = planner or (lambda g, c, t: llm_plan(session, g, c, t, task_record=task_record))

    # ── 创建 ──────────────────────────────────────────────────────────

    def create_plan(self, goal: str, context: str = "", tools: Optional[list] = None) -> Optional[Plan]:
        """分解目标为 3-7 步并落库。每会话最多 MAX_ACTIVE_PLANS 个活跃计划。"""
        if Plan.objects.filter(session=self.session, status=PlanStatus.ACTIVE).count() >= MAX_ACTIVE_PLANS:
            logger.warning("会话 %s 活跃计划已达上限", self.session.pk)
            return None
        try:
            steps = self.planner(goal, context, tools or [])
        except Exception as exc:  # noqa: BLE001 — LLM失败降级模板
            logger.warning("LLM规划失败，降级启发式: %s", exc)
            steps = []
        steps = _normalize_steps(steps) or _heuristic_steps(goal)
        plan = Plan.objects.create(session=self.session, goal=goal[:2000], steps=steps)
        return plan

    # ── 进度 ──────────────────────────────────────────────────────────

    @staticmethod
    def active_plan(session: AssistantSession) -> Optional[Plan]:
        return Plan.objects.filter(session=session, status=PlanStatus.ACTIVE).first()

    @staticmethod
    def mark_step(plan: Plan, step_id: int, status: str, note: str = "") -> bool:
        """更新单步状态（IN_PROGRESS/COMPLETED/FAILED/SKIPPED）。返回是否变更。

        note 即使状态未变也会写入（如失败记 note 但保持 IN_PROGRESS）。
        防倒退：COMPLETED/SKIPPED 为终态，不再被 IN_PROGRESS/PENDING 类推进改写
        （历史脏数据含重复 id 时 mark_step 命中首个匹配，须防止状态来回抖动）。
        phase1 修复 4.2：note-only 写入不刷新 updated_at——否则每次工具失败
        都写 note，重试同一坏工具的 agent 无限刷新自己的停滞时钟，
        放弃/重建路径永不触发。"""
        if status not in STEP_STATUSES - {"PENDING"}:
            return False
        _TERMINAL = {"COMPLETED", "SKIPPED"}
        changed = False
        note_changed = False
        for s in plan.steps:
            if s.get("id") == step_id:
                if s.get("status") != status:
                    if s.get("status") in _TERMINAL and status not in _TERMINAL:
                        break  # 终态步骤不再回退（重复id脏数据的防抖动保护）
                    s["status"] = status
                    changed = True
                if note and s.get("note") != note:
                    s["note"] = note[:500]
                    note_changed = True
                break
        if changed:
            Plan.objects.filter(pk=plan.pk).update(steps=plan.steps,
                                                    updated_at=timezone.now())
            plan.refresh_from_db(fields=["steps"])
        elif note_changed:
            # 仅 note 变化：落库步骤但不刷新 updated_at（停滞时钟不被失败重试自刷）
            Plan.objects.filter(pk=plan.pk).update(steps=plan.steps)
            plan.refresh_from_db(fields=["steps"])
        return changed or note_changed

    @staticmethod
    def finish_plan(plan: Plan, status: str) -> Plan:
        """计划收尾：COMPLETED / FAILED / ABANDONED。"""
        if status not in (PlanStatus.COMPLETED, PlanStatus.FAILED, PlanStatus.ABANDONED):
            raise ValueError(f"非法终态 {status}")
        plan.status = status
        plan.save(update_fields=["status", "updated_at"])
        return plan

    # ── 动态更新 ──────────────────────────────────────────────────────

    @staticmethod
    def should_update_plan(plan: Plan, blocked: bool = False) -> bool:
        """触发修订：完成过半、被阻塞、或出现失败步骤。"""
        if plan.status != PlanStatus.ACTIVE or not plan.steps:
            return False
        if blocked:
            return True
        statuses = [s.get("status") for s in plan.steps]
        if "FAILED" in statuses:
            return True
        done = sum(1 for st in statuses if st == "COMPLETED")
        return done >= len(statuses) / 2 and done < len(statuses)

    def update_plan(self, plan: Plan, reason: str = "",
                    new_steps: Optional[list] = None,
                    updater=None) -> Plan:
        """修订计划：保存 PlanRevision + 更新 steps。

        updater(new_steps_input) -> list：默认启发式保留已完成步骤、重置 FAILED→PENDING。
        """
        old_steps = [dict(s) for s in plan.steps]
        if new_steps is None:
            if updater:
                try:
                    new_steps = updater(old_steps)
                except Exception:  # noqa: BLE001
                    new_steps = None
            if new_steps is None:
                new_steps = _revise_steps(old_steps)
        norm_old = _normalize_steps(old_steps)
        new_steps = _normalize_steps(new_steps) or norm_old
        if new_steps != norm_old:
            PlanRevision.objects.create(plan=plan, revision=new_steps, reason=reason[:2000])
            Plan.objects.filter(pk=plan.pk).update(steps=new_steps,
                                                   updated_at=timezone.now())
            plan.refresh_from_db(fields=["steps"])
        return plan


def _normalize_steps(raw) -> list:
    """把 LLM/外部输出规整为 [{id, description, depends_on, status}]，取前 7 步。

    对畸形输出健壮（max 审查确认缺陷）：depends_on 缺失/非列表/含非法项按无依赖
    或逐项忽略；id 非数字按出现顺序补齐；单步解析异常不使整份计划失败。
    phase1 修复 4.4：重复 id 改为分配「计划内最小未占用正整数」（旧实现
    +100 会与既有 id 撞车），并维护 id 映射重写 depends_on（依赖跟随原步骤
    而非指向被重编号的步骤）；指向不存在 id 的依赖被丢弃。
    """
    if not isinstance(raw, list):
        return []
    # 第一遍：解析出原始 id 与步骤（重复 id 保留原始值待映射）
    parsed = []
    for i, item in enumerate(raw[:7]):
        if not isinstance(item, dict) or not item.get("description"):
            continue
        try:
            sid = int(item.get("id"))
        except (TypeError, ValueError):
            sid = i + 1
        # depends_on：非列表（含单个数字）→ 尝试取单值；逐项只留可解析数字
        deps_raw = item.get("depends_on") or []
        if not isinstance(deps_raw, (list, tuple)):
            deps_raw = [deps_raw]
        deps = []
        for d in deps_raw:
            try:
                deps.append(int(d))
            except (TypeError, ValueError):
                continue
        status = item.get("status") if item.get("status") in STEP_STATUSES else "PENDING"
        parsed.append({
            "orig_id": sid,
            "description": str(item["description"])[:500],
            "depends_on": deps,
            "status": status,
            **({"note": str(item["note"])[:500]} if item.get("note") else {}),
        })
    # 第二遍：分配唯一 id（最小未占用正整数），建映射并重写依赖
    used = set()
    id_map = {}  # (orig_id, occurrence) -> new_id
    occurrences: dict[int, int] = {}
    for p in parsed:
        oid = p["orig_id"]
        occurrences[oid] = occurrences.get(oid, 0) + 1
        key = (oid, occurrences[oid])
        if oid not in used and occurrences[oid] == 1:
            new_id = oid
        else:
            candidate = 1
            while candidate in used:
                candidate += 1
            new_id = candidate
        used.add(new_id)
        id_map[key] = new_id
        p["new_id"] = new_id
    # 每个原 id 的首次出现保留原值（若有冲突，后出现者已被重分配）；
    # 依赖按原 id 解析到「该原 id 的首个步骤」（保持依赖指向原步骤的语义）
    first_by_orig: dict[int, int] = {}
    for p in parsed:
        first_by_orig.setdefault(p["orig_id"], p["new_id"])
    out = []
    for p in parsed:
        deps = []
        for d in p["depends_on"]:
            target = first_by_orig.get(d)
            if target is not None and target != p["new_id"]:
                deps.append(target)
        out.append({
            "id": p["new_id"],
            "description": p["description"],
            "depends_on": deps,
            "status": p["status"],
            **({"note": p["note"]} if "note" in p else {}),
        })
    return out if len(out) >= 1 else []


def _heuristic_steps(goal: str) -> list:
    """无 LLM 时的兜底四步模板。"""
    return [
        {"id": 1, "description": f"收集信息：获取与「{goal[:60]}」相关的分集/场景/角色现状", "depends_on": [], "status": "PENDING"},
        {"id": 2, "description": "分析问题：定位需要修改的具体内容与原因", "depends_on": [1], "status": "PENDING"},
        {"id": 3, "description": "提出修改：用 propose_* 工具生成提案（等待用户确认）", "depends_on": [2], "status": "PENDING"},
        {"id": 4, "description": "复核结果：检查修改后的结构/节奏/一致性", "depends_on": [3], "status": "PENDING"},
    ]


def _revise_steps(old_steps: list) -> list:
    """默认修订：FAILED→PENDING（附重试标记），IN_PROGRESS 保留，其余保留。

    无失败步骤时原样返回（避免规范化噪声误触发 revision）。
    IN_PROGRESS 不回退：否则刚被只读工具推进的步骤会被随后的修订重置回
    PENDING，步骤永远无法 COMPLETED（max 审查确认缺陷）。
    """
    if not any(s.get("status") == "FAILED" for s in old_steps):
        return old_steps
    out = []
    for s in old_steps:
        s = dict(s)
        if s.get("status") == "FAILED":
            s["status"] = "PENDING"
            s["note"] = "失败后重置重试"
        out.append(s)
    return out


# ─── LLM 规划 ───────────────────────────────────────────────────────────

def llm_plan(session: AssistantSession, goal: str, context: str, tools: list,
             task_record=None) -> list:
    """经 AIGateway 生成结构化步骤。task_record：归因费用到本轮记录（入账/预算）。"""
    from apps.ai_gateway.gateway import AIGateway

    tools_text = ", ".join(t.get("name", "") for t in tools)[:500] or "通用创作工具"
    resp = AIGateway().chat(
        session.agent_config,
        messages=[{"role": "user", "content": [{"type": "text", "text": PLAN_PROMPT.format(
            goal=goal, context=context or "（无）", tools=tools_text)}]}],
        system=PLAN_SYSTEM,
        play=session.play,
        task_record=task_record,
    )
    return extract_json_array(chat_text(resp))


def _extract_json_array(text: str) -> list:  # 兼容旧调用名（tests 引用）
    return extract_json_array(text)


# ─── Reflexion 反思 ─────────────────────────────────────────────────────

@dataclass
class Reflection:
    tool_name: str
    root_cause: str
    lessons_learned: str
    strategy_adjustment: dict
    fail_count: int

    def to_dict(self) -> dict:
        return {
            "tool": self.tool_name,
            "root_cause": self.root_cause,
            "lessons_learned": self.lessons_learned,
            "strategy_adjustment": self.strategy_adjustment,
            "fail_count": self.fail_count,
        }


class FailureTracker:
    """跟踪每轮内各工具连续失败次数（runner 每轮新建）。"""

    def __init__(self):
        self._fails: dict[str, int] = {}

    def record(self, tool_name: str, is_error: bool) -> int:
        """记录一次工具结果，返回该工具当前连续失败数。"""
        if is_error:
            self._fails[tool_name] = self._fails.get(tool_name, 0) + 1
        else:
            self._fails[tool_name] = 0
        return self._fails.get(tool_name, 0)

    def get(self, tool_name: str) -> int:
        """该工具当前连续失败数（供外部查询，不暴露内部dict）。"""
        return self._fails.get(tool_name, 0)

    def worst(self) -> tuple[Optional[str], int]:
        if not self._fails:
            return None, 0
        name = max(self._fails, key=self._fails.get)
        return name, self._fails[name]


def reflect_on_failure(session: AssistantSession, tool_name: str,
                       failures: list, plan: Optional[Plan] = None,
                       reflector=None, tree=None, task_record=None) -> Reflection:
    """生成反思并写入会话树（type=reflection）。

    reflector(prompt_vars) -> dict；None 走 LLM→启发式。
    tree：调用方持有的本轮会话树；缺省独立加载并立即保存。
    task_record：辅助 LLM 调用归因费用到本轮记录（入账/预算）。
    """
    fail_count = len(failures)
    plan_text = json.dumps(plan.steps, ensure_ascii=False)[:800] if plan else "（无计划）"
    failures_text = json.dumps(failures[-5:], ensure_ascii=False, default=str)[:2000]

    if reflector:
        try:
            data = reflector(tool_name=tool_name, failures=failures_text, plan=plan_text) or {}
        except Exception:  # noqa: BLE001
            data = {}
    else:
        try:
            data = llm_reflect(session, tool_name, failures_text, plan_text,
                               task_record=task_record)
        except Exception as exc:  # noqa: BLE001 — 反思失败降级启发式（phase1 修复 6.2 补日志）
            logger.warning("LLM反思失败，降级启发式 tool=%s: %s", tool_name, exc)
            data = {}

    reflection = Reflection(
        tool_name=tool_name,
        root_cause=str(data.get("root_cause") or _heuristic_root_cause(failures)),
        lessons_learned=str(data.get("lessons_learned") or "失败后先核对参数与目标对象是否存在，再重试"),
        strategy_adjustment=data.get("strategy_adjustment") if isinstance(
            data.get("strategy_adjustment"), dict) else {
            "avoid": f"不要连续重复调用 {tool_name} 且参数不变",
            "prefer": "先用只读工具确认对象状态，再调整参数重试",
        },
        fail_count=fail_count,
    )
    _append_reflection_entry(session, reflection, tree=tree)
    return reflection


def _heuristic_root_cause(failures: list) -> str:
    text = json.dumps(failures, ensure_ascii=False, default=str)
    if "不存在" in text or "未找到" in text or "not found" in text.lower():
        return "目标对象不存在或ID错误"
    if "参数" in text or "validation" in text.lower() or "schema" in text.lower():
        return "参数格式或校验错误"
    if "超时" in text or "timeout" in text.lower():
        return "调用超时（服务不可用或负载过高）"
    return "工具执行失败（原因待查）"


def _append_tree_entry(session: AssistantSession, entry_type: str, content: dict,
                       tree=None, what: str = "条目"):
    """辅助条目写入会话树（树文件缺失时静默跳过，不影响主流程）。

    tree：调用方持有的本轮树——必须传入，否则独立加载的树会丢掉本轮未落盘的条目，
    且随后被 runner 的迭代末保存覆盖（仅独立加载的树会在此保存）。
    what：日志用的条目中文名。"""
    try:
        from .session_tree import SessionStorage, SessionTreeManager
        if tree is None:
            tree = SessionTreeManager.get_or_create(session)
            tree.append(entry_type, content)
            SessionStorage.save(tree)
        else:
            tree.append(entry_type, content)
    except Exception:  # noqa: BLE001
        logger.exception("%s写入会话树失败 session=%s", what, session.pk)


def _append_reflection_entry(session: AssistantSession, reflection: Reflection, tree=None):
    """反思写入会话树（type=reflection）。"""
    _append_tree_entry(session, "reflection", reflection.to_dict(), tree, "反思")


def llm_reflect(session: AssistantSession, tool_name: str,
                failures_text: str, plan_text: str, task_record=None) -> dict:
    """经 AIGateway 生成反思 JSON。task_record：归因费用到本轮记录。"""
    from apps.ai_gateway.gateway import AIGateway

    resp = AIGateway().chat(
        session.agent_config,
        messages=[{"role": "user", "content": [{"type": "text", "text": REFLECT_PROMPT.format(
            tool_name=tool_name, failures=failures_text, plan=plan_text)}]}],
        system=REFLECT_SYSTEM,
        play=session.play,
        task_record=task_record,
    )
    return extract_json_object(chat_text(resp))


# ─── 多路径探索（Tree of Thoughts）─────────────────────────────────────────

def _normalize_candidates(raw, n: int) -> list:
    """把 LLM 输出规整为 list[ToTCandidate]，取前 n 个、分数截断到 [0,1]。"""
    out = []
    if not isinstance(raw, dict):
        return out
    items = raw.get("candidates")
    if not isinstance(items, list):
        return out
    for i, item in enumerate(items[:n]):
        if not isinstance(item, dict) or not item.get("description"):
            continue
        try:
            score = float(item.get("score", 0.0))
        except (TypeError, ValueError):
            score = 0.0
        out.append(ToTCandidate(
            cid=str(item.get("id") or chr(ord("A") + i)),
            description=str(item["description"])[:500],
            rationale=str(item.get("rationale", ""))[:500],
            score=max(0.0, min(1.0, score)),
        ))
    return out


def _heuristic_candidates(decision: str, n: int) -> list:
    """无 LLM/降级兜底：三个通用差异化方向（保守/常规/激进），均分。"""
    templates = [
        ("保守", "在现有结构上做最小改动，先补齐明确缺口，风险最低"),
        ("常规", "按主流短剧节奏推进，平衡铺垫与冲突"),
        ("激进", "引入更强反转/视角切换，冲击力大但需用户确认"),
    ]
    return [ToTCandidate(cid=chr(ord("A") + i),
                         description=f"【{tag}】{decision[:60]}：{desc}",
                         rationale=desc, score=0.5)
            for i, (tag, desc) in enumerate(templates[:n])]


def llm_tot(session: AssistantSession, decision: str, context: str, n: int,
            task_record=None) -> list:
    """经 AIGateway 生成 n 个候选方案。task_record：归因费用到本轮记录（入账/预算）。"""
    from apps.ai_gateway.gateway import AIGateway

    resp = AIGateway().chat(
        session.agent_config,
        messages=[{"role": "user", "content": [{"type": "text", "text": TOT_PROMPT.format(
            decision=decision, context=context or "（无）", n=n)}]}],
        system=TOT_SYSTEM,
        play=session.play,
        task_record=task_record,
    )
    return _normalize_candidates(extract_json_object(chat_text(resp)), n)


def tot_explore(session: AssistantSession, decision: str, context: str = "",
                complexity: float = 0.0, task_record=None,
                generator=None) -> ToTResult:
    """多路径探索：生成 n 个候选 → 选最高分 → 返回全部候选（落树作备选）。

    generator(decision, context, n) -> list[ToTCandidate]；None 走 LLM→启发式降级。
    候选为空时兜底启发式。成本有界：候选数上限 TOT_NUM_CANDIDATES。
    """
    from django.conf import settings
    n = min(tot_num_candidates(), TOT_NUM_CANDIDATES)  # 配置化，且永不高于硬上限
    candidates = []
    if generator:
        try:
            candidates = generator(decision, context, n)
        except Exception:  # noqa: BLE001
            candidates = []
    else:
        try:
            candidates = llm_tot(session, decision, context, n, task_record=task_record)
        except Exception as exc:  # noqa: BLE001 — LLM失败降级启发式
            logger.warning("ToT LLM失败，降级启发式: %s", exc)
    candidates = candidates or _heuristic_candidates(decision, n)
    best = max(candidates, key=lambda c: c.score)
    return ToTResult(decision=decision, candidates=candidates, best=best,
                     complexity=complexity)


def maybe_tot(session: AssistantSession, user_text: str, tools: list,
              task_record=None, complexity: Optional[float] = None) -> Optional[ToTResult]:
    """复杂度 > ToT 门槛且开关启用时做一次多路径探索（feature flag 控制）。

    task_record：辅助 LLM 调用归因费用到本轮记录（入账/预算）。
    complexity：调用方已算好的复杂度（避免重复评估）；None 时内部现算。
    返回 ToTResult 并写会话树 tot 条目（供前端展示备选）；不触发/异常返回 None。
    """
    from django.conf import settings
    if not getattr(settings, "ENABLE_PLANNING_TOT", False):
        return None
    if not getattr(settings, "ENABLE_PLANNING", False):
        return None
    try:
        if complexity is None:
            complexity = estimate_task_complexity(
                user_text, context_entries=session.messages.count())
        if complexity <= complexity_tot_threshold():
            return None
        result = tot_explore(session, decision=user_text, complexity=complexity,
                             task_record=task_record)
        _append_tot_entry(session, result)
        if result.best:
            logger.info("ToT session=%s complexity=%.2f best=%s(score=%.2f)",
                        session.pk, complexity, result.best.cid, result.best.score)
        return result
    except Exception:  # noqa: BLE001
        logger.exception("多路径探索失败 session=%s", session.pk)
        return None


def _append_tot_entry(session: AssistantSession, result: ToTResult, tree=None):
    """ToT 结果写入会话树（type=tot）。"""
    _append_tree_entry(session, "tot", result.to_dict(), tree, "ToT")


# ─── runner 集成 ───────────────────────────────────────────────────────

def maybe_plan(session: AssistantSession, user_text: str, tools: list,
               task_record=None, complexity: Optional[float] = None) -> Optional[Plan]:
    """复杂度 > 阈值且无活跃计划时自动规划（feature flag 控制）。

    task_record：辅助 LLM 调用归因费用到本轮记录（入账/预算）。
    complexity：调用方已算好的复杂度（避免重复评估）；None 时内部现算。
    返回创建的 Plan；不触发返回 None。任何内部异常都不影响主流程。
    """
    from django.conf import settings
    if not getattr(settings, "ENABLE_PLANNING", False):
        return None
    try:
        if complexity is None:
            complexity = estimate_task_complexity(
                user_text, context_entries=session.messages.count())
        if complexity <= complexity_plan_threshold():
            return None
        active = PlanningEngine.active_plan(session)
        if active is not None:
            # 活跃计划长期无进度（步骤推进被修订节流/依赖卡死）时兜底放弃，
            # 避免旧计划永远 ACTIVE 且抑制该会话后续所有新计划（max 审查确认缺陷）。
            if _plan_stalled(active):
                PlanningEngine.finish_plan(active, PlanStatus.ABANDONED)
                events.emit(session.pk, "plan_abandoned", {
                    "plan_id": active.pk,
                    "reason": "活跃计划长期无进度，自动放弃以便新目标重建计划",
                })
                active = None
            else:
                return None  # 已有活跃计划，不重复创建
        plan = PlanningEngine(session, task_record=task_record).create_plan(
            goal=user_text, tools=tools)
        if plan:
            logger.info("自动规划 session=%s complexity=%.2f steps=%d",
                        session.pk, complexity, len(plan.steps))
            plan._complexity = complexity  # 透出给调用方发事件
        return plan
    except Exception:  # noqa: BLE001
        logger.exception("自动规划失败 session=%s", session.pk)
        return None


# 兜底阈值：活跃计划超过此轮数无状态推进视为停滞（设计 Open Question 的默认值，
# 可经 AGENT_PLAN_STALL_TURNS 调整）
PLAN_STALL_TURNS = 50


def _plan_stalled(plan: Plan, now=None) -> bool:
    """活跃计划是否长期无进度：updated_at 距今超过 PLAN_STALL_TURNS 轮仍未推进。

    Plan.updated_at 在任何 mark_step/update_plan 落库时刷新，因此长时间不变
    即代表该计划无进展。
    phase1 修复 4.3：配置 0 或负值表示禁用（旧实现 `or PLAN_STALL_TURNS`
    把 0 变成 50，`turns<=0` 分支不可达）。"""
    from django.conf import settings
    raw = getattr(settings, "AGENT_PLAN_STALL_TURNS", None)
    if raw is None:
        turns = PLAN_STALL_TURNS
    else:
        try:
            turns = int(raw)
        except (TypeError, ValueError):
            turns = PLAN_STALL_TURNS
    if turns <= 0:
        return False
    now = now or timezone.now()
    updated = plan.updated_at
    if not updated:
        return False
    return (now - updated) > timedelta(minutes=turns)


# ─── 步骤推进（runner 每次工具调用后驱动，Phase A）───────────────────────

# 只读工具：信息收集，推进首个可执行步骤 PENDING→IN_PROGRESS
_ADV_READ_TOOLS = {"get_play_overview", "get_outline", "get_episode_script", "get_scene",
                   "get_shot", "get_characters", "get_task_status", "get_cost_summary",
                   "search_asset_library", "memory_read", "review_episode",
                   "list_skills", "load_skill"}
# 提案/写工具：产出即步骤交付物，成功→当前 IN_PROGRESS 步骤 COMPLETED
_ADV_WRITE_TOOLS = {"propose_outline_edit", "propose_episode_script_edit", "propose_scene_edit",
                    "propose_shot_edit", "propose_shot_insert", "propose_shot_delete",
                    "propose_scene_create", "propose_character_edit", "propose_character_create",
                    "request_generation", "memory_save"}


def _first_runnable_step(steps: list) -> Optional[dict]:
    """首个 PENDING 且依赖已满足（全部 COMPLETED）的步骤。"""
    status_by_id = {s.get("id"): s.get("status") for s in steps}
    for s in steps:
        if s.get("status") != "PENDING":
            continue
        deps = s.get("depends_on") or []
        if all(status_by_id.get(d) == "COMPLETED" for d in deps):
            return s
    return None


def _current_in_progress(steps: list) -> Optional[dict]:
    """当前 IN_PROGRESS 的步骤（取首个）。"""
    return next((s for s in steps if s.get("status") == "IN_PROGRESS"), None)


def advance_plan_for_tool(session: AssistantSession, tool_name: str, is_error: bool):
    """runner 每次工具调用后驱动计划步骤推进（启发式、保守、失败静默）。

    映射：
    - 只读工具 → 首个可执行（PENDING+依赖满足）步骤置 IN_PROGRESS
    - 提案/写工具成功 → 当前 IN_PROGRESS 步骤置 COMPLETED
    - 工具失败 → 当前 IN_PROGRESS 步骤记 note（不轻易置 FAILED，避免噪声）

    推进后：should_update_plan→update_plan 修订；全部终态→finish_plan 收尾；
    状态变化→emit plan_progress 事件。任何异常不影响主轮次（调用方也兜底）。
    返回变更后的 Plan（无计划/未变更返回 None）。
    """
    from django.conf import settings
    if not getattr(settings, "ENABLE_PLANNING", False):
        return None
    plan = PlanningEngine.active_plan(session)
    if plan is None or plan.status != PlanStatus.ACTIVE:
        return None

    engine = PlanningEngine(session)
    changed = False
    if is_error:
        cur = _current_in_progress(plan.steps)
        if cur is not None:
            changed = engine.mark_step(plan, cur["id"], "IN_PROGRESS",
                                       note=f"工具 {tool_name} 执行失败，待重试或调整")
    elif tool_name in _ADV_READ_TOOLS:
        step = _first_runnable_step(plan.steps)
        if step is not None:
            changed = engine.mark_step(plan, step["id"], "IN_PROGRESS")
    elif tool_name in _ADV_WRITE_TOOLS:
        cur = _current_in_progress(plan.steps)
        if cur is not None:
            changed = engine.mark_step(plan, cur["id"], "COMPLETED")
        else:
            # phase1 修复 4.1：写优先的计划（首个动作即 propose_*/request_generation，
            # 无前置只读工具）永远没有 IN_PROGRESS 步骤可完成 → 进度恒 0%。
            # 无 IN_PROGRESS 时把首个可执行步骤直接置 COMPLETED（带 note 说明）。
            step = _first_runnable_step(plan.steps)
            if step is not None:
                changed = engine.mark_step(
                    plan, step["id"], "COMPLETED",
                    note=f"写工具 {tool_name} 已产出该步骤交付物，直接完成")

    # 修订检查每次工具后都跑（should_update_plan 廉价；完成过半/出现 FAILED 即触发），
    # 不与 changed 门控——否则计划停滞在 FAILED/过半状态时永远得不到修订。
    revised = _maybe_revise(engine, plan)
    _maybe_finish(session, plan)

    if not (changed or revised):
        return None
    events.emit(session.pk, "plan_progress", {
        "plan_id": plan.pk, "steps": plan.steps, "progress": plan.get_progress(),
    })
    return plan


def _maybe_revise(engine: PlanningEngine, plan: Plan) -> bool:
    """完成过半/出现失败步骤时触发一次修订（复用已有 PlanRevision 机制）。

    返回是否实际产生了修订（update_plan 仅在步骤变化时创建 PlanRevision）。"""
    try:
        if engine.should_update_plan(plan):
            before = [dict(s) for s in plan.steps]
            engine.update_plan(plan, reason="执行中动态修订（完成过半或出现失败步骤）")
            return [dict(s) for s in plan.steps] != before
    except Exception:  # noqa: BLE001
        logger.exception("计划修订失败 plan=%s", plan.pk)
    return False


def _maybe_finish(session: AssistantSession, plan: Plan):
    """全部步骤终态时收尾计划：有 FAILED→FAILED，否则 COMPLETED。"""
    statuses = [s.get("status") for s in plan.steps]
    if not statuses or any(st in ("PENDING", "IN_PROGRESS") for st in statuses):
        return
    final = PlanStatus.FAILED if "FAILED" in statuses else PlanStatus.COMPLETED
    if plan.status == PlanStatus.ACTIVE:
        PlanningEngine.finish_plan(plan, final)
        events.emit(session.pk, "plan_finished", {"plan_id": plan.pk, "status": final})
