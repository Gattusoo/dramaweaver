"""树状会话历史（Phase 1.2）：学习Pi Agent的核心创新。

设计对齐Pi Agent：
- 会话存储为树结构（JSONL格式）
- 支持从任何点创建分支
- 分支摘要保留被放弃分支的工作
- 导航命令：/tree、/fork、/clone
"""

from __future__ import annotations

import json
import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Optional

from django.conf import settings
from django.utils import timezone

from .llm_text import chat_text
from .models import AssistantMessage, AssistantSession, MessageRole

logger = logging.getLogger(__name__)


# ─── 常量 ───────────────────────────────────────────────────────────────────

SESSION_DIR = "sessions"
ENTRY_TYPES = {
    "user_message",
    "assistant_message",
    "tool_call",
    "tool_result",
    "plan",
    "plan_update",
    "reflection",
    "tot",
    "compaction",
    "branch_summary",
}

# MessageRole → entry_type 映射（_rebuild_from_db / append_message 共用）
_ROLE_TO_ENTRY = {
    "user": "user_message",
    "assistant": "assistant_message",
    "tool": "tool_result",
}


# ─── 数据结构 ───────────────────────────────────────────────────────────────

@dataclass
class SessionEntry:
    """会话树中的单个条目。"""
    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    parent_id: Optional[str] = None
    timestamp: str = field(default_factory=lambda: timezone.now().isoformat())
    type: str = "user_message"
    content: dict = field(default_factory=dict)
    metadata: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "timestamp": self.timestamp,
            "type": self.type,
            "content": self.content,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "SessionEntry":
        return cls(
            id=data["id"],
            parent_id=data.get("parent_id"),
            timestamp=data["timestamp"],
            type=data["type"],
            content=data.get("content", {}),
            metadata=data.get("metadata", {}),
        )


