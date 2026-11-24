"""派发闸测试：槽位-预扣顺序 / 每日时长 / 覆盖表 / 402 信封（design D3）。"""
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.billing.models import EntryStatus, EntryType, UserQuotaOverride, Wallet, WalletEntry
from apps.billing.services import (
    InsufficientPoints,
    QuotaExceeded,
    TaskOutcome,
    acquire_slot,
    check_daily_render,
    consume_daily_render,
    daily_render_used,
    dispatch_gate,
    effective_limits,
    grant,
    release_slot,
)

User = get_user_model()


def _mk_user(username, **kw):
    with patch("apps.billing.services.grant"):
        return User.objects.create_user(
            username=username, password="pass-12345678",
            email=f"{username}@x.com", **kw)


def _reset_store():
    """清空 MemoryStore 中 billing 的槽位/日累计 key（单例跨测试泄漏）。"""
    from apps.ai_gateway.redis_client import MemoryStore, get_store
    store = get_store()
    if isinstance(store, MemoryStore):
        for k in list(store._data):
            if str(k).startswith("billing:"):
                store._data.pop(k, None)


class SlotOrderTests(TestCase):
    """任务 3.1：先槽位后预扣；预扣失败槽位归还；开关关闭跳过。"""

    def setUp(self):
        _reset_store()
        self.owner = _mk_user("owner")
        grant(self.owner, 1000, ref_kind="test", ref_id="seed")

    def test_slot_full_no_hold_created(self):
        """槽位满 → QuotaExceeded 且不产生 hold。"""
        # 先占满 2 个 render 槽（全局默认 2）
        acquire_slot(self.owner, "render")
        acquire_slot(self.owner, "render")
        with self.assertRaises(QuotaExceeded) as ctx:
            dispatch_gate(self.owner, ("task", "T1"), 100, slot_kind="render")
        self.assertEqual(ctx.exception.kind, "render_slots")
        self.assertEqual(
            WalletEntry.objects.filter(type=EntryType.HOLD).count(), 0)
        Wallet.objects.get(user=self.owner).balance == 1000

    def test_insufficient_points_releases_slot(self):
        """预扣失败 → 槽位归还（finally 断言）。"""
        # 占 1 个槽后预扣超额 → InsufficientPoints，且槽位回到 1
        acquire_slot(self.owner, "render")
        with patch("apps.billing.services.wallet.hold_for_task",
                   side_effect=InsufficientPoints(99999, 1)):
            with self.assertRaises(InsufficientPoints):
                dispatch_gate(self.owner, ("task", "T1"), 99999, slot_kind="render")
        # 槽位应已归还：还能取到第 2 个槽（默认限 2，已占 1）
        self.assertTrue(acquire_slot(self.owner, "render"))
        # 释放后计数恢复
        release_slot(self.owner, "render")
        release_slot(self.owner, "render")

    def test_zero_estimate_skips_hold_but_takes_slot(self):
        """免费任务：不落 hold，但槽位仍占用。"""
        result = dispatch_gate(self.owner, ("task", "T1"), 0, slot_kind="render")
        self.assertIsNone(result)
        self.assertEqual(
            WalletEntry.objects.filter(type=EntryType.HOLD).count(), 0)
        # 槽位已占 1
        acquire_slot(self.owner, "render")
        with self.assertRaises(QuotaExceeded):
            dispatch_gate(self.owner, ("task", "T2"), 0, slot_kind="render")
        release_slot(self.owner, "render")
        release_slot(self.owner, "render")

    @override_settings(ENABLE_BILLING_HOLD=False)
    def test_switch_off_skips_gate(self):
        """开关关闭：跳过槽位与预扣直接派发。"""
        acquire_slot(self.owner, "render")
        acquire_slot(self.owner, "render")  # 占满
        result = dispatch_gate(self.owner, ("task", "T1"), 100, slot_kind="render")
        self.assertIsNone(result)
        self.assertEqual(
            WalletEntry.objects.filter(type=EntryType.HOLD).count(), 0)

    def test_task_outcome_success_settles(self):
        r = dispatch_gate(self.owner, ("task", "T1"), 100, slot_kind=None)
        TaskOutcome.success(r, 80)
        w = Wallet.objects.get(user=self.owner)
        # 1000 - 100 + 20(退差额) = 920
        self.assertEqual(w.balance, 920)

    def test_task_outcome_failure_refunds(self):
        r = dispatch_gate(self.owner, ("task", "T1"), 100, slot_kind=None)
        TaskOutcome.failure(r, reason="boom")
        w = Wallet.objects.get(user=self.owner)
        self.assertEqual(w.balance, 1000)
        r.entry.refresh_from_db()
        self.assertEqual(r.entry.status, EntryStatus.RELEASED)


