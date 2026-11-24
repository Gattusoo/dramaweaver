"""运行配置加载层。

优先级（高 → 低）：
1. 环境变量
2. 项目内本地配置 ``conf/env.py``（不入库）
3. ``conf/env.example.py`` 中的开发默认值

各配置项的实际读取（带环境变量覆盖）在 ``application/settings.py`` 中完成；
本包只负责把本地配置或模板暴露为 ``env`` 模块对象。

模板回退按文件路径加载（不依赖模块名）：历史上曾出现过「代码 import 的
模板模块名与实际模板文件名不一致」的缺陷（conf.env_example vs env.example.py），
全新检出的仓库无法启动。此处对文件名变化保持健壮。
"""
import importlib
import importlib.util
from pathlib import Path

_package_dir = Path(__file__).resolve().parent
_template_candidates = ("env.example.py", "env_example.py", "env.example")

env = None  # type: ignore[assignment]

try:  # 本地配置（conf/env.py，不入库）
    env = importlib.import_module("conf.env")
except ImportError:
    env = None

if env is None:
    # 模板回退：按候选文件名逐个尝试（文件路径加载，模块名不再敏感）
    for _name in _template_candidates:
        _path = _package_dir / _name
        if _path.is_file():
            _spec = importlib.util.spec_from_file_location("conf.env_template", _path)
            if _spec is not None and _spec.loader is not None:
                _mod = importlib.util.module_from_spec(_spec)
                _spec.loader.exec_module(_mod)
                env = _mod
                break

if env is None:
    raise ImportError(
        "conf/env.py 缺失且未找到配置模板（conf/env.example.py）。"
        "请复制模板为 conf/env.py 并按需填写。"
    )

__all__ = ("env",)
