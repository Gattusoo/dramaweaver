"""code-review 15 项问题的回归测试。"""
from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework import status
from rest_framework.test import APITestCase

from apps.ai_gateway.circuit_breaker import CircuitBreaker, CLOSED, HALF_OPEN, OPEN
from apps.ai_gateway.gateway import AIGateway
from apps.ai_gateway.rate_limiter import RateLimiter
from apps.ai_gateway.redis_client import MemoryStore, reset_store
from apps.ai_gateway.router import build_candidate_chain
from apps.creation.models import (
    AgentConfig, Asset, AssetStatus, AuditMode, AssetType, Episode, EpisodeStatus, Play, PlayStatus, RelationType, Scene, Shot, TaskRecord, TaskType,
)
from apps.ai_gateway.models import (
    AIModel, AIProvider,
)
from apps.core.system.models import (
    CustomUser, UserRole,
)
from apps.distribution.models import (
    DistributionChannel, Publication, SubtitleTrack,
)


def _user(name, role=None, **kw):
    """兼容旧签名：role 参数忽略（全局四级角色已退役），按 kw 建用户。"""
    return CustomUser.objects.create_user(username=name, password="Passw0rd!123", **kw)


_SEND = patch("apps.creation.pipeline.dispatcher.send_task", return_value=MagicMock(id="x"))


class ApiFixtureMixin:
    def setUp(self):
        from apps.creation.models import MemberRole, MemberStatus, PlayMember

        _SEND.start()
        self.addCleanup(_SEND.stop)
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
        self.scene = Scene.objects.create(episode=self.ep, scene_number=1)
        self.shot = Shot.objects.create(scene=self.scene, shot_number=1, dialogue="台词")
        self.track = SubtitleTrack.objects.create(
            episode=self.ep, language="zh-CN",
            subtitle_data=[{"start": 0, "end": 1.5, "text": "甲"}, {"start": 2.3, "end": 3, "text": "乙"}],
        )
        self.channel = DistributionChannel.objects.create(name="抖音", platform="douyin")

    def add_member(self, user, role):
        from apps.creation.models import MemberStatus, PlayMember
        return PlayMember.objects.create(
            play=self.play, user=user, role=role, status=MemberStatus.ACTIVE
        )


class PlayFilterFieldTests(ApiFixtureMixin, APITestCase):
    """#1 子实体 play_filter_field 路径正确，不再 500。"""

    def test_child_lists_ok_for_member_and_isolated_for_others(self):
        """成员可见子实体；非成员（含平台管理员）经业务接口均不可见。"""
        from apps.creation.models import MemberRole
        Publication.objects.create(episode=self.ep, channel=self.channel, title="t")
        urls = ["/api/scenes/", "/api/shots/", "/api/subtitle-tracks/", "/api/publications/"]
        self.add_member(self.editor, MemberRole.EDITOR)
        self.client.force_authenticate(self.editor)
        for url in urls:
            resp = self.client.get(url)
            self.assertEqual(resp.status_code, 200, url)
            self.assertEqual(resp.data["total"], 1, url)
        self.client.force_authenticate(self.viewer)  # 非成员
        for url in urls:
            self.assertEqual(self.client.get(url).data["total"], 0, url)
        self.client.force_authenticate(self.admin)  # 平台管理员走业务接口同样不可见
        for url in urls:
            self.assertEqual(self.client.get(url).data["total"], 0, url)

    def test_subtitle_export_is_plain_text(self):
        """#12 导出为纯文本，VTT cue 以空行分隔。"""
        self.client.force_authenticate(self.producer)
        srt = self.client.get(f"/api/subtitle-tracks/{self.track.pk}/export/srt/")
        self.assertEqual(srt.status_code, 200)
        body = srt.content.decode()
        self.assertTrue(body.startswith("1\n00:00:00,000 --> 00:00:01,500\n甲"))
        self.assertIn("00:00:02,300", body)  # #13 不再截断为 2,299
        vtt = self.client.get(f"/api/subtitle-tracks/{self.track.pk}/export/vtt/").content.decode()
        self.assertTrue(vtt.startswith("WEBVTT\n\n00:00:00.000 --> 00:00:01.500\n甲\n\n"))