@dataclass
class SessionTree:
    """会话树：管理所有条目和活动分支。"""
    session_id: str
    play_id: str
    entries: list[SessionEntry] = field(default_factory=list)
    active_entry_id: Optional[str] = None
    # phase1 修复 2.4：显式导航标记（持久化为 _meta 行）。旧实现用
    # active_entry_id != entries[-1].id 推断，本轮消息 append 后导航点即成
    # 尾条目 → 恒判未导航。None=从未导航；ISO 时间串=导航过。
    navigated_at: Optional[str] = None
    _entry_map: dict[str, SessionEntry] = field(default_factory=dict, repr=False)
    _children_map: dict[str, list[str]] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        """初始化索引。"""
        self._rebuild_index()

    def _rebuild_index(self):
        """重建索引（用于快速查询）。"""
        self._entry_map = {e.id: e for e in self.entries}
        self._children_map = {}
        for e in self.entries:
            if e.parent_id:
                self._children_map.setdefault(e.parent_id, []).append(e.id)

    # ─── 基本操作 ────────────────────────────────────────────────────────

    def append(self, entry_type: str, content: dict, parent_id: Optional[str] = None,
               metadata: Optional[dict] = None,
               entry_id: Optional[str] = None) -> SessionEntry:
        """添加条目到树中。

        Args:
            entry_type: 条目类型（user_message, assistant_message等）
            content: 条目内容
            parent_id: 父条目ID（None表示添加到当前活动条目）
            metadata: 元数据
            entry_id: 指定条目ID（DB重建用确定性ID msg-{pk}；缺省uuid4）
        """
        if parent_id is None:
            parent_id = self.active_entry_id

        entry = SessionEntry(
            id=entry_id or str(uuid.uuid4()),
            parent_id=parent_id,
            type=entry_type,
            content=content,
            metadata=metadata or {},
        )
        self.entries.append(entry)
        self._entry_map[entry.id] = entry
        if parent_id:
            self._children_map.setdefault(parent_id, []).append(entry.id)
        self.active_entry_id = entry.id
        return entry

    def get_entry(self, entry_id: str) -> Optional[SessionEntry]:
        """获取条目。"""
        return self._entry_map.get(entry_id)

    def get_children(self, entry_id: str) -> list[SessionEntry]:
        """获取子条目。"""
        child_ids = self._children_map.get(entry_id, [])
        return [self._entry_map[cid] for cid in child_ids]

    def get_parent(self, entry_id: str) -> Optional[SessionEntry]:
        """获取父条目。"""
        entry = self.get_entry(entry_id)
        if entry and entry.parent_id:
            return self.get_entry(entry.parent_id)
        return None

    # ─── 活动分支操作 ────────────────────────────────────────────────────

    def get_active_branch(self) -> list[SessionEntry]:
        """获取活动分支（从根到当前活动条目的路径）。

        按时间顺序返回，用于构建模型请求的history。
        """
        if not self.active_entry_id:
            return []
        return self.get_branch_path(self.active_entry_id)

    def navigate_to(self, entry_id: str) -> bool:
        """导航到某个条目（切换活动分支）。

        Returns:
            True: 成功导航
            False: 条目不存在
        """
        if entry_id not in self._entry_map:
            return False
        self.active_entry_id = entry_id
        # phase1 修复 2.4：显式记录导航事件，_tree_navigated 读此标记
        self.navigated_at = timezone.now().isoformat()
        return True

    def get_branch_path(self, entry_id: str) -> list[SessionEntry]:
        """获取到某个条目的路径（从根到该条目）。"""
        path = []
        current_id = entry_id
        visited = set()

        while current_id and current_id not in visited:
            visited.add(current_id)
            entry = self.get_entry(current_id)
            if not entry:
                break
            path.append(entry)
            current_id = entry.parent_id

        return list(reversed(path))

    # ─── 分支操作 ────────────────────────────────────────────────────────

    def create_branch(self, from_entry_id: str, new_content: dict,
                     entry_type: str = "user_message") -> Optional[SessionEntry]:
        """从某个条目创建新分支。

        新分支将从from_entry_id开始，添加new_content作为新条目。

        Args:
            from_entry_id: 分支的起始条目
            new_content: 新条目的内容
            entry_type: 新条目的类型
        """
        if from_entry_id not in self._entry_map:
            return None

        return self.append(entry_type, new_content, parent_id=from_entry_id)

    def fork(self, from_entry_id: str) -> list[SessionEntry]:
        """Fork：获取从根到某个条目的完整分支路径。

        用于创建新会话时复制历史。
        """
        return self.get_branch_path(from_entry_id)

    # ─── 树可视化 ────────────────────────────────────────────────────────

    def get_tree_structure(self, max_depth: int = 10) -> dict:
        """获取树的结构化表示（用于可视化）。

        Returns:
            {
                "id": "root_id",
                "type": "user_message",
                "children": [...]
            }
        """
        if not self.entries:
            return {"id": "root", "type": "empty", "children": []}

        # 找到根节点（parent_id为None的条目）
        root_entries = [e for e in self.entries if e.parent_id is None]
        if not root_entries:
            # 如果没有根节点，以第一个条目为根
            root_entries = [self.entries[0]]

        def build_tree(entry: SessionEntry, depth: int) -> dict:
            if depth > max_depth:
                return {"id": entry.id, "type": entry.type, "children": []}

            children = []
            for child_id in self._children_map.get(entry.id, []):
                child = self._entry_map.get(child_id)
                if child:
                    children.append(build_tree(child, depth + 1))

            return {
                "id": entry.id,
                "type": entry.type,
                "children": children,
                "content_preview": str(entry.content)[:50],
                "timestamp": entry.timestamp,
            }

        # 如果有多个根节点，创建一个虚拟根
        if len(root_entries) == 1:
            return build_tree(root_entries[0], 0)
        else:
            return {
                "id": "virtual_root",
                "type": "virtual",
                "children": [build_tree(e, 0) for e in root_entries],
            }

    # ─── 序列化 ──────────────────────────────────────────────────────────

    def to_jsonl(self, active_entry_id: Optional[str] = None) -> str:
        """序列化为JSONL格式。首行可选写入活动条目指针（持久化导航位置）。

        phase1 修复 2.4：导航过时额外持久化 navigated_at 标记（_meta 行）。"""
        lines = []
        meta = {"_meta": "active", "id": active_entry_id}
        if self.navigated_at:
            meta["navigated_at"] = self.navigated_at
        if active_entry_id is not None and active_entry_id in self._entry_map:
            lines.append(json.dumps(meta, ensure_ascii=False))
        lines.extend(json.dumps(e.to_dict(), ensure_ascii=False) for e in self.entries)
        return "\n".join(lines)

    @classmethod
    def from_jsonl(cls, jsonl_content: str, session_id: str, play_id: str) -> "SessionTree":
        """从JSONL反序列化。跳过损坏行与元数据行（导航指针）。"""
        entries = []
        active_entry_id = None
        navigated_at = None
        for line in jsonl_content.strip().split("\n"):
            if not line:
                continue
            try:
                data = json.loads(line)
            except json.JSONDecodeError:  # 损坏行（如写入中断）跳过，不使整树不可读
                logger.warning("会话树存在损坏行，已跳过 session=%s", session_id)
                continue
            if "_meta" in data:  # 持久化的活动条目指针
                active_entry_id = data.get("id")
                navigated_at = data.get("navigated_at")  # 旧格式缺该字段 → 视为未导航
                continue
            entries.append(SessionEntry.from_dict(data))

        # 无持久化指针时退回最后一个条目
        if active_entry_id is None:
            active_entry_id = entries[-1].id if entries else None

        return cls(
            session_id=session_id,
            play_id=play_id,
            entries=entries,
            active_entry_id=active_entry_id,
            navigated_at=navigated_at,
        )


