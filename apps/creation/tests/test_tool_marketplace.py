# -*- coding: utf-8 -*-
"""工具市场侧测试（openspec change: 2026-11-21-backend-tool-marketplace）。

覆盖：审核流状态机与审计、审核队列、复制式安装（幂等/冲突/副本独立）、
评分 upsert 与聚合、市场列表过滤聚合、文档渲染与权限、web_fetch 出站链
（mock httpx）、种子命令幂等与沙箱抽测、开关关闭零暴露。
"""
import json
from types import SimpleNamespace
from unittest import mock

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from apps.creation.assistant import registry as R
from apps.creation.assistant import tools as T
from apps.creation.assistant.models import (ToolRating, ToolRegistry,
                                            ToolReviewAction, ToolReviewLog,
                                            ToolType)

VALID_HANDLER = (
    "def run(args):\n"
    "    return {\"n\": args.get(\"x\", 0) * 2}\n"
)


def _user(name, admin=False):
    User = get_user_model()
    return User.objects.create_user(username=name, password="pw-12345",
                                    is_platform_admin=admin)


def _official(name="off_tool", **kw):
    """verified OFFICIAL 市场条目（作者 padmin）。"""
    padmin = _user("market_padmin", admin=True)
    params = dict(tool_name=name, tool_type=ToolType.OFFICIAL,
                  category="cat", description="官方工具",
                  input_schema={"type": "object",
                                "properties": {"x": {"type": "integer"}}},
                  handler_code=VALID_HANDLER, author=padmin,
                  is_verified=True, is_active=True)
    params.update(kw)
    return ToolRegistry.objects.create(**params)


# ── 审核流服务（tasks 1.2 / 3.x）────────────────────────────────────────────

@override_settings(ENABLE_TOOL_REGISTRY=True)
class ReviewActionTests(TestCase):
    def setUp(self):
        R._cache._entries.clear()
        self.padmin = _user("rev_padmin", admin=True)
        self.author = _user("rev_author")
        self.tool = ToolRegistry.objects.create(
            tool_name="rev_tool", tool_type=ToolType.CUSTOM, category="",
            description="待审", input_schema={}, handler_code=VALID_HANDLER,
            author=self.author)

    def test_verify_logs_and_flags(self):
        log = R.review_action(self.tool, actor=self.padmin,
                              action=ToolReviewAction.VERIFY, note="ok")
        self.tool.refresh_from_db()
        self.assertTrue(self.tool.is_verified)
        self.assertEqual((log.action, log.note), ("VERIFY", "ok"))

    def test_reject_requires_note(self):
        with self.assertRaises(R.RegistryValidationError):
            R.review_action(self.tool, actor=self.padmin,
                            action=ToolReviewAction.REJECT)

    def test_reject_stops_and_logs(self):
        self.tool.is_active = True
        self.tool.save()
        R.review_action(self.tool, actor=self.padmin,
                        action=ToolReviewAction.REJECT, note="格式不符")
        self.tool.refresh_from_db()
        self.assertFalse(self.tool.is_active)
        self.assertFalse(self.tool.is_verified)
        action, note = R.latest_review_note(self.tool)
        self.assertEqual((action, note), ("REJECT", "格式不符"))

    def test_reject_verified_tool_rejected(self):
        R.review_action(self.tool, actor=self.padmin, action=ToolReviewAction.VERIFY)
        with self.assertRaises(R.ReviewStateError):
            R.review_action(self.tool, actor=self.padmin,
                            action=ToolReviewAction.REJECT, note="x")

    def test_revoke_requires_verified(self):
        with self.assertRaises(R.ReviewStateError):
            R.review_action(self.tool, actor=self.padmin,
                            action=ToolReviewAction.REVOKE, note="x")

    def test_revoke_clears_verified_and_cache(self):
        R.review_action(self.tool, actor=self.padmin, action=ToolReviewAction.VERIFY)
        R._cache._entries["rev_tool"] = (9999.0, 1, {"name": "rev_tool"})
        R.review_action(self.tool, actor=self.padmin,
                        action=ToolReviewAction.REVOKE, note="安全原因")
        self.tool.refresh_from_db()
        self.assertFalse(self.tool.is_verified)
        self.assertFalse(self.tool.is_active)
        self.assertNotIn("rev_tool", R._cache._entries)

    def test_submit_then_queue_lifecycle(self):
        R.review_action(self.tool, actor=self.author, action=ToolReviewAction.SUBMIT)
        self.assertIn(self.tool, R.pending_review_tools())
        R.review_action(self.tool, actor=self.padmin,
                        action=ToolReviewAction.REJECT, note="首轮驳回")
        self.assertNotIn(self.tool, R.pending_review_tools())
        R.review_action(self.tool, actor=self.author, action=ToolReviewAction.SUBMIT,
                        note="已修复")
        self.assertIn(self.tool, R.pending_review_tools())
        R.review_action(self.tool, actor=self.padmin, action=ToolReviewAction.VERIFY)
        self.assertNotIn(self.tool, R.pending_review_tools())
        # 四步轨迹完整可追溯
        actions = list(self.tool.review_logs.order_by("id")
                       .values_list("action", flat=True))
        self.assertEqual(actions, ["SUBMIT", "REJECT", "SUBMIT", "VERIFY"])


