from django.apps import AppConfig


class SystemConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core.system"
    label = "system"

    def ready(self):
        from . import signals  # noqa: F401  账号注销校验钩子（pre_delete）
