"""request_id 中间件与异常日志测试（platform/api-conventions / design D3）。

覆盖 tasks 3.2：响应头 X-Request-ID 存在、自定义请求头透传、
未预期异常的日志与 500 响应体均携带同一 request_id。
"""
import logging

from rest_framework.test import APITestCase

from dvadmin.utils.exception import custom_exception_handler


class RequestIDHeaderTests(APITestCase):
    """中间件为每个响应生成/回写 X-Request-ID。"""

    def test_response_has_request_id_header(self):
        resp = self.client.get("/api/auth/me/")
        self.assertIn("X-Request-ID", resp)
        self.assertTrue(resp["X-Request-ID"])

    def test_custom_header_passthrough(self):
        rid = "custom-request-id-42"
        resp = self.client.get("/api/auth/me/", HTTP_X_REQUEST_ID=rid)
        self.assertEqual(resp["X-Request-ID"], rid)

    def test_header_differs_across_requests(self):
        rid1 = self.client.get("/api/auth/me/")["X-Request-ID"]
        rid2 = self.client.get("/api/auth/me/")["X-Request-ID"]
        self.assertTrue(rid1)
        self.assertTrue(rid2)
        self.assertNotEqual(rid1, rid2)


class RequestIDExceptionTests(APITestCase):
    """未预期异常：日志与 500 响应体携带同一 request_id。"""

    def test_unexpected_exception_logs_and_responds_request_id(self):
        class _FakeRequest:
            request_id = "req-abc-123"

        context = {"request": _FakeRequest()}
        with self.assertLogs("dvadmin.exception", level="ERROR") as logs:
            resp = custom_exception_handler(ValueError("boom"), context)

        self.assertEqual(resp.status_code, 500)
        self.assertEqual(resp.data["request_id"], "req-abc-123")
        # 日志记录携带同一 request_id（同一请求可见）
        self.assertTrue(any("req-abc-123" in m for m in logs.output))

    def test_exception_without_request_id_omits_it(self):
        context = {"request": object()}
        with self.assertLogs("dvadmin.exception", level="ERROR"):
            resp = custom_exception_handler(RuntimeError("x"), context)
        self.assertEqual(resp.status_code, 500)
        self.assertNotIn("request_id", resp.data)
