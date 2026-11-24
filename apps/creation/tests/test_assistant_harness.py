"""助手 harness 韧性与可观测性测试（openspec change: backend-assistant-harness-resilience）。

覆盖：trace/span 与 token 费用归因、错误分类、确定性重试、参数自动修复、降级备用工具、
硬终止条件、eval 钩子。
"""
from unittest.mock import patch

from django.test import TestCase
from rest_framework.test import APITestCase

from apps.creation.assistant import events, eval_hooks, resilience
from apps.creation.assistant import tools
from apps.creation.assistant.models import AssistantSession
from apps.creation.assistant.resilience import (
    PERMANENT, TRANSIENT, UNKNOWN, VALIDATION, ToolExecutor, auto_repair, classify,
)
from apps.creation.assistant.tools import SchemaValidationError, ToolError
from apps.creation.models import (
    MemberRole, MemberStatus, PlayMember, TaskRecord, TaskStatus, TaskType,
)
from dvadmin.system.models import CustomUser

from .test_assistant import AssistantFixtureMixin, FakeChatGateway, _say, _use


# ── 错误分类 ────────────────────────────────────────────────────────────────

class ClassifyTests(TestCase):
    def test_transient(self):
        import httpx
        from django.db import OperationalError
        self.assertEqual(classify(httpx.TimeoutException("t")), TRANSIENT)
        self.assertEqual(classify(httpx.ConnectError("c")), TRANSIENT)
        self.assertEqual(classify(OperationalError("db down")), TRANSIENT)

    def test_validation(self):
        self.assertEqual(classify(SchemaValidationError("缺参数")), VALIDATION)

    def test_permanent(self):
        self.assertEqual(classify(ToolError("第 3 集不存在")), PERMANENT)
        import httpx
        req = httpx.Request("POST", "https://x")
        err = httpx.HTTPStatusError("404", request=req,
                                    response=httpx.Response(404, request=req))
        self.assertEqual(classify(err), PERMANENT)

    def test_5xx_and_429_transient(self):
        import httpx
        req = httpx.Request("POST", "https://x")
        for code in (429, 500, 503):
            err = httpx.HTTPStatusError(str(code), request=req,
                                        response=httpx.Response(code, request=req))
            self.assertEqual(classify(err), TRANSIENT, code)

    def test_unknown(self):
        self.assertEqual(classify(ValueError("boom")), UNKNOWN)


# ── 参数自动修复 ────────────────────────────────────────────────────────────

class AutoRepairTests(TestCase):
    def test_string_digit_to_int(self):
        schema = {"type": "object", "properties": {"scene_id": {"type": "integer"}}}
        self.assertEqual(auto_repair(schema, {"scene_id": "12"}), {"scene_id": 12})

    def test_float_to_int(self):
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
        self.assertEqual(auto_repair(schema, {"n": 3.0}), {"n": 3})

    def test_single_element_array_unpack(self):
        schema = {"type": "object", "properties": {"scene_id": {"type": "integer"}}}
        self.assertEqual(auto_repair(schema, {"scene_id": [7]}), {"scene_id": 7})

    def test_truncate_overlong_string(self):
        schema = {"type": "object", "properties": {"s": {"type": "string", "maxLength": 5}}}
        self.assertEqual(auto_repair(schema, {"s": "abcdefghij"}), {"s": "abcde"})

    def test_no_repair_returns_none(self):
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
        self.assertIsNone(auto_repair(schema, {"n": "abc"}))  # 非数字字符串无法修
        self.assertIsNone(auto_repair(schema, {"x": 1}))      # 未声明字段不动


# ── 韧性状态机 ──────────────────────────────────────────────────────────────

def _budget(**kw):
    b = {"max_tool_attempts": 3, "max_total_tool_attempts": 10,
         "tool_retry_backoff_ms": 0, "max_tool_calls": 20}
    b.update(kw)
    return b


