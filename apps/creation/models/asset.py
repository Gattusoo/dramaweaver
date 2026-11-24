"""资产生命周期：Asset / AssetDependency / AssetLibrary。"""
import uuid as _uuid

from django.conf import settings
from django.db import models

from .play import Episode, Shot  # noqa: F401 — _update_current_asset_fk 等运行时引用


class AssetType(models.TextChoices):
    OUTLINE = "OUTLINE", "大纲"
    EPISODE_SCRIPT = "EPISODE_SCRIPT", "分集剧本"
    STORYBOARD = "STORYBOARD", "分镜"
    CHARACTER_REF = "CHARACTER_REF", "角色定妆"
    IMAGE = "IMAGE", "图片"
    AUDIO = "AUDIO", "音频"
    BGM = "BGM", "背景音乐"
    VIDEO = "VIDEO", "视频"
    SUBTITLE = "SUBTITLE", "字幕"
    TRANSITION = "TRANSITION", "转场"
    INTRO = "INTRO", "片头"
    OUTRO = "OUTRO", "片尾"
    BG_IMAGE = "BG_IMAGE", "背景图"
    PREVIEW = "PREVIEW", "预览成片"
    TRAILER = "TRAILER", "预告片"
    SFX = "SFX", "音效"


class AssetStatus(models.TextChoices):
    PENDING = "PENDING", "待生成"
    GENERATING = "GENERATING", "生成中"
    READY = "READY", "就绪"
    FAILED = "FAILED", "失败"
    SUPERSEDED = "SUPERSEDED", "已废弃"


class RelationType(models.TextChoices):
    SCRIPT_TO_STORYBOARD = "SCRIPT_TO_STORYBOARD", "剧本→分镜"
    STORYBOARD_TO_IMAGE = "STORYBOARD_TO_IMAGE", "分镜→图片"
    DIALOGUE_TO_AUDIO = "DIALOGUE_TO_AUDIO", "对白→音频"
    IMAGE_TO_VIDEO = "IMAGE_TO_VIDEO", "图片→视频"
    AUDIO_TO_VIDEO = "AUDIO_TO_VIDEO", "配音→视频"
    CHARACTER_REF_TO_IMAGE = "CHARACTER_REF_TO_IMAGE", "角色定妆→图片"
    VIDEO_TO_DERIVATIVE = "VIDEO_TO_DERIVATIVE", "成片→衍生品"


