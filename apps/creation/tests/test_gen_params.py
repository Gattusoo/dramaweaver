"""统一创作参数解析层测试（工作台 Stage A1，任务 1.1–1.5）。

覆盖：
- resolve_gen_params 优先级链（task > Play > AgentConfig > 默认）与 unsupported 回显
- generate_image 把有效 aspect_ratio 传入网关 payload 并记入 Asset 参数/元数据
- _canvas / _cover 按有效比例生成 16:9 画布（含 W/H patch 兼容）
- _effective_style 的 style → 配方 grade 映射
- 双模式 settings：默认 shared、local 回环校验、local SQLite 默认目录、shared 不变
"""
import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import MagicMock, patch

from django.test import SimpleTestCase, TestCase

from apps.ai_gateway.models import AIModel, AIProvider
from apps.creation.models import (
    AgentConfig, Asset, AssetStatus, AssetType, AuditMode, Episode, Play, Scene, Shot,
    TaskRecord, TaskType,
)
from apps.creation.pipeline.gen_params import (
    ASPECT_RATIOS, aspect_ratio_dims, resolve_gen_params,
)
from apps.creation.tasks import _canvas, _cover, _effective_style
from apps.core.system.models import CustomUser, UserRole


class GenParamsResolveTests(SimpleTestCase):
    """resolve_gen_params：优先级链、unsupported 回显、缺省行为。"""

    def setUp(self):
        class _Cfg:
            def __init__(self, style_params):
                self.style_params = style_params
        self._Cfg = _Cfg

        class _Play:
            def __init__(self, gen_params):
                self.gen_params = gen_params
        self._Play = _Play

    def test_default_no_params(self):
        """三层都没配置：effective 为空（缺省不硬塞默认值，行为不变）。"""
        r = resolve_gen_params(self._Play({}), self._Cfg({}))
        self.assertEqual(r["effective"], {})
        self.assertEqual(r["unsupported_params"], [])
        self.assertIsNone(r["style_mapping"])

    def test_play_aspect_ratio_wins_over_config(self):
        """Play.gen_params.aspect_ratio 覆盖配方 aspect_ratio（spec: 短剧画面比例覆盖配方）。"""
        play = self._Play({"aspect_ratio": "16:9"})
        cfg = self._Cfg({"aspect_ratio": "9:16"})
        r = resolve_gen_params(play, cfg)
        self.assertEqual(r["effective"]["aspect_ratio"], "16:9")
        self.assertEqual(r["sources"]["aspect_ratio"], "play")

    def test_task_params_highest_priority(self):
        """任务显式参数最高优先级。"""
        play = self._Play({"aspect_ratio": "16:9"})
        cfg = self._Cfg({"aspect_ratio": "9:16"})
        r = resolve_gen_params(play, cfg, {"aspect_ratio": "1:1"})
        self.assertEqual(r["effective"]["aspect_ratio"], "1:1")
        self.assertEqual(r["sources"]["aspect_ratio"], "task")

    def test_config_aspect_ratio_used_when_play_empty(self):
        """Play 未配置时回落配方 aspect_ratio。"""
        r = resolve_gen_params(self._Play({}), self._Cfg({"aspect_ratio": "4:3"}))
        self.assertEqual(r["effective"]["aspect_ratio"], "4:3")
        self.assertEqual(r["sources"]["aspect_ratio"], "config")

    def test_invalid_aspect_ratio_reported_unsupported(self):
        """非法比例值（不在支持表内）进 unsupported，不猜近似比例。"""
        r = resolve_gen_params(self._Play({"aspect_ratio": "21:9"}), self._Cfg({}))
        self.assertNotIn("aspect_ratio", r["effective"])
        self.assertIn("aspect_ratio", r["unsupported_params"])

    def test_style_maps_to_supported_grade(self):
        """style 命中配方 style_params.grade 时映射生效。"""
        cfg = self._Cfg({"grade": {"cinematic": {"brightness": 0.1, "contrast": 1.2}}})
        r = resolve_gen_params(self._Play({"style": "cinematic"}), cfg)
        self.assertEqual(r["effective"]["style"], "cinematic")
        self.assertEqual(r["style_mapping"], {"grade": {"brightness": 0.1, "contrast": 1.2}})

    def test_unknown_style_unsupported(self):
        """配方未配置该风格：style 进 unsupported，不猜映射。"""
        cfg = self._Cfg({"grade": {"cinematic": {"brightness": 0.1}}})
        r = resolve_gen_params(self._Play({"style": "vhs"}), cfg)
        self.assertNotIn("style", r["effective"])
        self.assertIn("style", r["unsupported_params"])

    def test_style_without_grade_config_unsupported(self):
        """配方完全没配 grade：任何 style 都进 unsupported。"""
        r = resolve_gen_params(self._Play({"style": "cinematic"}), self._Cfg({}))
        self.assertIn("style", r["unsupported_params"])

    def test_reserved_params_reported(self):
        """create_type/duration/count 携带即明示保留，不静默宣称生效。"""
        play = self._Play({"create_type": "film", "duration": 60})
        r = resolve_gen_params(play, self._Cfg({}), {"count": 3})
        self.assertIn("create_type", r["unsupported_params"])
        self.assertIn("duration", r["unsupported_params"])
        self.assertIn("count", r["unsupported_params"])
        self.assertNotIn("create_type", r["effective"])

    def test_none_play_and_config_safe(self):
        """无 Play / 无配方（null recipe）时安全返回空。"""
        r = resolve_gen_params(None, None)
        self.assertEqual(r["effective"], {})

    def test_aspect_ratio_dims(self):
        """比例 → 尺寸表；未知比例 None。"""
        self.assertEqual(aspect_ratio_dims("16:9"), (1920, 1080))
        self.assertEqual(aspect_ratio_dims("9:16"), (1080, 1920))
        self.assertIsNone(aspect_ratio_dims("21:9"))
        self.assertIsNone(aspect_ratio_dims(None))
        self.assertIn("16:9", ASPECT_RATIOS)


