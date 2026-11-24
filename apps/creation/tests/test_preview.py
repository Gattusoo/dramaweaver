"""提案预览测试（Phase 1.8.1）：before/after 快照、字段级 diff、成本估算、preview 端点。"""
from decimal import Decimal
from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework.test import APITestCase

from apps.creation.assistant import events
from apps.creation.assistant import preview as PV
from apps.creation.assistant.models import AssistantProposal, AssistantSession, ProposalKind, ProposalStatus
from apps.creation.assistant import proposals as P
from apps.creation.models import (
    AgentConfig, Asset, AssetStatus, AssetType, Character, Episode, MemberRole,
    MemberStatus, Play, PlayMember, PlayStatus, Scene, Shot,
)
from apps.core.system.models import CustomUser


def _make_proposal(play, session, kind, **kw):
    return AssistantProposal.objects.create(
        session=session, play=play, kind=kind, payload=kw.pop("payload", {}),
        summary="s", status=ProposalStatus.PENDING, **kw)


class PreviewFixture(TestCase):
    def setUp(self):
        events.set_backend(events.MemoryStreams())
        self.addCleanup(events.set_backend, None)
        self.user = CustomUser.objects.create_user(username="pv", password="Passw0rd!123")
        self.config = AgentConfig.objects.create(name="cfg", created_by=self.user)
        self.play = Play.objects.create(title="剧", created_by=self.user,
                                        agent_config=self.config, status=PlayStatus.PAUSED_AT_SCRIPT)
        PlayMember.objects.create(play=self.play, user=self.user, role=MemberRole.OWNER,
                                  status=MemberStatus.ACTIVE)
        self.ep = Episode.objects.create(play=self.play, episode_number=1)
        self.scene = Scene.objects.create(episode=self.ep, scene_number=1,
                                          location="办公室", mood="紧张", description="对峙")
        self.shot = Shot.objects.create(scene=self.scene, shot_number=1,
                                        dialogue="你逃不掉", visual_prompt="特写")
        self.char = Character.objects.create(play=self.play, name="林总", role="PROTAGONIST")
        self.session = AssistantSession.objects.create(play=self.play, user=self.user,
                                                       agent_config=self.config)

    def seed_asset(self, **fields):
        asset = Asset(play=self.play, status=AssetStatus.READY, **fields)
        asset._skip_pipeline_dispatch = True
        asset.save()
        return asset


