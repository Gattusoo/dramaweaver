"""出站统一入口（apps/ai_gateway/http.py）的 SSRF 防护测试。

任务 1.1 验证场景：
- 302 跳内网被拒（重定向逐跳校验）
- 白名单 host 放行
- 重定向超限报错
- DNS rebinding 场景（连接后 peer 校验拦截）
- safe_stream 同型防护

不复用 adapters 的 _ClientPatcher（那里 mock 掉 check_url/check_peer）——
本文件就是要验证这些校验真的生效。
"""
from unittest.mock import MagicMock, patch

import httpx
from django.test import SimpleTestCase, override_settings

from apps.ai_gateway.http import _MAX_REDIRECTS, safe_post, safe_request, safe_stream
from apps.core.utils.url_safety import UnsafeURLError


class _Provider:
    """轻量 provider 替身。"""

    def __init__(self, base_url):
        self.base_url = base_url


def _mk_response(url, status=200, body=b"ok", next_url=None):
    """构造一个最小可用的 httpx.Response（含 extensions/request）。"""
    req = httpx.Request("GET", url)
    resp = httpx.Response(status, content=body, request=req)
    if next_url:
        # 模拟 3xx：构造 next_request 让 response.is_redirect 成立
        resp.headers["Location"] = next_url
        resp.next_request = httpx.Request("GET", next_url)
    return resp


def _whitelisted_peer(resp):
    """让 response.extensions['network_stream'] 为 None → check_peer 保守放行。"""
    resp.extensions["network_stream"] = None
    return resp


class SafeRequestRedirectTests(SimpleTestCase):
    """重定向逐跳校验：任何一跳非公网即拒。"""

    def test_redirect_to_internal_rejected(self):
        """302 跳内网（169.254.169.254 云 metadata）必须被拒。"""
        provider = _Provider("https://api.example.com")
        # 第一跳：公网 OK，返回 302 指向内网
        first = _whitelisted_peer(_mk_response(
            "https://api.example.com/x",
            status=302,
            next_url="http://169.254.169.254/latest/meta-data",
        ))

        def fake_client(*args, **kw):
            client = MagicMock()

            def _request(method, url, **kw2):
                if "example.com" in url:
                    return first
                raise AssertionError(f"不应发起第二跳请求: {url}")

            client.request = _request
            client.__enter__ = lambda s: s
            client.__exit__ = lambda s, *a: False
            return client

        with patch("apps.ai_gateway.http.httpx.Client", side_effect=fake_client), \
             patch("apps.ai_gateway.http.check_url") as mock_check:
            # check_url 第一跳放行，第二跳（内网）抛 UnsafeURLError
            def check_side_effect(url):
                if "169.254.169.254" in url:
                    raise UnsafeURLError("禁止访问内网或保留地址")
                return "api.example.com"

            mock_check.side_effect = check_side_effect

            with self.assertRaises(UnsafeURLError):
                safe_request(provider, "GET", "https://api.example.com/x")

    def test_redirect_loop_exceeds_max(self):
        """重定向链超过 _MAX_REDIRECTS 抛 UnsafeURLError。"""
        provider = _Provider("https://api.example.com")

        def make_redirect(n):
            return _whitelisted_peer(_mk_response(
                f"https://api.example.com/r{n}",
                status=302,
                next_url=f"https://api.example.com/r{n+1}",
            ))

        call_count = {"n": 0}

        def fake_client(*args, **kw):
            client = MagicMock()

            def _request(method, url, **kw2):
                n = call_count["n"]
                call_count["n"] += 1
                return make_redirect(n)

            client.request = _request
            client.__enter__ = lambda s: s
            client.__exit__ = lambda s, *a: False
            return client

        with patch("apps.ai_gateway.http.httpx.Client", side_effect=fake_client), \
             patch("apps.ai_gateway.http.check_url", return_value="api.example.com"), \
             patch("apps.ai_gateway.http.check_peer", return_value=None):
            with self.assertRaises(UnsafeURLError) as ctx:
                safe_request(provider, "GET", "https://api.example.com/r0")
            self.assertIn("重定向次数超过", str(ctx.exception))
            # 实际请求数 = _MAX_REDIRECTS + 1
            self.assertEqual(call_count["n"], _MAX_REDIRECTS + 1)


class SafeRequestWhitelistTests(SimpleTestCase):
    """白名单豁免：provider.base_url 的 host 命中 SAFE_FETCH_ALLOWED_HOSTS 跳过校验。"""

    @override_settings(SAFE_FETCH_ALLOWED_HOSTS=["ollama.internal"])
    def test_whitelisted_host_skips_check_url(self):
        """白名单 provider：check_url/check_peer 完全不被调用。"""
        provider = _Provider("http://ollama.internal:11434")
        ok_resp = _whitelisted_peer(_mk_response("http://ollama.internal:11434/x", status=200, body=b"{}"))
        # status=200 时 is_redirect 已为 False（property 无 setter）

        def fake_client(*args, **kw):
            client = MagicMock()
            client.request = lambda *a, **k: ok_resp
            client.__enter__ = lambda s: s
            client.__exit__ = lambda s, *a: False
            return client

        with patch("apps.ai_gateway.http.httpx.Client", side_effect=fake_client), \
             patch("apps.ai_gateway.http.check_url") as mock_check_url, \
             patch("apps.ai_gateway.http.check_peer") as mock_check_peer:
            resp = safe_request(provider, "GET", "http://ollama.internal:11434/x")
            self.assertEqual(resp.status_code, 200)
            mock_check_url.assert_not_called()
            mock_check_peer.assert_not_called()

    def test_non_whitelisted_calls_check_url(self):
        """非白名单 provider：每个请求都要 check_url。"""
        provider = _Provider("https://api.example.com")
        ok_resp = _whitelisted_peer(_mk_response("https://api.example.com/x", status=200, body=b"{}"))
        # status=200 时 is_redirect 已为 False（property 无 setter）

        def fake_client(*args, **kw):
            client = MagicMock()
            client.request = lambda *a, **k: ok_resp
            client.__enter__ = lambda s: s
            client.__exit__ = lambda s, *a: False
            return client

        with patch("apps.ai_gateway.http.httpx.Client", side_effect=fake_client), \
             patch("apps.ai_gateway.http.check_url", return_value="api.example.com") as mock_check_url, \
             patch("apps.ai_gateway.http.check_peer", return_value=None):
            safe_request(provider, "GET", "https://api.example.com/x")
            mock_check_url.assert_called_once_with("https://api.example.com/x")


