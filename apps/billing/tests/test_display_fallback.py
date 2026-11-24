"""任务 4：双轨展示 / 跨路线回退 / 大额告警。"""
import json
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import SimpleTestCase, TestCase, override_settings

from apps.billing.services import maybe_alert_large_settle, read_alerts
from apps.billing.tests.test_quota_gates import _reset_store

User = get_user_model()


class AnnotatePriceTests(SimpleTestCase):
    """任务 4.1：序列化含 source + price_points。"""

    def test_annotate_includes_price_points(self):
        from apps.ai_gateway.router import annotate_chain_with_source

        class _P:
            def __init__(self, visibility, base_url="https://x.example"):
                self.visibility = visibility
                self.base_url = base_url

        platform = _P("platform")
        custom = _P("custom")
        chain = [("m1", platform), ("m2", custom)]
        # user=None → custom 剔除，仅 platform
        out = annotate_chain_with_source(chain, user=None)
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["source"], "platform")
        self.assertIsNone(out[0]["price_points"])

    @override_settings(SERVICE_FEE_PER_CALL=7)
    def test_custom_price_is_service_fee(self):
        from apps.ai_gateway.router import annotate_chain_with_source
        from apps.ai_gateway.models import ProviderVisibility

        class _P:
            def __init__(self, visibility, base_url="https://x.example"):
                self.visibility = visibility
                self.base_url = base_url

        custom = _P(ProviderVisibility.CUSTOM)

        class _U:
            pk = 1

        with patch("apps.ai_gateway.credentials.resolve_credential",
                   return_value=object()):
            out = annotate_chain_with_source([("m2", custom)], user=_U())
        self.assertEqual(out[0]["source"], "credential")
        self.assertEqual(out[0]["price_points"], 7)


class CrossRouteFallbackTests(SimpleTestCase):
    """任务 4.2：ENABLE_CROSS_ROUTE_FALLBACK 默认关，平台→渠道回退被跳过。"""

    @staticmethod
    def _chain():
        return [
            {"source": "platform", "name": "p1"},
            {"source": "credential", "name": "c1"},
            {"source": "platform", "name": "p2（回退）"},
        ]

    @override_settings(ENABLE_CROSS_ROUTE_FALLBACK=False)
    def test_default_blocks_platform_after_credential(self):
        from apps.ai_gateway.router import filter_cross_route_fallback
        out = filter_cross_route_fallback(self._chain())
        self.assertEqual([n["name"] for n in out], ["p1", "c1"])
        self.assertEqual(len(out.cross_route_blocked), 1)
        self.assertEqual(out.cross_route_blocked[0]["name"], "p2（回退）")

    @override_settings(ENABLE_CROSS_ROUTE_FALLBACK=True)
    def test_enabled_allows_fallback(self):
        from apps.ai_gateway.router import filter_cross_route_fallback
        out = filter_cross_route_fallback(self._chain())
        self.assertEqual([n["name"] for n in out], ["p1", "c1", "p2（回退）"])
        self.assertEqual(out.cross_route_blocked, [])

    @override_settings(ENABLE_CROSS_ROUTE_FALLBACK=False)
    def test_credential_to_platform_direction_always_allowed(self):
        """决策 9：平台→用户渠道默认允许（p1→c1 不拦）。"""
        from apps.ai_gateway.router import filter_cross_route_fallback
        chain = [
            {"source": "platform", "name": "p1"},
            {"source": "credential", "name": "c1"},
        ]
        out = filter_cross_route_fallback(chain)
        self.assertEqual([n["name"] for n in out], ["p1", "c1"])


class OwnerAlertTests(TestCase):
    """任务 4.3：大额 settle 告警埋点（含 triggered_by）。"""

    def setUp(self):
        _reset_store()
        with patch("apps.billing.services.grant"):
            self.owner = User.objects.create_user(
                username="alert-owner", password="pass-12345678", email="a@x.com")
            self.member = User.objects.create_user(
                username="alert-member", password="pass-12345678", email="m@x.com")

    @override_settings(OWNER_ALERT_THRESHOLD_POINTS=5000)
    def test_above_threshold_emits(self):
        ok = maybe_alert_large_settle(self.owner, 5000, triggered_by=self.member,
                                      task_ref=("task", "T1"))
        self.assertTrue(ok)
        alerts = read_alerts()
        self.assertEqual(len(alerts), 1)
        self.assertEqual(alerts[0]["points"], 5000)
        self.assertEqual(alerts[0]["triggered_by"], self.member.pk)
        self.assertEqual(alerts[0]["owner_id"], self.owner.pk)

    @override_settings(OWNER_ALERT_THRESHOLD_POINTS=5000)
    def test_below_threshold_no_emit(self):
        ok = maybe_alert_large_settle(self.owner, 4999, triggered_by=self.member,
                                      task_ref=("task", "T2"))
        self.assertFalse(ok)
        self.assertEqual(read_alerts(), [])

    @override_settings(OWNER_ALERT_THRESHOLD_POINTS=0)
    def test_zero_threshold_disables(self):
        ok = maybe_alert_large_settle(self.owner, 999999,
                                      triggered_by=self.owner, task_ref=("", ""))
        self.assertFalse(ok)