class SnapshotTests(PreviewFixture):
    """snapshot_current：各提案类型取目标当前值。"""

    def test_outline_snapshot(self):
        asset = self.seed_asset(asset_type=AssetType.OUTLINE, metadata={"text": "大纲v1"})
        p = _make_proposal(self.play, self.session, ProposalKind.OUTLINE)
        snap = PV.snapshot_current(p)
        self.assertEqual((snap["version"], snap["text"]), (asset.version, "大纲v1"))

    def test_outline_snapshot_no_asset(self):
        p = _make_proposal(self.play, self.session, ProposalKind.OUTLINE)
        snap = PV.snapshot_current(p)
        self.assertEqual((snap["version"], snap["text"]), (None, ""))

    def test_episode_script_snapshot_includes_scenes(self):
        self.seed_asset(asset_type=AssetType.EPISODE_SCRIPT, episode=self.ep,
                        metadata={"text": "剧本"})
        p = _make_proposal(self.play, self.session, ProposalKind.EPISODE_SCRIPT,
                           target_episode=self.ep,
                           payload={"scenes": {"1": {"mood": "绝望"}}})
        snap = PV.snapshot_current(p)
        self.assertEqual(snap["text"], "剧本")
        # 只快照 payload 中被改的字段
        self.assertEqual(snap["scenes"], {"1": {"mood": "紧张"}})

    def test_scene_and_shot_snapshot(self):
        p = _make_proposal(self.play, self.session, ProposalKind.SCENE, target_scene=self.scene)
        snap = PV.snapshot_current(p)
        self.assertEqual((snap["location"], snap["mood"]), ("办公室", "紧张"))
        p2 = _make_proposal(self.play, self.session, ProposalKind.SHOT, target_shot=self.shot)
        snap2 = PV.snapshot_current(p2)
        self.assertEqual(snap2["dialogue"], "你逃不掉")
        self.assertIn("visual_prompt", snap2)

    def test_shot_delete_snapshot(self):
        p = _make_proposal(self.play, self.session, ProposalKind.SHOT_DELETE,
                           target_shot=self.shot)
        self.assertEqual(PV.snapshot_current(p)["dialogue"], "你逃不掉")

    def test_create_kinds_snapshot(self):
        p = _make_proposal(self.play, self.session, ProposalKind.SHOT_CREATE,
                           target_scene=self.scene)
        snap = PV.snapshot_current(p)
        self.assertEqual(snap["existing_shot_numbers"], [1])
        p2 = _make_proposal(self.play, self.session, ProposalKind.SCENE_CREATE,
                            target_episode=self.ep)
        self.assertEqual(PV.snapshot_current(p2)["existing_scene_numbers"], [1])
        p3 = _make_proposal(self.play, self.session, ProposalKind.CHARACTER_CREATE)
        self.assertEqual(PV.snapshot_current(p3)["existing_names"], ["林总"])

    def test_character_edit_snapshot(self):
        p = _make_proposal(self.play, self.session, ProposalKind.CHARACTER_EDIT,
                           target_character=self.char)
        snap = PV.snapshot_current(p)
        self.assertEqual((snap["name"], snap["role"]), ("林总", "PROTAGONIST"))

    def test_generation_snapshot(self):
        p = _make_proposal(self.play, self.session, ProposalKind.GENERATION,
                           payload={"action": "redo_shot", "target_id": self.shot.pk})
        snap = PV.snapshot_current(p)
        self.assertEqual((snap["action"], snap["target_id"]), ("redo_shot", self.shot.pk))


class AfterAndDiffTests(TestCase):
    """build_after + compute_diff：纯函数，不需 DB。"""

    def test_text_replacement(self):
        p = MagicMock(kind=ProposalKind.OUTLINE, payload={"text": "新大纲"})
        after = PV.build_after({"version": 1, "text": "旧大纲"}, p)
        self.assertEqual(after["text"], "新大纲")
        self.assertEqual(after["version"], 1)

    def test_episode_script_scene_merge(self):
        p = MagicMock(kind=ProposalKind.EPISODE_SCRIPT,
                      payload={"scenes": {"1": {"mood": "绝望"}, "2": {"location": "天台"}}})
        before = {"text": "t", "scenes": {"1": {"mood": "紧张", "location": "办公室"}}}
        after = PV.build_after(before, p)
        self.assertEqual(after["scenes"]["1"], {"mood": "绝望", "location": "办公室"})
        self.assertEqual(after["scenes"]["2"], {"location": "天台"})

    def test_field_edit_after(self):
        p = MagicMock(kind=ProposalKind.SHOT, payload={"dialogue": "新台词"})
        after = PV.build_after({"dialogue": "旧台词", "mood": "x"}, p)
        self.assertEqual(after["dialogue"], "新台词")

    def test_delete_and_create_after(self):
        p = MagicMock(kind=ProposalKind.SHOT_DELETE, target_shot_id=7, payload={})
        self.assertEqual(PV.build_after({}, p), {"_deleted": True, "shot_id": 7})
        p2 = MagicMock(kind=ProposalKind.CHARACTER_CREATE, payload={"name": "新角色"})
        self.assertEqual(PV.build_after({}, p2)["_new"], True)

    def test_diff_modified_added_removed(self):
        before = {"a": 1, "b": 2, "c": 3}
        after = {"a": 1, "b": 9, "d": 4}
        diffs = {d["field"]: d for d in PV.compute_diff(before, after)}
        self.assertNotIn("a", diffs)  # unchanged 不列出
        self.assertEqual(diffs["b"]["change"], "modified")
        self.assertEqual((diffs["b"]["old"], diffs["b"]["new"]), (2, 9))
        self.assertEqual(diffs["c"]["change"], "removed")
        self.assertEqual(diffs["d"]["change"], "added")

    def test_diff_nested_scenes(self):
        before = {"scenes": {"1": {"mood": "紧张"}}}
        after = {"scenes": {"1": {"mood": "绝望"}}}
        diffs = PV.compute_diff(before, after)
        self.assertEqual(len(diffs), 1)
        self.assertEqual(diffs[0]["field"], "scenes.1.mood")
        self.assertEqual(diffs[0]["change"], "modified")


