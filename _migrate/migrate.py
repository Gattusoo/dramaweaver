"""一次性迁移脚本：shortplay/ → dvadmin/ + apps/。跑完即删。"""
import os
import shutil

ROOT = r"D:\pythonproject\ShortPlayGenerating"
SRC = os.path.join(ROOT, "shortplay")


def w(rel, content):
    """写文件（rel 相对 ROOT），自动建目录。"""
    p = os.path.join(ROOT, rel.replace("/", os.sep))
    os.makedirs(os.path.dirname(p), exist_ok=True)
    with open(p, "w", encoding="utf-8") as f:
        f.write(content)


def copy_file(src_rel, dst_rel):
    p = os.path.join(ROOT, src_rel.replace("/", os.sep))
    with open(p, "r", encoding="utf-8") as f:
        content = f.read()
    w(dst_rel, content)
    return content


def patch(text, replacements):
    for old, new in replacements:
        text = text.replace(old, new)
    return text


# ── 1. 共用工具原样搬到 dvadmin/utils ──
for name in ("media.py", "storage.py", "url_safety.py"):
    copy_file(f"shortplay/{name}", f"dvadmin/utils/{name}")

# ── 2. ai_gateway 子包 → apps/ai_gateway ──
# adapters/circuit_breaker/gateway/rate_limiter/redis_client：相对 import 不变
for name in ("__init__.py", "adapters.py", "circuit_breaker.py", "gateway.py",
             "rate_limiter.py", "redis_client.py"):
    copy_file(f"shortplay/ai_gateway/{name}", f"apps/ai_gateway/{name}")
# router.py: from ..models → from .models
router = open(os.path.join(SRC, "ai_gateway", "router.py"), encoding="utf-8").read()
w("apps/ai_gateway/router.py", router.replace("from ..models import", "from .models import"))
# cost_tracker.py: from ..models import CostRecord → from .models import CostRecord
ct = open(os.path.join(SRC, "ai_gateway", "cost_tracker.py"), encoding="utf-8").read()
w("apps/ai_gateway/cost_tracker.py", ct.replace("from ..models import CostRecord", "from .models import CostRecord"))

# ── 3. assistant 子包 → apps/creation/assistant ──
# models.py: 字符串外键 shortplay.X → creation.X
am = open(os.path.join(SRC, "assistant", "models.py"), encoding="utf-8").read()
am = am.replace('"shortplay.Play"', '"creation.Play"')
am = am.replace('"shortplay.AgentConfig"', '"creation.AgentConfig"')
am = am.replace('"shortplay.TaskRecord"', '"creation.TaskRecord"')
am = am.replace('"shortplay.Asset"', '"creation.Asset"')
am = am.replace('"shortplay.Episode"', '"creation.Episode"')
am = am.replace('"shortplay.Scene"', '"creation.Scene"')
am = am.replace('"shortplay.Shot"', '"creation.Shot"')
am = am.replace('"shortplay.Character"', '"creation.Character"')
w("apps/creation/assistant/models.py", am)
# memory.py / proposals.py / review.py / serializers.py / events.py：相对 import 不变
for name in ("__init__.py", "events.py", "memory.py", "proposals.py", "review.py", "serializers.py"):
    copy_file(f"shortplay/assistant/{name}", f"apps/creation/assistant/{name}")
# runner.py: from ..ai_gateway.gateway → from apps.ai_gateway.gateway
runner = open(os.path.join(SRC, "assistant", "runner.py"), encoding="utf-8").read()
w("apps/creation/assistant/runner.py",
  runner.replace("from ..ai_gateway.gateway import AIGateway",
                 "from apps.ai_gateway.gateway import AIGateway"))
# tools.py: from .. import media → dvadmin.utils; from ..distribution.analytics → apps.distribution
tools = open(os.path.join(SRC, "assistant", "tools.py"), encoding="utf-8").read()
tools = tools.replace("from .. import media", "from dvadmin.utils import media")
tools = tools.replace("from ..distribution.analytics import play_cost_breakdown",
                      "from apps.distribution.analytics import play_cost_breakdown")
w("apps/creation/assistant/tools.py", tools)
# views.py: UserRole 拆出；from ..ai_gateway.rate_limiter → apps.ai_gateway
av = open(os.path.join(SRC, "assistant", "views.py"), encoding="utf-8").read()
av = av.replace("from ..models import Play, TaskRecord, TaskStatus, TaskType, UserRole",
                "from ..models import Play, TaskRecord, TaskStatus, TaskType\nfrom dvadmin.system.models import UserRole")
av = av.replace("from ..ai_gateway.rate_limiter import RateLimiter",
                "from apps.ai_gateway.rate_limiter import RateLimiter")
w("apps/creation/assistant/views.py", av)

# ── 4. pipeline 子包 → apps/creation/pipeline ──
for name in ("__init__.py", "checkpoints.py", "redo.py", "state_machine.py"):
    copy_file(f"shortplay/pipeline/{name}", f"apps/creation/pipeline/{name}")
