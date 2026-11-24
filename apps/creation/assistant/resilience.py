"""工具调用韧性层（assistant/session）。

在 agent 循环内为单次工具调用提供 harness 级韧性：
- 错误分类：TRANSIENT（可重试）/ VALIDATION（可修复）/ PERMANENT（确定性）/ UNKNOWN
- 确定性重试：仅 TRANSIENT 按指数退避重试，受单工具与全局尝试上限约束
- 参数自动修复：VALIDATION 先做一次确定性修复再重校验，不盲目重试
- 降级备用工具：主工具 VALIDATION/PERMANENT 失败时尝试 fallback（防环）
- 硬终止：所有 continue 路径都消耗单调递增的计数器，必然在有限步内终止

核心不变量：不存在无限调用。重试/修复/降级均受 max_tool_attempts（单工具）与
max_total_tool_attempts（整轮全局）两个单调递增计数器硬约束。
"""
import logging
import time

from django.db import OperationalError
from django.db import transaction

from . import events
from .tools import SchemaValidationError, ToolError, get_tool, validate

logger = logging.getLogger(__name__)

# ── 错误分类 ────────────────────────────────────────────────────────────────

TRANSIENT = "TRANSIENT"      # 瞬时故障：重试可能成功
VALIDATION = "VALIDATION"    # 入参校验失败：确定性，先修复不重试
PERMANENT = "PERMANENT"      # 确定性业务错误：不重试
UNKNOWN = "UNKNOWN"          # 未识别异常：保守终止，避免无限


def _run_registry(executor, ctx, name, raw_input):
    """注册表工具执行回退（registry D2/D3）。返回 (result, is_error, meta) 或 None（不存在）。

    沙箱超时/崩坏/输出超限（SandboxError）归 PERMANENT 不重试——确定性失败；
    入参校验失败复用 VALIDATION 语义（但不重试：沙箱工具无 fallback）。
    """
    try:
        from . import registry
    except Exception:  # noqa: BLE001
        return None
    if not registry._enabled():
        return None
    try:
        record = registry.get_registry_tool(
            name, getattr(ctx, "user", None) or getattr(ctx.session, "user", None))
        if record is None:
            return None
    except Exception:  # noqa: BLE001 — 查询失败按未知工具处理，不击穿轮次
        logger.exception("注册表工具 %s 查询失败", name)
        return None
    meta = {"attempts": 1, "retries": 0, "repaired": False, "error_class": None,
            "fallback_chain": [], "terminated": False, "stop_reason": None}
    if executor.global_attempts >= executor.max_total:
        return executor._terminate(meta, name, None, "达到整轮全局工具尝试预算",
                                   stop=STOP_GLOBAL_BUDGET)
    executor.global_attempts += 1
    schema = record.input_schema if isinstance(record.input_schema, dict) \
        else {"type": "object", "properties": {}}
    try:
        args = validate(schema, raw_input if raw_input is not None else {})
    except SchemaValidationError as exc:
        meta["error_class"] = VALIDATION
        return {"error": f"入参校验失败：{exc}"}, True, meta
    except Exception as exc:  # noqa: BLE001
        meta["error_class"] = VALIDATION
        return {"error": f"入参校验失败：{exc}"}, True, meta
    try:
        return registry.run_in_sandbox(record, args), False, meta
    except registry.SandboxError as exc:
        meta["error_class"] = PERMANENT
        return {"error": f"工具 {name} 执行失败：{exc}"}, True, meta
    except Exception as exc:  # noqa: BLE001
        logger.exception("注册表工具 %s 执行失败", name)
        meta["error_class"] = UNKNOWN
        return {"error": f"工具 {name} 执行失败，请换个方式或稍后重试"}, True, meta# 终止原因（用于 span / 事件 / 错误结果）
STOP_MAX_ATTEMPTS = "MAX_TOOL_ATTEMPTS"
STOP_GLOBAL_BUDGET = "GLOBAL_TOOL_ATTEMPTS"
STOP_CLASS = "ERROR_CLASS"   # 因某类确定性错误且无法修复/降级而终止


def _transient_exceptions():
    exc = [OperationalError]
    try:
        import httpx
        exc.append(httpx.TransportError)  # 含 TimeoutException / NetworkError
    except Exception:  # noqa: BLE001
        pass
    return tuple(exc)


