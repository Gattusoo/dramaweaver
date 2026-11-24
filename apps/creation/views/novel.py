"""creation views —— 小说工作区（novel.py 族）。

ViewSet 族：Novel / NovelVolume / NovelChapter（含快照 action）/
ChapterSnapshot（只读）/ StoryAsset / EntityRelation（含图谱查询 action）/
Canvas / CanvasNode（含生成触发）/ DirectorScene / DirectorElement /
ShotDirecting（含构图检查标注 action）。
隔离复用 PlayScopedViewSet（play_filter_field 按各族父链配置）。
"""
from django.db.models import Q
from django.db import models as db_models
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.response import Response

from ..models import (
    Canvas, CanvasNode, CanvasNodeType, ChapterSnapshot, Clip, Composition,
    DirectorElement, DirectorElementType, DirectorScene, EntityRelation, Novel,
    NovelChapter, NovelVolume, ShotDirecting, StoryAsset, StoryAssetType,
    Track, TrackKind,
)
from ..permissions import IsPlayAuthorizedOrAdmin, can_write_play
from ..serializers.novel import (
    CanvasNodeSerializer, CanvasSerializer, ChapterSnapshotSerializer,
    ClipSerializer, CompositionSerializer, DirectorElementSerializer,
    DirectorSceneSerializer, EntityRelationSerializer, NovelChapterSerializer,
    NovelSerializer, NovelVolumeSerializer, ShotDirectingSerializer,
    StoryAssetSerializer, TrackSerializer,
)
from .base import PlayScopedViewSet


class NovelViewSet(PlayScopedViewSet):
    serializer_class = NovelSerializer
    queryset = Novel.objects.all()
    play_filter_field = "play"


class NovelVolumeViewSet(PlayScopedViewSet):
    serializer_class = NovelVolumeSerializer
    queryset = NovelVolume.objects.select_related("novel").all()
    play_filter_field = "novel__play"
    _PARENT_KEYS = ("novel",)


class NovelChapterViewSet(PlayScopedViewSet):
    serializer_class = NovelChapterSerializer
    queryset = NovelChapter.objects.select_related("volume__novel").all()
    play_filter_field = "volume__novel__play"
    _PARENT_KEYS = ("volume",)

    def get_queryset(self):
        qs = super().get_queryset()
        if self.action == "list" and self.request.query_params.get("include_deleted") != "true":
            qs = qs.filter(is_deleted=False)
        return qs.order_by("chapter_number")

    def perform_create(self, serializer):
        self._ensure_writable_parent(serializer.validated_data)
        serializer.save()  # 章节号缺省计算在序列化器 validate 阶段（绕 unique 误拒）

    def destroy(self, request, *args, **kwargs):
        """软删除：被改编引用过的章节保留引用计数提示（仍可删，提示随响应返回）。"""
        chapter = self.get_object()
        chapter.is_deleted = True
        chapter.save(update_fields=["is_deleted", "updated_at"])
        return Response({"id": chapter.pk, "is_deleted": True,
                         "adaptation_refs": chapter.adaptation_refs})

    @action(detail=True, methods=["get"])
    def snapshots(self, request, pk=None):
        """章节版本快照列表（新→旧）。"""
        chapter = self.get_object()
        snaps = chapter.snapshots.order_by("-pk")
        return Response(ChapterSnapshotSerializer(snaps, many=True).data)

    @action(detail=True, methods=["post"], url_path="restore")
    def restore(self, request, pk=None):
        chapter = self.get_object()
        if not chapter.is_deleted:
            return Response({"id": chapter.pk, "is_deleted": False})
        volume = chapter.volume
        if not can_write_play(request.user, volume.novel.play):
            return Response({"detail": "无权修改该短剧"}, status=403)
        chapter.is_deleted = False
        chapter.save(update_fields=["is_deleted", "updated_at"])
        return Response({"id": chapter.pk, "is_deleted": False})


