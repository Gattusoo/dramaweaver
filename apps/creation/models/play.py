"""短剧生产树：Play / Episode / Scene / Shot / Character / VoiceProfile。"""
import uuid as _uuid

from django.conf import settings
from django.db import models


# ─── Choices ──────────────────────────────────────────────────────────────────

class PlayStatus(models.TextChoices):
    DRAFT = "DRAFT", "草稿"
    OUTLINING = "OUTLINING", "生成大纲中"
    SCRIPTING = "SCRIPTING", "分集剧本中"
    STORYBOARDING = "STORYBOARDING", "分镜中"
    ASSET_GEN = "ASSET_GEN", "素材生成中"
    COMPOSING = "COMPOSING", "合成中"
    COMPLETED = "COMPLETED", "已完成"
    PUBLISHED = "PUBLISHED", "已发布"
    PAUSED_AT_OUTLINE = "PAUSED_AT_OUTLINE", "大纲审核暂停"
    PAUSED_AT_SCRIPT = "PAUSED_AT_SCRIPT", "分集剧本审核暂停"
    PAUSED_AT_ASSETS = "PAUSED_AT_ASSETS", "素材审核暂停"


class AuditMode(models.TextChoices):
    NONE = "NONE", "无审核"
    KEY_CHECKPOINTS = "KEY_CHECKPOINTS", "关键节点审核"
    EVERY_STEP = "EVERY_STEP", "每步审核"


class RedoGranularity(models.TextChoices):
    SHOT = "SHOT", "单镜头"
    SCENE = "SCENE", "单场景"
    EPISODE = "EPISODE", "单集"


class ConsistencyMode(models.TextChoices):
    REFERENCE_IMAGE = "REFERENCE_IMAGE", "参考图"
    LORA = "LORA", "LoRA"
    NONE = "NONE", "无一致性"


class EpisodeStatus(models.TextChoices):
    PENDING = "PENDING", "待处理"
    SCRIPTING = "SCRIPTING", "分集剧本中"
    STORYBOARDING = "STORYBOARDING", "分镜中"
    ASSET_GEN = "ASSET_GEN", "素材生成中"
    COMPOSING = "COMPOSING", "合成中"
    COMPLETED = "COMPLETED", "已完成"


class SceneStatus(models.TextChoices):
    PENDING = "PENDING", "待处理"
    DONE = "DONE", "完成"


class ShotStatus(models.TextChoices):
    PENDING = "PENDING", "待处理"
    READY = "READY", "就绪"
    REDO = "REDO", "重做"


class TimeOfDay(models.TextChoices):
    DAY = "DAY", "白天"
    NIGHT = "NIGHT", "夜晚"
    DUSK = "DUSK", "黄昏"
    DAWN = "DAWN", "黎明"


class ShotType(models.TextChoices):
    CLOSE_UP = "CLOSE_UP", "特写"
    WIDE = "WIDE", "远景"
    MEDIUM = "MEDIUM", "中景"
    EXTREME_CLOSEUP = "EXTREME_CLOSEUP", "极特写"


class CharacterRole(models.TextChoices):
    PROTAGONIST = "PROTAGONIST", "主角"
    SUPPORTING = "SUPPORTING", "配角"
    ANTAGONIST = "ANTAGONIST", "反派"


class Gender(models.TextChoices):
    MALE = "MALE", "男"
    FEMALE = "FEMALE", "女"


# ─── Production Tree ──────────────────────────────────────────────────────────

