"""ai_gateway models：AIProvider / AIModel / CostRecord。

CostRecord 反向引用 creation 的 Play / TaskRecord，用字符串外键。
"""
from django.conf import settings
from django.db import models


class ProviderType(models.TextChoices):
    LLM = "LLM", "语言模型"
    TTS = "TTS", "语音合成"
    IMAGE = "IMAGE", "图像生成"
    VIDEO = "VIDEO", "视频合成"


class ModelType(models.TextChoices):
    LLM = "LLM", "语言模型"
    TTS = "TTS", "语音合成"
    IMAGE = "IMAGE", "图像生成"
    VIDEO = "VIDEO", "视频合成"


class CostCurrency(models.TextChoices):
    CNY = "CNY", "人民币"
    USD = "USD", "美元"


class ProviderVisibility(models.TextChoices):
    PLATFORM = "platform", "平台内置"  # 管理员配置；可指内网（白名单）；可携带平台 Key
    CUSTOM = "custom", "用户自建"      # 用户创建；强制公网 HTTPS；不携带平台 Key（用 UserProviderCredential）


class AIProvider(models.Model):
    name = models.CharField("供应商名称", max_length=100)
    provider_type = models.CharField(
        "类型", max_length=20, choices=ProviderType.choices
    )
    adapter_type = models.CharField(
        "适配器类型", max_length=50, blank=True,
        help_text="适配器实现标识，如 openai / deepseek / qwen / claude / dall_e / "
                  "sd_comfyui / flux / cosyvoice / azure_tts / kling / hailuo",
    )
    base_url = models.URLField("API地址")
    api_key_encrypted = models.TextField(
        "平台API密钥", blank=True, default="",
        help_text=(
            "仅平台内置（visibility=platform）供应商可携带的平台付费 Key；"
            "用户自建（visibility=custom）行不应有值，用户 Key 落 "
            "UserProviderCredential.encrypted_key。"
        ),
    )
    visibility = models.CharField(
        "可见性", max_length=20, choices=ProviderVisibility.choices,
        default=ProviderVisibility.PLATFORM, db_index=True,
        help_text=(
            "platform=平台内置（管理员配置，可指内网，可带平台 Key）；"
            "custom=用户自建（仅公网 HTTPS，Key 走 UserProviderCredential）"
        ),
    )
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="custom_providers",
        help_text="visibility=custom 时的属主；platform 行为空",
    )
    is_active = models.BooleanField("是否启用", default=True)
    priority = models.PositiveIntegerField("降级优先级", default=99)
    rate_limit = models.JSONField("限流配置", default=dict)
    timeout_seconds = models.PositiveIntegerField("超时(秒)", default=60)
    retry_count = models.PositiveIntegerField("重试次数", default=3)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_aiprovider"
        ordering = ["provider_type", "priority"]
        verbose_name = "AI供应商"
        verbose_name_plural = "AI供应商"
        indexes = [
            models.Index(fields=["visibility", "is_active"], name="idx_aiprovider_vis_active"),
        ]

    def __str__(self):
        return f"{self.name} ({self.provider_type}/{self.visibility})"


