"""公开只读视图（public/api）。

- 权限 AllowAny + authentication_classes=[]：不解析 token，本地存着过期 token 的
  访客也不会被 401 拦下（走公开客户端 src/api/public.ts）。
- 数据范围：一律强制 status=PUBLISHED（集只取 COMPLETED）。
- 信封：走全局 CustomJSONRenderer，视图内不手工包 {code,msg,data}；列表用 CustomPagination。
"""
from django.db.models import Count, Q, Sum
from rest_framework.authentication import BaseAuthentication
from rest_framework.generics import ListAPIView, RetrieveAPIView
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.creation.models import Episode, Play, PlayStatus
from dvadmin.utils.pagination import CustomPagination

from .serializers import (
    PublicPlayDetailSerializer, PublicPlaySerializer, PublicStatsSerializer,
)

_PUBLISHED = PlayStatus.PUBLISHED
_COMPLETED = "COMPLETED"


class _NoAuth(BaseAuthentication):
    """显式空鉴权：authenticate 恒返回 None，配合 authentication_classes=[] 双保险。"""

    def authenticate(self, request):
        return None


class PublicPlayListView(ListAPIView):
    """GET /api/public/plays/ — 已发布短剧列表（题材/搜索/排序/分页）。"""

    permission_classes = [AllowAny]
    authentication_classes = []
    serializer_class = PublicPlaySerializer
    pagination_class = CustomPagination

    def get_queryset(self):
        qs = Play.objects.filter(status=_PUBLISHED)
        params = self.request.query_params
        genre = (params.get("genre") or "").strip()
        if genre:
            qs = qs.filter(genre=genre)
        search = (params.get("search") or "").strip()
        if search:
            qs = qs.filter(Q(title__icontains=search) | Q(synopsis__icontains=search))
        return qs

    def get_ordering(self):
        # -play_count 暂无真实播放量来源，用 -id 做确定性兜底（避免刷新顺序抖动）
        ordering = self.request.query_params.get("ordering")
        if ordering == "-play_count":
            return ["-id"]
        return ["-id"]  # -published_at 无来源列，同样用 -id 稳定兜底


class PublicPlayDetailView(RetrieveAPIView):
    """GET /api/public/plays/{id}/ — 公开详情（已完成集 + 角色）。

    不存在 / 非 PUBLISHED 一律 404（不 403），避免向匿名访客泄露"内容存在但你看不到"。
    """

    permission_classes = [AllowAny]
    authentication_classes = []
    serializer_class = PublicPlayDetailSerializer

    def get_queryset(self):
        return Play.objects.filter(status=_PUBLISHED)


class PublicStatsView(APIView):
    """GET /api/public/stats/ — 平台公开统计，口径仅 PUBLISHED。"""

    permission_classes = [AllowAny]
    authentication_classes = []

    def get(self, request):
        plays = Play.objects.filter(status=_PUBLISHED)
        episodes = Episode.objects.filter(play__status=_PUBLISHED, status=_COMPLETED)
        agg = episodes.aggregate(total=Sum("duration_seconds"))
        genres = (plays.values("genre").annotate(count=Count("id"))
                  .order_by("-count", "genre"))
        data = {
            "plays": plays.count(),
            "episodes": episodes.count(),
            "total_duration_seconds": int(agg["total"] or 0),
            "genres": [{"genre": g["genre"], "count": g["count"]} for g in genres if g["genre"]],
        }
        return Response(PublicStatsSerializer(data).data)