# ── 复制式安装（tasks 2.2）───────────────────────────────────────────────────

@override_settings(ENABLE_TOOL_REGISTRY=True)
class InstallToolTests(TestCase):
    def setUp(self):
        R._cache._entries.clear()
        self.padmin = _user("ins_padmin", admin=True)
        self.alice = _user("ins_alice")
        self.bob = _user("ins_bob")
        self.source = ToolRegistry.objects.create(
            tool_name="mkt_tool", tool_type=ToolType.OFFICIAL, category="文本",
            description="官方", input_schema={"type": "object"},
            handler_code=VALID_HANDLER, author=self.padmin,
            is_verified=True, is_active=True)

    def test_install_creates_inactive_copy(self):
        rec, created = R.install_tool(self.source, user=self.alice)
        self.assertTrue(created)
        self.assertEqual((rec.tool_type, rec.is_verified, rec.is_active, rec.version),
                         (ToolType.CUSTOM, False, False, 1))
        self.assertEqual(rec.installed_from_id, self.source.pk)
        self.assertEqual(rec.handler_code, self.source.handler_code)
        # 副本名 = 源名 + __u{pk}（全局唯一下的确定性分名）
        self.assertEqual(rec.tool_name, f"mkt_tool__u{self.alice.pk}")

    def test_install_idempotent(self):
        first, created1 = R.install_tool(self.source, user=self.alice)
        second, created2 = R.install_tool(self.source, user=self.alice)
        self.assertTrue(created1)
        self.assertFalse(created2)
        self.assertEqual(first.pk, second.pk)

    def test_install_conflicts_with_same_copy_name(self):
        # 副本名确定性（源名+__u{pk}）；他人已占用同副本名 → 409 语义冲突
        self.padmin2 = _user("ins_padmin2", admin=True)
        ToolRegistry.objects.create(
            tool_name=f"mkt_tool__u{self.alice.pk}", tool_type=ToolType.CUSTOM,
            category="", description="占位", input_schema={},
            handler_code=VALID_HANDLER, author=self.padmin2)
        with self.assertRaises(R.RegistryValidationError) as ctx:
            R.install_tool(self.source, user=self.alice)
        self.assertIn("请先改名", str(ctx.exception))

    def test_install_rejects_unverified_source(self):
        self.source.is_verified = False
        self.source.save()
        with self.assertRaises(R.RegistryValidationError):
            R.install_tool(self.source, user=self.alice)

    def test_copy_survives_source_revoke(self):
        rec, _ = R.install_tool(self.source, user=self.alice)
        R.review_action(self.source, actor=self.padmin,
                        action=ToolReviewAction.REVOKE, note="下架")
        rec.refresh_from_db()
        self.assertEqual(rec.installed_from_id, self.source.pk)
        self.assertTrue(rec.handler_code)  # 副本完好，独立生命周期

    def test_copy_full_lifecycle_via_existing_api(self):
        """副本走既有管理端点全生命周期（activate/try/更新/停用），无特权路径。"""
        from rest_framework.test import APIClient
        rec, _ = R.install_tool(self.source, user=self.alice)
        client = APIClient()
        client.force_authenticate(self.alice)
        # 激活 → 对模型可见
        resp = client.post(f"/api/assistant/tools/{rec.pk}/activate/")
        self.assertEqual(resp.status_code, 200)
        R._cache._entries.clear()
        names = [s["name"] for s in R.registry_specs(self.alice)]
        self.assertIn(f"mkt_tool__u{self.alice.pk}", names)
        # 更新（乐观并发）
        resp = client.patch(f"/api/assistant/tools/{rec.pk}/",
                            {"description": "改描述", "expected_version": 1},
                            format="json")
        self.assertEqual(resp.status_code, 200)
        # 试运行
        resp = client.post(f"/api/assistant/tools/{rec.pk}/try_run/",
                           {"args": {"x": 4}}, format="json")
        self.assertEqual(resp.status_code, 200)
        # 停用 → 下一轮不可见（缓存失效立即生效）
        resp = client.post(f"/api/assistant/tools/{rec.pk}/deactivate/")
        self.assertEqual(resp.status_code, 200)
        names = [s["name"] for s in R.registry_specs(self.alice)]
        self.assertNotIn(f"mkt_tool__u{self.alice.pk}", names)
        # 删除
        resp = client.delete(f"/api/assistant/tools/{rec.pk}/")
        self.assertEqual(resp.status_code, 204)
        self.assertFalse(ToolRegistry.objects.filter(pk=rec.pk).exists())


