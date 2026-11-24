"""配置加载测试（platform/configuration）。

验证：环境变量覆盖本地配置；DEBUG 关闭且无 SECRET_KEY 时启动失败。
"""
import importlib
import os
import sys
import unittest
from unittest import mock


def _reload_settings():
    """重新加载 application.settings，使配置变更生效。"""
    import django
    from django.conf import settings as django_settings

    # 清理已加载的 settings 缓存与子模块
    for name in list(sys.modules):
        if name == "application.settings" or name.startswith("application.settings."):
            del sys.modules[name]
    # 重置 django settings 包装
    type(django_settings)._wrapped = None
    importlib.import_module("application.settings")
    django_settings._setup()


class ConfigLoadingTests(unittest.TestCase):
    def setUp(self):
        # 保留原始环境，便于恢复
        self._env_backup = dict(os.environ)
        self._sys_modules_keys = set(sys.modules.keys())

    def tearDown(self):
        os.environ.clear()
        os.environ.update(self._env_backup)
        # 恢复 settings 为本机默认（DEBUG=True）
        _reload_settings()

    def test_env_var_overrides_local_config(self):
        """环境变量优先于本地 conf/env.py。"""
        os.environ["DATABASE_NAME"] = "env_override.sqlite3"
        _reload_settings()
        from django.conf import settings
        self.assertIn("env_override.sqlite3", str(settings.DATABASES["default"]["NAME"]))

    def test_production_without_secret_key_fails(self):
        """DEBUG=False 且无 SECRET_KEY 时拒绝启动。"""
        os.environ.pop("SECRET_KEY", None)
        os.environ["DEBUG"] = "false"
        # 让 _env 读不到 SECRET_KEY：覆盖 conf.env 模块的属性
        import conf
        conf.env.SECRET_KEY = ""
        with self.assertRaises(Exception) as ctx:
            _reload_settings()
        self.assertIn("SECRET_KEY", str(ctx.exception))

    def test_template_fallback_without_local_config(self):
        """5.1 全新检出（只有 conf/__init__.py + conf/env.example.py）也能 import conf。

        历史上模板回退依赖模块名 ``conf.env_example``，与真实文件名 ``env.example.py``
        不一致——全新检出直接 ImportError。此处用隔离副本复现该场景（子进程，
        避免污染当前进程的 conf 模块缓存）。
        """
        import shutil
        import subprocess
        import tempfile
        from pathlib import Path

        repo = Path(__file__).resolve().parents[4]
        with tempfile.TemporaryDirectory() as tmp:
            pkg = Path(tmp) / "conf"
            pkg.mkdir()
            shutil.copy(repo / "conf" / "__init__.py", pkg / "__init__.py")
            shutil.copy(repo / "conf" / "env.example.py", pkg / "env.example.py")
            self.assertFalse((pkg / "env.py").exists())
            proc = subprocess.run(
                [sys.executable, "-c",
                 "import conf; print(getattr(conf.env, 'DATABASE_NAME', ''))"],
                cwd=tmp, capture_output=True, text=True, timeout=60,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            self.assertIn("db.sqlite3", proc.stdout)


class FeatureFlagWiringTests(unittest.TestCase):
    """5.2 配置层与消费方一致：代码读取的开关必须在 settings 中定义。

    反向缺陷：开关有消费方（getattr(settings, "X", ...)）却未在配置层声明时，
    运维无法通过环境变量启用/关闭它——开关形同不存在。
    """

    def test_every_flag_read_by_code_is_defined_in_settings(self):
        import re
        from pathlib import Path

        from django.conf import settings

        repo = Path(__file__).resolve().parents[4]
        read_re = re.compile(r'getattr\(\s*settings\s*,\s*["\'](ENABLE_[A-Z_]+)["\']')
        used = set()
        for path in list((repo / "apps").rglob("*.py")):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            # 去掉整行注释，避免文档性提及被当成消费方
            code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
            used |= set(read_re.findall(code))
        missing = sorted(name for name in used if not hasattr(settings, name))
        self.assertEqual(missing, [], f"以下开关被代码读取但未在 settings 定义：{missing}")

    def test_removed_flags_not_read_by_code(self):
        """被删除的开关不得残留消费方（否则删除即功能静默失效）。"""
        import re
        from pathlib import Path

        from django.conf import settings

        repo = Path(__file__).resolve().parents[4]
        removed = [
            "ENABLE_WORKING_MEMORY", "ENABLE_EPISODIC_MEMORY", "ENABLE_SEMANTIC_MEMORY",
            "ENABLE_PLAN_AND_SOLVE", "ENABLE_AB_TESTING", "ENABLE_CONTEXT_MANAGEMENT",
            "ENABLE_SMART_COMPRESSION", "ENABLE_TOOL_ECOSYSTEM", "ENABLE_TOOL_MARKETPLACE",
            "ENABLE_REALTIME_COLLABORATION",
            "ENABLE_COST_OPTIMIZATION", "ENABLE_DYNAMIC_MODEL_SELECTION",
            # ENABLE_SMART_CACHE 曾为规划期占位开关被删除（2026-11-20 前后）；
            # 2026-11-22 成本优化变更真实现 SmartCache 后解禁（有定义有消费方）。
            # ENABLE_PROGRESSIVE_AUTONOMY 同期由信任分级与渐进自主变更真实现后解禁。
        ]
        read_re = re.compile(r'getattr\(\s*settings\s*,\s*["\'](ENABLE_[A-Z_]+)["\']')
        offenders = []
        for path in list((repo / "apps").rglob("*.py")):
            if "tests" in path.parts or "__pycache__" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            code = "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))
            offenders += [f"{path.name}:{n}" for n in read_re.findall(code) if n in removed]
        self.assertEqual(offenders, [])
        for name in removed:
            self.assertFalse(hasattr(settings, name), f"{name} 应已从配置层删除")


if __name__ == "__main__":
    unittest.main()