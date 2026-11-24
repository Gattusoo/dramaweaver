"""钱包三段式测试（design D7：断言全部经 billing/service 真实路径）。"""
from datetime import timedelta
from decimal import Decimal
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone

from apps.billing.models import EntryStatus, EntryType, Wallet, WalletEntry
from apps.billing.services import (
    InsufficientPoints,
    adjust,
    expire_stale_holds,
    get_or_create_wallet,
    grant,
    hold_for_task,
    refund_hold,
    settle_task,
    yuan_to_points,
)

User = get_user_model()


def _mk_user(username="alice"):
    """建用户但拦截注册赠金信号（本套件断言余额精确值）。

    signals 的 post_save 里 `from .services import grant` 每次触发时执行，
    故 patch services.grant 即可拦截；注册赠金的真实验证在 test_grant.py。
    """
    with patch("apps.billing.services.grant") as mock_grant:
        user = User.objects.create_user(
            username=username, password="pass-12345678", email=f"{username}@x.com",
        )
    return user


class YuanToPointsTests(TestCase):
    """任务 1.3：¥→分换算，全程禁 float。"""

    def test_basic(self):
        self.assertEqual(yuan_to_points(Decimal("1.00")), 100)
        self.assertEqual(yuan_to_points("0.01"), 1)
        self.assertEqual(yuan_to_points(0), 0)
        self.assertEqual(yuan_to_points(Decimal("10")), 1000)

    def test_half_up_boundary(self):
        """¥0.005 四舍五入 → 1 分。"""
        self.assertEqual(yuan_to_points(Decimal("0.005")), 1)
        self.assertEqual(yuan_to_points(Decimal("0.004")), 0)

    def test_float_rejected(self):
        with self.assertRaises(TypeError):
            yuan_to_points(0.05)

    def test_negative_rejected(self):
        with self.assertRaises(ValueError):
            yuan_to_points(Decimal("-1.00"))

    def test_thousand_entries_no_drift(self):
        """累计千次流水 balance_after 无漂移（重放校验）。"""
        u = _mk_user("drift")
        grant(u, 1_000_000, ref_kind="test", ref_id="seed")
        # 千次 +1/-1 往返
        for i in range(1000):
            adjust(u, 1, note=f"+{i}")
            adjust(u, -1, note=f"-{i}")
        w = Wallet.objects.get(user=u)
        entries = WalletEntry.objects.filter(wallet=w).order_by("created_at", "id")
        expected = 0
        for e in entries:
            expected += e.amount
            self.assertEqual(e.balance_after, expected,
                             f"entry#{e.pk} balance_after 漂移")
        self.assertEqual(w.balance, 1_000_000)