class SafeRequestPeerTests(SimpleTestCase):
    """DNS rebinding 防护：连接后用 socket.getpeername() 校验真实对端。"""

    def test_dns_rebinding_blocked_by_check_peer(self):
        """check_url 通过（解析为公网），但实际连接落到内网 → check_peer 拦截。"""
        provider = _Provider("https://api.example.com")
        resp = _mk_response("https://api.example.com/x", status=200)
        # 模拟对端为内网 IP
        fake_stream = MagicMock()
        fake_stream.get_extra_info.return_value = ("192.168.1.10", 443)
        resp.extensions["network_stream"] = fake_stream

        def fake_client(*args, **kw):
            client = MagicMock()
            client.request = lambda *a, **k: resp
            client.__enter__ = lambda s: s
            client.__exit__ = lambda s, *a: False
            return client

        with patch("apps.ai_gateway.http.httpx.Client", side_effect=fake_client), \
             patch("apps.ai_gateway.http.check_url", return_value="api.example.com"), \
             patch("apps.ai_gateway.http.check_peer") as mock_check_peer:
            # check_peer 检测到内网 IP 抛 UnsafeURLError
            mock_check_peer.side_effect = UnsafeURLError("实际连接目标非公网")
            with self.assertRaises(UnsafeURLError) as ctx:
                safe_request(provider, "GET", "https://api.example.com/x")
            self.assertIn("非公网", str(ctx.exception))
            # 验证 check_peer 收到正确的 (host, peer)
            mock_check_peer.assert_called_once()
            args = mock_check_peer.call_args[0]
            self.assertEqual(args[0], "api.example.com")
            self.assertEqual(args[1], ("192.168.1.10", 443))


class SafeStreamTests(SimpleTestCase):
    """safe_stream 与 safe_request 同型：连接后校验 + 重定向逐跳。"""

    def test_stream_yields_response_on_200(self):
        provider = _Provider("https://api.example.com")
        resp = _whitelisted_peer(_mk_response("https://api.example.com/s", status=200, body=b"data: x\n"))
        # status=200 时 is_redirect 已为 False（property 无 setter）

        class _StreamCtx:
            def __init__(self, r):
                self.r = r

            def __enter__(self):
                return self.r

            def __exit__(self, *exc):
                return False

        with patch("apps.ai_gateway.http.httpx.stream", return_value=_StreamCtx(resp)), \
             patch("apps.ai_gateway.http.check_url", return_value="api.example.com"), \
             patch("apps.ai_gateway.http.check_peer", return_value=None):
            with safe_stream(provider, "POST", "https://api.example.com/s", json={}) as r:
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.content, b"data: x\n")

    def test_stream_redirect_to_internal_rejected(self):
        provider = _Provider("https://api.example.com")
        first = _whitelisted_peer(_mk_response(
            "https://api.example.com/s",
            status=302,
            next_url="http://10.0.0.1/internal",
        ))

        class _StreamCtx:
            def __init__(self, r):
                self.r = r

            def __enter__(self):
                return self.r

            def __exit__(self, *exc):
                return False

        def check_side_effect(url):
            if "10.0.0.1" in url:
                raise UnsafeURLError("禁止访问内网")
            return "api.example.com"

        with patch("apps.ai_gateway.http.httpx.stream", return_value=_StreamCtx(first)), \
             patch("apps.ai_gateway.http.check_url", side_effect=check_side_effect), \
             patch("apps.ai_gateway.http.check_peer", return_value=None):
            with self.assertRaises(UnsafeURLError):
                with safe_stream(provider, "POST", "https://api.example.com/s", json={}):
                    pass


class SafePostShortcutTests(SimpleTestCase):
    """safe_post 是 safe_request 的 POST 语法糖。"""

    def test_safe_post_uses_post_method(self):
        provider = _Provider("https://api.example.com")
        seen = {}

        def fake_request(p, method, url, **kw):
            seen["method"] = method
            seen["url"] = url
            return _whitelisted_peer(_mk_response(url, status=200))

        with patch("apps.ai_gateway.http.safe_request", side_effect=fake_request):
            safe_post(provider, "https://api.example.com/x", json={"a": 1})
        self.assertEqual(seen["method"], "POST")
        self.assertEqual(seen["url"], "https://api.example.com/x")
