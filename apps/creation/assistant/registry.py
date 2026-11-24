"""工具注册表与沙箱执行（openspec change: backend-tool-registry-sandbox，D1–D3）。

- ToolRegistry 存纯函数工具定义（run(args: dict) -> dict，JSON 进 JSON 出）
- 创建/更新走静态检查（编译 + AST import 白名单 + builtins 危险调用 + run 契约 + schema 结构）
- 执行走隔离子进程（python -I + 自包含 bootstrap + builtins 裁剪 + 墙钟超时 + 输出上限）
- 动态加载：可见性查询 + (name, version) TTL 缓存 + 坏条目容错（对齐 skills 容错语义）

与 skills 的 tools.py 动态导入（进程内裸 exec）不同：注册表工具在子进程隔离执行，
白名单不含 subprocess/socket/http，递归与联网从能力上不存在。
"""
import ast
import json
import logging
import subprocess
import sys
import time
from pathlib import Path

from django.db.models import OuterRef
from django.conf import settings
from django.db import transaction

logger = logging.getLogger(__name__)

# ── 静态检查：import 白名单（新增模块按需放行，每次都是一次安全评审）─────────

IMPORT_WHITELIST = frozenset({
    "json", "math", "re", "random", "statistics", "itertools",
    "collections", "datetime", "unicodedata", "textwrap",
})

# AST 层直接拒绝的 builtins 名（调用位置）
_FORBIDDEN_CALLS = frozenset({
    "__import__", "eval", "exec", "compile", "open", "input", "breakpoint",
})

MAX_CODE_BYTES = 16 * 1024
MAX_DESCRIPTION = 300
MAX_CATEGORY = 50
TOOL_NAME_RE = r"^[a-z][a-z0-9_]{2,49}$"

SANDBOX_BOOTSTRAP = Path(__file__).resolve().parent / "sandbox" / "bootstrap.py"


class RegistryValidationError(ValueError):
    """创建/更新静态检查失败（区别于运行期 ToolError）。"""


def check_handler_code(code: str) -> None:
    """handler 源码静态检查。全部在父进程完成，零执行风险。

    1. 语法编译；2. import 白名单（含 from X import）；3. 危险 builtins 调用；
    4. 顶层必须恰好一个 run 函数定义且参数表为单个普通参数。
    """
    if not isinstance(code, str) or not code.strip():
        raise RegistryValidationError("handler 源码不能为空")
    if len(code.encode("utf-8")) > MAX_CODE_BYTES:
        raise RegistryValidationError(f"handler 源码超过 {MAX_CODE_BYTES} 字节上限")
    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise RegistryValidationError(f"handler 源码语法错误：{exc}") from exc

    run_defs = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            _check_import(node)
        elif isinstance(node, ast.FunctionDef) and node.name == "run":
            run_defs.append(node)
        elif isinstance(node, (ast.AsyncFunctionDef,)):
            raise RegistryValidationError("run 不能是 async 函数")
    if not run_defs:
        raise RegistryValidationError("handler 必须在顶层定义 run(args: dict) 函数")
    for fn in run_defs:
        args = fn.args
        if args.vararg or args.kwarg or args.kwonlyargs or args.defaults or args.kw_defaults:
            raise RegistryValidationError("run 函数参数表须为单个普通参数：run(args)")
        if len(args.args) != 1:
            raise RegistryValidationError("run 函数参数表须为单个普通参数：run(args)")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                and node.func.id in _FORBIDDEN_CALLS:
            raise RegistryValidationError(f"不允许调用 {node.func.id}()")
        # f-string / 属性链外的动态导入已由 builtins 裁剪兜底，AST 层不拦 getattr


def _check_import(node) -> None:
    if isinstance(node, ast.Import):
        names = [alias.name for alias in node.names]
    else:  # ImportFrom
        if node.level:  # 相对导入（import .x）一律拒绝
            raise RegistryValidationError("不允许相对导入")
        names = [node.module or ""]
    for name in names:
        root = name.split(".")[0]
        if root not in IMPORT_WHITELIST:
            raise RegistryValidationError(
                f"不允许导入 {name}；白名单：{', '.join(sorted(IMPORT_WHITELIST))}")


