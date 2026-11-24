"""认证与剧内成员权限验证测试（openspec: 2026-11-16-backend-play-membership）。

2026-11-16 重构后：全局四级角色退役（仅保留 is_platform_admin 平台标记），
业务权限只看 PlayMember；authorize 端点由邀请接受制取代。
"""
from rest_framework import status
from rest_framework.test import APITestCase

from apps.creation.models import (
    Episode, MemberRole, MemberStatus, Play, PlayMember,
)
from apps.core.system.models import CustomUser


def make_user(username, password="Passw0rd!123", **kw):
    return CustomUser.objects.create_user(username=username, password=password, **kw)


def add_member(play, user, role):
    return PlayMember.objects.create(
        play=play, user=user, role=role, status=MemberStatus.ACTIVE
    )


class AuthFlowTests(APITestCase):
    """任务 6.1 注册/登录/刷新/登出流程。"""

    def test_register_returns_tokens(self):
        resp = self.client.post(
            "/api/auth/register/",
            {"username": "alice", "password": "Passw0rd!123"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        self.assertIn("access", resp.data)
        self.assertIn("refresh", resp.data)
        # 新语义：无全局角色，仅平台管理员标记（默认 False）
        self.assertIs(resp.data["user"]["is_platform_admin"], False)
        self.assertNotIn("role", resp.data["user"])

    def test_register_duplicate_username_rejected(self):
        make_user("bob")
        resp = self.client.post(
            "/api/auth/register/",
            {"username": "bob", "password": "Passw0rd!123"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_400_BAD_REQUEST)

    def test_login_and_me(self):
        make_user("carol")
        resp = self.client.post(
            "/api/auth/login/",
            {"username": "carol", "password": "Passw0rd!123"},
            format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        token = resp.data["access"]
        self.assertEqual(resp.data["user"]["username"], "carol")
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        me = self.client.get("/api/auth/me/")
        self.assertEqual(me.status_code, status.HTTP_200_OK)
        self.assertEqual(me.data["username"], "carol")

    def test_refresh_and_logout(self):
        make_user("dave")
        login = self.client.post(
            "/api/auth/login/",
            {"username": "dave", "password": "Passw0rd!123"},
            format="json",
        )
        refresh = login.data["refresh"]
        token = login.data["access"]
        self.client.credentials(HTTP_AUTHORIZATION=f"Bearer {token}")
        # refresh 出新 access
        r = self.client.post("/api/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(r.status_code, status.HTTP_200_OK)
        # logout 黑名单 refresh
        lo = self.client.post("/api/auth/logout/", {"refresh": refresh}, format="json")
        self.assertEqual(lo.status_code, status.HTTP_200_OK)
        # 再次使用已黑名单的 refresh 应失败
        r2 = self.client.post("/api/auth/refresh/", {"refresh": refresh}, format="json")
        self.assertEqual(r2.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_me_requires_auth(self):
        resp = self.client.get("/api/auth/me/")
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)


class PlatformAdminFlagTests(APITestCase):
    """任务 4.2/6.2 平台管理员标记变更（原全局角色变更端点的替代）。"""

    def setUp(self):
        self.admin = make_user("admin", is_platform_admin=True)
        self.client.force_authenticate(self.admin)

    def test_admin_can_flag_user(self):
        target = make_user("target")
        resp = self.client.put(
            f"/api/auth/users/{target.pk}/platform-admin/",
            {"is_platform_admin": True}, format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        target.refresh_from_db()
        self.assertTrue(target.is_platform_admin)

    def test_non_admin_cannot_flag_user(self):
        producer = make_user("prod")
        self.client.force_authenticate(producer)
        target = make_user("t2")
        resp = self.client.put(
            f"/api/auth/users/{target.pk}/platform-admin/",
            {"is_platform_admin": True}, format="json",
        )
        self.assertEqual(resp.status_code, status.HTTP_403_FORBIDDEN)


class PlayPermissionTests(APITestCase):
    """任务 4.3/6.3/6.4/6.5 Play 归属隔离、子实体继承权限、邀请授权。"""

    def setUp(self):
        self.owner = make_user("p")
        self.editor = make_user("e")
        self.viewer = make_user("v")
        self.outsider = make_user("o")
        self.play = Play.objects.create(title="测试短剧", created_by=self.owner)
        add_member(self.play, self.owner, MemberRole.OWNER)
        self.episode = Episode.objects.create(
            play=self.play, episode_number=1, title="第一集"
        )

    def test_any_user_can_create_play(self):
        """决策 2：注册即创作者，任何普通用户可建剧（owner 行同事务创建）。"""
        resp = self.client.post(
            "/api/plays/", {"title": "新剧", "total_episodes": 5}, format="json"
        )  # setUp 未认证 -> 401；先认证
        self.assertEqual(resp.status_code, status.HTTP_401_UNAUTHORIZED)
        self.client.force_authenticate(self.outsider)
        resp = self.client.post(
            "/api/plays/", {"title": "新剧", "total_episodes": 5}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_201_CREATED)
        play = Play.objects.get(pk=resp.data["id"])
        self.assertTrue(
            PlayMember.objects.filter(
                play=play, user=self.outsider, role=MemberRole.OWNER,
                status=MemberStatus.ACTIVE,
            ).exists()
        )

    def test_play_isolation(self):
        # 非成员看不到别人的 play
        self.client.force_authenticate(self.outsider)
        resp = self.client.get("/api/plays/")
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(resp.data["total"], 0)

    def test_child_entity_inherits_access(self):
        # 未加入的 viewer 访问 episode -> 归属隔离（404，不泄露存在性）
        self.client.force_authenticate(self.viewer)
        resp = self.client.get(f"/api/episodes/{self.episode.pk}/")
        self.assertEqual(resp.status_code, status.HTTP_404_NOT_FOUND)
        # 未加入者列表隔离
        lst = self.client.get("/api/episodes/")
        self.assertEqual(lst.data["total"], 0)

    def test_invited_viewer_gets_read_only(self):
        """邀请接受后 viewer 可读；写仍 403（替代原 authorize 语义）。"""
        from apps.creation.models import PlayInvitation
        from django.utils import timezone
        from datetime import timedelta

        inv = PlayInvitation.objects.create(
            play=self.play, invitee=self.viewer, role=MemberRole.VIEWER,
            created_by=self.owner, expires_at=timezone.now() + timedelta(days=1),
        )
        self.client.force_authenticate(self.viewer)
        resp = self.client.post(
            "/api/plays/invitations/accept/", {"invitation_id": inv.pk}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
        self.assertEqual(
            self.client.get(f"/api/episodes/{self.episode.pk}/").status_code,
            status.HTTP_200_OK,
        )
        self.assertEqual(
            self.client.patch(
                f"/api/episodes/{self.episode.pk}/", {"title": "改"}, format="json"
            ).status_code,
            status.HTTP_403_FORBIDDEN,
        )

    def test_editor_member_can_write(self):
        add_member(self.play, self.editor, MemberRole.EDITOR)
        self.client.force_authenticate(self.editor)
        resp = self.client.patch(
            f"/api/episodes/{self.episode.pk}/", {"title": "改"}, format="json"
        )
        self.assertEqual(resp.status_code, status.HTTP_200_OK)
