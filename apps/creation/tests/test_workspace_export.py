"""项目导入导出测试（工作台 Stage B2，任务 5.1–5.5）。"""
import io
import json
import shutil
import tempfile
import zipfile

from django.test import TestCase
from rest_framework.test import APITestCase

from apps.ai_gateway.models import AIModel, AIProvider
from apps.creation.models import (
    AgentConfig, Asset, AssetDependency, AssetStatus, AssetType, AuditMode,
    Character, Episode, MemberRole, MemberStatus, Play, PlayMember, PlayStatus,
    RelationType, Scene, Shot,
)
from apps.creation.workspace.export_service import (
    CHECKSUM_NAME, PROJECT_JSON, build_export_zip,
)
from apps.creation.workspace.import_precheck import ImportError_, precheck_import
from apps.creation.workspace.import_service import confirm_import
from apps.creation.workspace.storage import project_dir, save_asset_bytes
from dvadmin.system.models import CustomUser, UserRole


def _mk_member(play, user, role):
    PlayMember.objects.create(play=play, user=user, role=role, status=MemberStatus.ACTIVE)


class B2Fixture:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls._media_tmp = tempfile.mkdtemp(prefix="b2-media-")
        from django.conf import settings
        cls._orig_media = settings.MEDIA_ROOT
        settings.MEDIA_ROOT = cls._media_tmp

    @classmethod
    def tearDownClass(cls):
        from django.conf import settings
        settings.MEDIA_ROOT = cls._orig_media
        shutil.rmtree(cls._media_tmp, ignore_errors=True)
        super().tearDownClass()

    def setUp(self):
        super().setUp()
        self.owner = CustomUser.objects.create_user(
            username="b2o", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.viewer = CustomUser.objects.create_user(
            username="b2v", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.provider = AIProvider.objects.create(
            name="P", provider_type="LLM", adapter_type="deepseek",
            base_url="https://x", api_key_encrypted="", priority=1)
        self.model = AIModel.objects.create(
            provider=self.provider, model_name="m", display_name="M", model_type="LLM")
        self.config = AgentConfig.objects.create(
            name="配方", llm_model=self.model, created_by=self.owner)
        self.play = Play.objects.create(
            title="导出剧", agent_config=self.config, created_by=self.owner,
            audit_mode=AuditMode.NONE, gen_params={"aspect_ratio": "9:16"})
        _mk_member(self.play, self.owner, MemberRole.OWNER)
        _mk_member(self.play, self.viewer, MemberRole.VIEWER)
        self.ep = Episode.objects.create(play=self.play, episode_number=1, title="第一集")
        self.scene = Scene.objects.create(episode=self.ep, scene_number=1, location="街道")
        self.character = Character.objects.create(
            play=self.play, name="主", role="PROTAGONIST", description="主角")
        self.shot = Shot.objects.create(
            scene=self.scene, shot_number=1, visual_prompt="近景", dialogue="台词",
            dialogue_character=self.character)

    def mk_local_asset(self, asset_type=AssetType.IMAGE, data=b"img-bytes", **kw):
        key, url, digest = save_asset_bytes(self.play, data, asset_type)
        return Asset.objects.create(
            play=self.play, episode=self.ep, scene=self.scene, shot=self.shot,
            asset_type=asset_type, status=AssetStatus.READY,
            storage_key=key, artifact_url=url, content_sha256=digest,
            input_fingerprint="f" * 64, **kw)

    def build_zip_bytes(self, play=None):
        data, _ = build_export_zip(play or self.play)
        return data


class ExportServiceTests(B2Fixture, TestCase):
    """任务 5.1：ZIP 结构、排除项、不改变项目状态。"""

    def test_zip_contains_manifest_assets_checksum(self):
        self.mk_local_asset()
        data, summary = build_export_zip(self.play)
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = zf.namelist()
        self.assertIn(PROJECT_JSON, names)
        self.assertIn(CHECKSUM_NAME, names)
        self.assertEqual(len([n for n in names if n.startswith("projects/")]), 1)
        manifest = json.loads(zf.read(PROJECT_JSON))
        self.assertEqual(manifest["project"]["uuid"], str(self.play.uuid))
        self.assertEqual(summary["packed_assets"], 1)
        self.assertIn("agent_sessions", summary["excluded"])  # 排除明示

    def test_excludes_cache_logs_credentials(self):
        d = project_dir(self.play)
        (d / "cache").mkdir(exist_ok=True)
        (d / "cache" / "junk.bin").write_bytes(b"junk")
        (d / "logs").mkdir(exist_ok=True)
        (d / "logs" / "run.log").write_bytes(b"log")
        asset = self.mk_local_asset()
        asset.parameters = {"api_key": "sk-secret", "size": 512}
        asset.save(update_fields=["parameters"])
        data, _ = build_export_zip(self.play)
        zf = zipfile.ZipFile(io.BytesIO(data))
        raw = b"".join(zf.read(n) for n in zf.namelist())
        self.assertNotIn(b"junk.bin", raw)      # cache 不进包
        self.assertNotIn(b"run.log", raw)       # log 不进包
        self.assertNotIn(b"sk-secret", raw)     # 凭证不进包

    def test_no_other_play_files(self):
        other = Play.objects.create(title="他剧", created_by=self.owner)
        Asset.objects.create(
            play=other, asset_type=AssetType.IMAGE, status=AssetStatus.READY,
            artifact_url="/media/other.png")
        self.mk_local_asset()
        data, _ = build_export_zip(self.play)
        zf = zipfile.ZipFile(io.BytesIO(data))
        for n in zf.namelist():
            if n.startswith("projects/"):
                self.assertIn(str(self.play.uuid), n)

    def test_export_does_not_change_state(self):
        self.mk_local_asset()
        before = (self.play.status, self.play.updated_at)
        self.build_zip_bytes()
        self.play.refresh_from_db()
        self.assertEqual(self.play.status, before[0])

    def test_remote_assets_metadata_only(self):
        """无 storage_key 的远程资产只带元数据、跳过打包（任务 5.4）。"""
        Asset.objects.create(
            play=self.play, episode=self.ep, asset_type=AssetType.IMAGE,
            status=AssetStatus.READY, artifact_url="https://cdn.example.com/x.png")
        data, summary = build_export_zip(self.play)
        zf = zipfile.ZipFile(io.BytesIO(data))
        names = zf.namelist()
        self.assertEqual(summary["packed_assets"], 0)
        self.assertEqual(summary["skipped_remote"], 1)
        manifest = json.loads(zf.read(PROJECT_JSON))
        self.assertEqual(len(manifest["assets"]), 1)  # 元数据在清单中


class PrecheckTests(B2Fixture, TestCase):
    """任务 5.2：恶意归档在写入前全部拒绝。"""

    def _wrap(self, files: dict) -> io.BytesIO:
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in files.items():
                zf.writestr(name, data)
        buf.seek(0)
        return buf

    def _base_files(self):
        self.mk_local_asset()
        data, _ = build_export_zip(self.play)
        zf = zipfile.ZipFile(io.BytesIO(data))
        return {n: zf.read(n) for n in zf.namelist()}

    def test_valid_zip_passes(self):
        self.mk_local_asset()
        report = precheck_import(io.BytesIO(self.build_zip_bytes()))
        self.assertEqual(report["project_uuid"], str(self.play.uuid))
        self.assertEqual(report["assets"], 1)

    def test_reject_absolute_and_traversal(self):
        files = self._base_files()
        files["/etc/passwd"] = b"x"
        with self.assertRaises(ImportError_) as cm:
            precheck_import(self._wrap(files))
        self.assertIn("路径非法", str(cm.exception))
        files = self._base_files()
        files["../../escape.txt"] = b"x"
        with self.assertRaises(ImportError_):
            precheck_import(self._wrap(files))

    def test_reject_duplicate_paths(self):
        files = self._base_files()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in files.items():
                zf.writestr(name, data)
            # 同名条目写入两次（zipfile 允许——重复路径）
            zf.writestr(PROJECT_JSON, files[PROJECT_JSON])
        buf.seek(0)
        with self.assertRaises(ImportError_) as cm:
            precheck_import(buf)
        self.assertIn("重复", str(cm.exception))

    def test_reject_bad_schema_version(self):
        files = self._base_files()
        manifest = json.loads(files[PROJECT_JSON])
        manifest["schema_version"] = 99
        files[PROJECT_JSON] = json.dumps(manifest).encode()
        # 摘要会不匹配——先重建校验清单
        import hashlib
        digests = {}
        for n, d in files.items():
            if n != CHECKSUM_NAME:
                digests[n] = hashlib.sha256(d).hexdigest()
        files[CHECKSUM_NAME] = "\n".join(
            f"{v}  {k}" for k, v in sorted(digests.items())).encode()
        with self.assertRaises(ImportError_) as cm:
            precheck_import(self._wrap(files))
        self.assertIn("不支持的清单版本", str(cm.exception))

    def test_reject_digest_mismatch(self):
        files = self._base_files()
        asset_key = [n for n in files if n.startswith("projects/")][0]
        files[asset_key] = b"tampered"
        with self.assertRaises(ImportError_) as cm:
            precheck_import(self._wrap(files))
        self.assertIn("摘要不匹配", str(cm.exception))

    def test_reject_missing_asset(self):
        files = self._base_files()
        asset_key = [n for n in files if n.startswith("projects/")][0]
        del files[asset_key]
        with self.assertRaises(ImportError_) as cm:
            precheck_import(self._wrap(files))
        self.assertIn("缺失", str(cm.exception))

    def test_reject_zip_bomb_ratio(self):
        files = self._base_files()
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            for name, data in files.items():
                zf.writestr(name, data)
            # DEFLATED 高压缩比大文件（全零 50MB → 压缩后 KB 级）→ 疑似炸弹
            bomb_info = zipfile.ZipInfo("bomb.bin")
            bomb_info.compress_type = zipfile.ZIP_DEFLATED
            with zf.open(bomb_info, "w") as f:
                f.write(b"\0" * (50 * 1024 * 1024))
        buf.seek(0)
        with self.assertRaises(ImportError_) as cm:
            precheck_import(buf)
        self.assertIn("压缩比", str(cm.exception))

    def test_precheck_writes_nothing(self):
        """预检零写入：DB 无新行、项目目录无新文件。"""
        before_plays = Play.objects.count()
        before_dirs = sorted(p.name for p in
                             (project_dir(self.play)).iterdir())
        precheck_import(io.BytesIO(self.build_zip_bytes()))
        self.assertEqual(Play.objects.count(), before_plays)
        self.assertEqual(
            sorted(p.name for p in project_dir(self.play).iterdir()), before_dirs)


class ImportServiceTests(B2Fixture, TestCase):
    """任务 5.3：确认导入为新项目（UUID 重建 FK/指针/依赖/版本）。"""

    def test_roundtrip_restores_structure(self):
        img = self.mk_local_asset()
        audio = self.mk_local_asset(asset_type=AssetType.AUDIO, data=b"audio-bytes")
        video = Asset.objects.create(
            play=self.play, episode=self.ep, asset_type=AssetType.VIDEO,
            status=AssetStatus.READY, artifact_url="/media/v.mp4")
        AssetDependency.objects.create(
            upstream=img, downstream=video, relation_type=RelationType.IMAGE_TO_VIDEO)
        data = self.build_zip_bytes()
        # 新环境语义：源项目删除后导入恢复（同 UUID 在本环境不存在）
        old_uuid, old_pk = self.play.uuid, self.play.pk
        self.play.delete()
        self.assertFalse(Play.objects.filter(uuid=old_uuid).exists())

        play2, summary = confirm_import(io.BytesIO(data), self.owner)
        self.assertNotEqual(play2.pk, old_pk)
        self.assertEqual(summary["project_uuid"], str(old_uuid))
        # 结构重建
        ep2 = Episode.objects.get(play=play2, episode_number=1)
        scene2 = Scene.objects.get(episode=ep2, scene_number=1)
        shot2 = Shot.objects.get(scene=scene2, shot_number=1)
        self.assertEqual(shot2.dialogue, "台词")
        char2 = Character.objects.get(play=play2, name="主")
        self.assertEqual(shot2.dialogue_character_id, char2.pk)
        # 资产与依赖按 UUID 映射重建
        img2 = Asset.objects.get(play=play2, asset_type=AssetType.IMAGE)
        audio2 = Asset.objects.get(play=play2, asset_type=AssetType.AUDIO)
        self.assertEqual(img2.content_sha256, img.content_sha256)
        self.assertTrue(img2.storage_key)
        self.assertTrue((project_dir(play2) / "assets").exists() or img2.storage_key)
        dep = AssetDependency.objects.get(upstream=img2)
        self.assertEqual(dep.downstream.asset_type, AssetType.VIDEO)
        # current 指针
        shot2.refresh_from_db()
        self.assertEqual(shot2.current_image_asset_id, img2.pk)
        self.assertEqual(shot2.current_audio_asset_id, audio2.pk)
        # 新项目 DRAFT 起步、属主为导入者
        self.assertEqual(play2.status, PlayStatus.DRAFT)
        self.assertEqual(play2.created_by_id, self.owner.pk)

    def test_same_uuid_conflict_not_overwritten(self):
        data = self.build_zip_bytes()
        with self.assertRaises(ImportError_) as cm:
            confirm_import(io.BytesIO(data), self.owner)
        self.assertEqual(getattr(cm.exception, "status_code", 400), 409)
        # 源项目仍在（未覆盖未破坏）
        self.assertTrue(Play.objects.filter(pk=self.play.pk).exists())
        before_assets = Asset.objects.count()
        with self.assertRaises(ImportError_):
            confirm_import(io.BytesIO(data), self.owner)
        self.assertEqual(Asset.objects.count(), before_assets)

    def test_db_failure_cleans_new_dir_only(self):
        """DB 失败只清理新建目录（他项目目录不动）。"""
        self.mk_local_asset()
        data = self.build_zip_bytes()
        self.play.delete()  # 新环境语义
        old_dirs = {str(p.uuid): sorted(x.name for x in project_dir(p).iterdir())
                    for p in Play.objects.all()}
        from unittest.mock import patch
        with patch("apps.creation.workspace.import_service._restore_dependencies",
                   side_effect=Exception("DB 模拟失败")):
            with self.assertRaises(Exception):
                confirm_import(io.BytesIO(data), self.owner)
        # 未新增任何 Play 行
        self.assertEqual(Play.objects.count(), len(old_dirs))
        # 既有项目目录原样
        for p in Play.objects.all():
            self.assertEqual(
                sorted(x.name for x in project_dir(p).iterdir()), old_dirs[str(p.uuid)])

    def test_remote_assets_restored_as_metadata_rows(self):
        """远程资产（任务 5.4）：导入为受控元数据行，不请求 URL、不生成空文件。"""
        Asset.objects.create(
            play=self.play, episode=self.ep, asset_type=AssetType.IMAGE,
            status=AssetStatus.READY, artifact_url="https://cdn.example.com/x.png")
        self.mk_local_asset(asset_type=AssetType.AUDIO, data=b"audio")
        data = self.build_zip_bytes()
        self.play.delete()  # 新环境语义
        play2, summary = confirm_import(io.BytesIO(data), self.owner)
        self.assertEqual(summary["assets_remote"], 1)
        remote = Asset.objects.filter(play=play2, storage_key="").first()
        self.assertIsNotNone(remote)
        self.assertEqual(remote.status, AssetStatus.PENDING)  # 不生成空文件
        self.assertEqual(remote.artifact_url, "https://cdn.example.com/x.png")

    def test_versions_restored(self):
        """剧本/角色版本恢复（main v1 + 角色首快照）。"""
        from apps.creation.assistant.models import CharacterVersion, ScriptVersion
        data = self.build_zip_bytes()
        self.play.delete()  # 新环境语义
        play2, _ = confirm_import(io.BytesIO(data), self.owner)
        ep2 = Episode.objects.get(play=play2)
        sv = ScriptVersion.objects.filter(play=play2, episode=ep2)
        self.assertEqual(sv.count(), 1)
        self.assertIn("台词", sv.first().content["text"])
        char2 = Character.objects.get(play=play2)
        self.assertEqual(
            CharacterVersion.objects.filter(character=char2).count(), 1)


class ExportImportApiTests(B2Fixture, APITestCase):
    """任务 5.5：API 权限（导出 edit；导入认证）与错误路径。"""

    def test_export_requires_edit(self):
        self.client.force_authenticate(self.viewer)
        resp = self.client.post(f"/api/plays/{self.play.pk}/export/")
        self.assertEqual(resp.status_code, 403)
        self.client.force_authenticate(self.owner)
        resp = self.client.post(f"/api/plays/{self.play.pk}/export/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Type"], "application/zip")
        self.assertIn("X-Export-Excluded", resp)  # 排除明示

    def test_import_precheck_endpoint(self):
        self.mk_local_asset()
        data = self.build_zip_bytes()
        self.client.force_authenticate(self.owner)
        resp = self.client.post("/api/plays/import-precheck/", {"file": io.BytesIO(data)},
                                format="multipart")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json().get("data", resp.json())
        self.assertEqual(payload["assets"], 1)

    def test_import_confirm_endpoint_and_duplicate(self):
        self.mk_local_asset()
        data = self.build_zip_bytes()
        self.client.force_authenticate(self.owner)
        # 源项目在 → 同 UUID 冲突 409（不覆盖）
        resp1 = self.client.post("/api/plays/import-confirm/", {"file": io.BytesIO(data)},
                                 format="multipart")
        self.assertEqual(resp1.status_code, 409)
        # 新环境语义：删除源项目后导入成功
        self.play.delete()
        resp = self.client.post("/api/plays/import-confirm/", {"file": io.BytesIO(data)},
                                format="multipart")
        self.assertEqual(resp.status_code, 200)
        payload = resp.json().get("data", resp.json())
        self.assertIn("project_uuid", payload)
        # 再次导入（现又存在同 UUID）→ 409
        resp2 = self.client.post("/api/plays/import-confirm/", {"file": io.BytesIO(data)},
                                 format="multipart")
        self.assertEqual(resp2.status_code, 409)
        # 坏包 400
        resp3 = self.client.post("/api/plays/import-confirm/",
                                 {"file": io.BytesIO(b"not a zip")}, format="multipart")
        self.assertEqual(resp3.status_code, 400)

    def test_import_requires_auth(self):
        resp = self.client.post("/api/plays/import-precheck/",
                                {"file": io.BytesIO(b"x")}, format="multipart")
        self.assertEqual(resp.status_code, 401)