class ChapterSnapshotViewSet(viewsets.ReadOnlyModelViewSet):
    """快照只读（列表走章节的 snapshots action，此处提供单条回看）。"""

    serializer_class = ChapterSnapshotSerializer
    permission_classes = [IsPlayAuthorizedOrAdmin]
    queryset = ChapterSnapshot.objects.select_related("chapter__volume__novel")

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        from ..permissions import get_accessible_play_ids
        return qs.filter(chapter__volume__novel__play__in=get_accessible_play_ids(user))


class StoryAssetViewSet(PlayScopedViewSet):
    serializer_class = StoryAssetSerializer
    queryset = StoryAsset.objects.select_related("character").prefetch_related("chapter_refs")
    play_filter_field = "play"
    _PARENT_KEYS = ("play", "character")

    def get_queryset(self):
        qs = super().get_queryset()
        stype = self.request.query_params.get("type")
        if stype:
            qs = qs.filter(type=stype)
        keyword = (self.request.query_params.get("q") or "").strip()
        if keyword:
            qs = qs.filter(name__icontains=keyword)
        return qs


class EntityRelationViewSet(PlayScopedViewSet):
    serializer_class = EntityRelationSerializer
    queryset = EntityRelation.objects.select_related(
        "from_character", "to_character", "start_chapter", "end_chapter",
    ).prefetch_related("events")
    play_filter_field = "play"
    _PARENT_KEYS = ("play", "from_character", "to_character")

    @action(detail=False, methods=["get"], url_path="graph")
    def graph(self, request):
        """关系图谱：可选 chapter_number 时点过滤——仅返回该时点生效的关系边与事件。

        生效判定：start_chapter 序号 <= 时点（无起始章视为始终生效），
        且（无 end_chapter 或 end_chapter 序号 >= 时点）。
        """
        qs = self.get_queryset()
        point = request.query_params.get("chapter_number")
        if point is not None and point.isdigit():
            point = int(point)
            qs = qs.filter(
                Q(start_chapter__isnull=True) | Q(start_chapter__chapter_number__lte=point),
                Q(end_chapter__isnull=True) | Q(end_chapter__chapter_number__gte=point),
            )
        data = EntityRelationSerializer(qs, many=True).data
        if point is not None and str(point).isdigit():
            for rel in data:
                rel["events"] = [e for e in rel["events"]
                                 if (e.get("chapter_number") or 0) <= int(point)]
        return Response({"relations": data, "chapter_number": point})


# ── 无限画布（canvas-board，2026-11-23 组 4）──────────────────────────────

class CanvasViewSet(PlayScopedViewSet):
    serializer_class = CanvasSerializer
    queryset = Canvas.objects.all()
    play_filter_field = "play"
    _PARENT_KEYS = ("play",)


