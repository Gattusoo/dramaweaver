"""creation serializers —— 智能体配方。"""
from decimal import Decimal

from rest_framework import serializers

from apps.ai_gateway.models import AIModel
from apps.ai_gateway.router import supports_tools
from apps.ai_gateway.serializers import AIModelSerializer

from ..models import AgentConfig


class AgentConfigSerializer(serializers.ModelSerializer):
    assistant_budget_effective = serializers.SerializerMethodField()
    llm_model = AIModelSerializer(read_only=True)
    tts_model = AIModelSerializer(read_only=True)
    image_model = AIModelSerializer(read_only=True)
    video_model = AIModelSerializer(read_only=True)

    def validate_is_template(self, value):
        """is_template 会让配方对所有非管理员可见/可克隆，仅平台管理员可设置。

        非平台管理员试图开启（新建或把已有配方改为模板）时拒绝，防止自发布到全租户。"""
        if value:
            from dvadmin.system.permissions import is_platform_admin
            request = self.context.get("request")
            user = getattr(request, "user", None)
            if user is None or not user.is_authenticated or \
                    not is_platform_admin(user):
                raise serializers.ValidationError("仅平台管理员可设置 is_template")
        return value

    llm_model_id = serializers.PrimaryKeyRelatedField(
        queryset=AIModel.objects.all(), source="llm_model", write_only=True, required=False, allow_null=True
    )
    tts_model_id = serializers.PrimaryKeyRelatedField(
        queryset=AIModel.objects.all(), source="tts_model", write_only=True, required=False, allow_null=True
    )
    image_model_id = serializers.PrimaryKeyRelatedField(
        queryset=AIModel.objects.all(), source="image_model", write_only=True, required=False, allow_null=True
    )
    video_model_id = serializers.PrimaryKeyRelatedField(
        queryset=AIModel.objects.all(), source="video_model", write_only=True, required=False, allow_null=True
    )
    created_by = serializers.PrimaryKeyRelatedField(read_only=True)
    assistant_model = AIModelSerializer(read_only=True)
    assistant_model_id = serializers.PrimaryKeyRelatedField(
        queryset=AIModel.objects.all(), source="assistant_model", write_only=True, required=False, allow_null=True
    )

    def validate_assistant_model_id(self, model):
        if model is not None and not supports_tools(model):
            raise serializers.ValidationError("创作助手模型须支持工具调用（capabilities.supports_tools=true）")
        return model

    def validate_assistant_failover(self, ids):
        if not isinstance(ids, list) or any(isinstance(i, bool) or not isinstance(i, int) for i in ids):
            raise serializers.ValidationError("应为 AIModel id 列表")
        found = {m.pk: m for m in AIModel.objects.filter(pk__in=ids)}
        bad = [i for i in ids if i not in found or not supports_tools(found[i])]
        if bad:
            raise serializers.ValidationError(f"以下模型不存在或不支持工具调用: {bad}")
        return ids

    # null 仅为金额类字段接受（语义"显式不限制"）；迭代/次数类字段下游按整数消费，拒绝 null。
    BUDGET_NULLABLE = ("max_cost", "max_collab_cost")
    # 韧性三字段（runner.budget_of 已消费）：正整数；max_total_tool_attempts 额外接受 0（按推导）。
    BUDGET_POSITIVE_INT = ("max_tool_attempts", "tool_retry_backoff_ms")
    BUDGET_NON_NEG_INT = ("max_total_tool_attempts",)

    def validate_assistant_budget(self, budget):
        if not isinstance(budget, dict):
            raise serializers.ValidationError("应为对象")
        for key, value in budget.items():
            if key not in ("max_cost", "max_collab_cost", "max_iterations", "max_tool_calls",
                           "max_tool_attempts", "max_total_tool_attempts", "tool_retry_backoff_ms"):
                raise serializers.ValidationError(f"未知字段 {key}")
            if value is None:
                if key not in self.BUDGET_NULLABLE:
                    raise serializers.ValidationError(f"{key} 须为正数（不支持 null）")
                continue
            if key in self.BUDGET_NON_NEG_INT:
                # 0 = 按 max_tool_calls*2 推导（显式合法值，不能被"正数"校验误杀）
                if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                    raise serializers.ValidationError(f"{key} 应为非负整数（0 表示按推导）")
                continue
            if key in self.BUDGET_POSITIVE_INT:
                if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
                    raise serializers.ValidationError(f"{key} 应为正整数")
                continue
            if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
                raise serializers.ValidationError(f"{key} 应为正数或 null")
        return budget

    class Meta:
        model = AgentConfig
        fields = [
            "id", "name", "description",
            "llm_model", "tts_model", "image_model", "video_model",
            "llm_model_id", "tts_model_id", "image_model_id", "video_model_id",
            "llm_failover", "tts_failover", "image_failover", "video_failover",
            "assistant_model", "assistant_model_id", "assistant_failover", "assistant_budget",
            "assistant_budget_effective",
            "retry_count", "retry_backoff", "fallback_on_limit",
            "default_audit_mode", "default_redo_granularity", "consistency_mode",
            "prompt_templates", "style_params", "is_template", "created_by",
            "created_at",
        ]
        read_only_fields = ["created_at", "assistant_budget_effective"]

    def get_assistant_budget_effective(self, obj):
        """生效额度（默认值合并后的实际值），使客户端可区分"限制到 ¥X"与"不限制"。

        金额字段以字符串回显（Decimal 不入 JSON）；null = 不限制。
        """
        from ..assistant.runner import DEFAULT_BUDGET, _NULLABLE_BUDGET_KEYS

        effective = dict(DEFAULT_BUDGET)
        for k, v in (obj.assistant_budget or {}).items():
            if k in DEFAULT_BUDGET:
                if v is None and k not in _NULLABLE_BUDGET_KEYS:
                    continue
                effective[k] = v
        return {k: (str(v) if isinstance(v, Decimal) else v) for k, v in effective.items()}
