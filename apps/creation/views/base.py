"""creation views —— PlayScopedViewSet 基类。"""
from rest_framework import viewsets
from rest_framework.exceptions import PermissionDenied

from ..permissions import (
    IsPlayAuthorizedOrAdmin, _resolve_play, can_manage_play, can_write_play,
    get_accessible_play_ids,
)


class PlayScopedViewSet(viewsets.ModelViewSet):
    """按短剧归属隔离的基类 ViewSet。"""

    permission_classes = [IsPlayAuthorizedOrAdmin]
    play_filter_field = "play"
    manage_actions = ()

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        play_ids = get_accessible_play_ids(user)
        return qs.filter(**{f"{self.play_filter_field}__in": play_ids})

    _PARENT_KEYS = ("play", "episode", "scene", "shot", "character",
                    "upstream", "downstream", "video_asset", "subtitle_track")

    def perform_create(self, serializer):
        self._ensure_writable_parent(serializer.validated_data)
        serializer.save()

    def perform_update(self, serializer):
        self._ensure_writable_new_parent(serializer.validated_data)
        serializer.save()

    def _ensure_writable_new_parent(self, data):
        if any(data.get(k) is not None for k in self._PARENT_KEYS):
            self._ensure_writable_parent(data)

    def _ensure_writable_parent(self, data):
        plays = self._resolve_parent_plays(data)
        if not plays:
            raise PermissionDenied("无法确定资源所属短剧")
        check = can_manage_play if self.action in self.manage_actions else can_write_play
        for play in plays:
            if not check(self.request.user, play):
                raise PermissionDenied("无权修改该短剧的资源")

    @classmethod
    def _resolve_parent_plays(cls, data):
        plays = {}
        for key in cls._PARENT_KEYS:
            obj = data.get(key)
            if obj is None:
                continue
            play = _resolve_play(obj)
            if play is None:
                raise PermissionDenied("无法确定资源所属短剧")
            plays[play.pk] = play
        return list(plays.values())