class ImageGenerationWiringTests(TestCase):
    """generate_image 把有效 aspect_ratio 传入网关 payload 并记入 Asset（任务 1.4）。"""

    def setUp(self):
        self.owner = CustomUser.objects.create_user(
            username="gp1", password="Passw0rd!123", role=UserRole.PRODUCER)
        self.provider = AIProvider.objects.create(
            name="P", provider_type="LLM", adapter_type="deepseek",
            base_url="https://x", api_key_encrypted="", priority=1)
        self.model = AIModel.objects.create(
            provider=self.provider, model_name="m", display_name="M", model_type="LLM")
        self.config = AgentConfig.objects.create(
            name="配方", llm_model=self.model, created_by=self.owner)
        self.play = Play.objects.create(
            title="比例剧", agent_config=self.config, created_by=self.owner,
            audit_mode=AuditMode.NONE, gen_params={"aspect_ratio": "16:9"})
        self.episode = Episode.objects.create(play=self.play, episode_number=1)
        self.scene = Scene.objects.create(episode=self.episode, scene_number=1)
        self.shot = Shot.objects.create(scene=self.scene, shot_number=1, visual_prompt="近景")

    class FakeGateway:
        def __init__(self):
            self.calls = []

        def call(self, provider_type, config, **payload):
            self.calls.append((provider_type, payload))
            return {"url": "https://x/img.png", "image_bytes": b"I", "usage": {}, "cost": 0}

    def test_gateway_receives_aspect_ratio(self):
        """Play.gen_params.aspect_ratio=16:9 → 网关 payload 带 aspect_ratio=16:9。"""
        from apps.creation.tasks import generate_image
        gw = self.FakeGateway()
        r = TaskRecord.objects.create(
            play=self.play, episode=self.episode, scene=self.scene, shot=self.shot,
            task_type=TaskType.IMAGE_GEN)
        with patch("apps.creation.tasks._gateway", return_value=gw), \
             patch("apps.creation.tasks._save_image_result", return_value="/media/images/x.png"):
            generate_image(r.pk)
        _, payload = gw.calls[0]
        self.assertEqual(payload.get("aspect_ratio"), "16:9")
        asset = Asset.objects.get(play=self.play, shot=self.shot, asset_type=AssetType.IMAGE)
        self.assertEqual(asset.parameters.get("aspect_ratio"), "16:9")
        self.assertEqual(asset.metadata["gen_params"], {"aspect_ratio": "16:9"})

    def test_no_params_no_behavior_change(self):
        """无 gen_params：payload 不带 aspect_ratio，Asset.parameters 为空（缺省行为不变）。"""
        from apps.creation.tasks import generate_image
        self.play.gen_params = {}
        self.play.save(update_fields=["gen_params"])
        gw = self.FakeGateway()
        r = TaskRecord.objects.create(
            play=self.play, episode=self.episode, scene=self.scene, shot=self.shot,
            task_type=TaskType.IMAGE_GEN)
        with patch("apps.creation.tasks._gateway", return_value=gw), \
             patch("apps.creation.tasks._save_image_result", return_value="/media/images/x.png"):
            generate_image(r.pk)
        _, payload = gw.calls[0]
        self.assertNotIn("aspect_ratio", payload)
        asset = Asset.objects.get(play=self.play, shot=self.shot, asset_type=AssetType.IMAGE)
        self.assertEqual(asset.parameters, {})
        self.assertNotIn("gen_params", asset.metadata)

    def test_unsupported_params_in_metadata(self):
        """保留字段（create_type 等）出现在 Asset.metadata.unsupported_params。"""
        from apps.creation.tasks import generate_image
        self.play.gen_params = {"aspect_ratio": "16:9", "create_type": "film", "count": 3}
        self.play.save(update_fields=["gen_params"])
        gw = self.FakeGateway()
        r = TaskRecord.objects.create(
            play=self.play, episode=self.episode, scene=self.scene, shot=self.shot,
            task_type=TaskType.IMAGE_GEN)
        with patch("apps.creation.tasks._gateway", return_value=gw), \
             patch("apps.creation.tasks._save_image_result", return_value="/media/images/x.png"):
            generate_image(r.pk)
        asset = Asset.objects.get(play=self.play, shot=self.shot, asset_type=AssetType.IMAGE)
        self.assertIn("create_type", asset.metadata["unsupported_params"])
        self.assertIn("count", asset.metadata["unsupported_params"])