def check_input_schema(schema) -> None:
    """input_schema 结构检查（tools.validate 支持的子集，形状级而非值级）。

    注意：不是把空对象当值去校验（那会把 required 当成对 schema 自身的要求），
    而是检查 schema 的形状——type/properties/required/items 等 key 的类型正确。
    """
    if not isinstance(schema, dict):
        raise RegistryValidationError("input_schema 应为 JSON 对象")
    if "type" in schema and schema["type"] not in ("object", ["object", "null"]):
        # 工具入参顶层必须是 object（或 object|null），否则模型无法传参
        raise RegistryValidationError("input_schema.type 须为 object")
    props = schema.get("properties")
    if props is not None and not isinstance(props, dict):
        raise RegistryValidationError("input_schema.properties 应为对象")
    for key in schema.get("required", []):
        if not isinstance(key, str) or key not in (props or {}):
            raise RegistryValidationError(f"required 中的 {key!r} 未在 properties 声明")


def check_description(description: str) -> None:
    if not isinstance(description, str) or not description.strip():
        raise RegistryValidationError("工具描述不能为空")
    if len(description) > MAX_DESCRIPTION:
        raise RegistryValidationError(f"描述超过 {MAX_DESCRIPTION} 字上限")


def check_tool_name(name: str) -> None:
    import re
    if not isinstance(name, str) or not re.match(TOOL_NAME_RE, name):
        raise RegistryValidationError(
            "工具名须为 3-50 位小写字母/数字/下划线且以字母开头")


# ── 创建 / 更新服务（D1）────────────────────────────────────────────────────

def _builtin_tool_names() -> set:
    from . import tools as T
    return set(T._TOOLS.keys())


def create_tool(*, tool_name, tool_type, category, description, input_schema,
                handler_code, author, is_verified=False) -> "ToolRegistry":
    """创建注册表工具（含全部静态检查与重名防御）。失败抛 RegistryValidationError。"""
    from .models import ToolRegistry, ToolType
    check_tool_name(tool_name)
    check_description(description)
    check_input_schema(input_schema)
    check_handler_code(handler_code)
    if tool_type not in ToolType.values:
        raise RegistryValidationError("工具类型非法")
    if ToolRegistry.objects.filter(tool_name=tool_name).exists():
        raise RegistryValidationError(f"工具名 {tool_name} 已存在")
    if tool_name in _builtin_tool_names():
        raise RegistryValidationError(f"工具名 {tool_name} 与内置工具重名")
    return ToolRegistry.objects.create(
        tool_name=tool_name, tool_type=tool_type, category=(category or "")[:MAX_CATEGORY],
        description=description, input_schema=input_schema, handler_code=handler_code,
        author=author, is_verified=bool(is_verified), is_active=False, version=1,
    )


# ── 市场侧服务（Phase 3.1 收尾：审核流 / 复制式安装 / 评分）─────────────────


class ReviewStateError(ValueError):
    """审核动作与条目当前状态不兼容（如 reject 已 verified 条目）。"""


def review_action(tool: "ToolRegistry", *, actor, action: str, note: str = "") -> "ToolReviewLog":
    """审核动作（design D2）：状态迁移 + append-only 审计 + 缓存失效。

    - VERIFY：note 可选；REJECT：note 必填且仅未 verified 条目；
    - REVOKE：note 必填，清 verified + 停用（下架）；
    - SUBMIT：作者送审表达意图（note 可选），不改条目状态。
    """
    from django.db import transaction
    from .models import ToolRegistry, ToolReviewAction, ToolReviewLog
    note = note or ""
    if len(note) > 500:
        raise RegistryValidationError("审核附注超过 500 字上限")
    if action in (ToolReviewAction.REJECT, ToolReviewAction.REVOKE) and not note.strip():
        raise RegistryValidationError(f"{action} 必须填写附注（驳回/下架原因须可达作者）")
    with transaction.atomic():
        locked = ToolRegistry.objects.select_for_update().filter(pk=tool.pk).first()
        if locked is None:
            raise RegistryValidationError("工具不存在")
        if action == ToolReviewAction.REJECT and locked.is_verified:
            raise ReviewStateError("已验证条目不能驳回（已上架条目请用下架）")
        if action == ToolReviewAction.REJECT:
            ToolRegistry.objects.filter(pk=locked.pk).update(is_active=False)
        elif action == ToolReviewAction.REVOKE:
            if not locked.is_verified:
                raise ReviewStateError("未验证条目无需下架（未上架请用驳回）")
            ToolRegistry.objects.filter(pk=locked.pk).update(is_verified=False, is_active=False)
        elif action == ToolReviewAction.VERIFY:
            ToolRegistry.objects.filter(pk=locked.pk).update(is_verified=True)
        _cache.invalidate(locked.tool_name)
        return ToolReviewLog.objects.create(
            target=locked, actor=actor, action=action, note=note)


