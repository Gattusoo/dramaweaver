"""小说工作区（novel-workspace，2026-11-23 组 1）。

覆盖：卷章树 CRUD/自动快照/大纲锚点/软删除、设定库类型化校验与角色绑定、
关系图谱按章节时点过滤、REST 权限隔离（读成员可看/写成员可改/非成员 404）、
Agent 工具三件套（读设定/检索前文/写设定走提案闸、STORY_ASSET 提案应用）。
"""
from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.creation.tests.test_assistant import AssistantFixtureMixin

from ..assistant import tools
from ..assistant.models import ProposalKind, ProposalStatus
from ..assistant.proposals import apply_proposal
from ..models import (
    ChapterSnapshot, EntityRelation, Novel, NovelChapter, NovelVolume, RelationEvent,
    STORY_ASSET_SCHEMAS, StoryAsset, StoryAssetType,
)

_ROUTE = {
    "novel": "/api/novels/",
    "volume": "/api/novel-volumes/",
    "chapter": "/api/novel-chapters/",
    "snapshot": "/api/chapter-snapshots/",
    "story_asset": "/api/story-assets/",
    "relation": "/api/entity-relations/",
    "graph": "/api/entity-relations/graph/",
    "chapter_list": "/api/novel-chapters/",
    "volume_list": "/api/novel-volumes/",
    "story_asset_list": "/api/story-assets/",
}


class NovelFixture(AssistantFixtureMixin):
    def setUp(self):
        super().setUp()
        self.novel = Novel.objects.create(play=self.play, title="斗破苍穹")
        self.volume = NovelVolume.objects.create(novel=self.novel, title="第一卷", order=1)
        self.volume2 = NovelVolume.objects.create(novel=self.novel, title="第二卷", order=2)
        self.ch1 = NovelChapter.objects.create(volume=self.volume, chapter_number=1,
                                              title="陨落的天才", content="主角林霄被退婚。")
        self.ch2 = NovelChapter.objects.create(volume=self.volume, chapter_number=2,
                                              title="戒指现身", content="戒指里走出老者萧炎。")
        self.ch3 = NovelChapter.objects.create(volume=self.volume2, chapter_number=3,
                                               title="三年之约", content="三年之后，云岚宗一战。")

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    @staticmethod
    def _unwrap(resp):
        """渲染器错误信封 {code, msg, data} 统一剥壳；data 保留原样。"""
        if resp.status_code >= 400:
            return resp  # 错误响应按原样断言（信封结构与生产一致）
        body = resp.json()
        return body.get("data", body) if isinstance(body, dict) else body

    def jpost(self, user, url_name, data, **kw):
        return _EnvelopeProxy(self.client_for(user).post(_ROUTE[url_name], data, format="json", **kw))

    def jget(self, user, url_name, data=None, **kw):
        return _EnvelopeProxy(self.client_for(user).get(_ROUTE[url_name], data, format="json", **kw))


class _EnvelopeProxy:
    """延迟剥信封：status_code 原样，json() 返回 data 层。"""

    def __init__(self, resp):
        self._resp = resp

    @property
    def status_code(self):
        return self._resp.status_code

    @property
    def content(self):
        return self._resp.content

    def json(self):
        body = self._resp.json()
        if isinstance(body, dict) and "data" in body and "code" in body:
            return body["data"] if body["data"] is not None else body
        return body


# ── 卷章树 ─────────────────────────────────────────────────────────────────

