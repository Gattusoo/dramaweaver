# 加载 Celery app，使 Django 启动时任务被注册。
from .celery import app as celery_app

__all__ = ("celery_app",)
