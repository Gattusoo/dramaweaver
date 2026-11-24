# -*- coding: utf-8 -*-
"""工具注册表与沙箱执行测试（openspec change: backend-tool-registry-sandbox）。

覆盖：静态检查各拒绝分支、沙箱真实子进程（含拒绝/超时/截断）、可见性矩阵、
乐观并发、动态加载与容错、chain_tools、API 权限与开关零暴露。
"""
import json
import time
from unittest import mock

from django.test import TestCase, override_settings

from apps.creation.assistant import registry as R
from apps.creation.assistant import tools as T
from apps.creation.assistant.models import ToolRegistry, ToolType

from django.contrib.auth import get_user_model

# ── 静态检查 ────────────────────────────────────────────────────────────────

VALID_HANDLER = (
    "import json\n"
    "import math\n"
    "\n"
    "def run(args):\n"
    "    return {\"double\": args.get(\"x\", 0) * 2, \"sqrt\": math.sqrt(args.get(\"x\", 0))}\n"
)


class StaticCheckTests(TestCase):
    def test_valid_handler_passes(self):
        R.check_handler_code(VALID_HANDLER)

    def test_missing_run(self):
        with self.assertRaises(R.RegistryValidationError):
            R.check_handler_code("def foo(args):\n    return {}")

    def test_run_with_two_args(self):
        with self.assertRaises(R.RegistryValidationError):
            R.check_handler_code("def run(a, b):\n    return {}")

    def test_run_with_vararg(self):
        with self.assertRaises(R.RegistryValidationError):
            R.check_handler_code("def run(*args):\n    return {}")

    def test_run_with_kwarg(self):
        with self.assertRaises(R.RegistryValidationError):
            R.check_handler_code("def run(args, **kw):\n    return {}")

    def test_async_run_rejected(self):
        with self.assertRaises(R.RegistryValidationError):
            R.check_handler_code("async def run(args):\n    return {}")

    def test_bad_import(self):
        with self.assertRaises(R.RegistryValidationError):
            R.check_handler_code("import subprocess\n\ndef run(args):\n    return {}")

    def test_from_import_whitelisted_ok(self):
        R.check_handler_code("from json import dumps\n\ndef run(args):\n    return {}")

    def test_relative_import_rejected(self):
        with self.assertRaises(R.RegistryValidationError):
            R.check_handler_code("from . import json\n\ndef run(args):\n    return {}")

    def test_forbidden_calls(self):
        for call in ("open(\"x\")", "eval(\"1\")", "exec(\"1\")",
                     "compile(\"1\", \"\", \"eval\")", "__import__(\"os\")"):
            with self.assertRaises(R.RegistryValidationError):
                R.check_handler_code(f"def run(args):\n    return {{\"x\": {call}}}")

    def test_syntax_error(self):
        with self.assertRaises(R.RegistryValidationError):
            R.check_handler_code("def run(args:\n    return {}")

    def test_empty_code(self):
        with self.assertRaises(R.RegistryValidationError):
            R.check_handler_code("")

    def test_oversize_code(self):
        with self.assertRaises(R.RegistryValidationError):
            R.check_handler_code("# pad\n" + "x" * (R.MAX_CODE_BYTES + 1))

    def test_tool_name_rules(self):
        for bad in ("GetScene", "ab", "1abc", "a" * 51, "tool-name", ""):
            with self.assertRaises(R.RegistryValidationError):
                R.check_tool_name(bad)
        R.check_tool_name("valid_tool_1")

    def test_description_rules(self):
        with self.assertRaises(R.RegistryValidationError):
            R.check_description("")
        with self.assertRaises(R.RegistryValidationError):
            R.check_description("x" * (R.MAX_DESCRIPTION + 1))

    def test_schema_rules(self):
        R.check_input_schema({"type": "object", "properties": {"x": {"type": "integer"}}})
        with self.assertRaises(R.RegistryValidationError):
            R.check_input_schema([])
        with self.assertRaises(R.RegistryValidationError):
            R.check_input_schema(None)


# ── 沙箱（真实子进程）───────────────────────────────────────────────────────

