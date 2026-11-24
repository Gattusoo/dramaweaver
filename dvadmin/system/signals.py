"""账号注销钩子（auth/membership 任务 4.4）。

规则：有 owner 剧未处置（转让或删除）则拒绝注销；参与（非属主）的剧成员行
由 PlayMember.user / PlayInvitation.invitee 的 FK CASCADE 自动清理。

本变更只落校验与钩子；账号自助注销端点属后台管理范围。
"""
from django.db.models.signals import pre_delete
from django.dispatch import receiver

from .models import CustomUser


class AccountDeletionBlocked(Exception):
    """账号仍是某部剧的属主，拒绝注销（需先转让或删除该剧）。"""


@receiver(pre_delete, sender=CustomUser, dispatch_uid="system.guard_play_ownership")
def guard_play_ownership(sender, instance, **kwargs):
    from apps.creation.models import Play

    owned = Play.objects.filter(created_by=instance)
    count = owned.count()
    if count:
        sample = "、".join(
            f"{title or '未命名'}(#{pk})" for pk, title in owned.values_list("pk", "title")[:3]
        )
        raise AccountDeletionBlocked(
            f"该账号仍是 {count} 部短剧的属主（{sample}{'…' if count > 3 else ''}），"
            "请先转让或删除后再注销"
        )