class HoldSettleRefundTests(TestCase):
    """任务 1.2：三段式 + 幂等 + 两向对账 + 过期回收。"""

    def setUp(self):
        self.user = _mk_user()
        self.wallet = get_or_create_wallet(self.user)
        grant(self.user, 500, ref_kind="test", ref_id="seed")

    def test_hold_deducts_and_is_active(self):
        r = hold_for_task(self.user, ("task", "T1"), 200)
        self.assertTrue(r.created)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 300)
        self.assertEqual(r.entry.type, EntryType.HOLD)
        self.assertEqual(r.entry.status, EntryStatus.ACTIVE)
        self.assertEqual(r.entry.amount, -200)

    def test_duplicate_hold_same_ref_noop(self):
        """同 ref 重复 hold：no-op（created=False，余额只扣一次）。"""
        r1 = hold_for_task(self.user, ("task", "T1"), 200)
        r2 = hold_for_task(self.user, ("task", "T1"), 200)
        self.assertTrue(r1.created)
        self.assertFalse(r2.created)
        self.assertEqual(r1.entry.pk, r2.entry.pk)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 300)

    def test_insufficient_raises_with_detail(self):
        with self.assertRaises(InsufficientPoints) as ctx:
            hold_for_task(self.user, ("task", "T1"), 501)
        self.assertEqual((ctx.exception.needed, ctx.exception.balance), (501, 500))

    def test_exact_balance_succeeds_to_zero(self):
        """预扣恰好等于余额 → 成功且余额归零。"""
        hold_for_task(self.user, ("task", "T1"), 500)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 0)

    def test_zero_estimate_no_entry(self):
        r = hold_for_task(self.user, ("task", "T1"), 0)
        self.assertIsNone(r)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 500)
        self.assertEqual(WalletEntry.objects.filter(type=EntryType.HOLD).count(), 0)

    def test_settle_actual_less_refunds_difference(self):
        """实际 150 < 预扣 200 → 退 50。"""
        r = hold_for_task(self.user, ("task", "T1"), 200)
        settle_task(r.entry, 150)
        self.wallet.refresh_from_db()
        # 500 - 200 + 50 = 350
        self.assertEqual(self.wallet.balance, 350)
        refunds = WalletEntry.objects.filter(type=EntryType.REFUND, ref_id="T1")
        self.assertEqual(refunds.count(), 1)
        self.assertEqual(refunds.first().amount, 50)

    def test_settle_actual_more_charges_difference(self):
        """实际 250 > 预扣 200 → 补扣 50。"""
        r = hold_for_task(self.user, ("task", "T1"), 200)
        settle_task(r.entry, 250)
        self.wallet.refresh_from_db()
        # 500 - 200 - 50 = 250
        self.assertEqual(self.wallet.balance, 250)
        fees = WalletEntry.objects.filter(type=EntryType.FEE, ref_id="T1")
        self.assertEqual(fees.count(), 1)
        self.assertEqual(fees.first().amount, -50)

    def test_settle_equal_no_extra_entries(self):
        """实际 == 预扣：无差额流水，仅状态翻转。"""
        r = hold_for_task(self.user, ("task", "T1"), 200)
        settle_task(r.entry, 200)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 300)
        self.assertEqual(
            WalletEntry.objects.filter(ref_id="T1").exclude(type=EntryType.HOLD).count(), 0)

    def test_double_settle_noop(self):
        """settle 后重复 settle：no-op（余额不变，不重复退/扣）。"""
        r = hold_for_task(self.user, ("task", "T1"), 200)
        settle_task(r.entry, 100)   # 退 100 → 400
        settle_task(r.entry, 100)   # no-op
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 400)

    def test_refund_hold_restores_balance(self):
        r = hold_for_task(self.user, ("task", "T1"), 200)
        refund_hold(r.entry, reason="failed")
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 500)
        r.entry.refresh_from_db()
        self.assertEqual(r.entry.status, EntryStatus.RELEASED)

    def test_double_refund_noop(self):
        r = hold_for_task(self.user, ("task", "T1"), 200)
        refund_hold(r.entry)
        refund_hold(r.entry)  # no-op
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 500)

    def test_hold_expired_refunded_balance_restored(self):
        """过期 hold 转 refund 且余额恢复。"""
        past = timezone.now() - timedelta(hours=1)
        r = hold_for_task(self.user, ("task", "T1"), 200, expires_at=past)
        self.assertEqual(r.entry.expires_at, past)
        count = expire_stale_holds()
        self.assertEqual(count, 1)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 500)
        r.entry.refresh_from_db()
        self.assertEqual(r.entry.status, EntryStatus.RELEASED)

    def test_unexpired_hold_not_collected(self):
        future = timezone.now() + timedelta(hours=10)
        hold_for_task(self.user, ("task", "T1"), 200, expires_at=future)
        self.assertEqual(expire_stale_holds(), 0)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 300)

    def test_settled_hold_not_collected_by_expiry(self):
        past = timezone.now() - timedelta(hours=1)
        r = hold_for_task(self.user, ("task", "T1"), 200, expires_at=past)
        settle_task(r.entry, 200)
        self.assertEqual(expire_stale_holds(), 0)
        self.wallet.refresh_from_db()
        self.assertEqual(self.wallet.balance, 300)

    def test_replay_chain_consistency(self):
        """全链重放：entry 累加 == 当前余额（只追加铁律）。"""
        r = hold_for_task(self.user, ("task", "T1"), 200)
        settle_task(r.entry, 150)  # 退 50
        adjust(self.user, 25, note="admin")
        hold_for_task(self.user, ("task", "T2"), 100)
        refund_hold(WalletEntry.objects.get(ref_id="T2", type=EntryType.HOLD))
        w = Wallet.objects.get(user=self.user)
        entries = WalletEntry.objects.filter(wallet=w).order_by("created_at", "id")
        expected = 0
        for e in entries:
            expected += e.amount
            self.assertEqual(e.balance_after, expected)
        self.assertEqual(expected, w.balance)


class GrantTests(TestCase):
    def test_grant_idempotent_by_ref(self):
        u = _mk_user("g1")
        e1 = grant(u, 100, ref_kind="signup", ref_id="1")
        self.assertIsNotNone(e1)
        # 同 ref 重复 → None（幂等）
        self.assertIsNone(grant(u, 100, ref_kind="signup", ref_id="1"))

    def test_grant_zero_or_negative_noop(self):
        u = _mk_user("g2")
        self.assertIsNone(grant(u, 0, ref_kind="x", ref_id="1"))
        self.assertIsNone(grant(u, -5, ref_kind="x", ref_id="2"))
        self.assertEqual(WalletEntry.objects.count(), 0)
