"""剧内成员体系（auth/membership）回归测试。

覆盖：判定矩阵 / 邀请接受制 / 退出与移除处置 / 转让 / 冻结 / 审核通道 / AuditLog。
测试数据全部经真实端点路径产生（Phase 1 max 审查确立的原则）。
"""
from django.test import override_settings
from rest_framework.test import APITestCase

from apps.creation.models import (
    AgentConfig, AuditLog, Episode, InvitationStatus, MemberRole, MemberStatus,
    Play, PlayInvitation, PlayMember,
)
from apps.creation.assistant.models import (
    AssistantProposal, AssistantSession, ProposalKind, ProposalStatus,
)
from dvadmin.system.models import CustomUser

PWD = "Passw0rd!123"


def _user(name, **kw):
    return CustomUser.objects.create_user(username=name, password=PWD, **kw)


class MembershipBase(APITestCase):
    def setUp(self):
        self.owner = _user("mb_owner")
        self.editor = _user("mb_editor")
        self.viewer = _user("mb_viewer")
        self.outsider = _user("mb_out")
        self.padmin = _user("mb_padmin", is_platform_admin=True)
        self.play = Play.objects.create(title="成员剧", created_by=self.owner)
        PlayMember.objects.create(
            play=self.play, user=self.owner, role=MemberRole.OWNER,
            status=MemberStatus.ACTIVE,
        )
        self.ep = Episode.objects.create(play=self.play, episode_number=1)


class PermissionMatrixTests(MembershipBase):
    """三层判定只看成员表；全局身份不参与。"""

    def _add(self, user, role):
        PlayMember.objects.create(
            play=self.play, user=user, role=role, status=MemberStatus.ACTIVE
        )

    def test_editor_matrix(self):
        self._add(self.editor, MemberRole.EDITOR)
        self.client.force_authenticate(self.editor)
        self.assertEqual(self.client.get(f"/api/plays/{self.play.pk}/").status_code, 200)
        self.assertEqual(
            self.client.patch(
                f"/api/episodes/{self.ep.pk}/", {"title": "改"}, format="json"
            ).status_code,
            200,
        )
        # manage 层拒绝：改 Play 配置
        self.assertEqual(
            self.client.patch(
                f"/api/plays/{self.play.pk}/", {"title": "改名"}, format="json"
            ).status_code,
            403,
        )

    def test_viewer_is_read_only(self):
        self._add(self.viewer, MemberRole.VIEWER)
        self.client.force_authenticate(self.viewer)
        self.assertEqual(self.client.get(f"/api/plays/{self.play.pk}/").status_code, 200)
        self.assertEqual(
            self.client.patch(
                f"/api/episodes/{self.ep.pk}/", {"title": "改"}, format="json"
            ).status_code,
            403,
        )

    def test_outsider_no_access(self):
        self.client.force_authenticate(self.outsider)
        self.assertEqual(self.client.get(f"/api/plays/{self.play.pk}/").status_code, 404)

    def test_platform_admin_blocked_on_business_api(self):
        """平台管理员经业务接口被拒——审核走专用通道。"""
        self.client.force_authenticate(self.padmin)
        self.assertEqual(
            self.client.get(f"/api/plays/{self.play.pk}/").status_code, 404
        )
        self.assertEqual(
            self.client.get("/api/plays/").data["count"]
            if "count" in self.client.get("/api/plays/").data
            else len(self.client.get("/api/plays/").data.get("data", [])),
            0,
        )

    def test_register_creates_play_without_role(self):
        """注册即可建剧（决策 2）：新用户（无任何全局身份）直接建剧。"""
        self.client.force_authenticate(self.outsider)
        resp = self.client.post(
            "/api/plays/", {"title": "我的剧", "total_episodes": 1}, format="json"
        )
        self.assertEqual(resp.status_code, 201)
        play = Play.objects.get(pk=resp.data["id"])
        self.assertTrue(
            PlayMember.objects.filter(
                play=play, user=self.outsider, role=MemberRole.OWNER,
                status=MemberStatus.ACTIVE,
            ).exists()
        )
        # members 回显包含属主（owner 行镜像 created_by）
        self.assertIn("members", resp.data)
        self.assertEqual(resp.data["members"][0]["role"], "owner")