def pending_review_tools():
    """审核队列（design D2）：最新动作为 SUBMIT 且其后无 VERIFY/REJECT/REVOKE。"""
    from .models import ToolReviewAction, ToolReviewLog, ToolRegistry
    latest = (ToolReviewLog.objects.filter(target_id=OuterRef("pk"))
              .order_by("-id").values("action")[:1])
    return ToolRegistry.objects.annotate(latest_action=latest).filter(
        latest_action=ToolReviewAction.SUBMIT)


def latest_review_note(tool):
    """条目最新的 REJECT/REVOKE 附注（驳回原因回显给作者；无则 (None, None)）。"""
    from .models import ToolReviewAction, ToolReviewLog
    log = tool.review_logs.filter(
        action__in=[ToolReviewAction.REJECT, ToolReviewAction.REVOKE]
    ).order_by("-id").first()
    return (log.action, log.note) if log else (None, None)


def install_tool(source: "ToolRegistry", *, user):
    """复制式安装（design D1）：市场条目 → 用户本地 CUSTOM 副本。

    返回 (副本, created)：created=False 表示同源已装、幂等返回既有副本。
    副本名 = f"{source.tool_name}__u{user.pk}"（tool_name 全局唯一，
    副本与源必须分名；后缀确定性且按用户隔离，可自行 PATCH 改名）。
    - 与用户自建同名条目冲突抛 RegistryValidationError（调用方转 409）；
    - 副本默认停用、独立生命周期；源后续变更不追溯（复制式语义）。
    """
    from .models import ToolRegistry, ToolType
    if not (source.tool_type == ToolType.OFFICIAL and source.is_verified):
        raise RegistryValidationError("仅市场（verified OFFICIAL）条目可安装")
    existing = ToolRegistry.objects.filter(
        author=user, installed_from=source).first()
    if existing is not None:
        return existing, False
    # 全局唯一下的副本命名：截断前缀保证 slug 长度合法（≤50）
    suffix = f"__u{user.pk}"
    copy_name = source.tool_name[:50 - len(suffix)] + suffix
    if ToolRegistry.objects.filter(tool_name=copy_name).exists():
        raise RegistryValidationError(
            f"你已有名为 {copy_name} 的工具，请先改名或删除后再安装")
    record = ToolRegistry.objects.create(
        tool_name=copy_name, tool_type=ToolType.CUSTOM,
        category=source.category, description=source.description,
        input_schema=source.input_schema, handler_code=source.handler_code,
        author=user, is_verified=False, is_active=False, version=1,
        installed_from=source,
    )
    return record, True


def rate_tool(tool: "ToolRegistry", *, user, rating: int, comment: str = ""):
    """评分 upsert（design D3）：(tool, user) 唯一，重复评分全量覆盖。

    自评与非可见工具由调用方（视图）校验（需要 request 语境），此处只做
    数值与长度校验 + 落库。
    """
    from .models import ToolRating
    if not isinstance(rating, int) or isinstance(rating, bool) or not (1 <= rating <= 5):
        raise RegistryValidationError("评分须为 1-5 的整数")
    comment = comment or ""
    if len(comment) > 200:
        raise RegistryValidationError("评论超过 200 字上限")
    obj, _ = ToolRating.objects.update_or_create(
        tool=tool, user=user,
        defaults={"rating": rating, "comment": comment},
    )
    return obj


# ── 文档自动生成（design D5：确定性 Markdown 渲染，零 LLM）───────────────────

_SAMPLE_VALUES = {
    "string": "text",
    "number": 0,
    "integer": 0,
    "boolean": True,
    "array": [],
    "object": {},
}


def _sample_for(prop: dict):
    """按 schema 属性生成最小示例值（无类型按 object 兜底）。"""
    t = prop.get("type")
    if isinstance(t, list):
        t = t[0] if t else None
    return _SAMPLE_VALUES.get(t, "text")