class CanvasNodeViewSet(PlayScopedViewSet):
    serializer_class = CanvasNodeSerializer
    queryset = CanvasNode.objects.select_related(
        "canvas", "scene", "character", "asset").all()
    play_filter_field = "canvas__play"
    _PARENT_KEYS = ("canvas", "scene", "character", "asset")

    def perform_create(self, serializer):
        self._ensure_writable_parent(serializer.validated_data)
        serializer.save()

    @action(detail=True, methods=["post"], url_path="generate")
    def generate(self, request, pk=None):
        """节点触发生成（design：逐镜头走既有派发闸——计费闸/冻结剧 4003/
        在跑去重/指纹复用/已就绪跳过——本端点不新增守卫语义）。

        - SCENE_BLOCK：对引用场景逐镜头派发 IMAGE+TTS（复用 dispatch_image_and_tts）
        - CHARACTER_STYLING：无既有定妆时派发 CHARACTER_REF
        - IMAGE_FLOW：对锚镜头派发 IMAGE_GEN（params.prompt 可覆盖提示词）
        - ASSET_REF：无生成语义 → 400
        → {dispatched: [task_record_id...], skipped: [{shot_id, reason}]}
        """
        from django.utils import timezone
        from rest_framework import status as http_status

        from ..models import (
            AssetStatus, AssetType, TaskStatus, TaskType,
        )
        from ..pipeline import dispatcher as D

        node = self.get_object()
        play = node.canvas.play
        if not can_write_play(request.user, play):
            return Response({"detail": "无权操作该短剧"}, status=403)
        if play.frozen_reason:
            resp = Response(
                {"detail": f"短剧已被平台管理员冻结：{play.frozen_reason}",
                 "reason": "play_frozen"},
                status=http_status.HTTP_403_FORBIDDEN)
            resp.business_code = 4003
            return resp
        dispatched, skipped = [], []
        try:
            if node.type == CanvasNodeType.SCENE_BLOCK:
                if node.scene is None:
                    return Response({"detail": "引用场景已被删除"},
                                    status=http_status.HTTP_400_BAD_REQUEST)
                for shot in node.scene.shots.order_by("shot_number"):
                    before = len(dispatched)
                    records = D.dispatch_image_and_tts(shot)
                    if records:
                        dispatched.extend(r.pk for r in records)
                    else:
                        # 既有守卫全拒（已就绪或在跑）：跳过并明示原因
                        reason = "already_ready" if not (
                            D._material_pending(play, shot, AssetType.IMAGE)
                            or D._material_pending(play, shot, AssetType.AUDIO)
                        ) else "in_progress"
                        skipped.append({"shot_id": shot.pk, "reason": reason})
            elif node.type == CanvasNodeType.CHARACTER_STYLING:
                if node.character is None:
                    return Response({"detail": "引用角色已被删除"},
                                    status=http_status.HTTP_400_BAD_REQUEST)
                from ..models import Asset
                has_ready = Asset.objects.filter(
                    play=play, character=node.character,
                    asset_type=AssetType.CHARACTER_REF,
                    status=AssetStatus.READY).exists()
                if has_ready:
                    skipped.append({"character_id": node.character.pk,
                                    "reason": "already_ready"})
                else:
                    records = D.dispatch_character_ref(play)
                    dispatched.extend(r.pk for r in records)
            elif node.type == CanvasNodeType.IMAGE_FLOW:
                if node.asset is None or node.asset.shot is None:
                    return Response({"detail": "生图流节点缺少镜头锚"},
                                    status=http_status.HTTP_400_BAD_REQUEST)
                records = D.dispatch_image_and_tts(node.asset.shot)
                if records:
                    dispatched.extend(r.pk for r in records)
                else:
                    skipped.append({"shot_id": node.asset.shot.pk,
                                    "reason": "already_ready"})
            else:
                return Response({"detail": "该节点类型不支持触发生成"},
                                status=http_status.HTTP_400_BAD_REQUEST)
        except D.PlayFrozenError as exc:
            resp = Response({"detail": str(exc), "reason": "play_frozen"},
                            status=http_status.HTTP_403_FORBIDDEN)
            resp.business_code = 4003
            return resp
        return Response({"dispatched": dispatched, "skipped": skipped})


# ── 导演台 2D（director-stage，2026-11-23 组 5）────────────────────────────


class DirectorSceneViewSet(PlayScopedViewSet):
    serializer_class = DirectorSceneSerializer
    queryset = DirectorScene.objects.all()
    play_filter_field = "play"
    _PARENT_KEYS = ("play",)


class DirectorElementViewSet(PlayScopedViewSet):
    serializer_class = DirectorElementSerializer
    queryset = DirectorElement.objects.select_related("scene", "character").all()
    play_filter_field = "scene__play"
    _PARENT_KEYS = ("scene", "character")

    def get_queryset(self):
        qs = super().get_queryset()
        scene_id = self.request.query_params.get("scene")
        if scene_id and scene_id.isdigit():
            qs = qs.filter(scene_id=int(scene_id))
        etype = self.request.query_params.get("type")
        if etype:
            qs = qs.filter(type=etype)
        return qs