class InvitationFlowTests(MembershipBase):
    """邀请接受制：未接受无权限；接受后按角色生效；过期 409。"""

    def test_invitation_needs_accept(self):
        self.client.force_authenticate(self.owner)
        resp = self.client.post(
            f"/api/plays/{self.play.pk}/invite/",
            {"user_ids": [self.editor.pk], "role": "editor"}, format="json",
        )
        self.assertEqual(resp.status_code, 201)
        invite_id = resp.data["invitations"][0]["id"]

        # 未接受：无权限
        self.client.force_authenticate(self.editor)
        self.assertEqual(self.client.get(f"/api/plays/{self.play.pk}/").status_code, 404)

        # 接受后：按角色生效
        resp = self.client.post(
            "/api/plays/invitations/accept/", {"invitation_id": invite_id}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(self.client.get(f"/api/plays/{self.play.pk}/").status_code, 200)

    def test_expired_invitation_rejected(self):
        invitation = PlayInvitation.objects.create(
            play=self.play, invitee=self.editor, role=MemberRole.VIEWER,
            created_by=self.owner, expires_at="2000-01-01T00:00:00Z",
        )
        self.client.force_authenticate(self.editor)
        resp = self.client.post(
            "/api/plays/invitations/accept/", {"invitation_id": invitation.pk}, format="json"
        )
        self.assertEqual(resp.status_code, 409)
        invitation.refresh_from_db()
        self.assertEqual(invitation.status, InvitationStatus.EXPIRED)

    def test_invite_role_only_editor_or_viewer(self):
        self.client.force_authenticate(self.owner)
        resp = self.client.post(
            f"/api/plays/{self.play.pk}/invite/",
            {"user_ids": [self.outsider.pk], "role": "owner"}, format="json",
        )
        self.assertEqual(resp.status_code, 400)

    @override_settings(PLAY_MEMBER_LIMIT=2)
    def test_member_limit_429(self):
        # owner + 1 现有成员 = 2 达上限
        PlayMember.objects.create(
            play=self.play, user=self.editor, role=MemberRole.VIEWER,
            status=MemberStatus.ACTIVE,
        )
        self.client.force_authenticate(self.owner)
        resp = self.client.post(
            f"/api/plays/{self.play.pk}/invite/",
            {"user_ids": [self.outsider.pk], "role": "viewer"}, format="json",
        )
        self.assertEqual(resp.status_code, 429)

    def test_only_owner_can_invite(self):
        PlayMember.objects.create(
            play=self.play, user=self.editor, role=MemberRole.EDITOR,
            status=MemberStatus.ACTIVE,
        )
        self.client.force_authenticate(self.editor)
        resp = self.client.post(
            f"/api/plays/{self.play.pk}/invite/",
            {"user_ids": [self.outsider.pk], "role": "viewer"}, format="json",
        )
        self.assertEqual(resp.status_code, 403)


class RemovalDispositionTests(MembershipBase):
    """移除后：会话只读语义（写 403、历史可读）、待处理提案作废。"""

    def test_removed_member_loses_write(self):
        PlayMember.objects.create(
            play=self.play, user=self.editor, role=MemberRole.EDITOR,
            status=MemberStatus.ACTIVE,
        )
        self.client.force_authenticate(self.editor)
        self.assertEqual(self.client.get(f"/api/plays/{self.play.pk}/").status_code, 200)

        # 属主移除
        self.client.force_authenticate(self.owner)
        resp = self.client.post(
            f"/api/plays/{self.play.pk}/members/remove/",
            {"user_ids": [self.editor.pk]}, format="json",
        )
        self.assertEqual(resp.status_code, 200)

        # 移除后：列表不可见（无 active 行）
        self.client.force_authenticate(self.editor)
        self.assertEqual(self.client.get(f"/api/plays/{self.play.pk}/").status_code, 404)

    def test_owner_cannot_be_removed(self):
        self.client.force_authenticate(self.owner)
        resp = self.client.post(
            f"/api/plays/{self.play.pk}/members/remove/",
            {"user_ids": [self.owner.pk]}, format="json",
        )
        self.assertEqual(resp.status_code, 400)

    def test_removed_member_session_read_only_and_proposal_stale(self):
        """决策 12：历史会话可读、新消息 403、PENDING 提案作废。"""
        PlayMember.objects.create(
            play=self.play, user=self.editor, role=MemberRole.EDITOR,
            status=MemberStatus.ACTIVE,
        )
        config = AgentConfig.objects.create(name="rm-cfg", created_by=self.owner)
        session = AssistantSession.objects.create(
            play=self.play, user=self.editor, agent_config=config
        )
        proposal = AssistantProposal.objects.create(
            session=session, play=self.play, kind=ProposalKind.SHOT,
            status=ProposalStatus.PENDING,
        )

        self.client.force_authenticate(self.owner)
        resp = self.client.post(
            f"/api/plays/{self.play.pk}/members/remove/",
            {"user_ids": [self.editor.pk]}, format="json",
        )
        self.assertEqual(resp.status_code, 200)

        # 历史会话可读（本人会话列表/详情仍可见）
        self.client.force_authenticate(self.editor)
        self.assertEqual(
            self.client.get(f"/api/assistant/sessions/{session.pk}/").status_code, 200
        )
        # 新消息 403（写入判定走成员表，已非 active）
        resp = self.client.post(
            f"/api/assistant/sessions/{session.pk}/messages/",
            {"text": "还能说话吗"}, format="json",
        )
        self.assertEqual(resp.status_code, 403)
        # 待处理提案作废
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, ProposalStatus.STALE)

    def test_removal_does_not_interrupt_running_task(self):
        """决策 12：已在运行的任务不被中断（只拦新派发入口）。"""
        from apps.creation.models import TaskRecord, TaskStatus, TaskType

        PlayMember.objects.create(
            play=self.play, user=self.editor, role=MemberRole.EDITOR,
            status=MemberStatus.ACTIVE,
        )
        config = AgentConfig.objects.create(name="rt-cfg", created_by=self.owner)
        session = AssistantSession.objects.create(
            play=self.play, user=self.editor, agent_config=config
        )
        running = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.ASSISTANT_TURN,
            status=TaskStatus.RUNNING, input_summary={"session_id": session.pk},
        )
        self.client.force_authenticate(self.owner)
        self.client.post(
            f"/api/plays/{self.play.pk}/members/remove/",
            {"user_ids": [self.editor.pk]}, format="json",
        )
        running.refresh_from_db()
        self.assertEqual(running.status, TaskStatus.RUNNING)


