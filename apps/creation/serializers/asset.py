"""creation serializers —— 资产生命周期。"""
from rest_framework import serializers

from ..models import (
    Asset, AssetDependency, AssetLibrary, AssetType, RelationType,
)
from .play import PlayScopedFieldMixin


class AssetDependencySerializer(serializers.ModelSerializer):
    class Meta:
        model = AssetDependency
        fields = ["id", "upstream", "downstream", "relation_type"]
        read_only_fields = ["id"]


class AssetSerializer(PlayScopedFieldMixin, serializers.ModelSerializer):
    _PLAY_SCOPED_FIELDS = ("depends_on_ids",)
    depends_on = serializers.SerializerMethodField()
    depends_on_ids = serializers.PrimaryKeyRelatedField(
        queryset=Asset.objects.all(), many=True, write_only=True, required=False
    )

    class Meta:
        model = Asset
        fields = [
            "id", "play", "episode", "scene", "shot", "character",
            "asset_type", "version", "status", "artifact_url", "thumbnail_url",
            "metadata", "ai_provider", "ai_model", "prompt", "parameters", "cost",
            "depends_on", "depends_on_ids", "created_at", "updated_at",
        ]
        # 流水线控制字段只由生成/合成任务写入：客户端可伪造 READY + artifact_url
        # 指向任意外链，经 Asset.save 把 Episode 直接推进到 COMPLETED 并成为成片，
        # 绕过生成、审核与发布校验。前端目前只 PATCH metadata，故一并设为只读。
        read_only_fields = [
            "created_at", "updated_at",
            "version", "status", "artifact_url", "thumbnail_url",
            "cost", "ai_provider", "ai_model",
        ]

    def get_depends_on(self, obj):
        return AssetSerializer(obj.depends_on.all(), many=True).data

    def create(self, validated_data):
        depends_on_ids = validated_data.pop("depends_on_ids", [])
        asset = super().create(validated_data)
        self._set_dependencies(asset, depends_on_ids)
        return asset

    def update(self, instance, validated_data):
        depends_on_ids = validated_data.pop("depends_on_ids", None)
        asset = super().update(instance, validated_data)
        if depends_on_ids is not None:
            self._set_dependencies(asset, depends_on_ids)
        return asset

    def _set_dependencies(self, asset, upstream_assets):
        AssetDependency.objects.filter(downstream=asset).delete()
        for up in upstream_assets:
            AssetDependency.objects.get_or_create(
                downstream=asset,
                upstream=up,
                defaults={"relation_type": self._guess_relation_type(asset, up)},
            )

    @staticmethod
    def _guess_relation_type(asset, upstream):
        mapping = {
            (AssetType.EPISODE_SCRIPT, AssetType.OUTLINE): RelationType.SCRIPT_TO_STORYBOARD,
            (AssetType.STORYBOARD, AssetType.EPISODE_SCRIPT): RelationType.SCRIPT_TO_STORYBOARD,
            (AssetType.IMAGE, AssetType.STORYBOARD): RelationType.STORYBOARD_TO_IMAGE,
            (AssetType.IMAGE, AssetType.CHARACTER_REF): RelationType.CHARACTER_REF_TO_IMAGE,
            (AssetType.AUDIO, AssetType.STORYBOARD): RelationType.DIALOGUE_TO_AUDIO,
            (AssetType.VIDEO, AssetType.IMAGE): RelationType.IMAGE_TO_VIDEO,
        }
        return mapping.get(
            (asset.asset_type, upstream.asset_type), RelationType.SCRIPT_TO_STORYBOARD
        )


class AssetLibrarySerializer(serializers.ModelSerializer):
    class Meta:
        model = AssetLibrary
        fields = [
            "id", "name", "asset_type", "file_url", "thumbnail_url",
            "metadata", "is_public", "owner", "created_at",
        ]
        read_only_fields = ["created_at"]