class SandboxTests(TestCase):
    def _record(self, code, **kw):
        return ToolRegistry(tool_name=kw.get("tool_name", "t_test"),
                            tool_type=ToolType.CUSTOM, description="测试",
                            input_schema=kw.get("input_schema", {}),
                            handler_code=code, author=None, **kw.get("extra", {}))

    def test_pure_function_roundtrip(self):
        rec = self._record(VALID_HANDLER)
        out = R.run_in_sandbox(rec, {"x": 8})
        self.assertEqual(out, {"double": 16, "sqrt": 2.8284271247461903})

    def test_open_denied(self):
        rec = self._record("def run(args):\n    return {\"x\": open(\"/etc/passwd\")}")
        with self.assertRaises(R.SandboxError):
            R.run_in_sandbox(rec, {})

    def test_eval_denied(self):
        rec = self._record("def run(args):\n    return {\"x\": eval(\"1+1\")}")
        with self.assertRaises(R.SandboxError):
            R.run_in_sandbox(rec, {})

    def test_subprocess_import_denied(self):
        rec = self._record("import subprocess\n\ndef run(args):\n    return {}")
        with self.assertRaises(R.SandboxError):
            R.run_in_sandbox(rec, {})

    def test_socket_denied(self):
        rec = self._record("import socket\n\ndef run(args):\n    return {}")
        with self.assertRaises(R.SandboxError):
            R.run_in_sandbox(rec, {})

    def test_handler_exception_is_error(self):
        rec = self._record("def run(args):\n    return 1/0")
        with self.assertRaises(R.SandboxError):
            R.run_in_sandbox(rec, {})

    def test_non_dict_result_rejected(self):
        rec = self._record("def run(args):\n    return [1, 2]")
        with self.assertRaises(R.SandboxError):
            R.run_in_sandbox(rec, {})

    def test_timeout_kills_loop(self):
        rec = self._record("def run(args):\n    x = 0\n    while True:\n        x += 1")
        with self.assertRaises(R.SandboxError) as ctx:
            R.run_in_sandbox(rec, {}, timeout_s=1)
        self.assertIn("超时", str(ctx.exception))

    def test_output_limit(self):
        rec = self._record("def run(args):\n    return {\"pad\": \"x\" * 100000}")
        with self.assertRaises(R.SandboxError):
            R.run_in_sandbox(rec, {}, output_limit=1024)


# ── 创建 / 更新 / 可见性 / 动态加载 ────────────────────────────────────────

def _user(name, admin=False):
    User = get_user_model()
    u = User.objects.create_user(username=name, password="pw-12345",
                                 is_platform_admin=admin)
    return u


