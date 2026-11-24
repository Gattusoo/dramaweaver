"""智能体配方：AgentConfig（生产配方模板，被 Play 引用）。"""
from django.conf import settings
from django.db import models

from .play import AuditMode, ConsistencyMode, RedoGranularity


class AgentConfig(models.Model):
    name = models.CharField("配方名称", max_length=200)
    description = models.TextField("描述", blank=True)
    llm_model = models.ForeignKey(
        "ai_gateway.AIModel", on_delete=models.SET_NULL, null=True, blank=True, related_name="llm_configs"
    )
    tts_model = models.ForeignKey(
        "ai_gateway.AIModel", on_delete=models.SET_NULL, null=True, blank=True, related_name="tts_configs"
    )
    image_model = models.ForeignKey(
        "ai_gateway.AIModel", on_delete=models.SET_NULL, null=True, blank=True, related_name="image_configs"
    )
    video_model = models.ForeignKey(
        "ai_gateway.AIModel", on_delete=models.SET_NULL, null=True, blank=True, related_name="video_configs"
    )
    llm_failover = models.JSONField("LLM降级链", default=list, blank=True)
    tts_failover = models.JSONField("TTS降级链", default=list, blank=True)
    image_failover = models.JSONField("图像降级链", default=list, blank=True)
    video_failover = models.JSONField("视频降级链", default=list, blank=True)
    # 创作助手（assistant/session）：为空时回退 llm_model / llm_failover；须支持工具调用
    assistant_model = models.ForeignKey(
        "ai_gateway.AIModel", on_delete=models.SET_NULL, null=True, blank=True, related_name="assistant_configs"
    )
    assistant_failover = models.JSONField("助手降级链", default=list, blank=True)
    # 成本优化（主计划 3.3.2/3.3.3）：{small, medium, large} AIModel pk 三档；
    # 缺省空 dict 回退 assistant_model 单档。档位模型须 supports_tools。
    assistant_models = models.JSONField("多级助手模型", default=dict, blank=True)
    # {max_cost, max_iterations, max_tool_calls}
    assistant_budget = models.JSONField("助手预算", default=dict, blank=True)
    # 上下文压缩（Phase 1.3）：{reserve_tokens, keep_recent_tokens, auto_compact, context_window}
    assistant_compaction = models.JSONField("助手压缩策略", default=dict, blank=True)
    retry_count = models.PositiveIntegerField("重试次数", default=3)
    retry_backoff = models.JSONField("退避策略", default=dict)
    fallback_on_limit = models.BooleanField("限流时降级", default=True)
    default_audit_mode = models.CharField(
        "默认审核模式", max_length=20, choices=AuditMode.choices, default=AuditMode.KEY_CHECKPOINTS
    )
    default_redo_granularity = models.CharField(
        "默认重做粒度", max_length=20, choices=RedoGranularity.choices, default=RedoGranularity.SHOT
    )
    consistency_mode = models.CharField(
        "默认一致性方案", max_length=20, choices=ConsistencyMode.choices, default=ConsistencyMode.NONE
    )
    prompt_templates = models.JSONField("Prompt模板", default=dict)
    style_params = models.JSONField("风格参数", default=dict)
    is_template = models.BooleanField("系统预置模板", default=False)
    created_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="agent_configs"
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_agentconfig"
        verbose_name = "智能体配方"
        verbose_name_plural = "智能体配方"

    def __str__(self):
        return self.name
