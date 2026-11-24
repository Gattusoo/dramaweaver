"""play-assistant 前置修复的回归测试：创建归属、approve 幂等、声线解绑、版本递增。"""
from rest_framework.test import APITestCase

from apps.creation.models import (
    AgentConfig, Asset, AssetLibrary, AssetStatus, AssetType, AuditMode, Character, Play, PlayStatus, ReviewDecision, ReviewLog, TaskRecord, TaskType, VoiceProfile,
)
from apps.ai_gateway.models import (
    AIProvider,
)
from .test_review_fixes import ApiFixtureMixin


class CreateOwnershipTests(ApiFixtureMixin, APITestCase):
    def test_producer_created_play_is_owned_and_writable(self):
        self.client.force_authenticate(self.producer)
        resp = self.client.post("/api/plays/", {"title": "新剧", "total_episodes": 1}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        play = Play.objects.get(pk=resp.data["id"])
        self.assertEqual(play.created_by, self.producer)
        # 创建者能看到并能为其创建分集
        self.assertEqual(self.client.get(f"/api/plays/{play.pk}/").status_code, 200)
        ep = self.client.post("/api/episodes/", {"play": play.pk, "episode_number": 1}, format="json")
        self.assertEqual(ep.status_code, 201, ep.data)

    def test_created_by_in_body_is_ignored(self):
        self.client.force_authenticate(self.producer)
        resp = self.client.post(
            "/api/plays/", {"title": "x", "created_by": self.admin.pk}, format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(Play.objects.get(pk=resp.data["id"]).created_by, self.producer)

    def test_agent_config_create_without_created_by(self):
        self.client.force_authenticate(self.producer)
        resp = self.client.post("/api/agent-configs/", {"name": "配方"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(AgentConfig.objects.get(pk=resp.data["id"]).created_by, self.producer)

    def test_private_library_asset_visible_to_creator(self):
        self.client.force_authenticate(self.editor)
        resp = self.client.post(
            "/api/asset-library/",
            {"name": "BGM", "asset_type": "BGM", "file_url": "https://e.com/a.mp3", "is_public": False},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(AssetLibrary.objects.get(pk=resp.data["id"]).owner, self.editor)
        ids = [r["id"] for r in self.client.get("/api/asset-library/").data["results"]]
        self.assertIn(resp.data["id"], ids)


class ApproveIdempotencyTests(ApiFixtureMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.play.status = PlayStatus.PAUSED_AT_OUTLINE
        self.play.save()
        self.client.force_authenticate(self.producer)

    def _script_tasks(self):
        return TaskRecord.objects.filter(task_type=TaskType.EPISODE_SCRIPT).count()

    def test_second_approve_conflicts_and_dispatches_once(self):
        url = f"/api/plays/{self.play.pk}/approve/"
        first = self.client.post(url, {"review_point": "OUTLINE"}, format="json")
        self.assertEqual(first.status_code, 200, first.data)
        self.assertEqual(first.data["status"], PlayStatus.SCRIPTING)
        self.assertEqual(self._script_tasks(), 1)

        second = self.client.post(url, {"review_point": "OUTLINE"}, format="json")
        self.assertEqual(second.status_code, 409)
        self.assertEqual(self._script_tasks(), 1)
        self.assertEqual(
            ReviewLog.objects.filter(play=self.play, decision=ReviewDecision.APPROVED).count(), 1,
        )

    def test_wrong_review_point_conflicts(self):
        resp = self.client.post(
            f"/api/plays/{self.play.pk}/approve/", {"review_point": "ASSETS"}, format="json",
        )
        self.assertEqual(resp.status_code, 409)
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.PAUSED_AT_OUTLINE)


class VoiceUnbindTests(ApiFixtureMixin, APITestCase):
    def test_patch_null_unbinds_voice(self):
        provider = AIProvider.objects.create(
            name="tts", provider_type="TTS", base_url="https://tts.example.com", api_key_encrypted="k",
        )
        voice = VoiceProfile.objects.create(name="声线", gender="FEMALE", provider=provider, voice_id="v1")
        char = Character.objects.create(
            play=self.play, name="女主", role="PROTAGONIST", voice_profile=voice,
        )
        self.client.force_authenticate(self.producer)
        resp = self.client.patch(
            f"/api/characters/{char.pk}/", {"voice_profile_id": None}, format="json",
        )
        self.assertEqual(resp.status_code, 200, resp.data)
        char.refresh_from_db()
        self.assertIsNone(char.voice_profile)


class TextAssetVersionTests(ApiFixtureMixin, APITestCase):
    """版本递增已由 Asset.save 的 _bump_version 保证，这里锁定该行为。"""

    def test_new_script_asset_bumps_version_and_supersedes(self):
        self.play.audit_mode = AuditMode.NONE
        self.play.save()
        v1 = Asset.objects.create(
            play=self.play, episode=self.ep, asset_type=AssetType.EPISODE_SCRIPT,
            status=AssetStatus.READY, metadata={"text": "a"},
        )
        v2 = Asset.objects.create(
            play=self.play, episode=self.ep, asset_type=AssetType.EPISODE_SCRIPT,
            status=AssetStatus.READY, metadata={"text": "b"},
        )
        v1.refresh_from_db()
        self.assertEqual(v2.version, 2)
        self.assertEqual(v1.status, AssetStatus.SUPERSEDED)
