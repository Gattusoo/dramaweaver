"""AI Gateway 管理端点与配置 ViewSet。"""
import logging
import time
from datetime import timedelta

from django.db.models import Avg, Count, DurationField, ExpressionWrapper, F, Sum
from django.utils import timezone
from rest_framework import status, viewsets
from rest_framework.decorators import action, api_view, permission_classes
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from dvadmin.system.permissions import IsAdminOnly
from dvadmin.utils.pagination import CustomPagination

from apps.creation.models import TaskRecord
from apps.creation.views.base import PlayScopedViewSet

from django.db import models as dj_models

from .adapters import build_adapter
from .circuit_breaker import CircuitBreaker, CLOSED, OPEN, HALF_OPEN
from .gateway import read_failover_events
from .http import safe_request
from .models import AIProvider, AIModel, CostRecord
from .rate_limiter import RateLimiter
from .redis_client import get_store
from .router import supports_tools
from .security import classify_test_error
from .serializers import (
    AIModelCreationSerializer, AIModelSerializer, AIProviderSerializer, CostRecordSerializer,
)

logger = logging.getLogger(__name__)


# ─── 配置 ViewSet ────────────────────────────────────────────────────────────

class AIProviderViewSet(viewsets.ModelViewSet):
    """AIProvider 行级权限视图：

    - list/retrieve：管理员看全部；普通用户看 platform 行 + 自己创建的 custom 行
    - create：管理员可创建 platform/custom；普通用户仅可创建 custom（serializer 校验）
    - update/partial_update/destroy：管理员任意；普通用户仅限自己创建的 custom 行
    """

    serializer_class = AIProviderSerializer
    permission_classes = [IsAuthenticated]
    queryset = AIProvider.objects.all()  # 供 router basename 推导；实际过滤在 get_queryset

    def get_queryset(self):
        user = self.request.user
        qs = AIProvider.objects.all()
        if user and getattr(user, "is_platform_admin", False):
            return qs
        # 普通用户：platform 行（可读目录）+ 自己的 custom 行
        return qs.filter(
            dj_models.Q(visibility="platform") | dj_models.Q(created_by=user)
        )

    def _check_row_permission(self, instance):
        user = self.request.user
        if user and getattr(user, "is_platform_admin", False):
            return
        # 普通用户只能改自己的 custom 行
        if instance.visibility != "custom" or instance.created_by_id != user.id:
            from rest_framework.exceptions import PermissionDenied
            raise PermissionDenied("仅可管理自己创建的 custom 供应商；platform 行由平台管理员维护。")

    def perform_update(self, serializer):
        self._check_row_permission(self.get_object())
        serializer.save()

    def perform_destroy(self, instance):
        self._check_row_permission(instance)
        instance.delete()


class UserProviderCredentialViewSet(viewsets.ModelViewSet):
    """用户凭证 CRUD + verify。

    - queryset 按 request.user 过滤（属主私有，他人不可见）
    - GET 永不回显明文 Key（serializer 仅回 key_last4）
    - POST /api/user-credentials/{id}/verify/ 触发一次最小请求验证凭证可用性，
      并回写 last_verified_at（无论成败）
    """

    serializer_class = None  # 延迟到 get_serializer_class
    permission_classes = [IsAuthenticated]
    http_method_names = ["get", "post", "put", "patch", "delete", "head", "options"]

    def get_serializer_class(self):
        from .serializers import UserProviderCredentialSerializer
        return UserProviderCredentialSerializer

    def get_queryset(self):
        from .models import UserProviderCredential
        return UserProviderCredential.objects.filter(user=self.request.user).select_related("provider")

    def perform_create(self, serializer):
        serializer.save()  # user 在 serializer.create 内自 request.user 取

    @action(detail=True, methods=["post"])
    def verify(self, request, pk=None):
        """对该凭证做一次最小请求（默认 GET provider.base_url）验证可用性。

        无论成败都回写 last_verified_at；失败文案脱敏（不回显响应体/异常串）。
        """
        from django.utils import timezone

        cred = self.get_object()
        ok = False
        reason = ""
        try:
            plain = cred.get_key()
            if not plain:
                reason = "凭证为空或解密失败"
            else:
                # 最小请求：对 base_url 发 GET，超时 10s
                # 401/403 视为"凭证无效"；其他 4xx/5xx 视为"端点可达但业务异常"
                resp = safe_request(cred.provider, "GET", cred.provider.base_url, timeout=10,
                                    headers={"Authorization": f"Bearer {plain}"})
                if resp.status_code in (401, 403):
                    reason = "鉴权失败（API Key 无效或无权限）"
                elif resp.status_code >= 500:
                    reason = f"目标服务返回 HTTP {resp.status_code}"
                else:
                    ok = True
                    reason = "ok"
        except Exception as exc:  # noqa: BLE001 — 脱敏后返回
            reason = classify_test_error(exc)

        cred.last_verified_at = timezone.now()
        cred.save(update_fields=["last_verified_at", "updated_at"])
        return Response({
            "ok": ok,
            "reason": reason,
            "last_verified_at": cred.last_verified_at,
        })