class Play(models.Model):
    # 稳定 UUID（工作台 Stage A2，design D3）：项目清单/导出/内容寻址存储的项目键，
    # 跨机器/跨数据库迁移时保持身份；与自增 pk 并存（pk 仍是内部关联主键）。
    uuid = models.UUIDField("项目UUID", default=_uuid.uuid4, unique=True, editable=False)
    title = models.CharField("标题", max_length=200)
    synopsis = models.TextField("剧情简介", blank=True)
    genre = models.CharField("类型", max_length=100, blank=True)
    total_episodes = models.PositiveIntegerField("总集数", default=10)
    status = models.CharField(
        "状态", max_length=20, choices=PlayStatus.choices, default=PlayStatus.DRAFT
    )
    audit_mode = models.CharField(
        "审核模式", max_length=20, choices=AuditMode.choices, default=AuditMode.KEY_CHECKPOINTS
    )
    redo_granularity = models.CharField(
        "重做粒度", max_length=20, choices=RedoGranularity.choices, default=RedoGranularity.SHOT
    )
    consistency_mode = models.CharField(
        "角色一致性方案", max_length=20, choices=ConsistencyMode.choices, default=ConsistencyMode.NONE
    )
    gen_params = models.JSONField("创作参数", default=dict)
    agent_config = models.ForeignKey(
        "AgentConfig", on_delete=models.SET_NULL, null=True, blank=True, related_name="plays"
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="created_plays", null=True, blank=True
    )
    frozen_reason = models.TextField("冻结原因", blank=True)
    thumbnail_url = models.URLField("缩略图URL", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_play"
        indexes = [
            models.Index(fields=["created_by", "status"], name="idx_play_creator_status"),
            models.Index(fields=["agent_config"], name="idx_play_agent_config"),
        ]
        ordering = ["-created_at"]
        verbose_name = "短剧"
        verbose_name_plural = "短剧"

    def __str__(self):
        return self.title

    def save(self, *args, **kwargs):
        if not self.pk and self.agent_config:
            self._apply_agent_defaults()
        super().save(*args, **kwargs)

    def _apply_agent_defaults(self):
        if not self.agent_config:
            return
        if self.audit_mode == self._meta.get_field("audit_mode").default:
            self.audit_mode = self.agent_config.default_audit_mode or AuditMode.KEY_CHECKPOINTS
        if self.redo_granularity == self._meta.get_field("redo_granularity").default:
            self.redo_granularity = (
                self.agent_config.default_redo_granularity or RedoGranularity.SHOT
            )
        if self.consistency_mode == self._meta.get_field("consistency_mode").default:
            self.consistency_mode = (
                self.agent_config.consistency_mode or ConsistencyMode.NONE
            )

    _EPISODE_STATUS_RANK = {
        EpisodeStatus.PENDING: 0,
        EpisodeStatus.SCRIPTING: 1,
        EpisodeStatus.STORYBOARDING: 2,
        EpisodeStatus.ASSET_GEN: 3,
        EpisodeStatus.COMPOSING: 4,
        EpisodeStatus.COMPLETED: 5,
    }

    PAUSED_STATES = {
        PlayStatus.PAUSED_AT_OUTLINE,
        PlayStatus.PAUSED_AT_SCRIPT,
        PlayStatus.PAUSED_AT_ASSETS,
    }

    def _update_play_status(self):
        if self.status in self.PAUSED_STATES:
            return
        episodes = self.episodes.all()
        if not episodes.exists():
            return
        latest = max(
            episodes, key=lambda ep: self._EPISODE_STATUS_RANK.get(ep.status, 0)
        ).status
        if latest == EpisodeStatus.COMPLETED:
            self.status = PlayStatus.COMPLETED
        elif latest == EpisodeStatus.COMPOSING:
            self.status = PlayStatus.COMPOSING
        elif latest == EpisodeStatus.ASSET_GEN:
            self.status = PlayStatus.ASSET_GEN
        elif latest == EpisodeStatus.STORYBOARDING:
            self.status = PlayStatus.STORYBOARDING
        elif latest == EpisodeStatus.SCRIPTING:
            self.status = PlayStatus.SCRIPTING
        else:
            self.status = PlayStatus.DRAFT
        super().save(update_fields=["status"])


class Episode(models.Model):
    play = models.ForeignKey(
        Play, on_delete=models.CASCADE, related_name="episodes", verbose_name="所属短剧"
    )
    episode_number = models.PositiveIntegerField("集号")
    title = models.CharField("标题", max_length=200, blank=True)
    synopsis = models.TextField("该集摘要", blank=True)
    status = models.CharField(
        "状态", max_length=20, choices=EpisodeStatus.choices, default=EpisodeStatus.PENDING
    )
    duration_seconds = models.PositiveIntegerField("时长(秒)", default=0)
    video_url = models.URLField("视频URL", blank=True)
    poster_url = models.URLField("封面URL", blank=True)
    current_video_asset = models.ForeignKey(
        "Asset", on_delete=models.SET_NULL, null=True, blank=True, related_name="current_video_for_episode"
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_episode"
        unique_together = ["play", "episode_number"]
        ordering = ["episode_number"]
        verbose_name = "分集"
        verbose_name_plural = "分集"

    def __str__(self):
        return f"{self.play.title} - E{self.episode_number}"


class Scene(models.Model):
    episode = models.ForeignKey(
        Episode, on_delete=models.CASCADE, related_name="scenes", verbose_name="所属分集"
    )
    scene_number = models.PositiveIntegerField("场景号")
    location = models.CharField("地点", max_length=200, blank=True)
    time_of_day = models.CharField(
        "时段", max_length=20, choices=TimeOfDay.choices, default=TimeOfDay.DAY
    )
    description = models.TextField("环境描述", blank=True)
    mood = models.CharField("情绪标签", max_length=100, blank=True)
    status = models.CharField(
        "状态", max_length=20, choices=SceneStatus.choices, default=SceneStatus.PENDING
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_scene"
        unique_together = ["episode", "scene_number"]
        ordering = ["scene_number"]
        verbose_name = "场景"
        verbose_name_plural = "场景"

    def __str__(self):
        return f"{self.episode.title} - Scene {self.scene_number}"


class Shot(models.Model):
    scene = models.ForeignKey(
        Scene, on_delete=models.CASCADE, related_name="shots", verbose_name="所属场景"
    )
    shot_number = models.PositiveIntegerField("分镜号")
    shot_type = models.CharField(
        "镜头类型", max_length=20, choices=ShotType.choices, default=ShotType.MEDIUM
    )
    visual_prompt = models.TextField("画面描述prompt", blank=True)
    dialogue = models.TextField("对白文本", blank=True)
    dialogue_character = models.ForeignKey(
        "Character", on_delete=models.SET_NULL, null=True, blank=True, related_name="dialogue_shots"
    )
    action_note = models.TextField("动作指示", blank=True)
    emotion_tag = models.CharField("情绪标注", max_length=100, blank=True)
    duration_estimate = models.PositiveIntegerField("时长预估(秒)", default=0)
    sound_effects = models.JSONField("音效", default=list, blank=True)
    status = models.CharField(
        "状态", max_length=20, choices=ShotStatus.choices, default=ShotStatus.PENDING
    )
    current_image_asset = models.ForeignKey(
        "Asset", on_delete=models.SET_NULL, null=True, blank=True, related_name="current_image_for_shot"
    )
    current_audio_asset = models.ForeignKey(
        "Asset", on_delete=models.SET_NULL, null=True, blank=True, related_name="current_audio_for_shot"
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_shot"
        unique_together = ["scene", "shot_number"]
        ordering = ["shot_number"]
        verbose_name = "分镜"
        verbose_name_plural = "分镜"

    def __str__(self):
        return f"Scene {self.scene.scene_number} - Shot {self.shot_number}"


class Character(models.Model):
    play = models.ForeignKey(
        Play, on_delete=models.CASCADE, related_name="characters", verbose_name="所属短剧"
    )
    name = models.CharField("角色名", max_length=100)
    role = models.CharField("角色定位", max_length=20, choices=CharacterRole.choices)
    description = models.TextField("性格背景描述", blank=True)
    appearance_prompt = models.TextField("外貌描述prompt", blank=True)
    reference_image = models.ForeignKey(
        "Asset", on_delete=models.SET_NULL, null=True, blank=True, related_name="character_refs"
    )
    voice_profile = models.ForeignKey(
        "VoiceProfile", on_delete=models.SET_NULL, null=True, blank=True, related_name="characters"
    )
    lora_model_id = models.CharField("LoRA模型ID", max_length=100, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_character"
        indexes = [models.Index(fields=["play", "name"], name="idx_character_play_name")]
        verbose_name = "角色"
        verbose_name_plural = "角色"

    def __str__(self):
        return f"{self.play.title} - {self.name}"


class VoiceProfile(models.Model):
    name = models.CharField("声线名称", max_length=100)
    gender = models.CharField("性别", max_length=10, choices=Gender.choices)
    provider = models.ForeignKey(
        "ai_gateway.AIProvider", on_delete=models.CASCADE, related_name="voice_profiles"
    )
    voice_id = models.CharField("供应商侧voice ID", max_length=200)
    parameters = models.JSONField("参数", default=dict)
    preview_url = models.URLField("试听音频URL", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_voiceprofile"
        verbose_name = "声线配置"
        verbose_name_plural = "声线配置"

    def __str__(self):
        return self.name
