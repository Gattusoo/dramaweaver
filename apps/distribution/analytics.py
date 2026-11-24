"""费用与生产数据分析（看板 API 的查询逻辑）。"""
from decimal import Decimal

from django.db.models import Avg, Count, Q, Sum
from django.utils import timezone

from apps.ai_gateway.models import AIProvider, CostRecord
from apps.creation.models import EpisodeStatus, Play, PlayStatus
from .models import Publication, PublicationStatus


def play_cost_breakdown(play):
    """按 Play 聚合总费用 + 按 provider_type 分组。"""
    records = CostRecord.objects.filter(play=play)
    total = records.aggregate(s=Sum("total_cost"))["s"] or Decimal("0")
    by_type = {}
    for row in records.values("provider_type").annotate(s=Sum("total_cost")):
        by_type[row["provider_type"]] = float(row["s"] or 0)
    return {"total": float(total), "by_type": by_type}


def cost_by_provider(start_date=None, end_date=None):
    """按供应商分组统计。"""
    qs = CostRecord.objects.all()
    if start_date:
        qs = qs.filter(created_at__date__gte=start_date)
    if end_date:
        qs = qs.filter(created_at__date__lte=end_date)
    rows = qs.values("provider__name", "provider_id").annotate(
        call_count=Count("id"), total=Sum("total_cost"),
    )
    result = []
    for row in rows:
        result.append({
            "provider": row["provider__name"],
            "call_count": row["call_count"],
            "total_cost": float(row["total"] or 0),
            "avg_cost": float(row["total"] or 0) / row["call_count"] if row["call_count"] else 0,
        })
    return result


def cost_by_model(start_date=None, end_date=None):
    """按模型分组统计。"""
    qs = CostRecord.objects.all()
    if start_date:
        qs = qs.filter(created_at__date__gte=start_date)
    if end_date:
        qs = qs.filter(created_at__date__lte=end_date)
    rows = qs.values("model").annotate(
        call_count=Count("id"), total=Sum("total_cost"),
    )
    result = []
    for row in rows:
        result.append({
            "model": row["model"],
            "call_count": row["call_count"],
            "total_cost": float(row["total"] or 0),
            "avg_cost": float(row["total"] or 0) / row["call_count"] if row["call_count"] else 0,
        })
    return result


def production_stats():
    """生产统计。"""
    total_plays = Play.objects.count()
    completed = Play.objects.filter(status=PlayStatus.COMPLETED).count()
    published = Play.objects.filter(status=PlayStatus.PUBLISHED).count()
    # 仅真正在流水线上的状态算 in_progress：OUTLINING..COMPOSING。
    # DRAFT（未开拍）、PAUSED_*（审核暂停）、COMPLETED/PUBLISHED（已终态）不计入，
    # 否则暂停/草稿剧会被误报成"进行中"虚高。
    in_progress = Play.objects.filter(status__in=(
        PlayStatus.OUTLINING, PlayStatus.SCRIPTING, PlayStatus.STORYBOARDING,
        PlayStatus.ASSET_GEN, PlayStatus.COMPOSING,
    )).count()
    cost_total = CostRecord.objects.aggregate(s=Sum("total_cost"))["s"] or Decimal("0")
    avg_cost = float(cost_total) / total_plays if total_plays else 0
    return {
        "total_plays": total_plays,
        "completed": completed,
        "published": published,
        "in_progress": in_progress,
        "avg_cost_per_play": avg_cost,
        "avg_duration_hours": 0,
    }


def publication_summary():
    """发布状态汇总。"""
    qs = Publication.objects.all()
    total = qs.count()
    by_status = {
        s: qs.filter(status=s).count() for s in PublicationStatus.values
    }
    by_channel = {}
    for row in qs.values("channel__platform").annotate(c=Count("id")):
        by_channel[row["channel__platform"]] = row["c"]
    return {
        "total": total,
        "published": by_status.get(PublicationStatus.PUBLISHED, 0),
        "reviewing": by_status.get(PublicationStatus.REVIEWING, 0),
        "rejected": by_status.get(PublicationStatus.REJECTED, 0),
        "uploading": by_status.get(PublicationStatus.UPLOADING, 0),
        "pending": by_status.get(PublicationStatus.PENDING, 0),
        "by_channel": by_channel,
    }


def cost_optimization_report(month=None):
    """月度成本优化报告（主计划 3.3.10 / design D5）。

    数据全部来自既有表：CostRecord（模型分布/调用数）、SmartCache（命中）、
    AssistantProposal payload.batch_info（批量节省估算）。month 为 "YYYY-MM"
    缺省当月。无数据返回零计数（不报错）。
    """
    from datetime import date

    from apps.ai_gateway.models import SmartCache

    now = timezone.now()
    if month:
        try:
            year, mon = int(month[:4]), int(month[5:7])
            start = date(year, mon, 1)
        except (ValueError, IndexError):
            start = now.date().replace(day=1)
    else:
        start = now.date().replace(day=1)
    if start.month == 12:
        end = date(start.year + 1, 1, 1)
    else:
        end = date(start.year, start.month + 1, 1)

    records = CostRecord.objects.filter(
        created_at__date__gte=start, created_at__date__lt=end)
    total_calls = records.count()
    total_cost = records.aggregate(t=Sum("total_cost"))["t"] or Decimal("0")
    model_rows = (records.values("model")
                  .annotate(call_count=Count("id"), total=Sum("total_cost"))
                  .order_by("-total"))

    # 缓存命中（本月）：last_hit_at 在月内的行，命中计数为累计值——报告口径
    # 取"当前存在条目的累计命中数"（月度精确增量需每日快照，留企业扩展期）
    cache_rows = SmartCache.objects.filter(
        last_hit_at__date__gte=start, last_hit_at__date__lt=end)
    cache_hits = sum(r.hit_count for r in cache_rows)
    cache_entries = SmartCache.objects.count()

    # 批量节省估算：本月批量提案的镜头数 ×（批次数与镜头数差）× 单次均值
    from apps.creation.assistant.models import AssistantProposal
    batch_count, batch_shots = 0, 0
    for p in AssistantProposal.objects.filter(
            created_at__date__gte=start, created_at__date__lt=end,
            kind="EPISODE_SCRIPT"):
        info = (p.payload or {}).get("batch_info")
        if info and info.get("batch"):
            batch_count += int(info.get("batch_count", 0))
            batch_shots += int(info.get("batch_shot_count", 0))
    saved_calls = max(0, batch_shots - batch_count) if batch_count else 0
    avg_cost = float(total_cost) / total_calls if total_calls else 0.0

    return {
        "month": start.strftime("%Y-%m"),
        "total_calls": total_calls,
        "total_cost": float(total_cost),
        "avg_cost_per_call": round(avg_cost, 6),
        "cache": {
            "entries": cache_entries,
            "hit_count": cache_hits,
            "hit_rate": round(cache_hits / (cache_hits + total_calls), 4)
            if (cache_hits + total_calls) else 0.0,
        },
        "models": [{
            "model": r["model"], "call_count": r["call_count"],
            "total_cost": float(r["total"] or 0),
        } for r in model_rows],
        "batch": {
            "batch_count": batch_count,
            "shot_count": batch_shots,
            "estimated_saved_calls": saved_calls,
            "estimated_saved_cost": round(saved_calls * avg_cost, 6),
        },
    }
