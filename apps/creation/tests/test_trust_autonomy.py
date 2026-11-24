"""信任分级与渐进自主（主计划 3.2.5 / 3.2.6 / 3.2.12）。

覆盖：信任分累积（加分/扣分/幂等/夹紧/分档）、自动批准矩阵（高信任高置信度/
无置信度/删除类排除/开关关闭/失败降级）、多方案竞争择优 confidence、
trust / realtime config 端点、配置接线。
"""
from unittest.mock import patch

from django.test import TestCase, override_settings

from apps.creation.tests.test_assistant import AssistantFixtureMixin

from ..assistant import trust as trust_mod
from ..assistant import variants as variants_mod
from ..assistant.models import (AssistantProposal, ProposalKind, ProposalStatus,
                                TrustLevel)
from ..assistant.proposals import (apply_proposal, create_proposal,
                                   discard_proposal)
from ..assistant.trust import (effective_threshold, level_label, record_outcome,
                               should_auto_approve, trust_level_of)


def _set_trust(user, score, threshold=None):
    return TrustLevel.objects.update_or_create(
        user=user, defaults={"trust_score": score, "auto_approve_threshold": threshold})


class TrustAccumulationTests(AssistantFixtureMixin, TestCase):
    def _pending_proposal(self):
        return create_proposal(
            self.session, None, ProposalKind.EPISODE_SCRIPT,
            payload={"text": "新剧本"}, summary="测试", episode=self.ep,
            target_asset=self.script, base_version=self.script.version)

    def test_apply_increments_once(self):
        _set_trust(self.producer, 0.5)
        p = self._pending_proposal()
        apply_proposal(p.pk, self.producer)
        row = TrustLevel.objects.get(user=self.producer)
        self.assertAlmostEqual(row.trust_score, 0.52)
        # 幂等：重复 apply 被状态门禁拒，分数不变
        with self.assertRaises(Exception):
            apply_proposal(p.pk, self.producer)
        row.refresh_from_db()
        self.assertAlmostEqual(row.trust_score, 0.52)

    def test_discard_decrements(self):
        _set_trust(self.producer, 0.5)
        p = self._pending_proposal()
        discard_proposal(p.pk, self.producer)
        row = TrustLevel.objects.get(user=self.producer)
        self.assertAlmostEqual(row.trust_score, 0.49)

    def test_stale_rejection_decrements(self):
        # 用户尝试应用已过期的提案 → 提案置 STALE 并抛 409 → 创建者扣分
        _set_trust(self.producer, 0.5)
        stale_p = create_proposal(
            self.session, None, ProposalKind.EPISODE_SCRIPT,
            payload={"text": "过期"}, summary="s", episode=self.ep, base_version=9999)
        with self.assertRaises(Exception):
            apply_proposal(stale_p.pk, self.producer)
        row = TrustLevel.objects.get(user=self.producer)
        self.assertAlmostEqual(row.trust_score, 0.49)

    def test_clamp_bounds(self):
        _set_trust(self.producer, 0.995)
        p1 = self._pending_proposal()
        apply_proposal(p1.pk, self.producer)
        row = TrustLevel.objects.get(user=self.producer)
        self.assertLessEqual(row.trust_score, 1.0)
        _set_trust(self.producer, 0.005)
        p2 = self._pending_proposal()
        discard_proposal(p2.pk, self.producer)
        row.refresh_from_db()
        self.assertGreaterEqual(row.trust_score, 0.0)

    def test_level_labels(self):
        self.assertEqual(level_label(0.1), "cautious")
        self.assertEqual(level_label(0.5), "standard")
        self.assertEqual(level_label(0.9), "trusted")

    def test_threshold_row_overrides_default(self):
        from django.conf import settings
        row = trust_level_of(self.producer)
        self.assertAlmostEqual(effective_threshold(row),
                               settings.TRUST_DEFAULT_THRESHOLD)
        row.auto_approve_threshold = 0.6
        self.assertAlmostEqual(effective_threshold(row), 0.6)

    def test_row_level_threshold_drives_auto_approve(self):
        # 行级阈值 0.6：0.65 信任分在默认阈值(0.8)下不批、行级阈值下可批
        _set_trust(self.producer, 0.65, threshold=0.6)
        p = self._pending_proposal()
        p.payload = {"text": "x", "confidence": 0.9}
        with override_settings(ENABLE_PROGRESSIVE_AUTONOMY=True):
            self.assertTrue(should_auto_approve(self.producer, p))


