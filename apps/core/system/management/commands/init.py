"""初始化基础数据：超级管理员 + 系统配方模板。

可重复执行：已存在的数据按唯一键更新而不重复创建；已存在用户不改密码。
密码来自命令参数或配置，不写死在代码中。
安全：已存在用户只有在 --admin-password 与其密码匹配时才确保管理员标记，
避免开放注册下同名账号被运维命令就地提权。
"""
from django.contrib.auth import get_user_model
from django.core.management.base import BaseCommand, CommandError

UserModel = get_user_model()

# 系统预置配方模板（is_template=True）；name 为唯一键
SYSTEM_AGENT_TEMPLATES = [
    {
        "name": "系统默认配方",
        "description": "开箱即用的默认智能体配方模板。",
        "default_audit_mode": "KEY_CHECKPOINTS",
        "default_redo_granularity": "SHOT",
        "consistency_mode": "NONE",
        "retry_count": 3,
        "fallback_on_limit": True,
        "prompt_templates": {},
        "style_params": {},
    },
]


class Command(BaseCommand):
    help = "初始化超级管理员与系统配方模板（可重复执行）"

    def add_arguments(self, parser):
        parser.add_argument("--admin-username", default="admin", help="超级管理员用户名")
        parser.add_argument(
            "--admin-password",
            default=None,
            help="超级管理员密码；新建时必填，用户已存在时须与既有密码匹配才会确保管理员标记",
        )

    def handle(self, *args, **options):
        username = options["admin_username"]
        password = options["admin_password"]

        user, created = UserModel.objects.get_or_create(
            username=username,
            defaults={"is_staff": True, "is_superuser": True, "role": "admin"},
        )
        if created:
            if not password:
                raise CommandError("新建管理员必须提供 --admin-password")
            user.set_password(password)
            user.save(update_fields=["password"])
            self.stdout.write(self.style.SUCCESS(f"已创建超级管理员：{username}"))
        else:
            # 已存在用户不改密码；仅确保 staff/superuser 标记。
            # 安全：开放注册下，同名账号可能是先抢注的普通用户（甚至是攻击者），
            # 不能就地提权——必须显式传 --admin-password 且匹配，才认作运维自己的账号。
            if not password or not user.check_password(password):
                raise CommandError(
                    f"用户 {username} 已存在且密码与 --admin-password 不匹配，"
                    "拒绝就地提权为管理员（如确为运维账号，请提供正确的 --admin-password）"
                )
            changed = []
            if not user.is_staff:
                user.is_staff = True
                changed.append("is_staff")
            if not user.is_superuser:
                user.is_superuser = True
                changed.append("is_superuser")
            if getattr(user, "role", None) != "admin":
                user.role = "admin"
                changed.append("role")
            if changed:
                user.save(update_fields=changed)
            self.stdout.write(
                self.style.SUCCESS(f"管理员已存在，未改密码：{username}（更新字段：{','.join(changed) or '无'}）")
            )

        # 系统配方模板
        from apps.creation.models import AgentConfig
        tpl_count = 0
        for tpl in SYSTEM_AGENT_TEMPLATES:
            _, c = AgentConfig.objects.update_or_create(
                name=tpl["name"],
                defaults={**tpl, "is_template": True, "created_by": user},
            )
            tpl_count += 1 if c else 0
        self.stdout.write(self.style.SUCCESS(
            f"系统配方模板：{tpl_count} 新建，{len(SYSTEM_AGENT_TEMPLATES) - tpl_count} 已存在"
        ))
