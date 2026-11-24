# -*- coding: utf-8 -*-
"""计费派发闸接线测试（openspec change: backend-billing-wiring）。

覆盖：dispatch_task 闸通过/槽位满/余额不足/估算零预扣/每日渲染超限/
幂等重放、三终态对账（settle 差额/refund/取消）、助手轮次 402/409、
闸旁路开关与 local 短路、限流租约（见 test_redis_store/test_ai_gateway）。
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase, override_settings

from apps.ai_gateway.models import AIModel, AIProvider
from apps.ai_gateway.redis_client import MemoryStore, reset_store
from apps.billing.models import Wallet, WalletEntry
from apps.billing.services import gate as gate_mod
from apps.billing.services.quotas import consume_daily_render
from apps.billing.services.wallet import grant
from apps.creation.models import (
    AgentConfig, AuditMode, Play, TaskRecord, TaskStatus, TaskType,
)
from apps.creation.pipeline import dispatcher
from apps.core.system.models import CustomUser, UserRole

from .test_assistant import AssistantFixtureMixin, _say, _use


def _reset_billing_store():
    """槽位/日累计 key 跨测试泄漏——每用例前清内存 store（对齐 billing 测试惯例）。"""
    reset_store()
    store = MemoryStore()
    return store


class BillingWiringFixture(TestCase):
    def setUp(self):
        super().setUp()
        self.owner = CustomUser.objects.create_user(
            username="bwo", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.provider = AIProvider.objects.create(
            name="P", provider_type="LLM", adapter_type="deepseek",
            base_url="https://x", api_key_encrypted="", priority=1)
        self.model = AIModel.objects.create(
            provider=self.provider, model_name="m", display_name="M", model_type="LLM")
        self.config = AgentConfig.objects.create(
            name="配方", llm_model=self.model, created_by=self.owner)
        self.play = Play.objects.create(
            title="接线剧", agent_config=self.config, created_by=self.owner,
            audit_mode=AuditMode.NONE)
        grant(self.owner, 100000, ref_kind="test", ref_id=f"seed-{id(self)}")
        # 槽位/日累计 store 跨测试泄漏——每用例换独立 MemoryStore
        from apps.ai_gateway import redis_client
        self._store = MemoryStore()
        patcher = patch.object(redis_client, "_store", self._store)
        patcher.start()
        self.addCleanup(patcher.stop)


class DispatchGateWiringTests(BillingWiringFixture):
    """任务 2.1/2.2：dispatch_task 插闸。"""

    def _dispatch(self, task_type=TaskType.OUTLINE):
        fake_result = MagicMock()
        fake_result.id = "fake-uuid"
        with patch.object(dispatcher, "send_task", return_value=fake_result) as fake_send:
            record = dispatcher.dispatch_task(task_type, play=self.play)
        return record, fake_send

    def test_gate_pass_dispatches(self):
        """闸通过：PENDING 派发、消息发出。"""
        record, fake_send = self._dispatch()
        self.assertEqual(record.status, TaskStatus.PENDING)
        fake_send.assert_called_once()

    def test_slot_full_marks_failed(self):
        """AI 槽位满 → FAILED + error_message，消息不发出。"""
        from apps.billing.services.quotas import acquire_slot
        for _ in range(10):  # 占满默认 ai=10（不释放，模拟持续满载）
            acquire_slot(self.owner, "ai")
        record, fake_send = self._dispatch()
        self.assertEqual(record.status, TaskStatus.FAILED)
        self.assertIn("槽位", record.error_message)
        fake_send.assert_not_called()

    def test_insufficient_balance_marks_failed(self):
        """余额不足 → FAILED + error_message，消息不发出。"""
        wallet = Wallet.objects.get(user=self.owner)
        Wallet.objects.filter(pk=wallet.pk).update(balance=0)
        WalletEntry.objects.filter(wallet=wallet).delete()
        record, fake_send = self._dispatch()
        self.assertEqual(record.status, TaskStatus.FAILED)
        self.assertIn("余额不足", record.error_message)
        fake_send.assert_not_called()

    def test_estimate_failure_zero_hold_dispatches(self):
        """计价缺失 → 0 预扣照常派发（不阻断创作）。"""
        with patch("apps.creation.assistant.cost_estimator.estimate_task_cost",
                   side_effect=Exception("no price")):
            record, fake_send = self._dispatch()
        self.assertEqual(record.status, TaskStatus.PENDING)
        fake_send.assert_called_once()
        self.assertNotIn("billing_hold", record.input_summary)

    def test_hold_written_to_input_summary(self):
        """闸通过且估算>0 → billing_hold 写入 input_summary。"""
        record, _ = self._dispatch()
        hold = record.input_summary.get("billing_hold")
        self.assertIsNotNone(hold)
        self.assertIn("entry_id", hold)
        # hold 流水真实存在且 ACTIVE
        entry = WalletEntry.objects.get(pk=hold["entry_id"])
        self.assertEqual(entry.type, "hold")
        self.assertEqual(entry.status, "ACTIVE")

    def test_hold_idempotent_on_redispatch(self):
        """同 ref 重复派发 → 幂等复用既有 hold（不重复预扣）。"""
        record, _ = self._dispatch()
        balance_after_first = Wallet.objects.get(user=self.owner).balance
        # 重复闸同一 record（模拟重放）
        ok = dispatcher._billing_gate(record, TaskType.OUTLINE, self.play)
        self.assertTrue(ok)
        self.assertEqual(Wallet.objects.get(user=self.owner).balance, balance_after_first)

    def test_daily_render_limit_marks_failed(self):
        """渲染类：当日时长超限 → FAILED（4029 语义）。"""
        consume_daily_render(self.owner.pk, 3600)  # 默认限额 3600s
        record, fake_send = self._dispatch(TaskType.VIDEO_COMPOSE)
        self.assertEqual(record.status, TaskStatus.FAILED)
        self.assertIn("渲染", record.error_message)
        fake_send.assert_not_called()

    def test_publish_takes_no_slot(self):
        """PUBLISH 不占槽位（外部动作非算力消费）。"""
        self.assertNotIn(TaskType.PUBLISH, dispatcher._SLOT_KINDS)
        record, fake_send = self._dispatch(TaskType.PUBLISH)
        self.assertEqual(record.status, TaskStatus.PENDING)
        fake_send.assert_called_once()


class TerminalSettleTests(BillingWiringFixture):
    """任务 2.4：三终态对账（独立于派发闸用例，自建带 hold 的记录）。"""

    def _record_with_hold(self):
        fake_result = MagicMock()
        fake_result.id = "fake-uuid"
        with patch.object(dispatcher, "send_task", return_value=fake_result):
            record = dispatcher.dispatch_task(TaskType.OUTLINE, play=self.play)
        hold = record.input_summary["billing_hold"]
        entry = WalletEntry.objects.get(pk=hold["entry_id"])
        return record, entry

    def test_success_settles_difference(self):
        """成功：按实际费用结算，差额退回。"""
        record, entry = self._record_with_hold()
        held = -entry.amount
        actual = held - 30  # 实际低于预扣 → 退差额
        from apps.billing.services.gate import TaskOutcome
        TaskOutcome.success(_shim(entry), actual, note="test")
        entry.refresh_from_db()
        self.assertEqual(entry.status, "SETTLED")

    def test_failed_refunds_full(self):
        """失败：整额退回。"""
        record, entry = self._record_with_hold()
        held = -entry.amount
        balance_before = Wallet.objects.get(user=self.owner).balance
        dispatcher._billing_settle(_fake_record(record, TaskStatus.FAILED))
        entry.refresh_from_db()
        self.assertEqual(entry.status, "RELEASED")
        self.assertEqual(Wallet.objects.get(user=self.owner).balance,
                         balance_before + held)

    def test_cancelled_refunds_full(self):
        """取消：整额退回（_mark_cancelled 路径）。"""
        from apps.creation import tasks as tasks_mod
        record, entry = self._record_with_hold()
        held = -entry.amount
        balance_before = Wallet.objects.get(user=self.owner).balance
        tasks_mod._mark_cancelled(record)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "RELEASED")
        self.assertEqual(Wallet.objects.get(user=self.owner).balance,
                         balance_before + held)

    def test_finish_success_settles(self):
        """_mark_success → 对账（settle 按实际 cost）。"""
        from apps.creation import tasks as tasks_mod
        record, entry = self._record_with_hold()
        record.cost = "0.01"  # 实际 ¥0.01
        record.save(update_fields=["cost"])
        tasks_mod._mark_success(record)
        entry.refresh_from_db()
        self.assertEqual(entry.status, "SETTLED")

    def test_no_hold_noop(self):
        """无 billing_hold 的记录对账 no-op（闸旁路/估算 0）。"""
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.OUTLINE, status=TaskStatus.SUCCESS,
            input_summary={})
        dispatcher._billing_settle(record)  # 不应抛错

    def test_missing_entry_skips(self):
        """billing_hold 指向不存在的 entry → 跳过不抛错。"""
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.OUTLINE, status=TaskStatus.FAILED,
            input_summary={"billing_hold": {"entry_id": 999999, "amount": 100}})
        dispatcher._billing_settle(record)


class _Shim:
    def __init__(self, entry):
        self.entry = entry


def _shim(entry):
    return _Shim(entry)


def _fake_record(record, status):
    """构造对账用的记录视图（保持 input_summary 引用）。"""
    record.status = status
    return record


class AssistantGateTests(AssistantFixtureMixin, TestCase):
    """任务 2.5：助手轮次接线（402/409 语义）。

    fixture 已 patch dispatcher.send_task（消息不会真发）。
    """

    def setUp(self):
        super().setUp()
        from apps.ai_gateway import redis_client
        self._store = MemoryStore()
        patcher = patch.object(redis_client, "_store", self._store)
        patcher.start()
        self.addCleanup(patcher.stop)
        # 消息级 RPM 限流与本测试无关（fixture 用户被全套件复用会撞 429）
        throttle_patcher = patch("apps.creation.assistant.views._throttle", return_value=True)
        throttle_patcher.start()
        self.addCleanup(throttle_patcher.stop)
        grant(self.producer, 100000, ref_kind="test", ref_id=f"ag-{id(self)}")

    def _post_message(self):
        from rest_framework.test import APIClient
        client = APIClient()
        client.force_authenticate(self.producer)
        return client.post(f"/api/assistant/sessions/{self.session.pk}/messages/",
                           {"text": "继续"}, format="json")

    def test_gate_pass_returns_202(self):
        resp = self._post_message()
        self.assertEqual(resp.status_code, 202, resp.data)

    def test_insufficient_points_returns_402(self):
        wallet = Wallet.objects.get(user=self.producer)
        Wallet.objects.filter(pk=wallet.pk).update(balance=0)
        WalletEntry.objects.filter(wallet=wallet).delete()
        resp = self._post_message()
        self.assertEqual(resp.status_code, 402, resp.data)
        self.assertEqual(resp.data.get("reason"), "insufficient_points")

    def test_slot_full_returns_409(self):
        from apps.billing.services.quotas import acquire_slot
        for _ in range(10):  # 占满默认 ai=10
            acquire_slot(self.producer, "ai")
        resp = self._post_message()
        self.assertEqual(resp.status_code, 409, resp.data)
        self.assertEqual(resp.data.get("reason"), "quota_exceeded")

    @override_settings(ENABLE_BILLING_HOLD=False)
    def test_gate_bypass(self):
        """ENABLE_BILLING_HOLD=false → 旁路直接派发。"""
        Wallet.objects.filter(user=self.producer).update(balance=0)
        resp = self._post_message()
        self.assertEqual(resp.status_code, 202, resp.data)


class LocalModeBypassTests(BillingWiringFixture):
    """local 模式短路（工作台 C1 决议不回归验证，只确认闸自身短路）。"""

    def test_local_mode_gate_returns_none(self):
        with override_settings(IS_LOCAL_MODE=True):
            fake_result = MagicMock()
            fake_result.id = "fake-uuid"
            with patch.object(dispatcher, "send_task", return_value=fake_result) as fake_send:
                record = dispatcher.dispatch_task(TaskType.OUTLINE, play=self.play)
            self.assertEqual(record.status, TaskStatus.PENDING)
            fake_send.assert_called_once()
            self.assertNotIn("billing_hold", record.input_summary)
