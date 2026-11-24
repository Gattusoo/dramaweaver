"""creation views —— 任务/审核记录 + 本地执行健康与恢复操作（Stage C2）。"""
from django.utils import timezone
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.response import Response

from .base import PlayScopedViewSet
from ..models import ExecutionClaimStatus, LocalTaskExecution, ReviewLog, TaskRecord, TaskStatus
from ..serializers import ReviewLogSerializer, TaskRecordSerializer

_READ_ONLY_METHODS = ["get", "head", "options"]


class TaskRecordViewSet(PlayScopedViewSet):
    serializer_class = TaskRecordSerializer
    queryset = TaskRecord.objects.all()
    http_method_names = ["get", "head", "options", "post"]

    def create(self, request, *args, **kwargs):
        """集合级 POST 不开放（POST 仅为 detail 自定义 action 服务，既有只读契约保持）。"""
        return Response({"detail": "方法不允许"}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def update(self, request, *args, **kwargs):
        return Response({"detail": "方法不允许"}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def partial_update(self, request, *args, **kwargs):
        return Response({"detail": "方法不允许"}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def destroy(self, request, *args, **kwargs):
        return Response({"detail": "方法不允许"}, status=status.HTTP_405_METHOD_NOT_ALLOWED)

    def _load_record(self, request, pk=None):
        record = self.get_object()  # PlayScoped 已按归属隔离
        from ..permissions import can_write_play
        if not can_write_play(request.user, record.play):
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("无权操作该短剧的任务")
        return record

    @action(detail=True, methods=["post"], url_path="issue-recovery-token")
    def issue_recovery_token(self, request, pk=None):
        """签发一次性恢复令牌（任务 7.2）：仅 RECOVERY_REQUIRED 记录。
        权限：edit（剧内写成员）。"""
        record = self._load_record(request, pk)
        from ..pipeline.recovery import RecoveryError, issue_recovery_token
        try:
            return Response(issue_recovery_token(record))
        except RecoveryError as exc:
            code = getattr(exc, "status_code", 400)
            return Response({"detail": str(exc)}, status=code)

    @action(detail=True, methods=["post"], url_path="confirm-recovery")
    def confirm_recovery(self, request, pk=None):
        """确认恢复重试（任务 7.2）：令牌换关联新执行，原记录保留审计。"""
        record = self._load_record(request, pk)
        from ..pipeline.recovery import RecoveryError, confirm_recovery
        token = (request.data or {}).get("recovery_token", "")
        try:
            return Response(confirm_recovery(record, token, request.user))
        except RecoveryError as exc:
            code = getattr(exc, "status_code", 400)
            return Response({"detail": str(exc)}, status=code)

    @action(detail=True, methods=["post"])
    def cancel(self, request, pk=None):
        """取消任务（任务 7.4）：PENDING/RETRYING/RECOVERY_REQUIRED → CANCELLED；
        RUNNING 置取消标记（渲染中由 _run 捕获），不复活。"""
        record = self._load_record(request, pk)
        if record.status in (TaskStatus.SUCCESS, TaskStatus.CANCELLED):
            return Response({"detail": f"任务已终态（{record.status}），无需取消"},
                            status=status.HTTP_400_BAD_REQUEST)
        now = timezone.now()
        if record.status == TaskStatus.RUNNING:
            # 渲染中：置 CANCELLED 由 _run 的条件更新尊重（不覆盖取消）
            updated = TaskRecord.objects.filter(
                pk=record.pk, status=TaskStatus.RUNNING
            ).update(status=TaskStatus.CANCELLED, completed_at=now)
        else:
            updated = TaskRecord.objects.filter(
                pk=record.pk,
                status__in=(TaskStatus.PENDING, TaskStatus.RETRYING,
                            TaskStatus.RECOVERY_REQUIRED),
            ).update(status=TaskStatus.CANCELLED, completed_at=now)
        # 本地执行行同步终结（不再被领取）
        LocalTaskExecution.objects.filter(record=record).update(
            claim_status=ExecutionClaimStatus.DONE)
        return Response({"cancelled": bool(updated), "task_id": record.pk})

    @action(detail=True, methods=["post"], url_path="retry-failed")
    def retry_failed(self, request, pk=None):
        """失败重派（任务 7.4）：FAILED → 新 PENDING 关联执行（审计链保留）。
        RECOVERY_REQUIRED 不走此入口（须先确认恢复）。"""
        record = self._load_record(request, pk)
        if record.status != TaskStatus.FAILED:
            return Response({"detail": f"仅失败任务可重派（当前 {record.status}）"},
                            status=status.HTTP_400_BAD_REQUEST)
        from ..pipeline.local_backend import send_task
        from ..pipeline.dispatcher import TASK_NAME_MAP
        task_name = TASK_NAME_MAP.get(record.task_type)
        if not task_name:
            return Response({"detail": "未知任务类型，无法重派"},
                            status=status.HTTP_400_BAD_REQUEST)
        new_record = TaskRecord.objects.create(
            play=record.play, episode=record.episode, scene=record.scene,
            shot=record.shot, task_type=record.task_type,
            status=TaskStatus.PENDING,
            input_summary=dict(record.input_summary or {}, retry_of=record.pk))
        result = send_task(task_name, args=[new_record.pk])
        if result is None:
            new_record.status = TaskStatus.FAILED
            new_record.error_message = "重派发送失败（后端不可用）"
            new_record.save(update_fields=["status", "error_message"])
            return Response({"detail": "重派发送失败（后端不可用）"},
                            status=status.HTTP_503_SERVICE_UNAVAILABLE)
        return Response({"retried": True, "new_task_id": new_record.pk})


class ReviewLogViewSet(PlayScopedViewSet):
    serializer_class = ReviewLogSerializer
    queryset = ReviewLog.objects.all()
    http_method_names = _READ_ONLY_METHODS
