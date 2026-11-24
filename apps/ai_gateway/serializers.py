"""ai_gateway serializers。"""
from django.core.exceptions import ValidationError
from rest_framework import serializers

from .models import AIModel, AIProvider, CostRecord, ProviderVisibility, UserProviderCredential
from .security import validate_base_url


class AIModelSerializer(serializers.ModelSerializer):
    class Meta:
        model = AIModel
        fields = [
            "id", "provider", "model_name", "display_name", "model_type",
            "capabilities", "cost_config", "is_active", "created_at",
        ]
        read_only_fields = ["created_at"]


class AIModelCreationSerializer(serializers.ModelSerializer):
    """创作者可读的创作模型轻量视图。

    只暴露 id / display_name / model_type / capabilities（含 gen_params）；
    不含 cost_config / model_name / provider —— 定价与内部标识属管理敏感数据。
    """

    class Meta:
        model = AIModel
        fields = ["id", "display_name", "model_type", "capabilities"]


class AIProviderSerializer(serializers.ModelSerializer):
    models = AIModelSerializer(many=True, read_only=True)
    has_key = serializers.SerializerMethodField()

    class Meta:
        model = AIProvider
        fields = [
            "id", "name", "provider_type", "adapter_type", "base_url", "api_key_encrypted",
            "has_key", "visibility", "created_by", "is_active", "priority",
            "rate_limit", "timeout_seconds", "retry_count", "models", "created_at",
        ]
        read_only_fields = ["created_at", "created_by"]
        extra_kwargs = {
            "adapter_type": {
                "help_text": (
                    "适配器实现标识，如 openai / deepseek / claude / dall_e / seedream / flux / "
                    "cosyvoice / openai_tts / volcengine_doubao / minimax_tts / edge_tts / "
                    "kling / hailuo，或自定义 custom_openai(_image/_tts)。"
                    "填好 base_url + api_key 后可用 GET .../providers/{id}/models/ 拉取模型列表。"
                ),
            },
            "base_url": {
                "help_text": (
                    "API 地址。MiniMax（海螺 TTS / 视频）需在 query 带 GroupId，"
                    "如 https://api.minimax.chat?GroupId=xxx；其余为标准根地址"
                    "（如 https://api.deepseek.com/v1、https://api.klingai.com）。"
                    "仅允许 http/https 公网地址；本地模型（Ollama/ComfyUI）需设"
                    " AI_GATEWAY_ALLOW_LOCAL_BASE_URL=True（仅管理员可配 platform 行内网）。"
                ),
            },
            "api_key_encrypted": {
                "write_only": True,  # GET 不回显明文 key，只透出 has_key
                "help_text": (
                    "API 密钥（仅 platform 行可写）。可灵（kling）约定存 'access_key:secret_key'"
                    "（冒号分隔）用于签 JWT；其余供应商为普通 api_key。custom 行不应携带。"
                ),
            },
            "visibility": {
                "help_text": (
                    "platform=平台内置（仅管理员可创建/修改，可指内网，可携带平台 Key）；"
                    "custom=用户自建（任何已认证用户可创建，强制公网 HTTPS，Key 走"
                    " UserProviderCredential，本字段必须为空）。"
                ),
            },
        }

    def validate_base_url(self, value):
        ok, reason = validate_base_url(value)
        if not ok:
            raise ValidationError(reason)
        return value

    def validate(self, attrs):
        visibility = attrs.get("visibility") or getattr(self.instance, "visibility", ProviderVisibility.PLATFORM)
        api_key = attrs.get("api_key_encrypted")
        base_url = attrs.get("base_url") or getattr(self.instance, "base_url", "")
        request = self.context.get("request")
        user = getattr(request, "user", None) if request else None
        # 平台管理员标记：项目用 CustomUser.is_platform_admin（非 Django is_staff）
        is_admin = bool(user and getattr(user, "is_platform_admin", False))

        # visibility=custom 行不允许携带平台 Key（Key 走 UserProviderCredential）
        if visibility == ProviderVisibility.CUSTOM and api_key:
            raise ValidationError({
                "api_key_encrypted": "custom 行不携带平台 Key；请使用 UserProviderCredential。"
            })

        # visibility=platform 行仅管理员可写；普通用户创建/修改 platform 行一律拒绝
        if visibility == ProviderVisibility.PLATFORM and not is_admin:
            raise ValidationError({
                "visibility": "platform 行仅平台管理员可创建/修改。"
            })

        # custom 行强制公网 HTTPS（user 自建不可指内网，即便设了 AI_GATEWAY_ALLOW_LOCAL_BASE_URL）
        if visibility == ProviderVisibility.CUSTOM and base_url:
            from urllib.parse import urlsplit
            parts = urlsplit(str(base_url))
            if parts.scheme != "https":
                raise ValidationError({
                    "base_url": "custom 行仅允许 https 公网地址（http 与内网均不允许）。"
                })

        return attrs

    def create(self, validated_data):
        # custom 行 created_by 自动落当前用户；platform 行 created_by 留空
        visibility = validated_data.get("visibility", ProviderVisibility.PLATFORM)
        request = self.context.get("request")
        if visibility == ProviderVisibility.CUSTOM and request and request.user.is_authenticated:
            validated_data["created_by"] = request.user
        return super().create(validated_data)

    def get_has_key(self, obj) -> bool:
        return bool(obj.api_key_encrypted)


