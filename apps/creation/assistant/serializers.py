from rest_framework import serializers

from .models import (
    AgentFeedback, AssistantMessage, AssistantProposal, AssistantSession, CharacterVersion,
    Inspiration, ReviewFeedback, ScriptVersion, SkillLibrary, StrategyExperiment,
)


class AssistantMessageSerializer(serializers.ModelSerializer):
    class Meta:
        model = AssistantMessage
        fields = ["id", "session", "seq", "role", "content", "task_record", "created_at"]
        read_only_fields = fields


class AssistantSessionSerializer(serializers.ModelSerializer):
    play_id = serializers.IntegerField(write_only=True)
    running_task_id = serializers.SerializerMethodField()
    last_cursor = serializers.SerializerMethodField()
    max_cost = serializers.SerializerMethodField()

    class Meta:
        model = AssistantSession
        fields = ["id", "play", "play_id", "user", "title", "status", "running_task_id", "last_cursor",
                  "total_cost", "total_tokens", "max_cost", "created_at", "updated_at"]
        read_only_fields = ["play", "user", "status", "total_cost", "total_tokens", "created_at", "updated_at"]

    def get_running_task_id(self, obj):
        from .views import active_task
        task = active_task(obj)
        return task.pk if task else None

    def get_last_cursor(self, obj):
        from . import events
        return events.last_cursor(obj.pk)

    def get_max_cost(self, obj):
        from .runner import budget_of
        limit = budget_of(obj)["max_cost"]
        return None if limit is None else str(limit)


_KIND_LABEL = {"OUTLINE": "大纲", "EPISODE_SCRIPT": "剧本", "SCENE": "场景", "SHOT": "镜头",
               "STORY_ASSET": "故事设定"}


class AssistantProposalSerializer(serializers.ModelSerializer):
    target_label = serializers.SerializerMethodField()
    impacted_summary = serializers.SerializerMethodField()

    class Meta:
        model = AssistantProposal
        fields = ["id", "session", "message", "play", "kind", "target_asset", "target_episode", "target_scene",
                  "target_shot", "target_character", "target_label", "base_version", "base_snapshot", "payload",
                  "summary", "impacted_asset_ids", "impacted_summary", "status", "applied_asset", "applied_by",
                  "applied_at", "result", "parent", "alternative_group", "alternative_label", "created_at"]
        read_only_fields = fields

    def get_target_label(self, p):
        """展示用目标名，如「第 3 集剧本」「第 2 集 · 场景 1 · 镜头 4」「角色 · 林总」。"""
        ep = f"第 {p.target_episode.episode_number} 集" if p.target_episode else ""
        scene = f"{ep} · 场景 {p.target_scene.scene_number}" if p.target_scene else ep
        pay = p.payload or {}
        if p.kind == "OUTLINE":
            return "大纲"
        if p.kind == "EPISODE_SCRIPT":
            return f"{ep}剧本" if ep else "剧本"
        if p.kind in ("SCENE", "SHOT"):
            if p.kind == "SHOT" and p.target_shot:
                return f"{scene} · 镜头 {p.target_shot.shot_number}"
            return scene or _KIND_LABEL[p.kind]
        if p.kind == "SHOT_CREATE":
            return f"{scene} · 在镜头 {pay.get('after_shot_number', 0)} 之后插入"
        if p.kind == "SHOT_DELETE":
            return f"{ep} · 场景 {pay.get('scene_number')} · 删除镜头 {pay.get('shot_number')}"
        if p.kind == "SCENE_CREATE":
            return f"{ep} · 新增场景"
        if p.kind == "CHARACTER_EDIT":
            return f"角色 · {p.target_character.name if p.target_character else pay.get('name', '')}"
        if p.kind == "CHARACTER_CREATE":
            return f"新角色 · {pay.get('name', '')}"
        if p.kind == "GENERATION":
            action = {"redo_shot": "重做镜头", "redo_scene": "重做场景", "preview_episode": "生成预览"}
            if pay.get("action") == "redo_shot" and p.target_shot:
                where = f"{scene} · 镜头 {p.target_shot.shot_number}"
            else:
                where = scene if pay.get("action") == "redo_scene" else ep
            return f"{action.get(pay.get('action'), '生成')} · {where}"
        if p.kind == "STORY_ASSET":
            verb = "更新设定" if pay.get("action") == "update" else "新建设定"
            return f"{verb} · {pay.get('name', '')}"
        return _KIND_LABEL.get(p.kind, p.kind)

    def get_impacted_summary(self, p):
        from django.db.models import Count

        from ..models import Asset
        rows = Asset.objects.filter(pk__in=p.impacted_asset_ids or []).values("asset_type").annotate(n=Count("id"))
        return {r["asset_type"]: r["n"] for r in rows}