# ── 评分（tasks 4.x）────────────────────────────────────────────────────────

@override_settings(ENABLE_TOOL_REGISTRY=True)
class RateToolTests(TestCase):
    def setUp(self):
        R._cache._entries.clear()
        self.padmin = _user("rate_padmin", admin=True)
        self.alice = _user("rate_alice")
        self.bob = _user("rate_bob")
        self.tool = ToolRegistry.objects.create(
            tool_name="rate_tool", tool_type=ToolType.OFFICIAL, category="",
            description="可评", input_schema={}, handler_code=VALID_HANDLER,
            author=self.padmin, is_verified=True, is_active=True)

    def test_rate_upsert_overwrites(self):
        R.rate_tool(self.tool, user=self.alice, rating=4, comment="不错")
        R.rate_tool(self.tool, user=self.alice, rating=5, comment="改五星")
        self.assertEqual(ToolRating.objects.filter(tool=self.tool).count(), 1)
        obj = ToolRating.objects.get(tool=self.tool, user=self.alice)
        self.assertEqual((obj.rating, obj.comment), (5, "改五星"))

    def test_rate_bounds(self):
        for bad in (0, 6, "4", 3.5, True):
            with self.assertRaises(R.RegistryValidationError):
                R.rate_tool(self.tool, user=self.alice, rating=bad)

    def test_multi_user_aggregate(self):
        from django.db.models import Avg
        R.rate_tool(self.tool, user=self.alice, rating=4)
        R.rate_tool(self.tool, user=self.bob, rating=5)
        agg = self.tool.ratings.aggregate(avg=Avg("rating"))
        self.assertAlmostEqual(agg["avg"], 4.5)


# ── 文档渲染（tasks 6.1）────────────────────────────────────────────────────

@override_settings(ENABLE_TOOL_REGISTRY=True)
class RenderDocsTests(TestCase):
    def setUp(self):
        self.record = SimpleNamespace(
            tool_name="doc_tool", tool_type="OFFICIAL", category="文本",
            description="文档工具", version=3, is_verified=True,
            input_schema={"type": "object",
                          "properties": {"s": {"type": "string"},
                                         "n": {"type": "number"},
                                         "b": {"type": "boolean"},
                                         "a": {"type": "array"},
                                         "o": {"type": "object"}},
                          "required": ["s"]})

    def test_doc_shape(self):
        doc = R.render_docs(self.record)
        self.assertIn("# 工具：`doc_tool`", doc)
        self.assertIn("文档工具", doc)
        self.assertIn("| `s` | string | 是 |", doc)
        self.assertIn("| `n` | number | 否 |", doc)
        self.assertIn('"s": "text"', doc)  # string 样例
        self.assertNotIn("def run", doc)  # 不含 handler 源码

    def test_doc_deterministic(self):
        self.assertEqual(R.render_docs(self.record), R.render_docs(self.record))

    def test_doc_empty_schema(self):
        self.record.input_schema = {}
        self.assertIn("无需入参", R.render_docs(self.record))


# ── web_fetch 出站链（tasks 5.1，mock httpx）────────────────────────────────