class AIModelViewSet(viewsets.ModelViewSet):
    serializer_class = AIModelSerializer
    queryset = AIModel.objects.all()
    permission_classes = [IsAdminOnly]


class CostRecordViewSet(PlayScopedViewSet):
    serializer_class = CostRecordSerializer
    queryset = CostRecord.objects.all()
    http_method_names = ["get", "head", "options"]


# ─── 看板端点 ────────────────────────────────────────────────────────────────

def _provider_status(provider, breaker, limiter):
    state = breaker.state(provider.pk)
    healthy = state != OPEN
    since = timezone.now() - timedelta(hours=24)

    tasks = TaskRecord.objects.filter(
        ai_provider=provider, created_at__gte=since
    )
    total_calls = tasks.count()
    success_calls = tasks.filter(status="SUCCESS").count()
    latency_expr = ExpressionWrapper(
        F("completed_at") - F("started_at"), output_field=DurationField()
    )
    avg_latency = tasks.filter(
        started_at__isnull=False, completed_at__isnull=False
    ).aggregate(avg=Avg(latency_expr))["avg"]
    avg_latency_ms = int(avg_latency.total_seconds() * 1000) if avg_latency else 0
    total_cost = CostRecord.objects.filter(
        provider=provider, created_at__gte=since
    ).aggregate(total=Sum("total_cost"))["total"] or 0

    rate_limit = provider.rate_limit or {}
    tool_models = [m.model_name for m in provider.models.filter(is_active=True) if supports_tools(m)]
    return {
        "id": provider.pk,
        "name": provider.name,
        "type": provider.provider_type,
        "state": state,
        "healthy": healthy,
        "supports_tools": bool(tool_models),
        "tool_models": tool_models,
        "stats": {
            "total_calls": total_calls,
            "success_rate": round(success_calls / total_calls, 4) if total_calls else 0,
            "avg_latency_ms": avg_latency_ms,
            "total_cost": float(total_cost),
        },
        "rate_limit": {
            "current_rpm": limiter.current_rpm(provider.pk),
            "max_rpm": rate_limit.get("max_rpm", 0),
            "current_concurrent": limiter.current_concurrent(provider.pk),
            "max_concurrent": rate_limit.get("max_concurrent", 0),
        },
    }


@api_view(["GET"])
@permission_classes([IsAdminOnly])
def supported_adapters(request):
    """GET /api/ai-gateway/adapters/ — 列出可选供应商/适配器类型。"""
    from .adapters import SUPPORTED_ADAPTERS

    items = [
        {"adapter_type": key, "label": label}
        for key, label in SUPPORTED_ADAPTERS.items()
    ]
    return Response({"count": len(items), "adapters": items})


@api_view(["GET"])
@permission_classes([IsAuthenticated])
def creation_models(request):
    """GET /api/ai-models/creation/ — 创作者可读的创作模型列表。

    仅返回 is_active=True 且 capabilities.gen_params 非空的模型（声明了创作参数
    选项的生成类模型），轻量序列化不含 cost_config / provider。管理端点
    AIModelViewSet（IsAdminOnly）保持不变。
    """
    qs = (
        AIModel.objects.filter(is_active=True, capabilities__has_key="gen_params")
        .order_by("id")
    )
    # JSON key 查询在各 DB 上对空 dict / 非 dict 的语义不完全一致，Python 侧再滤一遍。
    models = [
        m for m in qs
        if isinstance(m.capabilities.get("gen_params"), dict) and m.capabilities["gen_params"]
    ]
    paginator = CustomPagination()
    page = paginator.paginate_queryset(models, request)
    serializer = AIModelCreationSerializer(page, many=True)
    return paginator.get_paginated_response(serializer.data)


@api_view(["GET"])
@permission_classes([IsAdminOnly])
def ai_gateway_status(request):
    """GET /api/ai-gateway/status/ — Provider 健康看板。"""
    breaker = CircuitBreaker(store=get_store())
    limiter = RateLimiter(store=get_store())
    providers = AIProvider.objects.filter(is_active=True).order_by("provider_type", "priority")
    payload = [_provider_status(p, breaker, limiter) for p in providers]
    since_ts = time.time() - 24 * 3600
    events = read_failover_events(store=get_store(), since=since_ts)
    return Response({"providers": payload, "failover_events_24h": events})


