"""distribution views —— 字幕轨 / 渠道 / 发布。"""
from django.http import HttpResponse
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response

from apps.core.system.permissions import IsAdminOnly

from apps.creation.views.base import PlayScopedViewSet

from .models import (
    DistributionChannel, Publication, PublicationStatus, SubtitleStatus, SubtitleTrack,
)
from .serializers import (
    DistributionChannelSerializer, PublicationSerializer, SubtitleTrackSerializer,
)


class SubtitleTrackViewSet(PlayScopedViewSet):
    serializer_class = SubtitleTrackSerializer
    queryset = SubtitleTrack.objects.all()
    play_filter_field = "episode__play"

    def perform_update(self, serializer):
        self._ensure_writable_new_parent(serializer.validated_data)
        instance = serializer.instance
        if "subtitle_data" in serializer.validated_data:
            instance.review_count = (instance.review_count or 0) + 1
            serializer.validated_data["status"] = SubtitleStatus.REVIEW
        serializer.save()

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        """POST /api/subtitle-tracks/{id}/approve/ — 审核通过为 READY。"""
        track = self.get_object()
        track.status = SubtitleStatus.READY
        track.save(update_fields=["status", "updated_at"])
        return Response(self.get_serializer(track).data)

    @action(detail=True, methods=["get"], url_path="export/(?P<fmt>srt|vtt)")
    def export(self, request, pk=None, fmt="srt"):
        """GET /api/subtitle-tracks/{id}/export/srt|vtt — 导出字幕文件。"""
        track = self.get_object()
        from .subtitles import format_srt, format_vtt
        if fmt == "vtt":
            content = format_vtt(track.subtitle_data or [])
            mime, ext = "text/vtt", "vtt"
        else:
            content = format_srt(track.subtitle_data or [])
            mime, ext = "application/x-subrip", "srt"
        resp = HttpResponse(content, content_type=f"{mime}; charset=utf-8")
        resp["Content-Disposition"] = (
            f'attachment; filename="subtitle_{track.pk}.{ext}"'
        )
        return resp


class DistributionChannelViewSet(viewsets.ModelViewSet):
    serializer_class = DistributionChannelSerializer
    queryset = DistributionChannel.objects.all()
    permission_classes = [IsAdminOnly]


class PublicationViewSet(PlayScopedViewSet):
    serializer_class = PublicationSerializer
    queryset = Publication.objects.all()
    play_filter_field = "episode__play"
    manage_actions = ("create", "update", "partial_update", "destroy", "rendition", "sync")

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)
        episode = serializer.validated_data.get("episode")
        channel = serializer.validated_data.get("channel")
        if not episode or not channel:
            raise ValidationError({"detail": "episode 和 channel 必填"})
        self._ensure_writable_parent({"episode": episode})
        from .publication import create_publication, validate_format
        pub, created = create_publication(episode, channel)
        if not created and pub.status == PublicationStatus.REJECTED:
            pub.status = PublicationStatus.PENDING
            pub.save(update_fields=["status", "updated_at"])
        try:
            validate_format(pub)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        from .rendition import RenditionBusy, ensure_rendition
        try:
            ensure_rendition(pub)
        except RenditionBusy:
            pass
        return Response(
            self.get_serializer(pub).data, status=status.HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"], url_path="rendition")
    def rendition(self, request, pk=None):
        """POST /api/publications/{id}/rendition/ — 重新执行渠道适配判定与生成。"""
        publication = self.get_object()
        from .rendition import RenditionBusy, ensure_rendition
        try:
            ensure_rendition(publication, force=True)
        except RenditionBusy as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        publication.refresh_from_db()
        return Response(self.get_serializer(publication).data)

    @action(detail=True, methods=["post"])
    def sync(self, request, pk=None):
        """POST /api/publications/{id}/sync/ — 模拟平台状态轮询/回调。"""
        publication = self.get_object()
        from .publication import (
            advance_status, mark_published, mark_rejected, mark_reviewing,
        )
        new_status = request.data.get("status")
        try:
            if new_status == "uploading":
                advance_status(publication, "UPLOADING")
            elif new_status == "reviewing":
                mark_reviewing(publication, request.data.get("platform_video_id", ""))
            elif new_status == "published":
                mark_published(publication, request.data.get("platform_url", ""))
            elif new_status == "rejected":
                mark_rejected(publication, request.data.get("reject_reason", ""))
            else:
                return Response({"detail": "未知状态"}, status=status.HTTP_400_BAD_REQUEST)
        except ValueError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(publication).data)