def _fake_response(url, status=200, text="hello", redirect=None, content_type="text/plain"):
    resp = SimpleNamespace()
    resp.url = SimpleNamespace(host=url.split("/")[2], __str__=lambda s: url)
    resp.status_code = status
    resp.text = text
    resp.is_redirect = redirect is not None
    resp.next_request = (SimpleNamespace(url=redirect) if redirect else None)
    resp.headers = {"content-type": content_type}
    resp.extensions = {}
    return resp


class _FakeClient:
    """httpx.Client 替身：按预编程 URL→response 映射返回。"""
    responses = {}

    def __init__(self, *a, **kw):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get(self, url):
        return self.responses[url]


@override_settings(ENABLE_TOOL_REGISTRY=True, WEB_FETCH_ALLOWED_HOSTS="api.example.com")
class WebFetchTests(TestCase):
    def setUp(self):
        self.ctx = SimpleNamespace()

    def _call(self, url, **kw):
        class _Err(Exception):
            pass
        # 借用 ToolError 通道：直接调 handler 并捕获 ToolError
        try:
            return T._TOOLS["web_fetch"]["handler"](self.ctx, url, **kw)
        except T.ToolError as exc:
            return ("ERROR", str(exc))

    def test_whitelist_pass(self):
        _FakeClient.responses = {"https://api.example.com/v1":
                                 _fake_response("https://api.example.com/v1")}
        with mock.patch("httpx.Client", _FakeClient), \
             mock.patch("apps.core.utils.url_safety.check_url"), \
             mock.patch("apps.core.utils.url_safety.check_peer"):
            result = self._call("https://api.example.com/v1")
        self.assertEqual(result["status"], 200)
        self.assertEqual(result["text"], "hello")
        self.assertFalse(result["truncated"])

    def test_host_outside_whitelist(self):
        result = self._call("https://evil.example.com/x")
        self.assertEqual(result[0], "ERROR")

    def test_bad_scheme(self):
        result = self._call("ftp://api.example.com/x")
        self.assertEqual(result[0], "ERROR")

    def test_redirect_outside_whitelist(self):
        # 首跳白名单内、302 到白名单外 → 逐跳校验拒绝
        _FakeClient.responses = {"https://api.example.com/redirect":
                                 _fake_response("https://api.example.com/redirect",
                                                status=302,
                                                redirect="https://evil.example.net/x")}
        with mock.patch("httpx.Client", _FakeClient), \
             mock.patch("apps.core.utils.url_safety.check_url"), \
             mock.patch("apps.core.utils.url_safety.check_peer"):
            result = self._call("https://api.example.com/redirect")
        self.assertEqual(result[0], "ERROR")
        self.assertIn("白名单", result[1])

    def test_http_error_status(self):
        _FakeClient.responses = {"https://api.example.com/e":
                                 _fake_response("https://api.example.com/e", status=503)}
        with mock.patch("httpx.Client", _FakeClient), \
             mock.patch("apps.core.utils.url_safety.check_url"), \
             mock.patch("apps.core.utils.url_safety.check_peer"):
            result = self._call("https://api.example.com/e")
        self.assertEqual(result[0], "ERROR")

    def test_truncation(self):
        _FakeClient.responses = {"https://api.example.com/big":
                                 _fake_response("https://api.example.com/big",
                                                text="x" * 1000)}
        with mock.patch("httpx.Client", _FakeClient), \
             mock.patch("apps.core.utils.url_safety.check_url"), \
             mock.patch("apps.core.utils.url_safety.check_peer"), \
             override_settings(WEB_FETCH_MAX_BYTES=300):
            result = self._call("https://api.example.com/big")
        self.assertTrue(result["truncated"])
        self.assertLessEqual(len(result["text"].encode("utf-8")) * 3, 900)

    def test_empty_whitelist_disables_tool(self):
        with override_settings(WEB_FETCH_ALLOWED_HOSTS=""):
            specs = [s["name"] for s in T.tool_specs()]
            self.assertNotIn("web_fetch", specs)

    def test_whitelist_registers_tool(self):
        specs = [s["name"] for s in T.tool_specs()]
        self.assertIn("web_fetch", specs)

    def test_registry_off_hides_tool(self):
        with override_settings(ENABLE_TOOL_REGISTRY=False):
            specs = [s["name"] for s in T.tool_specs()]
            self.assertNotIn("web_fetch", specs)