class Asset(models.Model):
    # 稳定 UUID / 生成规格 / 输入指纹（工作台 Stage A2，design D3/D4）：
    # - uuid：项目清单/导出/依赖边引用的稳定资源标识（pk 仍是内部主键）；
    # - generation_spec：规范化生成规格（模型/模板版本/有效参数/生成器版本/上游依赖）；
    # - input_fingerprint：生成规格规范化 JSON 的 SHA-256（64 字符）——相同输入指纹
    #   且文件仍有效的 READY 资产可复用（Stage A2 任务 2.5）；历史资产无指纹时保守重建；
    # - storage_key：受控相对存储键（内容寻址，projects/<play_uuid>/assets/sha256/...）。
    # 三个新字段均 nullable：历史行迁移回填后无指纹 = 不自动复用（spec: asset-lifecycle）。
    uuid = models.UUIDField("资源UUID", default=_uuid.uuid4, unique=True, editable=False)
    generation_spec = models.JSONField("生成规格", null=True, blank=True)
    input_fingerprint = models.CharField(
        "输入指纹", max_length=64, blank=True, db_index=True)
    storage_key = models.CharField("存储键", max_length=500, blank=True)
    content_sha256 = models.CharField("内容摘要", max_length=64, blank=True)
    play = models.ForeignKey(
        "Play", on_delete=models.CASCADE, related_name="assets", verbose_name="所属短剧"
    )
    episode = models.ForeignKey(
        "Episode", on_delete=models.SET_NULL, null=True, blank=True, related_name="assets"
    )
    scene = models.ForeignKey(
        "Scene", on_delete=models.SET_NULL, null=True, blank=True, related_name="assets"
    )
    shot = models.ForeignKey(
        "Shot", on_delete=models.SET_NULL, null=True, blank=True, related_name="assets"
    )
    character = models.ForeignKey(
        "Character", on_delete=models.SET_NULL, null=True, blank=True, related_name="assets"
    )
    asset_type = models.CharField("类型", max_length=30, choices=AssetType.choices)
    version = models.PositiveIntegerField("版本号", default=1)
    status = models.CharField(
        "状态", max_length=20, choices=AssetStatus.choices, default=AssetStatus.PENDING
    )
    artifact_url = models.URLField("存储路径", blank=True)
    thumbnail_url = models.URLField("缩略图URL", blank=True)
    metadata = models.JSONField("元数据", default=dict)
    ai_provider = models.ForeignKey(
        "ai_gateway.AIProvider", on_delete=models.SET_NULL, null=True, blank=True, related_name="assets"
    )
    ai_model = models.CharField("使用的模型名", max_length=200, blank=True)
    prompt = models.TextField("生成prompt", blank=True)
    parameters = models.JSONField("生成参数", default=dict)
    cost = models.DecimalField("费用", max_digits=10, decimal_places=6, default=0)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    depends_on = models.ManyToManyField(
        "self",
        through="AssetDependency",
        through_fields=("downstream", "upstream"),
        symmetrical=False,
        blank=True,
        related_name="dependents",
    )

    class Meta:
        db_table = "shortplay_asset"
        indexes = [
            models.Index(
                fields=["play", "episode", "scene", "shot", "asset_type", "version"],
                name="idx_asset_scope_version",
            ),
        ]
        ordering = ["-version"]
        verbose_name = "产出物"
        verbose_name_plural = "产出物"

    def __str__(self):
        return f"{self.asset_type} v{self.version} ({self.play})"

    def save(self, *args, **kwargs):
        if not self.pk:
            self._bump_version()
        super().save(*args, **kwargs)
        if self.status == AssetStatus.READY:
            self._supersede_previous_version()
        self._update_current_asset_fk()

    def _scope_filter(self):
        return dict(
            play=self.play, episode=self.episode, scene=self.scene,
            shot=self.shot, character=self.character, asset_type=self.asset_type,
        )

    def _bump_version(self):
        last = Asset.objects.filter(**self._scope_filter()).order_by("-version").first()
        if last:
            self.version = last.version + 1

    def _supersede_previous_version(self):
        prev = Asset.objects.filter(
            **self._scope_filter(), status=AssetStatus.READY,
        ).exclude(pk=self.pk).order_by("-version").first()
        if prev and prev.version < self.version:
            prev.mark_superseded_recursive()

    def _update_current_asset_fk(self):
        if self.status != AssetStatus.READY:
            return
        if self.shot and self.asset_type in (AssetType.IMAGE, AssetType.AUDIO):
            fk_field = (
                "current_image_asset"
                if self.asset_type == AssetType.IMAGE
                else "current_audio_asset"
            )
            Shot.objects.filter(pk=self.shot.pk).update(**{fk_field: self})
        if self.episode and self.asset_type == AssetType.VIDEO:
            Episode.objects.filter(pk=self.episode.pk).update(
                current_video_asset=self
            )

    def mark_superseded_recursive(self):
        from django.db import transaction

        with transaction.atomic():
            self.status = AssetStatus.SUPERSEDED
            self.save(update_fields=["status"])
            self._cascade_superseded()

    def _cascade_superseded(self):
        edges = AssetDependency.objects.filter(upstream=self)
        for edge in edges:
            downstream = edge.downstream
            if downstream.status != AssetStatus.SUPERSEDED:
                downstream.status = AssetStatus.SUPERSEDED
                downstream.save(update_fields=["status"])
                if downstream.shot and downstream.asset_type in (AssetType.IMAGE, AssetType.AUDIO):
                    fk_field = (
                        "current_image_asset"
                        if downstream.asset_type == AssetType.IMAGE
                        else "current_audio_asset"
                    )
                    Shot.objects.filter(pk=downstream.shot.pk).update(**{fk_field: None})
                if downstream.episode and downstream.asset_type == AssetType.VIDEO:
                    Episode.objects.filter(pk=downstream.episode.pk).update(current_video_asset=None)
                downstream._cascade_superseded()


class AssetDependency(models.Model):
    upstream = models.ForeignKey(
        "Asset", on_delete=models.CASCADE, related_name="dependency_edges_upstream"
    )
    downstream = models.ForeignKey(
        "Asset", on_delete=models.CASCADE, related_name="dependency_edges_downstream"
    )
    relation_type = models.CharField(
        "关系类型", max_length=40, choices=RelationType.choices
    )

    class Meta:
        db_table = "shortplay_asset_dependency"
        unique_together = ["upstream", "downstream"]
        verbose_name = "资产依赖"
        verbose_name_plural = "资产依赖"

    def __str__(self):
        return f"{self.upstream} → {self.downstream} ({self.relation_type})"


class AssetLibrary(models.Model):
    name = models.CharField("名称", max_length=200)
    asset_type = models.CharField("类型", max_length=30, choices=AssetType.choices)
    file_url = models.URLField("文件URL")
    thumbnail_url = models.URLField("缩略图URL", blank=True)
    metadata = models.JSONField("元数据", default=dict)
    is_public = models.BooleanField("全局可用", default=True)
    owner = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True, related_name="asset_libraries"
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_asset_library"
        verbose_name = "素材库"
        verbose_name_plural = "素材库"

    def __str__(self):
        return f"{self.name} ({self.asset_type})"
