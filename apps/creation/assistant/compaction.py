"""上下文压缩（Phase 1.3）：学习Pi Agent的压缩机制。

设计对齐Pi Agent：
- 自动压缩：接近上下文限制时触发（context_tokens > context_window - reserve_tokens）
- 手动压缩：用户可主动触发（/compact命令，支持聚焦指令）
- 可配置策略：reserve_tokens、keep_recent_tokens、auto_compact、context_window
- 保留原始条目：压缩只影响发送给LLM的内容，原始条目保留在会话树中
- 迭代压缩：多次压缩时传递上一个摘要作为上下文，切割点从上次的 first_kept 位置起算
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Optional

from .llm_text import chat_text
from .session_tree import SessionEntry, SessionTree, SessionTreeManager

logger = logging.getLogger(__name__)

FEATURE_FLAG = "ENABLE_CONTEXT_COMPACTION"


# ─── 常量 ───────────────────────────────────────────────────────────────────

DEFAULT_RESERVE_TOKENS = 16384    # 保留给模型响应的token数
DEFAULT_KEEP_RECENT_TOKENS = 20000  # 压缩后保留的最近token数
DEFAULT_AUTO_COMPACT = True       # 是否自动压缩
DEFAULT_CONTEXT_WINDOW = 200000   # 默认上下文窗口

SUMMARY_SYSTEM = "你是对话历史压缩器，将创作助手会话历史压缩为结构化中文摘要，保留全部关键信息但大幅缩减篇幅。"

# 压缩提示模板（迭代压缩时携带上一个摘要与可选聚焦指令）
COMPACTION_PROMPT = """请将以下对话历史压缩为结构化摘要，保留关键信息但大幅减少token数。

上一个摘要（接续它，不要重复已覆盖的内容）：
{previous_summary}

{focus_section}对话历史：
{history}

请生成结构化摘要，包含以下部分：

## 对话概要
[一段简洁的对话概要]

## 关键决策
- [列出已做出的关键决策]

## 重要信息
- [列出需要保留的重要信息，如角色设定、创作约定、已确认的提案等]

## 待完成任务
- [列出尚未完成的任务或计划]

## 上下文关键词
[列出对后续对话重要的关键词]

请确保摘要包含所有关键信息，但远小于原始内容。"""

FOCUS_SECTION = """用户聚焦指令（摘要须重点保留相关内容）：
{instructions}

