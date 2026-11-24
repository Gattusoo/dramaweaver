"""导演台 2D（director-stage，2026-11-23 组 5）。

覆盖：导演台场景/元素 CRUD（摆位须引角色、运镜须 polyline、跨剧元素 400）、
镜头绑定（类型匹配/跨剧绑定拒绝/OneToOne 语义）、构图检查标注（覆盖/追加）、
参考帧注入（_video_segment 走既有 VIDEO 派发闸——prompt/last_frame 参数透传、
无绑定时不注入）、viewer 403、非成员 404。
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.creation.tests.test_assistant import AssistantFixtureMixin

from ..models import (
    Asset, AssetStatus, AssetType, Character, DirectorElement, DirectorElementType,
    DirectorScene, ShotDirecting,
)


class DirectorFixture(AssistantFixtureMixin):
    def setUp(self):
        super().setUp()
        self.character = Character.objects.create(
            play=self.play, name="林霄", role="PROTAGONIST")
        self.dscene = DirectorScene.objects.create(
            play=self.play, title="天台对峙",
            ground_plan={"width": 20, "height": 12, "grid": 1})
        self.camera = DirectorElement.objects.create(
            scene=self.dscene, type=DirectorElementType.CAMERA,
            label="主机位", x=5, y=2, heading=90)
        self.placement = DirectorElement.objects.create(
            scene=self.dscene, type=DirectorElementType.PLACEMENT,
            label="林霄位", x=8, y=6, character=self.character)
        self.move = DirectorElement.objects.create(
            scene=self.dscene, type=DirectorElementType.CAMERA_MOVE,
            label="推进", x=5, y=2, heading=90,
            path=[[5, 2], [7, 4], [9, 6]], move_instruction="缓慢推近至特写")
        # 他剧导演台（跨剧拒绝用）
        from ..models import Play, PlayStatus
        from apps.core.system.models import CustomUser
        other_owner = CustomUser.objects.create_user(username="dso", password="Passw0rd!123")
        other_play = Play.objects.create(title="他剧", created_by=other_owner,
                                         status=PlayStatus.PAUSED_AT_SCRIPT)
        self.foreign_scene_d = DirectorScene.objects.create(play=other_play, title="他剧台")

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def unwrap(self, resp):
        body = resp.json()
        if isinstance(body, dict) and "data" in body and "code" in body:
            return body["data"] if body["data"] is not None else body
        return body


class DirectorSceneCRUDTests(DirectorFixture, TestCase):
    def test_scene_list_and_create(self):
        client = self.client_for(self.producer)
        listing = self.unwrap(client.get("/api/director-scenes/"))
        # producer 只能看到自己成员的剧（1 个），他剧不可见
        self.assertEqual(len(listing), 1)
        created = client.post("/api/director-scenes/", {
            "play": self.play.pk, "title": "新台",
            "ground_plan": {"width": 10, "height": 8},
        }, format="json")
        self.assertEqual(created.status_code, 201, created.content)

    def test_scene_bad_ground_plan(self):
        client = self.client_for(self.producer)
        bad = client.post("/api/director-scenes/", {
            "play": self.play.pk, "title": "坏台",
            "ground_plan": {"width": -3},
        }, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_element_placement_requires_character(self):
        client = self.client_for(self.producer)
        bad = client.post("/api/director-elements/", {
            "scene": self.dscene.pk, "type": "PLACEMENT", "label": "无角色",
        }, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_element_move_path_shape(self):
        client = self.client_for(self.producer)
        bad = client.post("/api/director-elements/", {
            "scene": self.dscene.pk, "type": "CAMERA_MOVE", "label": "坏路径",
            "path": [[1, 2], ["x", 3]],
        }, format="json")
        self.assertEqual(bad.status_code, 400)
        # 机位存 polyline 拒绝
        bad2 = client.patch(f"/api/director-elements/{self.camera.pk}/",
                            {"path": [[1, 2]]}, format="json")
        self.assertEqual(bad2.status_code, 400)

    def test_element_scene_scoping(self):
        """跨剧 scene 引用 400（validate_scene 守卫）。"""
        client = self.client_for(self.producer)
        bad = client.post("/api/director-elements/", {
            "scene": self.foreign_scene_d.pk, "type": "CAMERA", "label": "他剧机位",
        }, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_element_type_filter(self):
        client = self.client_for(self.producer)
        cams = self.unwrap(client.get("/api/director-elements/?scene=%d&type=CAMERA" % self.dscene.pk))
        self.assertEqual(len(cams), 1)
        self.assertEqual(cams[0]["label"], "主机位")


class ShotDirectingTests(DirectorFixture, TestCase):
    def test_bind_camera_to_shot(self):
        """机位绑定镜头 + camera_description 注入文本。"""
        client = self.client_for(self.producer)
        resp = client.post("/api/shot-directing/", {
            "shot": self.shot.pk, "camera": self.camera.pk, "camera_move": self.move.pk,
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        body = self.unwrap(resp)
        self.assertIn("机位(5, 2) 朝向90", body["camera_description"])
        self.assertIn("缓慢推近至特写", body["camera_description"])
        # OneToOne：同镜头再绑 400
        resp2 = client.post("/api/shot-directing/", {
            "shot": self.shot.pk, "camera": self.camera.pk,
        }, format="json")
        self.assertEqual(resp2.status_code, 400)

    def test_bind_type_mismatch_rejected(self):
        """运镜元素绑 camera 槽拒绝。"""
        client = self.client_for(self.producer)
        bad = client.post("/api/shot-directing/", {
            "shot": self.shot.pk, "camera": self.move.pk,
        }, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_composition_check_notes(self):
        """构图检查标注：覆盖与追加两档。"""
        directing = ShotDirecting.objects.create(shot=self.shot, camera=self.camera)
        client = self.client_for(self.producer)
        resp = client.post(
            f"/api/shot-directing/{directing.pk}/composition-check/",
            {"notes": ["地平线倾斜"]}, format="json")
        self.assertEqual(resp.status_code, 200)
        directing.refresh_from_db()
        self.assertEqual(directing.composition_notes, ["地平线倾斜"])
        resp2 = client.post(
            f"/api/shot-directing/{directing.pk}/composition-check/",
            {"notes": ["人物出画"], "append": True}, format="json")
        self.assertEqual(resp2.status_code, 200)
        directing.refresh_from_db()
        self.assertEqual(directing.composition_notes, ["地平线倾斜", "人物出画"])
        # 非法形状 400
        bad = client.post(
            f"/api/shot-directing/{directing.pk}/composition-check/",
            {"notes": "不是列表"}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_viewer_write_forbidden(self):
        client = self.client_for(self.viewer)
        resp = client.post("/api/shot-directing/", {
            "shot": self.shot.pk, "camera": self.camera.pk,
        }, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_non_member_404(self):
        from apps.core.system.models import CustomUser
        stranger = CustomUser.objects.create_user(username="ds_stranger",
                                                  password="Passw0rd!123")
        client = self.client_for(stranger)
        self.assertEqual(
            client.get("/api/director-scenes/").status_code, 200)  # 空列表（非 404）
        listing = self.unwrap(client.get("/api/director-scenes/"))
        self.assertEqual(listing, [])
        resp = client.post("/api/shot-directing/", {
            "shot": self.shot.pk, "camera": self.camera.pk,
        }, format="json")
        # shot 跨剧守卫：validate_shot 拒他剧镜头引用
        self.assertEqual(resp.status_code, 400)


class ReferenceFrameInjectionTests(DirectorFixture, TestCase):
    """5.3 参考帧注入：_video_segment 走既有 VIDEO 派发（闸/计费不变）。"""

    def _image_asset(self):
        asset = Asset(play=self.play, episode=self.ep, scene=self.scene,
                      shot=self.shot, asset_type=AssetType.IMAGE,
                      status=AssetStatus.READY, artifact_url="http://cdn/img.png")
        asset._skip_pipeline_dispatch = True
        asset.save()
        return asset

    def _video_segment(self, shot, image_asset):
        from unittest.mock import MagicMock as _M
        from ..tasks import _video_segment as seg
        import tempfile
        from apps.creation.models import AgentConfig
        config = _M()
        config.video_model = object()  # 通过首行守卫
        with tempfile.TemporaryDirectory() as tmp:
            from pathlib import Path
            return seg(Path(tmp), shot, image_asset, config, 3.0)

    def test_no_injection_without_directing(self):
        """无绑定时：gateway 调用不带导演台参数（既有行为不变）。"""
        asset = self._image_asset()
        with patch("apps.creation.tasks._gateway") as gw:
            gw.return_value.call.return_value = {"url": ""}
            self._video_segment(self.shot, asset)
        kwargs = gw.return_value.call.call_args.kwargs
        self.assertNotIn("prompt", kwargs)
        self.assertNotIn("last_frame_image", kwargs)

    def test_injection_with_directing(self):
        """有绑定时：机位视角/运镜指令注入 prompt，参考帧 last 注入。"""
        ShotDirecting.objects.create(
            shot=self.shot, camera=self.camera, camera_move=self.move,
            reference_frames={"first": "http://cdn/f.png", "last": "http://cdn/l.png"})
        asset = self._image_asset()
        with patch("apps.creation.tasks._gateway") as gw:
            gw.return_value.call.return_value = {"url": ""}
            self._video_segment(self.shot, asset)
        kwargs = gw.return_value.call.call_args.kwargs
        self.assertIn("机位(5, 2) 朝向90", kwargs["prompt"])
        self.assertIn("缓慢推近至特写", kwargs["prompt"])
        self.assertEqual(kwargs["last_frame_image"], "http://cdn/l.png")
        # first 参考帧即 image_url 本身（不重复传）
        self.assertEqual(kwargs["image_url"], asset.artifact_url)

    def test_camera_description_only_camera(self):
        """仅机位无运镜：注入只含机位视角。"""
        ShotDirecting.objects.create(shot=self.shot, camera=self.camera)
        asset = self._image_asset()
        with patch("apps.creation.tasks._gateway") as gw:
            gw.return_value.call.return_value = {"url": ""}
            self._video_segment(self.shot, asset)
        kwargs = gw.return_value.call.call_args.kwargs
        self.assertIn("朝向90", kwargs["prompt"])
        self.assertNotIn("last_frame_image", kwargs)
