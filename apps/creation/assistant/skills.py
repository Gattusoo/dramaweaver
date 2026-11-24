"""Skills 机制（Phase 1.5）：学习 Pi Agent 的渐进式披露。

设计对齐 Pi Agent / Claude Code Skills：
- Skill 结构：name, description, instructions, tools, metadata
- 存储：media/skills/{name}/skill.md（YAML frontmatter + 正文）+ 可选 tools.py
- Progressive disclosure：启动只注入 name+description；agent 调 load_skill 工具才取全文
- 触发：match(user_text) 按 trigger 关键词/正则打分，超过阈值自动提示可用 Skill
- 内置 6 个短剧创作 Skills（打包在 builtin/，运行时可被 media/skills 同名覆盖）
"""

from __future__ import annotations

import importlib.util
import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from django.conf import settings

logger = logging.getLogger(__name__)

SKILLS_DIR = "skills"
BUILTIN_DIR = Path(__file__).resolve().parent / "builtin" / "skills"
MAX_SKILL_CHARS = 20000

_FM_RE = re.compile(r"^---\s*\n(.*?)\n---\s*\n", re.S)


# ─── 数据结构 ───────────────────────────────────────────────────────────────

@dataclass
class Skill:
    name: str
    description: str
    instructions: str = ""
    tools: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)
    source: str = "builtin"  # builtin | custom
    path: Optional[Path] = None
    _trigger_res: list = field(default_factory=list, repr=False, compare=False)

    def brief(self) -> dict:
        return {"name": self.name, "description": self.description,
                "triggers": self.triggers, "source": self.source}

    def full(self) -> dict:
        return {**self.brief(), "instructions": self.instructions,
                "tools": self.tools, "metadata": self.metadata}


# ─── skill.md 解析 ─────────────────────────────────────────────────────────

def _parse_scalar(v: str):
    v = v.strip()
    if v.startswith("[") and v.endswith("]"):
        try:
            return json.loads(v.replace("'", '"'))
        except Exception:  # noqa: BLE001
            return [x.strip() for x in v[1:-1].split(",") if x.strip()]
    return v.strip("'\"")


def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """解析 YAML frontmatter（支持 name/description/tools/triggers 的扁平键值）。"""
    m = _FM_RE.match(text)
    if not m:
        return {}, text
    meta = {}
    for line in m.group(1).splitlines():
        if ":" in line:
            k, _, v = line.partition(":")
            meta[k.strip()] = _parse_scalar(v)
    return meta, text[m.end():]


def _load_skill_file(path: Path, source: str) -> Optional[Skill]:
    """加载单个 skill.md。失败返回 None 被 _scan 跳过。

    phase1 修复 9.2：整体隔离失败——除 OSError 外，frontmatter 解析、
    Skill 构造、trigger 编译的任何异常都记 warning 后返回 None，
    单个坏文件不使整个注册表扫描失败（system_prompt / skills API 随之可用）。"""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        # 编码坏文件（非 UTF-8）与 IO 失败同样跳过（phase1 修复 9.2）
        logger.warning("技能文件读取失败，已跳过 %s", path)
        return None
    try:
        meta, body = _parse_frontmatter(text)
        tools = meta.get("tools", [])
        triggers = meta.get("triggers", [])
        if isinstance(tools, str):
            tools = [tools]
        if isinstance(triggers, str):
            triggers = [triggers]
        return Skill(
            name=meta.get("name") or path.parent.name,
            description=str(meta.get("description") or "").strip(),
            instructions=body.strip()[:MAX_SKILL_CHARS],
            tools=list(tools),
            triggers=list(triggers),
            metadata={k: v for k, v in meta.items()
                      if k not in ("name", "description", "tools", "triggers")},
            source=source,
            path=path,
            _trigger_res=_compile_triggers(triggers),
        )
    except Exception as exc:  # noqa: BLE001 — 单文件失败不拖垮扫描
        logger.warning("技能文件加载失败，已跳过 %s: %s", path, exc)
        return None


def _compile_triggers(triggers) -> list:
    """预编译合法正则 trigger；无法编译的保留原文供子串匹配。

    phase1 修复 9.1：非字符串/非正则触发条件（如 frontmatter 里的数字列表）
    的 TypeError 一并捕获——无法编译者按原文保留，match() 时子串匹配对
    非字符串转为 str 比较，不抛异常。"""
    compiled = []
    for trig in triggers or []:
        try:
            compiled.append(re.compile(trig))
        except (re.error, TypeError):
            compiled.append(trig)
    return compiled


