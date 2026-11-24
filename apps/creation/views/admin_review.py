"""平台管理员审核通道（决策 6 / design D4）。

跨剧查看与冻结/解冻的唯一入口：只读 + 强制审计日志；不经业务 ViewSet。
业务接口对平台管理员不再放行（成员资格是业务权限唯一来源）。
"""
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.system.permissions import is_platform_admin

from ..models import Asset, AuditLog, Episode, Play, Scene, Shot, TaskRecord
from ..permissions import _resolve_play


def _require_platform_admin(request):
    if not (request.user and request.user.is_authenticated and is_platform_admin(request.user)):
        return Response(
            {"detail": "仅平台管理员可执行此操作"},
            status=status.HTTP_403_FORBIDDEN,
        )
    return None


def _audit(request, action, play, **detail):
    """审核通道的每次访问强制落审计（spec: auth/object-permissions 审核场景）。"""
    AuditLog.objects.create(
        actor_id=request.user.pk, action=action, play=play,
        target_type="play", target_id=play.pk, detail=detail,
    )


class PlayReviewView(APIView):
    """GET /api/admin/plays/{id}/review/ — 跨剧只读审核快照（每次调用写审计）。"""

    permission_classes = [IsAuthenticated]

    def get(self, request, pk):
        denied = _require_platform_admin(request)
        if denied is not None:
            return denied
        play = Play.objects.filter(pk=pk).first()
        if play is None:
            return Response({"detail": "短剧不存在"}, status=status.HTTP_404_NOT_FOUND)

        episodes = list(
            Episode.objects.filter(play=play).order_by("episode_number")
            .values("id", "episode_number", "title", "status", "duration_seconds")
        )
        scene_count = Scene.objects.filter(episode__play=play).count()
        assets = list(
            Asset.objects.filter(play=play).values("id", "asset_type", "status")
        )[:200]
        tasks = list(
            TaskRecord.objects.filter(play=play)
            .order_by("-created_at").values("id", "task_type", "status", "created_at")
        )[:50]

        _audit(request, "play_review", play)
        return Response({
            "id": play.pk,
            "title": play.title,
            "genre": play.genre,
            "synopsis": play.synopsis,
            "status": play.status,
            "created_by": play.created_by_id,
            "frozen_reason": play.frozen_reason,
            "audit_mode": play.audit_mode,
            "created_at": play.created_at,
            "updated_at": play.updated_at,
            "episodes": episodes,
            "scene_count": scene_count,
            "assets": assets,
            "recent_tasks": tasks,
        })


class PlayFreezeView(APIView):
    """POST /api/admin/plays/{id}/freeze/ — 冻结（写审计；生成/发布入口随后拒绝）。"""

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        denied = _require_platform_admin(request)
        if denied is not None:
            return denied
        play = Play.objects.filter(pk=pk).first()
        if play is None:
            return Response({"detail": "短剧不存在"}, status=status.HTTP_404_NOT_FOUND)
        reason = (request.data.get("reason") or "").strip()
        if not reason:
            return Response(
                {"detail": "冻结必须填写原因（属主可见）"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        play.frozen_reason = reason
        play.save(update_fields=["frozen_reason"])
        _audit(request, "play_freeze", play, reason=reason)
        return Response({"detail": "已冻结", "frozen_reason": play.frozen_reason})


class PlayUnfreezeView(APIView):
    """POST /api/admin/plays/{id}/unfreeze/ — 解冻（写审计）。"""

    permission_classes = [IsAuthenticated]

    def post(self, request, pk):
        denied = _require_platform_admin(request)
        if denied is not None:
            return denied
        play = Play.objects.filter(pk=pk).first()
        if play is None:
            return Response({"detail": "短剧不存在"}, status=status.HTTP_404_NOT_FOUND)
        play.frozen_reason = ""
        play.save(update_fields=["frozen_reason"])
        _audit(request, "play_unfreeze", play)
        return Response({"detail": "已解冻"})
