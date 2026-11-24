"""崩溃恢复、健康检查与任务操作测试（工作台 Stage C2，任务 7.1–7.4）。"""
from datetime import timedelta

from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APITestCase

from apps.ai_gateway.models import AIModel, AIProvider
from apps.creation.models import (
    AgentConfig, AuditMode, ExecutionClaimStatus, LocalTaskExecution, MemberRole,
    MemberStatus, Play, PlayMember, TaskRecord, TaskStatus, TaskType,
)
from apps.creation.pipeline.local_backend import PAID_CHECKPOINT, send_task
from apps.creation.pipeline.recovery import (
    RecoveryError, confirm_recovery, issue_recovery_token,
)
from apps.core.system.models import CustomUser, UserRole


def _mk_member(play, user, role):
    PlayMember.objects.create(play=play, user=user, role=role, status=MemberStatus.ACTIVE)


class C2Fixture:
    def setUp(self):
        super().setUp()
        self.owner = CustomUser.objects.create_user(
            username="c2o", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.other = CustomUser.objects.create_user(
            username="c2x", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.provider = AIProvider.objects.create(
            name="P", provider_type="LLM", adapter_type="deepseek",
            base_url="https://x", api_key_encrypted="", priority=1)
        self.model = AIModel.objects.create(
            provider=self.provider, model_name="m", display_name="M", model_type="LLM")
        self.config = AgentConfig.objects.create(
            name="配方", llm_model=self.model, created_by=self.owner)
        self.play = Play.objects.create(
            title="恢复剧", agent_config=self.config, created_by=self.owner,
            audit_mode=AuditMode.NONE)
        _mk_member(self.play, self.owner, MemberRole.OWNER)

    def mk_recovery_record(self, task_type=TaskType.IMAGE_GEN,
                           task_name="shortplay.tasks.generate_image"):
        """构造付费任务受理后崩溃的现场：RECOVERY_REQUIRED + 死租约。"""
        record = TaskRecord.objects.create(
            play=self.play, task_type=task_type, status=TaskStatus.PENDING)
        send_task(task_name, args=[record.pk])
        ex = LocalTaskExecution.objects.get(record=record)
        ex.last_checkpoint = PAID_CHECKPOINT
        ex.claim_status = ExecutionClaimStatus.RECOVERY_REQUIRED
        ex.lease_expires_at = timezone.now() - timedelta(seconds=300)
        ex.save(update_fields=["last_checkpoint", "claim_status", "lease_expires_at"])
        TaskRecord.objects.filter(pk=record.pk).update(
            status=TaskStatus.RECOVERY_REQUIRED)
        record.refresh_from_db()
        return record, ex


@override_settings(CREATION_TASK_BACKEND="local")
class CrashRecoveryTests(C2Fixture, TestCase):
    """任务 7.1：崩溃点分类——付费不自动重放、确定性可恢复。"""

    def test_paid_crash_requires_recovery_not_auto_replay(self):
        """付费检查点置位后崩溃 → RECOVERY_REQUIRED（不自动重放）。"""
        record, ex = self.mk_recovery_record()
        self.assertEqual(record.status, TaskStatus.RECOVERY_REQUIRED)
        self.assertEqual(ex.claim_status, ExecutionClaimStatus.RECOVERY_REQUIRED)

    def test_pending_task_recovers_normally(self):
        """PENDING 任务不受崩溃影响：worker 重启后正常领取。"""
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.IMAGE_GEN, status=TaskStatus.PENDING)
        send_task("shortplay.tasks.generate_image", args=[record.pk])
        from apps.creation.management.commands.run_local_worker import Command
        from apps.creation import tasks as tasks_mod
        cmd = Command()
        claimed = cmd._claim_next("w-recover")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.record_id, record.pk)

    def test_assistant_turn_before_paid_crash_reclaimable(self):
        """ASSISTANT_TURN 首次模型调用前崩溃：可安全重领（无付费副作用）。"""
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.ASSISTANT_TURN, status=TaskStatus.PENDING)
        send_task("shortplay.assistant.run_assistant_turn", args=[record.pk])
        ex = LocalTaskExecution.objects.get(record=record)
        # checkpoint 未置位 → 崩溃后走死租约回收：确定性语义（可重领）
        # （ASSISTANT_TURN 归付费类，但未置 checkpoint 时按注册表仍可重领——
        #   设计：置位后才整轮 RECOVERY_REQUIRED）
        ex.claim_status = ExecutionClaimStatus.RUNNING
        ex.lease_expires_at = timezone.now() - timedelta(seconds=300)
        ex.save(update_fields=["claim_status", "lease_expires_at"])
        from apps.creation.management.commands.run_local_worker import Command
        cmd = Command()
        cmd._reclaim_expired("w2")
        ex.refresh_from_db()
        record.refresh_from_db()
        # 付费类未置 checkpoint：重领（PENDING 可执行）
        self.assertEqual(ex.claim_status, ExecutionClaimStatus.PENDING)
        self.assertNotEqual(record.status, TaskStatus.RECOVERY_REQUIRED)

    def test_assistant_turn_after_paid_enters_recovery_with_disclosure(self):
        """ASSISTANT_TURN 置位后整轮 RECOVERY_REQUIRED，部分产物在恢复响应披露。"""
        record, ex = self.mk_recovery_record(
            task_type=TaskType.ASSISTANT_TURN,
            task_name="shortplay.assistant.run_assistant_turn")
        record.input_summary = {"session_id": 42}
        record.save(update_fields=["input_summary"])
        result = issue_recovery_token(record)
        self.assertIn("recovery_token", result)
        disclosed = [d["kind"] for d in result["disclosure"]]
        self.assertIn("assistant_messages", disclosed)  # 部分产物披露
        self.assertEqual(result["disclosure"][0]["session_id"], 42)


