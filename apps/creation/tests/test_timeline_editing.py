"""多轨剪辑（timeline-editing，2026-11-24 组 6）。

覆盖：工程/轨/片段 CRUD（六轨唯一/字幕轨手工文本/跨轨父拒绝）、自动合成导入
（六轨初始态/幂等重导）、片段分割（父引用链/序顺延）、素材版本切换（六元组
位置校验/父链保留）、导出前校验（空引用/越界/非 READY 阻断+明细）、导出派发
（走既有闸——在跑去重/冻结 4003；FFmpeg 侧 mock）、viewer 403。
"""
from unittest.mock import MagicMock, patch

from django.test import TestCase
from rest_framework.test import APIClient

from apps.creation.tests.test_assistant import AssistantFixtureMixin

from ..models import (
    Asset, AssetStatus, AssetType, Clip, Composition, TaskRecord, TaskStatus,
    TaskType, Track, TrackKind,
)


class TimelineFixture(AssistantFixtureMixin):
    def setUp(self):
        super().setUp()
        self.comp = Composition.objects.create(
            play=self.play, episode=self.ep, title="精修")
        self.tracks = {kind: Track.objects.create(composition=self.comp, kind=kind, order=i)
                       for i, kind in enumerate(TrackKind.values)}
        self.image_asset = Asset(
            play=self.play, episode=self.ep, scene=self.scene, shot=self.shot,
            asset_type=AssetType.IMAGE, status=AssetStatus.READY,
            artifact_url="http://cdn/img-v1.png",
            metadata={"duration_seconds": 6.0})
        self.image_asset._skip_pipeline_dispatch = True
        self.image_asset.save()
        self.audio_asset = Asset(
            play=self.play, episode=self.ep, scene=self.scene, shot=self.shot,
            asset_type=AssetType.AUDIO, status=AssetStatus.READY,
            artifact_url="http://cdn/aud-v1.mp3",
            metadata={"duration_seconds": 4.0})
        self.audio_asset._skip_pipeline_dispatch = True
        self.audio_asset.save()
        self.clip = Clip.objects.create(
            track=self.tracks[TrackKind.VIDEO], asset=self.image_asset,
            label="镜头1", start=0, in_point=0, out_point=6, order=0)

    def client_for(self, user):
        c = APIClient()
        c.force_authenticate(user)
        return c

    def unwrap(self, resp):
        body = resp.json()
        if isinstance(body, dict) and "data" in body and "code" in body:
            return body["data"] if body["data"] is not None else body
        return body


