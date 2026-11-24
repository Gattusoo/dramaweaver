"""剧内成员体系：建表与冻结字段。

拆分自自动生成的 0011：本迁移只建 PlayMember / PlayInvitation / AuditLog
与 Play.frozen_reason；authorized_users 的数据回填在 0012、删列在 0013，
保证回填与删列之间旧字段与新表同时可读（design.md Migration/Rollout）。
"""
import django.db.models.deletion
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ("creation", "0010_script_versions"),
    ]

    operations = [
        migrations.AddField(
            model_name="play",
            name="frozen_reason",
            field=models.TextField(blank=True, verbose_name="冻结原因"),
        ),
        migrations.CreateModel(
            name="PlayMember",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[("owner", "属主"), ("editor", "编辑"), ("viewer", "只读")], default="viewer", max_length=10, verbose_name="剧内角色")),
                ("status", models.CharField(choices=[("invited", "已邀请"), ("active", "已生效"), ("removed", "已移除")], default="active", max_length=10, verbose_name="成员状态")),
                ("invited_at", models.DateTimeField(blank=True, null=True, verbose_name="邀请时间")),
                ("accepted_at", models.DateTimeField(blank=True, null=True, verbose_name="接受时间")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="创建时间")),
                ("play", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="members", to="creation.play", verbose_name="所属短剧")),
                ("user", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="play_memberships", to=settings.AUTH_USER_MODEL, verbose_name="成员用户")),
            ],
            options={
                "verbose_name": "剧内成员",
                "verbose_name_plural": "剧内成员",
                "db_table": "shortplay_play_member",
            },
        ),
        migrations.CreateModel(
            name="PlayInvitation",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("role", models.CharField(choices=[("owner", "属主"), ("editor", "编辑"), ("viewer", "只读")], default="viewer", max_length=10, verbose_name="邀请角色")),
                ("status", models.CharField(choices=[("pending", "待接受"), ("accepted", "已接受"), ("expired", "已过期"), ("revoked", "已撤销")], default="pending", max_length=10, verbose_name="邀请状态")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="邀请时间")),
                ("expires_at", models.DateTimeField(verbose_name="过期时间")),
                ("created_by", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="sent_play_invitations", to=settings.AUTH_USER_MODEL, verbose_name="邀请人")),
                ("invitee", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="play_invitations", to=settings.AUTH_USER_MODEL, verbose_name="被邀用户")),
                ("play", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="invitations", to="creation.play", verbose_name="所属短剧")),
            ],
            options={
                "verbose_name": "成员邀请",
                "verbose_name_plural": "成员邀请",
                "db_table": "shortplay_play_invitation",
            },
        ),
        migrations.CreateModel(
            name="AuditLog",
            fields=[
                ("id", models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name="ID")),
                ("action", models.CharField(max_length=50, verbose_name="动作")),
                ("target_type", models.CharField(blank=True, max_length=50, verbose_name="目标类型")),
                ("target_id", models.CharField(blank=True, max_length=64, verbose_name="目标ID")),
                ("detail", models.JSONField(default=dict, verbose_name="详情")),
                ("created_at", models.DateTimeField(auto_now_add=True, verbose_name="时间")),
                ("actor", models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name="audit_actions", to=settings.AUTH_USER_MODEL, verbose_name="操作者")),
                ("play", models.ForeignKey(blank=True, null=True, on_delete=django.db.models.deletion.SET_NULL, related_name="audit_logs", to="creation.play", verbose_name="相关短剧")),
            ],
            options={
                "verbose_name": "审计日志",
                "verbose_name_plural": "审计日志",
                "db_table": "shortplay_audit_log",
                "ordering": ["-created_at"],
            },
        ),
        migrations.AddConstraint(
            model_name="playmember",
            constraint=models.UniqueConstraint(fields=("play", "user"), name="uniq_play_member"),
        ),
        migrations.AlterModelOptions(
            name="auditlog",
            options={"verbose_name": "审计日志", "verbose_name_plural": "审计日志", "ordering": ["-created_at"]},
        ),
        migrations.AddIndex(
            model_name="playmember",
            index=models.Index(fields=["user", "status"], name="idx_member_user_status"),
        ),
        migrations.AddIndex(
            model_name="playinvitation",
            index=models.Index(fields=["invitee", "status"], name="idx_invite_invitee_status"),
        ),
        migrations.AddIndex(
            model_name="playinvitation",
            index=models.Index(fields=["play", "status"], name="idx_invite_play_status"),
        ),
        migrations.AddIndex(
            model_name="auditlog",
            index=models.Index(fields=["play", "created_at"], name="idx_audit_play_time"),
        ),
    ]
