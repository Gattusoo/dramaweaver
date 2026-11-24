"""任务与审核记录：TaskRecord / ReviewLog / LocalTaskExecution。"""
import uuid as _uuid

from django.conf import settings
from django.db import models


class TaskType(models.TextChoices):
    OUTLINE = "OUTLINE", "大纲生成"
    EPISODE_SCRIPT = "EPISODE_SCRIPT", "分集剧本生成"
    STORYBOARD = "STORYBOARD", "分镜生成"
    CHARACTER_REF = "CHARACTER_REF", "角色定妆生成"
    IMAGE_GEN = "IMAGE_GEN", "图片生成"
    TTS_GEN = "TTS_GEN", "语音合成"
    BGM_SELECT = "BGM_SELECT", "BGM选择"
    VIDEO_COMPOSE = "VIDEO_COMPOSE", "视频合成"
    PUBLISH = "PUBLISH", "发布"
    RENDITION = "RENDITION", "渠道版本转码"
    PREVIEW_RENDER = "PREVIEW_RENDER", "预览渲染"
    DERIVATIVES = "DERIVATIVES", "衍生品生成"
    ASSISTANT_TURN = "ASSISTANT_TURN", "创作助手对话"
    SUBTITLE_GEN = "SUBTITLE_GEN", "字幕生成"
    NOVEL_ADAPT = "NOVEL_ADAPT", "小说改编"
    TIMELINE_EXPORT = "TIMELINE_EXPORT", "剪辑工程导出"


class TaskStatus(models.TextChoices):
    PENDING = "PENDING", "待执行"
    RUNNING = "RUNNING", "执行中"
    SUCCESS = "SUCCESS", "成功"
    FAILED = "FAILED", "失败"
    RETRYING = "RETRYING", "重试中"
    CANCELLED = "CANCELLED", "已取消"
    # 工作台 Stage C1（design D2）：本地执行付费请求已受理后崩溃——不自动重放，
    # 等用户持一次性恢复令牌确认后创建关联新执行。
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED", "待恢复确认"


class ReviewPoint(models.TextChoices):
    OUTLINE = "OUTLINE", "大纲"
    EPISODE_SCRIPT = "EPISODE_SCRIPT", "分集剧本"
    CHARACTER_REF = "CHARACTER_REF", "角色定妆"
    ASSETS = "ASSETS", "素材"
    COMPLETED = "COMPLETED", "完成"


class ReviewDecision(models.TextChoices):
    APPROVED = "APPROVED", "通过"
    REJECTED = "REJECTED", "拒绝"
    MODIFIED = "MODIFIED", "修改"


class TaskRecord(models.Model):
    play = models.ForeignKey(
        "Play", on_delete=models.CASCADE, related_name="task_records"
    )
    episode = models.ForeignKey(
        "Episode", on_delete=models.SET_NULL, null=True, blank=True
    )
    scene = models.ForeignKey(
        "Scene", on_delete=models.SET_NULL, null=True, blank=True
    )
    shot = models.ForeignKey(
        "Shot", on_delete=models.SET_NULL, null=True, blank=True
    )
    asset = models.ForeignKey(
        "Asset", on_delete=models.SET_NULL, null=True, blank=True
    )
    task_type = models.CharField("任务类型", max_length=30, choices=TaskType.choices)
    celery_task_id = models.CharField("Celery任务ID", max_length=200, blank=True)
    status = models.CharField(
        "状态", max_length=20, choices=TaskStatus.choices, default=TaskStatus.PENDING
    )
    ai_provider = models.ForeignKey(
        "ai_gateway.AIProvider", on_delete=models.SET_NULL, null=True, blank=True
    )
    ai_model = models.CharField("使用的模型名", max_length=200, blank=True)
    input_summary = models.JSONField("输入摘要", default=dict)
    output_summary = models.JSONField("输出摘要", default=dict)
    error_message = models.TextField("错误信息", blank=True)
    retry_count = models.PositiveIntegerField("重试次数", default=0)
    cost = models.DecimalField("费用", max_digits=10, decimal_places=6, default=0)
    started_at = models.DateTimeField("开始时间", null=True, blank=True)
    completed_at = models.DateTimeField("完成时间", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_taskrecord"
        indexes = [
            models.Index(fields=["play", "status"], name="idx_task_play_status"),
            models.Index(fields=["celery_task_id"], name="idx_task_celery_id"),
        ]
        ordering = ["-created_at"]
        verbose_name = "任务记录"
        verbose_name_plural = "任务记录"

    def __str__(self):
        return f"[{self.task_type}] {self.play} - {self.status}"


class ReviewLog(models.Model):
    play = models.ForeignKey(
        "Play", on_delete=models.CASCADE, related_name="review_logs"
    )
    review_point = models.CharField(
        "审核节点", max_length=30, choices=ReviewPoint.choices
    )
    episode = models.ForeignKey(
        "Episode", on_delete=models.SET_NULL, null=True, blank=True
    )
    reviewer = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="review_logs"
    )
    decision = models.CharField(
        "审核结论", max_length=20, choices=ReviewDecision.choices
    )
    comment = models.TextField("审核意见", blank=True)
    modifications = models.JSONField("修改内容", default=dict)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_reviewlog"
        ordering = ["-created_at"]
        verbose_name = "审核记录"
        verbose_name_plural = "审核记录"

    def __str__(self):
        return f"{self.play} - {self.review_point} - {self.decision}"


