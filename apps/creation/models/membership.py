"""剧内成员与邀请模型（auth/membership 能力）。

权限判定唯一来源：PlayMember（status=active）。owner 行是 Play.created_by
的镜像——单一事实来源为 created_by，成员表保持一致行，统一成员查询用。
"""
from django.conf import settings
from django.db import models
from django.utils import timezone


class MemberRole(models.TextChoices):
    OWNER = "owner", "属主"
    EDITOR = "editor", "编辑"
    VIEWER = "viewer", "只读"


class MemberStatus(models.TextChoices):
    INVITED = "invited", "已邀请"
    ACTIVE = "active", "已生效"
    REMOVED = "removed", "已移除"


class InvitationStatus(models.TextChoices):
    PENDING = "pending", "待接受"
    ACCEPTED = "accepted", "已接受"
    EXPIRED = "expired", "已过期"
    REVOKED = "revoked", "已撤销"


class PlayMember(models.Model):

    """剧内成员行。(play, user) 唯一；owner 行与 Play.created_by 同事务保持一致。"""

    play = models.ForeignKey(
        "Play", on_delete=models.CASCADE, related_name="members", verbose_name="所属短剧"
    )
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="play_memberships", verbose_name="成员用户",
    )
    role = models.CharField(
        "剧内角色", max_length=10, choices=MemberRole.choices, default=MemberRole.VIEWER
    )
    status = models.CharField(
        "成员状态", max_length=10, choices=MemberStatus.choices, default=MemberStatus.ACTIVE
    )
    invited_at = models.DateTimeField("邀请时间", null=True, blank=True)
    accepted_at = models.DateTimeField("接受时间", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_play_member"
        constraints = [
            models.UniqueConstraint(fields=["play", "user"], name="uniq_play_member"),
        ]
        indexes = [
            models.Index(fields=["user", "status"], name="idx_member_user_status"),
        ]
        verbose_name = "剧内成员"
        verbose_name_plural = "剧内成员"

    def __str__(self):
        return f"{self.user_id}@{self.play_id}={self.role}/{self.status}"


class PlayInvitation(models.Model):
    """成员邀请（接受制）：被邀人显式接受后才建 active 成员行。7 天过期。"""

    play = models.ForeignKey(
        "Play", on_delete=models.CASCADE, related_name="invitations", verbose_name="所属短剧"
    )
    invitee = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="play_invitations", verbose_name="被邀用户",
    )
    role = models.CharField(
        "邀请角色", max_length=10, choices=MemberRole.choices, default=MemberRole.VIEWER
    )
    status = models.CharField(
        "邀请状态", max_length=10, choices=InvitationStatus.choices,
        default=InvitationStatus.PENDING,
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="sent_play_invitations", verbose_name="邀请人",
    )
    created_at = models.DateTimeField("邀请时间", auto_now_add=True)
    expires_at = models.DateTimeField("过期时间")

    class Meta:
        db_table = "shortplay_play_invitation"
        indexes = [
            models.Index(fields=["invitee", "status"], name="idx_invite_invitee_status"),
            models.Index(fields=["play", "status"], name="idx_invite_play_status"),
        ]
        verbose_name = "成员邀请"
        verbose_name_plural = "成员邀请"

    def __str__(self):
        return f"invite {self.invitee_id} to play {self.play_id} as {self.role}"

    @property
    def is_expired(self):
        return self.status == InvitationStatus.PENDING and timezone.now() > self.expires_at


class AuditLog(models.Model):
    """管理动作审计：审核查看、冻结/解冻、转让、成员移除等。只追加。"""

    actor = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="audit_actions", verbose_name="操作者",
    )
    action = models.CharField("动作", max_length=50)
    target_type = models.CharField("目标类型", max_length=50, blank=True)
    target_id = models.CharField("目标ID", max_length=64, blank=True)
    play = models.ForeignKey(
        "Play", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="audit_logs", verbose_name="相关短剧",
    )
    detail = models.JSONField("详情", default=dict)
    created_at = models.DateTimeField("时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_audit_log"
        indexes = [
            models.Index(fields=["play", "created_at"], name="idx_audit_play_time"),
        ]
        ordering = ["-created_at"]
        verbose_name = "审计日志"
        verbose_name_plural = "审计日志"

    def __str__(self):
        return f"{self.actor_id}:{self.action}:{self.target_type}#{self.target_id}"