def render_docs(record) -> str:
    """从工具元数据确定性生成 Markdown 使用文档（design D5）。

    含：标题/元信息行/description/参数表（name/type/required）/最小示例 args/
    沙箱边界说明段。不含 handler 源码（注入面与源码保护语义一致）。零 LLM。
    """
    schema = record.input_schema if isinstance(record.input_schema, dict) else {}
    props = schema.get("properties") or {}
    required = [r for r in schema.get("required", []) if isinstance(r, str)]
    lines = [
        f"# 工具：`{record.tool_name}`",
        "",
        f"- 分类：{record.category or '（无）'}",
        f"- 类型：{record.tool_type} · 版本 v{record.version}"
        f" · {'已验证' if record.is_verified else '未验证'}",
        "",
        "## 描述",
        "",
        record.description,
        "",
    ]
    if props:
        lines += ["## 参数", "",
                  "| 名称 | 类型 | 必填 |",
                  "|---|---|---|"]
        for name, prop in props.items():
            t = prop.get("type", "-") if isinstance(prop, dict) else "-"
            if isinstance(t, list):
                t = "|".join(str(x) for x in t)
            mark = "是" if name in required else "否"
            lines.append(f"| `{name}` | {t} | {mark} |")
        lines.append("")
        sample = {name: _sample_for(prop if isinstance(prop, dict) else {})
                  for name, prop in props.items()}
        lines += ["## 示例入参", "",
                  "```json",
                  json.dumps(sample, ensure_ascii=False, indent=2),
                  "```", ""]
    else:
        lines += ["## 参数", "",
                  "本工具无需入参（`args` 传空对象即可）。", ""]
    lines += [
        "## 执行边界",
        "",
        "- 在隔离子进程中执行：仅白名单 import、无网络/文件/数据库访问；",
        f"- 墙钟超时与输出上限见部署配置（默认 {int(_sandbox_timeout())}s / "
        f"{_sandbox_output_limit() // 1024}KB）；",
        "- handler 源码不随本文档输出（源码仅作者与平台管理员可读）。",
        "",
    ]
    return "\n".join(lines)


def update_tool(record: "ToolRegistry", *, expected_version: int, **fields) -> "ToolRegistry":
    """更新注册表工具（乐观并发 + 全量静态检查，失败整体拒绝不留半更新）。"""
    from .models import ToolRegistry
    with transaction.atomic():
        locked = ToolRegistry.objects.select_for_update().filter(pk=record.pk).first()
        if locked is None:
            raise RegistryValidationError("工具不存在")
        if locked.version != expected_version:
            raise RegistryValidationError(
                f"版本冲突：期望 {expected_version}，当前 {locked.version}")
        new_name = fields.pop("tool_name", locked.tool_name)
        if new_name != locked.tool_name:
            check_tool_name(new_name)
            if ToolRegistry.objects.filter(tool_name=new_name).exclude(pk=locked.pk).exists():
                raise RegistryValidationError(f"工具名 {new_name} 已存在")
            if new_name in _builtin_tool_names():
                raise RegistryValidationError(f"工具名 {new_name} 与内置工具重名")
            locked.tool_name = new_name
        if "description" in fields:
            check_description(fields["description"])
            locked.description = fields["description"]
        if "category" in fields:
            locked.category = (fields["category"] or "")[:MAX_CATEGORY]
        if "input_schema" in fields:
            check_input_schema(fields["input_schema"])
            locked.input_schema = fields["input_schema"]
        if "handler_code" in fields:
            check_handler_code(fields["handler_code"])
            locked.handler_code = fields["handler_code"]
        locked.version += 1
        locked.save()
        _cache.invalidate(locked.tool_name)
        return locked


# ── 可见性与动态加载（D2/D4）────────────────────────────────────────────────

def visible_tools(user):
    """对 user 可见的工具记录（is_active 且（OFFICIAL+verified 或 author=user））。"""
    from django.db.models import Q
    from .models import ToolRegistry, ToolType
    if not _enabled():
        return ToolRegistry.objects.none()
    if user is None or not getattr(user, "is_authenticated", False):
        return ToolRegistry.objects.none()
    q = Q(is_active=True) & (
        Q(tool_type=ToolType.OFFICIAL, is_verified=True) | Q(author=user))
    return ToolRegistry.objects.filter(q)


def _enabled() -> bool:
    return bool(getattr(settings, "ENABLE_TOOL_REGISTRY", False))


def _cache_ttl() -> float:
    return float(getattr(settings, "REGISTRY_CACHE_TTL", 60))


