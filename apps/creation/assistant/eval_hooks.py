"""助手 eval 钩子框架（assistant/eval-hooks）。

在轮次关键节点（turn_end / proposal_created）触发已注册的评估函数。
评估失败绝不阻断主流程：单个函数异常被吞掉记为 ok=false，其余照常执行。
本期只交付框架，默认注册表为空；指标后续按需 register。
"""
import logging

from . import events

logger = logging.getLogger(__name__)

# 触发点常量
TURN_END = "turn_end"
PROPOSAL_CREATED = "proposal_created"

_POINTS = (TURN_END, PROPOSAL_CREATED)

# point -> [ (name, fn) ]
_REGISTRY = {p: [] for p in _POINTS}


def register(point, name=None):
    """注册评估函数。可作装饰器：@register(TURN_END, "my_eval")。

    fn(context: dict) -> dict | None
    context 含 {session, record, trace_recorder, proposals}。
    返回值（dict）作为该次评估的 detail；返回 None 表示无附加信息。
    """
    if point not in _REGISTRY:
        raise ValueError(f"未知 eval 触发点 {point}；可选: {', '.join(_POINTS)}")

    def deco(fn):
        _REGISTRY[point].append((name or fn.__name__, fn))
        return fn

    return deco


def registered(point):
    return list(_REGISTRY.get(point, []))


def run_evals(point, session_id, context):
    """执行某触发点的全部评估函数 -> list[result]。

    单个函数异常被吞掉记为 ok=false；绝不抛出、绝不阻断主流程。
    结果同时写入 trace_recorder 并 emit eval 事件。
    """
    recorder = context.get("trace_recorder")
    results = []
    for fname, fn in _REGISTRY.get(point, []):
        entry = {"name": fname, "ok": False, "detail": None}
        try:
            out = fn(context)
            entry["ok"] = True
            entry["detail"] = out
        except Exception as exc:  # noqa: BLE001 — eval 失败不阻断主流程
            logger.warning("eval 钩子 %s 在 %s 执行失败: %s", fname, point, exc)
            entry["detail"] = {"error": str(exc)[:300]}
        results.append(entry)

    if results and recorder is not None:
        payload = {"point": point, "results": results}
        recorder.add_eval(payload)
        events.emit(session_id, "eval", payload)
    return results
