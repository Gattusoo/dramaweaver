"""提案树 / 多方案 / 剧本版本分支测试（Phase 2.4）。"""
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APITestCase

from apps.creation.assistant import tools, variants
from apps.creation.assistant.models import (
    AssistantProposal, AssistantSession, CharacterVersion, ProposalKind, ProposalStatus,
    ScriptVersion,
)
from apps.creation.assistant.proposals import (
    alternatives_of, apply_selected, compare_proposals, create_proposal, proposal_tree,
)
from apps.creation.assistant.script_versions import (
    VersionConflict, VersionError, branches, character_version_diff, create_branch, create_version,
    merge_versions, record_character_version, script_diff, snapshot_episode,
)
from apps.creation.models import AssetType, Character, Episode, Scene

from .test_assistant import AssistantFixtureMixin, FakeChatGateway, _say


class ProposalTreeTests(AssistantFixtureMixin, TestCase):
    def make(self, text="初稿", group=None, label="", parent=None, kind=ProposalKind.EPISODE_SCRIPT,
             stale_siblings=True, **kw):
        return create_proposal(
            self.session, None, kind, payload={"text": text}, summary=label or text,
            episode=self.ep if kind == ProposalKind.EPISODE_SCRIPT else None,
            base_version=self.script.version,
            alternative_group=group, alternative_label=label, parent=parent,
            stale_siblings=stale_siblings, **kw)

    def test_parent_chain(self):
        root = self.make("root")
        child = self.make("child", parent=root)
        grand = self.make("grand", parent=child)
        tree = proposal_tree(grand.pk)
        self.assertEqual([p.pk for p in tree["ancestors"]], [root.pk, child.pk])
        self.assertEqual([p.pk for p in tree["children"]], [])

    def test_children_listed(self):
        root = self.make("root")
        c1, c2 = self.make("c1", parent=root), self.make("c2", parent=root)
        self.assertEqual({p.pk for p in proposal_tree(root.pk)["children"]}, {c1.pk, c2.pk})

    def test_group_siblings_survive(self):
        import uuid
        group = uuid.uuid4()
        a = self.make("A", group=group, label="紧凑", stale_siblings=False)
        b = self.make("B", group=group, label="细腻", stale_siblings=False)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual((a.status, b.status), (ProposalStatus.PENDING, ProposalStatus.PENDING))
        self.assertEqual([p.pk for p in alternatives_of(a)], [b.pk])

    def test_new_proposal_stales_other_group(self):
        import uuid
        group = uuid.uuid4()
        old = self.make("old")
        new = self.make("new", group=group, label="x", stale_siblings=False)
        old.refresh_from_db()
        self.assertEqual(old.status, ProposalStatus.STALE)
        self.assertEqual(new.status, ProposalStatus.PENDING)

    def test_compare_detects_differences(self):
        import uuid
        group = uuid.uuid4()
        a = self.make("甲方案内容", group=group, label="A", stale_siblings=False)
        b = self.make("乙方案内容", group=group, label="B", stale_siblings=False)
        data = compare_proposals([a.pk, b.pk])
        self.assertEqual(len(data["proposals"]), 2)
        self.assertEqual(data["proposals"][0]["label"], "A")
        self.assertIn("text", data["differences"])
        self.assertNotEqual(data["differences"]["text"][0], data["differences"]["text"][1])

    def test_compare_rejects_missing(self):
        from apps.creation.assistant.proposals import ProposalInvalid
        a = self.make("a")
        with self.assertRaises(ProposalInvalid):
            compare_proposals([a.pk, 99999])

    def test_apply_selected_stales_siblings(self):
        import uuid
        group = uuid.uuid4()
        a = self.make("A", group=group, label="A", stale_siblings=False)
        b = self.make("B", group=group, label="B", stale_siblings=False)
        apply_selected(a.pk, self.producer)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.status, ProposalStatus.APPLIED)
        self.assertEqual(b.status, ProposalStatus.STALE)