class CostEstimateTests(PreviewFixture):
    """cost_estimate：按 impacted 资产类型×单价汇总。"""

    def test_no_impacted_assets(self):
        p = _make_proposal(self.play, self.session, ProposalKind.SHOT,
                           target_shot=self.shot, impacted_asset_ids=[])
        est = PV.cost_estimate(p)
        self.assertEqual((est["estimated_cost"], est["requires_confirm"]), ("0", False))
        self.assertEqual(est["breakdown"], [])

    def test_groups_by_asset_type(self):
        a1 = self.seed_asset(asset_type=AssetType.IMAGE, shot=self.shot)
        a2 = self.seed_asset(asset_type=AssetType.IMAGE, shot=self.shot)
        a3 = self.seed_asset(asset_type=AssetType.AUDIO, shot=self.shot)
        p = _make_proposal(self.play, self.session, ProposalKind.SHOT,
                           target_shot=self.shot,
                           impacted_asset_ids=[a1.pk, a2.pk, a3.pk])
        est = PV.cost_estimate(p)
        self.assertEqual(est["assets"], {"IMAGE": 2, "AUDIO": 1})
        # IMAGE 0.05×2 + AUDIO 0.02×1 = 0.12
        self.assertEqual(est["estimated_cost"], "0.1200")
        self.assertFalse(est["requires_confirm"])
        types = {b["asset_type"] for b in est["breakdown"]}
        self.assertEqual(types, {"IMAGE", "AUDIO"})

    def test_requires_confirm_over_threshold(self):
        assets = [self.seed_asset(asset_type=AssetType.VIDEO, episode=self.ep) for _ in range(3)]
        p = _make_proposal(self.play, self.session, ProposalKind.EPISODE_SCRIPT,
                           target_episode=self.ep,
                           impacted_asset_ids=[a.pk for a in assets])
        est = PV.cost_estimate(p)
        # VIDEO 0.50×3 = 1.50 >= 1.00
        self.assertTrue(est["requires_confirm"])
        self.assertEqual(est["estimated_cost"], "1.5000")

    def test_model_cost_config_overrides_default(self):
        from apps.ai_gateway.models import AIModel, AIProvider
        prov = AIProvider.objects.create(name="img", provider_type="IMAGE",
                                         adapter_type="x", base_url="https://x", priority=1)
        model = AIModel.objects.create(provider=prov, model_name="img-v1",
                                       display_name="img", model_type="IMAGE",
                                       cost_config={"cost_per_image": 0.20})
        self.config.image_model = model
        self.config.save(update_fields=["image_model"])
        self.play.refresh_from_db()
        a = self.seed_asset(asset_type=AssetType.IMAGE, shot=self.shot)
        p = _make_proposal(self.play, self.session, ProposalKind.SHOT,
                           target_shot=self.shot, impacted_asset_ids=[a.pk])
        est = PV.cost_estimate(p)
        self.assertEqual(est["breakdown"][0]["unit_cost"], "0.2")
        self.assertEqual(est["estimated_cost"], "0.2000")

    def test_video_cost_uses_metadata_duration(self):
        """B.2：VIDEO 按实测时长秒数计价，不按个数（成本不再低估压掉确认阈值）。"""
        assets = [self.seed_asset(asset_type=AssetType.VIDEO, episode=self.ep,
                                  metadata={"duration_seconds": 90.0}) for _ in range(3)]
        p = _make_proposal(self.play, self.session, ProposalKind.EPISODE_SCRIPT,
                           target_episode=self.ep,
                           impacted_asset_ids=[a.pk for a in assets])
        est = PV.cost_estimate(p)
        # 3 × 90s × 0.50/s = 135.00（旧按个数只算 1.50）
        self.assertEqual(est["estimated_cost"], "135.0000")
        self.assertTrue(est["requires_confirm"])
        self.assertEqual(est["breakdown"][0]["seconds"], "270.0")
        # 无 metadata 的资产兜底 1s/个（与旧计价一致的保守值）
        no_meta = self.seed_asset(asset_type=AssetType.VIDEO, episode=self.ep)
        p2 = _make_proposal(self.play, self.session, ProposalKind.EPISODE_SCRIPT,
                            target_episode=self.ep, impacted_asset_ids=[no_meta.pk])
        est2 = PV.cost_estimate(p2)
        self.assertEqual(est2["breakdown"][0]["seconds"], "1")
        self.assertEqual(est2["estimated_cost"], "0.5000")


