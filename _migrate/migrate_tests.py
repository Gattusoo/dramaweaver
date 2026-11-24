"""搬迁测试文件到新 apps，改写 import 与 patch/assertLogs 路径。"""
import os
import re

ROOT = r"D:\pythonproject\ShortPlayGenerating"
SRC = os.path.join(ROOT, "shortplay")

# 符号 → 应用 models 模块
SYMBOL_APP = {
    # system
    "CustomUser": "dvadmin.system.models", "UserRole": "dvadmin.system.models",
    # ai_gateway
    "AIProvider": "apps.ai_gateway.models", "AIModel": "apps.ai_gateway.models",
    "CostRecord": "apps.ai_gateway.models", "ProviderType": "apps.ai_gateway.models",
    "ModelType": "apps.ai_gateway.models", "CostCurrency": "apps.ai_gateway.models",
    # distribution
    "SubtitleSourceType": "apps.distribution.models", "SubtitleStatus": "apps.distribution.models",
    "SubtitleTrack": "apps.distribution.models", "PublicationStatus": "apps.distribution.models",
    "DistributionChannel": "apps.distribution.models", "Publication": "apps.distribution.models",
}
# 其余creation符号默认 apps.creation.models
CREATION_SYMBOLS = {
    "PlayStatus","AuditMode","RedoGranularity","ConsistencyMode","Play","EpisodeStatus",
    "Episode","SceneStatus","Scene","ShotStatus","Shot","TimeOfDay","ShotType","CharacterRole",
    "Gender","Character","VoiceProfile","AssetType","AssetStatus","RelationType","Asset",
    "AssetDependency","AssetLibrary","AgentConfig","TaskType","TaskStatus","TaskRecord",
    "ReviewPoint","ReviewDecision","ReviewLog","AssistantMemory","AssistantMessage",
    "AssistantProposal","AssistantSession",
}

# 测试文件 → 目标 tests 目录
ASSIGN = {
    "test_assistant.py": "apps/creation/tests",
    "test_assistant_prereqs.py": "apps/creation/tests",
    "test_assistant_toolset.py": "apps/creation/tests",
    "test_derivatives_style.py": "apps/creation/tests",
    "test_ffmpeg_quality.py": "apps/creation/tests",
    "test_media.py": "apps/creation/tests",
    "test_pipeline.py": "apps/creation/tests",
    "test_production.py": "apps/creation/tests",
    "test_rendition.py": "apps/creation/tests",
    "test_review_backlog.py": "apps/creation/tests",
    "test_review_fixes.py": "apps/creation/tests",
    "test_rbac.py": "apps/creation/tests",
    "tests.py": "apps/creation/tests",
    "test_ai_gateway.py": "apps/ai_gateway/tests",
    "test_redis_store.py": "apps/ai_gateway/tests",
    "test_distribution.py": "apps/distribution/tests",
}


def route_symbol(sym):
    sym = sym.strip().rstrip(",")
    if sym in SYMBOL_APP:
        return SYMBOL_APP[sym]
    if sym in CREATION_SYMBOLS:
        return "apps.creation.models"
    # 未知符号默认 creation（ conservative）
    return "apps.creation.models"


def rewrite_models_import(text):
    """拆分 from .models import (...) 多行块。"""
    # 匹配单行与多行两种
    pat = re.compile(r"from \.models import \(\n(.*?)\n\)", re.S)
    def repl(m):
        syms = [s.strip() for s in m.group(1).split(",") if s.strip()]
        buckets = {}
        for s in syms:
            app = route_symbol(s)
            buckets.setdefault(app, []).append(s)
        lines = []
        for app, names in buckets.items():
            lines.append(f"from {app} import (\n    " + ", ".join(names) + ",\n)")
        return "\n".join(lines)
    text = pat.sub(repl, text)
    # 单行 from .models import A, B
    text = re.sub(r"from \.models import ([^\n(]+)", lambda m: split_single(m), text)
    return text


def split_single(m):
    syms = [s.strip() for s in m.group(1).split(",") if s.strip()]
    buckets = {}
    for s in syms:
        app = route_symbol(s)
        buckets.setdefault(app, []).append(s)
    return "\n".join(f"from {app} import {', '.join(names)}" for app, names in buckets.items())