class VariantStyleTests(TestCase):
    def test_normalize_defaults(self):
        styles = variants.normalize_styles(None)
        self.assertEqual(len(styles), 3)
        self.assertIn("pacing", styles[0])

    def test_normalize_filters_unknown(self):
        styles = variants.normalize_styles([{"pacing": "飞快", "tone": "冷峻悬疑"}])
        self.assertEqual(styles[0], {"tone": "冷峻悬疑"})

    def test_normalize_caps_three(self):
        styles = variants.normalize_styles([{"tone": "冷峻悬疑"}] * 5)
        self.assertEqual(len(styles), 3)

    def test_label_and_similarity(self):
        label = variants.style_label({"pacing": "紧凑快切", "tone": "冷峻悬疑"})
        self.assertIn("节奏", label)
        self.assertEqual(variants.similarity("abc", "abc"), 1.0)
        self.assertLess(variants.similarity("abc", "完全不同的内容"), 0.5)

    def test_generate_variants_drops_duplicates(self):
        session = None  # generate_variant 由 patch 替代，不需要真实 session
        same = "完全相同的剧本内容"
        with patch.object(variants, "generate_variant", return_value=same):
            kept = variants.generate_variants(session, "原剧本", [{"tone": "冷峻悬疑"}])
        # 首条保留；重复方案重试仍相同 → 丢弃
        self.assertEqual(len(kept), 1)
        with patch.object(variants, "generate_variant", return_value=same):
            kept = variants.generate_variants(session, "原剧本",
                                              [{"tone": "冷峻悬疑"}, {"tone": "情感细腻"}])
        self.assertEqual(len(kept), 1)

    def test_generate_variants_keeps_distinct(self):
        texts = iter(["方案一：紧凑快切的悬疑故事" * 3, "另一版：舒缓细腻的情感铺陈" * 3])
        with patch.object(variants, "generate_variant", side_effect=lambda *a, **k: next(texts)):
            kept = variants.generate_variants(None, "原", [{"tone": "冷峻悬疑"}, {"tone": "情感细腻"}])
        self.assertEqual(len(kept), 2)


class ProposeMultipleTests(AssistantFixtureMixin, TestCase):
    def test_disabled_raises(self):
        with override_settings(ENABLE_MULTI_PROPOSAL=False):
            with self.assertRaises(ValueError):
                variants.propose_multiple(self.session, None, 3)

    @override_settings(ENABLE_MULTI_PROPOSAL=True)
    def test_creates_grouped_proposals(self):
        texts = iter(["紧凑版本内容A" * 5, "细腻版本内容B" * 5, "爽感版本内容C" * 5])
        with patch.object(variants, "generate_variant", side_effect=lambda *a, **k: next(texts)):
            result = variants.propose_multiple(self.session, None, 3)
        self.assertEqual(len(result["proposals"]), 3)
        proposals = AssistantProposal.objects.filter(
            alternative_group=result["alternative_group"])
        self.assertEqual(proposals.count(), 3)
        self.assertTrue(all(p.status == ProposalStatus.PENDING for p in proposals))
        labels = {p.alternative_label for p in proposals}
        self.assertEqual(len(labels), 3)

    @override_settings(ENABLE_MULTI_PROPOSAL=True)
    def test_tool_reports_proposals(self):
        texts = iter(["第一版：她亮出录音，全场哗然，节奏紧凑" * 3,
                      "第二版：雨夜长镜头，情绪铺陈，收在沉默里" * 3,
                      "第三版：她转身离开，雨声盖住对白，留一个背影" * 3])
        ctx = tools.ToolContext(self.session)
        with override_settings(ENABLE_MULTI_PROPOSAL=True), patch.object(
                variants, "generate_variant", side_effect=lambda *a, **k: next(texts)):
            result, is_error = tools.run_tool(ctx, "propose_multiple",
                                              {"episode_number": 3, "summary": "多方案"})
        self.assertFalse(is_error, result)
        self.assertEqual(len(result["proposals"]), 3)
        self.assertEqual(len(ctx.proposals), 3)

    @override_settings(ENABLE_MULTI_PROPOSAL=True)
    def test_runner_prompt_hints_multi_proposal(self):
        from apps.creation.assistant.runner import system_prompt
        self.assertIn("propose_multiple", system_prompt(self.session))

    @override_settings(ENABLE_MULTI_PROPOSAL=False)
    def test_runner_prompt_hidden_when_disabled(self):
        from apps.creation.assistant.runner import system_prompt
        self.assertNotIn("propose_multiple", system_prompt(self.session))

    @override_settings(ENABLE_MULTI_PROPOSAL=True)
    def test_missing_episode_errors(self):
        result, is_error = tools.run_tool(tools.ToolContext(self.session), "propose_multiple",
                                          {"episode_number": 9, "summary": "x"})
        self.assertTrue(is_error)


