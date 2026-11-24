"""创作助手工具集扩展测试（openspec change: backend-assistant-toolset）。"""
import json
from unittest.mock import patch

from django.test import SimpleTestCase, TestCase
from rest_framework.test import APITestCase

from apps.creation.assistant import events, tools
from apps.creation.assistant.project_memory import revision_of
from apps.creation.assistant.models import EMPTY_REVISION, AssistantProposal, ProposalKind, ProposalStatus
from apps.ai_gateway.models import (
    AIProvider,
)
from apps.creation.models import (
    Asset, AssetLibrary, AssetStatus, AssetType, Character, Episode, PlayStatus, ReviewLog, Scene, Shot, TaskRecord, TaskType, VoiceProfile,
)
from dvadmin.system.models import (
    CustomUser, UserRole,
)
from .test_assistant import AssistantFixtureMixin, FakeChatGateway, _say, _use


class ValidatorTests(SimpleTestCase):
    """任务 1.2。"""

    def test_number_nullable_boolean(self):
        v = tools.validate
        self.assertIsNone(v({"type": ["integer", "null"]}, None))
        self.assertEqual(v({"type": ["integer", "null"]}, 3), 3)
        self.assertEqual(v({"type": "number", "maximum": 2}, 1.5), 1.5)
        self.assertTrue(v({"type": "boolean"}, True))
        for schema, value in (({"type": ["integer", "null"]}, "3"), ({"type": "number"}, "1"),
                              ({"type": "number", "maximum": 2}, 3), ({"type": "boolean"}, 1),
                              ({"type": "integer", "maximum": 5}, 6)):
            with self.assertRaises(tools.ToolError):
                v(schema, value)


class ToolsetMixin(AssistantFixtureMixin):
    def setUp(self):
        super().setUp()
        self.s2 = Shot.objects.create(scene=self.scene, shot_number=2, dialogue="第二句")
        self.s3 = Shot.objects.create(scene=self.scene, shot_number=3, visual_prompt="远景")
        self.hero = Character.objects.create(play=self.play, name="林总", role="PROTAGONIST",
                                             appearance_prompt="西装")
        # 计费派发闸接线后渲染类预扣按 60s×单价（约数千分）——补测试余额防闸拒绝
        from apps.billing.services.wallet import grant
        grant(self.producer, 1_000_000, ref_kind="test", ref_id=f"ts-{id(self)}")

    def apply(self, proposal, user=None, **body):
        self.client.force_authenticate(user or self.producer)
        with self.captureOnCommitCallbacks(execute=True):
            return self.client.post(f"/api/assistant/proposals/{proposal.pk}/apply/", body, format="json")

    def numbers(self):
        return list(Shot.objects.filter(scene=self.scene).order_by("shot_number").values_list("pk", "shot_number"))

    def dispatched(self):
        return sorted(c.args[0] for c in self.send.call_args_list)


class QueryToolTests(ToolsetMixin, TestCase):
    """任务 2.1-2.3。"""

    def test_get_shot(self):
        self.seed(episode=self.ep, scene=self.scene, shot=self.shot, asset_type=AssetType.IMAGE)
        r, err = tools.run_tool(self.ctx(), "get_shot", {"shot_id": self.shot.pk})
        self.assertFalse(err, r)
        self.assertEqual((r["shot_number"], r["assets"]["image"]["status"], r["assets"]["audio"]),
                         (1, AssetStatus.READY, None))
        self.assertIn("sound_effects", r)
        other = Shot.objects.create(scene=self.foreign_scene, shot_number=1, dialogue="机密")
        r, err = tools.run_tool(self.ctx(), "get_shot", {"shot_id": other.pk})
        self.assertTrue(err)
        self.assertNotIn("机密", json.dumps(r, ensure_ascii=False))

    def test_get_characters(self):
        self.shot.dialogue_character = self.hero
        self.shot.save()
        self.seed(character=self.hero, asset_type=AssetType.CHARACTER_REF)
        r, _ = tools.run_tool(self.ctx(), "get_characters", {})
        c = r["characters"][0]
        self.assertEqual((c["name"], c["has_reference_image"], c["voice"], c["dialogue_shot_count"]),
                         ("林总", True, None, 1))

    def test_search_asset_library_visibility(self):
        stranger = CustomUser.objects.create_user(username="ls", password="Passw0rd!123")
        AssetLibrary.objects.create(name="门铃", asset_type=AssetType.SFX, file_url="https://x/a.mp3", is_public=True)
        AssetLibrary.objects.create(name="门铃私", asset_type=AssetType.SFX, file_url="https://x/b.mp3",
                                    is_public=False, owner=stranger)
        AssetLibrary.objects.create(name="门铃我的", asset_type=AssetType.SFX, file_url="https://x/c.mp3",
                                    is_public=False, owner=self.producer)
        AssetLibrary.objects.create(name="门铃BGM", asset_type=AssetType.BGM, file_url="https://x/d.mp3")
        r, _ = tools.run_tool(self.ctx(), "search_asset_library", {"query": "门铃", "asset_type": "SFX"})
        self.assertEqual(sorted(i["name"] for i in r["items"]), ["门铃", "门铃我的"])