def _skills_root() -> Path:
    return Path(settings.MEDIA_ROOT) / SKILLS_DIR


def ensure_skills_root() -> Path:
    """写路径专用：确保自定义 skill 目录存在。"""
    root = _skills_root()
    root.mkdir(parents=True, exist_ok=True)
    return root


def _scan(root: Path, source: str) -> dict[str, Skill]:
    out = {}
    if not root.exists():
        return out
    for skill_md in sorted(root.glob("*/skill.md")):
        skill = _load_skill_file(skill_md, source)
        if skill and skill.name:
            out[skill.name] = skill
    return out


# ─── 注册表（缓存） ────────────────────────────────────────────────────────

_cache: dict[str, Skill] = {}


def skill_registry(refresh: bool = False) -> dict[str, Skill]:
    """所有可用 Skill：内置 + media/skills（同名时自定义覆盖内置）。"""
    global _cache
    if refresh or not _cache:
        reg = _scan(BUILTIN_DIR, "builtin")
        reg.update(_scan(_skills_root(), "custom"))
        _cache = reg
    return _cache


def get_skill(name: str) -> Optional[Skill]:
    return skill_registry().get(name)


def list_skills() -> list[dict]:
    return [s.brief() for s in skill_registry().values()]


def load_skill(name: str) -> dict:
    """加载完整 Skill（含 instructions），工具集合动态注册。"""
    skill = get_skill(name)
    if skill is None:
        raise KeyError(name)
    _load_skill_tools(skill)
    return skill.full()


def _load_skill_tools(skill: Skill):
    """tools.py 动态导入：约定函数 register(registry_add) 或直接 tool_* 函数。

    文件路径：{skill_dir}/tools.py；函数签名 fn(ctx, **args)，用 @tool 装饰。
    """
    tools_py = skill.path.parent / "tools.py" if skill.path else None
    if not tools_py or not tools_py.exists():
        return
    spec = importlib.util.spec_from_file_location(f"skill_tools_{skill.name}", tools_py)
    if spec is None or spec.loader is None:
        return
    mod = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(mod)  # noqa: S102 — 用户自建skill目录，风险同插件机制
        logger.info("加载Skill工具 %s -> %s", skill.name, tools_py)
    except Exception:  # noqa: BLE001 — 工具文件有问题只记日志，不阻断skill使用
        logger.exception("Skill %s 的 tools.py 加载失败", skill.name)


def enabled() -> bool:
    return getattr(settings, "ENABLE_SKILLS", False)


# ─── 触发匹配 ─────────────────────────────────────────────────────────────

def match(user_text: str, limit: int = 3) -> list[Skill]:
    """按 trigger 关键词对 user_text 打分，返回最相关的 Skill。

    规则：triggers 中每个命中关键词/正则 +1 分；description 子串匹配 +0.5。
    """
    if not user_text:
        return []
    scored = []
    for skill in skill_registry().values():
        score = 0.0
        for trig in (skill._trigger_res or skill.triggers):
            if hasattr(trig, "search"):
                if trig.search(user_text):
                    score += 1.0
            elif trig and str(trig) in user_text:  # 非字符串触发条件转 str 子串比较
                score += 1.0
        if skill.description and skill.description in user_text:
            score += 0.5
        if score > 0:
            scored.append((score, skill))
    scored.sort(key=lambda x: -x[0])
    return [s for _, s in scored[:limit]]


# ─── 提示注入 ─────────────────────────────────────────────────────────────

def build_skills_prompt_section(matched: Optional[list[Skill]] = None) -> str:
    """注入到 system prompt：仅 name+description（渐进式披露第一层）。

    matched 非空时优先展示匹配的 Skill 并标注"可能适用"。
    """
    if not enabled():
        return ""
    reg = skill_registry()
    if not reg:
        return ""
    matched_names = {s.name for s in (matched or [])}
    lines = ["\n\n## 🧰 可用Skills（渐进式加载）",
             "以下为可用的创作技能模板。需要其详细指引时调用 load_skill 工具获取全文，",
             "不要凭名称猜测内容："]
    for s in sorted(reg.values(), key=lambda x: (x.name not in matched_names, x.name)):
        tag = " ⭐可能适用" if s.name in matched_names else ""
        tools = f"（附带工具: {', '.join(s.tools)}）" if s.tools else ""
        lines.append(f"- **{s.name}**{tag}: {s.description}{tools}")
    return "\n".join(lines)