class ResilienceTests(AssistantFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        events.set_backend(events.MemoryStreams())
        self.addCleanup(events.set_backend, None)
        # 测试不真正 sleep
        self._sleep = patch.object(resilience, "_sleep", lambda s: None)
        self._sleep.start()
        self.addCleanup(self._sleep.stop)

    def executor(self, **kw):
        return ToolExecutor(_budget(**kw), self.session.pk)

    def run_tool(self, name, args, **kw):
        return self.executor(**kw).run(self.ctx(), name, args)

    def test_success_meta(self):
        result, is_error, meta = self.run_tool("get_scene", {"scene_id": self.scene.pk})
        self.assertFalse(is_error)
        self.assertEqual(meta["attempts"], 1)
        self.assertFalse(meta["repaired"])
        self.assertIsNone(meta["error_class"])

    def test_validation_auto_repair(self):
        # scene_id 传字符串数字 → 自动修复为 int，成功
        result, is_error, meta = self.run_tool("get_scene", {"scene_id": str(self.scene.pk)})
        self.assertFalse(is_error, result)
        self.assertTrue(meta["repaired"])

    def test_validation_unrepairable_terminates(self):
        # 缺必填参数 → VALIDATION，不可修复 → 终止
        result, is_error, meta = self.run_tool("get_scene", {})
        self.assertTrue(is_error)
        self.assertEqual(meta["error_class"], VALIDATION)
        self.assertTrue(meta["terminated"])
        self.assertEqual(meta["stop_reason"], resilience.STOP_CLASS)

    def test_permanent_no_retry(self):
        # 跨剧 scene_id → handler 抛 ToolError（PERMANENT），不重试直接终止
        result, is_error, meta = self.run_tool("get_scene", {"scene_id": self.foreign_scene.pk})
        self.assertTrue(is_error)
        self.assertEqual(meta["error_class"], PERMANENT)
        self.assertEqual(meta["attempts"], 1)  # 不重试

    def test_transient_retry_then_success(self):
        calls = {"n": 0}

        @tools.tool("_flaky_tr", "flaky", fallback=None)
        def _flaky(ctx):
            calls["n"] += 1
            if calls["n"] == 1:
                from django.db import OperationalError
                raise OperationalError("db blip")
            return {"ok": True}

        try:
            result, is_error, meta = self.run_tool("_flaky_tr", {})
            self.assertFalse(is_error)
            self.assertEqual(calls["n"], 2)
            self.assertEqual(meta["retries"], 1)
            self.assertEqual(meta["attempts"], 2)
        finally:
            tools._TOOLS.pop("_flaky_tr", None)

    def test_transient_retry_exhausted(self):
        @tools.tool("_flaky_ex", "flaky")
        def _flaky(ctx):
            from django.db import OperationalError
            raise OperationalError("db down")

        try:
            result, is_error, meta = self.run_tool("_flaky_ex", {}, max_tool_attempts=3)
            self.assertTrue(is_error)
            self.assertEqual(meta["error_class"], TRANSIENT)
            self.assertEqual(meta["attempts"], 3)
            self.assertEqual(meta["retries"], 2)
            self.assertTrue(meta["terminated"])
        finally:
            tools._TOOLS.pop("_flaky_ex", None)

    def test_fallback_on_permanent(self):
        @tools.tool("_primary_pf", "primary", fallback="_backup_pf")
        def _primary(ctx):
            raise ToolError("主工具确定性失败")

        @tools.tool("_backup_pf", "backup")
        def _backup(ctx):
            return {"from": "backup"}

        try:
            result, is_error, meta = self.run_tool("_primary_pf", {})
            self.assertFalse(is_error, result)
            self.assertEqual(result["from"], "backup")
            self.assertIn("_backup_pf", meta["fallback_chain"])
        finally:
            tools._TOOLS.pop("_primary_pf", None)
            tools._TOOLS.pop("_backup_pf", None)

    def test_fallback_anti_loop(self):
        # 主备互相 fallback → 每个只降级一次，不无限互降
        @tools.tool("_pa", "a", fallback="_pb")
        def _pa(ctx):
            raise ToolError("a fail")

        @tools.tool("_pb", "b", fallback="_pa")
        def _pb(ctx):
            raise ToolError("b fail")

        try:
            result, is_error, meta = self.run_tool("_pa", {})
            self.assertTrue(is_error)
            # 主 + 备各尝试一次后终止，不会无限互降
            self.assertLessEqual(meta["attempts"], 3)
            self.assertTrue(meta["terminated"])
        finally:
            tools._TOOLS.pop("_pa", None)
            tools._TOOLS.pop("_pb", None)

    def test_global_budget_bounds_single_tool(self):
        # 全局预算 3 < 单工具上限 5 → 在全局预算内有限终止，不会无限重试到单工具上限
        @tools.tool("_always_tr", "tr")
        def _always(ctx):
            from django.db import OperationalError
            raise OperationalError("down")

        try:
            ex = self.executor(max_tool_attempts=5, max_total_tool_attempts=3)
            result, is_error, meta = ex.run(self.ctx(), "_always_tr", {})
            self.assertTrue(is_error)
            self.assertTrue(meta["terminated"])
            self.assertEqual(ex.global_attempts, 3)
            self.assertLessEqual(meta["attempts"], 3)
        finally:
            tools._TOOLS.pop("_always_tr", None)

    def test_global_budget_carries_across_tool_calls(self):
        # 全局预算跨同轮的多次工具调用累加：第一个工具耗尽预算后，
        # 第二个工具在入口即被整轮预算拦停（STOP_GLOBAL_BUDGET），不产生任何新尝试
        @tools.tool("_always_tr", "tr")
        def _always(ctx):
            from django.db import OperationalError
            raise OperationalError("down")

        try:
            ex = self.executor(max_tool_attempts=5, max_total_tool_attempts=3)
            ctx = self.ctx()
            ex.run(ctx, "_always_tr", {})
            self.assertEqual(ex.global_attempts, 3)
            result, is_error, meta = ex.run(ctx, "_always_tr", {})
            self.assertTrue(is_error)
            self.assertEqual(meta["stop_reason"], resilience.STOP_GLOBAL_BUDGET)
            self.assertEqual(meta["attempts"], 0)   # 入口即停，未产生新尝试
            self.assertEqual(ex.global_attempts, 3)  # 计数不再增长
        finally:
            tools._TOOLS.pop("_always_tr", None)

    def test_unknown_tool_permanent(self):
        result, is_error, meta = self.run_tool("_nope", {})
        self.assertTrue(is_error)
        self.assertEqual(meta["error_class"], PERMANENT)

    def test_tool_specs_exclude_fallback(self):
        @tools.tool("_fb_spec", "s", fallback="_other")
        def _fb(ctx):
            return {}
        try:
            spec = next(t for t in tools.tool_specs() if t["name"] == "_fb_spec")
            self.assertNotIn("fallback", spec)
            self.assertNotIn("handler", spec)
        finally:
            tools._TOOLS.pop("_fb_spec", None)


# ── trace 归因 ──────────────────────────────────────────────────────────────

class TraceTests(AssistantFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        events.set_backend(events.MemoryStreams())
        self.addCleanup(events.set_backend, None)

    def _turn(self, gw):
        from apps.creation.assistant.runner import _save, run_assistant_turn
        record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                           input_summary={"session_id": self.session.pk})
        _save(self.session, "user", [{"type": "text", "text": "改一下"}], record)
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        with patch("apps.ai_gateway.gateway.AIGateway", return_value=gw):
            run_assistant_turn(record.pk)
        record.refresh_from_db()
        return record

    def test_trace_structure_and_attribution(self):
        gw = FakeChatGateway([
            _use("t1", "get_scene", {"scene_id": 1}),  # 故意错 id → 触发重试路径? 不，PERMANENT
            _say("完成"),
        ])
        # 让第一次工具成功：用真实 scene pk
        gw = FakeChatGateway([
            _use("t1", "get_scene", {"scene_id": self.scene.pk}),
            _say("完成"),
        ])
        record = self._turn(gw)
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        trace = record.output_summary["trace"]
        self.assertIn("trace_id", trace)
        kinds = [s["kind"] for s in trace["spans"]]
        self.assertEqual(kinds.count("model_call"), 2)
        self.assertEqual(kinds.count("tool_call"), 1)
        # model_call span 记 usage
        mcalls = [s for s in trace["spans"] if s["kind"] == "model_call"]
        self.assertTrue(all("input_tokens" in s["attributes"] for s in mcalls))
        # 第二次模型调用 triggered_by_tool 归因到 get_scene
        second = mcalls[-1]["attributes"]
        self.assertEqual(second["triggered_by_tool"], "get_scene")
        # tool_call span 记画像
        tcall = next(s for s in trace["spans"] if s["kind"] == "tool_call")
        self.assertEqual(tcall["attributes"]["tool"], "get_scene")
        self.assertFalse(tcall["attributes"]["is_error"])
        self.assertEqual(tcall["attributes"]["attempts"], 1)
        # totals 聚合
        self.assertEqual(trace["totals"]["model_calls"], 2)
        self.assertEqual(trace["totals"]["tool_calls"], 1)
        self.assertEqual(trace["totals"]["input_tokens"], 20)  # 两次各 10

    def test_turn_span_present(self):
        record = self._turn(FakeChatGateway([_say("hi")]))
        trace = record.output_summary["trace"]
        turn = trace["spans"][0]
        self.assertEqual(turn["kind"], "turn")
        self.assertIsNone(turn["parent_id"])

    def test_span_events_pushed(self):
        self._turn(FakeChatGateway([_say("hi")]))
        types = [e["type"] for e in events.read(self.session.pk)[0]]
        self.assertIn("span", types)


class TriggeredByAttributionTests(AssistantFixtureMixin, TestCase):
    """成员变更任务 5.1：写操作事件/轮次记录带 triggered_by（属主 vs 成员）。"""

    def _turn_for(self, user):
        from apps.creation.assistant.runner import _save, run_assistant_turn
        session = AssistantSession.objects.create(
            play=self.play, user=user, agent_config=self.config
        )
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.ASSISTANT_TURN,
            input_summary={"session_id": session.pk},
        )
        _save(session, "user", [{"type": "text", "text": "改一下"}], record)
        AssistantSession.objects.filter(pk=session.pk).update(running_task=record)
        with patch("apps.ai_gateway.gateway.AIGateway",
                   return_value=FakeChatGateway([_say("ok")])):
            run_assistant_turn(record.pk)
        record.refresh_from_db()
        done = next(e for e in events.read(session.pk)[0] if e["type"] == "done")
        return record, done

    def test_owner_turn_marked_owner(self):
        record, done = self._turn_for(self.producer)
        self.assertEqual(record.output_summary["triggered_by"], "owner")
        self.assertEqual(done["data"]["triggered_by"], "owner")

    def test_member_turn_marked_member(self):
        editor = CustomUser.objects.create_user(username="ae", password="Passw0rd!123")
        PlayMember.objects.create(
            play=self.play, user=editor, role=MemberRole.EDITOR,
            status=MemberStatus.ACTIVE,
        )
        record, done = self._turn_for(editor)
        self.assertEqual(record.output_summary["triggered_by"], "member")
        self.assertEqual(done["data"]["triggered_by"], "member")


