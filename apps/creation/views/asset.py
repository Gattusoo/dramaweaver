"""creation views —— 资产生命周期 + 成片/预览鉴权下载（工作台 Stage D1）。"""
from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.response import Response

from apps.core.system.permissions import is_platform_admin

from ..models import (
    Asset, AssetDependency, AssetLibrary, AssetStatus, AssetType, Shot,
)
from ..permissions import AssetLibraryPermission, can_view_play
from ..serializers import (
    AssetDependencySerializer, AssetLibrarySerializer, AssetSerializer,
)
from .base import PlayScopedViewSet


class AssetViewSet(PlayScopedViewSet):
    serializer_class = AssetSerializer
    queryset = Asset.objects.all()

    @action(detail=True, methods=["get"])
    def download(self, request, pk=None):
        """成片/预览鉴权下载（任务 8.1/8.2，design D6）。

        仅 VIDEO / PREVIEW；剧内读权限。响应：
        - 本地 storage_key：FileResponse（MP4/SRT 等直读受控路径）；
        - shared 私有 S3：受控 SDK 签名 URL（短期、只读），不回显任意外部 URL，
          也绝不由服务器拉取归档/用户提供的 URL（无 SSRF）；
        - PREVIEW 带 X-Artifact-Kind: preview（不冒充正式 VIDEO）；
        - REMOTE 无受控对象 → 410 明确不可下载（不静默替换）。
        历史版本：?version=<n> 显式指定（不自动跳转到当前版本）。
        """
        asset = self.get_object()  # PlayScoped：非成员不可见（404）
        play = asset.play
        if not can_view_play(request.user, play):
            raise PermissionDenied("无权访问该短剧资源")

        version = request.query_params.get("version")
        if version is not None:
            if asset.asset_type != AssetType.VIDEO:
                return Response(
                    {"detail": "仅 VIDEO 支持指定历史版本"},
                    status=status.HTTP_400_BAD_REQUEST)
            if not str(version).isdigit():
                return Response(
                    {"detail": "version 须为正整数"},
                    status=status.HTTP_400_BAD_REQUEST)
            # 显式历史版本：允许 SUPERSEDED（历史保留可回查对比）；
            # 未生成的版本不算存在（PENDING/GENERATING/CANCELLED/FAILED）
            target = Asset.objects.filter(
                play=play, episode=asset.episode, asset_type=AssetType.VIDEO,
                version=int(version),
                status__in=(AssetStatus.READY, AssetStatus.SUPERSEDED),
            ).first()
            if target is None:
                return Response(
                    {"detail": f"VIDEO v{version} 不存在或未就绪"},
                    status=status.HTTP_404_NOT_FOUND)
            asset = target  # 历史版本不自动跳转（显式指定才返回）
        elif asset.status != AssetStatus.READY:
            # 无 version 参数：仅当前 READY 版本可下载
            return Response({"detail": "产物未就绪", "reason": "not_ready"},
                            status=status.HTTP_410_GONE)

        if asset.asset_type not in (AssetType.VIDEO, AssetType.PREVIEW):
            return Response({"detail": "仅成片/预览支持下载"},
                            status=status.HTTP_400_BAD_REQUEST)

        from ..workspace.download import DownloadError, build_download
        try:
            return build_download(request, asset)
        except DownloadError as exc:
            code = getattr(exc, "status_code", 400)
            return Response({"detail": str(exc)}, status=code)

    @action(detail=False, methods=["post"], url_path="preview-generate")
    def preview_generate(self, request):
        """低分辨率预览草稿图生成（主计划 3.5.3.5 / design D2）。

        {shot_id} → 派发带 preview 标记的 IMAGE_GEN 任务（计费闸/在跑去重/
        冻结剧拒绝全部继承），产出独立预览资产行（不挂 shot FK、不进版本链、
        不动主图指针、不被合成取用）。供用户确认画面方向后再决定正式重做。
        权限：edit；ENABLE_ASSET_PREVIEW 关闭时 404；同镜头预览进行中 409。
        """
        from django.conf import settings
        if not getattr(settings, "ENABLE_ASSET_PREVIEW", False):
            return Response({"detail": "资产预览生成未启用"},
                            status=status.HTTP_404_NOT_FOUND)
        shot_id = request.data.get("shot_id")
        if not shot_id:
            return Response({"detail": "shot_id 必填"},
                            status=status.HTTP_400_BAD_REQUEST)
        shot = (Shot.objects.select_related("scene__episode__play")
                .filter(pk=shot_id).first())
        if shot is None:
            return Response({"detail": "镜头不存在"},
                            status=status.HTTP_404_NOT_FOUND)
        play = shot.scene.episode.play
        from ..permissions import can_view_play, can_write_play
        # 读不到即 404（不泄露他剧镜头存在性）；读得到但无写权限 403
        if not can_view_play(request.user, play):
            return Response({"detail": "镜头不存在"},
                            status=status.HTTP_404_NOT_FOUND)
        if not can_write_play(request.user, play):
            raise PermissionDenied("无权修改该短剧")
        from ..pipeline.dispatcher import (
            PlayFrozenError, dispatch_image_preview,
        )
        try:
            record = dispatch_image_preview(shot)
        except PlayFrozenError as exc:
            resp = Response({"detail": str(exc), "reason": "play_frozen"},
                            status=status.HTTP_403_FORBIDDEN)
            resp.business_code = 4003
            return resp
        if record is None:
            return Response({"detail": "该镜头已有进行中的预览任务"},
                            status=status.HTTP_409_CONFLICT)
        return Response({"task_record_id": record.pk, "shot_id": shot.pk,
                        "preview": True}, status=status.HTTP_201_CREATED)

    @action(detail=True, methods=["get"])
    def compare(self, request, pk=None):
        """资产版本对比（主计划 3.5.3.6 / design D4）。

        GET /api/assets/{id}/compare/?against={other_id}
        → {identical, version_a, version_b, cost_delta, field_diffs[], ...}
        六元组（play/episode/scene/shot/character/asset_type）同位置才可比；
        跨位置/跨剧 400。无 against 参数 → 该位置全部版本清单（含
        SUPERSEDED 历史版本），前端先列后选。权限：剧内读（PlayScoped）。
        """
        asset = self.get_object()  # PlayScoped：非成员不可见（404）
        against = request.query_params.get("against")
        same_position = Asset.objects.filter(
            play=asset.play, episode=asset.episode, scene=asset.scene,
            shot=asset.shot, character=asset.character,
            asset_type=asset.asset_type,
        ).order_by("version")
        if not against:
            versions = [{
                "id": a.pk, "version": a.version, "status": a.status,
                "cost": str(a.cost), "created_at": a.created_at,
                "artifact_url": a.artifact_url, "prompt": a.prompt,
            } for a in same_position]
            return Response({"asset_id": asset.pk,
                             "asset_type": asset.asset_type,
                             "versions": versions})
        other = Asset.objects.filter(pk=against).first()
        if other is None:
            return Response({"detail": f"对比目标 {against} 不存在"},
                            status=status.HTTP_404_NOT_FOUND)
        if other.play_id != asset.play_id:
            # 跨剧：不泄露他剧存在性，按位置不一致拒绝
            return Response({"detail": "对比目标与该资产不在同一目标位置"},
                            status=status.HTTP_400_BAD_REQUEST)
        if (other.episode_id, other.scene_id, other.shot_id,
                other.character_id, other.asset_type) != (
                asset.episode_id, asset.scene_id, asset.shot_id,
                asset.character_id, asset.asset_type):
            return Response({"detail": "对比目标与该资产不在同一目标位置"},
                            status=status.HTTP_400_BAD_REQUEST)
        # diff 字段白名单（metadata 摘要级比较——媒体探针 JSON 可能很大；
        # status 排除：同位置旧版本被新版本自动 SUPERSEDED，两版本 status
        # 必然不同，属版本机制固有噪声而非内容差异——版本清单已展示 status）
        fields = ("artifact_url", "prompt", "ai_model", "parameters",
                  "metadata", "cost")
        diffs = []
        for name in fields:
            old, new = getattr(other, name), getattr(asset, name)
            if old != new:
                diffs.append({"field": name, "old": old, "new": new,
                              "change": "modified"})
        return Response({
            "identical": not diffs,
            "version_a": other.version, "version_b": asset.version,
            "asset_a": other.pk, "asset_b": asset.pk,
            "asset_type": asset.asset_type,
            "status_a": other.status, "status_b": asset.status,
            "cost_a": str(other.cost), "cost_b": str(asset.cost),
            "cost_delta": str((asset.cost or 0) - (other.cost or 0)),
            "created_at_a": other.created_at, "created_at_b": asset.created_at,
            "field_diffs": diffs,
        })


class AssetDependencyViewSet(PlayScopedViewSet):
    serializer_class = AssetDependencySerializer
    queryset = AssetDependency.objects.all()
    play_filter_field = "downstream__play"


class AssetLibraryViewSet(viewsets.ModelViewSet):
    serializer_class = AssetLibrarySerializer
    permission_classes = [AssetLibraryPermission]

    def get_queryset(self):
        qs = AssetLibrary.objects.all()
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        if is_platform_admin(user):
            return qs
        return qs.filter(Q(is_public=True) | Q(owner=user))

    def perform_create(self, serializer):
        serializer.save(owner=self.request.user)
