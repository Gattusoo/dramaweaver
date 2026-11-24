"""creation models —— 小说工作区 + 画布 + 导演台（novel.py 族）。

四族模型：卷章树（Novel/NovelVolume/NovelChapter + ChapterSnapshot 自动快照）、
多维设定库（StoryAsset 五类 + 角色绑定）、实体关系图谱（EntityRelation +
RelationEvent 按章节演变）、无限画布（Canvas/CanvasNode）与导演台 2D
（DirectorScene/DirectorElement/ShotDirecting）。全部挂 Play 下，
权限与隔离复用 PlayScopedViewSet。
"""
from django.conf import settings
from django.db import models

from .play import Play


class StoryAssetType(models.TextChoices):
    PERSON = "PERSON", "人物"
    FACTION = "FACTION", "势力"
    SKILL = "SKILL", "技能"
    FORESHADOWING = "FORESHADOWING", "伏笔"
    LOCATION = "LOCATION", "地点"


# 类型化字段结构：允许字段全集与必填字段（设定库表单与校验共用）
STORY_ASSET_SCHEMAS = {
    StoryAssetType.PERSON: {
        "allowed": {"identity", "personality", "appearance", "goal", "arc", "notes"},
        "required": {"identity"},
    },
    StoryAssetType.FACTION: {
        "allowed": {"purpose", "members", "strength", "relations", "notes"},
        "required": {"purpose"},
    },
    StoryAssetType.SKILL: {
        "allowed": {"effect", "cost", "limitation", "owner", "notes"},
        "required": {"effect"},
    },
    StoryAssetType.FORESHADOWING: {
        "allowed": {"setup_chapter", "payoff_plan", "status", "notes"},
        "required": {"setup_chapter"},
    },
    StoryAssetType.LOCATION: {
        "allowed": {"description", "atmosphere", "geography", "notes"},
        "required": {"description"},
    },
}


class Novel(models.Model):
    """小说容器（一剧一小说，卷章树的根）。"""

    play = models.OneToOneField(
        Play, on_delete=models.CASCADE, related_name="novel", verbose_name="所属短剧"
    )
    title = models.CharField("书名", max_length=200)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_novel"
        verbose_name = "小说"
        verbose_name_plural = "小说"

    def __str__(self):
        return self.title


class NovelVolume(models.Model):
    novel = models.ForeignKey(
        Novel, on_delete=models.CASCADE, related_name="volumes", verbose_name="所属小说"
    )
    title = models.CharField("卷名", max_length=200)
    order = models.PositiveIntegerField("卷序", default=1)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_novelvolume"
        ordering = ["order", "id"]
        unique_together = [("novel", "order")]
        verbose_name = "卷"
        verbose_name_plural = "卷"

    def __str__(self):
        return f"{self.novel.title} - {self.title}"