class ShotDirectingViewSet(PlayScopedViewSet):
    serializer_class = ShotDirectingSerializer
    queryset = ShotDirecting.objects.select_related(
        "shot__scene__episode", "camera", "camera_move").all()
    play_filter_field = "shot__scene__episode__play"
    _PARENT_KEYS = ("shot", "camera", "camera_move")

    @action(detail=True, methods=["post"], url_path="composition-check")
    def composition_check(self, request, pk=None):
        """抽帧构图检查标注（spec：检查结果落 ShotDirecting，不新建图像体系）。

        body: {"notes": ["地平线倾斜", ...], "append": true}
        append=false 覆盖；true 追加。返回更新后的完整标注列表。
        """
        directing = self.get_object()
        notes = request.data.get("notes")
        if not isinstance(notes, list) or not all(isinstance(n, str) for n in notes):
            return Response({"detail": "notes 须为字符串列表"}, status=400)
        if request.data.get("append"):
            directing.composition_notes = list(directing.composition_notes) + notes
        else:
            directing.composition_notes = notes
        directing.save(update_fields=["composition_notes", "updated_at"])
        return Response(ShotDirectingSerializer(directing).data)


# ── 多轨剪辑（timeline-editing，2026-11-24 组 6）──────────────────────────


class CompositionViewSet(PlayScopedViewSet):
    serializer_class = CompositionSerializer
    queryset = Composition.objects.select_related("play", "episode").prefetch_related("tracks__clips")
    play_filter_field = "play"
    _PARENT_KEYS = ("play", "episode")

    @action(detail=False, methods=["post"], url_path="import")
    def import_from_compose(self, request):
        """自动合成导入（spec：既有合成产物一键生成工程初始态，自动上轨）。

        body: {"episode_id": <int>} —— 取该集最新 READY VIDEO 合成产物为来源。
        幂等：同一来源资产重复导入返回既有工程。冻结剧 4003。
        """
        from rest_framework import status as http_status

        from ..models import Asset, AssetStatus, AssetType, Episode
        from ..pipeline import timeline as TL

        episode_id = request.data.get("episode_id")
        try:
            episode = Episode.objects.select_related("play").get(pk=episode_id)
        except (Episode.DoesNotExist, TypeError, ValueError):
            return Response({"detail": "剧集不存在"}, status=400)
        play = episode.play
        if not can_write_play(request.user, play):
            return Response({"detail": "无权操作该短剧"}, status=403)
        if play.frozen_reason:
            resp = Response(
                {"detail": f"短剧已被平台管理员冻结：{play.frozen_reason}",
                 "reason": "play_frozen"},
                status=http_status.HTTP_403_FORBIDDEN)
            resp.business_code = 4003
            return resp
        source = Asset.objects.filter(
            play=play, episode=episode, asset_type=AssetType.VIDEO,
            status=AssetStatus.READY).order_by("-version").first()
        if source is None or not source.artifact_url:
            return Response({"detail": "该集尚无自动合成产物可导入"}, status=400)
        comp, imported = TL.import_composition(play, episode, source)
        return Response(CompositionSerializer(comp).data | {"imported_clips": imported})

    @action(detail=True, methods=["post"], url_path="clips/split")
    def split_clip(self, request, pk=None):
        """片段分割（spec：两段记录父引用，追溯链完整）。

        body: {"clip_id": <int>, "offset": <float 素材内秒>}
        原片段保留为左段（分割即改写），右段新建挂 parent。
        """
        from rest_framework import status as http_status

        comp = self.get_object()
        clip = Clip.objects.filter(pk=request.data.get("clip_id"),
                                   track__composition=comp).first()
        if clip is None:
            return Response({"detail": "片段不在该工程"}, status=400)
        try:
            offset = float(request.data.get("offset"))
            left, right = clip.split_at(offset)
        except (TypeError, ValueError) as exc:
            return Response({"detail": f"分割点非法：{exc}"}, status=400)
        # 原片段改写为左段（父链记录到新建右段），右段落库
        clip.out_point = left.out_point
        clip.save(update_fields=["out_point", "updated_at"])
        right.parent = clip
        right.order = clip.order + 1
        # 同轨后续片段序顺延（时间线 UI 按序渲染）
        Clip.objects.filter(track=clip.track, order__gt=clip.order).update(order=db_models.F("order") + 1)
        right.save()
        return Response(ClipSerializer([clip, right], many=True).data)

    @action(detail=True, methods=["post"], url_path="export")
    def export(self, request, pk=None):
        """手动精修导出（spec：FFmpeg 按工程渲染，产物落 Asset 体系）。

        先跑导出前校验（6.5）：问题阻断并返回明细；通过则走既有派发闸派
        TIMELINE_EXPORT（render 槽位/冻结 4003/在跑去重），产物落 Asset(VIDEO)。
        """
        from django.utils import timezone
        from rest_framework import status as http_status

        from ..models import Asset, TaskRecord, TaskStatus, TaskType
        from ..pipeline import dispatcher as D
        from ..pipeline import timeline as TL

        comp = self.get_object()
        play = comp.play
        if not can_write_play(request.user, play):
            return Response({"detail": "无权操作该短剧"}, status=403)
        problems = TL.validate_composition(comp)
        if problems:
            return Response({"detail": "工程校验未通过，已阻断导出",
                            "problems": problems},
                            status=http_status.HTTP_400_BAD_REQUEST)
        running = TaskRecord.objects.filter(
            play=play, task_type=TaskType.TIMELINE_EXPORT,
            status__in=(TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.RETRYING),
            input_summary__composition_id=comp.pk,
        )
        if running.exists():
            return Response({"detail": "该工程已有导出任务在跑", "reason": "in_progress"},
                            status=http_status.HTTP_409_CONFLICT)
        try:
            record = D.dispatch_task(
                TaskType.TIMELINE_EXPORT, play=play, episode=comp.episode,
                input_summary={"composition_id": comp.pk})
        except D.PlayFrozenError as exc:
            resp = Response({"detail": str(exc), "reason": "play_frozen"},
                            status=http_status.HTTP_403_FORBIDDEN)
            resp.business_code = 4003
            return resp
        return Response({"task_record_id": record.pk, "status": record.status})