@api_view(["POST"])
@permission_classes([IsAdminOnly])
def provider_test(request, pk):
    """POST /api/ai-gateway/providers/{id}/test/ — 连通性测试。

    发起请求前先复验 base_url（防存量 SSRF），错误回显为分类文案不透出原始异常。"""
    from .security import classify_test_error, validate_base_url

    try:
        provider = AIProvider.objects.get(pk=pk)
    except AIProvider.DoesNotExist:
        return Response({"detail": "provider 不存在"}, status=status.HTTP_404_NOT_FOUND)

    ok, reason = validate_base_url(provider.base_url)
    if not ok:
        return Response({"detail": f"base_url 校验失败: {reason}"},
                        status=status.HTTP_400_BAD_REQUEST)

    start = time.time()
    tools_mode = request.data.get("mode") == "tools"
    try:
        adapter = build_adapter(provider)
        if provider.provider_type == "LLM" and tools_mode:
            model = next((m for m in provider.models.filter(is_active=True) if supports_tools(m)), None)
            if model is None:
                return Response({"success": False, "latency_ms": 0, "supports_tools": False,
                                 "error": "该供应商没有标记 supports_tools 的模型"})
            result = adapter.chat(
                [{"role": "user", "content": "调用 ping 工具"}], "You are a test.",
                [{"name": "ping", "description": "连通性测试", "input_schema": {"type": "object", "properties": {}}}],
                {"model": model.model_name, "max_tokens": 64},
            )
            return Response({"success": True, "latency_ms": int((time.time() - start) * 1000), "error": None,
                             "supports_tools": True, "stop_reason": result.get("stop_reason")})
        model = provider.models.filter(is_active=True).order_by("id").first()
        if provider.provider_type in ("LLM", "IMAGE") and model is None:
            return Response({"success": False, "latency_ms": 0, "error": "该供应商没有启用的模型"})
        if provider.provider_type in ("IMAGE", "VIDEO") and not request.data.get("confirm_cost"):
            return Response({"success": False, "latency_ms": 0, "requires_confirm": True,
                             "error": "图像/视频连通性测试会产生一次真实生成费用，请传 confirm_cost=true 确认"})
        if provider.provider_type == "LLM":
            adapter.generate("ping", "You are a test.", {"model": model.model_name, "max_tokens": 5})
        elif provider.provider_type == "TTS":
            adapter.synthesize("测试", "test_voice", {"duration_seconds": 0})
        elif provider.provider_type == "IMAGE":
            adapter.generate("ping", [], {"model": model.model_name})
        elif provider.provider_type == "VIDEO":
            adapter.image_to_video("https://example.com/ping.png", {"duration": 1}, wait=False)
        latency_ms = int((time.time() - start) * 1000)
        return Response({"success": True, "latency_ms": latency_ms, "error": None})
    except Exception as exc:  # noqa: BLE001 — 分类脱敏，不回显原始 str(exc)（防内网信息泄露）
        logger.warning("provider_test provider=%s 失败: %s", pk, exc)
        return Response(
            {"success": False, "latency_ms": int((time.time() - start) * 1000),
             "error": classify_test_error(exc)},
            status=status.HTTP_200_OK,
        )


# ─── 模型发现 ────────────────────────────────────────────────────────────────

@api_view(["GET"])
@permission_classes([IsAdminOnly])
def provider_models(request, pk):
    """GET /api/ai-gateway/providers/{id}/models/ — 拉取该供应商可用模型。

    LLM（OpenAI 兼容 / Claude）实时拉取；IMAGE/TTS/VIDEO 返回静态已知清单。
    始终可手动填 model_name 兜底。返回 {source, models, existing}。
    """
    try:
        provider = AIProvider.objects.get(pk=pk)
    except AIProvider.DoesNotExist:
        return Response({"detail": "provider 不存在"}, status=status.HTTP_404_NOT_FOUND)

    from .model_catalog import fetch_provider_models
    return Response(fetch_provider_models(provider))


@api_view(["POST"])
@permission_classes([IsAdminOnly])
def import_provider_models(request, pk):
    """POST /api/ai-gateway/providers/{id}/models/import/ — 批量导入模型为 AIModel。

    body {model_names: [...]}；缺省导入全部「尚未导入」的已知/实时模型。
    幂等：已存在的 model_name 跳过。返回 {imported, skipped}。
    """
    try:
        provider = AIProvider.objects.get(pk=pk)
    except AIProvider.DoesNotExist:
        return Response({"detail": "provider 不存在"}, status=status.HTTP_404_NOT_FOUND)

    model_names = request.data.get("model_names")
    if model_names is not None and not isinstance(model_names, list):
        return Response({"detail": "model_names 必须是列表"}, status=status.HTTP_400_BAD_REQUEST)

    from .model_catalog import import_provider_models as _import
    result = _import(provider, model_names)
    return Response(result)
