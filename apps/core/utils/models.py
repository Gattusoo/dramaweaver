"""模型基类。

CoreModel 为新增模型提供统一的 created_at / updated_at；现有业务模型字段不动
（本期不改字段，design Non-Goals），仅在后续新增模型时继承。
"""
from django.db import models


class CoreModel(models.Model):
    """抽象基类：统一的创建/更新时间戳。"""

    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        abstract = True