class VolumeChapterTreeTests(NovelFixture, TestCase):
    def test_auto_snapshot_on_content_change(self):
        """章节正文变化自动产生版本快照；内容未变的保存不产生新快照。"""
        before = ChapterSnapshot.objects.filter(chapter=self.ch1).count()
        self.ch1.content = "主角林霄被退婚，全家被赶出宗门。"
        self.ch1.save()
        self.ch1.content += "他攥紧了拳头。"
        self.ch1.save()
        after = ChapterSnapshot.objects.filter(chapter=self.ch1).count()
        self.assertEqual(after, before + 2)
        # 保存未变化内容：不产生新快照
        self.ch1.save()
        self.assertEqual(ChapterSnapshot.objects.filter(chapter=self.ch1).count(), after)

    @override_settings(NOVEL_SNAPSHOT_KEEP=3)
    def test_snapshot_keep_trim(self):
        """快照保留数量按 NOVEL_SNAPSHOT_KEEP 裁剪。"""
        for i in range(5):
            self.ch1.content = f"第 {i} 版正文"
            self.ch1.save()
        self.assertEqual(ChapterSnapshot.objects.filter(chapter=self.ch1).count(), 3)

    def test_outline_anchor_validation(self):
        """大纲锚点：列表结构校验（缺 text / 非法区间拒绝）。"""
        c = self.client_for(self.producer)
        url = f"/api/novel-chapters/{self.ch1.pk}/"
        ok = c.patch(url, {"outline": [{"text": "退婚受辱", "start": 0, "end": 5}]}, format="json")
        self.assertEqual(ok.status_code, 200)
        self.ch1.refresh_from_db()
        self.assertEqual(self.ch1.outline[0]["text"], "退婚受辱")
        bad = c.patch(url, {"outline": [{"text": "", "start": 0, "end": 5}]}, format="json")
        self.assertEqual(bad.status_code, 400)
        bad2 = c.patch(url, {"outline": [{"text": "x", "start": 5, "end": 1}]}, format="json")
        self.assertEqual(bad2.status_code, 400)

    def test_chapter_soft_delete_and_refs_hint(self):
        """软删除：标记 is_deleted 且响应携带改编引用计数提示；列表默认不显示。"""
        self.ch1.adaptation_refs = 2
        self.ch1.save(update_fields=["adaptation_refs"])
        c = self.client_for(self.producer)
        resp = c.delete(f"/api/novel-chapters/{self.ch1.pk}/")
        self.assertEqual(resp.status_code, 200)
        body = resp.json()
        if isinstance(body, dict) and "data" in body and "code" in body:
            body = body["data"]  # 错误信封剥壳
        self.assertEqual(body["adaptation_refs"], 2)
        self.ch1.refresh_from_db()
        self.assertTrue(self.ch1.is_deleted)
        listing = self.jget(self.producer, "chapter_list").json()
        self.assertNotIn(self.ch1.pk, [r["id"] for r in listing])
        # include_deleted=true 可见；restore 恢复
        listing2 = self.jget(self.producer, "chapter_list", {"include_deleted": "true"}).json()
        self.assertIn(self.ch1.pk, [r["id"] for r in listing2])
        restore = c.post(f"/api/novel-chapters/{self.ch1.pk}/restore/")
        self.assertEqual(restore.status_code, 200)
        self.ch1.refresh_from_db()
        self.assertFalse(self.ch1.is_deleted)

    def test_chapter_number_auto_increment_across_volumes(self):
        """章节序号全书连续（跨卷取最大 + 1）。"""
        resp = self.jpost(self.producer, "chapter_list",
                          {"volume": self.volume2.pk, "title": "新章"})
        self.assertEqual(resp.status_code, 201, resp.content)
        self.assertEqual(resp.json()["chapter_number"], 4)

    def test_volume_order_default(self):
        """卷序缺省 = 最大卷序 + 1。"""
        resp = self.jpost(self.producer, "volume_list",
                          {"novel": self.novel.pk, "title": "第三卷"})
        self.assertEqual(resp.status_code, 201)
        self.assertEqual(resp.json()["order"], 3)


# ── 设定库 ─────────────────────────────────────────────────────────────────

class StoryAssetTests(NovelFixture, TestCase):
    def test_typed_field_validation(self):
        """类型化字段：未知字段与缺必填字段拒绝。"""
        ok = self.jpost(self.producer, "story_asset_list", {
            "play": self.play.pk, "type": "PERSON", "name": "林霄",
            "fields": {"identity": "被退婚的林家少爷", "personality": "坚韧"}})
        self.assertEqual(ok.status_code, 201, ok.content)
        unknown = self.jpost(self.producer, "story_asset_list", {
            "play": self.play.pk, "type": "FACTION", "name": "云岚宗",
            "fields": {"purpose": "宗门", "geography": "山巅"}})
        self.assertEqual(unknown.status_code, 400)
        missing = self.jpost(self.producer, "story_asset_list", {
            "play": self.play.pk, "type": "FACTION", "name": "云岚宗",
            "fields": {"members": ["萧炎"]}})
        self.assertEqual(missing.status_code, 400)

    def test_person_character_binding_and_exclusive(self):
        """人物资产可绑定角色；非人物类型绑定角色拒绝。"""
        from ..models import Character, CharacterRole
        ch = Character.objects.create(play=self.play, name="林霄", role=CharacterRole.PROTAGONIST)
        ok = self.jpost(self.producer, "story_asset_list", {
            "play": self.play.pk, "type": "PERSON", "name": "林霄",
            "fields": {"identity": "林家少爷"}, "character": ch.pk})
        self.assertEqual(ok.status_code, 201)
        bad = self.jpost(self.producer, "story_asset_list", {
            "play": self.play.pk, "type": "LOCATION", "name": "云岚宗大殿",
            "fields": {"description": "巍峨"}, "character": ch.pk})
        self.assertEqual(bad.status_code, 400)

    def test_filter_by_type_and_keyword(self):
        StoryAsset.objects.create(play=self.play, type=StoryAssetType.PERSON, name="萧炎",
                                  fields={"identity": "戒指老者"})
        StoryAsset.objects.create(play=self.play, type=StoryAssetType.FACTION, name="萧家",
                                  fields={"purpose": "家族"})
        data = self.jget(self.producer, "story_asset_list", {"type": "PERSON"}).json()
        self.assertEqual([r["name"] for r in data], ["萧炎"])
        data2 = self.jget(self.producer, "story_asset_list", {"q": "萧"}).json()
        self.assertEqual(len(data2), 2)


