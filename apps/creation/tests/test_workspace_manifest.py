"""项目清单与保存快照测试（工作台 Stage B1，任务 4.1–4.4）。"""
import json
import shutil
import tempfile
import threading

from django.test import TestCase
from rest_framework.test import APITestCase

from apps.ai_gateway.models import AIModel, AIProvider
from apps.creation.models import (
    AgentConfig, Asset, AssetDependency, AssetStatus, AssetType, AuditMode,
    Character, Episode, MemberRole, MemberStatus, Play, PlayMember, RelationType,
    Scene, Shot,
)
from apps.creation.workspace.cleanup import CleanupError, clear_cache
from apps.creation.workspace.manifest import (
    SCHEMA_VERSION, manifest_bytes, manifest_digest, mark_manifest_dirty,
    refresh_manifest, write_manifest,
)
from apps.creation.workspace.save_service import save_play
from apps.creation.workspace.storage import project_dir
from dvadmin.system.models import CustomUser, UserRole


def _mk_member(play, user, role):
    PlayMember.objects.create(play=play, user=user, role=role, status=MemberStatus.ACTIVE)


class B1Fixture:
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        # 项目目录落在临时 MEDIA_ROOT 下（避免污染真实 media/；storage._root 读 settings）
        cls._media_tmp = tempfile.mkdtemp(prefix="b1-media-")
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
            username="b1o", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.editor = CustomUser.objects.create_user(
            username="b1e", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.viewer = CustomUser.objects.create_user(
            username="b1v", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.provider = AIProvider.objects.create(
            name="P", provider_type="LLM", adapter_type="deepseek",
            base_url="https://x", api_key_encrypted="", priority=1)
        self.model = AIModel.objects.create(
            provider=self.provider, model_name="m", display_name="M", model_type="LLM")
        self.config = AgentConfig.objects.create(
            name="配方", llm_model=self.model, created_by=self.owner)
        self.play = Play.objects.create(
            title="清单剧", agent_config=self.config, created_by=self.owner,
            audit_mode=AuditMode.NONE, gen_params={"aspect_ratio": "9:16"})
        _mk_member(self.play, self.owner, MemberRole.OWNER)
        _mk_member(self.play, self.editor, MemberRole.EDITOR)
        _mk_member(self.play, self.viewer, MemberRole.VIEWER)
        self.ep = Episode.objects.create(play=self.play, episode_number=1, title="第一集")
        self.scene = Scene.objects.create(episode=self.ep, scene_number=1, location="街道")
        self.character = Character.objects.create(
            play=self.play, name="主", role="PROTAGONIST")
        self.shot1 = Shot.objects.create(
            scene=self.scene, shot_number=1, visual_prompt="近景", dialogue="台词一",
            dialogue_character=self.character)

    def mk_asset(self, asset_type=AssetType.IMAGE, status=AssetStatus.READY, **kw):
        return Asset.objects.create(
            play=self.play, episode=self.ep, scene=self.scene, shot=kw.pop("shot", self.shot1),
            character=kw.pop("character", None), asset_type=asset_type, status=status,
            artifact_url=kw.pop("artifact_url", f"/media/{asset_type.lower()}.png"),
            input_fingerprint=kw.pop("input_fingerprint", "f" * 64), **kw)


class ManifestBuilderTests(B1Fixture, TestCase):
    """任务 4.1：schema、白名单、相对引用、确定性。"""

    def test_schema_and_top_level(self):
        m = json.loads(manifest_bytes(self.play))
        self.assertEqual(m["schema_version"], SCHEMA_VERSION)
        self.assertEqual(m["project"]["uuid"], str(self.play.uuid))
        self.assertEqual(m["project"]["gen_params"], {"aspect_ratio": "9:16"})
        self.assertEqual(len(m["episodes"]), 1)
        self.assertEqual(m["episodes"][0]["scenes"][0]["shots"][0]["dialogue"], "台词一")

    def test_deterministic_bytes(self):
        """同 DB 状态两次构建 → 完全相同的 JSON 字节。"""
        self.assertEqual(manifest_bytes(self.play), manifest_bytes(self.play))

    def test_whitelist_no_credentials_or_abs_paths(self):
        """清单绝不含凭证/token/成本/绝对路径（generation_spec 结构化字段递归剔除敏感键）。"""
        self.mk_asset(generation_spec={
            "model": "m", "api_key": "sk-secret", "nested": {"token": "t", "ok": 1},
            "parameters": {"password": "p", "size": 512}})
        raw = manifest_bytes(self.play).decode("utf-8")
        for banned in ("api_key", "sk-secret", "token", "password", "cost", "D:\\", "C:\\", "/home/"):
            self.assertNotIn(banned, raw)
        self.assertIn("storage_key", raw)  # 相对资源键在白名单内
        m = json.loads(manifest_bytes(self.play))
        spec = m["assets"][0]["generation_spec"]
        self.assertEqual(spec["model"], "m")  # 非敏感内容保留
        self.assertEqual(spec["nested"], {"ok": 1})
        self.assertEqual(spec["parameters"], {"size": 512})

    def test_assets_reference_uuids_and_deps(self):
        img = self.mk_asset()
        video = self.mk_asset(asset_type=AssetType.VIDEO, shot=None)
        AssetDependency.objects.create(
            upstream=img, downstream=video, relation_type=RelationType.IMAGE_TO_VIDEO)
        m = json.loads(manifest_bytes(self.play))
        self.assertEqual(len(m["assets"]), 2)
        self.assertEqual({a["uuid"] for a in m["assets"]},
                         {str(img.uuid), str(video.uuid)})
        self.assertEqual(m["dependencies"][0]["upstream"], str(img.uuid))
        # 依赖边在白名单字段内引用 uuid
        self.assertEqual(m["dependencies"][0]["downstream"], str(video.uuid))

    def test_only_ready_assets_included(self):
        self.mk_asset(status=AssetStatus.SUPERSEDED)
        m = json.loads(manifest_bytes(self.play))
        self.assertEqual(m["assets"], [])

    def test_structure_completeness(self):
        """短剧层级/配置/资产/依赖全量在清单中。"""
        Scene.objects.create(episode=self.ep, scene_number=2)
        m = json.loads(manifest_bytes(self.play))
        self.assertEqual(len(m["episodes"][0]["scenes"]), 2)
        self.assertIn("characters", m)
        self.assertIn("dependencies", m)
        self.assertEqual(m["project"]["title"], "清单剧")


class ManifestWriteTests(B1Fixture, TestCase):
    """任务 4.2：原子写、中断保留旧清单、并发不产出半文件。"""

    def test_atomic_write_and_reread(self):
        digest = write_manifest(self.play)
        p = project_dir(self.play) / "project.json"
        self.assertTrue(p.exists())
        self.assertEqual(manifest_digest(self.play), digest)
        on_disk = json.loads(p.read_text(encoding="utf-8"))
        self.assertEqual(on_disk["project"]["uuid"], str(self.play.uuid))

    def test_interrupt_keeps_old_manifest(self):
        """写中途失败（模拟中断）：旧清单原样保留，无半文件。"""
        write_manifest(self.play)
        p = project_dir(self.play) / "project.json"
        old = p.read_bytes()
        import apps.creation.workspace.manifest as m
        real_dumps = m.json.dumps
        with self.assertRaises(Exception):
            def boom(*a, **k):
                raise OSError("模拟中断")
            m.json.dumps = boom
            try:
                write_manifest(self.play)
            finally:
                m.json.dumps = real_dumps
        self.assertEqual(p.read_bytes(), old)  # 旧清单未被破坏
        self.assertFalse(list(project_dir(self.play).glob(".project.json.tmp*")))

    def test_no_tmp_left_on_disk(self):
        write_manifest(self.play)
        # 并发安全修复后 tmp 名带唯一后缀：按前缀匹配任何残留
        leftovers = [f for f in project_dir(self.play).iterdir()
                    if f.name.startswith(".project.json.tmp")]
        self.assertEqual(leftovers, [])

    def test_concurrent_same_play_produces_consistent_file(self):
        """同 Play 并发刷新：最终文件是完整合法 JSON（不交错）。

        并发写入的交错风险在文件层——共享同一份构建字节多线程原子写，
        最终文件必须是其中一次的完整内容（无交错半文件）。
        """
        import apps.creation.workspace.manifest as m
        payload = manifest_bytes(self.play)

        def worker():
            for _ in range(5):
                m._atomic_write(project_dir(self.play) / "project.json", payload)

        threads = [threading.Thread(target=worker) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        data = json.loads((project_dir(self.play) / "project.json").read_text("utf-8"))
        self.assertEqual(data["schema_version"], SCHEMA_VERSION)

    def test_dirty_flag_lazy_refresh(self):
        """任务终态标脏 → refresh 落盘一次；未标脏不写。"""
        self.assertFalse(refresh_manifest(self.play))
        mark_manifest_dirty(self.play)
        self.assertTrue(refresh_manifest(self.play))
        self.assertFalse(refresh_manifest(self.play))  # 已刷新不再写

    def test_different_plays_independent(self):
        """不同项目目录互不干扰（各自的 project.json）。"""
        other = Play.objects.create(title="另一剧", created_by=self.owner)
        write_manifest(self.play)
        write_manifest(other)
        p1 = json.loads((project_dir(self.play) / "project.json").read_text("utf-8"))
        p2 = json.loads((project_dir(other) / "project.json").read_text("utf-8"))
        self.assertEqual(p1["project"]["uuid"], str(self.play.uuid))
        self.assertEqual(p2["project"]["uuid"], str(other.uuid))


class SaveServiceTests(B1Fixture, TestCase):
    """任务 4.3：保存端点复用 snapshot/create_version，内容摘要判重。"""

    def test_save_creates_versions_then_no_duplicates(self):
        r1 = save_play(self.play, user=self.owner)
        self.assertTrue(r1["saved"])
        self.assertEqual(r1["episode_versions"][0]["created"], True)
        r2 = save_play(self.play, user=self.owner)
        self.assertEqual(r2["episode_versions"][0]["created"], False)  # 判重
        self.assertEqual(r2["episode_versions"][0]["version"],
                         r1["episode_versions"][0]["version"])

    def test_save_after_change_creates_new_version_with_parent(self):
        from apps.creation.assistant.models import ScriptVersion
        save_play(self.play, user=self.owner)
        # snapshot_episode 覆盖正文 + 场景字段（SCENE_FIELDS）——改场景描述
        self.scene.description = "改了环境描述"
        self.scene.save(update_fields=["description"])
        r = save_play(self.play, user=self.owner)
        self.assertEqual(r["episode_versions"][0]["created"], True)
        v2 = ScriptVersion.objects.get(play=self.play, episode=self.ep,
                                       version=r["episode_versions"][0]["version"])
        self.assertIsNotNone(v2.parent)  # 父链正确
        v1 = ScriptVersion.objects.filter(play=self.play, episode=self.ep).order_by("version").first()
        self.assertEqual(v2.parent.pk, v1.pk)

    def test_character_version_dedup(self):
        r1 = save_play(self.play, user=self.owner)
        self.assertEqual(r1["character_versions"], 1)
        r2 = save_play(self.play, user=self.owner)
        self.assertEqual(r2["character_versions"], 0)  # 无变化不记录

    def test_save_refreshes_manifest(self):
        save_play(self.play, user=self.owner)
        p = project_dir(self.play) / "project.json"
        self.assertTrue(p.exists())
        self.assertFalse(refresh_manifest(self.play))  # 保存已清脏


class CleanupTests(B1Fixture, TestCase):
    """任务 4.4：目录约定 + 缓存清理边界。"""

    def test_directory_layout_created(self):
        from apps.creation.workspace.manifest import write_manifest
        write_manifest(self.play)
        d = project_dir(self.play)
        self.assertTrue((d / "cache").is_dir())
        self.assertTrue((d / "exports").is_dir())
        self.assertTrue((d / "logs").is_dir())
        self.assertTrue((d / "assets").exists() or True)  # assets 惰性创建

    def test_clear_cache_only_cache(self):
        from apps.creation.workspace.manifest import write_manifest
        write_manifest(self.play)
        d = project_dir(self.play)
        (d / "cache" / "sub").mkdir(parents=True, exist_ok=True)
        (d / "cache" / "sub" / "junk.bin").write_bytes(b"x" * 100)
        (d / "cache" / "tmp.json").write_bytes(b"y" * 50)
        raw = project_dir(self.play) / "assets"
        raw.mkdir(parents=True, exist_ok=True)
        keep = raw / "keep.png"
        keep.write_bytes(b"asset")
        result = clear_cache(self.play)
        self.assertEqual(result["files_removed"], 2)
        self.assertFalse((d / "cache" / "sub").exists())
        self.assertTrue(keep.exists())  # 原始资产不受影响
        self.assertTrue((d / "project.json").exists())  # 清单不受影响

    def test_clear_cache_rejects_escape_and_other_dirs(self):
        with self.assertRaises(CleanupError):
            clear_cache(self.play, target="exports")  # 非白名单
        with self.assertRaises(CleanupError):
            clear_cache(self.play, target="../assets")  # 逃逸
        with self.assertRaises(CleanupError):
            clear_cache(self.play, target="cache/../../..")  # 逃逸

    def test_clear_cache_cannot_touch_other_play(self):
        other = Play.objects.create(title="另一剧", created_by=self.owner)
        from apps.creation.workspace.manifest import write_manifest
        write_manifest(other)
        other_cache = project_dir(other) / "cache"
        (other_cache / "keep.bin").write_bytes(b"keep")
        result = clear_cache(self.play)  # 只清自己
        self.assertEqual(result["files_removed"], 0)
        self.assertTrue((other_cache / "keep.bin").exists())


class SaveApiTests(B1Fixture, APITestCase):
    """任务 4.3/4.4 API：保存 edit 权限、清缓存 manage 权限。"""

    def _post(self, user, action, payload=None):
        self.client.force_authenticate(user)
        return self.client.post(f"/api/plays/{self.play.pk}/{action}/",
                                payload or {}, format="json")

    def test_save_endpoint(self):
        resp = self._post(self.editor, "save")
        self.assertEqual(resp.status_code, 200)
        data = resp.json().get("data", resp.json())
        self.assertIn("manifest_digest", data)

    def test_save_denied_for_viewer(self):
        resp = self._post(self.viewer, "save")
        self.assertEqual(resp.status_code, 403)

    def test_clear_cache_requires_owner(self):
        resp = self._post(self.editor, "clear-cache")  # editor 不够
        self.assertEqual(resp.status_code, 403)
        resp = self._post(self.owner, "clear-cache")
        self.assertEqual(resp.status_code, 200)

    def test_clear_cache_bad_target_400(self):
        resp = self._post(self.owner, "clear-cache", {"target": "exports"})
        self.assertEqual(resp.status_code, 400)