# ─── JSONL持久化 ───────────────────────────────────────────────────────────

class SessionStorage:
    """JSONL文件存储。"""

    @staticmethod
    def get_session_file(play_id: str, session_id: str) -> Path:
        """获取会话文件路径。"""
        media_root = Path(settings.MEDIA_ROOT)
        session_dir = media_root / SESSION_DIR / str(play_id)
        session_dir.mkdir(parents=True, exist_ok=True)
        return session_dir / f"{session_id}.jsonl"

    @staticmethod
    def save(session_tree: SessionTree) -> Path:
        """保存会话树到JSONL文件（原子写：临时文件+replace，避免中断损坏）。"""
        import os

        file_path = SessionStorage.get_session_file(
            session_tree.play_id, session_tree.session_id
        )
        tmp_path = file_path.with_suffix(f".{session_tree.session_id}.tmp")
        tmp_path.write_text(
            session_tree.to_jsonl(active_entry_id=session_tree.active_entry_id),
            encoding="utf-8")
        os.replace(tmp_path, file_path)  # 同目录原子替换
        logger.info("保存会话树 session=%s file=%s", session_tree.session_id, file_path)
        return file_path

    @staticmethod
    def load(play_id: str, session_id: str) -> Optional[SessionTree]:
        """从JSONL文件加载会话树。"""
        file_path = SessionStorage.get_session_file(play_id, session_id)
        if not file_path.exists():
            return None

        content = file_path.read_text(encoding="utf-8")
        return SessionTree.from_jsonl(content, session_id, play_id)


# ─── 会话树管理器 ─────────────────────────────────────────────────────────

class SessionTreeManager:
    """会话树管理器：桥接AssistantSession和SessionTree。"""

    @staticmethod
    def get_or_create(session: AssistantSession) -> SessionTree:
        """获取或创建会话树。

        首先尝试从JSONL文件加载，如果不存在则从数据库重建。
        """
        # 尝试从JSONL加载
        tree = SessionStorage.load(str(session.play_id), str(session.pk))
        if tree:
            return tree

        # 从数据库重建
        return SessionTreeManager._rebuild_from_db(session)

    @staticmethod
    def _rebuild_from_db(session: AssistantSession) -> SessionTree:
        """从AssistantMessage数据库重建会话树。"""
        tree = SessionTree(
            session_id=str(session.pk),
            play_id=str(session.play_id),
        )

        # 按seq顺序加载消息，重建线性历史（先不支持分支，后续增强）
        # 确定性 ID msg-{pk}：同一会话多次重建得到相同 ID 集合，使 /tree 的
        # entry_id 跨请求稳定、tree/navigate 可用（否则 uuid4 每次漂移 → navigate 404）。
        messages = session.messages.order_by("seq")
        for msg in messages:
            entry_type = _ROLE_TO_ENTRY.get(msg.role, "assistant_message")
            content = {"message_id": msg.pk, "blocks": msg.content}
            tree.append(entry_type, content, parent_id=None, entry_id=f"msg-{msg.pk}")  # 暂时线性

        return tree

    @staticmethod
    def append_message(session: AssistantSession, role: str, content: list,
                       parent_id: Optional[str] = None,
                       metadata: Optional[dict] = None,
                       tree: Optional[SessionTree] = None,
                       save: bool = True) -> SessionEntry:
        """添加消息到会话树。

        tree：调用方已持有的树（runner 每迭代加载一次，避免重复读盘）。
        save=False 时不落盘（由调用方统一保存）。"""
        if tree is None:
            tree = SessionTreeManager.get_or_create(session)

        entry_type = _ROLE_TO_ENTRY.get(role, "assistant_message")

        entry = tree.append(
            entry_type,
            {"blocks": content},
            parent_id=parent_id,
            metadata=metadata or {},
        )
        if save:
            SessionStorage.save(tree)
        return entry