@override_settings(ENABLE_TOOL_REGISTRY=True)
class RegistryServiceTests(TestCase):
    def setUp(self):
        self.alice = _user("alice_reg")
        self.bob = _user("bob_reg")
        R._cache._entries.clear()

    def _create(self, **kw):
        params = dict(tool_name="my_tool", tool_type=ToolType.CUSTOM,
                      category="测试", description="描述", input_schema={},
                      handler_code=VALID_HANDLER, author=self.alice)
        params.update(kw)
        return R.create_tool(**params)

    def test_create_defaults(self):
        rec = self._create()
        self.assertEqual((rec.tool_type, rec.is_active, rec.version),
                         (ToolType.CUSTOM, False, 1))

    def test_create_rejects_builtin_name(self):
        with self.assertRaises(R.RegistryValidationError) as ctx:
            self._create(tool_name="get_scene")
        self.assertIn("内置", str(ctx.exception))

    def test_create_rejects_duplicate(self):
        self._create()
        with self.assertRaises(R.RegistryValidationError):
            self._create(tool_name="my_tool", author=self.bob)

    def test_create_rejects_bad_code(self):
        with self.assertRaises(R.RegistryValidationError):
            self._create(handler_code="def run(a, b):\n    return {}")

    def test_update_optimistic_conflict(self):
        rec = self._create()
        with self.assertRaises(R.RegistryValidationError) as ctx:
            R.update_tool(rec, expected_version=99, description="新描述")
        self.assertIn("版本冲突", str(ctx.exception))
        rec.refresh_from_db()
        self.assertEqual(rec.description, "描述")

    def test_update_bumps_version_and_invalidates_cache(self):
        rec = self._create()
        R.registry_specs(self.alice)  # 预热缓存
        updated = R.update_tool(rec, expected_version=1, description="新描述")
        self.assertEqual(updated.version, 2)
        self.assertEqual(R._cache._entries.get("my_tool"), None)

    def test_update_rename_rejects_builtin(self):
        rec = self._create()
        with self.assertRaises(R.RegistryValidationError):
            R.update_tool(rec, expected_version=1, tool_name="memory_save")

    def test_update_rejects_bad_code_atomically(self):
        rec = self._create()
        with self.assertRaises(R.RegistryValidationError):
            R.update_tool(rec, expected_version=1, description="新描述",
                          handler_code="import os\n\ndef run(args):\n    return {}")
        rec.refresh_from_db()
        self.assertEqual((rec.description, rec.version), ("描述", 1))

    # ── 可见性矩阵 ──
    def _matrix(self):
        a = self._create(tool_name="alice_tool")
        b_official = self._create(tool_name="off_tool", tool_type=ToolType.OFFICIAL,
                                  author=self.alice)
        b = self._create(tool_name="bob_tool", author=self.bob)
        return a, b_official, b

    def test_visibility_matrix(self):
        alice_tool, off_tool, bob_tool = self._matrix()

        def names(user):
            return {r.tool_name for r in R.visible_tools(user)}

        # 未激活时全不可见
        ToolRegistry.objects.update(is_active=False)
        self.assertEqual(names(self.alice), set())
        # 激活后：作者见自己的 + verified OFFICIAL；未 verified OFFICIAL 仅管理员
        ToolRegistry.objects.update(is_active=True)
        self.assertEqual(names(self.alice), {"alice_tool", "off_tool"})
        self.assertEqual(names(self.bob), {"bob_tool"})

    def test_visibility_official_requires_verified(self):
        _, off_tool, _ = self._matrix()
        ToolRegistry.objects.update(is_active=True)
        self.assertNotIn("off_tool", {r.tool_name for r in R.visible_tools(self.bob)})
        ToolRegistry.objects.filter(pk=off_tool.pk).update(is_verified=True)
        self.assertIn("off_tool", {r.tool_name for r in R.visible_tools(self.bob)})

    def test_registry_specs_skips_bad_entry(self):
        self._create()
        # 直接注入一条坏 handler（绕过创建检查模拟历史脏数据/竞态）；两者都激活
        ToolRegistry.objects.create(
            tool_name="bad_tool", tool_type=ToolType.CUSTOM, description="坏",
            input_schema={}, handler_code="def run(a, b):\n    return {}",
            author=self.alice, is_active=True)
        ToolRegistry.objects.update(is_active=True)
        specs = R.registry_specs(self.alice)
        names = {s["name"] for s in specs}
        self.assertIn("my_tool", names)
        self.assertNotIn("bad_tool", names)

    def test_registry_specs_no_handler_leak(self):
        self._create()
        ToolRegistry.objects.update(is_active=True)
        for spec in R.registry_specs(self.alice):
            self.assertNotIn("handler_code", spec)
            self.assertNotIn("handler", spec)

    def test_get_registry_tool_respects_visibility(self):
        rec = self._create()
        ToolRegistry.objects.update(is_active=True)
        self.assertIsNotNone(R.get_registry_tool("my_tool", self.alice))
        self.assertIsNone(R.get_registry_tool("my_tool", self.bob))
        self.assertIsNone(R.get_registry_tool("get_scene", self.alice))  # 内置让位

    @override_settings(ENABLE_TOOL_REGISTRY=False)
    def test_disabled_zero_exposure(self):
        self._create()
        ToolRegistry.objects.update(is_active=True)
        self.assertEqual(list(R.visible_tools(self.alice)), [])
        self.assertEqual(R.registry_specs(self.alice), [])
        self.assertIsNone(R.get_registry_tool("my_tool", self.alice))


# ── run_tool 回退与 chain_tools ──────────────────────────────────────────────

class _Ctx:
    def __init__(self, user):
        self.user = user
        self.session = mock.MagicMock()
        self.session.user = user
        self.play = None
        self.proposals = []
        self.questions = None