class NovelChapter(models.Model):
    volume = models.ForeignKey(
        NovelVolume, on_delete=models.CASCADE, related_name="chapters", verbose_name="所属卷"
    )
    chapter_number = models.PositiveIntegerField("章节序号（全书连续）")
    title = models.CharField("章节标题", max_length=200, blank=True)
    content = models.TextField("正文", blank=True)
    # 树状大纲：[{id, text, start, end}]，start/end 为正文字符区间（含 end）
    outline = models.JSONField("大纲锚点", default=list, blank=True)
    # 改编引用计数（novel-adaptation 落剧本时递增；删除提示用）
    adaptation_refs = models.PositiveIntegerField("改编引用计数", default=0)
    is_deleted = models.BooleanField("软删除", default=False, db_index=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_novelchapter"
        ordering = ["chapter_number"]
        unique_together = [("volume", "chapter_number")]
        indexes = [models.Index(fields=["is_deleted", "chapter_number"])]
        verbose_name = "章节"
        verbose_name_plural = "章节"

    def __str__(self):
        return f"第 {self.chapter_number} 章 {self.title}".strip()

    def snapshot(self):
        """保存正文版本快照（内容变化时调用）；保留数量按配置裁剪。"""
        last = self.snapshots.order_by("-pk").first()
        if last is not None and last.content == self.content:
            return last
        snap = ChapterSnapshot.objects.create(chapter=self, content=self.content)
        keep = int(getattr(settings, "NOVEL_SNAPSHOT_KEEP", 20))
        if keep > 0:
            stale = self.snapshots.order_by("-pk").values_list("pk", flat=True)[keep:]
            if stale:
                ChapterSnapshot.objects.filter(pk__in=list(stale)).delete()
        return snap

    def save(self, *args, **kwargs):
        """正文变化时自动产生版本快照（model 层保证：API 与直接 save 都覆盖）。"""
        is_new = self.pk is None
        old_content = None if is_new else (type(self).objects.filter(pk=self.pk)
                                          .values_list("content", flat=True).first())
        super().save(*args, **kwargs)
        if old_content is None or old_content != self.content:
            self.snapshot()
        return self


class ChapterSnapshot(models.Model):
    chapter = models.ForeignKey(
        NovelChapter, on_delete=models.CASCADE, related_name="snapshots", verbose_name="所属章节"
    )
    content = models.TextField("正文快照", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_chaptersnapshot"
        ordering = ["-pk"]
        verbose_name = "章节版本快照"
        verbose_name_plural = "章节版本快照"

    def __str__(self):
        return f"章节 {self.chapter_id} 快照 #{self.pk}"


class StoryAsset(models.Model):
    """多维设定库条目（五类，类型化字段）。"""

    play = models.ForeignKey(
        Play, on_delete=models.CASCADE, related_name="story_assets", verbose_name="所属短剧"
    )
    type = models.CharField("类型", max_length=20, choices=StoryAssetType.choices, db_index=True)
    name = models.CharField("名称", max_length=200)
    # 类型化字段：键受 STORY_ASSET_SCHEMAS[type].allowed 约束（序列化层校验）
    fields = models.JSONField("类型化字段", default=dict, blank=True)
    # 人物资产可绑定既有角色（供定妆/一致性生成消费）
    character = models.ForeignKey(
        "Character", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="story_assets", verbose_name="绑定角色",
    )
    # 关联章节引用（人物在哪些章节出场等）
    chapter_refs = models.ManyToManyField(
        NovelChapter, blank=True, related_name="story_assets", verbose_name="关联章节"
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_storyasset"
        indexes = [models.Index(fields=["play", "type"], name="idx_storyasset_play_type")]
        unique_together = [("play", "type", "name")]
        verbose_name = "故事资产"
        verbose_name_plural = "故事资产"

    def __str__(self):
        return f"[{self.get_type_display()}] {self.name}"


class EntityRelation(models.Model):
    """角色关系边（图谱）：按起始章节生效，演变事件落 RelationEvent。"""

    play = models.ForeignKey(
        Play, on_delete=models.CASCADE, related_name="entity_relations", verbose_name="所属短剧"
    )
    from_character = models.ForeignKey(
        "Character", on_delete=models.CASCADE, related_name="relations_from",
        verbose_name="发起角色",
    )
    to_character = models.ForeignKey(
        "Character", on_delete=models.CASCADE, related_name="relations_to",
        verbose_name="指向角色",
    )
    relation_type = models.CharField("关系类型", max_length=50)
    start_chapter = models.ForeignKey(
        NovelChapter, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="relations_started", verbose_name="起始章节",
    )
    end_chapter = models.ForeignKey(
        NovelChapter, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="relations_ended", verbose_name="结束章节（关系终止）",
    )
    note = models.CharField("备注", max_length=500, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_entityrelation"
        indexes = [models.Index(fields=["play"], name="idx_entityrelation_play")]
        verbose_name = "实体关系"
        verbose_name_plural = "实体关系"

    def __str__(self):
        return f"{self.from_character_id} -[{self.relation_type}]-> {self.to_character_id}"


class RelationEvent(models.Model):
    """关系演变事件节点：发生在某章，描述关系变化。"""

    relation = models.ForeignKey(
        EntityRelation, on_delete=models.CASCADE, related_name="events", verbose_name="所属关系"
    )
    chapter = models.ForeignKey(
        NovelChapter, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="relation_events", verbose_name="发生章节",
    )
    change = models.CharField("变化描述", max_length=200)
    detail = models.TextField("事件详情", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_relationevent"
        ordering = ["chapter__chapter_number", "id"]
        verbose_name = "关系演变事件"
        verbose_name_plural = "关系演变事件"

    def __str__(self):
        return f"关系 {self.relation_id} 事件：{self.change}"


# ── 无限画布（canvas-board，2026-11-23 组 4）───────────────────────────────
# 节点引用既有对象用 FK（Shot/Character/Asset）不复制数据；节点间依赖语义
# 复用 AssetDependency（生成产物间依赖已有体系），画布父子边只表达布局层级。


class Canvas(models.Model):
    """短剧级多画布：节点式分镜/定妆/资产/生图流的工作面。"""

    play = models.ForeignKey(
        Play, on_delete=models.CASCADE, related_name="canvases", verbose_name="所属短剧"
    )
    title = models.CharField("画布名", max_length=200, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_canvas"
        ordering = ["id"]
        verbose_name = "画布"
        verbose_name_plural = "画布"

    def __str__(self):
        return self.title or f"画布 #{self.pk}"


class CanvasNodeType(models.TextChoices):
    SCENE_BLOCK = "SCENE_BLOCK", "分镜段落"
    CHARACTER_STYLING = "CHARACTER_STYLING", "角色定妆"
    ASSET_REF = "ASSET_REF", "资产引用"
    IMAGE_FLOW = "IMAGE_FLOW", "生图流"


class CanvasNode(models.Model):
    """画布节点：四类；坐标 + 布局父节点 + 引用锚。

    引用锚（scene/character/asset 三选一按类型）：
    - SCENE_BLOCK → scene（展示镜头集合，可逐镜头触发生成）
    - CHARACTER_STYLING → character（定妆资产序列）
    - ASSET_REF → asset（任意版本资产卡）
    - IMAGE_FLOW → asset（产出物版本序列锚：首版资产；后续版本经 _scope_filter
      自然进链——节点侧栏按锚位置列出全部版本）
    parent 表达布局层级（缩进/分组），非数据依赖（数据依赖在 AssetDependency）。
    """

    canvas = models.ForeignKey(
        Canvas, on_delete=models.CASCADE, related_name="nodes", verbose_name="所属画布"
    )
    type = models.CharField("节点类型", max_length=30, choices=CanvasNodeType.choices)
    title = models.CharField("节点标题", max_length=200, blank=True)
    # 画布坐标（前端 React Flow x/y）
    x = models.FloatField("X", default=0)
    y = models.FloatField("Y", default=0)
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="children", verbose_name="布局父节点",
    )
    scene = models.ForeignKey(
        "Scene", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="canvas_nodes", verbose_name="引用场景",
    )
    character = models.ForeignKey(
        "Character", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="canvas_nodes", verbose_name="引用角色",
    )
    asset = models.ForeignKey(
        "Asset", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="canvas_nodes", verbose_name="引用资产",
    )
    # 生图流参数：IMAGE_FLOW 节点触发生成时的 prompt（可被镜头级提示词覆盖）
    params = models.JSONField("节点参数", default=dict, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_canvasnode"
        ordering = ["id"]
        indexes = [models.Index(fields=["canvas", "type"], name="idx_canvasnode_canvas_type")]
        verbose_name = "画布节点"
        verbose_name_plural = "画布节点"

    def __str__(self):
        return f"[{self.get_type_display()}] {self.title or self.pk}"

    def anchor_label(self):
        """展示锚名（序列化用）：按类型取引用对象名。"""
        if self.type == CanvasNodeType.SCENE_BLOCK:
            return f"场景 {self.scene.scene_number}" if self.scene else "（场景已删除）"
        if self.type == CanvasNodeType.CHARACTER_STYLING:
            return self.character.name if self.character else "（角色已删除）"
        if self.asset is not None:
            return f"{self.asset.asset_type} v{self.asset.version}"
        return ""


# ── 导演台 2D（director-stage，2026-11-23 组 5）────────────────────────────


class DirectorScene(models.Model):
    """短剧级 2D 布景平面（design D4：MVP 只做 2D，平面图 JSON 对 3D 升级开放）。

    ground_plan 存尺寸网格（如 {"width": 20, "height": 12, "grid": 1}，单位米），
    由前端渲染俯视布景图；元素（DirectorElement）摆放在此平面上。
    """

    play = models.ForeignKey(
        Play, on_delete=models.CASCADE, related_name="director_scenes", verbose_name="所属短剧"
    )
    title = models.CharField("场景名", max_length=200, blank=True)
    # 2D 布景平面（尺寸网格 JSON）：{"width", "height", "grid"}；未来升 3D 只加渲染不改模型
    ground_plan = models.JSONField("布景平面", default=dict, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_directorscene"
        ordering = ["id"]
        verbose_name = "导演台场景"
        verbose_name_plural = "导演台场景"

    def __str__(self):
        return self.title or f"导演台 #{self.pk}"


class DirectorElementType(models.TextChoices):
    CAMERA = "CAMERA", "机位"
    PLACEMENT = "PLACEMENT", "角色摆位"
    CAMERA_MOVE = "CAMERA_MOVE", "运镜路径"


class DirectorElement(models.Model):
    """导演台元素：机位点（含朝向角）/ 角色摆位（引用 Character）/ 运镜路径。

    坐标单位与 ground_plan 一致（米）；朝向角 degrees（0=正北顺时针）；
    运镜路径存 polyline 点列 [[x, y], ...] 与运镜指令文本（move_instruction，
    如"推近/横摇/跟拍"）——文本是注入视频生成 prompt 的主要载体。
    """

    scene = models.ForeignKey(
        DirectorScene, on_delete=models.CASCADE, related_name="elements",
        verbose_name="所属导演台场景",
    )
    type = models.CharField("元素类型", max_length=20, choices=DirectorElementType.choices)
    label = models.CharField("元素标签", max_length=200, blank=True)
    x = models.FloatField("X", default=0)
    y = models.FloatField("Y", default=0)
    # CAMERA：朝向角（degrees）；PLACEMENT 无语义可为空
    heading = models.FloatField("朝向角", null=True, blank=True)
    # CAMERA_MOVE：polyline 点列 [[x, y], ...]；其他类型为空列表
    path = models.JSONField("路径点列", default=list, blank=True)
    # CAMERA_MOVE：运镜指令文本（推近/横摇/跟拍…）——视频生成参考注入载体
    move_instruction = models.TextField("运镜指令", blank=True)
    # PLACEMENT：摆位引用的角色（机位/运镜为空）
    character = models.ForeignKey(
        "Character", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="director_placements", verbose_name="摆位角色",
    )
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_directorelement"
        ordering = ["id"]
        indexes = [models.Index(fields=["scene", "type"], name="idx_directorelement_scene_type")]
        verbose_name = "导演台元素"
        verbose_name_plural = "导演台元素"

    def __str__(self):
        return f"[{self.get_type_display()}] {self.label or self.pk}"

    def describe(self):
        """运镜/构图视角文本（spec：镜头的导演台数据可携带机位视角描述）。

        供生图提示词与视频生成参考注入：机位→朝向描述；运镜→指令文本；
        摆位→角色位置描述。空元素返回空串。数值用 %g 归一（5.0 与 5 同渲染）。
        """
        def _n(v):
            return f"{v:g}" if isinstance(v, (int, float)) else v
        if self.type == DirectorElementType.CAMERA:
            heading = self.heading if self.heading is not None else 0
            return f"机位({_n(self.x)}, {_n(self.y)}) 朝向{_n(heading)}°"
        if self.type == DirectorElementType.CAMERA_MOVE:
            return self.move_instruction
        if self.type == DirectorElementType.PLACEMENT:
            name = self.character.name if self.character else "角色"
            return f"{name} 摆位({_n(self.x)}, {_n(self.y)})"
        return ""


class ShotDirecting(models.Model):
    """镜头↔导演台绑定：镜头绑定机位（+可选运镜元素），构图检查标注落此。

    每镜头至多一条绑定（OneToOne 语义）：绑定后合成/生图的提示词与视频生成
    参考可注入机位视角与运镜描述；composition_notes 存抽帧构图检查结果
    （标注/备注，spec：不新建图像体系）。
    """

    shot = models.OneToOneField(
        "Shot", on_delete=models.CASCADE, related_name="directing",
        verbose_name="绑定镜头",
    )
    camera = models.ForeignKey(
        DirectorElement, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="shot_bindings", verbose_name="绑定机位",
        limit_choices_to={"type": DirectorElementType.CAMERA},
    )
    camera_move = models.ForeignKey(
        DirectorElement, on_delete=models.SET_NULL, null=True, blank=True,
        related_name="shot_move_bindings", verbose_name="绑定运镜",
        limit_choices_to={"type": DirectorElementType.CAMERA_MOVE},
    )
    # 构图检查标注（抽帧检查结果：问题标记/备注文本，驱动重生成提示词）
    composition_notes = models.JSONField("构图检查标注", default=list, blank=True)
    # 视频生成参考帧（URL 或资产 id 引用）：首帧（first）/尾帧（last）
    reference_frames = models.JSONField("参考帧", default=dict, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_shotdirecting"
        verbose_name = "镜头导演台绑定"
        verbose_name_plural = "镜头导演台绑定"

    def __str__(self):
        return f"Shot#{self.shot_id} 导演台"

    def camera_description(self):
        """机位视角 + 运镜描述合并文本（注入生图 prompt 与视频生成参考）。"""
        parts = []
        if self.camera is not None:
            parts.append(self.camera.describe())
        if self.camera_move is not None:
            parts.append(self.camera_move.describe())
        return "；".join(p for p in parts if p)


# ── 多轨剪辑（timeline-editing，2026-11-24 组 6）───────────────────────────


class TrackKind(models.TextChoices):
    VIDEO = "VIDEO", "视频轨"
    OVERLAY = "OVERLAY", "叠加轨"
    DIALOGUE = "DIALOGUE", "对白轨"
    SFX = "SFX", "音效轨"
    MUSIC = "MUSIC", "音乐轨"
    SUBTITLE = "SUBTITLE", "字幕轨"


class Composition(models.Model):
    """剪辑工程：六轨时间线（design D5：自动合成导入→手动精修，不重写渲染器）。

    import_source：自动导入来源（既有 VIDEO 合成产物的 Asset id），手工创建为 None。
    """

    play = models.ForeignKey(
        Play, on_delete=models.CASCADE, related_name="compositions", verbose_name="所属短剧"
    )
    episode = models.ForeignKey(
        "Episode", on_delete=models.CASCADE, related_name="compositions", verbose_name="所属集"
    )
    title = models.CharField("工程名", max_length=200, blank=True)
    # 自动导入来源：VIDEO Asset id（int）或 None（手工创建）
    import_source = models.BigIntegerField("导入来源资产", null=True, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_composition"
        ordering = ["id"]
        verbose_name = "剪辑工程"
        verbose_name_plural = "剪辑工程"

    def __str__(self):
        return self.title or f"剪辑工程 #{self.pk}"


class Track(models.Model):
    """剪辑轨：六类（TrackKind）；每工程每类至多一轨。"""

    composition = models.ForeignKey(
        Composition, on_delete=models.CASCADE, related_name="tracks", verbose_name="所属工程"
    )
    kind = models.CharField("轨类型", max_length=20, choices=TrackKind.choices)
    order = models.PositiveIntegerField("轨序", default=0)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_track"
        ordering = ["order", "id"]
        constraints = [
            models.UniqueConstraint(fields=["composition", "kind"], name="uq_track_comp_kind"),
        ]
        verbose_name = "剪辑轨"
        verbose_name_plural = "剪辑轨"

    def __str__(self):
        return f"{self.composition} · {self.get_kind_display()}"


class Clip(models.Model):
    """轨上片段：入出点（asset 相对秒）+ 时间线起点 + 素材引用（任意版本）。

    - asset：引用既有 Asset 任意版本（版本切换 = 改 asset 引用，父链保留）
    - parent：分割父引用链（分割出的两段都指向原片段，可追溯）
    - text：字幕轨手工文本（asset 为空时字幕轨的合法形态）
    - in_point/out_point：素材内入出点（秒）；duration = out - in
    - start：时间线起点（秒）
    """

    track = models.ForeignKey(
        Track, on_delete=models.CASCADE, related_name="clips", verbose_name="所属轨"
    )
    asset = models.ForeignKey(
        "Asset", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="clips", verbose_name="引用素材",
    )
    parent = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="split_children", verbose_name="分割父片段",
    )
    label = models.CharField("片段标签", max_length=200, blank=True)
    start = models.FloatField("时间线起点", default=0)
    in_point = models.FloatField("入点", default=0)
    out_point = models.FloatField("出点", default=0)
    text = models.TextField("手工文本", blank=True)
    order = models.PositiveIntegerField("片段序", default=0)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_clip"
        ordering = ["track", "order", "id"]
        indexes = [models.Index(fields=["track", "order"], name="idx_clip_track_order")]
        verbose_name = "剪辑片段"
        verbose_name_plural = "剪辑片段"

    def __str__(self):
        return self.label or f"Clip #{self.pk}"

    @property
    def duration(self):
        return max(0.0, float(self.out_point) - float(self.in_point))

    def split_at(self, offset):
        """在素材内 offset 秒处分割为两段（都挂 parent=本片段）。

        返回 (left, right)：left 保留原 in_point～in+offset；right 从 in+offset 到原出点、
        时间线起点顺延。不落库（调用方 save）。
        """
        if not (0 < offset < self.duration):
            raise ValueError("分割点须在片段入出点之间")
        left = Clip(track=self.track, asset=self.asset, parent=self, label=self.label,
                    start=self.start, in_point=self.in_point,
                    out_point=self.in_point + offset, text=self.text, order=self.order)
        right = Clip(track=self.track, asset=self.asset, parent=self, label=self.label,
                     start=self.start + offset, in_point=self.in_point + offset,
                     out_point=self.out_point, text=self.text, order=self.order + 1)
        return left, right
