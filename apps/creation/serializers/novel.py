"""creation serializers —— 小说工作区（novel.py 族）。

四族：卷章树 / 章节快照 / 设定库（类型化字段校验）/ 关系图谱 / 画布 / 导演台。
"""
from rest_framework import serializers

from ..models import (
    Canvas, CanvasNode, CanvasNodeType, ChapterSnapshot, Clip, Composition,
    DirectorElement, DirectorElementType, DirectorScene, EntityRelation, Novel,
    NovelChapter, NovelVolume, RelationEvent, ShotDirecting, Track, TrackKind,
    STORY_ASSET_SCHEMAS, StoryAsset, StoryAssetType,
)
from .play import PlayScopedFieldMixin


def _get_accessible_plays(request):
    """当前用户可访问短剧 id 集合（各 validate_* 防跨剧引用共用）。"""
    if request is None:
        return set()
    from ..permissions import get_accessible_play_ids
    return get_accessible_play_ids(request.user)


class NovelVolumeSerializer(serializers.ModelSerializer):
    chapter_count = serializers.SerializerMethodField()
    # 显式传空=自动排卷序（True 时 validate 阶段按 novel 最大卷序 + 1 覆盖，
    # 绕开 UniqueTogetherValidator 对模型默认 order=1 的误拒）
    order = serializers.IntegerField(required=False, allow_null=True, min_value=1)

    class Meta:
        model = NovelVolume
        fields = ["id", "novel", "title", "order", "chapter_count", "created_at"]
        read_only_fields = ["id", "created_at"]

    def get_chapter_count(self, obj):
        return obj.chapters.filter(is_deleted=False).count()

    def validate_novel(self, novel):
        request = self.context.get("request")
        if novel.play_id not in _get_accessible_plays(request):
            raise serializers.ValidationError("无权访问该小说")
        return novel

    def to_internal_value(self, data):
        # UniqueTogetherValidator 对 unique_together 字段强制隐式 required（缺 order
        # 直接 400），validate() 钩子没机会跑——在进入验证器前补缺省序号。
        if isinstance(data, dict) and data.get("order", "") in ("", None):
            internal = super().to_internal_value(data)
            novel = internal.get("novel") or getattr(self.instance, "novel", None)
            last = novel.volumes.order_by("-order").values_list("order", flat=True).first() or 0
            internal["order"] = last + 1
            return internal
        return super().to_internal_value(data)

    def validate(self, attrs):
        # 未传 order 或显式传 null：按 novel 最大卷序 + 1 自动排（绕 unique 误拒）
        if attrs.get("order") is None:
            novel = attrs.get("novel") or getattr(self.instance, "novel", None)
            last = novel.volumes.order_by("-order").values_list("order", flat=True).first() or 0
            attrs["order"] = last + 1
        return attrs


class ChapterSnapshotSerializer(serializers.ModelSerializer):
    class Meta:
        model = ChapterSnapshot
        fields = ["id", "chapter", "content", "created_at"]
        read_only_fields = fields


class NovelChapterSerializer(serializers.ModelSerializer):
    # 显式传空=自动排全书连续章节号（validate 阶段跨卷取最大 + 1）
    chapter_number = serializers.IntegerField(required=False, allow_null=True, min_value=1)

    class Meta:
        model = NovelChapter
        fields = ["id", "volume", "chapter_number", "title", "content", "outline",
                  "adaptation_refs", "is_deleted", "created_at", "updated_at"]
        read_only_fields = ["id", "adaptation_refs", "is_deleted", "created_at", "updated_at"]

    def validate_volume(self, volume):
        request = self.context.get("request")
        play_ids = _get_accessible_plays(request)
        if not NovelVolume.objects.filter(pk=volume.pk, novel__play_id__in=play_ids).exists():
            raise serializers.ValidationError("无权访问该卷")
        return volume

    def to_internal_value(self, data):
        # 同卷章 unique_together 的隐式 required 问题：缺 chapter_number 时先补
        # 全书连续序号（跨卷最大 + 1）再进验证器。
        if isinstance(data, dict) and data.get("chapter_number", "") in ("", None):
            internal = super().to_internal_value(data)
            volume = internal.get("volume") or getattr(self.instance, "volume", None)
            last = (NovelChapter.objects.filter(volume__novel=volume.novel)
                    .order_by("-chapter_number").values_list("chapter_number", flat=True).first() or 0)
            internal["chapter_number"] = last + 1
            return internal
        return super().to_internal_value(data)

    def validate(self, attrs):
        if attrs.get("chapter_number") is None:
            volume = attrs.get("volume") or getattr(self.instance, "volume", None)
            last = (NovelChapter.objects.filter(volume__novel=volume.novel)
                    .order_by("-chapter_number").values_list("chapter_number", flat=True).first() or 0)
            attrs["chapter_number"] = last + 1
        return attrs

    def validate_outline(self, value):
        if not isinstance(value, list):
            raise serializers.ValidationError("大纲须为列表")
        for i, node in enumerate(value):
            if not isinstance(node, dict):
                raise serializers.ValidationError(f"大纲第 {i + 1} 项须为对象")
            text, start, end = node.get("text"), node.get("start"), node.get("end")
            if not (isinstance(text, str) and text.strip()):
                raise serializers.ValidationError(f"大纲第 {i + 1} 项缺 text")
            if not (isinstance(start, int) and isinstance(end, int) and 0 <= start <= end):
                raise serializers.ValidationError(f"大纲第 {i + 1} 项 start/end 须为非负整数且 start<=end")
        return value

    def update(self, instance, validated_data):
        # 快照在 model.save 内自动产生（serializer 与直接 save 均覆盖）
        return super().update(instance, validated_data)