@override_settings(ENABLE_TOOL_REGISTRY=True)
class RunToolFallbackTests(TestCase):
    def setUp(self):
        self.alice = _user("alice_rt")
        R._cache._entries.clear()
        self.rec = R.create_tool(
            tool_name="adder", tool_type=ToolType.CUSTOM, category="数学",
            description="两数相加",
            input_schema={"type": "object", "properties": {
                "a": {"type": "number"}, "b": {"type": "number"}},
                "required": ["a", "b"]},
            handler_code="def run(args):\n    return {\"sum\": args[\"a\"] + args[\"b\"]}",
            author=self.alice)
        ToolRegistry.objects.filter(pk=self.rec.pk).update(is_active=True)
        self.ctx = _Ctx(self.alice)

    def test_run_tool_falls_back_to_registry(self):
        result, err = T.run_tool(self.ctx, "adder", {"a": 1, "b": 2})
        self.assertFalse(err, result)
        self.assertEqual(result, {"sum": 3})

    def test_run_tool_invisible_returns_unknown(self):
        ctx_bob = _Ctx(_user("bob_rt"))
        result, err = T.run_tool(ctx_bob, "adder", {"a": 1, "b": 2})
        self.assertTrue(err)
        self.assertIn("未知工具", result["error"])

    def test_run_tool_validation_error(self):
        result, err = T.run_tool(self.ctx, "adder", {"a": "x"})
        self.assertTrue(err)
        self.assertIn("input.b", result["error"])  # 缺必填 b 的报错

    def test_run_tool_sandbox_failure(self):
        ToolRegistry.objects.filter(pk=self.rec.pk).update(
            handler_code="def run(args):\n    return 1/0")
        R._cache._entries.clear()
        result, err = T.run_tool(self.ctx, "adder", {"a": 1, "b": 2})
        self.assertTrue(err)
        self.assertIn("执行失败", result["error"])

    @override_settings(ENABLE_TOOL_REGISTRY=False)
    def test_disabled_fallback_returns_unknown(self):
        result, err = T.run_tool(self.ctx, "adder", {"a": 1, "b": 2})
        self.assertTrue(err)
        self.assertIn("未知工具", result["error"])


