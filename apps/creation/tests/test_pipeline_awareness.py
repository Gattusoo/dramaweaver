# -*- coding: utf-8 -*-
"""流水线感知工具测试（openspec change: backend-pipeline-awareness）。

覆盖：pipeline_status（暂停态/进行态/终态/恢复任务/只读/按剧隔离）、
pipeline_next_steps（全状态覆盖/暂停态 hint/恢复提示/不自动执行/预算内 E2E）。
"""
from unittest.mock import patch

from django.test import TestCase

from apps.creation.assistant.tools import ToolContext, run_tool, tool_specs
from apps.creation.models import (
    AgentConfig, Asset, AssetStatus, AssetType, Episode, Play, PlayStatus, TaskRecord,
    TaskStatus, TaskType,
)
from apps.core.system.models import CustomUser, UserRole

from .test_assistant import AssistantFixtureMixin, FakeChatGateway, _say, _use


class PipelineStatusTests(AssistantFixtureMixin, TestCase):
    def _fresh_ctx(self):
        """重取会话（避免 fixture 会话缓存已过期的 play 状态）。"""
        from apps.creation.assistant.models import AssistantSession
        fresh = AssistantSession.objects.select_related("play").get(pk=self.session.pk)
        return ToolContext(fresh)

    def _run(self):
        return run_tool(self._fresh_ctx(), "pipeline_status", {})

    def _play_state(self):
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, PlayStatus.PAUSED_AT_SCRIPT)

    def test_paused_state_snapshot(self):
        """暂停态：标记 + 待审核计数 + 分集进度（spec Scenario）。"""
        n_before = Asset.objects.filter(play=self.play).exclude(
            status=AssetStatus.SUPERSEDED).count()
        # 新类型（不与 fixture 的 OUTLINE/EPISODE_SCRIPT 同槽位，避免 READY 覆盖旧版本语义）
        Asset.objects.create(play=self.play, asset_type=AssetType.STORYBOARD,
                             status=AssetStatus.READY, episode=self.ep)
        Asset.objects.create(play=self.play, asset_type=AssetType.BGM,
                             status=AssetStatus.SUPERSEDED, episode=self.ep)
        result, is_err = self._run()
        self.assertFalse(is_err, result)
        self.assertEqual(result["play_status"], "PAUSED_AT_SCRIPT")
        self.assertEqual(result["play_status_label"], "分集剧本审核暂停")
        self.assertTrue(result["pending_reviews"]["paused"])
        # 待审核只数非 SUPERSEDED（play 全局：原有 + 新 READY 分镜，废弃不计）
        self.assertEqual(result["pending_reviews"]["reviewable_assets"], n_before + 1)
        self.assertEqual(len(result["episode_progress"]), 1)
        ep = result["episode_progress"][0]
        # 分集侧只数本集资产（fixture 大纲不挂集）：script(READY) + 分镜(READY)
        self.assertEqual(ep["asset_ready"], 2)
        self.assertEqual(ep["asset_total"], 2)

    def test_active_tasks_counted_by_type(self):
        TaskRecord.objects.create(play=self.play, task_type=TaskType.IMAGE_GEN,
                                  status=TaskStatus.RUNNING)
        TaskRecord.objects.create(play=self.play, task_type=TaskType.IMAGE_GEN,
                                  status=TaskStatus.PENDING)
        TaskRecord.objects.create(play=self.play, task_type=TaskType.TTS_GEN,
                                  status=TaskStatus.RETRYING)
        # 终态与助手轮次不计
        TaskRecord.objects.create(play=self.play, task_type=TaskType.VIDEO_COMPOSE,
                                  status=TaskStatus.SUCCESS)
        TaskRecord.objects.create(play=self.play, task_type=TaskType.ASSISTANT_TURN,
                                  status=TaskStatus.RUNNING)
        result, is_err = self._run()
        self.assertFalse(is_err)
        self.assertEqual(result["active_tasks"], {"IMAGE_GEN": 2, "TTS_GEN": 1})
        self.assertEqual(result["recovery_required"], 0)

    def test_recovery_required_listed_separately(self):
        """恢复任务单列，不计入普通进行中（spec Scenario）。"""
        TaskRecord.objects.create(play=self.play, task_type=TaskType.IMAGE_GEN,
                                  status=TaskStatus.RECOVERY_REQUIRED)
        result, is_err = self._run()
        self.assertFalse(is_err)
        self.assertEqual(result["active_tasks"], {})
        self.assertEqual(result["recovery_required"], 1)

    def test_play_isolation(self):
        """按剧隔离：另一剧的任务/资产不可见（spec Scenario）。"""
        TaskRecord.objects.create(play=self.play, task_type=TaskType.IMAGE_GEN,
                                  status=TaskStatus.RUNNING)
        other_play = Play.objects.create(title="乙剧", created_by=self.producer)
        TaskRecord.objects.create(play=other_play, task_type=TaskType.TTS_GEN,
                                  status=TaskStatus.RUNNING)
        result, is_err = self._run()
        self.assertFalse(is_err)
        self.assertEqual(result["active_tasks"], {"IMAGE_GEN": 1})

    def test_read_only_no_mutation(self):
        """纯只读：调用前后 play/episode/asset 状态不变。"""
        TaskRecord.objects.create(play=self.play, task_type=TaskType.IMAGE_GEN,
                                  status=TaskStatus.PENDING)
        ep_status_before = self.ep.status
        asset_qs_before = list(Asset.objects.filter(play=self.play).values_list("status", flat=True))
        _, is_err = self._run()
        self.assertFalse(is_err)
        self.ep.refresh_from_db()
        self.assertEqual(self.ep.status, ep_status_before)
        self.assertEqual(list(Asset.objects.filter(play=self.play).values_list("status", flat=True)),
                         asset_qs_before)
        # play.status 未被重算改写（未调 summary_play_status）
        self._play_state()

    def test_completed_and_empty_play(self):
        """终态剧：正常返回不报错（fixture 自带 1 个分集）。"""
        Play.objects.filter(pk=self.play.pk).update(status=PlayStatus.COMPLETED)
        result, is_err = self._run()
        self.assertFalse(is_err)
        self.assertEqual(result["play_status"], "COMPLETED")
        self.assertFalse(result["pending_reviews"]["paused"])
        self.assertEqual(len(result["episode_progress"]), 1)