# dispatcher.py: from ..celery → from application.celery
disp = open(os.path.join(SRC, "pipeline", "dispatcher.py"), encoding="utf-8").read()
disp = disp.replace("from ..celery import app as celery_app",
                    "from application.celery import app as celery_app")
w("apps/creation/pipeline/dispatcher.py", disp)

# ── 5. distribution 子包 → apps/distribution ──
for name in ("__init__.py",):
    copy_file(f"shortplay/distribution/{name}", f"apps/distribution/{name}")
# analytics.py: 拆分混合 import
an = open(os.path.join(SRC, "distribution", "analytics.py"), encoding="utf-8").read()
an = an.replace(
    "from ..models import (\n    AIProvider, CostRecord, EpisodeStatus, Play, PlayStatus, Publication,\n    PublicationStatus,\n)",
    "from apps.ai_gateway.models import AIProvider, CostRecord\n"
    "from apps.creation.models import EpisodeStatus, Play, PlayStatus\n"
    "from .models import Publication, PublicationStatus")
w("apps/distribution/analytics.py", an)
# publication.py: Asset/AssetStatus/AssetType/Episode 来自 creation；其余本应用
pub = open(os.path.join(SRC, "distribution", "publication.py"), encoding="utf-8").read()
pub = pub.replace(
    "from ..models import (\n    Asset, AssetStatus, AssetType, DistributionChannel, Episode, Publication,\n    PublicationStatus,\n)",
    "from apps.creation.models import Asset, AssetStatus, AssetType, Episode\n"
    "from .models import DistributionChannel, Publication, PublicationStatus")
w("apps/distribution/publication.py", pub)
# rendition.py: from .. import media → dvadmin.utils; Publication 本应用; TaskType 来自 creation
ren = open(os.path.join(SRC, "distribution", "rendition.py"), encoding="utf-8").read()
ren = ren.replace("from .. import media as media_utils",
                  "from dvadmin.utils import media as media_utils")
ren = ren.replace("from ..models import Publication, TaskType",
                  "from .models import Publication\nfrom apps.creation.models import TaskType")
ren = ren.replace("from ..pipeline.dispatcher import dispatch_task",
                  "from apps.creation.pipeline.dispatcher import dispatch_task")
w("apps/distribution/rendition.py", ren)
# subtitles.py: Asset/AssetStatus/AssetType/Shot 来自 creation；Subtitle* 本应用
sub = open(os.path.join(SRC, "distribution", "subtitles.py"), encoding="utf-8").read()
sub = sub.replace(
    "from ..models import (\n    Asset, AssetStatus, AssetType, Shot, SubtitleSourceType, SubtitleStatus,\n    SubtitleTrack,\n)",
    "from apps.creation.models import Asset, AssetStatus, AssetType, Shot\n"
    "from .models import SubtitleSourceType, SubtitleStatus, SubtitleTrack")
w("apps/distribution/subtitles.py", sub)

# ── 6. tasks.py → apps/creation/tasks.py ──
tasks = open(os.path.join(SRC, "tasks.py"), encoding="utf-8").read()
tasks = tasks.replace("from . import media as media_utils",
                      "from dvadmin.utils import media as media_utils")
# 拆分 models import：分布类移到 apps.distribution.models
tasks = tasks.replace(
    "from .models import (\n    Asset, AssetDependency, AssetLibrary, AssetStatus, AssetType, Character, Episode,\n    EpisodeStatus, PlayStatus, Publication, RelationType, Scene, Shot, SubtitleSourceType,\n    SubtitleStatus, SubtitleTrack, TaskRecord, TaskStatus,\n)",
    "from .models import (\n    Asset, AssetDependency, AssetLibrary, AssetStatus, AssetType, Character, Episode,\n    EpisodeStatus, PlayStatus, RelationType, Scene, Shot, TaskRecord, TaskStatus,\n)\n"
    "from apps.distribution.models import Publication, SubtitleSourceType, SubtitleStatus, SubtitleTrack")
tasks = tasks.replace("from .ai_gateway.gateway import AIGateway",
                      "from apps.ai_gateway.gateway import AIGateway")
tasks = tasks.replace("from .storage import save_artifact",
                      "from dvadmin.utils.storage import save_artifact")
tasks = tasks.replace("from .url_safety import check_url",
                      "from dvadmin.utils.url_safety import check_url")
tasks = tasks.replace("from .distribution import rendition as rd",
                      "from apps.distribution import rendition as rd")
tasks = tasks.replace("from .distribution.subtitles import generate_subtitle_track",
                      "from apps.distribution.subtitles import generate_subtitle_track")
tasks = tasks.replace("from .distribution.subtitles import translate_subtitle_track",
                      "from apps.distribution.subtitles import translate_subtitle_track")
tasks = tasks.replace("from .models import DistributionChannel",
                      "from apps.distribution.models import DistributionChannel")
tasks = tasks.replace("from .distribution.rendition import RenditionBusy, ensure_rendition",
                      "from apps.distribution.rendition import RenditionBusy, ensure_rendition")
# .pipeline / .assistant / .models(creation 符号) 保持不变
w("apps/creation/tasks.py", tasks)

print("bulk migration done")