@override_settings(ENABLE_TOOL_REGISTRY=True, WEB_FETCH_ALLOWED_HOSTS="api.example.com")
class WebFetchRunnerE2ETests(TestCase):
    """5.3 集成：模型轮次内调用 web_fetch 结果回填 + chain 串市场安装副本。"""

    def test_model_calls_web_fetch_in_turn(self):
        from apps.creation.assistant.runner import build_history
        # 直接走 run_tool 通道（runner 循环的同一下游入口）
        ctx = SimpleNamespace(user=_user("wf_e2e"))
        _FakeClient.responses = {"https://api.example.com/v1":
                                 _fake_response("https://api.example.com/v1",
                                                text="结果文本")}
        with mock.patch("httpx.Client", _FakeClient), \
             mock.patch("apps.core.utils.url_safety.check_url"), \
             mock.patch("apps.core.utils.url_safety.check_peer"):
            result, err = T.run_tool(ctx, "web_fetch",
                                     {"url": "https://api.example.com/v1"})
        self.assertFalse(err)
        self.assertEqual(result["text"], "结果文本")
        self.assertEqual(result["status"], 200)

    def test_chain_with_installed_copy(self):
        from apps.creation.assistant.models import ToolRegistry
        padmin = _user("wf_chain_padmin", admin=True)
        alice = _user("wf_chain_alice")
        source = ToolRegistry.objects.create(
            tool_name="wf_chain_tool", tool_type=ToolType.OFFICIAL, category="",
            description="链源", input_schema={"type": "object",
                                              "properties": {"x": {"type": "integer"}}},
            handler_code=VALID_HANDLER, author=padmin,
            is_verified=True, is_active=True)
        rec, created = R.install_tool(source, user=alice)
        self.assertTrue(created)
        ToolRegistry.objects.filter(pk=rec.pk).update(is_active=True)
        R._cache._entries.clear()
        ctx = SimpleNamespace(user=alice)
        result, err = T.run_tool(ctx, "chain_tools",
                                 {"chain": [{"tool": rec.tool_name,
                                             "args": {"x": 3}}]})
        self.assertFalse(err)
        self.assertEqual(result["completed"][0], {"n": 6})


# ── 市场 / 审核 / 评分 / 文档 API（tasks 2.1 / 3.x / 4.2 / 6.2）─────────────