class PipelineNextStepsTests(AssistantFixtureMixin, TestCase):
    def _fresh_ctx(self):
        from apps.creation.assistant.models import AssistantSession
        fresh = AssistantSession.objects.select_related("play").get(pk=self.session.pk)
        return ToolContext(fresh)

    def _run(self):
        return run_tool(self._fresh_ctx(), "pipeline_next_steps", {})

    def test_all_status_coverage(self):
        """全状态覆盖：每个 PlayStatus 都有建议或显式无建议。"""
        from apps.creation.models import PlayStatus as PS
        result_cache = {}
        for status in PS.values:
            Play.objects.filter(pk=self.play.pk).update(status=status)
            result, is_err = self._run()
            self.assertFalse(is_err, f"{status} 不应报错")
            if status in ("PAUSED_AT_OUTLINE", "PAUSED_AT_SCRIPT", "PAUSED_AT_ASSETS",
                          "DRAFT", "OUTLINING", "SCRIPTING", "STORYBOARDING", "ASSET_GEN",
                          "COMPOSING", "COMPLETED", "PUBLISHED"):
                self.assertTrue(result["next_suggestions"], f"{status} 应有建议")
                self.assertEqual(result["next_suggestions"][0]["source_status"], status)
            result_cache[status] = result
        # 暂停态建议语义抽查
        Play.objects.filter(pk=self.play.pk).update(status=PlayStatus.PAUSED_AT_ASSETS)
        result, _ = self._run()
        self.assertIn("合成", result["next_suggestions"][0]["suggestion"])

    def test_paused_hint_with_reviewable_count(self):
        """暂停态附待审核计数（spec Scenario）。"""
        from apps.creation.models import AssetType
        n_before = Asset.objects.filter(play=self.play).exclude(
            status=AssetStatus.SUPERSEDED).count()
        Asset.objects.create(play=self.play, asset_type=AssetType.STORYBOARD,
                             status=AssetStatus.READY, episode=self.ep)
        result, is_err = self._run()
        self.assertFalse(is_err)
        hint = result["next_suggestions"][0].get("hint", "")
        self.assertIn(str(n_before + 1), hint)

    def test_recovery_hint(self):
        """存在恢复任务时附人工提示。"""
        TaskRecord.objects.create(play=self.play, task_type=TaskType.IMAGE_GEN,
                                  status=TaskStatus.RECOVERY_REQUIRED)
        result, is_err = self._run()
        self.assertFalse(is_err)
        self.assertEqual(len(result["next_suggestions"]), 2)
        self.assertIn("恢复", result["next_suggestions"][1]["suggestion"])

    def test_no_auto_execution(self):
        """不自动执行：调用后无任何状态/任务变化（spec Scenario）。"""
        TaskRecord.objects.create(play=self.play, task_type=TaskType.IMAGE_GEN,
                                  status=TaskStatus.PENDING)
        before = (self.play.status, self.ep.status,
                  list(TaskRecord.objects.filter(play=self.play).values_list("status", flat=True)))
        _, is_err = self._run()
        self.assertFalse(is_err)
        self.play.refresh_from_db()
        self.ep.refresh_from_db()
        after = (self.play.status, self.ep.status,
                 list(TaskRecord.objects.filter(play=self.play).values_list("status", flat=True)))
        self.assertEqual(before, after)

    def test_unknown_status_graceful(self):
        """未知状态：显式无建议而非报错（自定义超集状态用 queryset 绕过校验）。"""
        Play.objects.filter(pk=self.play.pk).update(status="WEIRD_STATE")
        result, is_err = self._run()
        self.assertFalse(is_err)
        self.assertEqual(result["next_suggestions"], [])
        self.assertIn("无建议", result["note"])

    def test_in_tool_specs_and_budgeted_e2e(self):
        """工具面可见 + runner 轮内模型实际调用（计入预算）。"""
        names = [s["name"] for s in tool_specs()]
        self.assertIn("pipeline_status", names)
        self.assertIn("pipeline_next_steps", names)
        # E2E：模型第一轮调 pipeline_status，第二轮收尾
        from apps.creation.assistant.models import AssistantSession, MessageRole
        from apps.creation.assistant.runner import _save, run_assistant_turn
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.ASSISTANT_TURN,
            input_summary={"session_id": self.session.pk})
        _save(self.session, MessageRole.USER, [{"type": "text", "text": "现在到哪了"}], record)
        AssistantSession.objects.filter(pk=self.session.pk).update(running_task=record)
        gw = FakeChatGateway([
            _use("t1", "pipeline_status", {}),
            _say("当前在剧本审核暂停"),
        ])
        with patch("apps.ai_gateway.gateway.AIGateway", return_value=gw):
            run_assistant_turn(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        # 工具结果进入消息流（tool_result 无 error）
        from apps.creation.assistant.models import AssistantMessage
        tool_msgs = self.session.messages.filter(role="tool", task_record=record)
        blocks = [b for m in tool_msgs for b in (m.content or [])
                  if isinstance(b, dict) and b.get("type") == "tool_result"]
        self.assertTrue(any(not b.get("is_error") for b in blocks))