class PlayPermissionFixTests(ApiFixtureMixin, APITestCase):
    """#7 被授权编辑可审核/查看；删除/授权仍限属主。#8 依赖边 fail-closed。"""

    def setUp(self):
        super().setUp()
        from apps.creation.models import MemberRole
        self.add_member(self.editor, MemberRole.EDITOR)
        self.add_member(self.viewer, MemberRole.VIEWER)

    def test_editor_can_retrieve_and_review(self):
        self.client.force_authenticate(self.editor)
        self.assertEqual(self.client.get(f"/api/plays/{self.play.pk}/").status_code, 200)
        resp = self.client.post(f"/api/plays/{self.play.pk}/approve/", {}, format="json")
        self.assertEqual(resp.status_code, 400)  # 过了权限，卡在 review_point 必填

    def test_editor_cannot_destroy_or_invite(self):
        self.client.force_authenticate(self.editor)
        self.assertEqual(self.client.delete(f"/api/plays/{self.play.pk}/").status_code, 403)
        # 旧 authorize 端点已退役（404）；成员邀请属 manage 层（403）
        self.assertEqual(
            self.client.post(f"/api/plays/{self.play.pk}/authorize/",
                             {"user_ids": [self.editor.pk]}, format="json").status_code,
            404,
        )
        resp = self.client.post(
            f"/api/plays/{self.play.pk}/invite/",
            {"user_ids": [self.viewer.pk], "role": "viewer"}, format="json",
        )
        self.assertEqual(resp.status_code, 403)

    def test_viewer_read_only(self):
        self.client.force_authenticate(self.viewer)
        self.assertEqual(self.client.get(f"/api/plays/{self.play.pk}/").status_code, 200)
        resp = self.client.patch(f"/api/plays/{self.play.pk}/", {"title": "改"}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_asset_dependency_requires_write_on_both_plays(self):
        other_play = Play.objects.create(title="他人的剧", created_by=_user("p2", UserRole.PRODUCER))
        mine = Asset.objects.create(play=self.play, asset_type=AssetType.IMAGE)
        theirs = Asset.objects.create(play=other_play, asset_type=AssetType.VIDEO)
        mine2 = Asset.objects.create(play=self.play, asset_type=AssetType.VIDEO)
        url = "/api/asset-dependencies/"
        data = {"upstream": mine.pk, "downstream": theirs.pk,
                "relation_type": RelationType.IMAGE_TO_VIDEO}
        self.client.force_authenticate(self.editor)
        self.assertEqual(self.client.post(url, data, format="json").status_code, 403)
        self.client.force_authenticate(self.viewer)
        data["downstream"] = mine2.pk
        self.assertEqual(self.client.post(url, data, format="json").status_code, 403)
        self.client.force_authenticate(self.editor)
        resp = self.client.post(url, data, format="json")
        self.assertEqual(resp.status_code, 201)
        # 对象级权限可解析到 Play，编辑者能读取自己创建的依赖边
        self.assertEqual(self.client.get(f"{url}{resp.data['id']}/").status_code, 200)


class PublishFixTests(ApiFixtureMixin, APITestCase):
    """#5 发布需走状态机；trigger_publish 不得在首集发布时把 Play 置为终态。"""

    def test_publish_rejects_play_without_episodes(self):
        from apps.creation.models import MemberRole, MemberStatus, PlayMember
        empty = Play.objects.create(title="空", created_by=self.producer)
        PlayMember.objects.create(play=empty, user=self.producer,
                                  role=MemberRole.OWNER, status=MemberStatus.ACTIVE)
        self.client.force_authenticate(self.producer)
        resp = self.client.post(f"/api/plays/{empty.pk}/publish/", {}, format="json")
        self.assertEqual(resp.status_code, 400)
        empty.refresh_from_db()
        self.assertEqual(empty.status, PlayStatus.DRAFT)

    def test_trigger_publish_waits_for_all_episodes(self):
        from apps.creation.tasks import trigger_publish
        ep2 = Episode.objects.create(play=self.play, episode_number=2)
        Episode.objects.filter(play=self.play).update(status=EpisodeStatus.COMPLETED)
        Play.objects.filter(pk=self.play.pk).update(status=PlayStatus.COMPLETED)
        for ep, expect in ((self.ep, PlayStatus.COMPLETED), (ep2, PlayStatus.PUBLISHED)):
            rec = TaskRecord.objects.create(play=self.play, episode=ep, task_type=TaskType.PUBLISH)
            trigger_publish(rec.pk)
            self.play.refresh_from_db()
            self.assertEqual(self.play.status, expect)


class GatewayFixTests(TestCase):
    """#2 限流窗口 / #3 #10 熔断 / #9 retry_count=0 / #15 is_active。"""

    def setUp(self):
        reset_store()
        self.store = MemoryStore()
        owner = _user("gw", UserRole.PRODUCER)
        self.pa = AIProvider.objects.create(
            name="A", provider_type="LLM", adapter_type="deepseek", base_url="https://a",
            api_key_encrypted="", priority=1, retry_count=0,
        )
        self.pb = AIProvider.objects.create(
            name="B", provider_type="LLM", adapter_type="openai", base_url="https://b",
            api_key_encrypted="", priority=2, retry_count=0,
        )
        self.ma = AIModel.objects.create(provider=self.pa, model_name="a", display_name="A", model_type="LLM")
        self.mb = AIModel.objects.create(provider=self.pb, model_name="b", display_name="B", model_type="LLM")
        self.config = AgentConfig.objects.create(
            name="c", llm_model=self.ma, llm_failover=[self.mb.pk], created_by=owner,
        )

    def test_rpm_window_not_extended_and_conc_reject_keeps_rpm(self):
        import time
        rl = RateLimiter(store=self.store)
        self.assertTrue(rl.try_acquire(1, max_rpm=10, max_concurrent=1))
        key = "rl:1:rpm"
        value, _ = self.store._data[key]
        self.store._data[key] = (value, time.time() + 1)  # 窗口即将结束
        self.assertFalse(rl.try_acquire(1, max_rpm=10, max_concurrent=1))  # 并发满
        self.assertLessEqual(self.store.ttl(key), 1)  # 未被顺延
        self.assertEqual(rl.current_rpm(1), 1)  # 并发拒绝不消耗 RPM

    def test_chain_building_does_not_consume_probe(self):
        cb = CircuitBreaker(store=self.store)
        for _ in range(5):
            cb.record_failure(self.pb.pk)
        self.store.set(f"cb:{self.pb.pk}:cooldown_until", "0")
        for _ in range(3):
            chain = build_candidate_chain(self.config, "LLM", breaker=cb)
            self.assertEqual([p.pk for _, p in chain], [self.pa.pk, self.pb.pk])
        self.assertEqual(cb.state(self.pb.pk), OPEN)
        # 只有一个调用方能抢到探测名额
        self.assertTrue(cb.allow_request(self.pb.pk))
        self.assertEqual(cb.state(self.pb.pk), HALF_OPEN)
        self.assertFalse(cb.allow_request(self.pb.pk))

    def test_cooldown_escalates(self):
        cb = CircuitBreaker(store=self.store, cooldown_seconds=30, max_cooldown_seconds=100)
        for _ in range(5):
            cb.record_failure(1)
        lengths = []
        for _ in range(3):
            self.store.set("cb:1:cooldown_until", "0")
            self.assertTrue(cb.allow_request(1))
            cb.record_failure(1)
            lengths.append(cb.current_cooldown(1))
        self.assertEqual(lengths, [60, 100, 100])
        self.store.set("cb:1:cooldown_until", "0")
        cb.allow_request(1)
        cb.record_success(1)
        self.assertEqual(cb.state(1), CLOSED)
        self.assertEqual(cb.current_cooldown(1), 30)

    def test_retry_count_zero_means_single_call(self):
        calls = []

        class Failing:
            def generate(self, *a, **k):
                calls.append(1)
                raise RuntimeError("boom")

        gw = AIGateway(store=self.store, adapter_factory=lambda p: Failing())
        with patch("apps.ai_gateway.gateway.time.sleep"):
            with self.assertRaises(Exception):
                gw.call("LLM", self.config, prompt="x")
        self.assertEqual(len(calls), 2)  # A、B 各 1 次，无重试

    def test_inactive_model_or_provider_skipped(self):
        AIModel.objects.filter(pk=self.ma.pk).update(is_active=False)
        AIProvider.objects.filter(pk=self.pb.pk).update(is_active=False)
        self.config.refresh_from_db()
        self.assertEqual(build_candidate_chain(self.config, "LLM"), [])


class PipelineAndFormatFixTests(TestCase):
    """#4 无对白镜头 / #6 Play 状态回退 / #11 字幕游标 / #13 ASS 进位 / #14 SSML 转义。"""

    def setUp(self):
        _SEND.start()
        self.addCleanup(_SEND.stop)
        self.play = Play.objects.create(
            title="剧", created_by=_user("pp", UserRole.PRODUCER),
            audit_mode=AuditMode.NONE, status=PlayStatus.ASSET_GEN,
        )
        self.ep = Episode.objects.create(play=self.play, episode_number=1)
        self.scene = Scene.objects.create(episode=self.ep, scene_number=1)

    def _shot(self, n, dialogue="", est=0, audio=None, image=True):
        shot = Shot.objects.create(scene=self.scene, shot_number=n, dialogue=dialogue, duration_estimate=est)
        common = dict(play=self.play, episode=self.ep, scene=self.scene, shot=shot, status=AssetStatus.READY)
        if image:
            Asset.objects.create(asset_type=AssetType.IMAGE, **common)
        if audio is not None:
            Asset.objects.create(asset_type=AssetType.AUDIO, metadata={"duration_seconds": audio}, **common)
        return shot

    def test_silent_shot_does_not_block_materials(self):
        from apps.creation.pipeline.dispatcher import _episode_materials_ready
        self._shot(1, "台词", audio=2.0)
        self._shot(2)  # 无对白、无 AUDIO
        self.assertTrue(_episode_materials_ready(self.ep))

    def test_subtitle_cursor_advances_over_silent_shot(self):
        from apps.distribution.subtitles import build_auto_subtitles
        self._shot(1, "一", audio=2.0)
        self._shot(2, est=3)
        self._shot(3, "三", audio=1.0)
        entries = build_auto_subtitles(self.ep)
        self.assertEqual([(e["start"], e["end"]) for e in entries], [(0.0, 2.0), (5.0, 6.0)])

    def test_play_rolls_back_when_one_episode_redone(self):
        from apps.creation.pipeline.state_machine import summary_play_status
        ep2 = Episode.objects.create(play=self.play, episode_number=2, status=EpisodeStatus.COMPLETED)
        Episode.objects.filter(pk=self.ep.pk).update(status=EpisodeStatus.SCRIPTING)
        Play.objects.filter(pk=self.play.pk).update(status=PlayStatus.COMPLETED)
        self.play.refresh_from_db()
        summary_play_status(self.play)
        self.assertEqual(self.play.status, PlayStatus.SCRIPTING)
        self.assertTrue(ep2.pk)

    def test_ass_time_carry(self):
        from apps.creation.tasks import _ass_time
        self.assertEqual(_ass_time(1.996), "0:00:02.00")
        self.assertEqual(_ass_time(59.999), "0:01:00.00")
        self.assertEqual(_ass_time(3725.5), "1:02:05.50")

    def test_azure_ssml_escaped(self):
        from apps.ai_gateway.adapters import AzureTTSAdapter
        provider = MagicMock(base_url="https://azure", api_key_encrypted="k")
        captured = {}

        def fake_safe_post(p, url, *, timeout=None, **kw):
            captured["content"] = kw.get("content")
            return MagicMock(content=b"mp3")

        with patch("apps.ai_gateway.adapters.safe_post", side_effect=fake_safe_post):
            AzureTTSAdapter(provider).synthesize("你&我<笑>", "zh-CN-X'Y", {})
        ssml = captured["content"].decode()
        self.assertIn("xmlns='http://www.w3.org/2001/10/synthesis'", ssml)
        self.assertIn("你&amp;我&lt;笑&gt;", ssml)
        self.assertIn('name="zh-CN-X\'Y"', ssml)


class ParseJsonFixTests(TestCase):
    """_parse_json 应返回整个文档而非内层第一个对象。"""

    def _parse(self, text):
        from apps.creation.tasks import _parse_json
        return _parse_json(text)

    def test_whole_object_with_nested_array(self):
        text = '{"scenes":[{"scene_number":1,"description":"一"},{"scene_number":2,"description":"二"}]}'
        out = self._parse(text)
        self.assertIsInstance(out, dict)
        self.assertEqual(len(out["scenes"]), 2)
        self.assertEqual(out["scenes"][1]["scene_number"], 2)

    def test_whole_array(self):
        out = self._parse('[{"a":1},{"b":2}]')
        self.assertEqual(len(out), 2)
        self.assertEqual(out[1]["b"], 2)

    def test_fenced_code_block(self):
        text = '```json\n{"episodes":[{"n":1},{"n":2}]}\n```'
        out = self._parse(text)
        self.assertEqual(len(out["episodes"]), 2)

    def test_surrounding_prose(self):
        out = self._parse('好的，剧本如下：{"scenes":[{"n":1}]} 请查收。')
        self.assertEqual(out["scenes"][0]["n"], 1)

    def test_invalid_returns_none(self):
        self.assertIsNone(self._parse("完全不是 JSON"))
        self.assertIsNone(self._parse('{"unclosed": '))


class AssetTypeImportFixTests(ApiFixtureMixin, APITestCase):
    """depends_on_ids 不再因 AssetType 未导入而 500。"""

    def test_create_asset_with_dependency_succeeds(self):
        up = Asset.objects.create(play=self.play, asset_type=AssetType.IMAGE)
        self.client.force_authenticate(self.producer)
        resp = self.client.post(
            "/api/assets/",
            {"play": self.play.pk, "asset_type": AssetType.VIDEO, "depends_on_ids": [up.pk]},
            format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.data["depends_on"][0]["id"], up.pk)


class OwnershipReadOnlyFixTests(ApiFixtureMixin, APITestCase):
    """created_by / members 不可经 PATCH 被改归属（归属变更只能走转让端点）。"""

    def test_patch_cannot_reassign_owner(self):
        other = _user("o2")
        self.client.force_authenticate(self.producer)
        resp = self.client.patch(
            f"/api/plays/{self.play.pk}/",
            {"created_by": other.pk, "members": [other.pk]},
            format="json",
        )
        self.assertEqual(resp.status_code, 200)
        self.play.refresh_from_db()
        self.assertEqual(self.play.created_by, self.producer)
        from apps.creation.models import PlayMember
        self.assertFalse(PlayMember.objects.filter(play=self.play, user=other).exists())

    def test_editor_cannot_take_ownership(self):
        from apps.creation.models import MemberRole
        self.add_member(self.editor, MemberRole.EDITOR)
        self.client.force_authenticate(self.editor)
        resp = self.client.patch(
            f"/api/plays/{self.play.pk}/", {"created_by": self.editor.pk}, format="json"
        )
        # editor 不能修改短剧配置（RBAC manage 层）
        self.assertEqual(resp.status_code, 403)
        self.play.refresh_from_db()
        self.assertEqual(self.play.created_by, self.producer)


class AgentConfigIsolationTests(ApiFixtureMixin, APITestCase):
    """producer 只隔离自己的配方与系统模板；不能改他人的。"""

    def _cfg(self, owner, template=False):
        return AgentConfig.objects.create(name="cfg", created_by=owner, is_template=template)

    def test_producer_cannot_list_others(self):
        self._cfg(_user("o3", UserRole.PRODUCER))
        mine = self._cfg(self.producer)
        self.client.force_authenticate(self.producer)
        data = self.client.get("/api/agent-configs/").data
        self.assertEqual(data["total"], 1)
        self.assertEqual(data["results"][0]["id"], mine.pk)

    def test_producer_cannot_patch_others(self):
        other = self._cfg(_user("o4", UserRole.PRODUCER))
        self.client.force_authenticate(self.producer)
        self.assertEqual(
            # 他人配方被查询集过滤，返回 404（不泄露存在性）
            self.client.patch(f"/api/agent-configs/{other.pk}/", {"name": "hack"}).status_code, 404
        )

    def test_producer_cannot_delete_template(self):
        tpl = self._cfg(_user("o5", UserRole.PRODUCER), template=True)
        self.client.force_authenticate(self.producer)
        self.assertEqual(self.client.delete(f"/api/agent-configs/{tpl.pk}/").status_code, 403)

    def test_admin_sees_all_and_can_write(self):
        other = self._cfg(_user("o6", UserRole.PRODUCER))
        self.client.force_authenticate(self.admin)
        self.assertEqual(self.client.get("/api/agent-configs/").data["total"], 1)
        self.assertEqual(
            self.client.patch(f"/api/agent-configs/{other.pk}/", {"name": "ok"}).status_code, 200
        )

    def test_play_agent_config_scoped_to_own_or_template(self):
        # producer 不能把短剧配方设为他人私有配方
        other = self._cfg(_user("o7", UserRole.PRODUCER))
        self.client.force_authenticate(self.producer)
        resp = self.client.patch(
            f"/api/plays/{self.play.pk}/", {"agent_config_id": other.pk}, format="json"
        )
        self.assertEqual(resp.status_code, 400)  # 校验失败，不指向他人配方

    def test_producer_can_clone_system_template(self):
        tpl = self._cfg(_user("o8", UserRole.PRODUCER), template=True)
        self.client.force_authenticate(self.producer)
        resp = self.client.post(f"/api/agent-configs/{tpl.pk}/clone/", {}, format="json")
        self.assertEqual(resp.status_code, 201, resp.content)
        clone = AgentConfig.objects.get(pk=resp.data["id"])
        self.assertFalse(clone.is_template)
        self.assertEqual(clone.created_by, self.producer)

    def test_producer_cannot_clone_others_private(self):
        other = self._cfg(_user("o9", UserRole.PRODUCER))
        self.client.force_authenticate(self.producer)
        resp = self.client.post(f"/api/agent-configs/{other.pk}/clone/", {}, format="json")
        self.assertEqual(resp.status_code, 404)


class SfxOwnershipFixTests(ApiFixtureMixin, APITestCase):
    """合成时只加载可访问的素材库音效，拒绝他人私有音效。"""

    def test_foreign_private_sfx_skipped(self):
        from apps.creation.tasks import _resolve_sound_effects
        from apps.creation.models import AssetLibrary
        other = AssetLibrary.objects.create(
            name="私有", asset_type=AssetType.SFX, file_url="http://x/sfx.mp3",
            is_public=False, owner=_user("sfx", UserRole.PRODUCER),
        )
        self.shot.sound_effects = [{"library_id": other.pk, "url": "", "offset": 0, "volume": 1.0}]
        self.shot.save(update_fields=["sound_effects"])
        self.shot.refresh_from_db()
        with patch("apps.creation.tasks._download", return_value="/tmp/sfx.mp3"):
            out = _resolve_sound_effects("/tmp", self.shot)
        self.assertEqual(out, [])  # 私有音效被拒绝，不混入成片


class TriggerPublishSyncTests(ApiFixtureMixin, APITestCase):
    """重做后 trigger_publish 同步最新视频，不发布旧成片。"""

    def test_video_asset_synced_on_republish(self):
        from apps.creation.tasks import trigger_publish
        old_video = Asset.objects.create(
            play=self.play, episode=self.ep, asset_type=AssetType.VIDEO,
            status=AssetStatus.READY,
        )
        pub = Publication.objects.create(
            episode=self.ep, channel=self.channel, title="t", video_asset=old_video,
        )
        new_video = Asset.objects.create(
            play=self.play, episode=self.ep, asset_type=AssetType.VIDEO,
            status=AssetStatus.READY,
        )
        Episode.objects.filter(pk=self.ep.pk).update(current_video_asset=new_video)
        Episode.objects.filter(pk=self.ep.pk).update(status=EpisodeStatus.COMPLETED)
        Play.objects.filter(pk=self.play.pk).update(status=PlayStatus.COMPLETED)
        rec = TaskRecord.objects.create(play=self.play, episode=self.ep, task_type=TaskType.PUBLISH)
        trigger_publish(rec.pk)
        pub.refresh_from_db()
        self.assertEqual(pub.video_asset_id, new_video.pk)


class SceneRedoUpdateTests(TestCase):
    """重做剧本时覆盖旧场景内容并删除多余场景。"""

    def _run(self, play, ep, scene_list):
        from apps.creation.tasks import generate_episode_script
        rec = TaskRecord.objects.create(play=play, episode=ep, task_type=TaskType.EPISODE_SCRIPT)
        with patch("apps.creation.tasks._config", return_value=None), \
             patch("apps.creation.tasks._gateway") as gw:
            gw.return_value.call.return_value = {"text": '{"scenes":' + scene_list + '}', "cost": 0}
            generate_episode_script(rec.pk)
        return rec

    def test_redo_updates_existing_and_removes_leftovers(self):
        play = Play.objects.create(title="剧", created_by=_user("rd", UserRole.PRODUCER),
                                   audit_mode=AuditMode.NONE)
        ep = Episode.objects.create(play=play, episode_number=1)
        Scene.objects.create(episode=ep, scene_number=1, description="旧")
        Scene.objects.create(episode=ep, scene_number=2, description="将被删除")
        self._run(play, ep, '[{"scene_number":1,"description":"新","mood":"喜","location":"家"}]')
        self.assertEqual(Scene.objects.filter(episode=ep).count(), 1)
        s = Scene.objects.get(episode=ep, scene_number=1)
        self.assertEqual(s.description, "新")
        self.assertEqual(s.mood, "喜")


class TaskResilienceTests(TestCase):
    """C.2：瞬时失败且 Celery 将自动重试时 TaskRecord 保持 RUNNING，不闪烁 FAILED。

    _run 经 _retry_pending 镜像 Celery autoretry 判定（celery/app/autoretry.py）：
    异常在 autoretry_for 内 + 非直接调用 + 名额未耗尽 → 重试挂起；否则落 FAILED。
    """

    def setUp(self):
        self.producer = _user("tr", UserRole.PRODUCER)
        self.play = Play.objects.create(title="剧", created_by=self.producer)

    def _record(self):
        from apps.creation.models import TaskStatus
        return TaskRecord.objects.create(play=self.play, task_type=TaskType.IMAGE_GEN)

    @staticmethod
    def _celery_task(called_directly, retries):
        """模拟 worker 中的 Celery 任务对象（带 _TASK_RETRY 策略）。"""
        import httpx
        from apps.ai_gateway.gateway import AIGatewayError
        from apps.creation import tasks as T
        task = MagicMock()
        task.autoretry_for = T._TRANSIENT_ERRORS
        task.max_retries = 3
        task.request = MagicMock(called_directly=called_directly, retries=retries)
        assert issubclass(AIGatewayError, Exception) and issubclass(httpx.ReadTimeout, Exception)
        return task

    def test_retry_pending_mirror(self):
        """_retry_pending 镜像判定：瞬时异常+worker+有名额 → True；其余 → False。"""
        import httpx
        from apps.ai_gateway.gateway import AIGatewayError
        from apps.creation.tasks import _retry_pending
        with patch("celery.current_task", self._celery_task(False, 0)):
            self.assertTrue(_retry_pending(AIGatewayError("候选链全挂")))
            self.assertTrue(_retry_pending(httpx.ReadTimeout("read timeout")))
            self.assertFalse(_retry_pending(ValueError("参数错误")))  # 非瞬时
        with patch("celery.current_task", self._celery_task(True, 0)):   # 直接调用
            self.assertFalse(_retry_pending(AIGatewayError("x")))
        with patch("celery.current_task", self._celery_task(False, 4)):  # 名额耗尽
            self.assertFalse(_retry_pending(AIGatewayError("x")))

    def test_run_keeps_running_when_retry_pending(self):
        """瞬时失败待重试：TaskRecord 保持 RUNNING（重试中不落 FAILED）。"""
        import httpx
        from apps.creation.models import TaskStatus
        from apps.creation import tasks as T
        record = self._record()
        with patch("celery.current_task", self._celery_task(False, 0)):
            with self.assertRaises(httpx.ReadTimeout):
                T._run(record.pk, MagicMock(side_effect=httpx.ReadTimeout("t")))
        record.refresh_from_db()
        self.assertEqual(record.status, TaskStatus.RUNNING)

    def test_run_marks_failed_when_exhausted(self):
        """名额耗尽的那次失败：落 FAILED 终态。"""
        import httpx
        from apps.creation.models import TaskStatus
        from apps.creation import tasks as T
        record = self._record()
        with patch("celery.current_task", self._celery_task(False, 4)):
            with self.assertRaises(httpx.ReadTimeout):
                T._run(record.pk, MagicMock(side_effect=httpx.ReadTimeout("t")))
        record.refresh_from_db()
        self.assertEqual(record.status, TaskStatus.FAILED)

    def test_run_marks_failed_on_direct_call(self):
        """直接调用（单测/脚本）永不重试：保持旧行为落 FAILED。"""
        import httpx
        from apps.creation.models import TaskStatus
        from apps.creation import tasks as T
        record = self._record()
        with patch("celery.current_task", self._celery_task(True, 0)):
            with self.assertRaises(httpx.ReadTimeout):
                T._run(record.pk, MagicMock(side_effect=httpx.ReadTimeout("t")))
        record.refresh_from_db()
        self.assertEqual(record.status, TaskStatus.FAILED)

    def test_task_retry_policy_applied(self):
        """全部流水线 @shared_task 带 autoretry/time_limit 策略。"""
        from django.apps import apps as dj_apps
        from application.celery import app  # noqa: F401 — 触发任务注册
        from apps.creation import tasks as T
        for name, fn in vars(T).items():
            if getattr(fn, "__module__", None) == "celery.local" and hasattr(fn, "autoretry_for"):
                self.assertTrue(fn.autoretry_for, f"{name} 缺 autoretry_for")
                self.assertEqual(fn.max_retries, 3)
                self.assertEqual(fn.time_limit, 1900)
                self.assertEqual(fn.soft_time_limit, 1800)
                self.assertTrue(fn.retry_backoff)


class CodeReviewMaxQuickFixTests(ApiFixtureMixin, APITestCase):
    """/code-review max 四项快修的回归测试。"""

    # ── #1 /start/ 改为 manage 层门禁：editor 被授权仍不能启动流水线 ──
    def test_editor_cannot_start_pipeline(self):
        from apps.creation.models import MemberRole
        self.add_member(self.editor, MemberRole.EDITOR)
        self.client.force_authenticate(self.editor)
        # /start/ 第一步即 _ensure_reviewer（manage 层），editor 不达 manage → 403
        resp = self.client.post(f"/api/plays/{self.play.pk}/start/", {}, format="json")
        self.assertEqual(resp.status_code, 403)
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.DRAFT)  # 未被启动

    # ── #2 is_template 仅管理员可设：非 admin 设 true 被拒 ──
    def test_non_admin_cannot_set_is_template(self):
        self.client.force_authenticate(self.producer)
        resp = self.client.post(
            "/api/agent-configs/", {"name": "伪装模板", "is_template": True}, format="json",
        )
        self.assertEqual(resp.status_code, 400)
        self.assertFalse(
            AgentConfig.objects.filter(name="伪装模板", is_template=True).exists())

    def test_admin_can_set_is_template(self):
        self.client.force_authenticate(self.admin)
        resp = self.client.post(
            "/api/agent-configs/", {"name": "官方模板", "is_template": True}, format="json",
        )
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertTrue(AgentConfig.objects.get(pk=resp.data["id"]).is_template)

    # ── #5 publish 校验成片视频：COMPLETED 但 current_video_asset 缺失 → 400 ──
    def test_publish_rejects_episode_without_video(self):
        Episode.objects.filter(pk=self.ep.pk).update(status=EpisodeStatus.COMPLETED)
        Play.objects.filter(pk=self.play.pk).update(status=PlayStatus.COMPLETED)
        # current_video_asset 本就为 None（fixture 未产出成片）
        self.assertIsNone(self.ep.current_video_asset)
        self.client.force_authenticate(self.producer)
        resp = self.client.post(f"/api/plays/{self.play.pk}/publish/", {}, format="json")
        self.assertEqual(resp.status_code, 400)
        self.assertIn("视频", resp.data["detail"])
        self.assertFalse(Publication.objects.filter(episode=self.ep).exists())