@override_settings(ENABLE_TOOL_REGISTRY=True)
class ChainToolsTests(TestCase):
    def setUp(self):
        self.alice = _user("alice_chain")
        R._cache._entries.clear()
        for name, handler in [
            ("c_first", "def run(args):\n    return {\"n\": args.get(\"x\", 3)}"),
            ("c_second", "def run(args):\n    return {\"n\": args[\"prev\"][\"n\"] * 10}"),
            ("c_fail", "def run(args):\n    return 1/0"),
        ]:
            rec = R.create_tool(tool_name=name, tool_type=ToolType.CUSTOM,
                                category="链", description=f"{name} 步骤",
                                input_schema={}, handler_code=handler, author=self.alice)
            ToolRegistry.objects.filter(pk=rec.pk).update(is_active=True)
        self.ctx = _Ctx(self.alice)

    def _chain(self, steps, stop_on_error=True):
        return T.run_tool(self.ctx, "chain_tools",
                          {"chain": steps, "stop_on_error": stop_on_error})

    def test_normal_chain(self):
        result, err = self._chain([{"tool": "c_first", "args": {"x": 3}},
                                   {"tool": "c_second"}])
        self.assertFalse(err, result)
        self.assertEqual(result["completed"], [{"n": 3}, {"n": 30}])
        self.assertEqual(result["steps"], 2)

    def test_intermediate_failure_returns_partial(self):
        result, err = self._chain([{"tool": "c_first"}, {"tool": "c_fail"},
                                   {"tool": "c_second"}])
        # 失败信息在结果 dict 里（failed_at/error 键），不是 is_error
        self.assertFalse(err, result)
        self.assertEqual(result["completed"], [{"n": 3}])
        self.assertEqual(result["failed_at"], 1)

    def test_continue_on_error(self):
        result, err = self._chain([{"tool": "c_fail"}, {"tool": "c_first"}],
                                  stop_on_error=False)
        self.assertFalse(err, result)
        self.assertEqual(len(result["completed"]), 2)
        self.assertIn("error", result["completed"][0])

    def test_recursion_rejected(self):
        result, err = self._chain([{"tool": "c_first"},
                                   {"tool": "chain_tools"}])
        self.assertTrue(err)
        self.assertIn("chain_tools", result["error"])

    def test_duplicate_step_rejected(self):
        result, err = self._chain([{"tool": "c_first"}, {"tool": "c_first"}])
        self.assertTrue(err)
        self.assertIn("重复", result["error"])

    def test_prev_key_conflict_rejected(self):
        # 步骤 args schema 允许自由键（additionalProperties: {}），prev 可透传到
        # handler ——handler 检测到预定义 prev 即拒绝该步（registry D5 冲突语义）
        result, err = self._chain([{"tool": "c_first"},
                                   {"tool": "c_second", "args": {"prev": {}}}])
        self.assertTrue(err)
        self.assertIn("prev", result["error"])

    def test_prev_merged_into_step(self):
        result, err = self._chain([{"tool": "c_first"},
                                   {"tool": "c_second"}])
        self.assertFalse(err, result)
        # c_second 收到 prev={"n":3} → n=30
        self.assertEqual(result["completed"][1], {"n": 30})

    def test_max_steps_enforced(self):
        steps = [{"tool": "c_first"} for _ in range(6)]
        result, err = self._chain(steps)
        self.assertTrue(err)
        self.assertIn("最多", result["error"])

    def test_unknown_step_tool(self):
        result, err = self._chain([{"tool": "no_such_tool"}])
        # 未知工具是部分结果语义（模型需要看到失败位置），不是 is_error
        self.assertFalse(err, result)
        self.assertIn("未知工具", result["error"])
        self.assertEqual(result["failed_at"], 0)

    def test_disabled_flag_rejects(self):
        with override_settings(ENABLE_TOOL_REGISTRY=False):
            result, err = self._chain([{"tool": "c_first"}])
        self.assertTrue(err)
        self.assertIn("未启用", result["error"])


# ── 工具面零暴露（tool_specs / 注册表合并）──────────────────────────────────

class ToolSpecsExposureTests(TestCase):
    def test_chain_tools_hidden_when_disabled(self):
        names = {s["name"] for s in T.tool_specs()}
        self.assertNotIn("chain_tools", names)

    @override_settings(ENABLE_TOOL_REGISTRY=True)
    def test_chain_tools_visible_when_enabled(self):
        names = {s["name"] for s in T.tool_specs()}
        self.assertIn("chain_tools", names)

# -*- coding: utf-8 -*-
# 追加块：API 测试（由 Bash 拼接进 test_tool_registry.py 末尾）

from rest_framework.test import APITestCase  # noqa: E402

API = "/api/assistant/tools"


