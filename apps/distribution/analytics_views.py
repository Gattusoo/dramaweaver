"""数据分析看板端点（/api/analytics/...）。"""
from rest_framework import status
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from dvadmin.system.permissions import IsAdminOnly

from apps.creation.models import Play

from .analytics import (
    cost_by_model, cost_by_provider, cost_optimization_report,
    play_cost_breakdown, production_stats, publication_summary,
)


@api_view(["GET"])
@permission_classes([IsAdminOnly])
def play_cost(request, pk):
    """GET /api/analytics/plays/{id}/cost/ — 按 Play 聚合费用。"""
    try:
        play = Play.objects.get(pk=pk)
    except Play.DoesNotExist:
        return Response({"detail": "短剧不存在"}, status=status.HTTP_404_NOT_FOUND)
    return Response(play_cost_breakdown(play))


@api_view(["GET"])
@permission_classes([IsAdminOnly])
def cost_provider(request):
    """GET /api/analytics/cost/by-provider/ — 按供应商分组。"""
    return Response(cost_by_provider(
        start_date=request.query_params.get("start_date"),
        end_date=request.query_params.get("end_date"),
    ))


@api_view(["GET"])
@permission_classes([IsAdminOnly])
def cost_model(request):
    """GET /api/analytics/cost/by-model/ — 按模型分组。"""
    return Response(cost_by_model(
        start_date=request.query_params.get("start_date"),
        end_date=request.query_params.get("end_date"),
    ))


@api_view(["GET"])
@permission_classes([IsAdminOnly])
def production_stats_view(request):
    """GET /api/analytics/production/stats/ — 生产统计。"""
    return Response(production_stats())


@api_view(["GET"])
@permission_classes([IsAdminOnly])
def publication_summary_view(request):
    """GET /api/analytics/publications/summary/ — 发布状态汇总。"""
    return Response(publication_summary())


@api_view(["GET"])
@permission_classes([IsAdminOnly])
def cost_optimization_report_view(request):
    """GET /api/analytics/cost-optimization/report/?month=YYYY-MM — 月度成本优化报告（主计划 3.3.10）。"""
    return Response(cost_optimization_report(month=request.query_params.get("month")))
