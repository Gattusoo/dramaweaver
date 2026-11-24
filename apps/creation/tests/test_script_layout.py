"""剧本专业排版与导出（script-layout，2026-11-23 组 3）。

覆盖：Fountain 五要素渲染（场次/角色/对话/动作/转场）、txt 排版、pdf 字节流
（fpdf2）、历史 ScriptVersion 快照导出、端点权限（读成员可导出/viewer 404 非
成员）、格式校验与错误信封外的文件流形态（不包 JSON 信封）。
"""
from django.test import TestCase
from rest_framework.test import APIClient

from apps.creation.tests.test_assistant import AssistantFixtureMixin

from ..assistant.models import ScriptVersion
from ..pipeline import script_layout


class LayoutFixture(AssistantFixtureMixin):
    def setUp(self):
        super().setUp()
        from ..models import Character, CharacterRole
        self.ch = Character.objects.create(play=self.play, name="林霄",
                                           role=CharacterRole.PROTAGONIST)
        self.scene.location = "写字楼办公室"
        self.scene.save(update_fields=["location"])
        self.shot.dialogue_character = self.ch
        self.shot.action_note = "攥紧拳头，眼神冷冽"
        self.shot.visual_prompt = "全景：林家大厅，众人围观"
        self.shot.save(update_fields=["dialogue_character", "action_note", "visual_prompt"])

    def get(self, user, query):
        client = APIClient()
        client.force_authenticate(user)
        return client.get(f"/api/plays/{self.play.pk}/script/export/", query)


class FountainRenderTests(LayoutFixture, TestCase):
    def test_five_elements_golden(self):
        """Fountain 渲染 golden：场次标题 EXT/INT 前缀、角色大写、对话、动作、CUT TO:。"""
        scenes = list(self.ep.scenes.order_by("scene_number"))
        text = script_layout.render_fountain("第三集", scenes)
        self.assertIn("EXT. 写字楼办公室 - 日. 场景 1".upper(), text)
        self.assertIn("攥紧拳头，眼神冷冽", text)       # 动作（action_note 非空优先）
        self.assertIn("林霄".upper(), text)              # 角色行大写
        self.assertIn("你以为你还能逃吗", text)           # 对白
        self.assertIn("CUT TO:", text)                   # 转场
        self.assertTrue(text.startswith("Title: 第三集"))

    def test_plain_text_layout(self):
        """txt：中文可读分镜式排版（镜头行 + 缩进对白）。"""
        scenes = list(self.ep.scenes.order_by("scene_number"))
        text = script_layout.render_plain_text("第三集", scenes)
        self.assertIn("【场景 1】写字楼办公室", text)
        self.assertIn("镜头1：攥紧拳头，眼神冷冽", text)
        self.assertIn("林霄：你以为你还能逃吗", text)

    def test_pdf_bytes(self):
        """pdf：fpdf2 产出合法 PDF 字节流（%PDF 头）。"""
        scenes = list(self.ep.scenes.order_by("scene_number"))
        fountain = script_layout.render_fountain("第三集", scenes)
        data = script_layout.render_pdf("第三集", fountain)
        self.assertTrue(data.startswith(b"%PDF"))

    def test_snapshot_dict_shape(self):
        """dict 快照渲染（ScriptVersion.content.scenes 形状）与模型渲染同出。"""
        snap = script_layout.snapshot_scenes(self.ep)
        self.assertEqual(len(snap), 1)
        text = script_layout.render_fountain("第三集", [
            {"scene_number": 1, **snap["1"]}])
        self.assertIn("你以为你还能逃吗", text)
        self.assertIn("CUT TO:", text)


class ExportEndpointTests(LayoutFixture, TestCase):
    def test_fountain_export_file_stream(self):
        """fountain 导出：文件流形态（无 JSON 信封）+ 附件头。"""
        resp = self.get(self.producer, {"episode_id": self.ep.pk, "fmt": "fountain"})
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/plain", resp["Content-Type"])
        self.assertIn("episode-3.fountain", resp["Content-Disposition"])
        body = resp.content.decode("utf-8")
        self.assertIn("CUT TO:", body)
        self.assertNotIn('"code"', body)  # 不包 JSON 信封

    def test_txt_and_pdf_formats(self):
        txt = self.get(self.viewer, {"episode_id": self.ep.pk, "fmt": "txt"})
        self.assertEqual(txt.status_code, 200)
        self.assertIn("【场景 1】", txt.content.decode("utf-8"))
        pdf = self.get(self.viewer, {"episode_id": self.ep.pk, "fmt": "pdf"})
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")
        self.assertTrue(pdf.content.startswith(b"%PDF"))

    def test_bad_format_400(self):
        resp = self.get(self.producer, {"episode_id": self.ep.pk, "fmt": "docx"})
        self.assertEqual(resp.status_code, 400)

    def test_missing_episode_404(self):
        resp = self.get(self.producer, {"episode_id": 999999})
        self.assertEqual(resp.status_code, 404)

    def test_version_export_uses_snapshot(self):
        """历史版本导出：按 ScriptVersion 快照渲染（非当前结构）。"""
        version = ScriptVersion.objects.create(
            play=self.play, episode=self.ep, version=1, branch_name="main",
            content={"text": "旧版剧本", "scenes": {
                "1": {"location": "旧地点", "time_of_day": "NIGHT",
                      "description": "旧版场景描述",
                      "shots": [{"shot_number": 1, "description": "旧镜头",
                                 "dialogue": "旧对白", "action_note": ""}]}}})
        resp = self.get(self.producer, {
            "episode_id": self.ep.pk, "fmt": "fountain",
            "script_version_id": version.pk})
        self.assertEqual(resp.status_code, 200)
        body = resp.content.decode("utf-8")
        self.assertIn("旧地点", body)
        self.assertIn("旧对白", body)
        self.assertNotIn("办公室对峙", body)  # 当前结构不混入

    def test_cross_play_version_404(self):
        version = ScriptVersion.objects.create(
            play=self.foreign_scene.episode.play,
            episode=self.foreign_scene.episode, version=1, branch_name="main",
            content={"scenes": {}})
        resp = self.get(self.producer, {
            "episode_id": self.ep.pk, "fmt": "fountain",
            "script_version_id": version.pk})
        self.assertEqual(resp.status_code, 404)
