"""distribution serializers。"""
from rest_framework import serializers

from apps.core.utils.media import validate_requirements

from .models import DistributionChannel, Publication, SubtitleTrack


class SubtitleTrackSerializer(serializers.ModelSerializer):
    class Meta:
        model = SubtitleTrack
        fields = [
            "id", "episode", "language", "source_type", "status",
            "asset", "subtitle_data", "review_count", "created_at", "updated_at",
        ]
        read_only_fields = ["status", "created_at", "updated_at"]


class DistributionChannelSerializer(serializers.ModelSerializer):
    class Meta:
        model = DistributionChannel
        fields = [
            "id", "name", "platform", "api_config", "format_requirements",
            "is_active", "created_at",
        ]
        read_only_fields = ["created_at"]

    def validate_format_requirements(self, value):
        errors = validate_requirements(value or {})
        if errors:
            raise serializers.ValidationError(errors)
        return value or {}


class PublicationSerializer(serializers.ModelSerializer):
    class Meta:
        model = Publication
        fields = [
            "id", "episode", "channel", "status", "video_asset",
            "subtitle_track", "title", "description", "tags", "cover_url",
            "rendition", "platform_video_id", "platform_url", "reject_reason",
            "published_at", "created_at", "updated_at",
        ]
        read_only_fields = ["status", "rendition", "created_at", "updated_at"]
