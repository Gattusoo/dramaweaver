"""apps/core/system：用户与认证模型（原 dvadmin 脚手架，已并入项目）。"""
from django.contrib.auth.models import AbstractUser
from django.db import models


class UserRole(models.TextChoices):
    ADMIN = "admin", "管理员"
    PRODUCER = "producer", "制片人"
    EDITOR = "editor", "编辑"
    VIEWER = "viewer", "查看者"


class CustomUser(AbstractUser):
    """自定义用户模型。

    全局身份双层（2026-11-16 成员体系重构）：is_platform_admin（平台管理员标记）
    与普通用户；业务内容的访问权全部由剧内成员表（PlayMember）决定。
    role 列保留但已停读——仅作历史数据迁移来源，代码不再消费其业务分级。
    """

    role = models.CharField(
        "角色(已停读)", max_length=20, choices=UserRole.choices, default=UserRole.VIEWER
    )
    is_platform_admin = models.BooleanField("平台管理员", default=False)

    class Meta:
        db_table = "shortplay_user"
        verbose_name = "用户"
        verbose_name_plural = "用户"

    def __str__(self):
        return self.username