def classify(exc):
    """把异常归为 TRANSIENT / VALIDATION / PERMANENT / UNKNOWN。"""
    try:
        from .registry import SandboxError
        if isinstance(exc, SandboxError):
            return PERMANENT  # 沙箱超时/崩坏/超限：确定性失败，不重试
    except Exception:  # noqa: BLE001
        pass
    if isinstance(exc, SchemaValidationError):
        return VALIDATION
    if isinstance(exc, _transient_exceptions()):
        return TRANSIENT
    if isinstance(exc, ToolError):
        # handler 内的业务 ToolError（未找到 / 权限 / 确定性校验）→ 确定性，不重试
        return PERMANENT
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status is not None:
        if status == 429 or status >= 500:
            return TRANSIENT
        if 400 <= status < 500:
            return PERMANENT
    return UNKNOWN


# ── 参数自动修复 ────────────────────────────────────────────────────────────

def _target_type(pschema):
    t = pschema.get("type")
    if isinstance(t, list):
        for cand in ("integer", "string", "array"):
            if cand in t:
                return cand
        return t[0] if t else None
    return t


def auto_repair(schema, value):
    """对入参做一次确定性修复（不猜业务语义）。返回修复后的副本，无可修复处返回 None。

    仅处理能确定性纠正的形态错误：
    - 纯数字字符串 → 整数（目标为 integer）
    - 整数值 float → int（目标为 integer）
    - 单元素数组解包（目标非 array）
    - 超长字符串按 maxLength 截断（声明 x-keep: full 的参数除外——
      内容完整性语义（如记忆全文）截断即静默丢数据，必须显式报错而非"修复"）
    修复后由调用方重新走完整 validate()，通过才采用。
    """
    if not isinstance(value, dict):
        return None
    props = schema.get("properties", {})
    out = dict(value)
    changed = False
    for k, v in list(out.items()):
        pschema = props.get(k)
        if not isinstance(pschema, dict):
            continue
        t = _target_type(pschema)
        if t == "integer":
            # phase1 修复 8.1：int() 置于 try 内、成功即采用——去掉 lstrip("-")
            # 预判（旧实现 "--5" 通过 isdigit 预判但 int() 抛 ValueError，
            # 未捕获 → 击穿整个轮次）
            if isinstance(v, str):
                try:
                    out[k] = int(v.strip())
                    changed = True
                    continue
                except ValueError:
                    pass
            if isinstance(v, float) and v.is_integer():
                out[k] = int(v)
                changed = True
                continue
        if t and t != "array" and isinstance(v, list) and len(v) == 1:
            out[k] = v[0]
            changed = True
            continue
        if t == "string" and isinstance(v, str) and "maxLength" in pschema \
                and len(v) > pschema["maxLength"] \
                and pschema.get("x-keep") != "full":
            out[k] = v[:pschema["maxLength"]]
            changed = True
            continue
    return out if changed else None


def _sleep(seconds):
    time.sleep(seconds)


def _backoff(base_ms, retry_index):
    """指数退避：base × 2^(retry_index-1)，retry_index 从 1 起。"""
    return max(0.0, base_ms / 1000.0) * (2 ** max(0, retry_index - 1))


def _args_summary(raw_input):
    """入参摘要（截断长字符串），用于 span 展示，不回传模型。"""
    if not isinstance(raw_input, dict):
        return {}
    out = {}
    for k, v in raw_input.items():
        if isinstance(v, str) and len(v) > 80:
            out[k] = v[:80] + "…"
        else:
            out[k] = v
    return out