class ExecutionClaimStatus(models.TextChoices):
    """本地执行领取状态（LocalTaskExecution.claim_status）。"""
    PENDING = "PENDING", "待领取"
    RUNNING = "RUNNING", "执行中"
    RECOVERY_REQUIRED = "RECOVERY_REQUIRED", "待恢复确认"
    DONE = "DONE", "已终结"


class LocalTaskExecution(models.Model):
    """本地持久化执行记录（工作台 Stage C1，design D2）。

    one-to-one TaskRecord（业务状态仍以 TaskRecord 为准，本表只管领取与恢复）：
    - uuid：执行身份（send_task 返回给 dispatcher 的"真实 UUID"）；
    - payload：task_name + args/kwargs（事务提交后写入）；
    - claim_status / lease_worker_id / lease_expires_at：事务条件领取 + 租约，
      同库单活跃 worker 靠租约记录防重复领取；
    - attempt / next_attempt_at：瞬时故障的本地有界重试（确定性任务）；
    - last_checkpoint：付费请求前置位 paid_request_started（崩溃分类依据）；
    - recovery_token_hash：一次性恢复令牌（只存 hash，明文仅回显一次）。
    """
    uuid = models.UUIDField("执行UUID", default=_uuid.uuid4, unique=True, editable=False)
    record = models.OneToOneField(
        TaskRecord, on_delete=models.CASCADE, related_name="local_execution")
    task_name = models.CharField("任务名", max_length=200)
    args = models.JSONField("位置参数", default=list)
    kwargs = models.JSONField("关键字参数", default=dict)
    claim_status = models.CharField(
        "领取状态", max_length=20, choices=ExecutionClaimStatus.choices,
        default=ExecutionClaimStatus.PENDING)
    lease_worker_id = models.CharField("租约worker", max_length=100, blank=True)
    lease_expires_at = models.DateTimeField("租约到期", null=True, blank=True)
    attempt = models.PositiveIntegerField("尝试次数", default=0)
    next_attempt_at = models.DateTimeField("下次可执行", null=True, blank=True)
    last_checkpoint = models.CharField("检查点", max_length=50, blank=True)
    recovery_token_hash = models.CharField("恢复令牌哈希", max_length=64, blank=True)
    # 令牌有效期（一次性：确认/过期即作废，不重放）
    recovery_expires_at = models.DateTimeField("恢复令牌到期", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_local_task_execution"
        indexes = [
            models.Index(fields=["claim_status", "next_attempt_at"],
                         name="idx_lte_claim_next"),
        ]
        verbose_name = "本地任务执行"
        verbose_name_plural = "本地任务执行"

    def __str__(self):
        return f"{self.task_name} {self.uuid}"