class TrackViewSet(PlayScopedViewSet):
    serializer_class = TrackSerializer
    queryset = Track.objects.select_related("composition").prefetch_related("clips")
    play_filter_field = "composition__play"
    _PARENT_KEYS = ("composition",)

    def get_queryset(self):
        qs = super().get_queryset()
        comp_id = self.request.query_params.get("composition")
        if comp_id and comp_id.isdigit():
            qs = qs.filter(composition_id=int(comp_id))
        return qs


class ClipViewSet(PlayScopedViewSet):
    serializer_class = ClipSerializer
    queryset = Clip.objects.select_related("track__composition", "asset", "parent").all()
    play_filter_field = "track__composition__play"
    _PARENT_KEYS = ("track", "asset", "parent")

    def get_queryset(self):
        qs = super().get_queryset()
        track_id = self.request.query_params.get("track")
        if track_id and track_id.isdigit():
            qs = qs.filter(track_id=int(track_id))
        return qs

    @action(detail=True, methods=["post"], url_path="switch-version")
    def switch_version(self, request, pk=None):
        """素材版本切换（spec：改 asset 引用，父引用链保留）。

        body: {"asset_id": <int>} —— 目标须与当前素材同位置（六元组）且 READY。
        """
        from rest_framework import status as http_status

        from ..models import Asset, AssetStatus

        clip = self.get_object()
        target_id = request.data.get("asset_id")
        target = Asset.objects.filter(pk=target_id, play=clip.track.composition.play).first()
        if target is None:
            return Response({"detail": "目标素材不存在"}, status=400)
        if target.status != AssetStatus.READY:
            return Response({"detail": f"目标素材状态 {target.status}，仅 READY 可切换"},
                            status=400)
        cur = clip.asset
        if cur is not None and (cur.play_id, cur.episode_id, cur.scene_id, cur.shot_id,
                                cur.character_id, cur.asset_type) != (
                target.play_id, target.episode_id, target.scene_id, target.shot_id,
                target.character_id, target.asset_type):
            return Response({"detail": "目标素材与当前素材不在同一位置（六元组不匹配）"},
                            status=400)
        clip.asset = target  # parent 引用链不动
        clip.save(update_fields=["asset", "updated_at"])
        return Response(ClipSerializer(clip).data)