class ShotAndCharacterProposalTests(ToolsetMixin, APITestCase):
    """任务 3.1-3.3。"""

    def test_shot_edit_extended_fields(self):
        other_char = Character.objects.create(play=self.foreign_scene.episode.play, name="外人", role="SUPPORTING")
        r, err = tools.run_tool(self.ctx(), "propose_shot_edit", {
            "shot_id": self.shot.pk, "summary": "s", "dialogue_character_id": other_char.pk})
        self.assertTrue(err)
        self.assertIn("不属于本剧", r["error"])
        p = self.propose("propose_shot_edit", {
            "shot_id": self.shot.pk, "summary": "s", "dialogue_character_id": self.hero.pk,
            "shot_type": "CLOSE_UP", "duration_estimate": 4,
            "sound_effects": [{"library_id": 3, "volume": 0.5}, {"offset": 1}]})
        self.assertEqual(p.payload["sound_effects"], [{"library_id": 3, "url": "", "offset": 0.0,
                                                       "volume": 0.5, "duration": None}])
        self.assertEqual(p.base_snapshot["dialogue_character_id"], None)
        self.assertEqual(self.apply(p).status_code, 200)
        self.shot.refresh_from_db()
        self.assertEqual((self.shot.dialogue_character_id, self.shot.shot_type, self.shot.duration_estimate),
                         (self.hero.pk, "CLOSE_UP", 4))

    def test_character_edit_and_create(self):
        r, err = tools.run_tool(self.ctx(), "propose_character_create", {"name": "林总", "role": "SUPPORTING",
                                                                         "summary": "s"})
        self.assertTrue(err)
        self.assertIn("已有角色", r["error"])
        self.shot.dialogue_character = self.hero
        self.shot.save()
        img = self.seed(episode=self.ep, scene=self.scene, shot=self.shot, asset_type=AssetType.IMAGE)
        ref = self.seed(character=self.hero, asset_type=AssetType.CHARACTER_REF)
        edit = self.propose("propose_character_edit", {"character_id": self.hero.pk, "summary": "改外貌",
                                                       "appearance_prompt": "黑色风衣，左眉疤痕"})
        self.assertEqual(sorted(edit.impacted_asset_ids), sorted([img.pk, ref.pk]))
        resp = self.apply(edit, redo=True)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.hero.refresh_from_db()
        ref.refresh_from_db()
        self.assertEqual((self.hero.appearance_prompt, ref.status), ("黑色风衣，左眉疤痕", AssetStatus.SUPERSEDED))
        self.assertEqual(self.dispatched(), ["shortplay.tasks.generate_character_ref"])
        # 工作台 A2：dispatch 生成快照写入 input_summary["generation"]（含 task_type 等），
        # 业务字段仍平铺在顶层；此处只断言业务字段（generation 由 test_generation_spec 覆盖）。
        record = TaskRecord.objects.get(task_type=TaskType.CHARACTER_REF)
        self.assertEqual(record.input_summary["character"], self.hero.pk)
        self.assertIn("generation", record.input_summary)
        self.assertEqual(record.input_summary["generation"]["task_type"], "CHARACTER_REF")
        self.assertEqual(ReviewLog.objects.get(play=self.play).review_point, "CHARACTER_REF")

        create = self.propose("propose_character_create", {"name": "王经理", "role": "ANTAGONIST",
                                                           "description": "阴险", "summary": "加反派"})
        Character.objects.create(play=self.play, name="王经理", role="SUPPORTING")  # 期间被手工新增
        self.assertEqual(self.apply(create).status_code, 409)
        create.refresh_from_db()
        self.assertEqual(create.status, ProposalStatus.STALE)