# ── eval 钩子 ───────────────────────────────────────────────────────────────

class EvalHookTests(AssistantFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        events.set_backend(events.MemoryStreams())
        self.addCleanup(events.set_backend, None)

    def test_register_and_trigger_turn_end(self):
        seen = {}

        @eval_hooks.register(eval_hooks.TURN_END, "collect")
        def _collect(context):
            seen["ran"] = True
            return {"note": "ok"}

        try:
            from apps.creation.assistant.runner import _save, run_assistant_turn
            record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                               input_summary={"session_id": self.session.pk})
            _save(self.session, "user", [{"type": "text", "text": "x"}], record)
            AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
            with patch("apps.ai_gateway.gateway.AIGateway",
                       return_value=FakeChatGateway([_say("hi")])):
                run_assistant_turn(record.pk)
            self.assertTrue(seen.get("ran"))
            record.refresh_from_db()
            evals = record.output_summary["trace"]["evals"]
            self.assertTrue(any(e["point"] == eval_hooks.TURN_END for e in evals))
        finally:
            eval_hooks._REGISTRY[eval_hooks.TURN_END] = []

    def test_eval_exception_does_not_block(self):
        @eval_hooks.register(eval_hooks.TURN_END, "boom")
        def _boom(context):
            raise RuntimeError("eval 崩了")

        try:
            from apps.creation.assistant.runner import _save, run_assistant_turn
            record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                               input_summary={"session_id": self.session.pk})
            _save(self.session, "user", [{"type": "text", "text": "x"}], record)
            AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
            with patch("apps.ai_gateway.gateway.AIGateway",
                       return_value=FakeChatGateway([_say("hi")])):
                run_assistant_turn(record.pk)
            record.refresh_from_db()
            # eval 失败不阻断：轮次仍 SUCCESS，eval 结果记 ok=false
            self.assertEqual(record.status, TaskStatus.SUCCESS)
            evals = record.output_summary["trace"]["evals"]
            entry = next(r for e in evals for r in e["results"] if r["name"] == "boom")
            self.assertFalse(entry["ok"])
        finally:
            eval_hooks._REGISTRY[eval_hooks.TURN_END] = []

    def test_run_evals_swallows_and_returns(self):
        @eval_hooks.register(eval_hooks.PROPOSAL_CREATED, "good")
        def _good(context):
            return {"v": 1}

        @eval_hooks.register(eval_hooks.PROPOSAL_CREATED, "bad")
        def _bad(context):
            raise ValueError("x")

        try:
            recorder = _RecorderStub()
            results = eval_hooks.run_evals(eval_hooks.PROPOSAL_CREATED, self.session.pk,
                                           {"trace_recorder": recorder})
            names = {r["name"]: r["ok"] for r in results}
            self.assertTrue(names["good"])
            self.assertFalse(names["bad"])
        finally:
            eval_hooks._REGISTRY[eval_hooks.PROPOSAL_CREATED] = []


