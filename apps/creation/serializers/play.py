"""creation serializers —— play 树资源。

helpers（_scoped_play_qs / PlayScopedFieldMixin / CurrentUserDefault*）也在此，
被 asset.py 等复用。
"""
from django.contrib.auth import get_user_model
from rest_framework import serializers

from ..models import (
    AgentConfig, Asset, Character, Episode, Play, Scene, Shot, VoiceProfile,
)
from ..models import MemberStatus, PlayMember
from ..permissions import get_accessible_play_ids
from apps.core.system.permissions import is_platform_admin

UserModel = get_user_model()


def _scoped_play_qs(request, model):
    """按当前用户可访问短剧过滤的 queryset（用于关联字段防跨剧引用）。"""
    if request is None:
        return model.objects.none()
    return model.objects.filter(play__in=get_accessible_play_ids(request.user))


class PlayScopedFieldMixin:
    """让指定关联字段的 queryset 随请求用户限定到可访问短剧。"""

    _PLAY_SCOPED_FIELDS = ()

    def get_fields(self):
        fields = super().get_fields()
        request = self.context.get("request")
        if request is None:
            return fields
        for name in self._PLAY_SCOPED_FIELDS:
            field = fields.get(name)
            if field is None:
                continue
            target = getattr(field, "child_relation", field)
            target.queryset = _scoped_play_qs(request, target.queryset.model)
        return fields


class CurrentUserDefault:
    def __call__(self, serializer_field):
        return serializer_field.context["request"].user


class CurrentUserDefaultSerializerField(serializers.PrimaryKeyRelatedField):
    def __init__(self, **kwargs):
        kwargs["queryset"] = UserModel.objects.all()
        kwargs["default"] = CurrentUserDefault()
        super().__init__(**kwargs)

    def get_value(self, data):
        return self.default(self)


class VoiceProfileSerializer(serializers.ModelSerializer):
    class Meta:
        model = VoiceProfile
        fields = ["id", "name", "gender", "provider", "voice_id", "parameters", "preview_url", "created_at"]
        read_only_fields = ["created_at"]


class CharacterSerializer(PlayScopedFieldMixin, serializers.ModelSerializer):
    _PLAY_SCOPED_FIELDS = ("reference_image",)
    voice_profile = VoiceProfileSerializer(read_only=True)
    voice_profile_id = serializers.PrimaryKeyRelatedField(
        queryset=VoiceProfile.objects.all(), source="voice_profile", write_only=True, required=False,
        allow_null=True,
    )
    reference_image = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects.all(), required=False, allow_null=True
    )

    class Meta:
        model = Character
        fields = [
            "id", "play", "name", "role", "description", "appearance_prompt",
            "reference_image", "voice_profile", "voice_profile_id", "lora_model_id",
            "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class ShotSerializer(PlayScopedFieldMixin, serializers.ModelSerializer):
    _PLAY_SCOPED_FIELDS = ("current_image_asset", "current_audio_asset", "dialogue_character")
    current_image_asset = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects.all(), allow_null=True, required=False
    )
    current_audio_asset = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects.all(), allow_null=True, required=False
    )
    dialogue_character = serializers.PrimaryKeyRelatedField(
        queryset=Character.objects.all(), allow_null=True, required=False
    )

    class Meta:
        model = Shot
        fields = [
            "id", "scene", "shot_number", "shot_type", "visual_prompt", "dialogue",
            "dialogue_character", "action_note", "emotion_tag", "duration_estimate",
            "status", "current_image_asset", "current_audio_asset",
            "created_at", "updated_at",
        ]
        read_only_fields = ["status", "created_at", "updated_at"]


class SceneSerializer(serializers.ModelSerializer):
    shots = ShotSerializer(many=True, read_only=True)

    class Meta:
        model = Scene
        fields = [
            "id", "episode", "scene_number", "location", "time_of_day",
            "description", "mood", "status", "shots",
            "created_at", "updated_at",
        ]
        read_only_fields = ["status", "created_at", "updated_at"]


class EpisodeSerializer(PlayScopedFieldMixin, serializers.ModelSerializer):
    _PLAY_SCOPED_FIELDS = ("current_video_asset",)
    scenes = SceneSerializer(many=True, read_only=True)
    subtitle_tracks = serializers.PrimaryKeyRelatedField(many=True, read_only=True)
    current_video_asset = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects.all(), allow_null=True, required=False
    )

    class Meta:
        model = Episode
        fields = [
            "id", "play", "episode_number", "title", "synopsis", "status",
            "duration_seconds", "video_url", "current_video_asset", "scenes",
            "subtitle_tracks", "created_at", "updated_at",
        ]
        read_only_fields = ["status", "created_at", "updated_at"]


class AgentConfigBriefSerializer(serializers.ModelSerializer):
    class Meta:
        model = AgentConfig
        fields = ["id", "name", "description"]


class PlaySerializer(serializers.ModelSerializer):
    agent_config = AgentConfigBriefSerializer(read_only=True)
    agent_config_id = serializers.PrimaryKeyRelatedField(
        # allow_null：前端"不选配方"时显式传 null。模型 agent_config 本身可空（on_delete=SET_NULL），
        # null 即"不挂配方"，audit/redo/consistency 用表单里显式传的值或 Play 自身列默认。
        queryset=AgentConfig.objects.all(), source="agent_config", write_only=True,
        required=False, allow_null=True,
    )
    episodes = EpisodeSerializer(many=True, read_only=True)
    characters = CharacterSerializer(many=True, read_only=True)
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    members = serializers.SerializerMethodField()

    def get_members(self, obj):
        rows = obj.members.filter(status=MemberStatus.ACTIVE).select_related("user").order_by("created_at")
        return [{"user": r.user_id, "username": r.user.username, "role": r.role,
                 "accepted_at": r.accepted_at} for r in rows]

    class Meta:
        model = Play
        fields = [
            "id", "title", "synopsis", "genre", "total_episodes", "status",
            "audit_mode", "redo_granularity", "consistency_mode", "gen_params",
            "agent_config", "agent_config_id", "created_by", "members",
            "thumbnail_url",
            "episodes", "characters", "created_at", "updated_at",
        ]
        read_only_fields = ["status", "created_at", "updated_at"]

    def get_fields(self):
        from django.db.models import Q

        fields = super().get_fields()
        request = self.context.get("request")
        if request is None:
            return fields
        user = request.user
        qs = AgentConfig.objects.all()
        if not user.is_authenticated:
            qs = AgentConfig.objects.none()
        elif not is_platform_admin(user):
            qs = qs.filter(Q(created_by=user) | Q(is_template=True))
        fields["agent_config_id"].queryset = qs
        return fields