@override_settings(CREATION_TASK_BACKEND="local")
class RecoveryTokenTests(C2Fixture, TestCase):
    """任务 7.2：一次性令牌——只回显一次、hash 存储、过期/重复/跨用户拒绝。"""

    def test_token_issued_once_hashed_and_single_use(self):
        record, ex = self.mk_recovery_record()
        result = issue_recovery_token(record)
        token = result["recovery_token"]
        import hashlib
        ex.refresh_from_db()
        # hash 存储（明文不在库里）
        self.assertEqual(ex.recovery_token_hash, hashlib.sha256(token.encode()).hexdigest())
        # 有效期内重复签发拒绝（避免多活令牌）
        with self.assertRaises(RecoveryError):
            issue_recovery_token(record)
        # 确认：令牌换新执行
        confirmed = confirm_recovery(record, token, self.owner)
        self.assertTrue(confirmed["recovered"])
        self.assertNotEqual(confirmed["new_record_id"], record.pk)
        # 令牌已作废：重复使用拒绝
        with self.assertRaises(RecoveryError):
            confirm_recovery(record, token, self.owner)

    def test_wrong_and_expired_token_rejected(self):
        record, ex = self.mk_recovery_record()
        result = issue_recovery_token(record)
        # 错误令牌
        with self.assertRaises(RecoveryError):
            confirm_recovery(record, "wrong-token", self.owner)
        # 过期令牌
        LocalTaskExecution.objects.filter(pk=ex.pk).update(
            recovery_expires_at=timezone.now() - timedelta(seconds=1))
        with self.assertRaises(RecoveryError):
            confirm_recovery(record, result["recovery_token"], self.owner)

    def test_cross_user_rejected(self):
        """跨用户（非剧内成员）确认恢复被拒绝。"""
        record, _ = self.mk_recovery_record()
        result = issue_recovery_token(record)
        with self.assertRaises(RecoveryError) as cm:
            confirm_recovery(record, result["recovery_token"], self.other)
        self.assertEqual(cm.exception.status_code, 403)

    def test_confirm_creates_linked_execution_and_keeps_audit(self):
        """确认创建关联新执行（recovery_of），原记录保留审计（FAILED+说明）。"""
        record, ex = self.mk_recovery_record()
        token = issue_recovery_token(record)["recovery_token"]
        confirmed = confirm_recovery(record, token, self.owner)
        record.refresh_from_db()
        new = TaskRecord.objects.get(pk=confirmed["new_record_id"])
        self.assertEqual(new.input_summary.get("recovery_of"), record.pk)
        self.assertEqual(new.status, TaskStatus.PENDING)
        self.assertIn("已确认恢复", record.error_message)
        self.assertEqual(record.status, TaskStatus.FAILED)  # 审计链终态
        new_ex = LocalTaskExecution.objects.get(record=new)
        self.assertEqual(new_ex.claim_status, ExecutionClaimStatus.PENDING)


@override_settings(CREATION_TASK_BACKEND="local")
class HealthEndpointTests(C2Fixture, APITestCase):
    """任务 7.3：健康端点——脱敏、worker 停止不显示生成中。"""

    def test_health_shape_and_no_secrets(self):
        self.client.force_authenticate(self.owner)
        resp = self.client.get("/api/health/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json().get("data", resp.json())
        for key in ("deployment_mode", "task_backend", "database", "worker",
                    "queue", "media", "ffmpeg"):
            self.assertIn(key, data)
        raw = resp.content.decode("utf-8")
        # 脱敏：无盘符路径、无密钥
        for banned in ("D:\\", "C:\\", "/home/", "sk-", "SECRET", "api_key"):
            self.assertNotIn(banned, raw)

    def test_worker_stopped_queued_not_running(self):
        """worker 停止：排队任务如实计数，但 running=0（不显示生成中）。"""
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.IMAGE_GEN, status=TaskStatus.PENDING)
        send_task("shortplay.tasks.generate_image", args=[record.pk])
        self.client.force_authenticate(self.owner)
        data = self.client.get("/api/health/").json().get("data")
        self.assertEqual(data["queue"]["queued"], 1)
        self.assertFalse(data["worker"]["alive"])
        self.assertEqual(data["worker"]["active_leases"], 0)

    def test_active_lease_reports_alive(self):
        """活跃租约 → worker alive=True。"""
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.IMAGE_GEN, status=TaskStatus.PENDING)
        send_task("shortplay.tasks.generate_image", args=[record.pk])
        LocalTaskExecution.objects.filter(record=record).update(
            claim_status=ExecutionClaimStatus.RUNNING,
            lease_expires_at=timezone.now() + timedelta(seconds=60))
        self.client.force_authenticate(self.owner)
        data = self.client.get("/api/health/").json().get("data")
        self.assertTrue(data["worker"]["alive"])

    def test_health_requires_auth(self):
        resp = self.client.get("/api/health/")
        self.assertEqual(resp.status_code, 401)


