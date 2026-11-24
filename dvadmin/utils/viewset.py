"""ViewSet 薄基类。"""
from rest_framework import viewsets


class CustomModelViewSet(viewsets.ModelViewSet):
    """业务 ViewSet 的统一基类。

    本期仅作为占位与后续扩展点（统一鉴权、统一响应在渲染层完成）。
    """