class AssistantSkillLibrarySerializer(serializers.ModelSerializer):
    """技能库（Phase 2.2）。置信度与计数只读，由学习流程维护。"""
    samples = serializers.IntegerField(read_only=True)

    class Meta:
        model = SkillLibrary
        fields = ["id", "name", "scope", "play", "skill_type", "description", "trigger_conditions",
                  "execution_steps", "success_count", "failure_count", "confidence", "samples",
                  "source_turn", "is_active", "created_at", "updated_at"]
        read_only_fields = ["success_count", "failure_count", "confidence", "samples", "source_turn",
                            "created_at", "updated_at"]

    def validate_name(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("技能名不能为空")
        return value

    def validate_trigger_conditions(self, value):
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise serializers.ValidationError("trigger_conditions 须为字符串数组")
        return [v.strip() for v in value if v.strip()][:8]

    def validate_execution_steps(self, value):
        if not isinstance(value, list) or any(not isinstance(v, str) for v in value):
            raise serializers.ValidationError("execution_steps 须为字符串数组")
        return [v.strip() for v in value if v.strip()][:8]


class ScriptVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = ScriptVersion
        fields = ["id", "play", "episode", "version", "branch_name", "parent", "content", "summary",
                  "created_by", "created_at"]
        read_only_fields = ["play", "episode", "version", "parent", "created_by", "created_at"]


class CharacterVersionSerializer(serializers.ModelSerializer):
    class Meta:
        model = CharacterVersion
        fields = ["id", "character", "version", "snapshot", "changed_fields", "impacted_asset_ids",
                  "created_by", "created_at"]
        read_only_fields = fields


class AssistantExperimentSerializer(serializers.ModelSerializer):
    class Meta:
        model = StrategyExperiment
        fields = ["id", "name", "description", "variants", "status", "created_by", "created_at", "updated_at"]
        read_only_fields = ["created_by", "created_at", "updated_at"]


class ToolRegistrySerializer(serializers.Serializer):
    """注册表工具序列化（registry D6 + 市场侧扩展）。

    写侧白名单在视图层控制（tool_name/category/description/input_schema/
    handler_code/expected_version）；读侧回显展示字段 + mine 布尔。
    handler_code 仅作者/平台管理员可见（他人 verified 工具不回源码）。
    市场侧：installed_from 溯源、my_rating 本人评分回显；review_action/
    review_note 仅在视图显式传 show_review_note 时回显（驳回原因只给作者）。
    """
    id = serializers.IntegerField(read_only=True)
    tool_name = serializers.CharField()
    tool_type = serializers.CharField()
    category = serializers.CharField()
    description = serializers.CharField()
    input_schema = serializers.JSONField()
    is_verified = serializers.BooleanField()
    is_active = serializers.BooleanField()
    version = serializers.IntegerField()
    installed_from = serializers.SerializerMethodField()
    created_at = serializers.DateTimeField()
    updated_at = serializers.DateTimeField()
    mine = serializers.SerializerMethodField()
    handler_code = serializers.SerializerMethodField()
    my_rating = serializers.SerializerMethodField()
    review_action = serializers.SerializerMethodField()
    review_note = serializers.SerializerMethodField()

    def get_mine(self, obj):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        return bool(user and getattr(user, "is_authenticated", False)
                    and obj.author_id == user.pk)

    def get_handler_code(self, obj):
        # 上下文标记由视图显式传入：is_author / is_admin（默认 False → 不回源码）
        return obj.handler_code if (self.context.get("is_author")
                                    or self.context.get("is_admin")) else None

    def get_installed_from(self, obj):
        return obj.installed_from_id

    def get_my_rating(self, obj):
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if not (user and getattr(user, "is_authenticated", False)):
            return None
        rating = obj.ratings.filter(user_id=user.pk).first()
        return rating.rating if rating else None

    def get_review_action(self, obj):
        if not self.context.get("show_review_note"):
            return None
        from .registry import latest_review_note
        return latest_review_note(obj)[0]

    def get_review_note(self, obj):
        if not self.context.get("show_review_note"):
            return None
        from .registry import latest_review_note
        return latest_review_note(obj)[1]


class InspirationSerializer(serializers.ModelSerializer):
    """灵感库（Phase 3.5.1）：source/used_count 只读（agent 通道与检索维护）。"""

    class Meta:
        model = Inspiration
        fields = ["id", "play", "user", "category", "content", "source",
                  "rating", "used_count", "created_at", "updated_at"]
        read_only_fields = ["user", "source", "used_count", "created_at", "updated_at"]

    def validate_content(self, value):
        value = (value or "").strip()
        if not value:
            raise serializers.ValidationError("灵感内容不能为空")
        if len(value) > 2000:
            raise serializers.ValidationError("灵感内容超过 2000 字上限")
        return value

    def validate_rating(self, value):
        if value is None:
            return 3
        if not isinstance(value, int) or isinstance(value, bool) or not (1 <= value <= 5):
            raise serializers.ValidationError("评分须为 1-5 的整数")
        return value


class AgentFeedbackSerializer(serializers.ModelSerializer):
    """协作轮反馈（Phase 3.2.8）。"""

    class Meta:
        model = AgentFeedback
        fields = ["id", "session", "user", "turn", "rating", "comment",
                  "created_at", "updated_at"]
        read_only_fields = ["session", "user", "created_at", "updated_at"]


class ReviewFeedbackSerializer(serializers.ModelSerializer):
    """审查反馈（Phase 3.5.2）。issues_feedback 形状宽松接收（位置性键）。"""

    class Meta:
        model = ReviewFeedback
        fields = ["id", "episode", "review_log", "user", "rating",
                  "issues_feedback", "created_at", "updated_at"]
        read_only_fields = ["user", "created_at", "updated_at"]

    def validate_issues_feedback(self, value):
        if not value:
            return []
        if not isinstance(value, list):
            raise serializers.ValidationError("issues_feedback 应为列表")
        for item in value:
            if not isinstance(item, dict) or "issue_key" not in item \
                    or not isinstance(item.get("agree"), bool):
                raise serializers.ValidationError(
                    "每条须为 {issue_key, agree, suggestion?} 形状")
        return value