class CanvasAspectTests(SimpleTestCase):
    """_canvas / _cover 按有效比例生成画布（任务 1.4）。"""

    def test_default_canvas_unchanged(self):
        """无比例：保持 W/H 默认画布且 full=True（_COVER 常量滤镜复用）。"""
        from apps.creation import tasks
        c = _canvas()
        self.assertEqual((c["w"], c["h"]), (tasks.W, tasks.H))
        self.assertTrue(c["full"])
        self.assertEqual(_cover(c), tasks._COVER)

    def test_canvas_16x9(self):
        """16:9 → 1920x1080 且 full=False（动态滤镜）。"""
        c = _canvas(aspect_ratio="16:9")
        self.assertEqual((c["w"], c["h"]), (1920, 1080))
        self.assertFalse(c["full"])
        cover = _cover(c)
        self.assertIn("scale=1920:1080", cover)
        self.assertIn("crop=1920:1080", cover)

    def test_canvas_unknown_ratio_falls_back(self):
        """未知比例：保持默认 W/H 画布（不猜）。"""
        c = _canvas(aspect_ratio="21:9")
        from apps.creation import tasks
        self.assertEqual((c["w"], c["h"]), (tasks.W, tasks.H))

    def test_preview_canvas_scaled_from_ratio(self):
        """预览画布按比例画布缩放（16:9 预览保持横画布）。"""
        c = _canvas({"preview_scale": 1 / 3}, preview=True, aspect_ratio="16:9")
        self.assertEqual((c["w"], c["h"]), (640, 360))

    def test_patch_w_compat(self):
        """测试 patch W/H（ffmpeg_quality 套件的用法）不受 aspect_ratio=None 路径影响。"""
        with patch("apps.creation.tasks.W", 180), patch("apps.creation.tasks.H", 320):
            c = _canvas()
            self.assertEqual((c["w"], c["h"]), (180, 320))
            self.assertTrue(c["full"])
            self.assertEqual(_cover(c), _cover())
            # 显式 9:16 与 patch 后的 W/H 不同尺寸 → full=False
            c2 = _canvas(aspect_ratio="9:16")
            self.assertEqual((c2["w"], c2["h"]), (1080, 1920))
            self.assertFalse(c2["full"])


class EffectiveStyleTests(SimpleTestCase):
    """_effective_style：style 命中配方 grade 时覆盖合成风格。"""

    class _Cfg:
        def __init__(self, style_params):
            self.style_params = style_params

    class _Play:
        def __init__(self, gen_params):
            self.gen_params = gen_params

    def test_style_overrides_grade(self):
        """Play.gen_params.style 命中配方 grade → 覆盖 style_params.grade。"""
        cfg = self._Cfg({"grade": {"base": {"contrast": 1.0}, "cinematic": {"contrast": 1.3}},
                        "shot_tail_padding": 0.4})
        play = self._Play({"style": "cinematic"})
        eff = _effective_style(cfg, play)
        self.assertEqual(eff["grade"], {"contrast": 1.3})
        self.assertEqual(eff["shot_tail_padding"], 0.4)  # 其他风格键保留

    def test_no_style_keeps_config(self):
        """未配置 style：style_params 原样（既有行为不变）。"""
        cfg = self._Cfg({"grade": {"base": {"contrast": 1.0}}, "shot_tail_padding": 0.4})
        eff = _effective_style(cfg, self._Play({}))
        self.assertEqual(eff, {"grade": {"base": {"contrast": 1.0}}, "shot_tail_padding": 0.4})

    def test_unknown_style_keeps_config(self):
        """style 未命中配方：保持配方原样（合成是本地确定性任务，不猜映射）。"""
        cfg = self._Cfg({"grade": {"cinematic": {"contrast": 1.3}}})
        eff = _effective_style(cfg, self._Play({"style": "vhs"}))
        self.assertEqual(eff["grade"], {"cinematic": {"contrast": 1.3}})

    def test_play_none(self):
        """play=None：纯配方 style_params。"""
        cfg = self._Cfg({"shot_tail_padding": 0.4})
        self.assertEqual(_effective_style(cfg, None), {"shot_tail_padding": 0.4})


