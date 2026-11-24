"""注册赠金（真实信号路径）+ 管理员 adjust 端点测试。"""
from unittest.mock import patch

from django.contrib.auth import get_user_model
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.billing.models import EntryType, Wallet, WalletEntry
from apps.billing.services import get_or_create_wallet

User = get_user_model()


def _mk_admin():
    """管理员建号（拦截赠金，保证列表断言确定）。"""
    with patch("apps.billing.services.grant"):
        return User.objects.create_user(
            username="admin", password="pass-12345678",
            email="admin@x.com", is_platform_admin=True,
        )


def _mk_plain(username="bob"):
    return User.objects.create_user(
        username=username, password="pass-12345678", email=f"{username}@x.com",
    )


class SignupGrantTests(TestCase):
    """任务 2.1：注册 post_save 信号事务内赠金，幂等、可关。"""

    @override_settings(REGISTER_GRANT_POINTS=1000)
    def test_signup_grants_points(self):
        """注册后余额 = 配置值。真实信号路径（不经 _mk_user 的拦截）。"""
        u = User.objects.create_user(
            username="newbie", password="pass-12345678", email="n@x.com")
        w = Wallet.objects.get(user=u)
        self.assertEqual(w.balance, 1000)
        entry = WalletEntry.objects.get(ref_kind="signup", ref_id=str(u.pk))
        self.assertEqual(entry.type, EntryType.GRANT)
        self.assertEqual(entry.amount, 1000)

    @override_settings(REGISTER_GRANT_POINTS=500)
    def test_config_value_respected(self):
        u = User.objects.create_user(
            username="newbie2", password="pass-12345678", email="n2@x.com")
        self.assertEqual(Wallet.objects.get(user=u).balance, 500)

    @override_settings(REGISTER_GRANT_POINTS=0)
    def test_zero_config_disables_grant(self):
        """配置 0 时注册无 grant 行（也无钱包动作）。"""
        u = User.objects.create_user(
            username="newbie3", password="pass-12345678", email="n3@x.com")
        self.assertFalse(
            WalletEntry.objects.filter(ref_kind="signup", ref_id=str(u.pk)).exists())

    @override_settings(REGISTER_GRANT_POINTS=1000)
    def test_duplicate_signal_no_double_grant(self):
        """重复信号不重复发：手动再发一次 post_save（created=False）也不重复。"""
        u = User.objects.create_user(
            username="newbie4", password="pass-12345678", email="n4@x.com")
        # 模拟信号重放：直接再调用 grant 函数（同 ref 幂等）
        from apps.billing.services import grant
        self.assertIsNone(grant(u, 1000, ref_kind="signup", ref_id=str(u.pk)))
        self.assertEqual(Wallet.objects.get(user=u).balance, 1000)
        self.assertEqual(
            WalletEntry.objects.filter(ref_kind="signup", ref_id=str(u.pk)).count(), 1)

    def test_update_not_grant(self):
        """post_save（created=False）不触发赠金：更新已有用户不新增 signup 流水。"""
        u = _mk_plain("existing")  # 建号时赠金一次（settings 默认开）
        before = WalletEntry.objects.filter(ref_kind="signup").count()
        u.first_name = "x"
        u.save()
        u.email = "y@x.com"
        u.save()
        after = WalletEntry.objects.filter(ref_kind="signup").count()
        self.assertEqual(before, after)


class AdminWalletEndpointTests(TestCase):
    """任务 2.2：POST /api/admin/wallets/{user}/adjust/ + GET /api/admin/wallets/。"""

    def setUp(self):
        self.admin = _mk_admin()
        # 拦截建号赠金，保证余额断言确定（admin 建号同样拦截）
        with patch("apps.billing.services.grant"):
            self.target = User.objects.create_user(
                username="target", password="pass-12345678", email="t@x.com")
        self.admin_client = APIClient()
        self.admin_client.force_authenticate(self.admin)
        self.plain_client = APIClient()
        self.plain_client.force_authenticate(self.target)

    def _balance(self):
        return Wallet.objects.get(user=self.target).balance

    def test_adjust_creates_entry_and_audit(self):
        resp = self.admin_client.post(
            f"/api/admin/wallets/{self.target.pk}/adjust/",
            {"amount": 250, "note": "活动奖励"}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(self._balance(), 250)
        # 流水
        e = WalletEntry.objects.get(type=EntryType.ADJUST)
        self.assertEqual(e.amount, 250)
        self.assertEqual(e.note, "活动奖励")
        # 审计
        from apps.creation.models import AuditLog
        audit = AuditLog.objects.filter(action="wallet_adjust").first()
        self.assertIsNotNone(audit)
        self.assertEqual(audit.actor_id, self.admin.pk)
        self.assertEqual(audit.detail.get("amount"), 250)

    def test_adjust_negative(self):
        self.admin_client.post(
            f"/api/admin/wallets/{self.target.pk}/adjust/",
            {"amount": -100, "note": "纠错"}, format="json")
        self.assertEqual(self._balance(), -100)

    def test_adjust_requires_note(self):
        """无 note → 400。"""
        resp = self.admin_client.post(
            f"/api/admin/wallets/{self.target.pk}/adjust/",
            {"amount": 100}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_adjust_rejects_zero(self):
        resp = self.admin_client.post(
            f"/api/admin/wallets/{self.target.pk}/adjust/",
            {"amount": 0, "note": "x"}, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_plain_user_forbidden(self):
        """普通用户 403。"""
        resp = self.plain_client.post(
            f"/api/admin/wallets/{self.target.pk}/adjust/",
            {"amount": 999999, "note": "自肥"}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_list_wallets_admin_only(self):
        self.admin_client.post(
            f"/api/admin/wallets/{self.target.pk}/adjust/",
            {"amount": 250, "note": "x"}, format="json")
        resp = self.admin_client.get("/api/admin/wallets/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        data = body.get("data", body)
        results = data.get("results", data) if isinstance(data, dict) else data
        # 至少包含 target 与 admin 的钱包（余额/近期流水）
        pks = [row.get("user") for row in (results if isinstance(results, list) else [])]
        self.assertIn(self.target.pk, pks)

    def test_list_wallets_plain_forbidden(self):
        resp = self.plain_client.get("/api/admin/wallets/")
        self.assertEqual(resp.status_code, 403)
