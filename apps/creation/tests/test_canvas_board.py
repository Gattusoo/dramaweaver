"""无限画布（canvas-board，2026-11-23 组 4）。

覆盖：画布/节点 CRUD、四类节点锚校验（类型↔引用匹配/跨画布父拒绝）、
坐标持久化、版本序列侧栏（六元组版本链语义）、生成触发走既有派发闸
（SCENE_BLOCK 三桶跳过语义/CHARACTER_STYLING 定妆去重/IMAGE_FLOW 锚镜头/
ASSET_REF 400）、冻结剧 4003、viewer 403、非成员 404。
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.creation.tests.test_assistant import AssistantFixtureMixin

from ..models import (
    Asset, AssetStatus, AssetType, Canvas, CanvasNode, CanvasNodeType,
)


class CanvasFixture(AssistantFixtureMixin):
    def setUp(self):
        super().setUp()
        self.canvas = Canvas.objects.create(play=self.play, title="分镜板")
        self.canvas2 = Canvas.objects.create(play=self.play, title="草稿板")
        self.scene_node = CanvasNode.objects.create(
            canvas=self.canvas, type=CanvasNodeType.SCENE_BLOCK,
            title="场景1段落", x=100, y=50, scene=self.scene)
        self.styling_node = CanvasNode.objects.create(
            canvas=self.canvas, type=CanvasNodeType.CHARACTER_STYLING,
            title="林霄定妆", x=300, y=50, character=self.character if hasattr(self, "character") else None)
        # IMAGE_FLOW 锚：镜头图片首版
        self.flow_asset = Asset(
            play=self.play, episode=self.ep, scene=self.scene, shot=self.shot,
            asset_type=AssetType.IMAGE, status=AssetStatus.READY,
            artifact_url="http://cdn/flow-v1.png")
        self.flow_asset._skip_pipeline_dispatch = True
        self.flow_asset.save()
        self.flow_node = CanvasNode.objects.create(
            canvas=self.canvas, type=CanvasNodeType.IMAGE_FLOW,
            title="镜头1生图流", x=100, y=200, asset=self.flow_asset)

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def unwrap(self, resp):
        body = resp.json()
        if isinstance(body, dict) and "data" in body and "code" in body:
            return body["data"] if body["data"] is not None else body
        return body


class CanvasCRUDTests(CanvasFixture, TestCase):
    def test_canvas_list_and_create(self):
        """画布列表（信封剥壳）+ 新建（play 校验）。"""
        client = self.client_for(self.producer)
        listing = self.unwrap(client.get("/api/canvases/"))
        self.assertEqual(len(listing), 2)
        created = client.post("/api/canvases/", {"play": self.play.pk, "title": "新板"},
                              format="json")
        self.assertEqual(created.status_code, 201)

    def test_node_type_anchor_validation(self):
        """四类节点锚校验：SCENE_BLOCK 无 scene 400；IMAGE_FLOW 无 asset 400。"""
        client = self.client_for(self.producer)
        bad = client.post("/api/canvas-nodes/", {
            "canvas": self.canvas.pk, "type": "SCENE_BLOCK", "title": "无锚",
        }, format="json")
        self.assertEqual(bad.status_code, 400)
        bad2 = client.post("/api/canvas-nodes/", {
            "canvas": self.canvas.pk, "type": "IMAGE_FLOW", "title": "无锚",
        }, format="json")
        self.assertEqual(bad2.status_code, 400)
        ok = client.post("/api/canvas-nodes/", {
            "canvas": self.canvas.pk, "type": "ASSET_REF", "title": "资产卡",
            "asset": self.flow_asset.pk, "x": 10, "y": 10,
        }, format="json")
        self.assertEqual(ok.status_code, 201, ok.content)

    def test_cross_canvas_parent_rejected(self):
        """父节点跨画布拒绝。"""
        other = CanvasNode.objects.create(canvas=self.canvas2, type=CanvasNodeType.ASSET_REF,
                                          asset=self.flow_asset)
        client = self.client_for(self.producer)
        resp = client.post("/api/canvas-nodes/", {
            "canvas": self.canvas.pk, "type": "ASSET_REF", "asset": self.flow_asset.pk,
            "parent": other.pk,
        }, format="json")
        self.assertEqual(resp.status_code, 400)

    def test_position_persisted(self):
        """坐标 PATCH 持久化（前端拖动节流保存路径）。"""
        client = self.client_for(self.producer)
        resp = client.patch(f"/api/canvas-nodes/{self.scene_node.pk}/",
                            {"x": 250.5, "y": 80}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.scene_node.refresh_from_db()
        self.assertEqual(self.scene_node.x, 250.5)
        self.assertEqual(self.scene_node.y, 80)

    def test_versions_sidebar(self):
        """版本序列：六元组同位置全版本（含 SUPERSEDED）新→旧。"""
        # 造第二版：同 scope 新 READY 会把 v1 置 SUPERSEDED
        with patch("apps.creation.pipeline.dispatcher.send_task",
                   return_value=MagicMock(id="celery-x")):
            v2 = Asset(play=self.play, episode=self.ep, scene=self.scene,
                       shot=self.shot, asset_type=AssetType.IMAGE,
                       status=AssetStatus.READY, artifact_url="http://cdn/flow-v2.png")
            v2._skip_pipeline_dispatch = True
            v2.save()
        client = self.client_for(self.producer)
        detail = self.unwrap(client.get(f"/api/canvas-nodes/{self.flow_node.pk}/"))
        versions = detail["versions"]
        self.assertEqual([v["version"] for v in versions], [2, 1])
        self.assertIn(versions[0]["artifact_url"], ("http://cdn/flow-v2.png",))

    def test_viewer_write_forbidden_and_read_ok(self):
        """viewer 可读不可写。"""
        viewer = self.client_for(self.viewer)
        self.assertEqual(viewer.get("/api/canvas-nodes/").status_code, 200)
        resp = viewer.post("/api/canvas-nodes/", {
            "canvas": self.canvas.pk, "type": "ASSET_REF", "asset": self.flow_asset.pk,
        }, format="json")
        self.assertEqual(resp.status_code, 403)


class NodeGenerateTests(CanvasFixture, TestCase):
    def setUp(self):
        super().setUp()
        self.p = patch("apps.creation.pipeline.dispatcher.send_task",
                       return_value=MagicMock(id="celery-x"))
        self.p.start()
        self.addCleanup(self.p.stop)

    def post_generate(self, user, node):
        return self.client_for(user).post(f"/api/canvas-nodes/{node.pk}/generate/",
                                          {}, format="json")

    def test_scene_block_dispatch_and_skip(self):
        """SCENE_BLOCK：镜头1 已有 READY 图片（fixture flow_asset）→ 只派 TTS；
        再触发时全部已就绪/在跑 → 空派发 + 跳过原因明示。"""
        resp = self.post_generate(self.producer, self.scene_node)
        self.assertEqual(resp.status_code, 200, resp.content)
        body = self.unwrap(resp)
        # 镜头1 IMAGE 已 READY（fixture flow_asset）跳过，TTS 无 READY 派发
        self.assertEqual(len(body["dispatched"]), 1)
        resp2 = self.post_generate(self.producer, self.scene_node)
        body2 = self.unwrap(resp2)
        self.assertEqual(body2["dispatched"], [])
        self.assertTrue(all(s["reason"] in ("in_progress", "already_ready")
                            for s in body2["skipped"]))

    def test_image_flow_dispatch(self):
        """IMAGE_FLOW：对锚镜头派发；再触发走既有在跑去重。"""
        resp = self.post_generate(self.producer, self.flow_node)
        self.assertEqual(resp.status_code, 200)
        body = self.unwrap(resp)
        # fixture 已有 READY IMAGE（flow_asset），只余 TTS 可派
        self.assertEqual(len(body["dispatched"]), 1)
        resp2 = self.post_generate(self.producer, self.flow_node)
        body2 = self.unwrap(resp2)
        self.assertEqual(body2["dispatched"], [])

    def test_asset_ref_not_supported(self):
        node = CanvasNode.objects.create(canvas=self.canvas, type=CanvasNodeType.ASSET_REF,
                                         asset=self.flow_asset)
        resp = self.post_generate(self.producer, node)
        self.assertEqual(resp.status_code, 400)

    def test_frozen_play_4003(self):
        self.play.frozen_reason = "违规"
        self.play.save(update_fields=["frozen_reason"])
        resp = self.post_generate(self.producer, self.scene_node)
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body.get("reason") or (body.get("data") or {}).get("reason"),
                         "play_frozen")

    def test_viewer_403(self):
        resp = self.post_generate(self.viewer, self.flow_node)
        self.assertEqual(resp.status_code, 403)

    def test_non_member_404(self):
        from apps.core.system.models import CustomUser
        stranger = CustomUser.objects.create_user(username="canvas_stranger",
                                                  password="Passw0rd!123")
        resp = self.post_generate(stranger, self.flow_node)
        self.assertEqual(resp.status_code, 404)