class _RecorderStub:
    def __init__(self):
        self.evals = []

    def add_eval(self, result):
        self.evals.append(result)


# ── trace 查询端点 ──────────────────────────────────────────────────────────

class TraceApiTests(AssistantFixtureMixin, APITestCase):
    """GET /api/assistant/sessions/{id}/turns/{record_id}/trace/"""

    def _url(self, record_id):
        return f"/api/assistant/sessions/{self.session.pk}/turns/{record_id}/trace/"

    def _make_record(self, status=TaskStatus.SUCCESS, trace=None):
        output = {"outcome": "done", "session_id": self.session.pk}
        if trace is not None:
            output["trace"] = trace
        return TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                         status=status, input_summary={"session_id": self.session.pk},
                                         output_summary=output)

    def test_owner_reads_trace(self):
        record = self._make_record(trace={"trace_id": "t", "outcome": "done",
                                          "totals": {"input_tokens": 20}, "spans": [], "evals": []})
        self.client.force_authenticate(self.producer)
        resp = self.client.get(self._url(record.pk))
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["task_record_id"], record.pk)
        self.assertEqual(resp.data["trace"]["trace_id"], "t")
        self.assertEqual(resp.data["trace"]["totals"]["input_tokens"], 20)

    def test_running_turn_without_trace(self):
        record = self._make_record(status=TaskStatus.RUNNING, trace=None)
        self.client.force_authenticate(self.producer)
        resp = self.client.get(self._url(record.pk))
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertNotIn("trace", resp.data)
        self.assertEqual(resp.data["status"], TaskStatus.RUNNING)

    def test_non_owner_not_found(self):
        # 会话按属主隔离：非属主（含剧内成员）读他人会话的 trace 与既有会话端点一致返回 404
        record = self._make_record(trace={"trace_id": "t", "spans": []})
        self.client.force_authenticate(self.viewer)  # 剧内成员但非会话属主
        self.assertEqual(self.client.get(self._url(record.pk)).status_code, 404)

    def test_turn_of_other_session_not_found(self):
        record = self._make_record(trace={"trace_id": "t", "spans": []})
        # 把 record 改挂到别的会话，再以本会话身份查 → 404
        other = AssistantSession.objects.create(play=self.play, user=self.producer,
                                                agent_config=self.config)
        record.input_summary = {"session_id": other.pk}
        record.save()
        self.client.force_authenticate(self.producer)
        self.assertEqual(self.client.get(self._url(record.pk)).status_code, 404)