class DailyRenderLimitTests(TestCase):
    """任务 3.2：每日渲染时长限额 + UserQuotaOverride。"""

    def setUp(self):
        _reset_store()
        self.owner = _mk_user("render-owner")

    def test_within_limit_passes(self):
        check_daily_render(self.owner, 1800)
        consume_daily_render(self.owner.pk, 1800)
        check_daily_render(self.owner, 1800)  # 累计 3600 = 上限，不抛

    def test_over_limit_raises(self):
        """超限当日拒派（4029 语义）。"""
        consume_daily_render(self.owner.pk, 3600)
        with self.assertRaises(QuotaExceeded) as ctx:
            check_daily_render(self.owner, 1)
        self.assertEqual(ctx.exception.kind, "daily_render")

    def test_override_changes_limit(self):
        """覆盖表数值生效。"""
        UserQuotaOverride.objects.create(
            user=self.owner, daily_render_seconds=100, override_enabled=True)
        consume_daily_render(self.owner.pk, 60)
        with self.assertRaises(QuotaExceeded):
            check_daily_render(self.owner, 60)

    def test_override_disabled_falls_back_to_default(self):
        """管理员置 override_enabled=False → 回退全局默认。"""
        UserQuotaOverride.objects.create(
            user=self.owner, daily_render_seconds=1, override_enabled=False)
        consume_daily_render(self.owner.pk, 3600)
        check_daily_render(self.owner, 0)  # 全局 3600：恰好不抛

    def test_override_blank_field_falls_back(self):
        """覆盖行字段空 → 该字段回退全局默认。"""
        UserQuotaOverride.objects.create(
            user=self.owner, daily_render_seconds=None, override_enabled=True)
        limits = effective_limits(self.owner)
        self.assertEqual(limits["daily_render_seconds"], 3600)  # settings 默认


class InsufficientPointsEnvelopeTests(TestCase):
    """任务 3.3：402/4001 错误信封形状。"""

    def setUp(self):
        self.owner = _mk_user("poor")
        grant(self.owner, 50, ref_kind="test", ref_id="seed")
        # 走真实派发路径的视图不易构造（dispatch 在 Celery 前），此处用
        # estimate→gate→异常→渲染的信封链验证（dispatch_task 的接线测试在
        # 集成回归批次覆盖）。
        from apps.creation.models import Play
        self.play = Play.objects.create(title="p", created_by=self.owner)

    def _render_402(self):
        """模拟派发闸余额不足时视图应渲染的信封（对齐 design D6）。"""
        try:
            dispatch_gate(self.owner, ("task", "T1"), 100, slot_kind=None)
        except InsufficientPoints as exc:
            from django.http import JsonResponse
            return JsonResponse(
                status=402,
                data={
                    "code": 4001,
                    "msg": "积分不足",
                    "data": {
                        "errors": {"points": "积分不足"},
                        "reason": "insufficient_points",
                        "needed": exc.needed,
                        "balance": exc.balance,
                    },
                })
        self.fail("应抛 InsufficientPoints")

    def test_envelope_shape(self):
        from apps.ai_gateway.http import _MAX_REDIRECTS  # noqa: F401 — 环境自检
        resp = self._render_402()
        self.assertEqual(resp.status_code, 402)
        import json
        body = json.loads(resp.content)
        self.assertEqual(body["code"], 4001)
        self.assertEqual(body["data"]["reason"], "insufficient_points")
        self.assertEqual(body["data"]["needed"], 100)
        self.assertEqual(body["data"]["balance"], 50)

    def test_exact_balance_succeeds_to_zero(self):
        """预扣恰好等于余额 → 成功且归零。"""
        grant(self.owner, 50, ref_kind="test", ref_id="seed2")  # 现有 50；再发 50 → 100
        r = dispatch_gate(self.owner, ("task", "T1"), 100, slot_kind=None)
        self.assertIsNotNone(r)
        w = Wallet.objects.get(user=self.owner)
        self.assertEqual(w.balance, 0)