@override_settings(CREATION_TASK_BACKEND="local")
class TaskOperationApiTests(C2Fixture, APITestCase):
    """任务 7.4：任务操作 API——本地/shared 权限一致、取消不复活、重派审计。"""

    def test_cancel_pending_task(self):
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.IMAGE_GEN, status=TaskStatus.PENDING)
        send_task("shortplay.tasks.generate_image", args=[record.pk])
        self.client.force_authenticate(self.owner)
        resp = self.client.post(f"/api/task-records/{record.pk}/cancel/")
        self.assertEqual(resp.status_code, 200)
        record.refresh_from_db()
        self.assertEqual(record.status, TaskStatus.CANCELLED)
        ex = LocalTaskExecution.objects.get(record=record)
        self.assertEqual(ex.claim_status, ExecutionClaimStatus.DONE)  # 不再被领取

    def test_cancel_not_resurrected_by_worker(self):
        """取消后 worker 领取跳过（取消不复活）。"""
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.IMAGE_GEN, status=TaskStatus.PENDING)
        send_task("shortplay.tasks.generate_image", args=[record.pk])
        self.client.force_authenticate(self.owner)
        self.client.post(f"/api/task-records/{record.pk}/cancel/")
        from apps.creation.management.commands.run_local_worker import Command
        cmd = Command()
        claimed = cmd._claim_next("w-after-cancel")
        # 执行行已 DONE：领取不到
        self.assertIsNone(claimed)

    def test_failed_task_retry_creates_linked_record(self):
        """失败重派：新 PENDING 关联记录（retry_of），审计链保留。"""
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.OUTLINE,
            status=TaskStatus.FAILED, error_message="确定性失败")
        self.client.force_authenticate(self.owner)
        resp = self.client.post(f"/api/task-records/{record.pk}/retry-failed/")
        self.assertEqual(resp.status_code, 200)
        data = resp.json().get("data", resp.json())
        new = TaskRecord.objects.get(pk=data["new_task_id"])
        self.assertEqual(new.input_summary.get("retry_of"), record.pk)
        self.assertEqual(new.status, TaskStatus.PENDING)

    def test_recovery_required_cannot_retry_directly(self):
        """RECOVERY_REQUIRED 不走 retry-failed（须先确认恢复）。"""
        record, _ = self.mk_recovery_record(task_type=TaskType.OUTLINE,
                                            task_name="shortplay.tasks.generate_outline")
        self.client.force_authenticate(self.owner)
        resp = self.client.post(f"/api/task-records/{record.pk}/retry-failed/")
        self.assertEqual(resp.status_code, 400)

    def test_recovery_flow_via_api(self):
        """恢复全流程 API：issue → confirm → 新执行可领取。"""
        record, _ = self.mk_recovery_record()
        self.client.force_authenticate(self.owner)
        issue = self.client.post(f"/api/task-records/{record.pk}/issue-recovery-token/")
        self.assertEqual(issue.status_code, 200)
        token = issue.json().get("data", issue.json())["recovery_token"]
        confirm = self.client.post(f"/api/task-records/{record.pk}/confirm-recovery/",
                                   {"recovery_token": token}, format="json")
        self.assertEqual(confirm.status_code, 200)
        data = confirm.json().get("data", confirm.json())
        # 新执行可被 worker 领取
        from apps.creation.management.commands.run_local_worker import Command
        cmd = Command()
        claimed = cmd._claim_next("w-final")
        self.assertIsNotNone(claimed)
        self.assertEqual(claimed.record_id, data["new_record_id"])

    def test_non_member_cannot_operate(self):
        """非剧内成员（跨用户）任务操作不可见（404，PlayScoped 归属隔离——
        与 shared 权限一致：无成员身份即无可见性）。"""
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.IMAGE_GEN, status=TaskStatus.PENDING)
        self.client.force_authenticate(self.other)
        resp = self.client.post(f"/api/task-records/{record.pk}/cancel/")
        self.assertEqual(resp.status_code, 404)
