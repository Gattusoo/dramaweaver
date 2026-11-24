"""creation serializers —— 任务/审核记录。"""
from django.contrib.auth import get_user_model
from rest_framework import serializers

from ..models import ReviewLog, TaskRecord

UserModel = get_user_model()


class TaskRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = TaskRecord
        fields = [
            "id", "play", "episode", "scene", "shot", "asset",
            "task_type", "celery_task_id", "status", "ai_provider", "ai_model",
            "input_summary", "output_summary", "error_message", "retry_count",
            "cost", "started_at", "completed_at", "created_at", "updated_at",
        ]
        read_only_fields = ["created_at", "updated_at"]


class ReviewLogSerializer(serializers.ModelSerializer):
    reviewer = serializers.PrimaryKeyRelatedField(
        queryset=UserModel.objects.all(), required=False
    )

    class Meta:
        model = ReviewLog
        fields = [
            "id", "play", "review_point", "episode", "reviewer",
            "decision", "comment", "modifications", "created_at",
        ]
        read_only_fields = ["created_at"]
