"""角色级权限（平台管理员标记）。

平台管理端点族（供应商/渠道/统计/审核通道等）的入口权限，判定读
is_platform_admin 标记（全局二元身份）。剧内业务内容的访问权在
apps.creation.permissions（成员表三层判定），与全局身份无关。
"""
from rest_framework import permissions


def _is_authenticated(request):
    return bool(request.user and request.user.is_authenticated)


def is_platform_admin(user):
    """平台管理员判定：标记位（superuser 保持 Django 语义并入标记）。"""
    return bool(user and getattr(user, "is_platform_admin", False))


class IsAdminOnly(permissions.BasePermission):
    """仅平台管理员可访问。"""

    message = "仅平台管理员可执行此操作。"

    def has_permission(self, request, view):
        return _is_authenticated(request) and is_platform_admin(request.user)

    def has_object_permission(self, request, view, obj):
        return _is_authenticated(request) and is_platform_admin(request.user)
