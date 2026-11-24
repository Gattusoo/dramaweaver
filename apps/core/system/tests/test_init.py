"""初始化命令测试（platform/configuration —— 初始化基础数据）。"""
from django.contrib.auth import get_user_model
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase

from apps.creation.models import AgentConfig

UserModel = get_user_model()


class InitCommandTests(TestCase):
    def test_first_init_creates_admin_and_template(self):
        """首次初始化：创建超级管理员与系统配方模板。"""
        call_command("init", admin_username="root", admin_password="Passw0rd!123")
        user = UserModel.objects.get(username="root")
        self.assertTrue(user.is_superuser)
        self.assertTrue(user.is_staff)
        self.assertEqual(user.role, "admin")
        self.assertTrue(user.check_password("Passw0rd!123"))
        self.assertTrue(AgentConfig.objects.filter(is_template=True, name="系统默认配方").exists())

    def test_repeat_init_does_not_duplicate_or_change_password(self):
        """重复执行：不重复创建，已有用户密码不被覆盖。"""
        call_command("init", admin_username="root", admin_password="Passw0rd!123")
        # 手动改密码，再用新密码 init，密码应保持
        user = UserModel.objects.get(username="root")
        user.set_password("Changed!456")
        user.save(update_fields=["password"])
        call_command("init", admin_username="root", admin_password="Changed!456")
        user.refresh_from_db()
        self.assertEqual(UserModel.objects.filter(username="root").count(), 1)
        self.assertTrue(user.check_password("Changed!456"), "已存在用户的密码不应被覆盖")
        self.assertEqual(AgentConfig.objects.filter(is_template=True).count(), 1)

    def test_existing_user_with_mismatched_password_is_not_escalated(self):
        """安全：抢注同名账号后 init 不得就地提权（密码不匹配即拒绝）。"""
        squatter = UserModel.objects.create_user(username="admin", password="attacker!123")
        self.assertFalse(squatter.is_superuser)
        with self.assertRaises(CommandError):
            call_command("init", admin_username="admin", admin_password="Passw0rd!123")
        with self.assertRaises(CommandError):
            call_command("init", admin_username="admin")  # 不传密码同样拒绝
        squatter.refresh_from_db()
        self.assertFalse(squatter.is_superuser)
        self.assertFalse(squatter.is_staff)
        self.assertNotEqual(squatter.role, "admin")
