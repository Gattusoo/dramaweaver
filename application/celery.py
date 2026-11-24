"""Celery 应用入口（broker/backend = Redis 或本地内存）。

无参 ``autodiscover_tasks()`` 走 Django fixup，遍历 ``INSTALLED_APPS`` 加载各应用的
``tasks.py``；``apps.creation.tasks`` 末尾已显式导入 ``assistant.runner``，
故 ``shortplay.assistant.run_assistant_turn`` 一并注册。
Celery 任务的显式 ``name="shortplay.tasks.xxx"`` 保持不变（消息路由键与模块路径解耦）。

注意：``find_related_module`` 不递归，切勿传 ``["apps"]`` 这类父包——那只会查找
``apps.tasks`` 而漏掉深层包中的任务。
"""
import os

from celery import Celery

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "application.settings")

app = Celery("application")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