class StructureProposalTests(ToolsetMixin, APITestCase):
    """任务 4.1-4.4。"""

    def test_insert_shot(self):
        p = self.propose("propose_shot_insert", {"scene_id": self.scene.pk, "after_shot_number": 1,
                                                 "visual_prompt": "她推门而入", "dialogue": "等等", "summary": "s"})
        self.assertTrue(tools.run_tool(self.ctx(), "propose_shot_insert", {
            "scene_id": self.scene.pk, "after_shot_number": 9, "visual_prompt": "x", "summary": "s"})[1])
        resp = self.apply(p)
        self.assertEqual(resp.status_code, 200, resp.data)
        new_id = resp.data["result"]["shot_id"]
        self.assertEqual(self.numbers(), [(self.shot.pk, 1), (new_id, 2), (self.s2.pk, 3), (self.s3.pk, 4)])
        new = Shot.objects.get(pk=new_id)
        self.assertEqual((new.visual_prompt, new.dialogue), ("她推门而入", "等等"))
        board = Asset.objects.get(shot=new, asset_type=AssetType.STORYBOARD)
        self.assertEqual(json.loads(board.metadata["text"])["shot_number"], 2)
        self.send.assert_not_called()

    def test_insert_at_front_and_redo(self):
        p = self.propose("propose_shot_insert", {"scene_id": self.scene.pk, "after_shot_number": 0,
                                                 "visual_prompt": "开场", "summary": "s"})
        resp = self.apply(p, redo=True)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self.numbers()[0], (resp.data["result"]["shot_id"], 1))
        self.assertEqual(self.dispatched(), ["shortplay.tasks.generate_image", "shortplay.tasks.generate_tts"])

    def test_delete_shot(self):
        img = self.seed(episode=self.ep, scene=self.scene, shot=self.s2, asset_type=AssetType.IMAGE)
        p = self.propose("propose_shot_delete", {"shot_id": self.s2.pk, "summary": "多余"})
        self.assertEqual(self.apply(p, redo=True).status_code, 400)
        resp = self.apply(p)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self.numbers(), [(self.shot.pk, 1), (self.s3.pk, 2)])
        img.refresh_from_db()
        self.assertEqual((img.status, img.shot_id), (AssetStatus.SUPERSEDED, None))  # 历史保留
        p.refresh_from_db()
        self.assertEqual((p.status, p.target_shot_id, p.payload["shot_number"]), (ProposalStatus.APPLIED, None, 2))
        self.client.force_authenticate(self.producer)
        label = self.client.get(f"/api/assistant/proposals/{p.pk}/").data["target_label"]
        self.assertEqual(label, "第 3 集 · 场景 1 · 删除镜头 2")
        self.send.assert_not_called()

    def test_structure_change_makes_stale(self):
        p = self.propose("propose_shot_insert", {"scene_id": self.scene.pk, "after_shot_number": 3,
                                                 "visual_prompt": "x", "summary": "s"})
        Shot.objects.create(scene=self.scene, shot_number=4)
        resp = self.apply(p)
        self.assertEqual((resp.status_code, resp.data["status"]), (409, "STALE"))
        d = self.propose("propose_shot_delete", {"shot_id": self.s3.pk, "summary": "s"})
        Shot.objects.filter(pk=self.s3.pk).update(dialogue="手工改过")
        self.assertEqual(self.apply(d).status_code, 409)

    def test_inserts_do_not_stale_each_other(self):
        a = self.propose("propose_shot_insert", {"scene_id": self.scene.pk, "after_shot_number": 1,
                                                 "visual_prompt": "a", "summary": "s"})
        b = self.propose("propose_shot_insert", {"scene_id": self.scene.pk, "after_shot_number": 2,
                                                 "visual_prompt": "b", "summary": "s"})
        a.refresh_from_db()
        self.assertEqual((a.status, b.status), (ProposalStatus.PENDING, ProposalStatus.PENDING))

    def test_scene_create(self):
        p = self.propose("propose_scene_create", {"episode_number": 3, "location": "天台", "description": "对峙",
                                                  "time_of_day": "NIGHT", "summary": "s"})
        resp = self.apply(p, redo=True)
        self.assertEqual(resp.status_code, 200, resp.data)
        scene = Scene.objects.get(pk=resp.data["result"]["scene_id"])
        self.assertEqual((scene.scene_number, scene.location, scene.time_of_day), (2, "天台", "NIGHT"))
        self.assertEqual(self.dispatched(), ["shortplay.tasks.generate_storyboard"])