class AutoApproveMatrixTests(AssistantFixtureMixin, TestCase):
    def _create(self, payload, kind=ProposalKind.EPISODE_SCRIPT, trust=0.9):
        if trust is not None:
            _set_trust(self.producer, trust)
        return create_proposal(
            self.session, None, kind, payload=payload, summary="s",
            episode=self.ep, target_asset=self.script,
            base_version=self.script.version)

    @override_settings(ENABLE_PROGRESSIVE_AUTONOMY=True)
    def test_high_trust_high_confidence_auto_applied(self):
        p = self._create({"text": "新版剧本", "confidence": 0.95})
        p.refresh_from_db()
        self.assertEqual(p.status, ProposalStatus.APPLIED)
        self.assertTrue(p.payload.get("auto_applied"))

    @override_settings(ENABLE_PROGRESSIVE_AUTONOMY=True)
    def test_no_confidence_stays_pending(self):
        p = self._create({"text": "新版剧本"})
        p.refresh_from_db()
        self.assertEqual(p.status, ProposalStatus.PENDING)
        self.assertNotIn("auto_applied", p.payload)

    @override_settings(ENABLE_PROGRESSIVE_AUTONOMY=True)
    def test_shot_delete_excluded(self):
        # SHOT_DELETE 排除：即使置信度达标也保持 PENDING
        p = self._create({"confidence": 0.95}, kind=ProposalKind.SHOT_DELETE)
        p.refresh_from_db()
        self.assertEqual(p.status, ProposalStatus.PENDING)

    def test_flag_off_zero_change(self):
        p = self._create({"text": "新版剧本", "confidence": 0.95})
        p.refresh_from_db()
        self.assertEqual(p.status, ProposalStatus.PENDING)

    @override_settings(ENABLE_PROGRESSIVE_AUTONOMY=True)
    def test_low_trust_stays_pending(self):
        p = self._create({"text": "新版剧本", "confidence": 0.95}, trust=0.3)
        p.refresh_from_db()
        self.assertEqual(p.status, ProposalStatus.PENDING)

    @override_settings(ENABLE_PROGRESSIVE_AUTONOMY=True)
    def test_manual_apply_not_marked_auto(self):
        p = self._create({"text": "新版剧本"})  # 无 confidence → PENDING
        apply_proposal(p.pk, self.producer)
        p.refresh_from_db()
        self.assertEqual(p.status, ProposalStatus.APPLIED)
        self.assertNotIn("auto_applied", p.payload)

    @override_settings(ENABLE_PROGRESSIVE_AUTONOMY=True)
    def test_gate_rejection_degrades_to_pending(self):
        # 短剧状态不在可应用状态 → 门禁拒 → 降级 PENDING（不丢提案）
        from ..models import PlayStatus
        self.play.status = PlayStatus.ASSET_GEN
        self.play.save(update_fields=["status"])
        p = self._create({"text": "新版剧本", "confidence": 0.95})
        p.refresh_from_db()
        self.assertEqual(p.status, ProposalStatus.PENDING)

    @override_settings(ENABLE_PROGRESSIVE_AUTONOMY=True)
    def test_stale_rejection_sets_stale(self):
        # base_version 与当前不一致 → 过期语义置 STALE（不降级 PENDING）
        p = self._create({"text": "新版剧本", "confidence": 0.95})
        p.refresh_from_db()
        # 直接造一条过期提案：base_version 指向不存在的版本
        stale = create_proposal(
            self.session, None, ProposalKind.EPISODE_SCRIPT,
            payload={"text": "过期", "confidence": 0.95}, summary="s",
            episode=self.ep, base_version=9999)
        stale.refresh_from_db()
        self.assertEqual(stale.status, ProposalStatus.STALE)


