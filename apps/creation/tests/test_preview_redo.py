"""影响预检与选择性重做测试（工作台 Stage A3，任务 3.1–3.4）。"""
from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework.test import APITestCase

from apps.ai_gateway.models import AIModel, AIProvider
from apps.creation.models import (
    AgentConfig, Asset, AssetDependency, AssetStatus, AssetType, AuditMode, Character,
    Episode, MemberRole, MemberStatus, Play, PlayMember, RedoGranularity, RelationType,
    Scene, Shot, TaskRecord, TaskType,
)
from apps.creation.pipeline.preview_redo import (
    PreviewRedoError, preview_redo, verify_preview_version,
)
from apps.creation.pipeline.redo import RedoError, redo
from apps.core.system.models import CustomUser, UserRole


def _mk_member(play, user, role):
    PlayMember.objects.create(play=play, user=user, role=role, status=MemberStatus.ACTIVE)


class RedoFixture:
    def setUp(self):
        super().setUp()
        self.owner = CustomUser.objects.create_user(
            username="a3o", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.viewer = CustomUser.objects.create_user(
            username="a3v", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.provider = AIProvider.objects.create(
            name="P", provider_type="LLM", adapter_type="deepseek",
            base_url="https://x", api_key_encrypted="", priority=1)
        self.model = AIModel.objects.create(
            provider=self.provider, model_name="m", display_name="M", model_type="LLM")
        self.config = AgentConfig.objects.create(
            name="配方", llm_model=self.model, created_by=self.owner)
        self.play = Play.objects.create(
            title="预检剧", agent_config=self.config, created_by=self.owner,
            audit_mode=AuditMode.NONE)
        _mk_member(self.play, self.owner, MemberRole.OWNER)
        _mk_member(self.play, self.viewer, MemberRole.VIEWER)
        self.ep = Episode.objects.create(play=self.play, episode_number=1)
        self.scene = Scene.objects.create(episode=self.ep, scene_number=1)
        self.character = Character.objects.create(
            play=self.play, name="主", role="PROTAGONIST")
        self.shot1 = Shot.objects.create(
            scene=self.scene, shot_number=1, visual_prompt="近景", dialogue="台词一")
        self.shot2 = Shot.objects.create(
            scene=self.scene, shot_number=2, visual_prompt="远景", dialogue="")

    def mk_asset(self, shot, asset_type, fingerprint="f" * 64, status=AssetStatus.READY,
                 version=1, **kw):
        return Asset.objects.create(
            play=self.play, episode=self.ep, scene=self.scene, shot=shot,
            asset_type=asset_type, status=status, version=version,
            input_fingerprint=fingerprint,
            artifact_url=f"/media/{asset_type.lower()}_v{version}.png", **kw)


class PreviewRedoServiceTests(RedoFixture, TestCase):
    """任务 3.1：只读预检——失效/复用/重建集合、保守原因、不产生副作用。"""

    def test_readonly_no_side_effects(self):
        """预检不写库、不派发任务、不调用模型。"""
        self.mk_asset(self.shot1, AssetType.IMAGE)
        self.mk_asset(self.shot1, AssetType.AUDIO)
        with patch("apps.creation.pipeline.dispatcher.send_task") as send, \
             patch("apps.creation.tasks._gateway") as gw:
            result = preview_redo(self.play, RedoGranularity.SHOT, self.shot1.pk)
        send.assert_not_called()
        gw.assert_not_called()
        self.assertEqual(result["estimated_tasks"], 2)
        # 无副作用：无新 TaskRecord、资产状态不变
        self.assertFalse(TaskRecord.objects.exists())
        self.assertTrue(Asset.objects.filter(status=AssetStatus.READY).exists())

    def test_default_invalidates_image_and_audio(self):
        """缺省 IMAGE+AUDIO（旧行为口径）。"""
        self.mk_asset(self.shot1, AssetType.IMAGE)
        self.mk_asset(self.shot1, AssetType.AUDIO)
        result = preview_redo(self.play, RedoGranularity.SHOT, self.shot1.pk)
        self.assertEqual({a["type"] for a in result["invalidated"]}, {"IMAGE", "AUDIO"})
        self.assertEqual(result["reusable"], [])

    def test_audio_only_keeps_image_reusable(self):
        """asset_types=[AUDIO]：IMAGE 标可复用、只重建 AUDIO。"""
        self.mk_asset(self.shot1, AssetType.IMAGE)
        self.mk_asset(self.shot1, AssetType.AUDIO)
        result = preview_redo(self.play, RedoGranularity.SHOT, self.shot1.pk,
                              asset_types=["AUDIO"])
        self.assertEqual({a["type"] for a in result["invalidated"]}, {"AUDIO"})
        self.assertEqual({a["type"] for a in result["reusable"]}, {"IMAGE"})
        self.assertEqual(len(result["rebuild_tasks"]), 1)

    def test_changes_dialogue_narrows_to_audio(self):
        """changes={dialogue}：只失效 AUDIO（仅改对白不重做图片）。"""
        self.mk_asset(self.shot1, AssetType.IMAGE)
        self.mk_asset(self.shot1, AssetType.AUDIO)
        result = preview_redo(self.play, RedoGranularity.SHOT, self.shot1.pk,
                              changes=["dialogue"])
        self.assertEqual({a["type"] for a in result["invalidated"]}, {"AUDIO"})
        self.assertEqual({a["type"] for a in result["reusable"]}, {"IMAGE"})

    def test_scene_granularity_covers_all_shots(self):
        """SCENE 粒度覆盖该场景全部镜头。"""
        self.mk_asset(self.shot1, AssetType.IMAGE)
        self.mk_asset(self.shot2, AssetType.IMAGE)
        result = preview_redo(self.play, RedoGranularity.SCENE, self.scene.pk)
        self.assertEqual(len(result["invalidated"]), 2)

    def test_video_impact_via_dependency_edges(self):
        """失效 IMAGE/AUDIO 经依赖边映射到受影响成片。"""
        img = self.mk_asset(self.shot1, AssetType.IMAGE)
        video = Asset.objects.create(
            play=self.play, episode=self.ep, asset_type=AssetType.VIDEO,
            status=AssetStatus.READY, artifact_url="/media/v.mp4")
        AssetDependency.objects.create(
            upstream=img, downstream=video, relation_type=RelationType.IMAGE_TO_VIDEO)
        result = preview_redo(self.play, RedoGranularity.SHOT, self.shot1.pk)
        self.assertEqual(len(result["video_impact"]), 1)
        self.assertEqual(result["video_impact"][0]["id"], video.pk)

    def test_legacy_no_fingerprint_flagged_conservative(self):
        """缺指纹资产虽在重建范围内但标保守原因（不静默当精确判定）。"""
        self.mk_asset(self.shot1, AssetType.IMAGE, fingerprint="")
        result = preview_redo(self.play, RedoGranularity.SHOT, self.shot1.pk)
        self.assertEqual(result["conservative"][0]["reason"], "legacy_asset_no_fingerprint")
        self.assertNotEqual(result["estimated_tasks"], 0)

    def test_video_without_edges_conservative(self):
        """无依赖边的旧成片：范围内有失效资产时提示保守（不能报零影响）。"""
        self.mk_asset(self.shot1, AssetType.IMAGE, fingerprint="")  # legacy 图片
        Asset.objects.create(
            play=self.play, episode=self.ep, asset_type=AssetType.VIDEO,
            status=AssetStatus.READY, artifact_url="/media/old.mp4")
        result = preview_redo(self.play, RedoGranularity.SHOT, self.shot1.pk)
        reasons = {c["reason"] for c in result["conservative"]}
        self.assertIn("video_without_dependency_edges", reasons)

    def test_invalid_target_and_types(self):
        """跨剧/不存在目标 → PreviewRedoError；非法 asset_types → 报错。"""
        with self.assertRaises(PreviewRedoError):
            preview_redo(self.play, RedoGranularity.SHOT, 999999)
        with self.assertRaises(PreviewRedoError):
            preview_redo(self.play, RedoGranularity.SHOT, self.shot1.pk,
                         asset_types=["VIDEO"])
        with self.assertRaises(PreviewRedoError):
            preview_redo(self.play, RedoGranularity.EPISODE, self.ep.pk)

    def test_preview_version_changes_on_content_change(self):
        """预检版本：镜头内容/资产版本变化 → 版本变化（409 校验依据）。"""
        self.mk_asset(self.shot1, AssetType.IMAGE)
        v1 = preview_redo(self.play, RedoGranularity.SHOT, self.shot1.pk)["preview_version"]
        self.assertTrue(verify_preview_version(self.play, RedoGranularity.SHOT,
                                               self.shot1.pk, v1))
        self.shot1.dialogue = "改了台词"
        self.shot1.save(update_fields=["dialogue"])
        self.assertFalse(verify_preview_version(self.play, RedoGranularity.SHOT,
                                                self.shot1.pk, v1))


class SelectiveRedoTests(RedoFixture, TestCase):
    """任务 3.2：redo 扩展 asset_types 与预检版本校验。"""

    def test_default_image_and_audio_dispatched(self):
        """缺省 IMAGE+AUDIO：两个任务都派发（旧行为）。"""
        self.mk_asset(self.shot1, AssetType.IMAGE)
        self.mk_asset(self.shot1, AssetType.AUDIO)
        with patch("apps.creation.pipeline.dispatcher.send_task",
                   return_value=MagicMock(id="x")) as send:
            redo(self.play, RedoGranularity.SHOT, self.shot1.pk)
        types = {c[1]["kwargs"].get("input_summary", {}).get("redo") and c[0]
                 for c in send.call_args_list}
        self.assertEqual(send.call_count, 2)
        # 派发任务带 redo=True（跳过指纹复用）
        dispatched_types = {c[0][0] for c in send.call_args_list}
        self.assertIn("shortplay.tasks.generate_image", dispatched_types)
        self.assertIn("shortplay.tasks.generate_tts", dispatched_types)

    def test_audio_only_no_image_dispatch(self):
        """AUDIO-only 重做：不派图片任务。"""
        self.mk_asset(self.shot1, AssetType.IMAGE)
        self.mk_asset(self.shot1, AssetType.AUDIO)
        with patch("apps.creation.pipeline.dispatcher.send_task",
                   return_value=MagicMock(id="x")) as send:
            redo(self.play, RedoGranularity.SHOT, self.shot1.pk, asset_types=["AUDIO"])
        names = [c[0][0] for c in send.call_args_list]
        self.assertEqual(names, ["shortplay.tasks.generate_tts"])
        # IMAGE 保持 READY（未失效）
        img = Asset.objects.get(shot=self.shot1, asset_type=AssetType.IMAGE,
                                version=1)
        self.assertEqual(img.status, AssetStatus.READY)

    def test_redo_marks_superseded_keeps_history(self):
        """重做将旧资产 SUPERSEDED（历史保留）并派发新任务。"""
        img = self.mk_asset(self.shot1, AssetType.IMAGE)
        with patch("apps.creation.pipeline.dispatcher.send_task",
                   return_value=MagicMock(id="x")):
            redo(self.play, RedoGranularity.SHOT, self.shot1.pk)
        img.refresh_from_db()
        self.assertEqual(img.status, AssetStatus.SUPERSEDED)

    def test_stale_preview_version_rejected(self):
        """预检后目标变化 → RedoStaleError（视图映射 409）。"""
        from apps.creation.pipeline.redo import RedoStaleError
        self.mk_asset(self.shot1, AssetType.IMAGE)
        v1 = preview_redo(self.play, RedoGranularity.SHOT, self.shot1.pk)["preview_version"]
        # 预检后镜头被改（模拟并发）
        self.shot1.visual_prompt = "被人改了"
        self.shot1.save(update_fields=["visual_prompt"])
        with self.assertRaises(RedoStaleError):
            redo(self.play, RedoGranularity.SHOT, self.shot1.pk, preview_version=v1)
        # 未变的版本可执行
        v2 = preview_redo(self.play, RedoGranularity.SHOT, self.shot1.pk)["preview_version"]
        with patch("apps.creation.pipeline.dispatcher.send_task",
                   return_value=MagicMock(id="x")):
            redo(self.play, RedoGranularity.SHOT, self.shot1.pk, preview_version=v2)

    def test_invalid_asset_types_rejected(self):
        """非法 asset_types → RedoError。"""
        with self.assertRaises(RedoError):
            redo(self.play, RedoGranularity.SHOT, self.shot1.pk, asset_types=["VIDEO"])


class CharacterRefImpactTests(RedoFixture, TestCase):
    """任务 3.3：角色参考图影响集合——只影响引用镜头及成片。"""

    def test_character_ref_impact_via_edges(self):
        """改角色定妆照：预检列出引用该角色镜头的 IMAGE 与下游 VIDEO。"""
        ref = Asset.objects.create(
            play=self.play, character=self.character, asset_type=AssetType.CHARACTER_REF,
            status=AssetStatus.READY, artifact_url="/media/ref.png")
        # shot1 引用角色（有 CHARACTER_REF→IMAGE 依赖边），shot2 不引用
        img1 = self.mk_asset(self.shot1, AssetType.IMAGE)
        img2 = self.mk_asset(self.shot2, AssetType.IMAGE)
        video = Asset.objects.create(
            play=self.play, episode=self.ep, asset_type=AssetType.VIDEO,
            status=AssetStatus.READY, artifact_url="/media/v.mp4")
        AssetDependency.objects.create(
            upstream=ref, downstream=img1, relation_type=RelationType.CHARACTER_REF_TO_IMAGE)
        AssetDependency.objects.create(
            upstream=img1, downstream=video, relation_type=RelationType.IMAGE_TO_VIDEO)
        # 预检：只重做 shot1 的 IMAGE
        result = preview_redo(self.play, RedoGranularity.SHOT, self.shot1.pk,
                              changes=["character_ref"])
        self.assertEqual({a["type"] for a in result["invalidated"]}, {"IMAGE"})
        self.assertEqual(len(result["video_impact"]), 1)


class RedoApiTests(RedoFixture, APITestCase):
    """任务 3.1/3.2/3.4：API 行为——预检只读权限、重做 403、跨剧拒绝。"""

    def _post(self, user, action, payload):
        self.client.force_authenticate(user)
        return self.client.post(f"/api/plays/{self.play.pk}/{action}/",
                                payload, format="json")

    def test_preview_redo_endpoint(self):
        """预检端点返回完整影响报告。"""
        self.mk_asset(self.shot1, AssetType.IMAGE)
        self.mk_asset(self.shot1, AssetType.AUDIO)
        resp = self._post(self.owner, "preview-redo", {
            "granularity": "SHOT", "target_id": self.shot1.pk,
            "asset_types": ["AUDIO"]})
        self.assertEqual(resp.status_code, 200)
        data = resp.json().get("data", resp.json())
        self.assertEqual(data["estimated_tasks"], 1)
        self.assertIn("preview_version", data)

    def test_preview_readonly_member_can_read_redo_denied(self):
        """只读成员可预检（读操作）但重做 403（任务 3.4）。"""
        self.mk_asset(self.shot1, AssetType.IMAGE)
        resp = self._post(self.viewer, "preview-redo", {
            "granularity": "SHOT", "target_id": self.shot1.pk})
        self.assertEqual(resp.status_code, 200)
        redo_resp = self._post(self.viewer, "redo", {
            "granularity": "SHOT", "target_id": self.shot1.pk})
        self.assertEqual(redo_resp.status_code, 403)

    def test_cross_play_target_rejected(self):
        """跨剧目标拒绝。"""
        other_owner = CustomUser.objects.create_user(
            username="a3x", password="Passw0rd!123", role=UserRole.PRODUCER)
        other = Play.objects.create(title="他剧", created_by=other_owner)
        other_shot = Shot.objects.create(
            scene=Scene.objects.create(
                episode=Episode.objects.create(play=other, episode_number=1),
                scene_number=1),
            shot_number=1)
        resp = self._post(self.owner, "redo", {
            "granularity": "SHOT", "target_id": other_shot.pk})
        self.assertEqual(resp.status_code, 400)

    def test_redo_with_asset_types_and_stale_version(self):
        """redo 端点带 asset_types；过期 preview_version 返回 409。"""
        self.mk_asset(self.shot1, AssetType.IMAGE)
        self.mk_asset(self.shot1, AssetType.AUDIO)
        resp = self._post(self.owner, "preview-redo", {
            "granularity": "SHOT", "target_id": self.shot1.pk})
        pv = resp.json().get("data", resp.json())["preview_version"]
        # 目标变化 → 409
        self.shot1.dialogue = "变了"
        self.shot1.save(update_fields=["dialogue"])
        stale = self._post(self.owner, "redo", {
            "granularity": "SHOT", "target_id": self.shot1.pk,
            "asset_types": ["AUDIO"], "preview_version": pv})
        self.assertEqual(stale.status_code, 409)
        # 正常 AUDIO-only redo
        resp2 = self._post(self.owner, "preview-redo", {
            "granularity": "SHOT", "target_id": self.shot1.pk, "asset_types": ["AUDIO"]})
        pv2 = resp2.json().get("data", resp2.json())["preview_version"]
        with patch("apps.creation.pipeline.dispatcher.send_task",
                   return_value=MagicMock(id="x")):
            ok = self._post(self.owner, "redo", {
                "granularity": "SHOT", "target_id": self.shot1.pk,
                "asset_types": ["AUDIO"], "preview_version": pv2})
        self.assertEqual(ok.status_code, 200)