"""

SUMMARY_PREFIX = "[已压缩的历史摘要]"


# ─── 数据结构 ───────────────────────────────────────────────────────────────

@dataclass
class CompactionEntry:
    """压缩结果（注意与 SessionEntry(type="compaction") 区分：这是返回值载体）。"""
    summary: str = ""
    first_kept_entry_id: Optional[str] = None
    tokens_before: int = 0
    compacted: bool = True
    details: Optional[dict] = None

    def to_dict(self) -> dict:
        return {
            "summary": self.summary,
            "first_kept_entry_id": self.first_kept_entry_id,
            "tokens_before": self.tokens_before,
            "compacted": self.compacted,
            "details": self.details or {},
        }


@dataclass
class CompactionConfig:
    """压缩配置。"""
    reserve_tokens: int = DEFAULT_RESERVE_TOKENS
    keep_recent_tokens: int = DEFAULT_KEEP_RECENT_TOKENS
    auto_compact: bool = DEFAULT_AUTO_COMPACT
    context_window: int = DEFAULT_CONTEXT_WINDOW
    summary_model: object = None  # AIModel pk：摘要走更便宜的小模型（缺省用 assistant 候选链）


def compaction_config(session=None) -> CompactionConfig:
    """压缩配置：会话 agent_config.assistant_compaction 覆盖默认值。"""
    cfg = CompactionConfig()
    extra = {}
    if session is not None and session.agent_config:
        extra = session.agent_config.assistant_compaction or {}
        if not extra:
            # 兼容旧数据：budget 内嵌 compaction
            extra = ((session.agent_config.assistant_budget or {}).get("compaction") or {})
    for k in ("reserve_tokens", "keep_recent_tokens", "auto_compact", "context_window",
              "summary_model"):
        if k in extra and extra[k] is not None:
            setattr(cfg, k, extra[k])
    return cfg


# ─── Token估算 ─────────────────────────────────────────────────────────────

def estimate_tokens(text: str) -> int:
    """估算文本的token数。

    中文剧本内容约 1 token/字（CJK 字符），ASCII 约 4 字符/token；
    按字符类型分段估算，比统一 len//4 在中文场景下准确 ~3-4×。"""
    if not text:
        return 1
    cjk = sum(1 for ch in text if ord(ch) > 0x2E7F)  # CJK 及全角字符区间起点
    return max(1, cjk + (len(text) - cjk) // 4)


def estimate_entry_tokens(entry: SessionEntry) -> int:
    """估算单个条目的token数。"""
    content_str = json.dumps(entry.content, ensure_ascii=False)
    return estimate_tokens(content_str)


def estimate_branch_tokens(branch: list[SessionEntry]) -> int:
    """估算分支的总token数。"""
    return sum(estimate_entry_tokens(e) for e in branch)


def _last_compaction(branch: list[SessionEntry]) -> Optional[SessionEntry]:
    """活动分支上最后一个压缩条目。"""
    for e in reversed(branch):
        if e.type == "compaction":
            return e
    return None


def effective_context_entries(branch: list[SessionEntry]) -> list[SessionEntry]:
    """压缩生效后的上下文条目：最后一个compaction条目 + 其first_kept之后的内容。"""
    last = _last_compaction(branch)
    if last is None:
        return [e for e in branch if e.type != "compaction"]
    first_kept_id = (last.content or {}).get("first_kept_entry_id")
    idx = next((i for i, e in enumerate(branch) if e.id == first_kept_id), None)
    if idx is None:
        # first_kept不在分支上（如导航切换），退化为仅摘要
        return [last]
    return [last] + [e for e in branch[idx:] if e.type != "compaction"]


def estimate_context_tokens(session_tree: SessionTree) -> int:
    """估算当前发送给LLM的上下文token数（已扣除被压缩部分）。"""
    return estimate_branch_tokens(effective_context_entries(session_tree.get_active_branch()))


# ─── 压缩触发检测 ─────────────────────────────────────────────────────────

def should_compact(session_tree: SessionTree, config: CompactionConfig) -> bool:
    """触发条件：context_tokens > context_window - reserve_tokens。"""
    if not config.auto_compact:
        return False
    return estimate_context_tokens(session_tree) > config.context_window - config.reserve_tokens


# ─── 压缩算法 ───────────────────────────────────────────────────────────

def _result_use_ids(entry: SessionEntry) -> list[str]:
    """从 tool_result 条目提取配对的 tool_use_id 列表。

    生产写入形状（runner._tree_sync → append_message）是
    content={"blocks": [{type:"tool_result", tool_use_id:...}, ...]}；
    旧数据可能把 tool_use_id 放在顶层 content。两者都取不到时返回 []。"""
    content = entry.content or {}
    ids = []
    if isinstance(content, dict):
        if content.get("tool_use_id"):
            ids.append(content["tool_use_id"])
        for b in content.get("blocks") or []:
            if isinstance(b, dict) and b.get("type") == "tool_result" and b.get("tool_use_id"):
                ids.append(b["tool_use_id"])
    return ids


def _tool_call_groups(entries: list[SessionEntry]) -> list[list[int]]:
    """把条目序列切分为不可分割的组（返回每组的条目索引列表）。

    tool_use 与其后所有条目直到配对的 tool_result（含中间的 reflection 等
    辅助条目）必须同生共死：切开任何一侧都会产生孤儿 tool_use/tool_result
    （LLM API 拒收）。因此组划分规则：
    - tool_result 与"其归属的"前序条目同组（从最近的 tool_use 起）
    - 其余条目（user_message / assistant_message / plan 等）各自成组

    tool_use_id 从 content["blocks"] 内的 tool_result 块提取（生产形状），
    兼容顶层旧形状（phase1 修复 1.4）；两者都取不到的 tool_result 并入
    紧邻前一条目的组（保守归组），不再使后续条目被永久粘合。"""
    groups: list[list[int]] = []
    open_use: dict[str, list[int]] = {}   # tool_use_id -> 正在累积的组
    i = 0
    n = len(entries)
    while i < n:
        e = entries[i]
        if e.type == "assistant_message":
            blocks = (e.content or {}).get("blocks") or []
            use_ids = [b.get("id") for b in blocks if isinstance(b, dict)
                       and b.get("type") == "tool_use"]
            if use_ids:
                # assistant(tool_use) 开启新组；后续条目直至所有 tool_result 配对完成
                group = [i]
                groups.append(group)
                for uid in use_ids:
                    open_use[uid] = group if len(use_ids) == 1 else group  # 多use同组
                i += 1
                continue
            groups.append([i])
            i += 1
            continue
        if e.type == "tool_result":
            tuids = _result_use_ids(e)
            matched = False
            for tuid in tuids:
                group = open_use.pop(tuid, None)
                if group is not None:
                    group.append(i)
                    matched = True
            if matched:
                i += 1
                continue
            # 无归属 tool_use（历史脏数据）：与紧邻前一条目保守同组
            if groups:
                groups[-1].append(i)
                i += 1
                continue
            groups.append([i])
            i += 1
            continue
        # reflection / plan / user_message 等：若存在未配对完成的组则并入（中间条目），
        # 否则独立成组
        if open_use:
            groups[-1].append(i) if groups else groups.append([i])
        else:
            groups.append([i])
        i += 1
    return groups


def _importance_protection_enabled() -> bool:
    """layered-memory D2 开关门控：关闭时压缩走纯位置语义（现状）。"""
    from django.conf import settings
    return bool(getattr(settings, "ENABLE_LAYERED_MEMORY", False))


def _protection_threshold() -> float:
    from django.conf import settings
    return float(getattr(settings, "MEMORY_IMPORTANT_THRESHOLD", 0.8))


def _group_max_importance(entries: list[SessionEntry], group: list[int]) -> float:
    """组内条目重要性最大值（layered-memory D1 分值表；纯位置路径不调用）。"""
    try:
        from .memory_layers import score_importance
        return max((score_importance(entries[j]) for j in group), default=0.0)
    except Exception:  # noqa: BLE001 — 评分不可用时按 0（即不保护）
        logger.exception("重要性评分不可用，按低分处理")
        return 0.0


class Compactor:
    """上下文压缩器。"""

    def __init__(self, config: Optional[CompactionConfig] = None,
                 summarizer=None):
        self.config = config or CompactionConfig()
        # summarizer(messages_text, previous_summary, instructions) -> str；None 用启发式兜底
        self.summarizer = summarizer or heuristic_summary
        # layered-memory D2：最近一次切割点选择中被保护的高分组数（可观测）
        self._last_protected_count = 0

    def find_cutoff_point(self, entries: list[SessionEntry]) -> Optional[int]:
        """从后往前按「不可分割组」累积 token 直到 keep_recent_tokens。

        返回合法边界索引（之前的被压缩）；返回 None 表示无可压缩内容
        （phase1 修复 1.3：旧实现用 0 兼作「无需压缩」与「压缩到头部」，
        单组超阈值时返回 0 被 compact() 当作无需压缩 → 永久压不了）。

        - 边界只能落在组边界上 → tool_use/tool_result（含中间 reflection）
          永远同组，不可能产生孤儿 tool_result；
        - 溢出组是最后一组时返回该组起始 → 该组之前的历史被压缩，
          超大组本身保留（压缩其之前历史而非拒绝压缩）；
        - 全部组累计仍 < keep_recent_tokens 时返回 None；
        - Phase 3.4（layered-memory D2）：重要性保护——重要性达到阈值的组
          不作为切割边界（边界前推，高分组划入保留区）；全部候选边界被保护时
          退回纯位置语义的切割点（兜底可压缩，防「全是大组」永久压不了，
          同 1.3 教训）。开关关闭时与纯位置语义逐项一致。
        """
        groups = _tool_call_groups(entries)
        if not groups:
            return None
        group_tokens = [sum(estimate_entry_tokens(entries[j]) for j in g) for g in groups]
        # 先按纯位置语义找到基准切割点（也作保护路径的兜底）
        position_cutoff = None
        accumulated = 0
        for gi in range(len(groups) - 1, -1, -1):
            accumulated += group_tokens[gi]
            if accumulated >= self.config.keep_recent_tokens:
                position_cutoff = (groups[gi + 1][0] if gi + 1 < len(groups)
                                    else groups[gi][0])
                break
        if position_cutoff is None:
            return None  # 全部都能保留

        if not _importance_protection_enabled():
            return position_cutoff
        # 重要性保护（D2）：摘要区（pool[:position_cutoff]）内的高分组划入保留区
        # ——边界前移（更小索引）到最早的高分组，该组及其后全部保留（宁可多保留）。
        # 边界推到 0（全部组高分）时兜底退纯位置语义：compact() 把 cutoff==0
        # 视为「无可压缩的更早历史」，不退就会永久压不了（phase1 1.3 教训）。
        threshold = _protection_threshold()
        protected_count = 0
        boundary = position_cutoff
        for gi in range(len(groups)):
            group_start = groups[gi][0]
            if group_start >= position_cutoff:
                break  # 已在保留区
            if _group_max_importance(entries, groups[gi]) >= threshold:
                protected_count += 1
                boundary = min(boundary, group_start)  # 最早的高分组起点
        self._last_protected_count = protected_count
        if protected_count and boundary > 0:
            return boundary
        # 无保护命中或推到 0（全组高分）→ 纯位置语义
        self._last_protected_count = 0 if boundary == 0 else protected_count
        return position_cutoff

    @staticmethod
    def _format_history(entries: list[SessionEntry]) -> str:
        lines = []
        for e in entries:
            content = e.content.get("blocks", e.content)
            text = json.dumps(content, ensure_ascii=False, default=str)
            lines.append(f"[{e.type} #{e.id[:8]}] {text}")
        return "\n".join(lines)

    def compact(self, session_tree: SessionTree, instructions: str = "") -> CompactionEntry:
        """执行压缩：找切割点→提取消息→生成摘要→追加compaction条目。"""
        branch = session_tree.get_active_branch()
        if not branch:
            return CompactionEntry(summary="无历史消息可压缩", compacted=False)

        last = _last_compaction(branch)
        previous_summary = (last.content or {}).get("summary", "") if last else ""

        # 待压缩区间：上一个first_kept之前的compaction已覆盖，本次从那里之后开始
        if last:
            anchor = (last.content or {}).get("first_kept_entry_id")
            start = next((i for i, e in enumerate(branch) if e.id == anchor), 0)
        else:
            start = 0
        pool = [e for e in branch[start:] if e.type != "compaction"]

        if not pool:
            return CompactionEntry(summary="无新历史可压缩", compacted=False)

        tokens_before = estimate_branch_tokens(
            [e for e in branch if e.type != "compaction"])

        cutoff = self.find_cutoff_point(pool)
        if cutoff is None:
            return CompactionEntry(
                summary="最近上下文未超保留阈值，无需压缩", compacted=False,
                tokens_before=tokens_before)
        if cutoff == 0:
            # 整个池子是一个超大组且它就是全部内容：无更早历史可压缩
            return CompactionEntry(
                summary="无可压缩的更早历史", compacted=False,
                tokens_before=tokens_before)
        # cutoff 必落在组边界上（find_cutoff_point 保证），无需旧版孤儿修补循环

        to_summarize, kept = pool[:cutoff], pool[cutoff:]
        first_kept_id = kept[0].id if kept else None

        history_text = self._format_history(to_summarize)
        try:
            summary = self.summarizer(history_text, previous_summary, instructions)
        except Exception as exc:  # noqa: BLE001 — 摘要失败降级为启发式，不打断会话
            logger.warning("LLM摘要失败，降级为启发式摘要: %s", exc)
            summary = heuristic_summary(history_text, previous_summary, instructions)

        session_tree.append(
            "compaction",
            {
                "summary": summary,
                "first_kept_entry_id": first_kept_id,
                "tokens_before": tokens_before,
                "instructions": instructions,
                # layered-memory D2：被保护高分组数（开关关闭时为 0）
                "protected_groups": self._last_protected_count,
            },
        )

        return CompactionEntry(
            summary=summary,
            first_kept_entry_id=first_kept_id,
            tokens_before=tokens_before,
            details={
                "summarized_count": len(to_summarize),
                "kept_count": len(kept),
                "iterative": bool(previous_summary),
                "protected_groups": self._last_protected_count,
            },
        )


# ─── 摘要生成 ─────────────────────────────────────────────────────────────

def heuristic_summary(history_text: str, previous_summary: str = "",
                      instructions: str = "") -> str:
    """启发式摘要（无LLM/降级用）：抽取工具名与文本行首。"""
    tools, heads = [], []
    for line in history_text.splitlines():
        if '"name"' in line:
            try:
                tools.append(json.loads(line.split("]", 1)[1]).get("name", ""))
            except Exception:  # noqa: BLE001
                pass
        elif len(heads) < 12:
            heads.append(line[:120])
    parts = []
    if previous_summary:
        parts.append(f"（接前次摘要）{previous_summary[:200]}")
    if instructions:
        parts.append(f"聚焦：{instructions}")
    parts.append("早期对话要点：\n" + "\n".join(heads))
    if tools:
        parts.append(f"调用过的工具: {', '.join(sorted(set(t for t in tools if t)))}")
    return "\n\n".join(parts)


def llm_summary(session, history_text: str, previous_summary: str = "",
                instructions: str = "", task_record=None) -> str:
    """通过AIGateway生成摘要（CHAT链路，复用熔断/限流/计费）。

    task_record：归因费用到本轮记录（入账/预算）；手动 /compact 无记录时为 None。
    summary_model：配置了更便宜的小模型时固定走它（摘要无需工具调用），降本。"""
    from apps.ai_gateway.gateway import AIGateway

    focus = FOCUS_SECTION.format(instructions=instructions) if instructions else ""
    prompt = COMPACTION_PROMPT.format(
        previous_summary=previous_summary or "（无）",
        focus_section=focus,
        history=history_text,
    )
    summary_model = _resolve_summary_model(session)
    resp = AIGateway().chat(
        session.agent_config,
        messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
        system=SUMMARY_SYSTEM,
        play=session.play,
        task_record=task_record,
        model=summary_model,
    )
    return chat_text(resp).strip() or heuristic_summary(history_text, previous_summary, instructions)


def _resolve_summary_model(session):
    """解析摘要小模型 → AIModel；未配置/无效返回 None（走默认链）。

    优先级（design D1）：summary_model（既有 pk 配置）> assistant_models.small
    （多级模型 small 档，主计划 3.3.2）> None。"""
    model_id = compaction_config(session).summary_model
    if not model_id:
        # 回退多级 small 档（无需工具调用，supports_tool_calls=False）
        try:
            from apps.ai_gateway.router import select_model_for_task
            return select_model_for_task(session.agent_config, 0.0,
                                         supports_tool_calls=False)
        except Exception:  # noqa: BLE001
            return None
    try:
        from apps.ai_gateway.models import AIModel
        return AIModel.objects.filter(pk=int(model_id), is_active=True).select_related("provider").first()
    except (ValueError, TypeError):
        return None


def _run_compaction(session, tree, instructions: str = "", task_record=None) -> CompactionEntry:
    """共用的压缩执行体：LLM摘要（失败降级启发式由 Compactor 内部处理）。"""
    config = compaction_config(session)
    summarizer = (lambda h, p, i: llm_summary(session, h, p, i, task_record=task_record))
    return Compactor(config, summarizer=summarizer).compact(tree, instructions)


def compact_session(session, instructions: str = "") -> CompactionEntry:
    """手动压缩（/compact命令）：LLM摘要 + 写JSONL。"""
    from .session_tree import SessionStorage

    tree = SessionTreeManager.get_or_create(session)
    result = _run_compaction(session, tree, instructions)
    if result.compacted:
        SessionStorage.save(tree)
    return result


def maybe_auto_compact(session, tree=None, task_record=None) -> Optional[CompactionEntry]:
    """自动压缩入口（runner调用）：超阈值时用LLM摘要压缩。

    tree：调用方已加载的会话树（runner 每迭代加载一次并统一落盘）。
    task_record：归因 LLM 摘要费用到本轮记录（入账/预算）。"""
    from django.conf import settings
    from .session_tree import SessionStorage

    if not getattr(settings, FEATURE_FLAG, False):
        return None
    caller_saves = tree is not None
    tree = tree or SessionTreeManager.get_or_create(session)
    config = compaction_config(session)
    if not should_compact(tree, config):
        return None
    result = _run_compaction(session, tree, task_record=task_record)
    if result.compacted:
        if not caller_saves:
            SessionStorage.save(tree)
        logger.info("自动压缩 session=%s tokens_before=%d",
                    session.pk, result.tokens_before)
    return result


# ─── 上下文重建 ─────────────────────────────────────────────────────────

def compacted_history_blocks(session, tree=None) -> Optional[list[dict]]:
    """若压缩生效，返回压缩后的模型消息列表；未压缩返回None（走旧路径）。

    tree：调用方已加载的会话树（runner 每迭代加载一次）。

    输出结构与 runner.build_history 一致：compaction摘要作为首条user消息，
    其后是 first_kept 之后的消息（user/assistant/tool 映射同 SessionTreeManager）。
    """
    tree = tree or SessionTreeManager.get_or_create(session)
    branch = tree.get_active_branch()
    last = _last_compaction(branch)
    if last is None:
        return None

    entries = effective_context_entries(branch)
    messages = [{
        "role": "user",
        "content": [{"type": "text",
                     "text": f"{SUMMARY_PREFIX}\n{(last.content or {}).get('summary', '')}"}],
    }]
    for e in entries[1:]:
        blocks = e.content.get("blocks")
        if blocks is None:
            continue
        if e.type == "assistant_message":
            role = "assistant"
        else:  # user_message / tool_result 都走 user
            role = "user"
        if messages and messages[-1]["role"] == role:
            messages[-1]["content"] += blocks
        else:
            messages.append({"role": role, "content": list(blocks)})
    return messages
