"""前后端联调阻断项修复的回归测试。

覆盖两项：
1. 显式启动入口 POST /api/plays/{id}/start/（status 只读后的替代启动链路）
2. 创建短剧允许 agent_config_id=null（默认配方）

说明：原计划的"分镜结构完成屏障"已回退——既有的 _episode_materials_ready 已含
"存在无镜头场景则不就绪"检查，且 generate_storyboard 在单事务内原子落库整套镜头，
worker 不会看到半套镜头；额外的 STORYBOARD 资产屏障会破坏既有素材齐备契约（7 项测试失败）。

媒体访问（DEBUG 下 Django 服务 /media、Vite 代理）为部署/开发服务器配置，
无法在不污染真实 MEDIA_ROOT 的情况下稳定单测，不做自动化断言。
"""
from unittest.mock import MagicMock, patch

from rest_framework.test import APIClient

from apps.creation.models import PlayStatus
from apps.core.system.models import UserRole

from .test_pipeline import BasePipelineTest


class StartActionTests(BasePipelineTest):
    """显式启动入口：替代 PATCH status（已只读）。"""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.producer)

    @patch("apps.creation.pipeline.dispatcher.send_task", side_effect=lambda *a, **k: MagicMock(id="c1"))
    def test_start_dispatches_outline(self, mock_send):
        resp = self.client.post(f"/api/plays/{self.play.pk}/start/")
        self.assertEqual(resp.status_code, 200)
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.OUTLINING)
        names = [c.args[0] for c in mock_send.call_args_list]
        self.assertIn("shortplay.tasks.generate_outline", names)

    def test_start_requires_draft(self):
        self.play.status = PlayStatus.SCRIPTING
        self.play.save(update_fields=["status"])
        resp = self.client.post(f"/api/plays/{self.play.pk}/start/")
        self.assertEqual(resp.status_code, 409)

    def test_start_requires_episodes(self):
        self.play.episodes.all().delete()
        resp = self.client.post(f"/api/plays/{self.play.pk}/start/")
        self.assertEqual(resp.status_code, 400)

    def test_patch_status_is_ignored(self):
        """确认回归前提：PATCH status 不再启动流水线（只读字段被忽略）。"""
        resp = self.client.patch(f"/api/plays/{self.play.pk}/", {"status": "OUTLINING"}, format="json")
        self.assertEqual(resp.status_code, 200)
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.DRAFT)


class NullRecipeTests(BasePipelineTest):
    """创建短剧允许默认配方（agent_config_id=null）。"""

    def setUp(self):
        super().setUp()
        self.client = APIClient()
        self.client.force_authenticate(self.producer)

    def test_create_with_null_recipe(self):
        resp = self.client.post("/api/plays/", {
            "title": "无配方短剧", "agent_config_id": None,
            "audit_mode": "KEY_CHECKPOINTS", "redo_granularity": "SHOT",
            "consistency_mode": "NONE",
        }, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        body = resp.json()
        self.assertIsNone(body["data"]["agent_config"])