class ProposalPreviewTests(PreviewFixture):
    """proposal_preview 汇总 + preview API 端点。"""

    def test_full_preview_shot(self):
        p = _make_proposal(self.play, self.session, ProposalKind.SHOT,
                           target_shot=self.shot,
                           base_snapshot={"dialogue": "你逃不掉"},
                           payload={"dialogue": "你逃不掉的，林总"})
        data = PV.proposal_preview(p.pk)
        self.assertEqual(data["id"], p.pk)
        self.assertEqual(data["kind"], ProposalKind.SHOT)
        self.assertEqual(data["before"]["dialogue"], "你逃不掉")
        self.assertEqual(data["after"]["dialogue"], "你逃不掉的，林总")
        dlg = [d for d in data["diff"] if d["field"] == "dialogue"]
        self.assertEqual(len(dlg), 1)
        self.assertEqual(dlg[0]["change"], "modified")
        self.assertIsNone(data["stale_reason"])

    def test_preview_nonexistent_returns_none(self):
        self.assertIsNone(PV.proposal_preview(999999))

    def test_stale_reason_in_preview(self):
        p = _make_proposal(self.play, self.session, ProposalKind.SHOT,
                           target_shot=self.shot,
                           base_snapshot={"dialogue": "旧值"})
        self.shot.dialogue = "已被改"
        self.shot.save(update_fields=["dialogue"])
        data = PV.proposal_preview(p.pk)
        self.assertIsNotNone(data["stale_reason"])


class PreviewApiTests(PreviewFixture, APITestCase):
    """GET /api/assistant/proposals/{id}/preview/"""

    def setUp(self):
        super().setUp()
        self.client.force_authenticate(self.user)

    def _data(self, resp):
        return resp.json().get("data", resp.json())

    def test_preview_endpoint(self):
        p = _make_proposal(self.play, self.session, ProposalKind.SCENE,
                           target_scene=self.scene, payload={"mood": "愤怒"})
        resp = self.client.get(f"/api/assistant/proposals/{p.pk}/preview/")
        self.assertEqual(resp.status_code, 200)
        data = self._data(resp)
        self.assertEqual(data["before"]["mood"], "紧张")
        self.assertEqual(data["after"]["mood"], "愤怒")
        self.assertIn("cost_estimate", data)
        self.assertIn("diff", data)

    def test_preview_endpoint_foreign_play_404(self):
        other_owner = CustomUser.objects.create_user(username="pvx", password="Passw0rd!123")
        other = Play.objects.create(title="别人", created_by=other_owner)
        ep = Episode.objects.create(play=other, episode_number=1)
        sc = Scene.objects.create(episode=ep, scene_number=1, mood="m")
        sess = AssistantSession.objects.create(play=other, user=other_owner)
        p = _make_proposal(other, sess, ProposalKind.SCENE, target_scene=sc, payload={})
        resp = self.client.get(f"/api/assistant/proposals/{p.pk}/preview/")
        self.assertEqual(resp.status_code, 404)


