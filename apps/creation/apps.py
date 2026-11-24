from django.apps import AppConfig


class CreationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.creation"
    label = "creation"

    def ready(self):
        from .pipeline.dispatcher import connect_signals
        connect_signals()
        # Phase 1.7: 注册默认评估指标（ENABLE_EVALUATION 由各记录函数内部判定）
        try:
            from .assistant.evaluation import register_defaults
            register_defaults()
        except Exception:  # noqa: BLE001 — 评估注册失败不阻断启动
            import logging
            logging.getLogger(__name__).exception("eval指标注册失败")