class ScriptVersionTests(AssistantFixtureMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.v1 = create_version(self.play, episode=self.ep, summary="初始", user=self.producer)

    def test_snapshot_contains_text_and_scenes(self):
        self.assertEqual(self.v1.content["text"], "第3集剧本")
        self.assertEqual(self.v1.content["scenes"]["1"]["description"], "办公室对峙")
        self.assertEqual(self.v1.version, 1)

    def test_versions_increment_per_branch(self):
        v2 = create_version(self.play, episode=self.ep, summary="二版")
        self.assertEqual(v2.version, 2)
        other = create_version(self.play, episode=self.ep, branch_name="alt")
        self.assertEqual(other.version, 1)
        self.assertEqual(set(branches(self.play, self.ep)), {"main", "alt"})

    def test_create_branch_inherits_content(self):
        branch = create_branch(self.play, from_version=self.v1, branch_name="实验线", user=self.producer)
        self.assertEqual(branch.content, self.v1.content)
        self.assertEqual(branch.parent_id, self.v1.pk)
        self.assertEqual(branch.branch_name, "实验线")

    def test_same_branch_name_rejected(self):
        with self.assertRaises(VersionError):
            create_branch(self.play, from_version=self.v1, branch_name="main")

    def test_diff_text_and_scene(self):
        v2 = create_version(self.play, episode=self.ep, content={
            "text": "第3集剧本\n新增一行", "scenes": {"1": {"description": "走廊对峙", "mood": "紧张"}}})
        diff = script_diff(self.v1, v2)
        self.assertFalse(diff["identical"])
        self.assertTrue(any(c["kind"] == "add" and "新增一行" in c["line"] for c in diff["text_changes"]))
        self.assertEqual(diff["scene_changes"]["1"]["description"],
                         {"from": "办公室对峙", "to": "走廊对峙"})

    def test_identical_diff(self):
        v2 = create_version(self.play, episode=self.ep, content=self.v1.content)
        self.assertTrue(script_diff(self.v1, v2)["identical"])

    def test_merge_clean_when_only_one_side_changes(self):
        base = self.v1
        ours = create_version(self.play, episode=self.ep, content={
            "text": "我们的改动", "scenes": base.content["scenes"]})
        theirs = create_version(self.play, episode=self.ep, content=base.content)
        result = merge_versions(base, ours, theirs)
        self.assertTrue(result["clean"])
        self.assertEqual(result["content"]["text"], "我们的改动")

    def test_merge_conflict_and_resolution(self):
        base = self.v1
        ours = create_version(self.play, episode=self.ep, content={"text": "我们的版本", "scenes": {}})
        theirs = create_version(self.play, episode=self.ep, content={"text": "他们的版本", "scenes": {}})
        result = merge_versions(base, ours, theirs)
        self.assertFalse(result["clean"])
        self.assertEqual(result["conflicts"][0]["path"], "text")
        resolved = merge_versions(base, ours, theirs, resolutions={"text": "theirs"})
        self.assertTrue(resolved["clean"])
        self.assertEqual(resolved["content"]["text"], "他们的版本")
        self.assertEqual(resolved["resolved"][0]["chosen"], "theirs")

    def test_merge_same_change_not_conflict(self):
        base = self.v1
        same = {"text": "双方都改成这样", "scenes": base.content["scenes"]}
        ours = create_version(self.play, episode=self.ep, content=same)
        theirs = create_version(self.play, episode=self.ep, content=same)
        self.assertTrue(merge_versions(base, ours, theirs)["clean"])

    def test_character_edit_proposal_records_version(self):
        character = Character.objects.create(play=self.play, name="林总", description="旧设定")
        proposal = create_proposal(
            self.session, None, ProposalKind.CHARACTER_EDIT,
            payload={"description": "新设定"}, summary="改设定", character=character,
            base_snapshot={"description": "旧设定"})
        from apps.creation.assistant.proposals import apply_proposal
        apply_proposal(proposal.pk, self.producer)
        version = CharacterVersion.objects.get(character=character)
        self.assertEqual(version.snapshot["description"], "新设定")
        self.assertEqual(version.changed_fields, ["description"])
        character.refresh_from_db()
        self.assertEqual(character.description, "新设定")

    def test_character_version_records_impacted_assets(self):
        character = Character.objects.create(play=self.play, name="林总")
        self.shot.dialogue_character = character
        self.shot.save(update_fields=["dialogue_character"])
        ref = self.seed(asset_type=AssetType.CHARACTER_REF, character=character)
        image = self.seed(asset_type=AssetType.IMAGE, shot=self.shot)
        version = record_character_version(character, changed_fields=["description"])
        self.assertEqual(version.version, 1)
        self.assertEqual(set(version.impacted_asset_ids), {ref.pk, image.pk})
        again = record_character_version(character)
        self.assertEqual(again.version, 2)
        diff = character_version_diff(version, again)
        self.assertEqual(diff, {})


class ScriptVersionApiTests(AssistantFixtureMixin, APITestCase):
    def setUp(self):
        super().setUp()
        self.v1 = create_version(self.play, episode=self.ep, summary="初始", user=self.producer)
        self.url = f"/api/scripts/{self.play.pk}/versions/"

    def test_list_branches(self):
        self.client.force_authenticate(self.producer)
        resp = self.client.get(f"{self.url}?episode=3")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.data["branches"][0]["branch_name"], "main")
        self.assertEqual(len(resp.data["versions"]), 1)

    def test_create_version(self):
        self.client.force_authenticate(self.producer)
        resp = self.client.post(self.url, {"episode": 3, "branch_name": "alt", "summary": "新分支"},
                                format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["version"], 1)

    def test_viewer_cannot_create(self):
        self.client.force_authenticate(self.viewer)
        resp = self.client.post(self.url, {"episode": 3}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_foreign_play_404(self):
        from dvadmin.system.models import CustomUser
        other = CustomUser.objects.create_user(username="x", password="Passw0rd!123")
        self.client.force_authenticate(other)
        self.assertEqual(self.client.get(self.url).status_code, 404)

    def test_branch_endpoint(self):
        self.client.force_authenticate(self.producer)
        resp = self.client.post(f"{self.url}{self.v1.pk}/branch/", {"branch_name": "实验"}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["branch_name"], "实验")

    def test_diff_endpoint(self):
        v2 = create_version(self.play, episode=self.ep, content={"text": "改后", "scenes": {}})
        self.client.force_authenticate(self.producer)
        resp = self.client.get(f"{self.url}{v2.pk}/diff/?against={self.v1.pk}")
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(resp.data["identical"])

    def test_merge_endpoint_conflict_409(self):
        ours = create_version(self.play, episode=self.ep, content={"text": "ours", "scenes": {}})
        theirs = create_version(self.play, episode=self.ep, content={"text": "theirs", "scenes": {}})
        self.client.force_authenticate(self.producer)
        resp = self.client.post(f"{self.url}merge/", {
            "base": self.v1.pk, "ours": ours.pk, "theirs": theirs.pk, "branch_name": "merged"},
            format="json")
        self.assertEqual(resp.status_code, 409)
        resp = self.client.post(f"{self.url}merge/", {
            "base": self.v1.pk, "ours": ours.pk, "theirs": theirs.pk, "branch_name": "merged",
            "resolutions": {"text": "ours"}}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["content"]["text"], "ours")

    def test_character_versions_api(self):
        character = Character.objects.create(play=self.play, name="林总")
        self.client.force_authenticate(self.producer)
        url = f"/api/characters/{character.pk}/versions/"
        resp = self.client.post(url, {}, format="json")
        self.assertEqual(resp.status_code, 201, resp.data)
        self.assertEqual(resp.data["version"], 1)
        listing = self.client.get(url)
        self.assertEqual(len(listing.data["results"]), 1)


# ── Phase 2 code-review 修复回归 ─────────────────────────────────────────────

class _MakeProposalMixin:
    """与 ProposalTreeTests.make 同构的提案构造器（回归用例独立成类时不继承父测试）。"""

    def make(self, text="初稿", group=None, label="", parent=None, kind=ProposalKind.EPISODE_SCRIPT,
             stale_siblings=True, **kw):
        return create_proposal(
            self.session, None, kind, payload={"text": text}, summary=label or text,
            episode=self.ep if kind == ProposalKind.EPISODE_SCRIPT else None,
            base_version=self.script.version,
            alternative_group=group, alternative_label=label, parent=parent,
            stale_siblings=stale_siblings, **kw)


class ComparePreviewFixTests(_MakeProposalMixin, AssistantFixtureMixin, TestCase):
    """修复：compare_proposals 把对象传给只收 id 的 proposal_preview。"""

    def test_compare_rows_have_diff_and_cost(self):
        import uuid
        group = uuid.uuid4()
        a = self.make("甲方案内容", group=group, label="A", stale_siblings=False)
        b = self.make("乙方案内容", group=group, label="B", stale_siblings=False)
        data = compare_proposals([a.pk, b.pk])
        for row in data["proposals"]:
            # 修复前 proposal_preview 收到对象必抛 TypeError 被静默吞掉，两字段恒 None
            self.assertIsNotNone(row["diff"], row)
            self.assertIsNotNone(row["cost_estimate"], row)


class MergeSceneDeletionTests(AssistantFixtureMixin, TestCase):
    """修复：三方合并把场景删除静默降级成空字段场景。"""

    def setUp(self):
        super().setUp()
        self.base = create_version(self.play, episode=self.ep, content={
            "text": "第3集剧本", "scenes": {"1": {"description": "办公室对峙"},
                                            "2": {"description": "走廊追逐"}}})

    def test_deleted_scene_wins_when_other_side_unchanged(self):
        ours = create_version(self.play, episode=self.ep, content={
            "text": "第3集剧本", "scenes": {"1": {"description": "办公室对峙"}}})  # ours 删了场景 2
        theirs = create_version(self.play, episode=self.ep, content=self.base.content)
        result = merge_versions(self.base, ours, theirs)
        self.assertTrue(result["clean"])
        self.assertNotIn("2", result["content"]["scenes"], result["content"]["scenes"])
        self.assertEqual(result["content"]["scenes"]["1"]["description"], "办公室对峙")

    def test_delete_vs_edit_conflicts(self):
        ours = create_version(self.play, episode=self.ep, content={
            "text": "第3集剧本", "scenes": {"1": {"description": "办公室对峙"}}})  # 删场景 2
        theirs = create_version(self.play, episode=self.ep, content={
            "text": "第3集剧本", "scenes": {"1": {"description": "办公室对峙"},
                                          "2": {"description": "走廊追逐（改）"}}})
        result = merge_versions(self.base, ours, theirs)
        self.assertFalse(result["clean"])
        self.assertEqual(result["conflicts"][0]["path"], "scenes.2")
        # 显式按删除方解决
        resolved = merge_versions(self.base, ours, theirs, resolutions={"scenes.2": "ours"})
        self.assertTrue(resolved["clean"])
        self.assertNotIn("2", resolved["content"]["scenes"])

    def test_both_sides_delete_is_clean(self):
        same = {"text": "第3集剧本", "scenes": {"1": {"description": "办公室对峙"}}}
        ours = create_version(self.play, episode=self.ep, content=same)
        theirs = create_version(self.play, episode=self.ep, content=same)
        result = merge_versions(self.base, ours, theirs)
        self.assertTrue(result["clean"])
        self.assertNotIn("2", result["content"]["scenes"])


class ApplySelectedKeepAlternativesTests(_MakeProposalMixin, AssistantFixtureMixin, TestCase):
    """修复：apply_selected 的 discard_others=False 曾是死参数。"""

    def test_discard_others_false_keeps_group_siblings(self):
        import uuid
        group = uuid.uuid4()
        a = self.make("A", group=group, label="A", stale_siblings=False)
        b = self.make("B", group=group, label="B", stale_siblings=False)
        apply_selected(a.pk, self.producer, discard_others=False)
        a.refresh_from_db()
        b.refresh_from_db()
        self.assertEqual(a.status, ProposalStatus.APPLIED)
        # 同组备选保持 PENDING（继续待选），组外同目标的旧提案仍失效
        self.assertEqual(b.status, ProposalStatus.PENDING, b.status)


class DeletedSceneProposalTests(AssistantFixtureMixin, TestCase):
    """修复：编辑目标场景被删除后 stale_reason 漏判，应用时才崩在 _apply_changes。"""

    def test_deleted_target_scene_marks_stale_and_raises(self):
        from apps.creation.assistant.proposals import ProposalError, apply_proposal
        scene2 = Scene.objects.create(episode=self.ep, scene_number=2, description="走廊")
        proposal = self.propose("propose_episode_script_edit", {
            "episode_number": 3, "summary": "改场景2",
            "scenes": [{"scene_number": 2, "description": "走廊（改）"}]})
        scene2.delete()
        with self.assertRaises(ProposalError) as ctx:
            apply_proposal(proposal.pk, self.producer)
        self.assertIn("已被删除", str(ctx.exception))
        proposal.refresh_from_db()
        self.assertEqual(proposal.status, ProposalStatus.STALE)

    def test_apply_guard_catches_stale_check_race(self):
        # 过期校验与应用之间的竞态：stale_reason 放行时由 _apply_changes 兜底抛 ProposalError
        from unittest.mock import patch as mock_patch
        from apps.creation.assistant.proposals import ProposalError, apply_proposal
        scene2 = Scene.objects.create(episode=self.ep, scene_number=2, description="走廊")
        proposal = self.propose("propose_episode_script_edit", {
            "episode_number": 3, "summary": "改场景2",
            "scenes": [{"scene_number": 2, "description": "走廊（改）"}]})
        scene2.delete()
        with mock_patch("apps.creation.assistant.proposals.stale_reason", return_value=None):
            with self.assertRaises(ProposalError) as ctx:
                apply_proposal(proposal.pk, self.producer)
        self.assertIn("已被删除", str(ctx.exception))
