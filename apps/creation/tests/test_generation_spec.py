"""生成规格、指纹与依赖测试（工作台 Stage A2，任务 2.1–2.6）。"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from apps.ai_gateway.models import AIModel, AIProvider
from apps.creation.models import (
    AgentConfig, Asset, AssetDependency, AssetStatus, AssetType, AuditMode, Character,
    Episode, Play, RelationType, Scene, Shot, TaskRecord, TaskStatus, TaskType,
)
from apps.creation.pipeline.generation import (
    GENERATOR_VERSION, SPEC_SCHEMA_VERSION, fingerprint_of, normalize_spec, upstream_refs,
)
from apps.creation.tasks import generate_image, generate_tts
from apps.core.system.models import CustomUser, UserRole


class MigrationTests(TestCase):
    """任务 2.1：迁移可前滚/回滚、既有行 UUID 唯一回填。"""

    def test_uuid_backfilled_unique(self):
        """存量 Play/Asset 行有唯一 UUID（default=uuid4 回填），新行自动生成。"""
        p1 = Play.objects.create(title="剧一")
        p2 = Play.objects.create(title="剧二")
        self.assertNotEqual(p1.uuid, p2.uuid)
        a = Asset.objects.create(play=p2, asset_type=AssetType.IMAGE,
                                 status=AssetStatus.READY)
        self.assertTrue(a.uuid)
        # UUID 全局唯一（unique 约束 + default）
        uuids = list(Play.objects.values_list("uuid", flat=True))
        self.assertEqual(len(uuids), len(set(uuids)))

    def test_new_fields_nullable_default(self):
        """新字段均 nullable/空默认——历史数据保持可读（spec: asset-lifecycle）。"""
        p = Play.objects.create(title="旧数据剧")
        a = Asset.objects.create(play=p, asset_type=AssetType.IMAGE,
                                 status=AssetStatus.READY, artifact_url="/media/x.png")
        self.assertEqual(a.generation_spec, None)
        self.assertEqual(a.input_fingerprint, "")
        self.assertEqual(a.storage_key, "")
        self.assertEqual(a.content_sha256, "")


class FingerprintSpecTests(SimpleTestCase):
    """任务 2.2：规范化规格与 SHA-256 指纹。"""

    def _spec(self, **overrides):
        base = dict(
            target_type="IMAGE",
            direct_content={"prompt": "近景"},
            model=None, template=None, effective_params={"aspect_ratio": "16:9"},
            upstream=[],
        )
        base.update(overrides)
        return normalize_spec(**base)

    def test_key_order_and_whitespace_stable(self):
        """JSON 键顺序变化但语义相同 → 指纹一致。"""
        s1 = self._spec(direct_content={"prompt": "近景", "neg": ""})
        s2 = self._spec(direct_content={"neg": "", "prompt": "近景"})
        self.assertEqual(fingerprint_of(s1), fingerprint_of(s2))

    def test_model_change_changes_fingerprint(self):
        class _M:
            pk = 7
            model_name = "img-v2"
        s1 = self._spec()
        s2 = self._spec(model=_M())
        self.assertNotEqual(fingerprint_of(s1), fingerprint_of(s2))

    def test_params_change_changes_fingerprint(self):
        s1 = self._spec(effective_params={"aspect_ratio": "9:16"})
        s2 = self._spec(effective_params={"aspect_ratio": "16:9"})
        self.assertNotEqual(fingerprint_of(s1), fingerprint_of(s2))

    def test_upstream_change_changes_fingerprint(self):
        s1 = self._spec(upstream=[{"uuid": "u1", "fingerprint": "f1", "content_sha256": ""}])
        s2 = self._spec(upstream=[{"uuid": "u1", "fingerprint": "f2", "content_sha256": ""}])
        self.assertNotEqual(fingerprint_of(s1), fingerprint_of(s2))

    def test_generator_version_in_spec(self):
        """规格含 schema/generator 版本——生成逻辑升级使旧指纹失效。"""
        s = self._spec()
        self.assertEqual(s["schema_version"], SPEC_SCHEMA_VERSION)
        self.assertEqual(s["generator_version"], GENERATOR_VERSION)

    def test_seed_only_when_explicit(self):
        """未指定种子：不进规格（两次生成同指纹）；显式种子改变指纹。"""
        s1 = self._spec()
        s2 = self._spec()
        self.assertEqual(fingerprint_of(s1), fingerprint_of(s2))
        self.assertNotIn("seed", s1)
        s3 = self._spec(seed=42)
        self.assertIn("seed", s3)
        self.assertNotEqual(fingerprint_of(s1), fingerprint_of(s3))

    def test_output_fields_not_in_spec(self):
        """用量/成本/地址不进规格（产出侧信息不参与输入指纹）。"""
        s = self._spec()
        for key in ("usage", "cost", "artifact_url", "url"):
            self.assertNotIn(key, s)


class DispatchSnapshotTests(TestCase):
    """任务 2.3：派发时保存不可变生成快照，任务记录不含凭证。"""

    def setUp(self):
        self.owner = CustomUser.objects.create_user(
            username="a2d", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.provider = AIProvider.objects.create(
            name="P", provider_type="LLM", adapter_type="deepseek",
            base_url="https://x", api_key_encrypted="SECRET", priority=1)
        self.model = AIModel.objects.create(
            provider=self.provider, model_name="m", display_name="M", model_type="LLM")
        self.config = AgentConfig.objects.create(
            name="配方", llm_model=self.model, created_by=self.owner)
        self.play = Play.objects.create(
            title="快照剧", agent_config=self.config, created_by=self.owner,
            audit_mode=AuditMode.NONE, gen_params={"aspect_ratio": "16:9"})
        self.ep = Episode.objects.create(play=self.play, episode_number=1)
        self.scene = Scene.objects.create(episode=self.ep, scene_number=1)
        self.shot = Shot.objects.create(scene=self.scene, shot_number=1,
                                        visual_prompt="近景", dialogue="台词")

    def test_generation_snapshot_saved_on_dispatch(self):
        from apps.creation.pipeline.dispatcher import dispatch_task
        with patch("apps.creation.pipeline.dispatcher.send_task", return_value=MagicMock(id="x")):
            rec = dispatch_task(TaskType.IMAGE_GEN, play=self.play, episode=self.ep,
                                scene=self.scene, shot=self.shot)
        snap = rec.input_summary["generation"]
        self.assertEqual(snap["effective_params"], {"aspect_ratio": "16:9"})
        self.assertIn("llm_model", snap)
        # 任务记录不含凭证
        self.assertNotIn("SECRET", json.dumps(rec.input_summary))
        self.assertNotIn("api_key", json.dumps(rec.input_summary))

    def test_snapshot_immutable_after_play_change(self):
        """排队后修改 Play.gen_params 不改变已派发请求的快照。"""
        from apps.creation.pipeline.dispatcher import dispatch_task
        with patch("apps.creation.pipeline.dispatcher.send_task", return_value=MagicMock(id="x")):
            rec = dispatch_task(TaskType.IMAGE_GEN, play=self.play, episode=self.ep,
                                scene=self.scene, shot=self.shot)
        before = dict(rec.input_summary["generation"])
        self.play.gen_params = {"aspect_ratio": "9:16"}
        self.play.save(update_fields=["gen_params"])
        rec.refresh_from_db()
        self.assertEqual(rec.input_summary["generation"], before)

    def test_non_generation_task_no_snapshot(self):
        """非生成类任务（如 ASSISTANT_TURN）不写生成快照。"""
        from apps.creation.pipeline.dispatcher import dispatch_task
        with patch("apps.creation.pipeline.dispatcher.send_task", return_value=MagicMock(id="x")):
            rec = dispatch_task(TaskType.ASSISTANT_TURN, play=self.play)
        self.assertNotIn("generation", rec.input_summary)


class DependencyEdgeTests(TestCase):
    """任务 2.4：IMAGE 记录 STORYBOARD/CHARACTER_REF 依赖、AUDIO 记录文本/分镜依赖。"""

    def setUp(self):
        self.owner = CustomUser.objects.create_user(
            username="a2e", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.provider = AIProvider.objects.create(
            name="P2", provider_type="LLM", adapter_type="deepseek",
            base_url="https://x", api_key_encrypted="", priority=1)
        self.model = AIModel.objects.create(
            provider=self.provider, model_name="m", display_name="M", model_type="LLM")
        self.config = AgentConfig.objects.create(
            name="配方", llm_model=self.model, created_by=self.owner)
        self.play = Play.objects.create(
            title="依赖剧", agent_config=self.config, created_by=self.owner,
            audit_mode=AuditMode.NONE)
        self.ep = Episode.objects.create(play=self.play, episode_number=1)
        self.scene = Scene.objects.create(episode=self.ep, scene_number=1)
        self.character = Character.objects.create(
            play=self.play, name="主", role="PROTAGONIST")
        self.shot = Shot.objects.create(
            scene=self.scene, shot_number=1, visual_prompt="近景", dialogue="台词")
        self.shot.dialogue_character = self.character
        self.shot.save(update_fields=["dialogue_character"])
        # 上游：分镜条目 + 剧本 + 定妆照
        self.storyboard = Asset.objects.create(
            play=self.play, episode=self.ep, scene=self.scene, shot=self.shot,
            asset_type=AssetType.STORYBOARD, status=AssetStatus.READY)
        self.script = Asset.objects.create(
            play=self.play, episode=self.ep, asset_type=AssetType.EPISODE_SCRIPT,
            status=AssetStatus.READY)
        self.char_ref = Asset.objects.create(
            play=self.play, character=self.character, asset_type=AssetType.CHARACTER_REF,
            status=AssetStatus.READY, artifact_url="/media/x.png")
        self.character.reference_image = self.char_ref
        self.character.save(update_fields=["reference_image"])

    class FakeGateway:
        def __init__(self):
            self.calls = []

        def call(self, provider_type, config, **payload):
            self.calls.append((provider_type, payload))
            return {"url": "https://x/img.png", "audio_bytes": b"AUD", "image_bytes": b"IMG",
                    "usage": {}, "cost": 0, "duration_seconds": 1.5}

    def test_image_dependency_edges(self):
        """IMAGE 依赖 STORYBOARD + CHARACTER_REF（可遍历依赖边）。"""
        gw = self.FakeGateway()
        r = TaskRecord.objects.create(play=self.play, episode=self.ep, scene=self.scene,
                                      shot=self.shot, task_type=TaskType.IMAGE_GEN)
        with patch("apps.creation.tasks._gateway", return_value=gw), \
             patch("apps.creation.tasks._save_image_result", return_value="/media/i.png"), \
             patch("apps.creation.tasks._store_generated_bytes",
                   return_value=("", "/media/i.png", "")):
            generate_image(r.pk)
        image = Asset.objects.get(shot=self.shot, asset_type=AssetType.IMAGE)
        ups = set(image.depends_on.all())
        self.assertIn(self.storyboard, ups)
        self.assertIn(self.char_ref, ups)
        self.assertNotIn(self.script, ups)

    def test_audio_dependency_edges(self):
        """AUDIO 依赖 STORYBOARD + EPISODE_SCRIPT（文本/分镜上游）。"""
        gw = self.FakeGateway()
        r = TaskRecord.objects.create(play=self.play, episode=self.ep, scene=self.scene,
                                      shot=self.shot, task_type=TaskType.TTS_GEN)
        with patch("apps.creation.tasks._gateway", return_value=gw), \
             patch("apps.creation.tasks._validate_media_bytes",
                   return_value={"duration": 1.5, "has_audio": True}), \
             patch("apps.creation.tasks._store_generated_bytes",
                   return_value=("k", "/media/a.mp3", "d" * 64)):
            generate_tts(r.pk)
        audio = Asset.objects.get(shot=self.shot, asset_type=AssetType.AUDIO)
        ups = set(audio.depends_on.all())
        self.assertIn(self.storyboard, ups)
        self.assertIn(self.script, ups)
        # 定妆照不是音频上游
        self.assertNotIn(self.char_ref, ups)


class ReuseAndRedoTests(TestCase):
    """任务 2.5：相同输入指纹复用；显式 force/redo 跳过复用并保留旧版本。"""

    def setUp(self):
        self.owner = CustomUser.objects.create_user(
            username="a2r", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.provider = AIProvider.objects.create(
            name="P3", provider_type="LLM", adapter_type="deepseek",
            base_url="https://x", api_key_encrypted="", priority=1)
        self.model = AIModel.objects.create(
            provider=self.provider, model_name="m", display_name="M", model_type="LLM")
        self.config = AgentConfig.objects.create(
            name="配方", llm_model=self.model, created_by=self.owner)
        self.play = Play.objects.create(
            title="复用剧", agent_config=self.config, created_by=self.owner,
            audit_mode=AuditMode.NONE)
        self.ep = Episode.objects.create(play=self.play, episode_number=1)
        self.scene = Scene.objects.create(episode=self.ep, scene_number=1)
        self.shot = Shot.objects.create(
            scene=self.scene, shot_number=1, visual_prompt="近景", dialogue="台词")

    class FakeGateway:
        def __init__(self):
            self.calls = []

        def call(self, provider_type, config, **payload):
            self.calls.append((provider_type, payload))
            return {"url": "https://x/img.png", "image_bytes": b"IMG", "usage": {}, "cost": 0}

    def _gen(self, input_summary=None):
        gw = self.FakeGateway()
        r = TaskRecord.objects.create(
            play=self.play, episode=self.ep, scene=self.scene, shot=self.shot,
            task_type=TaskType.IMAGE_GEN, input_summary=input_summary or {})
        with patch("apps.creation.tasks._gateway", return_value=gw), \
             patch("apps.creation.tasks._save_image_result", return_value="/media/i.png"), \
             patch("apps.creation.tasks._store_generated_bytes",
                   return_value=("", "/media/i.png", "")):
            generate_image(r.pk)
        r.refresh_from_db()
        return gw, r

    def test_same_input_reuses_without_model_call(self):
        """相同输入不重复调用网关：第一次生成、第二次复用。"""
        gw1, r1 = self._gen()
        self.assertEqual(len(gw1.calls), 1)
        gw2, r2 = self._gen()
        self.assertEqual(len(gw2.calls), 0)  # 未调用模型
        self.assertTrue(r2.output_summary.get("reused"))
        self.assertEqual(r2.output_summary["asset_id"], r1.output_summary["asset_id"])
        # 只有一条 IMAGE 资产（复用不落新版本）
        self.assertEqual(Asset.objects.filter(shot=self.shot, asset_type=AssetType.IMAGE).count(), 1)

    def test_force_redo_creates_new_version_keeps_old(self):
        """显式 redo：跳过复用，生成新版本且旧版本保留。"""
        gw1, r1 = self._gen()
        old_id = r1.output_summary["asset_id"]
        gw2, r2 = self._gen(input_summary={"redo": True})
        self.assertEqual(len(gw2.calls), 1)  # 重新调用模型
        self.assertFalse(r2.output_summary.get("reused"))
        new_id = r2.output_summary["asset_id"]
        self.assertNotEqual(old_id, new_id)
        # 旧版本仍在（SUPERSEDED 而非删除）
        old = Asset.objects.get(pk=old_id)
        self.assertEqual(old.status, AssetStatus.SUPERSEDED)
        self.assertEqual(Asset.objects.filter(shot=self.shot, asset_type=AssetType.IMAGE).count(), 2)

    def test_prompt_change_changes_fingerprint(self):
        """改 visual_prompt 后指纹变化，不复用旧图。"""
        self._gen()
        self.shot.visual_prompt = "远景"
        self.shot.save(update_fields=["visual_prompt"])
        gw, r = self._gen()
        self.assertEqual(len(gw.calls), 1)
        self.assertFalse(r.output_summary.get("reused"))

    def test_legacy_no_fingerprint_not_reused(self):
        """历史无指纹资产不被当缓存命中（保守重建）。"""
        Asset.objects.create(
            play=self.play, episode=self.ep, scene=self.scene, shot=self.shot,
            asset_type=AssetType.IMAGE, status=AssetStatus.READY,
            artifact_url="/media/legacy.png", input_fingerprint="")
        gw, r = self._gen()
        self.assertEqual(len(gw.calls), 1)
        self.assertFalse(r.output_summary.get("reused"))


class ProjectStorageTests(TestCase):
    """任务 2.6：项目 UUID + 内容寻址保存，同内容不互相覆盖，路径逃逸拒绝。"""

    def setUp(self):
        self.owner = CustomUser.objects.create_user(
            username="a2s", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.play = Play.objects.create(title="存储剧", created_by=self.owner)

    def test_content_addressed_save(self):
        from apps.creation.workspace.storage import (
            asset_ext_for, project_dir, resolve_storage_key, save_asset_bytes,
        )
        key1, url1, sha1 = save_asset_bytes(self.play, b"ABC", "IMAGE")
        # sha256("ABC")：用 hashlib 即时验证而非硬编码（避免记错摘要）
        import hashlib as _h
        self.assertEqual(sha1, _h.sha256(b"ABC").hexdigest())
        self.assertTrue(key1.startswith(f"projects/{self.play.uuid}/assets/sha256/"))
        # 同内容幂等（同路径同字节）
        key2, _, _ = save_asset_bytes(self.play, b"ABC", "IMAGE")
        self.assertEqual(key1, key2)
        # 不同内容 → 不同路径
        key3, _, _ = save_asset_bytes(self.play, b"XYZ", "IMAGE")
        self.assertNotEqual(key1, key3)
        # 文件真实存在且可经 storage_key 解析回来
        p = resolve_storage_key(key1)
        self.assertTrue(p.is_file())
        self.assertEqual(p.read_bytes(), b"ABC")
        # URL 是 /media/ 相对地址
        self.assertTrue(url1.startswith("/media/projects/"))
        # 不同项目互不覆盖（UUID 目录隔离）
        other = Play.objects.create(title="他剧", created_by=self.owner)
        key4, _, _ = save_asset_bytes(other, b"ABC", "IMAGE")
        self.assertNotEqual(key1, key4)

    def test_resolve_rejects_escape(self):
        from apps.creation.workspace.storage import resolve_storage_key
        with self.assertRaises(ValueError):
            resolve_storage_key("../../etc/passwd")
        with self.assertRaises(ValueError):
            resolve_storage_key("projects/x/assets/sha256/ab/abcd.png/../../../escape.png")
        with self.assertRaises(ValueError):
            resolve_storage_key("")

    def test_project_dir_created(self):
        from apps.creation.workspace.storage import project_dir
        d = project_dir(self.play)
        self.assertTrue(d.is_dir())
        self.assertIn(str(self.play.uuid), str(d))