class NovelSerializer(serializers.ModelSerializer):
    volumes = NovelVolumeSerializer(many=True, read_only=True)

    class Meta:
        model = Novel
        fields = ["id", "play", "title", "volumes", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_play(self, play):
        request = self.context.get("request")
        if play.pk not in _get_accessible_plays(request):
            raise serializers.ValidationError("无权访问该短剧")
        return play


class StoryAssetSerializer(PlayScopedFieldMixin, serializers.ModelSerializer):
    # 不用 _PLAY_SCOPED_FIELDS：mixin 的 _scoped_play_qs 按 play 字段过滤，
    # NovelChapter 的 play 在 volume.novel.play 父链上（构建时即 FieldError）。
    # chapter_refs 的跨剧防护改由 get_fields 覆盖按父链过滤。
    _PLAY_SCOPED_FIELDS = ()

    def get_fields(self):
        fields = super().get_fields()
        field = fields.get("chapter_refs")
        request = self.context.get("request")
        if field is not None and request is not None:
            from ..models import NovelChapter
            field.child_relation.queryset = NovelChapter.objects.filter(
                volume__novel__play_id__in=_get_accessible_plays(request))
        return fields

    class Meta:
        model = StoryAsset
        fields = ["id", "play", "type", "name", "fields", "character", "chapter_refs",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate(self, attrs):
        stype = attrs.get("type") or getattr(self.instance, "type", None)
        schema = STORY_ASSET_SCHEMAS.get(stype)
        if schema is None:
            raise serializers.ValidationError({"type": "未知设定类型"})
        fields_data = attrs.get("fields")
        if fields_data is None:
            fields_data = getattr(self.instance, "fields", {}) or {}
        if not isinstance(fields_data, dict):
            raise serializers.ValidationError({"fields": "类型化字段须为对象"})
        unknown = set(fields_data) - schema["allowed"]
        if unknown:
            raise serializers.ValidationError(
                {"fields": f"类型 {stype} 不支持字段：{', '.join(sorted(unknown))}"})
        missing = schema["required"] - set(fields_data)
        if missing:
            raise serializers.ValidationError(
                {"fields": f"类型 {stype} 缺少必填字段：{', '.join(sorted(missing))}"})
        # 人物资产才可绑定角色
        if attrs.get("character") is not None and stype != StoryAssetType.PERSON:
            raise serializers.ValidationError({"character": "仅人物资产可绑定角色"})
        return attrs


class RelationEventSerializer(serializers.ModelSerializer):
    chapter_number = serializers.IntegerField(source="chapter.chapter_number", read_only=True,
                                              default=None)

    class Meta:
        model = RelationEvent
        fields = ["id", "relation", "chapter", "chapter_number", "change", "detail", "created_at"]
        read_only_fields = ["id", "created_at"]


class EntityRelationSerializer(serializers.ModelSerializer):
    events = RelationEventSerializer(many=True, read_only=True)
    from_character_name = serializers.CharField(source="from_character.name", read_only=True)
    to_character_name = serializers.CharField(source="to_character.name", read_only=True)

    class Meta:
        model = EntityRelation
        fields = ["id", "play", "from_character", "from_character_name", "to_character",
                  "to_character_name", "relation_type", "start_chapter", "end_chapter",
                  "note", "events", "created_at"]
        read_only_fields = ["id", "created_at"]


# ── 无限画布（canvas-board，2026-11-23 组 4）─────────────────────────────


class CanvasSerializer(serializers.ModelSerializer):
    node_count = serializers.SerializerMethodField()

    class Meta:
        model = Canvas
        fields = ["id", "play", "title", "node_count", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_node_count(self, obj):
        return obj.nodes.count()

    def validate_play(self, play):
        request = self.context.get("request")
        if play.pk not in _get_accessible_plays(request):
            raise serializers.ValidationError("无权访问该短剧")
        return play


class CanvasNodeSerializer(serializers.ModelSerializer):
    anchor_label = serializers.SerializerMethodField()
    # 版本序列（IMAGE_FLOW / CHARACTER_STYLING / ASSET_REF：按锚位置列全部版本）
    versions = serializers.SerializerMethodField()

    class Meta:
        model = CanvasNode
        fields = ["id", "canvas", "type", "title", "x", "y", "parent", "scene",
                  "character", "asset", "params", "anchor_label", "versions",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "anchor_label", "versions", "created_at", "updated_at"]

    def get_anchor_label(self, obj):
        return obj.anchor_label()

    def get_versions(self, obj):
        """锚位置的版本序列（新→旧）：生图流/定妆/资产引用节点侧栏数据源。

        版本链语义与 Asset._scope_filter 一致（六元组同位置）。
        """
        if obj.asset is None:
            return []
        from ..models import Asset
        scope = dict(
            play=obj.asset.play, episode=obj.asset.episode, scene=obj.asset.scene,
            shot=obj.asset.shot, character=obj.asset.character,
            asset_type=obj.asset.asset_type,
        )
        qs = Asset.objects.filter(**scope)
        return [{"id": a.pk, "version": a.version, "status": a.status,
                 "artifact_url": a.artifact_url, "created_at": a.created_at}
                for a in qs.order_by("-version")[:50]]

    def validate(self, attrs):
        ntype = attrs.get("type") or getattr(self.instance, "type", None)
        scene = attrs.get("scene", getattr(self.instance, "scene", None))
        character = attrs.get("character", getattr(self.instance, "character", None))
        asset = attrs.get("asset", getattr(self.instance, "asset", None))
        # 引用锚与类型匹配：SCENE_BLOCK→scene、CHARACTER_STYLING→character、
        # ASSET_REF/IMAGE_FLOW→asset；跨剧引用由父键守卫拦截
        if ntype == CanvasNodeType.SCENE_BLOCK and scene is None:
            raise serializers.ValidationError({"scene": "分镜段落节点须引用场景"})
        if ntype == CanvasNodeType.CHARACTER_STYLING and character is None:
            raise serializers.ValidationError({"character": "角色定妆节点须引用角色"})
        if ntype in (CanvasNodeType.ASSET_REF, CanvasNodeType.IMAGE_FLOW) and asset is None:
            raise serializers.ValidationError({"asset": "该节点类型须引用资产"})
        parent = attrs.get("parent", getattr(self.instance, "parent", None))
        canvas = attrs.get("canvas") or getattr(self.instance, "canvas", None)
        if parent is not None and parent.canvas_id != canvas.pk:
            raise serializers.ValidationError({"parent": "父节点不在同一画布"})
        return attrs


# ── 导演台 2D（director-stage，2026-11-23 组 5）───────────────────────────


class DirectorSceneSerializer(serializers.ModelSerializer):
    element_count = serializers.SerializerMethodField()

    class Meta:
        model = DirectorScene
        fields = ["id", "play", "title", "ground_plan", "element_count",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def get_element_count(self, obj):
        return obj.elements.count()

    def validate_play(self, play):
        request = self.context.get("request")
        if play.pk not in _get_accessible_plays(request):
            raise serializers.ValidationError("无权访问该短剧")
        return play

    def validate_ground_plan(self, value):
        if value in (None, {}):
            return value or {}
        if not isinstance(value, dict):
            raise serializers.ValidationError("布景平面须为对象")
        for key in ("width", "height"):
            if key in value:
                w = value[key]
                if not (isinstance(w, (int, float)) and w > 0):
                    raise serializers.ValidationError(f"{key} 须为正数")
        return value


class DirectorElementSerializer(serializers.ModelSerializer):
    class Meta:
        model = DirectorElement
        fields = ["id", "scene", "type", "label", "x", "y", "heading", "path",
                  "move_instruction", "character", "created_at", "updated_at"]
        read_only_fields = ["id", "created_at", "updated_at"]

    def validate_scene(self, scene):
        request = self.context.get("request")
        if scene.play_id not in _get_accessible_plays(request):
            raise serializers.ValidationError("无权访问该导演台场景")
        return scene

    def validate(self, attrs):
        etype = attrs.get("type") or getattr(self.instance, "type", None)
        character = attrs.get("character", getattr(self.instance, "character", None))
        if etype == DirectorElementType.PLACEMENT and character is None:
            raise serializers.ValidationError({"character": "角色摆位元素须引用角色"})
        if etype != DirectorElementType.PLACEMENT and character is not None:
            raise serializers.ValidationError({"character": "仅角色摆位元素可引用角色"})
        path = attrs.get("path", getattr(self.instance, "path", None))
        if path:
            if etype != DirectorElementType.CAMERA_MOVE:
                raise serializers.ValidationError({"path": "仅运镜路径元素可存 polyline"})
            if (not isinstance(path, list) or not path
                    or not all(isinstance(p, (list, tuple)) and len(p) == 2
                               and all(isinstance(v, (int, float)) for v in p) for p in path)):
                raise serializers.ValidationError({"path": "路径须为 [[x, y], ...] 点列"})
        return attrs


class ShotDirectingSerializer(serializers.ModelSerializer):
    camera_description = serializers.SerializerMethodField()

    class Meta:
        model = ShotDirecting
        fields = ["id", "shot", "camera", "camera_move", "composition_notes",
                  "reference_frames", "camera_description", "created_at", "updated_at"]
        read_only_fields = ["id", "camera_description", "created_at", "updated_at"]

    def get_camera_description(self, obj):
        return obj.camera_description()

    def validate_shot(self, shot):
        request = self.context.get("request")
        from ..models import Scene
        if not Scene.objects.filter(
                pk=shot.scene_id, episode__play_id__in=_get_accessible_plays(request)).exists():
            raise serializers.ValidationError("无权访问该镜头")
        return shot

    def validate(self, attrs):
        # 绑定元素类型匹配 + 与镜头同剧（跨剧引用由父键守卫兜底，这里给出明确报错）
        camera = attrs.get("camera", getattr(self.instance, "camera", None))
        camera_move = attrs.get("camera_move", getattr(self.instance, "camera_move", None))
        if camera is not None:
            if camera.type != DirectorElementType.CAMERA:
                raise serializers.ValidationError({"camera": "绑定的元素须为机位类型"})
            shot = attrs.get("shot") or getattr(self.instance, "shot", None)
            if shot is not None and camera.scene.play_id != shot.scene.episode.play_id:
                raise serializers.ValidationError({"camera": "机位与镜头不在同一短剧"})
        if camera_move is not None:
            if camera_move.type != DirectorElementType.CAMERA_MOVE:
                raise serializers.ValidationError({"camera_move": "绑定的元素须为运镜类型"})
            shot = attrs.get("shot") or getattr(self.instance, "shot", None)
            if shot is not None and camera_move.scene.play_id != shot.scene.episode.play_id:
                raise serializers.ValidationError({"camera_move": "运镜与镜头不在同一短剧"})
        frames = attrs.get("reference_frames")
        if frames is not None:
            if not isinstance(frames, dict):
                raise serializers.ValidationError({"reference_frames": "参考帧须为对象"})
            unknown = set(frames) - {"first", "last"}
            if unknown:
                raise serializers.ValidationError(
                    {"reference_frames": f"不支持的参考帧键：{', '.join(sorted(unknown))}"})
        notes = attrs.get("composition_notes")
        if notes is not None and not isinstance(notes, list):
            raise serializers.ValidationError({"composition_notes": "构图标注须为列表"})
        return attrs


# ── 多轨剪辑（timeline-editing，2026-11-24 组 6）──────────────────────────


class ClipSerializer(serializers.ModelSerializer):
    duration = serializers.SerializerMethodField()
    asset_version = serializers.SerializerMethodField()

    class Meta:
        model = Clip
        fields = ["id", "track", "asset", "parent", "label", "start", "in_point",
                  "out_point", "duration", "text", "order", "asset_version",
                  "created_at", "updated_at"]
        read_only_fields = ["id", "duration", "asset_version", "created_at", "updated_at"]

    def get_duration(self, obj):
        return obj.duration

    def get_asset_version(self, obj):
        return obj.asset.version if obj.asset is not None else None

    def validate(self, attrs):
        track = attrs.get("track") or getattr(self.instance, "track", None)
        asset = attrs.get("asset", getattr(self.instance, "asset", None))
        text = attrs.get("text", getattr(self.instance, "text", ""))
        in_p = attrs.get("in_point", getattr(self.instance, "in_point", 0) or 0)
        out_p = attrs.get("out_point", getattr(self.instance, "out_point", 0) or 0)
        # 字幕轨：asset 或手工文本二选一；其他轨：须引素材（空引用由导出校验阻断）
        if track.kind == TrackKind.SUBTITLE:
            if asset is None and not (text or "").strip():
                raise serializers.ValidationError(
                    {"text": "字幕片段须引用素材或提供手工文本"})
        if asset is None and track.kind != TrackKind.SUBTITLE:
            raise serializers.ValidationError({"asset": "该轨片段须引用素材"})
        if asset is not None:
            request = self.context.get("request")
            from ..models import Asset
            if not Asset.objects.filter(
                    pk=asset.pk, play_id__in=_get_accessible_plays(request)).exists():
                raise serializers.ValidationError({"asset": "无权引用该素材"})
        if out_p < in_p:
            raise serializers.ValidationError({"out_point": "出点不得早于入点"})
        parent = attrs.get("parent", getattr(self.instance, "parent", None))
        if parent is not None and parent.track_id != track.pk:
            raise serializers.ValidationError({"parent": "分割父片段不在同一轨"})
        return attrs


class TrackSerializer(serializers.ModelSerializer):
    clips = ClipSerializer(many=True, read_only=True)

    class Meta:
        model = Track
        fields = ["id", "composition", "kind", "order", "clips", "created_at"]
        read_only_fields = ["id", "created_at"]

    def validate_composition(self, composition):
        request = self.context.get("request")
        play_ids = _get_accessible_plays(request)
        if composition.play_id not in play_ids:
            raise serializers.ValidationError("无权访问该剪辑工程")
        # 读成员引用工程可过（新建行发生在写路径——view.perform_create 的
        # can_write 守卫兜底），写路径权限不足时先给出 403 语义
        from rest_framework.exceptions import PermissionDenied
        from ..permissions import can_write_play
        method = (getattr(request, "method", "") or "").upper()
        if request is not None and method not in ("GET", "HEAD", "OPTIONS") \
                and not can_write_play(request.user, composition.play):
            raise PermissionDenied("无权修改该短剧的剪辑工程")
        return composition

    def validate(self, attrs):
        composition = attrs.get("composition") or getattr(self.instance, "composition", None)
        kind = attrs.get("kind") or getattr(self.instance, "kind", None)
        qs = Track.objects.filter(composition=composition, kind=kind)
        if self.instance is not None:
            qs = qs.exclude(pk=self.instance.pk)
        if qs.exists():
            raise serializers.ValidationError({"kind": "每工程每类轨至多一条"})
        return attrs


class CompositionSerializer(serializers.ModelSerializer):
    track_kinds = serializers.SerializerMethodField()
    clip_count = serializers.SerializerMethodField()

    class Meta:
        model = Composition
        fields = ["id", "play", "episode", "title", "import_source", "track_kinds",
                  "clip_count", "created_at", "updated_at"]
        read_only_fields = ["id", "import_source", "track_kinds", "clip_count",
                            "created_at", "updated_at"]

    def get_track_kinds(self, obj):
        return list(obj.tracks.values_list("kind", flat=True))

    def get_clip_count(self, obj):
        return Clip.objects.filter(track__composition=obj).count()

    def validate_play(self, play):
        request = self.context.get("request")
        if play.pk not in _get_accessible_plays(request):
            raise serializers.ValidationError("无权访问该短剧")
        return play

    def validate_episode(self, episode):
        request = self.context.get("request")
        if episode.play_id not in _get_accessible_plays(request):
            raise serializers.ValidationError("无权访问该集")
        return episode