class UserProviderCredentialSerializer(serializers.ModelSerializer):
    """用户凭证序列化器：写接收明文（api_key 字段），读只回 key_last4 + label。

    - 永远不回显明文 Key（含 platform 管理员，决策 6）
    - 属主私有：queryset 在 view 层按 request.user 过滤；他人不可见
    - verify 动作见 UserProviderCredentialViewSet.verify()
    """

    api_key = serializers.CharField(
        write_only=True, required=False, allow_blank=False, trim_whitespace=True,
        help_text="API 密钥（write-only，仅创建/更新时提交；读取只回 key_last4）。",
    )
    key_last4 = serializers.SerializerMethodField()
    provider_name = serializers.CharField(source="provider.name", read_only=True)

    class Meta:
        model = UserProviderCredential
        fields = [
            "id", "user", "provider", "provider_name", "label",
            "api_key", "key_last4", "is_active",
            "last_verified_at", "created_at", "updated_at",
        ]
        read_only_fields = ["user", "key_last4", "last_verified_at", "created_at", "updated_at"]

    def get_key_last4(self, obj) -> str:
        return obj.key_last4

    def validate_provider(self, provider):
        # 只能给 visibility=custom 的供应商配凭证（platform 行走平台 Key，无需用户凭证）
        if provider.visibility != ProviderVisibility.CUSTOM:
            raise ValidationError(
                "仅 visibility=custom 的供应商支持用户凭证；"
                "platform 行的 Key 由平台维护。"
            )
        return provider

    def create(self, validated_data):
        plain = validated_data.pop("api_key", None)
        if not plain:
            raise ValidationError({"api_key": "创建凭证必须提供 api_key。"})
        request = self.context["request"]
        validated_data["user"] = request.user
        instance = UserProviderCredential(**validated_data)
        instance.set_key(plain)
        instance.save()
        return instance

    def update(self, instance, validated_data):
        plain = validated_data.pop("api_key", None)
        for attr, value in validated_data.items():
            setattr(instance, attr, value)
        if plain:
            instance.set_key(plain)
        instance.save()
        return instance


class UserProviderCredentialVerifySerializer(serializers.Serializer):
    """verify 端点的响应序列化（不发请求体；POST 空 body 触发一次最小验证）。"""

    ok = serializers.BooleanField(read_only=True)
    reason = serializers.CharField(read_only=True)
    last_verified_at = serializers.DateTimeField(read_only=True)


class CostRecordSerializer(serializers.ModelSerializer):
    class Meta:
        model = CostRecord
        fields = [
            "id", "play", "task_record", "provider", "model", "provider_type",
            "usage", "unit_cost", "total_cost", "currency", "created_at",
        ]
        read_only_fields = ["created_at"]