# ─── 分支摘要 ────────────────────────────────────────────────────────────

BRANCH_SUMMARY_SYSTEM = "你是分支摘要器：把创作助手被放弃分支的工作压缩为中文摘要，供切换到新分支时作为上下文。"

BRANCH_SUMMARY_PROMPT = """以下会话树中，用户从某个分支切换离开。请总结被放弃分支上完成的工作，使新分支能了解之前的探索结果而不重复劳动。

被放弃的条目历史：
{history}

已提取的线索：
- 调用过的工具: {tools}
- 产生的提案: {proposals}
- 读过的对象: {read_targets}
- 尝试修改的对象: {write_targets}
- 反思: {reflections}

请生成简洁摘要（3-6句），包含：分支目标、关键决策、产出的提案/方案、未完成事项、对后续分支的提示。"""

# 只读/写工具 → 文件（资产）归类，用于cumulative tracking（名称与 tools.py 注册名对齐）
_READ_TOOLS = {"get_play_overview", "get_outline", "get_episode_script", "get_scene", "get_shot",
               "get_characters", "get_task_status", "get_cost_summary", "search_asset_library",
               "memory_read", "review_episode", "list_skills", "load_skill"}
_WRITE_TOOLS = {"propose_outline_edit", "propose_episode_script_edit", "propose_scene_edit",
                "propose_shot_edit", "propose_shot_insert", "propose_shot_delete",
                "propose_scene_create", "propose_character_edit", "propose_character_create",
                "request_generation", "memory_save"}