class CompetitiveScoringTests(AssistantFixtureMixin, TestCase):
    def _variants(self):
        return [
            {"style": {}, "label": "A", "text": "甲方案" * 10, "similarity": 0.0, "score": 70.0},
            {"style": {}, "label": "B", "text": "乙方案" * 10, "similarity": 0.1, "score": 92.0},
            {"style": {}, "label": "C", "text": "丙方案" * 10, "similarity": 0.1},
        ]

    def test_only_best_variant_carries_confidence(self):
        group, proposals = variants_mod.create_alternative_proposals(
            self.session, None, self.ep, self._variants())
        by_label = {p.alternative_label: p for p in proposals}
        self.assertAlmostEqual(by_label["B"].payload["confidence"], 0.92)
        self.assertNotIn("confidence", by_label["A"].payload)
        self.assertNotIn("confidence", by_label["C"].payload)
        # 默认开关关：全部 PENDING（即便 B 带 confidence）
        self.assertTrue(all(p.status == ProposalStatus.PENDING for p in proposals))

    @override_settings(ENABLE_PROGRESSIVE_AUTONOMY=True)
    def test_best_variant_auto_applied_siblings_pending(self):
        _set_trust(self.producer, 0.9)
        group, proposals = variants_mod.create_alternative_proposals(
            self.session, None, self.ep, self._variants())
        by_label = {p.alternative_label: p for p in proposals}
        self.assertEqual(by_label["B"].status, ProposalStatus.APPLIED)
        # 兄弟方案不被自动应用打断（保持待选对比语义）
        self.assertEqual(by_label["A"].status, ProposalStatus.PENDING)

    def test_score_variants_disabled_noop(self):
        vs = self._variants()
        out = variants_mod.score_variants(self.session, vs)
        self.assertIs(out, vs)  # 开关关：原列表原样返回，零模型调用
        self.assertAlmostEqual(out[1]["score"], 92.0)

    @override_settings(ENABLE_AGENT_COMPETITION=True)
    def test_score_variants_writes_overall(self):
        fake = type("GW", (), {})()
        fake.chat = lambda self_, **kw: {
            "content_blocks": [{"type": "text", "text": '{"overall": 88, "scores": {}, "suggestions": []}'}],
            "usage": {}}
        vs = [{"style": {}, "label": "A", "text": "方案", "similarity": 0.0}]
        out = variants_mod.score_variants(self.session, vs, gateway=fake)
        self.assertAlmostEqual(out[0]["score"], 88.0)

    @override_settings(ENABLE_AGENT_COMPETITION=True)
    def test_score_variants_unparseable_keeps_none(self):
        fake = type("GW", (), {})()
        fake.chat = lambda self_, **kw: {
            "content_blocks": [{"type": "text", "text": "不是 JSON"}], "usage": {}}
        vs = [{"style": {}, "label": "A", "text": "方案", "similarity": 0.0}]
        out = variants_mod.score_variants(self.session, vs, gateway=fake)
        self.assertNotIn("score", out[0])


class TrustEndpointTests(AssistantFixtureMixin, TestCase):
    def test_defaults_without_row(self):
        self.client.force_login(self.producer)
        r = self.client.get("/api/assistant/trust/")
        self.assertEqual(r.status_code, 200)
        self.assertAlmostEqual(r.data["trust_score"], 0.5)
        self.assertAlmostEqual(r.data["auto_approve_threshold"], 0.8)
        self.assertEqual(r.data["level"], "standard")
        # 未落库
        self.assertFalse(TrustLevel.objects.filter(user=self.producer).exists())

    def test_returns_accumulated_values(self):
        _set_trust(self.producer, 0.85)
        self.client.force_login(self.producer)
        r = self.client.get("/api/assistant/trust/")
        self.assertAlmostEqual(r.data["trust_score"], 0.85)
        self.assertEqual(r.data["level"], "trusted")

    def test_requires_auth(self):
        r = self.client.get("/api/assistant/trust/")
        self.assertEqual(r.status_code, 401)


class RealtimeConfigEndpointTests(AssistantFixtureMixin, TestCase):
    def test_enabled_ws(self):
        self.client.force_login(self.producer)
        with override_settings(ENABLE_REALTIME_EVENTS=True):
            r = self.client.get("/api/assistant/realtime/config/")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["enabled"])
        self.assertEqual(r.data["mode"], "ws")

    def test_disabled_polling(self):
        self.client.force_login(self.producer)
        with override_settings(ENABLE_REALTIME_EVENTS=False):
            r = self.client.get("/api/assistant/realtime/config/")
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.data["enabled"])
        self.assertEqual(r.data["mode"], "polling")

    def test_requires_auth(self):
        r = self.client.get("/api/assistant/realtime/config/")
        self.assertEqual(r.status_code, 401)


class TrustFlagWiringTests(TestCase):
    def test_settings_defaults(self):
        from django.conf import settings
        self.assertIs(settings.ENABLE_PROGRESSIVE_AUTONOMY, False)
        self.assertAlmostEqual(settings.AUTO_APPROVE_MIN_CONFIDENCE, 0.8)
        self.assertAlmostEqual(settings.TRUST_DEFAULT_THRESHOLD, 0.8)
        self.assertAlmostEqual(settings.TRUST_STEP_APPLY, 0.02)
        self.assertAlmostEqual(settings.TRUST_STEP_DISCARD, 0.01)
        self.assertAlmostEqual(settings.TRUST_INITIAL_SCORE, 0.5)

    def test_env_template_has_keys(self):
        from pathlib import Path
        import application.settings as _  # noqa: F401 — 确保 Django 可载入
        tpl = (Path(__file__).resolve().parents[3] / "conf" / "env.example.py"
               ).read_text(encoding="utf-8")
        for key in ("ENABLE_PROGRESSIVE_AUTONOMY", "AUTO_APPROVE_MIN_CONFIDENCE",
                    "TRUST_DEFAULT_THRESHOLD", "TRUST_STEP_APPLY",
                    "TRUST_STEP_DISCARD", "TRUST_INITIAL_SCORE"):
            self.assertIn(key, tpl)
