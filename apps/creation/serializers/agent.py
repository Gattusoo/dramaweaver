"""creation serializers —— 智能体配方。"""
from decimal import Decimal

from rest_framework import serializers

from apps.ai_gateway.credentials import is_model_usable_for_user
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
            from apps.core.system.permissions import is_platform_admin
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

    @staticmethod
    def _coerce_model_id_list(ids):
        """降级链类型校验 + 数字字符串归一化（兼容历史 JSON 数据）。

        历史四条链无字段级校验，存量行可能含 "5"；新校验不能把这类数据
        在 PUT 回传时直接判 400，统一归一化为 int 后落库。
        """
        if not isinstance(ids, list):
            raise serializers.ValidationError("应为 AIModel id 列表")
        normalized = []
        for i in ids:
            if isinstance(i, bool):
                raise serializers.ValidationError("应为 AIModel id 列表")
            if isinstance(i, int):
                normalized.append(i)
            elif isinstance(i, str) and i.isdigit():
                normalized.append(int(i))
            else:
                raise serializers.ValidationError("应为 AIModel id 列表")
        return normalized

    def validate_assistant_failover(self, ids):
        ids = self._coerce_model_id_list(ids)
        found = {m.pk: m for m in AIModel.objects.filter(pk__in=ids)}
        bad = [i for i in ids if i not in found or not supports_tools(found[i])]
        if bad:
            raise serializers.ValidationError(f"以下模型不存在或不支持工具调用: {bad}")
        return ids

    ASSISTANT_MODEL_TIERS = ("small", "medium", "large")

    def validate_assistant_models(self, value):
        """多级助手模型三档：{small|medium|large: AIModel pk 或 null}。

        未知键 400（拼错档位必须拒绝，不能静默剔除导致误以为生效）；
        null = PATCH 时删除该档（PUT 按整体替换语义处理）；
        档位模型须为启用 LLM、供应商启用，且对当前用户可用
        （platform 人人可用；custom 须属主有启用凭证）。
        small 档允许不支持工具调用的模型——运行期摘要/压缩路径以
        supports_tool_calls=False 消费 small；medium/large 仍须支持工具调用。
        """
        if not isinstance(value, dict):
            raise serializers.ValidationError("应为对象（{small|medium|large: 模型 id 或 null}）")
        unknown = [k for k in value if k not in self.ASSISTANT_MODEL_TIERS]
        if unknown:
            raise serializers.ValidationError(
                f"未知档位 {unknown}；仅接受 {list(self.ASSISTANT_MODEL_TIERS)}"
            )
        request = self.context.get("request")
        user = getattr(request, "user", None)
        for tier, mid in value.items():
            if mid is None:
                continue
            if isinstance(mid, bool) or not isinstance(mid, int):
                raise serializers.ValidationError(f"{tier} 档应为 AIModel id 或 null")
            model = AIModel.objects.select_related("provider").filter(pk=mid).first()
            if model is None:
                raise serializers.ValidationError(f"{tier} 档模型不存在: {mid}")
            if model.model_type != "LLM":
                raise serializers.ValidationError(
                    f"{tier} 档须为 LLM（当前 {model.display_name} 不满足）"
                )
            if tier != "small" and not supports_tools(model):
                raise serializers.ValidationError(
                    f"{tier} 档须为支持工具调用的 LLM（当前 {model.display_name} 不满足）"
                )
            if not model.is_active or not model.provider.is_active:
                raise serializers.ValidationError(f"{tier} 档模型或供应商已停用")
            if user is not None and user.is_authenticated:
                ok, reason = is_model_usable_for_user(user, model)
                if not ok:
                    raise serializers.ValidationError(f"{tier} 档{reason}")
        return value

    # 模型引用字段（attrs 键为 source 名）：创建/更新时逐个做「当前用户可用」校验
    _MODEL_SOURCE_FIELDS = (
        "llm_model", "tts_model", "image_model", "video_model", "assistant_model",
    )
    _FAILOVER_FIELDS = (
        "llm_failover", "tts_failover", "image_failover", "video_failover", "assistant_failover",
    )

    def _assert_model_usable(self, user, model, field_name):
        ok, reason = is_model_usable_for_user(user, model)
        if not ok:
            raise serializers.ValidationError({field_name: [reason]})

    def validate(self, attrs):
        """模型引用可用性：platform 模型人人可用；custom 模型须属主有启用凭证。

        覆盖主模型五字段与四条降级链；越权/缺凭证的引用在配置层直接拒绝，
        避免运行期被凭证链静默剔除造成「配置成功但不可用」。"""
        request = self.context.get("request")
        user = getattr(request, "user", None)
        if user is None or not user.is_authenticated:
            return attrs

        for source in self._MODEL_SOURCE_FIELDS:
            model = attrs.get(source)
            if model is not None:
                self._assert_model_usable(user, model, source + "_id")

        for field_name in self._FAILOVER_FIELDS:
            ids = attrs.get(field_name)
            if ids is None:
                continue
            try:
                ids = self._coerce_model_id_list(ids)
            except serializers.ValidationError as exc:
                # 对象级 validate 里的裸错误会落成 non_field_errors；
                # 降级链错误必须挂回对应字段，保持既有 API 契约。
                raise serializers.ValidationError({field_name: [exc.detail]}) from exc
            attrs[field_name] = ids
            if not ids:
                continue
            found = {
                m.pk: m
                for m in AIModel.objects.select_related("provider").filter(pk__in=ids)
            }
            missing = [i for i in ids if i not in found]
            if missing:
                raise serializers.ValidationError(
                    {field_name: [f"以下模型不存在: {missing}"]}
                )
            for mid in ids:
                self._assert_model_usable(user, found[mid], field_name)
        return attrs

    def update(self, instance, validated_data):
        """PATCH 时 assistant_models 按档合并；PUT 保持整体替换。

        合并语义对齐 docs/API.md：null 删除该档，未提交档位保持不变；
        显式传 {} 表示清空全部档位（回退单档），而不是 PATCH 空操作。
        """
        if self.partial and "assistant_models" in validated_data:
            incoming = validated_data["assistant_models"]
            if isinstance(incoming, dict):
                if not incoming:
                    validated_data["assistant_models"] = {}
                else:
                    merged = dict(instance.assistant_models or {})
                    for tier, mid in incoming.items():
                        if mid is None:
                            merged.pop(tier, None)
                        else:
                            merged[tier] = mid
                    validated_data["assistant_models"] = merged
        return super().update(instance, validated_data)

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
            "assistant_models",
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