class BranchSummarizer:
    """分支摘要：切换分支时自动摘要被放弃的分支（Phase 1.4，对齐Pi Agent）。

    - should_summarize: 仅当被放弃段含实际工作（工具/提案/计划/反思）才摘要
    - cumulative tracking: 汇总 read_files / modified_files（资产对象）
    - summarize_branch: 启发式摘要；可选注入LLM summarizer
    - 摘要以 branch_summary 条目挂到分叉点父条目下，使新分支上下文可见
    """

    @staticmethod
    def divergent_segment(from_branch: list[SessionEntry],
                          to_branch: list[SessionEntry]) -> tuple[list[SessionEntry], Optional[SessionEntry]]:
        """计算被放弃的分支段：from_branch 中不在 to_branch 上的后缀。

        Returns:
            (abandoned_entries, fork_point) — fork_point为两分支最后公共条目
        """
        to_ids = {e.id for e in to_branch}
        fork_point = None
        fork_idx = -1
        for i, e in enumerate(from_branch):
            if e.id in to_ids:
                fork_point, fork_idx = e, i
        abandoned = from_branch[fork_idx + 1:] if fork_point else list(from_branch)
        return abandoned, fork_point

    @staticmethod
    def should_summarize(session_tree: SessionTree, from_branch: list[SessionEntry],
                        to_branch: list[SessionEntry]) -> bool:
        """被放弃段有工具调用/计划/反思/提案时才摘要（纯闲聊不摘要）。

        phase1 修复 2.1：对齐真实写入形状——工具调用是 assistant_message
        条目 blocks 内的 tool_use 块，纯文本回复不算工作；其余类型按条目
        类型识别（tool_result/plan/plan_update/reflection）。"""
        abandoned, _ = BranchSummarizer.divergent_segment(from_branch, to_branch)
        for e in abandoned:
            if e.type in ("tool_result", "plan", "plan_update", "reflection"):
                return True
            if BranchSummarizer._tool_uses(e):
                return True
        return False

    @staticmethod
    def _tool_uses(entry: SessionEntry) -> list[dict]:
        """提取条目内的 tool_use 块（名称 + input）。"""
        if entry.type != "assistant_message":
            return []
        blocks = (entry.content or {}).get("blocks") or []
        return [b for b in blocks if isinstance(b, dict) and b.get("type") == "tool_use"]

    @staticmethod
    def collect_cumulative(from_branch: list[SessionEntry]) -> dict:
        """累积文件/资产跟踪：读过的对象与尝试修改的对象。

        phase1 修复 2.1：工具调用取自 assistant_message 条目的 tool_use 块
        （生产写入形状），不再依赖从未写入的 tool_call 条目类型。"""
        read_targets, write_targets, tools = set(), set(), []
        for e in from_branch:
            for use in BranchSummarizer._tool_uses(e):
                name = use.get("name", "")
                tools.append(name)
                args = use.get("input") or {}
                target = args.get("target") or args.get("episode_number") or args.get("scene_id") \
                    or args.get("shot_id") or args.get("path") or ""
                if name in _READ_TOOLS and target:
                    read_targets.add(str(target))
                elif name in _WRITE_TOOLS and target:
                    write_targets.add(str(target))
        return {
            "tools": sorted(set(tools)),
            "read_files": sorted(read_targets),
            "modified_files": sorted(write_targets),
        }

    @staticmethod
    def _extract_clues(branch: list[SessionEntry]) -> dict:
        """从分支提取结构化线索（工具/提案/反思）。

        phase1 修复 2.1：对齐真实写入形状——工具调用取 assistant_message 的
        tool_use 块；反思条目写的是 lessons_learned（兼容旧 lesson 键）。"""
        tool_calls, proposals, reflections = [], [], []
        for entry in branch:
            for use in BranchSummarizer._tool_uses(entry):
                tool_calls.append(use.get("name", "unknown"))
            if entry.type == "tool_result":
                content_str = json.dumps(entry.content, ensure_ascii=False)
                if "proposal" in content_str.lower():
                    proposals.append((entry.content or {}).get("summary", ""))
            elif entry.type == "reflection":
                content = entry.content or {}
                reflections.append(content.get("lessons_learned") or content.get("lesson", ""))
        return {"tools": tool_calls, "proposals": [p for p in proposals if p],
                "reflections": [r for r in reflections if r]}

    @staticmethod
    def _heuristic_summary(clues: dict, cumulative: dict) -> str:
        parts = []
        if clues["tools"]:
            parts.append(f"调用了工具: {', '.join(sorted(set(clues['tools'])))}")
        if clues["proposals"]:
            parts.append(f"生成了提案: {', '.join(clues['proposals'])}")
        if cumulative["read_files"]:
            parts.append(f"查看了: {', '.join(cumulative['read_files'][:10])}")
        if cumulative["modified_files"]:
            parts.append(f"尝试修改: {', '.join(cumulative['modified_files'][:10])}")
        if clues["reflections"]:
            parts.append(f"反思教训: {', '.join(clues['reflections'])}")
        return ("在另一个分支中：" + "; ".join(parts)) if parts else "在另一个分支中进行了一些探索"

    @staticmethod
    def summarize_branch(session_tree: SessionTree,
                        from_branch: list[SessionEntry],
                        to_branch: Optional[list[SessionEntry]] = None,
                        summarizer=None) -> str:
        """生成被放弃分支的摘要并追加 branch_summary 条目（挂在分叉点下）。

        Args:
            from_branch: 离开前的完整活动分支
            to_branch: 导航后的活动分支（用于算分叉点；缺省视为整段放弃）
            summarizer: 可选 (history_text, clues: dict) -> str，缺省启发式
        """
        if to_branch is not None:
            abandoned, fork_point = BranchSummarizer.divergent_segment(from_branch, to_branch)
        else:
            abandoned, fork_point = from_branch, None

        clues = BranchSummarizer._extract_clues(abandoned)
        cumulative = BranchSummarizer.collect_cumulative(abandoned)

        if summarizer:
            history = "\n".join(
                f"[{e.type}] {json.dumps(e.content, ensure_ascii=False, default=str)[:300]}"
                for e in abandoned)
            clue_dict = {
                "tools": cumulative["tools"], "proposals": clues["proposals"],
                "read_targets": cumulative["read_files"],
                "write_targets": cumulative["modified_files"],
                "reflections": clues["reflections"],
            }
            try:
                summary = summarizer(history, clue_dict)
            except Exception as exc:  # noqa: BLE001 — 降级启发式（phase1 修复 6.2 补日志）
                logger.warning("LLM分支摘要失败，降级启发式: %s", exc)
                summary = BranchSummarizer._heuristic_summary(clues, cumulative)
        else:
            summary = BranchSummarizer._heuristic_summary(clues, cumulative)

        session_tree.append(
            "branch_summary",
            {
                "summary": summary,
                "summarized_entry_ids": [e.id for e in abandoned],
                "read_files": cumulative["read_files"],
                "modified_files": cumulative["modified_files"],
                "tools": cumulative["tools"],
            },
            # 挂到分叉点下，使新分支路径上能看到这份摘要
            parent_id=fork_point.id if fork_point else None,
        )
        return summary

    @staticmethod
    def llm_summarizer(session, task_record=None):
        """返回经AIGateway的分支摘要器（CHAT链路，失败由调用方降级）。

        task_record：摘要 LLM 调用归因费用（CostRecord → 会话预算，
        phase1 修复 7.2）。"""
        def _summarize(history: str, clues: dict) -> str:
            from apps.ai_gateway.gateway import AIGateway
            from apps.ai_gateway.router import select_model_for_task
            prompt = BRANCH_SUMMARY_PROMPT.format(
                history=history,
                tools=", ".join(clues.get("tools") or ["无"]),
                proposals=", ".join(clues.get("proposals") or ["无"]),
                read_targets=", ".join(clues.get("read_targets") or ["无"]),
                write_targets=", ".join(clues.get("write_targets") or ["无"]),
                reflections=", ".join(clues.get("reflections") or ["无"]),
            )
            # 摘要优先 small 档（主计划 3.3.2，design D1）；未配置回退默认链
            try:
                summary_model = select_model_for_task(
                    session.agent_config, 0.0, supports_tool_calls=False)
            except Exception:  # noqa: BLE001
                summary_model = None
            resp = AIGateway().chat(
                session.agent_config,
                messages=[{"role": "user", "content": [{"type": "text", "text": prompt}]}],
                system=BRANCH_SUMMARY_SYSTEM,
                play=session.play,
                task_record=task_record,
                model=summary_model,
            )
            return chat_text(resp).strip()
        return _summarize

    @staticmethod
    def on_navigate(session, session_tree: SessionTree, entry_id: str,
                    task_record=None) -> Optional[str]:
        """导航时检测分支切换：放弃段有工作则生成摘要（LLM优先）。

        开关：默认启用；settings.ENABLE_BRANCH_SUMMARY=False 显式关闭。
        task_record：摘要 LLM 费用归因（phase1 修复 7.2）。"""
        from django.conf import settings
        if getattr(settings, "ENABLE_BRANCH_SUMMARY", True) is False:
            return None

        from_branch = session_tree.get_active_branch()
        # 目标路径即导航后的分支
        to_branch = session_tree.get_branch_path(entry_id)
        if not from_branch or not to_branch:
            return None
        if from_branch[-1].id == to_branch[-1].id:
            return None  # 导航到当前活动条目，无切换

        if not BranchSummarizer.should_summarize(session_tree, from_branch, to_branch):
            return None

        try:
            summarizer = BranchSummarizer.llm_summarizer(session, task_record=task_record)
        except Exception:  # noqa: BLE001
            summarizer = None
        return BranchSummarizer.summarize_branch(
            session_tree, from_branch, to_branch, summarizer=summarizer)