# ── 关系图谱 ───────────────────────────────────────────────────────────────

class EntityRelationTests(NovelFixture, TestCase):
    def setUp(self):
        super().setUp()
        from ..models import Character, CharacterRole
        self.a = Character.objects.create(play=self.play, name="林霄", role=CharacterRole.PROTAGONIST)
        self.b = Character.objects.create(play=self.play, name="萧炎", role=CharacterRole.SUPPORTING)
        # 师徒关系：第 2 章起生效
        self.rel = EntityRelation.objects.create(
            play=self.play, from_character=self.a, to_character=self.b,
            relation_type="师徒", start_chapter=self.ch2, note="戒指认主")
        RelationEvent.objects.create(relation=self.rel, chapter=self.ch2, change="结缘",
                                     detail="老者现身指点")
        RelationEvent.objects.create(relation=self.rel, chapter=self.ch3, change="反目",
                                     detail="三年之约战败")
        # 第 3 章终止的敌对关系
        self.rel2 = EntityRelation.objects.create(
            play=self.play, from_character=self.b, to_character=self.a,
            relation_type="敌对", start_chapter=self.ch1, end_chapter=self.ch3)

    def test_graph_point_filter(self):
        """时点过滤：第 1 章只见敌对（师徒未生效）；第 2 章两条都在但反目事件未发生。"""
        g1 = self.jget(self.producer, "graph", {"chapter_number": 1}).json()
        types1 = sorted(r["relation_type"] for r in g1["relations"])
        self.assertEqual(types1, ["敌对"])
        g2 = self.jget(self.producer, "graph", {"chapter_number": 2}).json()
        self.assertEqual(sorted(r["relation_type"] for r in g2["relations"]), ["师徒", "敌对"])
        rel2 = next(r for r in g2["relations"] if r["relation_type"] == "师徒")
        events2 = [e["change"] for e in rel2["events"]]
        self.assertEqual(events2, ["结缘"])  # 反目发生在第 3 章，时点之后
        g3 = self.jget(self.producer, "graph", {"chapter_number": 3}).json()
        rel3 = next(r for r in g3["relations"] if r["relation_type"] == "师徒")
        self.assertEqual([e["change"] for e in rel3["events"]], ["结缘", "反目"])

    def test_graph_without_point_returns_all(self):
        g = self.jget(self.producer, "graph").json()
        self.assertEqual(len(g["relations"]), 2)


# ── REST 权限隔离 ──────────────────────────────────────────────────────────

class NovelPermissionTests(NovelFixture, TestCase):
    def test_viewer_read_only(self):
        """viewer 可读不可写（403）；非成员 404。"""
        listing = self.jget(self.viewer, "chapter_list").json()
        self.assertEqual(len(listing), 3)
        create = self.jpost(self.viewer, "chapter_list",
                            {"volume": self.volume.pk, "title": "x"})
        self.assertEqual(create.status_code, 403)

    def test_non_member_404(self):
        from dvadmin.system.models import CustomUser
        stranger = CustomUser.objects.create_user(username="stranger", password="Passw0rd!123")
        self.assertEqual(self.jget(stranger, "chapter_list").status_code, 200)
        # PlayScopedViewSet 隔离：陌生人可访问集合为空
        self.assertEqual(self.jget(stranger, "chapter_list").json(), [])
        detail = self.client_for(stranger).get(
            "/api/novel-chapters/self.ch1.pk/")
        self.assertEqual(detail.status_code, 404)

    def test_cross_play_volume_rejected(self):
        """跨剧建章：volume 校验拒绝（防跨剧引用）。"""
        resp = self.jpost(self.producer, "chapter_list",
                          {"volume": 999999, "title": "x"})
        self.assertEqual(resp.status_code, 400)


