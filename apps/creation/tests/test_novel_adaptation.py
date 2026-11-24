"""小说→剧本改编（novel-adaptation，2026-11-23 组 2）。

覆盖：分块与断点续跑（块级幂等）、设定库注入、已有剧集保护（confirm_overwrite
两档）、端点（派发/409 重复/404 无小说/权限/冻结剧 4003）、提案应用落 Episode/
Scene/Shot、改编引用计数标记。
"""
from unittest.mock import patch

from django.test import TestCase, override_settings
from rest_framework.test import APIClient

from apps.creation.tests.test_assistant import AssistantFixtureMixin

from ..assistant.models import ProposalKind, ProposalStatus
from ..assistant.proposals import ProposalError, apply_proposal, create_proposal
from ..models import (
    Episode, Novel, NovelChapter, NovelVolume, Scene, Shot,
    StoryAsset, StoryAssetType, TaskRecord, TaskStatus, TaskType,
)
from ..pipeline import novel_adaptation as NA
from ..tasks import adapt_novel


class AdaptFixture(AssistantFixtureMixin):
    def setUp(self):
        super().setUp()
        self.novel = Novel.objects.create(play=self.play, title="斗破苍穹")
        self.volume = NovelVolume.objects.create(novel=self.novel, title="第一卷", order=1)
        self.chapters = []
        for i in range(1, 5):
            self.chapters.append(NovelChapter.objects.create(
                volume=self.volume, chapter_number=i, title=f"第{i}章",
                content=f"第 {i} 章正文。林霄与萧炎互动。戒指发光。" + "剧情推进。" * 300))
        # 人物设定入库（供注入断言）
        StoryAsset.objects.create(
            play=self.play, type=StoryAssetType.PERSON, name="林霄",
            fields={"identity": "被退婚的林家少爷", "personality": "坚韧"})

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def _fake_llm(self, episodes_by_block):
        """mock 网关：按调用序返回块剧集（episodes_by_block 每项为剧集列表）。"""
        calls = {"n": 0}

        class FakeGW:
            def call(self, provider_type, config, **kw):
                i = calls["n"]
                calls["n"] += 1
                eps = episodes_by_block[min(i, len(episodes_by_block) - 1)]
                import json
                text = json.dumps({"summary": f"块{i + 1}摘要", "episodes": eps},
                                  ensure_ascii=False)
                return {"text": text, "usage": {}, "model": "fake", "cost": 0}
        return FakeGW(), calls


_EPISODES = [{
    "episode_number": 1, "title": "退婚之辱",
    "scenes": [{
        "scene_number": 1, "location": "林家大厅", "time_of_day": "DAY",
        "description": "当众退婚", "mood": "羞辱",
        "shots": [
            {"shot_number": 1, "description": "全景林家大厅",
             "dialogue": "林霄，你被退婚了", "action": "众人围观"},
            {"shot_number": 2, "description": "特写林霄攥拳",
             "dialogue": "三年后我会拿回来", "action": ""},
        ]}],
}]


class OrchestrationTests(AdaptFixture, TestCase):
    def test_chunk_and_context(self):
        """分块按字符量切；设定库注入命出场人物。"""
        chapters = NA.collect_chapters(self.play)
        blocks = NA.chunk_chapters(chapters)
        self.assertGreaterEqual(len(blocks), 1)
        self.assertEqual(blocks[0]["start"], 1)
        self.assertEqual(blocks[-1]["end"], 4)
        context = NA.story_context(self.play, chapters)
        self.assertIn("林霄", context)
        self.assertIn("被退婚的林家少爷", context)

    def test_full_run_produces_proposal(self):
        """整链：任务跑完产出 EPISODE_SCRIPT 提案（adapt payload），章节引用计数标记。"""
        fake, calls = self._fake_llm([_EPISODES])
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.NOVEL_ADAPT, status=TaskStatus.PENDING,
            input_summary={"adapt": {"from_chapter": 1, "to_chapter": 4, "episode_start": 1}})
        with patch("apps.creation.tasks._gateway", return_value=fake), \
                patch.object(NA, "emit_progress") as emit:
            adapt_novel(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        self.assertIn("proposal_id", record.output_summary)
        from ..assistant.models import AssistantProposal
        proposal = AssistantProposal.objects.get(pk=record.output_summary["proposal_id"])
        self.assertEqual(proposal.kind, ProposalKind.EPISODE_SCRIPT)
        self.assertEqual(proposal.status, ProposalStatus.PENDING)
        self.assertIn("adapt", proposal.payload)
        # 章节改编引用计数
        self.chapters[0].refresh_from_db()
        self.assertEqual(self.chapters[0].adaptation_refs, 1)

    def test_block_resume(self):
        """断点续跑：前块产出在 output_summary，恢复只调剩余块的 LLM。"""
        fake, calls = self._fake_llm([_EPISODES])
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.NOVEL_ADAPT, status=TaskStatus.RETRYING,
            input_summary={"adapt": {"from_chapter": 1, "to_chapter": 4, "episode_start": 1}},
            output_summary={"blocks": {"1-4": {"episodes": _EPISODES, "summary": "已跑"}},
                            "tail_summary": "已跑"})
        with patch("apps.creation.tasks._gateway", return_value=fake):
            adapt_novel(record.pk)
        record.refresh_from_db()
        self.assertEqual(record.status, TaskStatus.SUCCESS)
        self.assertEqual(calls["n"], 0)  # 唯一块已完成：零 LLM 调用

    def test_empty_episode_block_raises(self):
        """块返回空剧集：内容为空是失败生成，不产空提案、任务非 SUCCESS
        （直调重试判定受 Celery current_task 上下文影响，FAILED/RUNNING 均可接受——
        关键是不产提案且重试路径最终会因 ValueError 恒定失败而耗尽）。"""
        fake, _ = self._fake_llm([[]])
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.NOVEL_ADAPT, status=TaskStatus.PENDING,
            input_summary={"adapt": {"from_chapter": 1, "to_chapter": 1, "episode_start": 1}})
        with patch("apps.creation.tasks._gateway", return_value=fake):
            try:
                adapt_novel(record.pk)
            except Exception:
                pass  # raise 或被 Celery 重试吞掉，两种路径都不产提案
        record.refresh_from_db()
        self.assertNotEqual(record.status, TaskStatus.SUCCESS)
        self.assertFalse("proposal_id" in (record.output_summary or {}))


