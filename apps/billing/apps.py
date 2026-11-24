from django.apps import AppConfig


class BillingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.billing"
    label = "billing"
    verbose_name = "计费"

    def ready(self):
        # 注册赠金信号（post_save on CustomUser create）
        from . import signals  # noqa: F401