# ─── 分支摘要注入模型上下文（Phase B）─────────────────────────────────────

BRANCH_SUMMARY_INJECT_PREFIX = "[另一分支探索摘要]"


def collect_branch_summaries_for_active(tree: SessionTree) -> list[str]:
    """收集活动路径各分叉点下挂着的 branch_summary 摘要文本（供注入模型上下文）。

    摘要条目挂在分叉点（活动路径上的某条目）下，是活动路径的**兄弟叶节点**，
    get_active_branch() 取不到——此函数专门把它们捞出来。

    返回摘要文本列表（按活动路径顺序，先分叉的先出现；每摘要挂在唯一分叉点下，天然不重复）。
    """
    summaries: list[str] = []
    for entry in tree.get_active_branch():
        for child in tree.get_children(entry.id):
            if child.type != "branch_summary":
                continue
            text = (child.content or {}).get("summary", "").strip()
            if text:
                summaries.append(text)
    return summaries


def branch_summary_inject_messages(tree: SessionTree) -> list[dict]:
    """把分支摘要包装成注入到对话首部的 user 消息（标注来源，避免误认为当前分支历史）。

    无摘要返回 []。"""
    summaries = collect_branch_summaries_for_active(tree)
    if not summaries:
        return []
    body = "\n\n".join(f"- {s}" for s in summaries)
    return [{
        "role": "user",
        "content": [{"type": "text",
                     "text": f"{BRANCH_SUMMARY_INJECT_PREFIX}\n以下是此前在其他分支中探索的成果摘要，"
                             f"供参考，不是当前对话历史：\n{body}"}],
    }]