@override_settings(ENABLE_TOOL_REGISTRY=True)
class ToolRegistryAPITests(APITestCase):
    def setUp(self):
        self.alice = _user("api_alice")
        self.bob = _user("api_bob")
        self.padmin = _user("api_padmin", admin=True)
        R._cache._entries.clear()
        # 节流共享限流器（模块级内存存储，跨测试泄漏）；本套件测权限与契约，
        # 429 行为单独验证（test_tool_registry_throttle）
        patcher = mock.patch("apps.creation.assistant.views._throttle", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.payload = {
            "tool_name": "api_tool",
            "category": "math",
            "description": "API test tool",
            "input_schema": {"type": "object", "properties": {"x": {"type": "integer"}}},
            "handler_code": "def run(args):\n    return {\"n\": args.get(\"x\", 0)}",
        }

    def _create(self, as_user=None, **kw):
        if as_user is not None:
            self.client.force_authenticate(as_user)
        data = {**self.payload, **kw}
        return self.client.post(API + "/", data, format="json")

    def test_create_requires_auth(self):
        resp = self.client.post(API + "/", self.payload, format="json")
        self.assertIn(resp.status_code, (401, 403))

    def test_create_and_defaults(self):
        resp = self._create(self.alice)
        self.assertEqual(resp.status_code, 201, resp.data)
        rec = ToolRegistry.objects.get(tool_name="api_tool")
        self.assertEqual((rec.tool_type, rec.is_active, rec.version), ("CUSTOM", False, 1))
        self.assertEqual(rec.author, self.alice)

    def test_create_official_requires_admin(self):
        resp = self._create(self.alice, tool_type="OFFICIAL")
        self.assertEqual(resp.status_code, 403)

    def test_create_admin_official_ok(self):
        resp = self._create(self.padmin, tool_type="OFFICIAL")
        self.assertEqual(resp.status_code, 201, resp.data)

    def test_create_bad_code_400(self):
        resp = self._create(self.alice, handler_code="def run(a, b):\n    return {}")
        self.assertEqual(resp.status_code, 400)

    def test_create_builtin_name_400(self):
        resp = self._create(self.alice, tool_name="get_scene")
        self.assertEqual(resp.status_code, 400)

    def test_list_visibility(self):
        self._create(self.alice)
        resp = self._create(self.padmin, tool_name="api_official", tool_type="OFFICIAL")
        off_id = resp.data["id"]
        ToolRegistry.objects.filter(pk=off_id).update(is_verified=True, is_active=True)

        self.client.force_authenticate(self.alice)
        resp = self.client.get(API + "/")
        self.assertEqual(resp.status_code, 200)
        names = {t["tool_name"] for t in resp.data["tools"]}
        self.assertEqual(names, {"api_tool", "api_official"})

        self.client.force_authenticate(self.bob)
        resp = self.client.get(API + "/")
        names = {t["tool_name"] for t in resp.data["tools"]}
        self.assertEqual(names, {"api_official"})

        self.client.force_authenticate(self.padmin)
        resp = self.client.get(API + "/")
        names = {t["tool_name"] for t in resp.data["tools"]}
        self.assertEqual(names, {"api_tool", "api_official"})

    def test_handler_code_hidden_from_others(self):
        self._create(self.padmin, tool_name="api_official2", tool_type="OFFICIAL")
        self.client.force_authenticate(self.bob)
        resp = self.client.get(API + "/")
        for t in resp.data["tools"]:
            self.assertIsNone(t["handler_code"], f"handler_code leaked: {t['tool_name']}")

    def test_update_author_ok_conflict_409(self):
        resp = self._create(self.alice)
        pk = resp.data["id"]
        self.client.force_authenticate(self.alice)
        resp = self.client.patch(f"{API}/{pk}/",
                                 {"description": "new desc", "expected_version": 1},
                                 format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(resp.data["version"], 2)
        resp = self.client.patch(f"{API}/{pk}/",
                                 {"description": "again", "expected_version": 1},
                                 format="json")
        self.assertEqual(resp.status_code, 409)

    def test_update_by_non_author_403(self):
        resp = self._create(self.alice)
        pk = resp.data["id"]
        self.client.force_authenticate(self.bob)
        resp = self.client.patch(f"{API}/{pk}/",
                                 {"description": "hijack", "expected_version": 1},
                                 format="json")
        self.assertEqual(resp.status_code, 403)

    def test_update_by_admin_200(self):
        resp = self._create(self.alice)
        pk = resp.data["id"]
        self.client.force_authenticate(self.padmin)
        resp = self.client.patch(f"{API}/{pk}/",
                                 {"category": "admin", "expected_version": 1},
                                 format="json")
        self.assertEqual(resp.status_code, 200, resp.data)

    def test_activate_deactivate(self):
        resp = self._create(self.alice)
        pk = resp.data["id"]
        self.client.force_authenticate(self.alice)
        resp = self.client.post(f"{API}/{pk}/activate/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data["is_active"])
        resp = self.client.post(f"{API}/{pk}/deactivate/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertFalse(resp.data["is_active"])

    def test_activate_by_others_403(self):
        resp = self._create(self.alice)
        pk = resp.data["id"]
        self.client.force_authenticate(self.bob)
        resp = self.client.post(f"{API}/{pk}/activate/")
        self.assertEqual(resp.status_code, 403)

    def test_verify_requires_admin(self):
        resp = self._create(self.alice)
        pk = resp.data["id"]
        self.client.force_authenticate(self.alice)
        self.assertEqual(self.client.post(f"{API}/{pk}/verify/").status_code, 403)
        self.client.force_authenticate(self.padmin)
        resp = self.client.post(f"{API}/{pk}/verify/")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data["is_verified"])

    def test_try_run_by_author(self):
        resp = self._create(self.alice)
        pk = resp.data["id"]
        self.client.force_authenticate(self.alice)
        resp = self.client.post(f"{API}/{pk}/try_run/", {"args": {"x": 7}}, format="json")
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertTrue(resp.data["ok"], resp.data)
        self.assertEqual(resp.data["result"], {"n": 7})

    def test_try_run_failure_returns_error(self):
        resp = self._create(self.alice,
                            handler_code="def run(args):\n    return 1/0")
        pk = resp.data["id"]
        self.client.force_authenticate(self.alice)
        resp = self.client.post(f"{API}/{pk}/try_run/", {"args": {}}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["ok"])
        self.assertIn("失败", resp.data["error"])

    def test_try_run_by_others_403(self):
        resp = self._create(self.alice)
        pk = resp.data["id"]
        self.client.force_authenticate(self.bob)
        resp = self.client.post(f"{API}/{pk}/try_run/", {"args": {}}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_delete_by_author(self):
        resp = self._create(self.alice)
        pk = resp.data["id"]
        self.client.force_authenticate(self.alice)
        resp = self.client.delete(f"{API}/{pk}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ToolRegistry.objects.filter(pk=pk).exists())

    def test_delete_by_others_403(self):
        resp = self._create(self.alice)
        pk = resp.data["id"]
        self.client.force_authenticate(self.bob)
        self.assertEqual(self.client.delete(f"{API}/{pk}/").status_code, 403)

    @override_settings(ENABLE_TOOL_REGISTRY=False)
    def test_disabled_list_empty_flag(self):
        self._create(self.alice)
        self.client.force_authenticate(self.alice)
        resp = self.client.get(API + "/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data, {"tools": [], "enabled": False})

    @override_settings(ENABLE_TOOL_REGISTRY=False)
    def test_disabled_write_paths_404(self):
        # API 关闭时创建也 404 -> 记录用服务层直建（绕过 API）
        rec = R.create_tool(tool_name="api_tool", tool_type=ToolType.CUSTOM,
                            category="c", description="d", input_schema={},
                            handler_code="def run(args):\n    return {}",
                            author=self.alice)
        pk = rec.pk
        self.client.force_authenticate(self.alice)
        self.assertEqual(self.client.post(API + "/", self.payload, format="json").status_code, 404)
        self.assertEqual(self.client.post(f"{API}/{pk}/try_run/", format="json").status_code, 404)
        self.assertEqual(self.client.post(f"{API}/{pk}/verify/").status_code, 404)


class ToolRegistryThrottleTests(APITestCase):
    """create/try_run 走 _throttle 限流（registry D6）。"""

    def setUp(self):
        self.user = _user("thr_user")
        R._cache._entries.clear()
        # 清共享限流器内存存储（模块级单例跨测试泄漏）
        from apps.ai_gateway.rate_limiter import RateLimiter
        self._rl_store = RateLimiter().store
        for key in list(getattr(self._rl_store, "_data", {})):
            if key.startswith("rl:"):
                del self._rl_store._data[key]

    def test_try_run_throttled(self):
        from .test_assistant import AssistantFixtureMixin  # noqa: F401 — 不用，独立构造
        payload = {
            "tool_name": "thr_tool", "category": "c", "description": "d",
            "input_schema": {},
            "handler_code": "def run(args):\n    return {}",
        }
        self.client.force_authenticate(self.user)
        with override_settings(ENABLE_TOOL_REGISTRY=True):
            # 连续打满限流额度（MESSAGE_RPM=10；首步创建成功）
            codes = []
            for i in range(12):
                resp = self.client.post(API + "/",
                                        {**payload, "tool_name": f"thr_tool_{i}"},
                                        format="json")
                codes.append(resp.status_code)
            self.assertIn(429, codes)
            self.assertIn(201, codes)
