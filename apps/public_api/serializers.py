"""公开序列化器（public/api）——严格白名单。

刻意不复用业务序列化器：公开区面向匿名访客，绝不能误带 created_by、成本、
内部状态（审核暂停态）、agent_config、authorized_users、ai_model 等内部字段。
字段契约以前端 src/types/public.ts 为准。

字段映射（模型无同名列）：
- Play.thumbnail_url  -> cover_url
- Play 无 published_at 列 -> 恒 null（前端可空，显示兜底）
- Play 无播放量来源 -> play_count 恒 0（前端为 0 不显示）
- Episode.poster_url -> cover_url
"""
from rest_framework import serializers

from apps.creation.models import Character, Episode, Play


class PublicPlaySerializer(serializers.ModelSerializer):
    cover_url = serializers.URLField(source="thumbnail_url", read_only=True)
    published_at = serializers.SerializerMethodField()
    play_count = serializers.SerializerMethodField()

    class Meta:
        model = Play
        fields = ["id", "title", "genre", "synopsis", "total_episodes",
                  "cover_url", "published_at", "play_count"]

    def get_published_at(self, obj):
        return None

    def get_play_count(self, obj):
        return 0


class PublicEpisodeSerializer(serializers.ModelSerializer):
    cover_url = serializers.URLField(source="poster_url", read_only=True)

    class Meta:
        model = Episode
        fields = ["id", "episode_number", "title", "duration_seconds", "video_url", "cover_url"]


class PublicCharacterSerializer(serializers.ModelSerializer):
    class Meta:
        model = Character
        fields = ["id", "name", "role", "description"]


class PublicPlayDetailSerializer(PublicPlaySerializer):
    episodes = serializers.SerializerMethodField()
    characters = serializers.SerializerMethodField()

    class Meta(PublicPlaySerializer.Meta):
        fields = PublicPlaySerializer.Meta.fields + ["episodes", "characters"]

    def get_episodes(self, obj):
        eps = obj.episodes.filter(status="COMPLETED").order_by("episode_number")
        return PublicEpisodeSerializer(eps, many=True).data

    def get_characters(self, obj):
        chars = obj.characters.all().order_by("pk")
        return PublicCharacterSerializer(chars, many=True).data


class PublicStatsSerializer(serializers.Serializer):
    plays = serializers.IntegerField()
    episodes = serializers.IntegerField()
    total_duration_seconds = serializers.IntegerField()
    genres = serializers.ListField(child=serializers.DictField())