@override_settings(ENABLE_TOOL_REGISTRY=True, WEB_FETCH_ALLOWED_HOSTS="")
class MarketplaceAPITests(APITestCase):
    def setUp(self):
        self.padmin = _user("mk_padmin", admin=True)
        self.alice = _user("mk_alice")
        self.bob = _user("mk_bob")
        R._cache._entries.clear()
        patcher = mock.patch("apps.creation.assistant.views._throttle", return_value=True)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.source = ToolRegistry.objects.create(
            tool_name="mkt_api_tool", tool_type=ToolType.OFFICIAL, category="文本",
            description="市场工具", input_schema={"type": "object",
                                                  "properties": {"x": {"type": "integer"}}},
            handler_code=VALID_HANDLER, author=self.padmin,
            is_verified=True, is_active=True)
        self.hidden = ToolRegistry.objects.create(
            tool_name="mkt_hidden", tool_type=ToolType.OFFICIAL, category="文本",
            description="未验证官方", input_schema={}, handler_code=VALID_HANDLER,
            author=self.padmin, is_verified=False)

    def test_marketplace_lists_verified_official_only(self):
        self.client.force_authenticate(self.alice)
        resp = self._unwrap(self.client.get("/api/assistant/tools/marketplace/"))
        names = [t["tool_name"] for t in resp["tools"]]
        self.assertIn("mkt_api_tool", names)
        self.assertNotIn("mkt_hidden", names)
        entry = resp["tools"][0]
        self.assertEqual(entry["avg_rating"], None)
        self.assertEqual(entry["has_docs"], True)
        self.assertNotIn("handler_code", [k for k, v in entry.items() if v])

    def test_marketplace_filter_and_search(self):
        ToolRegistry.objects.create(
            tool_name="mkt_other", tool_type=ToolType.OFFICIAL, category="数据",
            description="另一个", input_schema={}, handler_code=VALID_HANDLER,
            author=self.padmin, is_verified=True)
        self.client.force_authenticate(self.alice)
        resp = self._unwrap(self.client.get("/api/assistant/tools/marketplace/?category=文本"))
        self.assertEqual([t["tool_name"] for t in resp["tools"]], ["mkt_api_tool"])
        resp = self._unwrap(self.client.get("/api/assistant/tools/marketplace/?q=另一个"))
        self.assertEqual([t["tool_name"] for t in resp["tools"]], ["mkt_other"])

    def test_marketplace_rating_aggregate(self):
        R.rate_tool(self.source, user=self.alice, rating=4)
        R.rate_tool(self.source, user=self.bob, rating=5)
        self.client.force_authenticate(self.alice)
        resp = self._unwrap(self.client.get("/api/assistant/tools/marketplace/"))
        entry = [t for t in resp["tools"] if t["tool_name"] == "mkt_api_tool"][0]
        self.assertEqual(entry["avg_rating"], "4.50")
        self.assertEqual(entry["rating_count"], 2)

    def test_install_then_idempotent(self):
        self.client.force_authenticate(self.alice)
        resp = self.client.post(f"/api/assistant/tools/{self.source.pk}/marketplace/install/")
        self.assertEqual(resp.status_code, 201)
        data = self._unwrap(resp)
        self.assertTrue(data["installed"])
        resp2 = self.client.post(f"/api/assistant/tools/{self.source.pk}/marketplace/install/")
        self.assertEqual(resp2.status_code, 200)
        self.assertFalse(self._unwrap(resp2)["installed"])
        self.assertEqual(ToolRegistry.objects.filter(
            author=self.alice, installed_from=self.source).count(), 1)

    def test_install_unverified_409(self):
        self.client.force_authenticate(self.alice)
        resp = self.client.post(f"/api/assistant/tools/{self.hidden.pk}/marketplace/install/")
        self.assertEqual(resp.status_code, 409)

    def test_rate_flow(self):
        self.client.force_authenticate(self.alice)
        resp = self._unwrap(self.client.post(
            f"/api/assistant/tools/{self.source.pk}/rate/",
            {"rating": 5, "comment": "好用"}, format="json"))
        self.assertEqual(resp["rating"], 5)
        self.assertEqual(resp["avg_rating"], "5.00")
        # upsert
        resp = self._unwrap(self.client.post(
            f"/api/assistant/tools/{self.source.pk}/rate/",
            {"rating": 3}, format="json"))
        self.assertEqual(resp["rating"], 3)
        self.assertEqual(ToolRating.objects.filter(tool=self.source).count(), 1)

    def test_rate_own_tool_400(self):
        self.client.force_authenticate(self.padmin)
        resp = self.client.post(f"/api/assistant/tools/{self.source.pk}/rate/",
                                {"rating": 5}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_rate_invisible_tool_404(self):
        self.client.force_authenticate(self.alice)
        resp = self.client.post(f"/api/assistant/tools/{self.hidden.pk}/rate/",
                                {"rating": 5}, format="json")
        self.assertEqual(resp.status_code, 404)

    def test_docs_permissions(self):
        # 作者可读自己的隐藏条目
        self.client.force_authenticate(self.padmin)
        resp = self._unwrap(self.client.get(f"/api/assistant/tools/{self.hidden.pk}/docs/"))
        self.assertIn("doc", resp)  # {"doc": markdown}
        self.assertNotIn("def run", resp["doc"])
        # 市场条目任何登录用户可读
        self.client.force_authenticate(self.alice)
        resp = self._unwrap(self.client.get(f"/api/assistant/tools/{self.source.pk}/docs/"))
        self.assertIn("| `x` | integer | 否 |", resp["doc"])
        # 他人未验证条目 404
        self.client.force_authenticate(self.bob)
        resp = self.client.get(f"/api/assistant/tools/{self.hidden.pk}/docs/")
        self.assertEqual(resp.status_code, 404)

    def test_review_queue_admin_only_and_lifecycle(self):
        # alice 无自定义条目可送审——造一个
        tool = ToolRegistry.objects.create(
            tool_name="mk_submit", tool_type=ToolType.CUSTOM, category="",
            description="送审", input_schema={}, handler_code=VALID_HANDLER,
            author=self.alice)
        self.client.force_authenticate(self.bob)
        resp = self.client.get("/api/assistant/tools/reviews/")
        self.assertEqual(resp.status_code, 403)
        # 作者送审
        self.client.force_authenticate(self.alice)
        self.client.post(f"/api/assistant/tools/{tool.pk}/submit/", {}, format="json")
        # 管理员看到队列
        self.client.force_authenticate(self.padmin)
        data = self._unwrap(self.client.get("/api/assistant/tools/reviews/"))
        self.assertIn("mk_submit", [t["tool_name"] for t in data["tools"]])
        # 驳回（note 必填）
        resp = self.client.post(f"/api/assistant/tools/{tool.pk}/reject/",
                                {}, format="json")
        self.assertEqual(resp.status_code, 400)
        resp = self._unwrap(self.client.post(
            f"/api/assistant/tools/{tool.pk}/reject/",
            {"note": "描述不清"}, format="json"))
        tool.refresh_from_db()
        self.assertFalse(tool.is_active)
        data = self._unwrap(self.client.get("/api/assistant/tools/reviews/"))
        self.assertNotIn("mk_submit", [t["tool_name"] for t in data["tools"]])
        # 驳回原因经 reject 响应体回显给作者（review_note）
        self.assertEqual(resp["review_note"], "描述不清")

    def test_revoke_flow(self):
        self.client.force_authenticate(self.padmin)
        resp = self._unwrap(self.client.post(
            f"/api/assistant/tools/{self.source.pk}/revoke/",
            {"note": "安全下架"}, format="json"))
        self.source.refresh_from_db()
        self.assertFalse(self.source.is_verified)

    def test_disabled_zero_exposure(self):
        with override_settings(ENABLE_TOOL_REGISTRY=False):
            self.client.force_authenticate(self.alice)
            resp = self._unwrap(self.client.get("/api/assistant/tools/marketplace/"))
            self.assertEqual(resp, {"tools": [], "enabled": False})
            resp = self.client.post(
                f"/api/assistant/tools/{self.source.pk}/marketplace/install/")
            self.assertEqual(resp.status_code, 404)
            resp = self.client.post(f"/api/assistant/tools/{self.source.pk}/rate/",
                                    {"rating": 5}, format="json")
            self.assertEqual(resp.status_code, 404)
            resp = self.client.get(f"/api/assistant/tools/{self.source.pk}/docs/")
            self.assertEqual(resp.status_code, 404)
            resp = self.client.get("/api/assistant/tools/reviews/")
            self.assertEqual(resp.status_code, 404)

    @staticmethod
    def _unwrap(resp):
        body = resp.json()
        return body.get("data", body)


# ── 种子命令（tasks 7.x）────────────────────────────────────────────────────

@override_settings(ENABLE_TOOL_REGISTRY=True)
class SeedOfficialToolsTests(TestCase):
    def setUp(self):
        # 种子命令需要至少一个用户作为官方作者（优先平台管理员）
        self.padmin = _user("seed_padmin", admin=True)

    def test_seed_idempotent_and_verified(self):
        from django.core.management import call_command
        call_command("seed_official_tools", stdout=mock.Mock())
        self.assertEqual(ToolRegistry.objects.filter(
            tool_type=ToolType.OFFICIAL, is_verified=True).count(), 10)
        first_pks = set(ToolRegistry.objects.values_list("pk", flat=True))
        call_command("seed_official_tools", stdout=mock.Mock())
        self.assertEqual(ToolRegistry.objects.count(), 10)
        self.assertEqual(first_pks,
                         set(ToolRegistry.objects.values_list("pk", flat=True)))
        # 全部默认停用 + OFFICIAL
        self.assertEqual(ToolRegistry.objects.filter(is_active=True).count(), 0)

    def test_seed_tools_sandbox_smoke(self):
        from django.core.management import call_command
        call_command("seed_official_tools", stdout=mock.Mock())
        rec = ToolRegistry.objects.get(tool_name="json_validate")
        result = R.run_in_sandbox(rec, {"text": "{\"a\": 1}"})
        self.assertEqual(result["valid"], True)

    def test_seed_tool_visible_when_active(self):
        from django.core.management import call_command
        call_command("seed_official_tools", stdout=mock.Mock())
        alice = _user("seed_alice")
        rec = ToolRegistry.objects.get(tool_name="text_stats")
        rec.is_active = True
        rec.save()
        names = [s["name"] for s in R.registry_specs(alice)]
        self.assertIn("text_stats", names)