class GenerationRequestTests(ToolsetMixin, APITestCase):
    """任务 5.1-5.2。"""

    def test_estimate_and_dispatch(self):
        p = self.propose("request_generation", {"action": "redo_scene", "target_id": self.scene.pk, "summary": "s"})
        self.assertEqual(p.payload["estimated_tasks"], 5)  # 3 张图 + 2 段配音
        self.send.assert_not_called()
        img = self.seed(episode=self.ep, scene=self.scene, shot=self.shot, asset_type=AssetType.IMAGE)
        self.assertEqual(self.apply(p, redo=True).status_code, 400)
        resp = self.apply(p)
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(len(resp.data["result"]["task_record_ids"]), 5)
        self.assertEqual(self.send.call_count, 5)
        img.refresh_from_db()
        self.assertEqual(img.status, AssetStatus.SUPERSEDED)
        self.assertFalse(ReviewLog.objects.exists())
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.PAUSED_AT_SCRIPT)

    def test_status_gate_and_preview(self):
        from apps.creation.models import Play
        Play.objects.filter(pk=self.play.pk).update(status=PlayStatus.STORYBOARDING)
        redo = self.propose("request_generation", {"action": "redo_shot", "target_id": self.shot.pk, "summary": "s"})
        self.assertEqual(redo.payload["estimated_tasks"], 2)
        self.assertEqual(self.apply(redo).status_code, 409)
        preview = self.propose("request_generation", {"action": "preview_episode", "target_id": self.ep.pk,
                                                      "summary": "s"})
        resp = self.apply(preview)  # 预览不受流水线状态门禁
        self.assertEqual(resp.status_code, 200, resp.data)
        self.assertEqual(self.dispatched(), ["shortplay.tasks.render_preview"])
        again = self.propose("request_generation", {"action": "preview_episode", "target_id": self.ep.pk,
                                                    "summary": "s"})
        self.assertEqual(self.apply(again).status_code, 409)  # 同集已有进行中的预览

    def test_wrong_target(self):
        r, err = tools.run_tool(self.ctx(), "request_generation", {
            "action": "redo_scene", "target_id": self.foreign_scene.pk, "summary": "s"})
        self.assertTrue(err)


class ReviewTests(ToolsetMixin, TestCase):
    """任务 6.1-6.2。"""

    def codes(self):
        r, err = tools.run_tool(self.ctx(), "review_episode", {"episode_number": 3})
        self.assertFalse(err, r)
        return r, {i["code"] for i in r["issues"]}

    def test_detects_each_issue(self):
        Scene.objects.create(episode=self.ep, scene_number=2)
        self.shot.dialogue = "王经理：你被开除了，现在就收拾东西给我滚出公司大门不许回来听到没有我再说一遍滚出去"
        self.shot.duration_estimate = 3
        self.shot.visual_prompt = "办公室"
        self.shot.save()
        self.s2.dialogue_character = self.hero
        self.s2.save()
        r, codes = self.codes()
        self.assertEqual(codes, {"empty_scene", "missing_visual_prompt", "dialogue_without_character",
                                 "unknown_speaker", "duration_too_short", "missing_image", "missing_audio",
                                 "character_without_ref", "character_without_voice",
                                 "episode_duration_out_of_range"})
        short = next(i for i in r["issues"] if i["code"] == "duration_too_short")
        self.assertEqual((short["scene_number"], short["shot_number"]), (1, 1))
        self.assertEqual(r["stats"]["shots"], 3)

    def test_clean_episode(self):
        provider = AIProvider.objects.create(name="t", provider_type="TTS", base_url="https://t", api_key_encrypted="")
        self.hero.voice_profile = VoiceProfile.objects.create(name="v", gender="MALE", provider=provider, voice_id="v")
        self.hero.save()
        self.seed(character=self.hero, asset_type=AssetType.CHARACTER_REF)
        Shot.objects.filter(scene=self.scene).delete()
        for n in range(1, 21):  # 20 × 4 秒 = 80 秒
            shot = Shot.objects.create(scene=self.scene, shot_number=n, visual_prompt="画面", dialogue="林总：好的",
                                       dialogue_character=self.hero, duration_estimate=4)
            for t in (AssetType.IMAGE, AssetType.AUDIO):
                self.seed(episode=self.ep, scene=self.scene, shot=shot, asset_type=t)
        r, codes = self.codes()
        self.assertEqual(r["issues"], [])
        self.assertEqual(r["stats"]["estimated_seconds"], 80.0)


