"""认证端点：注册 / 登录 / 刷新 / 登出 / 当前用户 / 平台管理员标记。

注册接口接入 Django 密码强度校验（AUTH_PASSWORD_VALIDATORS）。
全局四级角色已退役（2026-11-16）：身份只分平台管理员（is_platform_admin）与普通用户。
"""
from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.exceptions import TokenError
from rest_framework_simplejwt.tokens import RefreshToken
from rest_framework_simplejwt.views import (
    TokenObtainPairView,
    TokenRefreshView,
)

from ..models import CustomUser
from ..permissions import IsAdminOnly


def _user_payload(user):
    return {
        "id": user.pk,
        "username": user.username,
        "email": user.email,
        "is_platform_admin": bool(getattr(user, "is_platform_admin", False)),
        "first_name": user.first_name,
        "last_name": user.last_name,
    }


def _tokens_for(user):
    refresh = RefreshToken.for_user(user)
    return {
        "access": str(refresh.access_token),
        "refresh": str(refresh),
    }


class RegisterView(APIView):
    """POST /api/auth/register/ — 注册新用户并返回 token。

    默认角色为 viewer；仅当调用者是管理员时才允许指定 role。
    password 须通过 Django 密码强度校验，不通过返回 400 且不创建用户。
    """

    permission_classes = [AllowAny]

    def post(self, request):
        username = request.data.get("username", "").strip()
        password = request.data.get("password", "")
        email = request.data.get("email", "").strip()
        if not username or not password:
            return Response(
                {"detail": "username 和 password 为必填项"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        # 密码强度校验（不通过不创建用户）
        try:
            validate_password(password, user=CustomUser(username=username))
        except DjangoValidationError as exc:
            return Response(
                {"detail": "密码强度不足", "errors": {"password": list(exc.messages)}},
                status=status.HTTP_400_BAD_REQUEST,
            )

        if CustomUser.objects.filter(username=username).exists():
            return Response(
                {"detail": "用户名已存在"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        user = CustomUser.objects.create_user(
            username=username, password=password, email=email
        )
        return Response(
            {"user": _user_payload(user), **_tokens_for(user)},
            status=status.HTTP_201_CREATED,
        )


class LoginView(TokenObtainPairView):
    """POST /api/auth/login/ — 验证并返回 token 及用户信息。"""

    def post(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        try:
            serializer.is_valid(raise_exception=True)
        except TokenError as exc:
            return Response(
                {"detail": str(exc)}, status=status.HTTP_401_UNAUTHORIZED
            )
        user = serializer.user
        return Response({**serializer.validated_data, "user": _user_payload(user)})


class RefreshView(TokenRefreshView):
    """POST /api/auth/refresh/ — 刷新 access token。"""


class LogoutView(APIView):
    """POST /api/auth/logout/ — 将 refresh token 加入黑名单。"""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        refresh = request.data.get("refresh")
        if not refresh:
            return Response(
                {"detail": "refresh token 为必填项"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        try:
            token = RefreshToken(refresh)
            token.blacklist()
        except TokenError:
            return Response(
                {"detail": "token 无效或已失效"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        return Response({"detail": "已登出"})


class MeView(APIView):
    """GET /api/auth/me/ — 返回当前登录用户信息。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(_user_payload(request.user))


class PlatformAdminFlagView(APIView):
    """PUT /api/auth/users/{id}/platform-admin/ — 平台管理员标记管理。

    全局四级角色已退役（spec: auth/rbac-roles REMOVED 角色变更）：
    普通用户的全局角色变更端点不存在；本端点只管理 is_platform_admin 标记，
    并保留"不能移除最后一个平台管理员"守卫（迁移自原 ChangeRoleView）。
    """

    permission_classes = [IsAdminOnly]

    def put(self, request, pk):
        try:
            user = CustomUser.objects.get(pk=pk)
        except CustomUser.DoesNotExist:
            return Response(
                {"detail": "用户不存在"},
                status=status.HTTP_404_NOT_FOUND,
            )
        flag = request.data.get("is_platform_admin")
        if not isinstance(flag, bool):
            return Response(
                {"detail": "is_platform_admin 必填（布尔）"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if user.is_platform_admin and not flag:
            # 不能移除最后一个有效平台管理员，否则无人能再管理平台
            others = CustomUser.objects.filter(
                is_platform_admin=True, is_active=True,
            ).exclude(pk=user.pk)
            if not others.exists():
                return Response(
                    {"detail": "不能移除最后一个平台管理员"},
                    status=status.HTTP_400_BAD_REQUEST,
                )
        user.is_platform_admin = flag
        user.save(update_fields=["is_platform_admin"])
        return Response(_user_payload(user))