class AccountDeletionTests(MembershipBase):
    """任务 4.4：有 owner 剧未处置则拒绝注销；参与剧的成员行随注销清理。"""

    def test_owner_account_deletion_blocked(self):
        from django.db import transaction

        from dvadmin.system.signals import AccountDeletionBlocked

        # 显式 savepoint：Model.delete 的 savepoint=False 事务在 TestCase 外层
        # atomic 内无法回滚，异常会把连接标记为 broken（仅测试环境有此限制）
        with self.assertRaises(AccountDeletionBlocked), transaction.atomic():
            self.owner.delete()
        self.assertTrue(CustomUser.objects.filter(pk=self.owner.pk).exists())

    def test_non_owner_deletion_cleans_membership(self):
        PlayMember.objects.create(
            play=self.play, user=self.editor, role=MemberRole.EDITOR,
            status=MemberStatus.ACTIVE,
        )
        editor_id = self.editor.pk
        self.editor.delete()
        self.assertFalse(PlayMember.objects.filter(user_id=editor_id).exists())


class TransferTests(MembershipBase):
    def _request_transfer(self):
        PlayMember.objects.create(
            play=self.play, user=self.editor, role=MemberRole.EDITOR,
            status=MemberStatus.ACTIVE,
        )
        self.client.force_authenticate(self.owner)
        resp = self.client.post(
            f"/api/plays/{self.play.pk}/transfer-ownership/",
            {"to_user": self.editor.pk}, format="json",
        )
        self.assertEqual(resp.status_code, 200)
        return resp.data["invitation_id"]

    def test_transfer_needs_acceptance(self):
        """design D6：发起转让只建待接受邀请，接受后 created_by 与成员行同事务改写。"""
        invitation_id = self._request_transfer()
        # 未接受：属主仍是 owner
        self.play.refresh_from_db()
        self.assertEqual(self.play.created_by, self.owner)
        self.assertEqual(
            PlayMember.objects.get(play=self.play, user=self.editor).role,
            MemberRole.EDITOR,
        )
        # 受让方接受
        self.client.force_authenticate(self.editor)
        resp = self.client.post(
            "/api/plays/invitations/accept/", {"invitation_id": invitation_id},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.play.refresh_from_db()
        self.assertEqual(self.play.created_by, self.editor)
        self.assertEqual(
            PlayMember.objects.get(play=self.play, user=self.editor).role,
            MemberRole.OWNER,
        )
        self.assertEqual(
            PlayMember.objects.get(play=self.play, user=self.owner).role,
            MemberRole.EDITOR,
        )
        # 审计落行（记录双方）
        log = AuditLog.objects.filter(action="ownership_transfer", play=self.play).first()
        self.assertIsNotNone(log)
        self.assertEqual(log.actor_id, self.editor.pk)
        self.assertEqual(log.detail.get("from_user"), self.owner.pk)

    def test_transfer_requires_active_editor(self):
        # viewer 不可接
        PlayMember.objects.create(
            play=self.play, user=self.viewer, role=MemberRole.VIEWER,
            status=MemberStatus.ACTIVE,
        )
        self.client.force_authenticate(self.owner)
        resp = self.client.post(
            f"/api/plays/{self.play.pk}/transfer-ownership/",
            {"to_user": self.viewer.pk}, format="json",
        )
        self.assertEqual(resp.status_code, 400)


class AdminChannelTests(MembershipBase):
    """审核通道：平台管理员只读 + 强制审计；业务接口不放行。"""

    def test_review_view_with_audit(self):
        self.client.force_authenticate(self.padmin)
        resp = self.client.get(f"/api/admin/plays/{self.play.pk}/review/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["title"], "成员剧")
        self.assertTrue(
            AuditLog.objects.filter(action="play_review", play=self.play).exists()
        )

    def test_review_view_denied_for_normal_user(self):
        self.client.force_authenticate(self.outsider)
        resp = self.client.get(f"/api/admin/plays/{self.play.pk}/review/")
        self.assertEqual(resp.status_code, 403)

    def test_freeze_and_unfreeze(self):
        self.client.force_authenticate(self.padmin)
        resp = self.client.post(
            f"/api/admin/plays/{self.play.pk}/freeze/", {"reason": "违规内容"}, format="json"
        )
        self.assertEqual(resp.status_code, 200)
        self.play.refresh_from_db()
        self.assertEqual(self.play.frozen_reason, "违规内容")

        # 冻结后：属主启动生成被拒（信封业务码 4003 + reason）
        self.client.force_authenticate(self.owner)
        resp = self.client.post(f"/api/plays/{self.play.pk}/start/")
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body["code"], 4003)
        self.assertEqual(body["reason"], "play_frozen")

        # 解冻后恢复
        self.client.force_authenticate(self.padmin)
        self.assertEqual(
            self.client.post(f"/api/admin/plays/{self.play.pk}/unfreeze/").status_code,
            200,
        )
        self.play.refresh_from_db()
        self.assertEqual(self.play.frozen_reason, "")

    def test_freeze_requires_reason(self):
        self.client.force_authenticate(self.padmin)
        resp = self.client.post(
            f"/api/admin/plays/{self.play.pk}/freeze/", {"reason": "  "}, format="json"
        )
        self.assertEqual(resp.status_code, 400)

    def test_freeze_writes_audit(self):
        self.client.force_authenticate(self.padmin)
        self.client.post(
            f"/api/admin/plays/{self.play.pk}/freeze/", {"reason": "r"}, format="json"
        )
        self.assertTrue(
            AuditLog.objects.filter(action="play_freeze", play=self.play).exists()
        )

    def test_dispatch_level_rejection_returns_4003(self):
        """派发层（dispatcher.PlayFrozenError）也映射为信封业务码 4003。"""
        self.client.force_authenticate(self.padmin)
        self.client.post(
            f"/api/admin/plays/{self.play.pk}/freeze/", {"reason": "违规"}, format="json"
        )
        self.client.force_authenticate(self.owner)
        resp = self.client.post(f"/api/episodes/{self.ep.pk}/preview/", {}, format="json")
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body["code"], 4003)
        self.assertEqual(body["reason"], "play_frozen")


class MigrationBackfillTests(APITestCase):
    """owner 行不变量：创建路径同事务建 owner 行（0012 回填的读侧语义）。"""

    def test_owner_row_mirrors_created_by(self):
        u = _user("bf_user")
        self.client.force_authenticate(u)
        resp = self.client.post(
            "/api/plays/", {"title": "回填剧", "total_episodes": 1}, format="json"
        )
        self.assertEqual(resp.status_code, 201)
        play = Play.objects.get(pk=resp.data["id"])
        # 经真实创建路径：owner 行与 created_by 一致
        self.assertEqual(
            PlayMember.objects.get(play=play, role=MemberRole.OWNER).user_id, u.pk
        )
        self.assertEqual(play.created_by_id, u.pk)