class UserProviderCredential(models.Model):
    """用户对某 AIProvider 的私有凭证（API Key 加密落库）。

    - 属主私有：他人（含剧内成员）不可见；属主也不能读成员的凭证
    - 接口只写：POST/PUT 接收明文，GET 仅回 key_last4 + label（决策 6）
    - 加密：dvadmin.utils.encryption（Fernet，SECRET_KEY 派生密钥）
    - UNIQUE(user, provider)：同一用户对同一供应商一条凭证（多 Key 后续再扩）
    """

    ENCRYPTION_PURPOSE = "user_provider_credential"

    user = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="provider_credentials"
    )
    provider = models.ForeignKey(
        AIProvider, on_delete=models.CASCADE, related_name="user_credentials"
    )
    label = models.CharField("备注标签", max_length=100, blank=True, default="")
    encrypted_key = models.TextField("加密API密钥")
    is_active = models.BooleanField("是否启用", default=True)
    last_verified_at = models.DateTimeField("最近验证时间", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_userprovidercredential"
        verbose_name = "用户供应商凭证"
        verbose_name_plural = "用户供应商凭证"
        constraints = [
            models.UniqueConstraint(
                fields=["user", "provider"], name="uniq_user_provider_credential"
            ),
        ]
        indexes = [
            models.Index(fields=["user", "is_active"], name="idx_upc_user_active"),
        ]

    def __str__(self):
        return f"{self.user_id}@{self.provider_id} ({self.label or 'default'})"

    # ── 加密读写（避免明文落审计/日志） ──────────────────────────────────────
    def set_key(self, plain: str) -> None:
        from dvadmin.utils.encryption import encrypt_value
        self.encrypted_key = encrypt_value(plain, purpose=self.ENCRYPTION_PURPOSE)

    def get_key(self) -> str:
        from dvadmin.utils.encryption import decrypt_value
        return decrypt_value(self.encrypted_key, purpose=self.ENCRYPTION_PURPOSE)

    @property
    def key_last4(self) -> str:
        """解密后取末 4 位（仅展示用）；空 Key 返回空串。"""
        try:
            plain = self.get_key()
        except Exception:  # noqa: BLE001 — 密文损坏时不出 500
            return ""
        return plain[-4:] if len(plain) >= 4 else ""


class AIModel(models.Model):
    provider = models.ForeignKey(
        AIProvider, on_delete=models.CASCADE, related_name="models"
    )
    model_name = models.CharField("模型标识", max_length=200)
    display_name = models.CharField("显示名称", max_length=200)
    model_type = models.CharField("类型", max_length=20, choices=ModelType.choices)
    capabilities = models.JSONField("能力描述", default=dict)
    cost_config = models.JSONField("成本配置", default=dict)
    is_active = models.BooleanField("是否启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_aimodel"
        verbose_name = "AI模型"
        verbose_name_plural = "AI模型"

    def __str__(self):
        return f"{self.display_name} ({self.provider.name})"


class CostRecord(models.Model):
    play = models.ForeignKey(
        "creation.Play", on_delete=models.CASCADE, related_name="cost_records"
    )
    task_record = models.ForeignKey(
        "creation.TaskRecord", on_delete=models.SET_NULL, null=True, blank=True
    )
    provider = models.ForeignKey(AIProvider, on_delete=models.CASCADE)
    model = models.CharField("模型名", max_length=200)
    provider_type = models.CharField("供应商类型", max_length=20, choices=ProviderType.choices)
    usage = models.JSONField("用量", default=dict)
    unit_cost = models.DecimalField("单价", max_digits=12, decimal_places=8)
    total_cost = models.DecimalField("总费用", max_digits=12, decimal_places=6)
    currency = models.CharField("货币", max_length=10, choices=CostCurrency.choices, default=CostCurrency.CNY)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_costrecord"
        indexes = [models.Index(fields=["play", "provider", "model"], name="idx_cost_play_provider_model")]
        ordering = ["-created_at"]
        verbose_name = "费用记录"
        verbose_name_plural = "费用记录"

    def __str__(self):
        return f"{self.play} - {self.provider.name} - {self.total_cost} {self.currency}"


class SmartCache(models.Model):
    """模型响应缓存（主计划 3.3.4–3.3.6 成本优化）。

    只覆盖纯文本对话（LLM/CHAT 可重放）；IMAGE/TTS/VIDEO 受理即计费不缓存。
    cache_key = SHA-256(规范化 model+messages+system)；TTL 惰性判定（读时），
    写时顺带清理过期行。命中计数供成本优化报告消费。
    """

    model_id = models.PositiveIntegerField("模型ID")
    model_name = models.CharField("模型名", max_length=200)
    cache_key = models.CharField("缓存键", max_length=64, unique=True)
    payload_digest = models.CharField("请求摘要", max_length=64, blank=True)
    cache_value = models.JSONField("缓存响应")
    hit_count = models.PositiveIntegerField("命中次数", default=0)
    last_hit_at = models.DateTimeField("最近命中", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_smartcache"
        indexes = [models.Index(fields=["model_id", "created_at"], name="idx_smartcache_model_created")]
        verbose_name = "模型响应缓存"
        verbose_name_plural = "模型响应缓存"

    def __str__(self):
        return f"cache:{self.cache_key[:12]} hits={self.hit_count}"
