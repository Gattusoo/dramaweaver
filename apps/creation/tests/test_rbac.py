"""剧内成员三层权限（view / edit / manage）回归测试。

2026-11-16 重构后：判定只看 PlayMember（status=active）；全局四级角色已退役，
平台管理员标记不赋予业务接口访问权（跨剧查看走审核通道）。
"""
from rest_framework.test import APITestCase

from apps.creation.models import (
    Asset, AssetLibrary, AssetType, Episode, MemberRole, MemberStatus, Play,
    PlayMember, ReviewLog,
)
from apps.distribution.models import Publication
from dvadmin.system.models import CustomUser

PWD = "Passw0rd!123"


def _user(name, **kw):
    return CustomUser.objects.create_user(username=name, password=PWD, **kw)


class ApiFixtureMixin:
    def setUp(self):
        self.admin = _user("adm", is_platform_admin=True)
        self.producer = _user("prd")
        self.editor = _user("edt")
        self.viewer = _user("vwr")
        self.play = Play.objects.create(title="剧", created_by=self.producer)
        PlayMember.objects.create(
            play=self.play, user=self.producer, role=MemberRole.OWNER,
            status=MemberStatus.ACTIVE,
        )
        self.ep = Episode.objects.create(play=self.play, episode_number=1)
        from apps.creation.models import Scene, Shot
        self.scene = Scene.objects.create(episode=self.ep, scene_number=1)
        self.shot = Shot.objects.create(scene=self.scene, shot_number=1, dialogue="台词")
        from apps.distribution.models import DistributionChannel
        self.channel = DistributionChannel.objects.create(name="渠道", format_requirements={})

    def add_member(self, user, role):
        return PlayMember.objects.create(
            play=self.play, user=user, role=role, status=MemberStatus.ACTIVE
        )


class DemotedOwnerTests(ApiFixtureMixin, APITestCase):
    """属主转让后权限随剧内角色收缩（替代原全局角色降级语义）。"""

    def test_transferred_owner_becomes_editor(self):
        self.add_member(self.editor, MemberRole.EDITOR)
        self.client.force_authenticate(self.producer)
        resp = self.client.post(
            f"/api/plays/{self.play.pk}/transfer-ownership/",
            {"to_user": self.editor.pk}, format="json",
        )
        self.assertEqual(resp.status_code, 200)
        # 受让方接受后转让生效（design D6）
        self.client.force_authenticate(self.editor)
        self.assertEqual(
            self.client.post(
                "/api/plays/invitations/accept/",
                {"invitation_id": resp.data["invitation_id"]}, format="json",
            ).status_code,
            200,
        )
        # 原属主降为 editor：能改内容、不能 manage
        self.client.force_authenticate(self.producer)
        self.assertEqual(
            self.client.patch(f"/api/shots/{self.shot.pk}/", {"dialogue": "新"}, format="json").status_code,
            200,
        )
        self.assertEqual(
            self.client.patch(f"/api/plays/{self.play.pk}/", {"title": "改"}, format="json").status_code,
            403,
        )
        self.assertEqual(self.client.delete(f"/api/plays/{self.play.pk}/").status_code, 403)

    def test_owner_keeps_manage(self):
        self.client.force_authenticate(self.producer)
        self.assertEqual(
            self.client.patch(f"/api/plays/{self.play.pk}/", {"title": "改名"}, format="json").status_code,
            200,
        )


class EditorScopeTests(ApiFixtureMixin, APITestCase):
    """editor 成员：能改内容、能审核；不能 manage。"""

    def setUp(self):
        super().setUp()
        self.add_member(self.editor, MemberRole.EDITOR)
        self.client.force_authenticate(self.editor)

    def test_editor_can_edit_content(self):
        resp = self.client.patch(f"/api/shots/{self.shot.pk}/", {"dialogue": "新"}, format="json")
        self.assertEqual(resp.status_code, 200)
        resp = self.client.post(f"/api/plays/{self.play.pk}/approve/", {}, format="json")
        self.assertEqual(resp.status_code, 400)  # 过了权限，卡在 review_point

    def test_editor_cannot_manage(self):
        play_url = f"/api/plays/{self.play.pk}/"
        self.assertEqual(self.client.patch(play_url, {"title": "改"}, format="json").status_code, 403)
        self.assertEqual(self.client.post(f"{play_url}publish/", {}, format="json").status_code, 403)
        self.assertEqual(
            self.client.post("/api/episodes/", {"play": self.play.pk, "episode_number": 2},
                             format="json").status_code,
            403,
        )
        self.assertEqual(self.client.delete(f"/api/episodes/{self.ep.pk}/").status_code, 403)
        self.assertEqual(
            self.client.post("/api/publications/", {"episode": self.ep.pk, "channel": self.channel.pk},
                             format="json").status_code,
            403,
        )
        pub = Publication.objects.create(episode=self.ep, channel=self.channel, title="t")
        self.assertEqual(
            self.client.post(f"/api/publications/{pub.pk}/sync/", {"status": "uploading"},
                             format="json").status_code,
            403,
        )


class ViewerScopeTests(ApiFixtureMixin, APITestCase):
    def test_viewer_read_only(self):
        self.add_member(self.viewer, MemberRole.VIEWER)
        self.client.force_authenticate(self.viewer)
        self.assertEqual(self.client.get(f"/api/plays/{self.play.pk}/").status_code, 200)
        self.assertEqual(
            self.client.patch(f"/api/shots/{self.shot.pk}/", {"dialogue": "新"}, format="json").status_code,
            403,
        )


