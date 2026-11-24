"""轮次级 trace 与 span（assistant/trace）。

一次轮次 = 一个 trace。span 为扁平列表 + parent_id（便于序列化与增量追加）。
零新增依赖；trace 最终由 runner 写入 TaskRecord.output_summary["trace"]。
token/费用归因：model_call span 记 usage/cost 与 triggered_by_tool，可定位
「哪个工具的结果导致下一次模型调用 token 膨胀」。
"""
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone as _tz

from . import events


def _iso(ts):
    return datetime.fromtimestamp(ts, _tz.utc).isoformat()


class TraceRecorder:
    """收集一次轮次的所有 span，finish() 计算 totals 并返回可序列化 trace。"""

    def __init__(self, session_id):
        self.session_id = session_id
        self.trace_id = str(uuid.uuid4())
        self._spans = []
        self._seq = 0
        self._turn_span = None
        self._turn_start = None
        # 归因：记录最近一个工具名，供紧随其后的 model_call 标 triggered_by_tool
        self.last_tool = None
        self.evals = []

    def _next_id(self):
        self._seq += 1
        return f"s{self._seq}"

    @property
    def turn_span_id(self):
        return self._turn_span

    def start_turn(self, name="assistant_turn"):
        self._turn_span = self._next_id()
        self._turn_start = time.time()
        self._pending = {
            "span_id": self._turn_span, "parent_id": None, "kind": "turn", "name": name,
            "started_at": _iso(self._turn_start), "attributes": {},
        }
        return self._turn_span

    @contextmanager
    def span(self, kind, name, parent_id=None, attributes=None):
        """上下文管理器：yield 出 span dict，调用方可就地补 attributes（如 usage/cost）。"""
        span_id = self._next_id()
        started = time.time()
        span = {
            "span_id": span_id, "parent_id": parent_id or self._turn_span, "kind": kind,
            "name": name, "started_at": _iso(started), "attributes": dict(attributes or {}),
        }
        status = "ok"
        try:
            yield span
        except Exception:
            status = "error"
            raise
        finally:
            span["duration_ms"] = int(round((time.time() - started) * 1000))
            span["status"] = status
            self._spans.append(span)
            self._emit_span(span)

    def _emit_span(self, span):
        # 只推送 model_call / tool_call 关键 span；失败只记日志（events.emit 已保证）
        if span["kind"] not in ("model_call", "tool_call"):
            return
        events.emit(self.session_id, "span", {
            "trace_id": self.trace_id, "span_id": span["span_id"],
            "parent_id": span["parent_id"], "kind": span["kind"], "name": span["name"],
            "duration_ms": span.get("duration_ms"), "status": span["status"],
            "attributes": span["attributes"],
        })

    def add_eval(self, result):
        self.evals.append(result)

    def _totals(self):
        model_calls = tool_calls = tool_retries = tool_repairs = 0
        input_tokens = output_tokens = 0
        cost = 0.0
        for s in self._spans:
            a = s.get("attributes", {})
            if s["kind"] == "model_call":
                model_calls += 1
                input_tokens += int(a.get("input_tokens", 0) or 0)
                output_tokens += int(a.get("output_tokens", 0) or 0)
                try:
                    cost += float(a.get("cost", 0) or 0)
                except (TypeError, ValueError):
                    pass
            elif s["kind"] == "tool_call":
                tool_calls += 1
                tool_retries += int(a.get("retries", 0) or 0)
                if a.get("repaired"):
                    tool_repairs += 1
        return {
            "model_calls": model_calls, "tool_calls": tool_calls,
            "tool_retries": tool_retries, "tool_repairs": tool_repairs,
            "input_tokens": input_tokens, "output_tokens": output_tokens,
            "cost": f"{cost:.6f}",
        }

    def finish(self, outcome):
        if self._pending is not None:
            span = self._pending
            span["duration_ms"] = int(round((time.time() - self._turn_start) * 1000))
            span["status"] = "ok" if outcome == "done" else outcome
            self._spans.insert(0, span)  # turn span 放最前
            self._pending = None
        return {
            "trace_id": self.trace_id,
            "started_at": _iso(self._turn_start) if self._turn_start else None,
            "finished_at": _iso(time.time()),
            "outcome": outcome,
            "totals": self._totals(),
            "spans": self._spans,
            "evals": self.evals,
        }

    # 兼容 start_turn 里未初始化 _pending 的情形
    _pending = None