# ── 双模式 settings（任务 1.1 / 1.2）─────────────────────────────────────────

def _reload_settings():
    """重新加载 application.settings（与 apps/core/system/tests/test_config.py 相同手法）。"""
    import django
    import importlib
    from django.conf import settings as django_settings

    for name in list(sys.modules):
        if name == "application.settings" or name.startswith("application.settings."):
            del sys.modules[name]
    type(django_settings)._wrapped = None
    importlib.import_module("application.settings")
    django_settings._setup()


class DeploymentModeSettingsTests(unittest.TestCase):
    """DEPLOYMENT_MODE 配置解析与启动安全检查（local 回环 / 数据目录 / SQLite 默认）。"""

    def setUp(self):
        self._env_backup = dict(os.environ)
        os.environ.pop("ALLOWED_HOSTS", None)
        os.environ.pop("DEPLOYMENT_MODE", None)
        os.environ.pop("CREATION_TASK_BACKEND", None)
        os.environ.pop("LOCAL_DATA_DIR", None)
        import conf
        self._conf = conf
        # 备份 conf.env 上会被测试改写的属性（含本机 env.py 的既有值，如 ALLOWED_HOSTS=["*"]）
        self._saved_attrs = {}
        for name in ("DEPLOYMENT_MODE", "LOCAL_DATA_DIR", "CREATION_TASK_BACKEND",
                     "DATABASE_NAME", "DATABASE_ENGINE", "ALLOWED_HOSTS"):
            self._saved_attrs[name] = getattr(conf.env, name, None)
        # 本机 env.py 的通配/遗留值不进测试（各用例显式设置需要的值）
        conf.env.ALLOWED_HOSTS = []
        conf.env.DEPLOYMENT_MODE = "shared"

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        import conf
        for name, val in self._saved_attrs.items():
            setattr(conf.env, name, val)
        _reload_settings()

    def _set_conf(self, **kw):
        import conf
        for name, val in kw.items():
            setattr(conf.env, name, val)

    def test_default_is_shared(self):
        """未设置 DEPLOYMENT_MODE：缺省 shared，不切换本地队列、不放宽鉴权。"""
        os.environ.pop("DEPLOYMENT_MODE", None)
        self._set_conf(DEPLOYMENT_MODE=None)
        _reload_settings()
        from django.conf import settings
        self.assertEqual(settings.DEPLOYMENT_MODE, "shared")
        self.assertFalse(settings.IS_LOCAL_MODE)
        # shared 缺省仍选择 Celery 传输（任务 1.1）
        self.assertEqual(settings.CREATION_TASK_BACKEND, "celery")
        # shared 不读取 local 快捷配置：LOCAL_DATA_DIR 不做要求
        self.assertEqual(settings.DATABASES["default"]["NAME"].name, "db.sqlite3")

    def test_local_minimal_config(self):
        """local 最小配置：数据目录 + 持久密钥 + 未配置 Redis 可启动（任务 1.2）。"""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop("DEPLOYMENT_MODE", None)
            os.environ.pop("DATABASE_NAME", None)
            os.environ.pop("SECRET_KEY", None)
            self._set_conf(DEPLOYMENT_MODE="local", LOCAL_DATA_DIR=tmp,
                           DATABASE_NAME=None, DATABASE_ENGINE=None, SECRET_KEY="")
            _reload_settings()
            from django.conf import settings
            self.assertTrue(settings.IS_LOCAL_MODE)
            self.assertEqual(settings.CREATION_TASK_BACKEND, "local")
            # SQLite 默认落数据目录
            self.assertEqual(Path(str(settings.DATABASES["default"]["NAME"])),
                             Path(tmp) / "db.sqlite3")
            # 回环监听缺省补齐
            self.assertIn("127.0.0.1", settings.ALLOWED_HOSTS)
            # DEBUG 下密钥持久化到数据目录（重启不换）
            key_file = Path(tmp) / ".secret_key"
            self.assertTrue(key_file.is_file())
            first = settings.SECRET_KEY
            _reload_settings()
            self.assertEqual(first, settings.SECRET_KEY)

    def test_local_requires_data_dir(self):
        """local 未配置 LOCAL_DATA_DIR：启动检查失败。"""
        os.environ.pop("DEPLOYMENT_MODE", None)
        self._set_conf(DEPLOYMENT_MODE="local", LOCAL_DATA_DIR="")
        with self.assertRaises(Exception) as ctx:
            _reload_settings()
        self.assertIn("LOCAL_DATA_DIR", str(ctx.exception))

    def test_local_rejects_non_loopback_hosts(self):
        """local 配置 0.0.0.0/局域网地址：启动检查失败并提示改用 shared。"""
        os.environ.pop("ALLOWED_HOSTS", None)
        with tempfile.TemporaryDirectory() as tmp:
            self._set_conf(DEPLOYMENT_MODE="local", LOCAL_DATA_DIR=tmp)
            os.environ["ALLOWED_HOSTS"] = "0.0.0.0"
            with self.assertRaises(Exception) as ctx:
                _reload_settings()
            self.assertIn("shared", str(ctx.exception))
            os.environ["ALLOWED_HOSTS"] = "192.168.1.10,example.com"
            with self.assertRaises(Exception) as ctx2:
                _reload_settings()
            self.assertIn("回环", str(ctx2.exception))

    def test_local_accepts_loopback_hosts(self):
        """local 显式配置回环地址：允许。"""
        os.environ.pop("ALLOWED_HOSTS", None)
        with tempfile.TemporaryDirectory() as tmp:
            self._set_conf(DEPLOYMENT_MODE="local", LOCAL_DATA_DIR=tmp)
            os.environ["ALLOWED_HOSTS"] = "127.0.0.1,localhost,::1"
            _reload_settings()
            from django.conf import settings
            self.assertEqual(settings.ALLOWED_HOSTS, ["127.0.0.1", "localhost", "::1"])

    def test_invalid_mode_rejected(self):
        """DEPLOYMENT_MODE 非法值：启动失败。"""
        self._set_conf(DEPLOYMENT_MODE="cluster")
        with self.assertRaises(Exception):
            _reload_settings()

    def test_shared_allows_any_hosts(self):
        """shared 模式不启用本机快捷身份/回环限制（既有部署行为不变）。"""
        os.environ.pop("ALLOWED_HOSTS", None)
        os.environ.pop("DEPLOYMENT_MODE", None)
        self._set_conf(DEPLOYMENT_MODE="shared")
        os.environ["ALLOWED_HOSTS"] = "example.com"
        _reload_settings()
        from django.conf import settings
        self.assertEqual(settings.ALLOWED_HOSTS, ["example.com"])

    def test_task_backend_explicit_celery_in_local(self):
        """local 显式 CREATION_TASK_BACKEND=celery：尊重显式配置（灰度切换用）。"""
        with tempfile.TemporaryDirectory() as tmp:
            os.environ.pop("DEPLOYMENT_MODE", None)
            self._set_conf(DEPLOYMENT_MODE="local", LOCAL_DATA_DIR=tmp,
                           CREATION_TASK_BACKEND="celery")
            _reload_settings()
            from django.conf import settings
            self.assertEqual(settings.CREATION_TASK_BACKEND, "celery")


class EnvTemplateTests(unittest.TestCase):
    """配置模板可加载且含双模式示例（任务 1.1 / 1.5）。"""

    def test_template_has_dual_mode_keys(self):
        """env.example.py 含 DEPLOYMENT_MODE/CREATION_TASK_BACKEND/LOCAL_DATA_DIR 且默认 shared。"""
        import conf
        template = Path(conf.__file__).parent / "env.example.py"
        self.assertTrue(template.is_file())
        # 模板按代码加载（与 conf/__init__.py 回退路径一致），验证可解析且默认值正确
        import importlib.util
        spec = importlib.util.spec_from_file_location("_tpl_env", template)
        mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(mod)
        self.assertEqual(mod.DEPLOYMENT_MODE, "shared")
        self.assertEqual(mod.CREATION_TASK_BACKEND, "")
        self.assertEqual(mod.LOCAL_DATA_DIR, "")
        text = template.read_text(encoding="utf-8")
        # 双模式说明完整（local 最小配置与 shared 兼容说明）
        self.assertIn("local", text)
        self.assertIn("shared", text)
        self.assertIn("run_local_worker", text)
        self.assertIn("DEPLOYMENT_MODE", text)


if __name__ == "__main__":
    unittest.main()