class ToolExecutor:
    """一次轮次内复用，持有整轮全局工具尝试计数（跨多次工具调用累加）。"""

    def __init__(self, budget, session_id):
        self.max_tool_attempts = int(budget.get("max_tool_attempts", 3))
        self.max_total = int(budget.get("max_total_tool_attempts", 0)) or \
            int(budget.get("max_tool_calls", 20)) * 2
        self.backoff_ms = int(budget.get("tool_retry_backoff_ms", 250))
        self.session_id = session_id
        self.global_attempts = 0

    # ── 对外入口 ────────────────────────────────────────────────────────────
    def run(self, ctx, name, raw_input):
        """执行工具 -> (result_dict, is_error, meta)。

        meta: {attempts, retries, repaired, error_class, fallback_chain, terminated, stop_reason}
        永不抛异常；所有失败都以 is_error 结果返回给模型。
        """
        meta = {"attempts": 0, "retries": 0, "repaired": False, "error_class": None,
                "fallback_chain": [], "terminated": False, "stop_reason": None}
        tried = set()
        current = name

        while True:
            spec = get_tool(current)
            if spec is None:
                # 注册表工具回退（registry D2）：内置未命中 → 沙箱执行。
                # 沙箱失败（超时/崩坏/超限）为 SandboxError → classify 归 PERMANENT 不重试。
                reg = _run_registry(self, ctx, current, raw_input)
                if reg is not None:
                    return reg
                return self._terminate(meta, name, PERMANENT, "未知工具")

            # ── 硬上限：全局预算 ──
            if self.global_attempts >= self.max_total:
                return self._terminate(meta, name, None, "达到整轮全局工具尝试预算",
                                       stop=STOP_GLOBAL_BUDGET)
            # ── 硬上限：单工具尝试 ──
            if meta["attempts"] >= self.max_tool_attempts:
                fb = self._switch_fallback(current, tried, meta)
                if fb is None:
                    return self._terminate(meta, name, meta["error_class"],
                                           "达到单工具尝试上限", stop=STOP_MAX_ATTEMPTS)
                current = fb
                continue

            self.global_attempts += 1
            meta["attempts"] += 1

            # ── 校验（含一次自动修复）──
            args, verr, did_repair = self._validate(spec, raw_input)
            if did_repair:
                meta["repaired"] = True
            if args is None:
                # VALIDATION 且不可修复
                meta["error_class"] = VALIDATION
                fb = self._switch_fallback(current, tried, meta)
                if fb is None:
                    return self._terminate(meta, name, VALIDATION, f"入参校验失败：{verr}",
                                           stop=STOP_CLASS)
                current = fb
                continue

            # ── 执行 ──
            try:
                with transaction.atomic():
                    result = spec["handler"](ctx, **args)
                meta["error_class"] = None
                return result, False, meta
            except Exception as exc:  # noqa: BLE001
                klass = classify(exc)
                meta["error_class"] = klass
                if klass == TRANSIENT and meta["attempts"] < self.max_tool_attempts \
                        and self.global_attempts < self.max_total:
                    meta["retries"] += 1
                    _sleep(_backoff(self.backoff_ms, meta["retries"]))
                    continue
                # PERMANENT / VALIDATION / UNKNOWN / 重试耗尽 → 降级或终止
                fb = self._switch_fallback(current, tried, meta)
                if fb is None:
                    return self._terminate(meta, name, klass, str(exc)[:300], stop=STOP_CLASS)
                current = fb
                continue

    # ── 内部 ────────────────────────────────────────────────────────────────
    def _validate(self, spec, raw_input):
        """校验入参；VALIDATION 时尝试一次自动修复。返回 (args, error_str, repaired)。

        phase1 修复 8.2：修复阶段的任何异常（如畸形入参触发 auto_repair
        内部错误）都被捕获并按校验失败处理——ToolExecutor.run 对任意
        畸形入参不抛异常，轮次不被击穿。"""
        schema = spec["input_schema"]
        normalized = raw_input if raw_input is not None else {}
        try:
            return validate(schema, normalized), None, False
        except SchemaValidationError as exc:
            err = str(exc)
        except Exception as exc:  # noqa: BLE001 — validate 本身异常按校验失败处理
            logger.warning("入参校验内部异常，按校验失败处理: %s", exc)
            err = f"入参校验失败：{exc}"
        try:
            fixed = auto_repair(schema, normalized)
        except Exception as exc:  # noqa: BLE001 — 修复阶段异常按校验失败处理
            logger.warning("入参自动修复异常，按校验失败处理: %s", exc)
            return None, err, False
        if fixed is not None:
            try:
                return validate(schema, fixed), None, True
            except SchemaValidationError:
                return None, err, False
            except Exception as exc:  # noqa: BLE001
                logger.warning("修复后校验内部异常，按校验失败处理: %s", exc)
                return None, err, False
        return None, err, False

    def _switch_fallback(self, current, tried, meta):
        """切到备用工具（防环）；无可用的返回 None。重置单工具计数但保留全局计数。"""
        fb = (get_tool(current) or {}).get("fallback")
        if not fb or fb in tried or get_tool(fb) is None:
            return None
        tried.add(current)
        tried.add(fb)
        meta["fallback_chain"].append(fb)
        meta["attempts"] = 0  # 备用工具是新的调用，重置单工具计数
        # 全局计数 self.global_attempts 不重置（仍受整轮预算约束）
        return fb

    def _terminate(self, meta, name, error_class, reason, stop=STOP_CLASS):
        meta["terminated"] = True
        meta["stop_reason"] = stop
        if error_class is not None:
            meta["error_class"] = error_class
        klass = meta.get("error_class") or UNKNOWN
        logger.warning("助手工具 %s 终止（%s / %s）：%s", name, klass, stop, reason)
        events.emit(self.session_id, "tool_error", {
            "tool": name, "error_class": klass, "stop_reason": stop,
            "reason": reason[:300], "attempts": meta["attempts"], "retries": meta["retries"],
            "repaired": meta["repaired"], "fallback_chain": meta["fallback_chain"],
        })
        result = {"error": reason, "error_class": klass}
        return result, True, meta