class _SpecCache:
    """(tool_name, version) -> 工具 spec 视图。进程内 TTL 短缓存。"""

    def __init__(self):
        self._entries = {}  # name -> (expires_at, version, payload)

    def get(self, name, record):
        entry = self._entries.get(name)
        now = time.monotonic()
        if entry is not None and entry[0] > now and entry[1] == record.version:
            return entry[2]
        payload = self._build(record)
        self._entries[name] = (now + _cache_ttl(), record.version, payload)
        return payload

    def invalidate(self, name):
        self._entries.pop(name, None)

    @staticmethod
    def _build(record):
        if len(record.handler_code.encode("utf-8")) > MAX_CODE_BYTES:
            raise RegistryValidationError("handler 源码超长")
        check_handler_code(record.handler_code)  # 让位容错：单条失败由调用方跳过
        return {
            "name": record.tool_name,
            "description": record.description[:MAX_DESCRIPTION],
            "input_schema": record.input_schema if isinstance(record.input_schema, dict)
            else {"type": "object", "properties": {}},
            "_registry_pk": record.pk,
            "_version": record.version,
        }


_cache = _SpecCache()


def registry_specs(user):
    """发给模型的注册表工具列表（不含 handler）。单条失败跳过并记日志（D2 容错）。"""
    specs = []
    for record in visible_tools(user):
        try:
            specs.append(_cache.get(record.tool_name, record))
        except Exception:  # noqa: BLE001 — 坏条目不拖垮轮次（对齐 skills 容错）
            logger.exception("注册表工具 %s 读取失败，已跳过", record.tool_name)
    return specs


def get_registry_tool(name, user):
    """按名取对 user 可见的工具记录（含 handler_code）。不可见返回 None。

    内置优先：调用方（run_tool）已先查内置；此处再挡一次撞名，注册表让位内置。
    """
    if not _enabled() or name in _builtin_tool_names():
        return None
    return visible_tools(user).filter(tool_name=name).first()


# ── 沙箱执行（D3）────────────────────────────────────────────────────────────

class SandboxError(Exception):
    """沙箱执行失败（超时/崩溃/输出超限/结果不可序列化）。确定性，不重试。"""


def run_in_sandbox(record, args: dict, *, timeout_s=None, output_limit=None) -> dict:
    """子进程执行 handler。返回结果 dict；失败抛 SandboxError。

    子进程：python -I bootstrap -，stdin 收 {handler_code, args}。
    env 清空（-I 忽略 PYTHON* 但显式更稳）、cwd 为空临时目录。
    """
    if not SANDBOX_BOOTSTRAP.exists():
        raise SandboxError("沙箱 bootstrap 缺失")
    timeout_s = timeout_s if timeout_s is not None else _sandbox_timeout()
    output_limit = output_limit if output_limit is not None else _sandbox_output_limit()
    payload = json.dumps({"handler_code": record.handler_code, "args": args},
                         ensure_ascii=False)
    try:
        proc = subprocess.run(
            [sys.executable, "-I", str(SANDBOX_BOOTSTRAP), "-"],
            input=payload, capture_output=True, text=True, timeout=timeout_s,
            env={"PYTHONHASHSEED": "0"}, cwd=temp_dir(),
        )
    except subprocess.TimeoutExpired as exc:
        raise SandboxError(f"沙箱执行超时（>{timeout_s}s），已终止") from exc
    except OSError as exc:
        raise SandboxError(f"沙箱进程启动失败：{exc}") from exc
    if proc.returncode != 0:
        reason = (proc.stderr or proc.stdout or "").strip().splitlines()
        raise SandboxError(f"沙箱执行失败：{reason[-1][:300] if reason else '无输出'}")
    stdout = proc.stdout
    if len(stdout.encode("utf-8")) > output_limit:
        raise SandboxError(f"沙箱输出超过 {output_limit} 字节上限")
    lines = [ln for ln in stdout.strip().splitlines() if ln.strip()]
    if not lines:
        raise SandboxError("沙箱无结果输出")
    try:
        result = json.loads(lines[-1])
    except (json.JSONDecodeError, ValueError) as exc:
        raise SandboxError("沙箱结果不是合法 JSON") from exc
    if not isinstance(result, dict):
        raise SandboxError("沙箱结果须为 JSON 对象")
    return result


def _sandbox_timeout() -> float:
    return float(getattr(settings, "SANDBOX_TIMEOUT_S", 5))


def sandbox_try_timeout() -> float:
    return float(getattr(settings, "SANDBOX_TRY_TIMEOUT_S", 2))


def _sandbox_output_limit() -> int:
    return int(getattr(settings, "SANDBOX_OUTPUT_LIMIT", 65536))


_TEMP_DIR = None


def temp_dir() -> str:
    """沙箱子进程 cwd：空临时目录（懒建，进程级复用）。"""
    global _TEMP_DIR
    if _TEMP_DIR is None:
        import tempfile
        _TEMP_DIR = tempfile.mkdtemp(prefix="tool_sandbox_")
    return _TEMP_DIR