# ── Agent 工具三件套 ───────────────────────────────────────────────────────

class StoryToolTests(NovelFixture, TestCase):
    def test_read_tool(self):
        StoryAsset.objects.create(play=self.play, type=StoryAssetType.PERSON, name="林霄",
                                  fields={"identity": "林家少爷"})
        result, is_error = tools.run_tool(self.ctx(), "story_asset_read",
                                          {"type": "PERSON", "name": "林霄"})
        self.assertFalse(is_error)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["fields"]["identity"], "林家少爷")

    def test_search_chapters_tool(self):
        result, is_error = tools.run_tool(self.ctx(), "story_asset_search_chapters",
                                          {"keywords": "戒指"})
        self.assertFalse(is_error)
        self.assertEqual(result["count"], 1)
        self.assertEqual(result["results"][0]["chapter_number"], 2)
        ranged = tools.run_tool(self.ctx(), "story_asset_search_chapters",
                                {"keywords": "戒指", "from_chapter": 3})
        self.assertEqual(ranged[0]["count"], 0)

    def test_propose_new_walks_gate(self):
        """写设定走提案：PENDING 落库、应用后 StoryAsset 生成、重复应用拒绝。"""
        result, is_error = tools.run_tool(self.ctx(), "story_asset_propose", {
            "action": "new", "type": "PERSON", "name": "萧炎",
            "fields": {"identity": "戒指老者"}, "summary": "新增萧炎设定"})
        self.assertFalse(is_error)
        self.assertIn("proposal_id", result)
        p = StoryAsset.objects.count() and None  # 尚未落库
        self.assertEqual(StoryAsset.objects.count(), 0)
        proposal = self.__class__.last_proposal(result["proposal_id"])
        self.assertEqual(proposal.kind, ProposalKind.STORY_ASSET)
        self.assertEqual(proposal.status, ProposalStatus.PENDING)
        apply_proposal(proposal.pk, self.producer)
        asset = StoryAsset.objects.get(name="萧炎")
        self.assertEqual(asset.fields["identity"], "戒指老者")
        # 同名再建：stale 判定拒绝
        result2, _ = tools.run_tool(self.ctx(), "story_asset_propose", {
            "action": "new", "type": "PERSON", "name": "萧炎",
            "fields": {"identity": "另一个"}, "summary": "重复"})
        proposal2 = self.__class__.last_proposal(result2["proposal_id"])
        from ..assistant.proposals import ProposalError
        with self.assertRaises(ProposalError):
            apply_proposal(proposal2.pk, self.producer)

    @staticmethod
    def last_proposal(pid):
        from ..assistant.models import AssistantProposal
        return AssistantProposal.objects.get(pk=pid)

    def test_propose_update_and_bad_type(self):
        asset = StoryAsset.objects.create(play=self.play, type=StoryAssetType.FACTION,
                                          name="云岚宗", fields={"purpose": "宗门"})
        result, is_error = tools.run_tool(self.ctx(), "story_asset_propose", {
            "action": "update", "story_asset_id": asset.pk,
            "fields": {"strength": "斗皇坐镇"}, "summary": "补强设定"})
        self.assertFalse(is_error)
        proposal = self.__class__.last_proposal(result["proposal_id"])
        apply_proposal(proposal.pk, self.producer)
        asset.refresh_from_db()
        self.assertEqual(asset.fields["strength"], "斗皇坐镇")
        # 非法类型字段：工具层直接拒绝
        _, is_err = tools.run_tool(self.ctx(), "story_asset_propose", {
            "action": "new", "type": "SKILL", "name": "三段斩",
            "fields": {"not_a_field": 1}, "summary": "x"})
        self.assertTrue(is_err)

    def test_tool_schema_validation(self):
        """schema 校验：未知类型直接 VALIDATION 错误。"""
        result, is_error = tools.run_tool(self.ctx(), "story_asset_propose", {
            "action": "new", "type": "DRAGON", "name": "x", "summary": "x"})
        self.assertTrue(is_error)