# ── phase1 max 审查修复 3.1：空 impacted 时推算待生成资产 ────────────────

class Phase1CostEstimatePendingTests(PreviewFixture):
    """全新短剧（无 READY 资产）的 GENERATION 类提案不得估 0 元。"""

    def _gen_proposal(self, kind=ProposalKind.GENERATION, **kw):
        return _make_proposal(self.play, self.session, kind,
                              impacted_asset_ids=[], **kw)

    def test_redo_scene_new_play_estimated_nonzero(self):
        """全新短剧 redo_scene 提案：按场景镜头推算，非零费用 + basis=estimated。"""
        # 场景已有 3 个镜头（fixture 1 个 + 补 2 个），2 条对白
        Shot.objects.create(scene=self.scene, shot_number=2, dialogue="另一个选择")
        Shot.objects.create(scene=self.scene, shot_number=3, dialogue="")
        p = self._gen_proposal(target_scene=self.scene,
                               payload={"action": "redo_scene", "target_id": self.scene.pk})
        est = PV.cost_estimate(p)
        self.assertEqual(est.get("basis"), "estimated")
        # IMAGE 3×0.05 = 0.15 + TTS 0.02×(2×2字/4s=1s + 保底) > 0
        self.assertNotEqual(est["estimated_cost"], "0")
        self.assertGreater(Decimal(est["estimated_cost"]), Decimal("0.15"))
        self.assertFalse(est["requires_confirm"])

    def test_expensive_estimate_requires_confirm(self):
        """推算费用超阈值时 requires_confirm 为真（不绕过确认门）。"""
        # 30 个带对白镜头：IMAGE 30×0.05=1.5 + TTS 已超阈值
        for i in range(30):
            Shot.objects.create(scene=self.scene, shot_number=i + 2, dialogue="说点什么台词")
        p = self._gen_proposal(target_scene=self.scene,
                               payload={"action": "redo_scene", "target_id": self.scene.pk})
        est = PV.cost_estimate(p)
        self.assertGreater(Decimal(est["estimated_cost"]), Decimal("1.00"))
        self.assertTrue(est["requires_confirm"])

    def test_text_field_edit_zero_cost_flag(self):
        """纯文本字段修改类：零费用 + no_generation_cost 标记。"""
        p = self._gen_proposal(kind=ProposalKind.SHOT, target_shot=self.shot)
        est = PV.cost_estimate(p)
        self.assertEqual(est["estimated_cost"], "0")
        self.assertTrue(est.get("no_generation_cost"))
        self.assertFalse(est["requires_confirm"])
        self.assertNotIn("basis", est)

    def test_preview_episode_action_full_scale(self):
        """preview_episode 动作：按整集规模（60s）计价，非零。"""
        p = self._gen_proposal(target_episode=self.ep,
                               payload={"action": "preview_episode", "target_id": self.ep.pk})
        est = PV.cost_estimate(p)
        self.assertEqual(est.get("basis"), "estimated")
        self.assertEqual(est["assets"], {"PREVIEW": 1})
        self.assertNotEqual(est["estimated_cost"], "0")

    def test_with_ready_assets_unchanged(self):
        """有 READY 资产时口径不变（impacted 非空走原路径，无 basis 字段）。"""
        a = self.seed_asset(asset_type=AssetType.IMAGE, shot=self.shot)
        p = _make_proposal(self.play, self.session, ProposalKind.SHOT,
                           target_shot=self.shot, impacted_asset_ids=[a.pk])
        est = PV.cost_estimate(p)
        self.assertEqual(est["estimated_cost"], "0.0500")
        self.assertNotIn("basis", est)