class AskUserAndMemoryTests(ToolsetMixin, APITestCase):
    """任务 7.1-7.4。"""

    def test_ask_user_ends_turn(self):
        from apps.creation.assistant.runner import _save, run_assistant_turn
        questions = [{"id": "who", "prompt": "反派身份？", "type": "single", "options": ["同事", "前任"]},
                     {"id": "ep", "prompt": "第几集出场？", "type": "text"}]
        bad, err = tools.run_tool(self.ctx(), "ask_user", {"questions": [{"id": "x", "prompt": "?", "type": "single",
                                                                           "options": ["只有一个"]}]})
        self.assertTrue(err)
        gw = FakeChatGateway([_use("q1", "ask_user", {"questions": questions}), _say("不应到达")])
        record = TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                           input_summary={"session_id": self.session.pk})
        _save(self.session, "user", [{"type": "text", "text": "加一个反派"}], record)
        with patch("apps.ai_gateway.gateway.AIGateway", return_value=gw):
            run_assistant_turn(record.pk)
        record.refresh_from_db()
        evs = [e for e in events.read(self.session.pk)[0] if e["type"] not in {"span", "tool_error", "eval"}]
        self.assertEqual([e["type"] for e in evs][-2:], ["question", "done"])
        self.assertEqual(evs[-2]["data"], {"tool_use_id": "q1", "questions": questions})
        self.assertEqual((record.status, record.output_summary["outcome"], len(gw.calls)),
                         ("SUCCESS", "awaiting_user", 1))

    def test_memory_tool_revision(self):
        # Phase 1.1: 新短剧自动创建预填充的记忆（非空）
        r, _ = tools.run_tool(self.ctx(), "memory_read", {})
        self.assertNotEqual(r["revision"], EMPTY_REVISION)  # 自动初始化
        self.assertIn("创作约定", r["content"])  # 模板内容
        saved, err = tools.run_tool(self.ctx(), "memory_save", {"content": "对白不超过 20 字", "revision": r["revision"]})
        self.assertFalse(err, saved)
        self.assertEqual(saved["revision"], revision_of("对白不超过 20 字"))
        stale, err = tools.run_tool(self.ctx(), "memory_save", {"content": "覆盖", "revision": r["revision"]})
        self.assertTrue(err)
        self.assertIn("重新读取", stale["error"])
        # 另一个会话读到同一份记忆
        from apps.creation.assistant.models import AssistantSession
        other = AssistantSession.objects.create(play=self.play, user=self.producer)
        self.assertEqual(tools.run_tool(tools.ToolContext(other), "memory_read", {})[0]["content"], "对白不超过 20 字")

    def test_memory_api(self):
        url = f"/api/assistant/memories/{self.play.pk}/"
        self.client.force_authenticate(self.viewer)
        # Phase 1.1: 新短剧自动创建预填充的记忆（非空）
        self.assertNotEqual(self.client.get(url).data["revision"], EMPTY_REVISION)
        self.assertEqual(self.client.put(url, {"content": "x", "revision": EMPTY_REVISION}, format="json")
                         .status_code, 403)
        self.client.force_authenticate(self.producer)
        # 获取当前版本号
        current = self.client.get(url).data
        ok = self.client.put(url, {"content": "偏好A", "revision": current["revision"]}, format="json")
        self.assertEqual(ok.status_code, 200, ok.data)
        conflict = self.client.put(url, {"content": "偏好B", "revision": current["revision"]}, format="json")
        self.assertEqual((conflict.status_code, conflict.data["content"]), (409, "偏好A"))
        stranger = CustomUser.objects.create_user(username="ms", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.client.force_authenticate(stranger)
        self.assertEqual(self.client.get(url).status_code, 404)

    def test_system_prompt_rules(self):
        from apps.creation.assistant.runner import DEFAULT_SYSTEM
        # Phase 1.1: 记忆已自动加载到system prompt，memory_read不再是必须的
        # 但 memory_save 仍然是工具，需要保存时调用
        for word in ("request_generation", "ask_user", "memory_save", "review_episode"):
            self.assertIn(word, DEFAULT_SYSTEM)
