"""ASGI config for the application project.

Phase 3.2（backend-realtime-events D6）：ProtocolTypeRouter 组装 HTTP + WS。
- HTTP：get_asgi_application()（与 WSGI 行为一致）
- WS：仅 ENABLE_REALTIME_EVENTS 开启时挂载 ws 路由（channels 仅作 protocol router，
  不用 layer/worker）。开关关闭时不挂 ws 路由——ASGI 服务器下 ws 连接 404，
  WSGI 部署本就无 WS 路由，行为一致。
channels 导入失败时抛明确错误（残缺环境），不静默降级为无 WS。
"""
import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "application.settings")

application = get_asgi_application()

# Django 应用须先初始化（ORM/auth 可用）再挂 WS 路由
try:
    from channels.routing import ProtocolTypeRouter, URLRouter
    from channels.security.websocket import AllowedHostsOriginValidator
except ImportError as exc:  # 残缺环境：channels 未安装却以 ASGI 启动
    raise ImportError(
        "ASGI 启动需要 channels（uv sync 安装依赖）；若不需 WS 请用 WSGI 启动"
    ) from exc


def _build_ws_router():
    from django.conf import settings
    if not getattr(settings, "ENABLE_REALTIME_EVENTS", False):
        return None
    from apps.creation.assistant.consumers import AssistantConsumer
    from django.urls import path
    return URLRouter([
        path("api/assistant/sessions/<int:session_id>/ws/", AssistantConsumer.as_asgi()),
    ])


_ws_router = _build_ws_router()
if _ws_router is not None:
    application = ProtocolTypeRouter({
        "http": application,
        "websocket": AllowedHostsOriginValidator(_ws_router),
    })
# 开关关闭：纯 HTTP ASGI（与 WSGI 行为一致，无 WS 路由）