# ── phase1 max 审查修复 8.1–8.3：参数自动修复不击穿轮次 ──────────────────

class Phase1AutoRepairGuardTests(TestCase):
    """8.1：整数分支与 int() 转换判定一致。"""

    def test_double_dash_not_crash(self):
        schema = {"type": "object", "properties": {"shot_id": {"type": "integer"}}}
        out = auto_repair(schema, {"shot_id": "--5"})
        self.assertIn(out, (None, {"shot_id": "--5"}))  # 不抛异常

    def test_negative_string_repaired(self):
        schema = {"type": "object", "properties": {"n": {"type": "integer"}}}
        self.assertEqual(auto_repair(schema, {"n": "-5"}), {"n": -5})

    def test_plain_digits_still_repaired(self):
        schema = {"type": "object", "properties": {"shot_id": {"type": "integer"}}}
        self.assertEqual(auto_repair(schema, {"shot_id": "12"}), {"shot_id": 12})


class Phase1ExecutorRobustTests(AssistantFixtureMixin, TestCase):
    """8.2/8.3：任意畸形入参不抛异常，轮次不被击穿。"""

    def setUp(self):
        super().setUp()
        events.set_backend(events.MemoryStreams())
        self.addCleanup(events.set_backend, None)

    def executor(self, **kw):
        b = {"max_tool_attempts": 3, "max_total_tool_attempts": 10,
             "tool_retry_backoff_ms": 0, "max_tool_calls": 20}
        b.update(kw)
        return ToolExecutor(b, self.session.pk)

    def test_malformed_args_return_is_error_not_raise(self):
        """8.2：executor.run 对 '--5' 返回 is_error 结果而非抛异常。"""
        result, is_error, meta = self.executor().run(
            self.ctx(), "get_shot", {"shot_id": "--5"})
        self.assertTrue(is_error)
        self.assertEqual(meta.get("error_class"), VALIDATION)
        self.assertTrue(meta.get("terminated"))

    def test_malformed_args_weird_types_not_raise(self):
        """8.2：更离谱的入参类型也不抛。"""
        for bad_args in ({"shot_id": {"nested": "obj"}}, {"shot_id": None},
                         {"shot_id": [[1]]}, None, "not-a-dict"):
            result, is_error, meta = self.executor().run(self.ctx(), "get_shot", bad_args)
            self.assertTrue(is_error, f"{bad_args!r} 应返回错误而非成功")

    def test_multi_agent_path_not_raise(self):
        """8.2：multi_agent 只读工具路径同样不抛。

        选"有 required 参数"的工具：frozenset 迭代顺序随 PYTHONHASHSEED 变化，
        只按 properties 挑选会随机选中无 required 的工具（如 search_asset_library），
        未知键被剥掉后空参合法执行 → is_error=False，断言非确定性失败。
        """
        from apps.creation.assistant.multi_agent import READ_ONLY_TOOLS
        from apps.creation.assistant.tools import get_tool
        target = next((t for t in READ_ONLY_TOOLS if (get_tool(t) or {}).get("input_schema", {})
                       .get("required")), None)
        if target is None:
            self.skipTest("无必填参数只读工具")
        result, is_error, meta = self.executor().run(self.ctx(), target, {"x": object()})
        self.assertTrue(is_error)

    def test_successful_tool_then_malformed_keeps_turn(self):
        """8.3（真实轮次）：先成功调用一个工具、再传畸形参数，
        轮次 outcome 不是 error 且已收集的工具结果仍在。"""
        from apps.creation.assistant.models import AssistantSession
        from apps.creation.assistant.runner import _save, run_assistant_turn
        from apps.creation.models import TaskRecord, TaskStatus, TaskType
        from apps.creation.tests.test_assistant import FakeChatGateway, _say, _use
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.ASSISTANT_TURN,
            input_summary={"session_id": self.session.pk})
        _save(self.session, "user", [{"type": "text", "text": "查一下场景"}], record)
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        # 第一轮：成功调用只读工具；第二轮：畸形参数；第三轮：收尾
        gw = FakeChatGateway([
            _use("t1", "get_scene", {"scene_id": self.scene.pk}),
            _use("t2", "get_shot", {"shot_id": "--5"}),
            _say("已查看场景；镜头参数无效已跳过"),
        ])
        with patch("apps.ai_gateway.gateway.AIGateway", return_value=gw):
            run_assistant_turn(record.pk)
        record.refresh_from_db()
        self.assertNotEqual(record.status, TaskStatus.FAILED,
                            "畸形参数不得击穿轮次")
        # 成功工具的结果仍在消息流中
        from apps.creation.assistant.models import AssistantMessage
        msgs = list(self.session.messages.filter(task_record=record).order_by("seq"))
        tool_results = [b for m in msgs if m.role == "tool" for b in (m.content or [])
                        if b.get("type") == "tool_result"]
        self.assertTrue(any(not b.get("is_error") for b in tool_results),
                        "成功的工具结果仍在")
        self.assertTrue(any(b.get("is_error") for b in tool_results),
                        "畸形参数产生 is_error 结果（而非抛异常）")