class TimelineCRUDTests(TimelineFixture, TestCase):
    def test_track_kind_unique_per_composition(self):
        """每工程每类轨至多一条。"""
        client = self.client_for(self.producer)
        bad = client.post("/api/tracks/", {
            "composition": self.comp.pk, "kind": "VIDEO"}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_clip_requires_asset_except_subtitle(self):
        """非字幕轨无素材 400；字幕轨手工文本合法。"""
        client = self.client_for(self.producer)
        bad = client.post("/api/clips/", {
            "track": self.tracks[TrackKind.VIDEO].pk, "start": 0,
            "in_point": 0, "out_point": 3}, format="json")
        self.assertEqual(bad.status_code, 400)
        ok = client.post("/api/clips/", {
            "track": self.tracks[TrackKind.SUBTITLE].pk, "start": 0,
            "in_point": 0, "out_point": 2, "text": "你以为你还能逃吗"}, format="json")
        self.assertEqual(ok.status_code, 201, ok.content)
        # 字幕轨既无素材又无文本 400
        bad2 = client.post("/api/clips/", {
            "track": self.tracks[TrackKind.SUBTITLE].pk, "start": 0,
            "in_point": 0, "out_point": 2}, format="json")
        self.assertEqual(bad2.status_code, 400)

    def test_out_point_before_in_point_rejected(self):
        client = self.client_for(self.producer)
        bad = client.patch(f"/api/clips/{self.clip.pk}/",
                           {"in_point": 5, "out_point": 2}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_cross_track_parent_rejected(self):
        other = Clip.objects.create(
            track=self.tracks[TrackKind.DIALOGUE], asset=self.audio_asset)
        client = self.client_for(self.producer)
        bad = client.post("/api/clips/", {
            "track": self.tracks[TrackKind.VIDEO].pk, "asset": self.image_asset.pk,
            "in_point": 0, "out_point": 2, "parent": other.pk}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_split_parent_chain(self):
        """分割：两段父引用链完整 + 同轨序顺延。"""
        clip2 = Clip.objects.create(
            track=self.tracks[TrackKind.VIDEO], asset=self.image_asset,
            label="镜头1b", start=6, in_point=0, out_point=6, order=1)
        client = self.client_for(self.producer)
        resp = client.post(f"/api/compositions/{self.comp.pk}/clips/split/",
                           {"clip_id": self.clip.pk, "offset": 2.5}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        self.clip.refresh_from_db()
        self.assertEqual((self.clip.in_point, self.clip.out_point), (0, 2.5))
        right = Clip.objects.filter(parent=self.clip).get()
        self.assertEqual((right.in_point, right.out_point, right.start), (2.5, 6, 2.5))
        self.assertEqual(right.parent.pk, self.clip.pk)
        # 原序 1 的片段顺延到 2
        clip2.refresh_from_db()
        self.assertEqual(clip2.order, 2)
        # 非法分割点（越界）400
        bad = client.post(f"/api/compositions/{self.comp.pk}/clips/split/",
                          {"clip_id": self.clip.pk, "offset": 99}, format="json")
        self.assertEqual(bad.status_code, 400)

    def test_switch_version_same_scope(self):
        """版本切换：同六元组位置才可换；父链保留。"""
        Clip.objects.create(
            track=self.tracks[TrackKind.VIDEO], asset=self.image_asset, parent=self.clip)
        v2 = Asset(play=self.play, episode=self.ep, scene=self.scene, shot=self.shot,
                   asset_type=AssetType.IMAGE, status=AssetStatus.READY,
                   artifact_url="http://cdn/img-v2.png", version=2,
                   metadata={"duration_seconds": 6.0})
        v2._skip_pipeline_dispatch = True
        v2.save()
        # 异位置素材拒绝（AUDIO 不在 VIDEO 位置）
        bad = self.client_for(self.producer).post(
            f"/api/clips/{self.clip.pk}/switch-version/",
            {"asset_id": self.audio_asset.pk}, format="json")
        self.assertEqual(bad.status_code, 400)
        ok = self.client_for(self.producer).post(
            f"/api/clips/{self.clip.pk}/switch-version/",
            {"asset_id": v2.pk}, format="json")
        self.assertEqual(ok.status_code, 200, ok.content)
        self.clip.refresh_from_db()
        self.assertEqual(self.clip.asset.pk, v2.pk)
        # 分割子片段父链仍指向本片段
        self.assertTrue(Clip.objects.filter(parent=self.clip).exists())

    def test_viewer_write_forbidden(self):
        client = self.client_for(self.viewer)
        self.assertEqual(client.get("/api/compositions/").status_code, 200)
        resp = client.post("/api/tracks/", {
            "composition": self.comp.pk, "kind": "OVERLAY"}, format="json")
        self.assertEqual(resp.status_code, 403)


class ImportTests(TimelineFixture, TestCase):
    def test_import_from_compose(self):
        """自动合成导入：六轨初始态 + timeline 素材上轨 + 幂等重导。"""
        source = Asset(
            play=self.play, episode=self.ep, asset_type=AssetType.VIDEO,
            status=AssetStatus.READY, artifact_url="http://cdn/ep.mp4",
            metadata={"timeline": [
                {"start": 0, "duration": 6, "shot_id": self.shot.pk,
                 "role": "body", "index": 0},
                {"start": 6, "duration": 4, "role": "intro", "index": 1},
            ]})
        source._skip_pipeline_dispatch = True
        source.save()
        client = self.client_for(self.producer)
        resp = client.post("/api/compositions/import/",
                           {"episode_id": self.ep.pk}, format="json")
        self.assertEqual(resp.status_code, 200, resp.content)
        body = self.unwrap(resp)
        # 镜头1 上视频轨/对白轨/字幕轨（fixture shot 有 dialogue）三段
        self.assertEqual(body["imported_clips"], 3)
        comp = Composition.objects.get(pk=body["id"])
        self.assertEqual(comp.import_source, source.pk)
        self.assertEqual(comp.tracks.count(), 6)
        self.assertEqual(
            Clip.objects.filter(track__composition=comp,
                                track__kind=TrackKind.VIDEO).count(), 1)
        self.assertEqual(
            Clip.objects.filter(track__composition=comp,
                                track__kind=TrackKind.SUBTITLE).count(), 1)
        # 片头轨外素材不上轨
        self.assertEqual(
            Clip.objects.filter(track__composition=comp).count(), 3)
        # 幂等重导：同一来源返回既有工程
        resp2 = client.post("/api/compositions/import/",
                            {"episode_id": self.ep.pk}, format="json")
        body2 = self.unwrap(resp2)
        self.assertEqual(body2["id"], comp.pk)

    def test_import_no_source_400(self):
        client = self.client_for(self.producer)
        resp = client.post("/api/compositions/import/",
                           {"episode_id": self.ep.pk}, format="json")
        self.assertEqual(resp.status_code, 400)


class ExportTests(TimelineFixture, TestCase):
    def test_validation_blocks(self):
        """导出前校验：空引用/越界/非 READY 三阻断 + 明细。"""
        # 空引用：SFX 轨无素材片段（绕过序列化器直接落库模拟坏数据）
        Clip.objects.create(
            track=self.tracks[TrackKind.SFX], label="坏片段",
            start=0, in_point=0, out_point=2)
        # 越界：出点超出素材时长 6s
        Clip.objects.create(
            track=self.tracks[TrackKind.VIDEO], asset=self.image_asset,
            label="越界", start=10, in_point=0, out_point=9, order=1)
        # 非 READY 素材
        failed = Asset(play=self.play, episode=self.ep, scene=self.scene, shot=self.shot,
                       asset_type=AssetType.AUDIO, status=AssetStatus.FAILED,
                       artifact_url="http://cdn/f.mp3")
        failed._skip_pipeline_dispatch = True
        failed.save()
        Clip.objects.create(
            track=self.tracks[TrackKind.DIALOGUE], asset=failed,
            label="失败素材", start=0, in_point=0, out_point=2)
        client = self.client_for(self.producer)
        resp = client.post(f"/api/compositions/{self.comp.pk}/export/",
                           {}, format="json")
        self.assertEqual(resp.status_code, 400)
        problems = resp.json().get("problems") or (resp.json().get("data") or {}).get("problems")
        self.assertEqual(len(problems), 3)
        codes = {p["code"] for p in problems}
        self.assertEqual(codes, {"empty_reference", "out_of_bounds", "asset_unavailable"})

    def test_export_dispatch(self):
        """校验通过：走既有派发闸派 TIMELINE_EXPORT；在跑去重 409。"""
        with patch("apps.creation.pipeline.dispatcher.send_task",
                   return_value=MagicMock(id="celery-x")):
            client = self.client_for(self.producer)
            resp = client.post(f"/api/compositions/{self.comp.pk}/export/",
                               {}, format="json")
            self.assertEqual(resp.status_code, 200, resp.content)
            body = self.unwrap(resp)
            record = TaskRecord.objects.get(pk=body["task_record_id"])
            self.assertEqual(record.task_type, TaskType.TIMELINE_EXPORT)
            self.assertEqual(record.input_summary["composition_id"], self.comp.pk)
            # 在跑去重
            resp2 = client.post(f"/api/compositions/{self.comp.pk}/export/",
                                {}, format="json")
            self.assertEqual(resp2.status_code, 409)

    def test_export_frozen_4003(self):
        self.play.frozen_reason = "违规"
        self.play.save(update_fields=["frozen_reason"])
        with patch("apps.creation.pipeline.dispatcher.send_task",
                   return_value=MagicMock(id="celery-x")):
            resp = self.client_for(self.producer).post(
                f"/api/compositions/{self.comp.pk}/export/", {}, format="json")
        self.assertEqual(resp.status_code, 403)
        body = resp.json()
        self.assertEqual(body.get("reason") or (body.get("data") or {}).get("reason"),
                         "play_frozen")

    def test_export_viewer_403(self):
        resp = self.client_for(self.viewer).post(
            f"/api/compositions/{self.comp.pk}/export/", {}, format="json")
        self.assertEqual(resp.status_code, 403)

    def test_worker_validates_again(self):
        """worker 侧导出前再校验一次（派发后改坏阻断）。"""
        record = TaskRecord.objects.create(
            play=self.play, episode=self.ep, task_type=TaskType.TIMELINE_EXPORT,
            status=TaskStatus.RUNNING,
            input_summary={"composition_id": self.comp.pk})
        # 派发后改坏：视频轨片段素材置 FAILED
        self.image_asset.status = AssetStatus.FAILED
        self.image_asset._skip_pipeline_dispatch = True
        self.image_asset.save(update_fields=["status"])
        from ..tasks import export_composition
        with self.assertRaises(ValueError):
            export_composition(record.pk)