# 普通相对 import 替换
REL_REPLACES = [
    ("from .ai_gateway.", "from apps.ai_gateway."),
    ("from .assistant.", "from apps.creation.assistant."),
    ("from .assistant import ", "from apps.creation.assistant import "),
    ("from .distribution.", "from apps.distribution."),
    ("from .distribution import ", "from apps.distribution import "),
    ("from .pipeline.", "from apps.creation.pipeline."),
    ("from .pipeline import ", "from apps.creation.pipeline import "),
    ("from .url_safety import ", "from dvadmin.utils.url_safety import "),
    ("from .tasks import ", "from apps.creation.tasks import "),
]
# from . import media / tasks / media, tasks
def rewrite_rel_imports(text):
    for old, new in REL_REPLACES:
        text = text.replace(old, new)
    # from . import media, tasks
    text = text.replace("from . import media, tasks",
                        "from dvadmin.utils import media\nfrom apps.creation import tasks")
    text = text.replace("from . import media", "from dvadmin.utils import media")
    text = text.replace("from . import tasks", "from apps.creation import tasks")
    return text


# patch / assertLogs 路径替换（保留任务名字符串）
PATCH_REPLACES = [
    ("shortplay.pipeline.dispatcher.send_task", "apps.creation.pipeline.dispatcher.send_task"),
    ("shortplay.pipeline.dispatcher.dispatch_task", "apps.creation.pipeline.dispatcher.dispatch_task"),
    ("shortplay.pipeline.dispatcher._after_video_ready", "apps.creation.pipeline.dispatcher._after_video_ready"),
    ("shortplay.tasks._gateway", "apps.creation.tasks._gateway"),
    ("shortplay.tasks._download", "apps.creation.tasks._download"),
    ("shortplay.tasks.run_ffmpeg", "apps.creation.tasks.run_ffmpeg"),
    ("shortplay.tasks._probe_duration", "apps.creation.tasks._probe_duration"),
    ("shortplay.tasks._probe_media", "apps.creation.tasks._probe_media"),
    ("shortplay.tasks._decode_check", "apps.creation.tasks._decode_check"),
    ("shortplay.tasks._build_hls", "apps.creation.tasks._build_hls"),
    ("shortplay.tasks._build_gif", "apps.creation.tasks._build_gif"),
    ("shortplay.tasks._video_segment", "apps.creation.tasks._video_segment"),
    ("shortplay.tasks._style", "apps.creation.tasks._style"),
    ("shortplay.tasks._config", "apps.creation.tasks._config"),
    ("shortplay.tasks._COVER", "apps.creation.tasks._COVER"),
    ("shortplay.tasks.W", "apps.creation.tasks.W"),
    ("shortplay.tasks.H", "apps.creation.tasks.H"),
    ('assertLogs("shortplay.tasks"', 'assertLogs("apps.creation.tasks"'),
    ("shortplay.storage.save_artifact", "dvadmin.utils.storage.save_artifact"),
    ("shortplay.media.rendition_filter", "dvadmin.utils.media.rendition_filter"),
    ("shortplay.assistant.runner._gateway", "apps.creation.assistant.runner._gateway"),
    ('assertLogs("shortplay.assistant.events"', 'assertLogs("apps.creation.assistant.events"'),
    ('assertLogs("shortplay.assistant.runner"', 'assertLogs("apps.creation.assistant.runner"'),
    ("shortplay.ai_gateway.adapters.httpx.Client", "apps.ai_gateway.adapters.httpx.Client"),
    ("shortplay.ai_gateway.gateway.time.sleep", "apps.ai_gateway.gateway.time.sleep"),
    ('assertLogs("shortplay.ai_gateway.gateway"', 'assertLogs("apps.ai_gateway.gateway"'),
]


def rewrite_patches(text):
    for old, new in PATCH_REPLACES:
        text = text.replace(old, new)
    return text


count = 0
for fn, dst in ASSIGN.items():
    src = os.path.join(SRC, fn)
    if not os.path.exists(src):
        print("MISSING", fn)
        continue
    text = open(src, encoding="utf-8").read()
    text = rewrite_rel_imports(text)
    text = rewrite_models_import(text)
    text = rewrite_patches(text)
    os.makedirs(os.path.join(ROOT, dst.replace("/", os.sep)), exist_ok=True)
    out = os.path.join(ROOT, dst.replace("/", os.sep), fn)
    with open(out, "w", encoding="utf-8") as f:
        f.write(text)
    count += 1
    print("moved", fn, "->", dst)
print("total", count)
