"""资产生成优化（主计划 3.5.3.5-3.5.3.8，design D1-D8）。

覆盖：预览生成（低分辨率参数/独立资产行隔离/重复 409/计费闸标记/
开关 404/只读 403）、版本对比（字段 diff/identical/跨位置 400/
版本清单/历史版本）、批量生成（三桶聚合/各守卫路径/权限/冻结剧）、
配置接线。
"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from apps.ai_gateway.models import AIModel, AIProvider
from apps.creation.models import (
    AgentConfig, Asset, AssetStatus, AssetType, AuditMode, Episode, MemberRole,
    MemberStatus, Play, PlayMember, Scene, Shot, TaskRecord, TaskStatus, TaskType,
)
from dvadmin.system.models import CustomUser, UserRole


def _mk_member(play, user, role):
    PlayMember.objects.create(play=play, user=user, role=role, status=MemberStatus.ACTIVE)


class OptimFixture:
    """预览/对比/批量共用 fixture（对齐 test_preview_redo 惯例）。"""

    def setUp(self):
        super().setUp()
        self.owner = CustomUser.objects.create_user(
            username="ago", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.viewer = CustomUser.objects.create_user(
            username="agv", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.provider = AIProvider.objects.create(
            name="P", provider_type="LLM", adapter_type="deepseek",
            base_url="https://x", api_key_encrypted="", priority=1)
        self.model = AIModel.objects.create(
            provider=self.provider, model_name="m", display_name="M", model_type="LLM")
        self.config = AgentConfig.objects.create(
            name="配方", llm_model=self.model, created_by=self.owner)
        self.play = Play.objects.create(
            title="资产优化剧", agent_config=self.config, created_by=self.owner,
            audit_mode=AuditMode.NONE)
        _mk_member(self.play, self.owner, MemberRole.OWNER)
        _mk_member(self.play, self.viewer, MemberRole.VIEWER)
        self.ep = Episode.objects.create(play=self.play, episode_number=1)
        self.scene = Scene.objects.create(episode=self.ep, scene_number=1)
        self.shot1 = Shot.objects.create(
            scene=self.scene, shot_number=1, visual_prompt="近景", dialogue="台词一")
        self.shot2 = Shot.objects.create(
            scene=self.scene, shot_number=2, visual_prompt="远景", dialogue="")
        self.shot3 = Shot.objects.create(
            scene=self.scene, shot_number=3, visual_prompt="特写", dialogue="台词三")
        # send_task 打桩：不依赖 broker
        p = patch("apps.creation.pipeline.dispatcher.send_task",
                  return_value=MagicMock(id="celery-x"))
        self.send = p.start()
        self.addCleanup(p.stop)

    def mk_asset(self, shot, asset_type, *, status=AssetStatus.READY, **extra):
        return Asset.objects.create(
            play=self.play, episode=self.ep, scene=self.scene, shot=shot,
            asset_type=asset_type, status=status, **extra)

    def dispatch_record(self, shot, asset_type):
        return TaskRecord.objects.create(
            play=self.play, episode=self.ep, scene=self.scene, shot=shot,
            task_type={AssetType.IMAGE: TaskType.IMAGE_GEN,
                       AssetType.AUDIO: TaskType.TTS_GEN}[asset_type])


# ═══ 预览生成（3.5.3.5）══════════════════════════════════════════════════

class FakePreviewGateway:
    def __init__(self):
        self.calls = []

    def call(self, provider_type, config, **payload):
        self.calls.append((provider_type, payload))
        return {"url": "https://x/preview.png", "image_bytes": None, "usage": {}, "cost": 0}


class PreviewGenerationTests(OptimFixture, TestCase):
    """worker 侧：低分辨率参数下发 + 预览行四路隔离（design D1/D2）。"""

    def _run_preview(self):
        from apps.creation.tasks import generate_image
        gw = FakePreviewGateway()
        r = TaskRecord.objects.create(
            play=self.play, episode=self.ep, scene=self.scene, shot=self.shot1,
            task_type=TaskType.IMAGE_GEN,
            input_summary={"preview": True, "shot": self.shot1.pk})
        with patch("apps.creation.tasks._gateway", return_value=gw), \
             patch("apps.creation.tasks._save_image_result",
                   return_value="/media/images/preview.png"):
            generate_image(r.pk)
        return gw, r

    def test_preview_low_res_params(self):
        """预览任务：网关 payload 带 size（低档）与 quality=low。"""
        gw, _ = self._run_preview()
        _, payload = gw.calls[0]
        self.assertEqual(payload.get("quality"), "low")
        self.assertTrue(str(payload.get("size", "")).startswith("512"))

    def test_preview_asset_row_isolated(self):
        """预览行不挂 shot/episode FK、空指纹、metadata 标记齐全。"""
        _, r = self._run_preview()
        r.refresh_from_db()  # _finish 按条件更新写库，测试侧实例需回读
        preview = Asset.objects.filter(
            play=self.play, asset_type=AssetType.IMAGE,
            metadata__preview=True).get()
        self.assertEqual(preview.shot, None)
        self.assertEqual(preview.episode, None)
        self.assertEqual(preview.character, None)
        self.assertEqual(preview.input_fingerprint, "")
        self.assertEqual(preview.metadata["preview_of_shot"], self.shot1.pk)
        self.assertEqual(preview.status, AssetStatus.READY)
        self.assertEqual(r.output_summary.get("preview"), True)

    def test_preview_does_not_pollute_main_chain(self):
        """既有正式图版本链/主图指针不受预览行影响（design D1 四路隔离）。"""
        main = self.mk_asset(self.shot1, AssetType.IMAGE, version=1)
        self._run_preview()
        main.refresh_from_db()
        self.assertEqual(main.status, AssetStatus.READY)  # 未被 SUPERSEDED
        self.shot1.refresh_from_db()
        self.assertEqual(self.shot1.current_image_asset, main)  # 指针不动
        # 合成取图查询（按 shot 过滤 READY）取不到预览行
        compose_visible = Asset.objects.filter(
            shot=self.shot1, asset_type=AssetType.IMAGE, status=AssetStatus.READY)
        self.assertEqual(set(compose_visible), {main})

    @override_settings(ASSET_PREVIEW_SIZE="768x768")
    def test_preview_size_from_settings(self):
        """ASSET_PREVIEW_SIZE 覆盖下发尺寸。"""
        gw, _ = self._run_preview()
        self.assertEqual(gw.calls[0][1].get("size"), "768x768")

    def test_regular_generation_unchanged(self):
        """非预览任务走既有路径：指纹复用逻辑在、产出行挂 shot。"""
        from apps.creation.tasks import generate_image
        gw = FakePreviewGateway()
        r = self.dispatch_record(self.shot1, AssetType.IMAGE)  # input_summary 无 preview
        with patch("apps.creation.tasks._gateway", return_value=gw), \
             patch("apps.creation.tasks._save_image_result",
                   return_value="/media/images/main.png"):
            generate_image(r.pk)
        asset = Asset.objects.get(play=self.play, shot=self.shot1,
                                  asset_type=AssetType.IMAGE)
        self.assertNotIn("preview", asset.metadata)


class PreviewEndpointTests(OptimFixture, APITestCase):
    """端点侧：权限/重复 409/开关 404/冻结剧（design D2/D7）。"""

    def _post(self, user, payload=None):
        self.client.force_authenticate(user)
        return self.client.post("/api/assets/preview-generate/",
                                payload or {"shot_id": self.shot1.pk}, format="json")

    @override_settings(ENABLE_ASSET_PREVIEW=True)
    def test_dispatch_and_dedupe(self):
        """首次派发 201；同镜头进行中预览再请求 409。"""
        resp = self._post(self.owner)
        self.assertEqual(resp.status_code, 201)
        data = resp.json().get("data", resp.json())
        record = TaskRecord.objects.get(pk=data["task_record_id"])
        self.assertTrue(record.input_summary.get("preview"))
        self.assertEqual(record.input_summary.get("shot"), self.shot1.pk)
        # record 置 RUNNING 模拟进行中，再请求 409
        record.status = TaskStatus.RUNNING
        record.save(update_fields=["status"])
        resp2 = self._post(self.owner)
        self.assertEqual(resp2.status_code, 409)

    def test_flag_off_404(self):
        """开关关闭：端点 404。"""
        resp = self._post(self.owner)
        self.assertEqual(resp.status_code, 404)

    @override_settings(ENABLE_ASSET_PREVIEW=True)
    def test_viewer_403(self):
        """只读成员 403。"""
        resp = self._post(self.viewer)
        self.assertEqual(resp.status_code, 403)

    @override_settings(ENABLE_ASSET_PREVIEW=True)
    def test_foreign_shot_404(self):
        """他剧镜头 404（不泄露存在性）。"""
        other_owner = CustomUser.objects.create_user(
            username="agx", password="Passw0rd!123", role=UserRole.PRODUCER)
        other = Play.objects.create(title="他剧", created_by=other_owner)
        other_shot = Shot.objects.create(
            scene=Scene.objects.create(
                episode=Episode.objects.create(play=other, episode_number=1),
                scene_number=1),
            shot_number=1)
        resp = self._post(self.owner, {"shot_id": other_shot.pk})
        self.assertEqual(resp.status_code, 404)

    @override_settings(ENABLE_ASSET_PREVIEW=True)
    def test_frozen_play_rejected(self):
        """冻结剧拒绝（4003 语义）。"""
        self.play.frozen_reason = "违规"
        self.play.save(update_fields=["frozen_reason"])
        resp = self._post(self.owner)
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body.get("reason") or (body.get("data") or {}).get("reason"),
                         "play_frozen")


# ═══ 版本对比（3.5.3.6）══════════════════════════════════════════════════

class AssetCompareTests(OptimFixture, APITestCase):
    """GET /api/assets/{id}/compare/（design D4）。"""

    def _get(self, user, asset_id, query=""):
        self.client.force_authenticate(user)
        return self.client.get(f"/api/assets/{asset_id}/compare/{query}")

    @staticmethod
    def _data(resp):
        return resp.json().get("data", resp.json())

    def test_field_diff(self):
        """两版本 prompt/cost 不同 → 字段差异与费用差额。"""
        v1 = self.mk_asset(self.shot1, AssetType.IMAGE,
                           prompt="旧", cost="0.10", version=1)
        v2 = Asset.objects.create(
            play=self.play, episode=self.ep, scene=self.scene, shot=self.shot1,
            asset_type=AssetType.IMAGE, status=AssetStatus.READY,
            prompt="新", cost="0.25")
        resp = self._get(self.owner, v2.pk, f"?against={v1.pk}")
        self.assertEqual(resp.status_code, 200)
        data = self._data(resp)
        self.assertFalse(data["identical"])
        self.assertEqual((data["version_a"], data["version_b"]), (1, 2))
        fields = {d["field"] for d in data["field_diffs"]}
        self.assertIn("prompt", fields)
        self.assertIn("cost", fields)  # 费用差异也进字段差异（cost 从 status 排除后纳入白名单）
        self.assertEqual(Decimal(data["cost_delta"]), Decimal("0.15"))

    def test_identical_versions(self):
        """完全一致 → identical=true 且无差异。"""
        v1 = self.mk_asset(self.shot1, AssetType.IMAGE, prompt="同", cost="0.10", version=1)
        v2 = Asset.objects.create(
            play=self.play, episode=self.ep, scene=self.scene, shot=self.shot1,
            asset_type=AssetType.IMAGE, status=AssetStatus.READY,
            prompt="同", cost="0.10")
        resp = self._get(self.owner, v2.pk, f"?against={v1.pk}")
        data = self._data(resp)
        self.assertTrue(data["identical"])
        self.assertEqual(data["field_diffs"], [])

    def test_cross_position_rejected(self):
        """不同镜头的资产对比 → 400。"""
        a = self.mk_asset(self.shot1, AssetType.IMAGE, version=1)
        b = self.mk_asset(self.shot2, AssetType.IMAGE, version=1)
        resp = self._get(self.owner, a.pk, f"?against={b.pk}")
        self.assertEqual(resp.status_code, 400)

    def test_cross_play_rejected(self):
        """他剧资产作为对比目标 → 400（不泄露存在性）。"""
        other_owner = CustomUser.objects.create_user(
            username="agp", password="Passw0rd!123", role=UserRole.PRODUCER)
        other = Play.objects.create(title="他剧2", created_by=other_owner)
        other_asset = Asset.objects.create(
            play=other, asset_type=AssetType.IMAGE, status=AssetStatus.READY)
        a = self.mk_asset(self.shot1, AssetType.IMAGE, version=1)
        resp = self._get(self.owner, a.pk, f"?against={other_asset.pk}")
        self.assertEqual(resp.status_code, 400)

    def test_version_listing(self):
        """无 against → 版本清单（含 SUPERSEDED 历史版本）。"""
        v1 = self.mk_asset(self.shot1, AssetType.IMAGE, version=1)
        v1.status = AssetStatus.SUPERSEDED
        v1.save(update_fields=["status"])
        v2 = Asset.objects.create(
            play=self.play, episode=self.ep, scene=self.scene, shot=self.shot1,
            asset_type=AssetType.IMAGE, status=AssetStatus.READY)
        resp = self._get(self.owner, v2.pk)
        data = self._data(resp)
        versions = {v["version"]: v["status"] for v in data["versions"]}
        self.assertEqual(versions, {1: "SUPERSEDED", 2: "READY"})

    def test_viewer_can_compare(self):
        """只读成员可对比（读权限）。"""
        a = self.mk_asset(self.shot1, AssetType.IMAGE, version=1)
        b = Asset.objects.create(
            play=self.play, episode=self.ep, scene=self.scene, shot=self.shot1,
            asset_type=AssetType.IMAGE, status=AssetStatus.READY)
        resp = self._get(self.viewer, b.pk, f"?against={a.pk}")
        self.assertEqual(resp.status_code, 200)


# ═══ 批量生成（3.5.3.7）══════════════════════════════════════════════════

class BatchGenerateTests(OptimFixture, APITestCase):
    """POST /api/plays/{id}/batch-generate/ 三桶语义（design D5）。"""

    def _post(self, user, payload):
        self.client.force_authenticate(user)
        return self.client.post(f"/api/plays/{self.play.pk}/batch-generate/",
                                payload, format="json")

    @staticmethod
    def _data(resp):
        return resp.json().get("data", resp.json())

    def test_three_buckets(self):
        """镜头1图已就绪、镜头2 无对白：派发 1 配音+2 图，reused/skipped 明示。"""
        self.mk_asset(self.shot1, AssetType.IMAGE)  # 镜头1 图片已就绪
        resp = self._post(self.owner, {"scope": "scene", "target_id": self.scene.pk})
        self.assertEqual(resp.status_code, 200)
        data = self._data(resp)
        # 派发：镜头1 配音 + 镜头2/3 图片 + 镜头3 配音
        self.assertEqual(data["summary"]["dispatched"], 4)
        # reused：镜头1 图片
        self.assertEqual(data["summary"]["reused"], 1)
        self.assertEqual(data["reused"][0]["reason"], "already_ready")
        # skipped：镜头2 配音（无对白）
        self.assertEqual(data["summary"]["skipped"], 1)
        self.assertEqual(data["skipped"][0]["reason"], "no_dialogue")
        self.assertEqual(data["summary"]["shot_count"], 3)

    def test_in_progress_dedup(self):
        """在跑任务去重：镜头1 图片任务 RUNNING → skipped(in_progress)。"""
        record = self.dispatch_record(self.shot1, AssetType.IMAGE)
        record.status = TaskStatus.RUNNING
        record.save(update_fields=["status"])
        resp = self._post(self.owner, {"scope": "scene", "target_id": self.scene.pk,
                                       "asset_types": ["IMAGE"]})
        data = self._data(resp)
        reasons = {s["shot_id"]: s["reason"] for s in data["skipped"]}
        self.assertEqual(reasons.get(self.shot1.pk), "in_progress")
        self.assertEqual(data["summary"]["dispatched"], 2)  # 镜头2/3 图片照常

    def test_asset_types_filter(self):
        """asset_types=[AUDIO]：只派配音，图片不派。"""
        resp = self._post(self.owner, {"scope": "scene", "target_id": self.scene.pk,
                                       "asset_types": ["AUDIO"]})
        data = self._data(resp)
        types = {d["asset_type"] for d in data["dispatched"]}
        self.assertEqual(types, {"AUDIO"})
        self.assertEqual(data["summary"]["dispatched"], 2)  # 镜头1/3 有对白

    def test_invalid_scope_rejected(self):
        """未知 scope → 400。"""
        resp = self._post(self.owner, {"scope": "play", "target_id": self.play.pk})
        self.assertEqual(resp.status_code, 400)

    def test_cross_play_target_rejected(self):
        """跨剧目标 → 400。"""
        other_owner = CustomUser.objects.create_user(
            username="agb", password="Passw0rd!123", role=UserRole.PRODUCER)
        other = Play.objects.create(title="他剧3", created_by=other_owner)
        ep = Episode.objects.create(play=other, episode_number=1)
        scene = Scene.objects.create(episode=ep, scene_number=1)
        resp = self._post(self.owner, {"scope": "scene", "target_id": scene.pk})
        self.assertEqual(resp.status_code, 400)

    def test_viewer_403(self):
        """只读成员 403（派发即付费）。"""
        resp = self._post(self.viewer, {"scope": "scene", "target_id": self.scene.pk})
        self.assertEqual(resp.status_code, 403)

    def test_frozen_play_rejected(self):
        """冻结剧拒绝（4003）。"""
        self.play.frozen_reason = "违规"
        self.play.save(update_fields=["frozen_reason"])
        resp = self._post(self.owner, {"scope": "scene", "target_id": self.scene.pk})
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body.get("reason") or (body.get("data") or {}).get("reason"),
                         "play_frozen")

    def test_billing_gate_per_task(self):
        """计费闸逐任务判定：占满槽位后批量 → FAILED 记录归 skipped(billing_gate)。"""
        from apps.ai_gateway import redis_client
        from apps.billing.services.quotas import acquire_slot
        with patch.object(redis_client, "_store", redis_client.MemoryStore()):
            for _ in range(10):  # 占满默认 ai 槽位
                acquire_slot(self.owner, "ai")
            resp = self._post(self.owner, {"scope": "scene", "target_id": self.scene.pk,
                                           "asset_types": ["IMAGE"]})
        data = self._data(resp)
        self.assertEqual(data["summary"]["dispatched"], 0)
        # 有对白的镜头照常被计费闸拦截；no_dialogue 属正常守卫不在此断言
        self.assertIn("billing_gate", {s["reason"] for s in data["skipped"]})

    def test_episode_scope(self):
        """scope=episode：整集镜头收集。"""
        resp = self._post(self.owner, {"scope": "episode", "target_id": self.ep.pk})
        data = self._data(resp)
        self.assertEqual(data["summary"]["shot_count"], 3)


# ═══ 配置接线 ════════════════════════════════════════════════════════════

class AssetPreviewConfigTests(TestCase):
    """settings + env.example 含三键（对齐 FeatureFlagWiring 惯例）。"""

    def test_defaults(self):
        from django.conf import settings
        self.assertFalse(settings.ENABLE_ASSET_PREVIEW)
        self.assertEqual(settings.ASSET_PREVIEW_SIZE, "512x512")
        self.assertAlmostEqual(settings.PREVIEW_COST_RATIO, 0.333)

    def test_env_example_contains_keys(self):
        from pathlib import Path
        repo = Path(__file__).resolve().parents[3]
        text = (repo / "conf" / "env.example.py").read_text(encoding="utf-8")
        for key in ("ENABLE_ASSET_PREVIEW", "ASSET_PREVIEW_SIZE", "PREVIEW_COST_RATIO"):
            self.assertIn(key, text)
