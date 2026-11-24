"""distribution models：SubtitleTrack / DistributionChannel / Publication。"""
from django.db import models


class SubtitleSourceType(models.TextChoices):
    AUTO = "AUTO", "自动从TTS对白生成"
    MANUAL = "MANUAL", "人工编写"
    TRANSLATION = "TRANSLATION", "LLM翻译"


class SubtitleStatus(models.TextChoices):
    PENDING = "PENDING", "待处理"
    GENERATING = "GENERATING", "生成中"
    REVIEW = "REVIEW", "审核中"
    READY = "READY", "就绪"
    SUPERSEDED = "SUPERSEDED", "已废弃"


class PublicationStatus(models.TextChoices):
    PENDING = "PENDING", "待发布"
    UPLOADING = "UPLOADING", "上传中"
    REVIEWING = "REVIEWING", "平台审核中"
    PUBLISHED = "PUBLISHED", "已发布"
    REJECTED = "REJECTED", "已拒绝"
    FAILED = "FAILED", "失败"


class SubtitleTrack(models.Model):
    episode = models.ForeignKey(
        "creation.Episode", on_delete=models.CASCADE, related_name="subtitle_tracks"
    )
    language = models.CharField("语言代码", max_length=20)
    source_type = models.CharField(
        "来源类型", max_length=20, choices=SubtitleSourceType.choices
    )
    status = models.CharField(
        "状态", max_length=20, choices=SubtitleStatus.choices, default=SubtitleStatus.PENDING
    )
    asset = models.ForeignKey(
        "creation.Asset", on_delete=models.SET_NULL, null=True, blank=True
    )
    subtitle_data = models.JSONField("字幕数据", default=list)
    review_count = models.PositiveIntegerField("审核次数", default=0)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_subtitletrack"
        unique_together = ["episode", "language"]
        ordering = ["language"]
        verbose_name = "字幕轨"
        verbose_name_plural = "字幕轨"

    def __str__(self):
        return f"{self.episode} - {self.language}"


class DistributionChannel(models.Model):
    name = models.CharField("渠道名称", max_length=100)
    platform = models.CharField("平台标识", max_length=50)
    api_config = models.JSONField("API配置", default=dict)
    format_requirements = models.JSONField("格式要求", default=dict)
    is_active = models.BooleanField("是否启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_distributionchannel"
        ordering = ["name"]
        verbose_name = "发布渠道"
        verbose_name_plural = "发布渠道"

    def __str__(self):
        return self.name


class Publication(models.Model):
    episode = models.ForeignKey(
        "creation.Episode", on_delete=models.CASCADE, related_name="publications"
    )
    channel = models.ForeignKey(
        DistributionChannel, on_delete=models.CASCADE, related_name="publications"
    )
    status = models.CharField(
        "状态", max_length=20, choices=PublicationStatus.choices, default=PublicationStatus.PENDING
    )
    video_asset = models.ForeignKey(
        "creation.Asset", on_delete=models.SET_NULL, null=True, blank=True
    )
    subtitle_track = models.ForeignKey(
        SubtitleTrack, on_delete=models.SET_NULL, null=True, blank=True
    )
    title = models.CharField("平台标题", max_length=200, blank=True)
    description = models.TextField("描述", blank=True)
    tags = models.JSONField("标签", default=list)
    cover_url = models.URLField("封面URL", blank=True)
    # 渠道版本：{status: pending|processing|ready|not_needed|failed, url, media, error}
    rendition = models.JSONField("渠道版本", default=dict, blank=True)
    platform_video_id = models.CharField("平台视频ID", max_length=200, blank=True)
    platform_url = models.URLField("播放链接", blank=True)
    reject_reason = models.TextField("拒绝原因", blank=True)
    published_at = models.DateTimeField("发布时间", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_publication"
        unique_together = ["episode", "channel"]

    def __str__(self):
        return f"{self.episode} → {self.channel.name} [{self.status}]"
