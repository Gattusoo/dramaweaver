"""系统健康端点（工作台 Stage C2 任务 7.3）。

GET /api/health/ —— 认证用户可读（本机创作工作台的自查入口；
shared 下同样可用，报告按当前配置如实输出）。

脱敏契约：布尔/计数/版本号，无路径、无密钥、无完整 URL。
"""
from rest_framework import permissions
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from ..pipeline.health import health_report


@api_view(["GET"])
@permission_classes([permissions.IsAuthenticated])
def system_health(request):
    return Response(health_report())
