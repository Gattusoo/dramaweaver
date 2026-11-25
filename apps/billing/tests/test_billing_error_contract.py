"""计费错误契约（2026-11-25 billing-error-contract-alignment）。

覆盖：渲染器 402 默认 4001 / 显式 4029；adjust 校验中文文案。
（start 预检、助手 402 字段、provider 费率见对应测试目录用例。）
"""
import json

from django.test import SimpleTestCase

from apps.billing.serializers.wallet import WalletAdjustSerializer
from apps.core.utils.renderers import CustomJSONRenderer


class _Resp:
    def __init__(self, status_code, business_code=None):
        self.status_code = status_code
        if business_code is not None:
            self.business_code = business_code


class Renderer402Tests(SimpleTestCase):
    def _wrap(self, resp, data):
        out = CustomJSONRenderer().render(
            data, renderer_context={"response": resp},
        )
        return json.loads(out)

    def test_402_defaults_to_4001(self):
        body = self._wrap(
            _Resp(402),
            {"detail": "积分不足", "reason": "insufficient_points",
             "needed": 500, "balance": 0},
        )
        self.assertEqual(body["code"], 4001)
        self.assertEqual(body["msg"], "积分不足")
        self.assertIsNone(body["data"])
        # 业务字段透传信封顶层（前端读顶层 needed/balance）
        self.assertEqual(body["reason"], "insufficient_points")
        self.assertEqual(body["needed"], 500)
        self.assertEqual(body["balance"], 0)

    def test_business_code_4029_overrides(self):
        body = self._wrap(
            _Resp(402, business_code=4029),
            {"detail": "额度用完", "reason": "daily_render_exhausted"},
        )
        self.assertEqual(body["code"], 4029)
        self.assertEqual(body["reason"], "daily_render_exhausted")

    def test_403_frozen_unchanged(self):
        body = self._wrap(
            _Resp(403, business_code=4003),
            {"detail": "冻结", "reason": "play_frozen"},
        )
        self.assertEqual(body["code"], 4003)

    def test_402_without_detail_keeps_4001(self):
        """无 detail 的错误体也要走 HTTP/显式业务码映射，不能硬编码 4000。"""
        body = self._wrap(_Resp(402), {"reason": "insufficient_points"})
        self.assertEqual(body["code"], 4001)
        self.assertEqual(body["msg"], "参数校验失败")
        self.assertEqual(body["data"]["errors"], {"reason": "insufficient_points"})

    def test_409_without_detail_keeps_4090(self):
        body = self._wrap(_Resp(409), ["conflict"])
        self.assertEqual(body["code"], 4090)


class WalletAdjustSerializerTests(SimpleTestCase):
    def test_missing_note_chinese_message(self):
        s = WalletAdjustSerializer(data={"amount": 100})
        self.assertFalse(s.is_valid())
        self.assertEqual(s.errors["note"], ["请填写调整原因"])

    def test_blank_note_chinese_message(self):
        s = WalletAdjustSerializer(data={"amount": 100, "note": "   "})
        self.assertFalse(s.is_valid())
        self.assertEqual(s.errors["note"], ["请填写调整原因"])

    def test_zero_amount_rejected(self):
        s = WalletAdjustSerializer(data={"amount": 0, "note": "测试"})
        self.assertFalse(s.is_valid())
        self.assertIn("调整额不可为 0", str(s.errors["amount"]))

    def test_non_integer_amount_chinese_message(self):
        s = WalletAdjustSerializer(data={"amount": "abc", "note": "测试"})
        self.assertFalse(s.is_valid())
        self.assertEqual(s.errors["amount"], ["调整额必须为整数"])

    def test_amount_too_long_chinese_message(self):
        s = WalletAdjustSerializer(data={"amount": "1" * 1001, "note": "测试"})
        self.assertFalse(s.is_valid())
        self.assertEqual(s.errors["amount"], ["调整额必须为整数"])

    def test_note_invalid_type_chinese_message(self):
        s = WalletAdjustSerializer(data={"amount": 100, "note": {"a": 1}})
        self.assertFalse(s.is_valid())
        self.assertEqual(s.errors["note"], ["调整原因必须为字符串"])

    def test_missing_amount_chinese_message(self):
        s = WalletAdjustSerializer(data={"note": "测试"})
        self.assertFalse(s.is_valid())
        self.assertEqual(s.errors["amount"], ["请填写调整额"])

    def test_valid_payload(self):
        s = WalletAdjustSerializer(data={"amount": -100, "note": "修正"})
        self.assertTrue(s.is_valid(), s.errors)