class ProposalApplyTests(AdaptFixture, TestCase):
    def _proposal(self, episodes=None, episode_start=1, confirm=False):
        eps = episodes if episodes is not None else _EPISODES
        payload = {"adapt": {"episodes": eps, "episode_start": episode_start,
                             "from_chapter": 1, "to_chapter": 2}}
        if confirm:
            payload["adapt"]["confirm_overwrite"] = True
        return create_proposal(
            self.session, None, ProposalKind.EPISODE_SCRIPT, payload=payload,
            summary="改编测试", episode=None, base_version=1)

    def test_apply_creates_structure(self):
        """应用：Episode/Scene/Shot（含对白）落库，镜头序连续。"""
        p = self._proposal()
        apply_proposal(p.pk, self.producer)
        episode = Episode.objects.get(play=self.play, episode_number=1)
        self.assertEqual(episode.title, "退婚之辱")
        scene = episode.scenes.get(scene_number=1)
        self.assertEqual(scene.location, "林家大厅")
        shots = list(scene.shots.order_by("shot_number"))
        self.assertEqual(len(shots), 2)
        self.assertEqual(shots[0].dialogue, "林霄，你被退婚了")

    def test_existing_content_guard(self):
        """已有剧集保护：范围内已有场景且未确认 → 拒绝；确认后覆盖。"""
        ep = Episode.objects.create(play=self.play, episode_number=1, title="旧集")
        Scene.objects.create(episode=ep, scene_number=1, description="旧内容")
        p = self._proposal()
        with self.assertRaises(ProposalError):
            apply_proposal(p.pk, self.producer)
        # 确认覆盖路径
        p2 = self._proposal(confirm=True)
        apply_proposal(p2.pk, self.producer)
        self.assertEqual(Scene.objects.filter(
            episode__play=self.play, episode__episode_number=1).count(), 1)
        self.assertEqual(Shot.objects.filter(
            scene__episode__play=self.play, scene__episode__episode_number=1).count(), 2)

    def test_episode_start_offset(self):
        """episode_start：剧集号偏移落位（不改写低号已有集）。"""
        Episode.objects.create(play=self.play, episode_number=1, title="保留集")
        p = self._proposal(episode_start=2, confirm=True)
        apply_proposal(p.pk, self.producer)
        self.assertTrue(Episode.objects.filter(play=self.play, episode_number=2).exists())
        old = Episode.objects.get(play=self.play, episode_number=1)
        self.assertEqual(old.title, "保留集")


class EndpointTests(AdaptFixture, TestCase):
    def _post(self, user, data, path="/api/plays/%s/novel/adapt/"):
        return self.client_for(user).post(path % self.play.pk, data, format="json")

    def test_dispatch_and_conflict(self):
        """派发创建 PENDING 记录；进行中重复请求 409。"""
        resp = self._post(self.producer, {"from_chapter": 1, "to_chapter": 4})
        self.assertEqual(resp.status_code, 200, resp.content)
        body = resp.json()
        data = body.get("data", body)
        self.assertIn("task_record_id", data)
        self.assertEqual(data["block_count"], 1)
        conflict = self._post(self.producer, {"from_chapter": 1, "to_chapter": 4})
        self.assertEqual(conflict.status_code, 409)

    def test_no_novel_400(self):
        Novel.objects.filter(play=self.play).delete()
        resp = self._post(self.producer, {})
        self.assertEqual(resp.status_code, 400)

    def test_viewer_forbidden(self):
        resp = self._post(self.viewer, {"from_chapter": 1})
        self.assertEqual(resp.status_code, 403)

    def test_frozen_play_4003(self):
        self.play.frozen_reason = "违规内容"
        self.play.save(update_fields=["frozen_reason"])
        resp = self._post(self.producer, {"from_chapter": 1})
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body.get("reason") or (body.get("data") or {}).get("reason"),
                         "play_frozen")

    def test_resume_endpoint(self):
        """断点续跑端点：FAILED 记录置 PENDING 重派。"""
        record = TaskRecord.objects.create(
            play=self.play, task_type=TaskType.NOVEL_ADAPT, status=TaskStatus.FAILED,
            input_summary={"adapt": {"from_chapter": 1, "to_chapter": 4, "episode_start": 1}})
        resp = self.client_for(self.producer).post(
            f"/api/plays/{self.play.pk}/novel/adapt/resume/",
            {"task_record_id": record.pk}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        record.refresh_from_db()
        self.assertEqual(record.status, TaskStatus.PENDING)
        # 他人剧的记录 → 404
        other_record = TaskRecord.objects.create(
            play=self.foreign_scene.episode.play, task_type=TaskType.NOVEL_ADAPT,
            status=TaskStatus.FAILED, input_summary={})
        resp2 = self.client_for(self.producer).post(
            f"/api/plays/{self.play.pk}/novel/adapt/resume/",
            {"task_record_id": other_record.pk}, format="json")
        self.assertEqual(resp2.status_code, 404)