class PlatformAdminScopeTests(ApiFixtureMixin, APITestCase):
    """平台管理员：业务接口不放行，审核通道放行。"""

    def test_business_api_denied(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get(f"/api/plays/{self.play.pk}/").status_code, 404)
        self.assertEqual(self.client.get("/api/plays/").data.get("count", 0), 0)

    def test_review_channel_allowed(self):
        self.client.force_authenticate(self.admin)
        self.assertEqual(
            self.client.get(f"/api/admin/plays/{self.play.pk}/review/").status_code, 200
        )


class AuditReadOnlyTests(ApiFixtureMixin, APITestCase):
    def test_audit_endpoints_reject_writes(self):
        self.client.force_authenticate(self.producer)
        log = ReviewLog.objects.create(play=self.play, reviewer=self.producer,
                                       review_point="OUTLINE", decision="APPROVED")
        self.assertEqual(self.client.get("/api/review-logs/").status_code, 200)
        self.assertEqual(
            self.client.post("/api/review-logs/", {"play": self.play.pk, "reviewer": self.admin.pk,
                                                   "review_point": "OUTLINE", "decision": "APPROVED"},
                             format="json").status_code,
            405,
        )
        self.assertEqual(self.client.delete(f"/api/review-logs/{log.pk}/").status_code, 405)
        self.assertEqual(self.client.post("/api/cost-records/", {}, format="json").status_code, 405)
        self.assertEqual(self.client.post("/api/task-records/", {}, format="json").status_code, 405)


class CrossPlayParentTests(ApiFixtureMixin, APITestCase):
    """写入时所有父级外键都要校验，不能只校验第一个。"""

    def test_cannot_attach_asset_to_foreign_episode(self):
        other = _user("p3")
        victim = Play.objects.create(title="他人", created_by=other)
        PlayMember.objects.create(
            play=victim, user=other, role=MemberRole.OWNER, status=MemberStatus.ACTIVE
        )
        victim_ep = Episode.objects.create(play=victim, episode_number=1)
        self.client.force_authenticate(self.producer)
        resp = self.client.post(
            "/api/assets/",
            {"play": self.play.pk, "episode": victim_ep.pk, "asset_type": AssetType.IMAGE},
            format="json",
        )
        self.assertEqual(resp.status_code, 403)
        self.assertFalse(Asset.objects.filter(episode=victim_ep).exists())


class AssetLibraryPermissionTests(ApiFixtureMixin, APITestCase):
    def _item(self, owner, is_public):
        return AssetLibrary.objects.create(name="素材", asset_type="BGM", owner=owner,
                                           file_url="https://e.com/a.mp3", is_public=is_public)

    def test_owner_manages_own_item(self):
        mine = self._item(self.editor, False)
        self.client.force_authenticate(self.editor)
        url = f"/api/asset-library/{mine.pk}/"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.patch(url, {"name": "新"}, format="json").status_code, 200)
        self.assertEqual(self.client.delete(url).status_code, 204)

    def test_public_item_read_only_for_others(self):
        public = self._item(self.admin, True)
        self.client.force_authenticate(self.editor)
        url = f"/api/asset-library/{public.pk}/"
        self.assertEqual(self.client.get(url).status_code, 200)
        self.assertEqual(self.client.patch(url, {"name": "改"}, format="json").status_code, 403)

    def test_any_authenticated_user_can_upload(self):
        """新语义：所有认证用户可上传（全局 viewer 角色已退役，决策 2 注册即创作者）。"""
        self.client.force_authenticate(self.viewer)
        resp = self.client.post("/api/asset-library/", {"name": "x", "asset_type": "BGM",
                                                        "file_url": "https://e.com/x.mp3"}, format="json")
        self.assertEqual(resp.status_code, 201)


class InviteValidationTests(ApiFixtureMixin, APITestCase):
    def test_invalid_user_ids_rejected(self):
        self.client.force_authenticate(self.producer)
        url = f"/api/plays/{self.play.pk}/invite/"
        for bad in ([999999], ["abc"], [], "1"):
            resp = self.client.post(url, {"user_ids": bad, "role": "viewer"}, format="json")
            self.assertEqual(resp.status_code, 400, bad)
        self.assertEqual(
            self.client.post(url, {"user_ids": [self.editor.pk], "role": "viewer"},
                             format="json").status_code,
            201,
        )

    def test_legacy_authorize_endpoint_retired(self):
        """原 authorize/revoke 端点退役（spec: auth/object-permissions REMOVED）。"""
        self.client.force_authenticate(self.producer)
        for path in ("authorize", "revoke"):
            self.assertEqual(
                self.client.post(f"/api/plays/{self.play.pk}/{path}/",
                                 {"user_ids": [self.editor.pk]}, format="json").status_code,
                404,
            )


class LastPlatformAdminTests(ApiFixtureMixin, APITestCase):
    def test_cannot_remove_last_platform_admin(self):
        self.client.force_authenticate(self.admin)
        url = f"/api/auth/users/{self.admin.pk}/platform-admin/"
        self.assertEqual(
            self.client.put(url, {"is_platform_admin": False}, format="json").status_code, 400
        )
        _user("adm2", is_platform_admin=True)
        self.assertEqual(
            self.client.put(url, {"is_platform_admin": False}, format="json").status_code, 200
        )

    def test_legacy_role_endpoint_retired(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.put(
            f"/api/auth/users/{self.admin.pk}/role/", {"role": "viewer"}, format="json"
        )
        self.assertEqual(resp.status_code, 404)
