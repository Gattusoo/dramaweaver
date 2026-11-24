"""Django settings for the application project.

配置加载（design D5 / platform/configuration）：
优先级（高 → 低）：环境变量 → 本地 ``conf/env.py`` → ``conf/env.example.py`` 默认值。
代码中不硬编码生产密钥；DEBUG 默认关闭，DEBUG 关闭且无 SECRET_KEY 时拒绝启动。
"""
import os
import secrets
import warnings
from pathlib import Path

from django.core.exceptions import ImproperlyConfigured

from conf import env

BASE_DIR = Path(__file__).resolve().parent.parent


def _env(name, default=None):
    """读取配置项：环境变量优先，其次本地/模板默认值。

    显式 None（conf/env.py 或模板里写 X = None 表示"未配置"）视同缺省，
    回落到默认值——与模板留空字符串语义一致。"""
    val = os.environ.get(name)
    if val is not None and val != "":
        return val
    val = getattr(env, name, None)
    if val is not None:
        return val
    return default


# ─── 部署模式（2026-11-18 工作台，platform/local-runtime & configuration）─────
# DEPLOYMENT_MODE=local|shared，缺省 shared 保持既有部署兼容：
#   shared —— 现有数据库/Celery/Redis/认证/对象存储全部不变（默认）。
#   local  —— 个人创作工作台：SQLite + 本地数据目录 + 数据库持久化任务队列 +
#             单逻辑 worker（run_local_worker）；不要求 Redis/Celery broker。
# 模式只切换"运行适配层"（任务传输/存储布局/监听边界），创作业务内核不变。
# 本节做配置解析与启动安全检查（回环绑定/持久密钥），执行器/队列在 Stage C 接入。

DEPLOYMENT_MODE = (_env("DEPLOYMENT_MODE", "shared") or "shared").strip().lower()
if DEPLOYMENT_MODE not in ("local", "shared"):
    raise ImproperlyConfigured(
        f"DEPLOYMENT_MODE 仅支持 local/shared，当前值：{DEPLOYMENT_MODE}"
    )
IS_LOCAL_MODE = DEPLOYMENT_MODE == "local"

# 任务传输后端：celery|local。缺省按部署模式选择（shared→celery，local→local）。
# Stage A 仅定义与解析，dispatcher 侧的 local 分支在 Stage C（任务 6.2）接入。
_creations_backend_default = "local" if IS_LOCAL_MODE else "celery"
CREATION_TASK_BACKEND = (_env("CREATION_TASK_BACKEND", _creations_backend_default)
                         or _creations_backend_default).strip().lower()
if CREATION_TASK_BACKEND not in ("celery", "local"):
    raise ImproperlyConfigured(
        f"CREATION_TASK_BACKEND 仅支持 celery/local，当前值：{CREATION_TASK_BACKEND}"
    )

# 本地数据目录（仅 local 模式消费；shared 保持 MEDIA_ROOT/db.sqlite3 原布局）。
# 项目清单/内容寻址资产/导出/缓存目录在 Stage B 由 workspace 服务按此根展开。
LOCAL_DATA_DIR = _env("LOCAL_DATA_DIR", "")


# ─── 密钥与调试开关 ────────────────────────────────────────────────────────

DEBUG = _env("DEBUG", False)
if isinstance(DEBUG, str):
    DEBUG = DEBUG.strip().lower() in ("1", "true", "yes", "on")

SECRET_KEY = _env("SECRET_KEY", "") or ""
if not SECRET_KEY:
    if DEBUG:
        # local 模式（DEBUG 开发默认）：密钥持久化到 LOCAL_DATA_DIR/.secret_key，
        # 重启不换密钥（JWT/session/CSRF 不失效）——本地工作台会频繁启停。
        # 写入失败（只读目录等）回退进程内临时密钥并告警，不阻断启动。
        if IS_LOCAL_MODE:
            try:
                _secret_file = Path(LOCAL_DATA_DIR).expanduser() / ".secret_key"
                if _secret_file.is_file():
                    _persisted = _secret_file.read_text(encoding="utf-8").strip()
                    if _persisted:
                        SECRET_KEY = _persisted
                if not SECRET_KEY:
                    _secret_file.parent.mkdir(parents=True, exist_ok=True)
                    SECRET_KEY = secrets.token_urlsafe(50)
                    _secret_file.write_text(SECRET_KEY, encoding="utf-8")
            except OSError as _exc:
                SECRET_KEY = secrets.token_urlsafe(50)
                warnings.warn(
                    f"DEPLOYMENT_MODE=local 持久化 SECRET_KEY 到 LOCAL_DATA_DIR 失败"
                    f"（{_exc}），已回退进程内临时密钥。",
                    RuntimeWarning,
                )
        if not SECRET_KEY:
            SECRET_KEY = secrets.token_urlsafe(50)
            warnings.warn(
                "DEBUG=True 且未配置 SECRET_KEY，已生成进程内临时密钥；"
                "生产环境必须在 conf/env.py 或环境变量中设置 SECRET_KEY。",
                RuntimeWarning,
            )
    else:
        raise ImproperlyConfigured(
            "生产环境（DEBUG=False）必须在 conf/env.py 或环境变量 SECRET_KEY 中设置密钥。"
        )

ALLOWED_HOSTS = _env("ALLOWED_HOSTS", [])
if isinstance(ALLOWED_HOSTS, str):
    ALLOWED_HOSTS = [h.strip() for h in ALLOWED_HOSTS.split(",") if h.strip()]

# local 模式的启动安全检查（spec: platform/local-runtime「本机访问边界」）：
# 1. 必须显式提供本地数据目录（SQLite/资产/清单都落在里面，缺省无静默回退）；
# 2. 只允许回环监听（ALLOWED_HOSTS ⊆ 127.0.0.1/::1/localhost），0.0.0.0/局域网/
#    公网绑定直接拒绝启动，并提示改用 shared 模式配置认证与访问控制；
# 3. shared 模式不读取本节任何快捷身份配置，鉴权行为不变。
if IS_LOCAL_MODE:
    if not (LOCAL_DATA_DIR or "").strip():
        raise ImproperlyConfigured(
            "DEPLOYMENT_MODE=local 必须配置 LOCAL_DATA_DIR（本地数据目录，"
            "含 SQLite/资产/项目清单），缺省无回退。"
        )
    _loopback_hosts = {"127.0.0.1", "localhost", "::1"}
    bad_hosts = [h for h in ALLOWED_HOSTS if h not in _loopback_hosts]
    if bad_hosts:
        raise ImproperlyConfigured(
            f"DEPLOYMENT_MODE=local 只允许回环监听（127.0.0.1/::1/localhost），"
            f"当前 ALLOWED_HOSTS 含非回环地址：{bad_hosts}。"
            "需要局域网/公网访问请改用 DEPLOYMENT_MODE=shared 并配置认证与访问控制。"
        )
    # local 默认回环监听（ALLOWED_HOSTS 为空时补回环，显式配置了回环则保持）
    if not ALLOWED_HOSTS:
        ALLOWED_HOSTS = ["127.0.0.1", "::1"]


# ─── 用户模型与认证 ────────────────────────────────────────────────────────

AUTH_USER_MODEL = "system.CustomUser"

from datetime import timedelta  # noqa: E402

SIMPLE_JWT = {
    "ACCESS_TOKEN_LIFETIME": timedelta(hours=12),
    "REFRESH_TOKEN_LIFETIME": timedelta(days=7),
    "ROTATE_REFRESH_TOKENS": False,
    "BLACKLIST_AFTER_ROTATION": True,
    "UPDATE_LAST_LOGIN": True,
    "AUTH_HEADER_TYPES": ("Bearer",),
}

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": (
        "rest_framework_simplejwt.authentication.JWTAuthentication",
        "rest_framework.authentication.SessionAuthentication",
    ),
    "DEFAULT_PERMISSION_CLASSES": (
        "rest_framework.permissions.IsAuthenticated",
    ),
    "DEFAULT_RENDERER_CLASSES": (
        "dvadmin.utils.renderers.CustomJSONRenderer",
    ),
    "DEFAULT_PAGINATION_CLASS": "dvadmin.utils.pagination.CustomPagination",
    "PAGE_SIZE": 20,
    "EXCEPTION_HANDLER": "dvadmin.utils.exception.custom_exception_handler",
}


# ─── Application definition ────────────────────────────────────────────────

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "rest_framework",
    "rest_framework_simplejwt.token_blacklist",
    "dvadmin.system",
    "channels",  # Phase 3.2：仅 protocol router（不用 layer/worker）
    "apps.ai_gateway",
    "apps.creation",
    "apps.billing",
    "apps.distribution",
    "apps.public_api",
]

MIDDLEWARE = [
    "dvadmin.utils.middleware.RequestIDMiddleware",
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "application.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "application.wsgi.application"
ASGI_APPLICATION = "application.asgi.application"


# ─── Redis / Cache / Celery ────────────────────────────────────────────────

REDIS_URL = _env("REDIS_URL", "")
_HAS_REDIS = bool(REDIS_URL)

AI_GATEWAY_STORE = _env("AI_GATEWAY_STORE", "redis" if _HAS_REDIS else "memory")
if AI_GATEWAY_STORE == "auto":
    AI_GATEWAY_STORE = "redis" if _HAS_REDIS else "memory"

# 供应商并发限流租约时长（秒）：到租约期未释放的槽位由后续 acquire 自动回收，
# 兜底进程崩溃泄漏（billing 变更后限流槽位租约化）
RATE_LIMIT_LEASE_SECONDS = int(_env("RATE_LIMIT_LEASE_SECONDS", "600") or 600)

# provider base_url SSRF 防护：默认拒绝内网/回环地址（仅公网 API 供应商）。
# 本地模型（Ollama / ComfyUI / vLLM）部署时设 true 放行；链路本地/云 metadata 始终拒绝。
AI_GATEWAY_ALLOW_LOCAL_BASE_URL = _env(
    "AI_GATEWAY_ALLOW_LOCAL_BASE_URL", "false").lower() in ("1", "true", "yes", "on")

# 服务费埋点（决策 18，分/次）：用户自带凭证调用产生的平台服务费单价。
# 默认 0 关闭；属主凭证路由（custom provider + UserProviderCredential）的每次成功
# 调用会 emit `service_fee` 事件（amount=配置值），由 billing 变更消费落流水。
# 此处只埋事件，不做扣费/结算。
SERVICE_FEE_PER_CALL = int(_env("SERVICE_FEE_PER_CALL", "0") or 0)

# ─── 计费与限额配置族（决策 7/8/16/17/18；billing 变更）──────────────────
# 注册冷启动赠金（分，默认 1000 = ¥10；0 = 关闭）
REGISTER_GRANT_POINTS = int(_env("REGISTER_GRANT_POINTS", "1000") or 0)
# 单任务结算额 ≥ 此值时向属主推大额消费告警事件（分；0 = 关闭）
OWNER_ALERT_THRESHOLD_POINTS = int(_env("OWNER_ALERT_THRESHOLD_POINTS", "5000") or 0)
# 资源限额全局默认（决策 7：渲染 2 / AI 10；UserQuotaOverride 可按用户覆盖）
MAX_CONCURRENT_RENDER = int(_env("MAX_CONCURRENT_RENDER", "2") or 2)
MAX_CONCURRENT_AI = int(_env("MAX_CONCURRENT_AI", "10") or 10)
# 每日渲染时长上限（秒，按属主累计）
DAILY_RENDER_SECONDS_LIMIT = int(_env("DAILY_RENDER_SECONDS_LIMIT", "3600") or 3600)
# 预扣闸应急开关（默认开；关闭=跳过预扣直接派发，用于事故止血）
ENABLE_BILLING_HOLD = _env("ENABLE_BILLING_HOLD", "true").lower() in ("1", "true", "yes", "on")
# 工作台 Stage C1（任务 6.5）：local 模式禁用 billing 调度路径（无预扣/钱包副作用）
if IS_LOCAL_MODE:
    ENABLE_BILLING_HOLD = False
# 降级链跨路线回退开关（决策 9：平台→我的渠道方向默认拒绝；反方向默认允许）
ENABLE_CROSS_ROUTE_FALLBACK = _env("ENABLE_CROSS_ROUTE_FALLBACK", "false").lower() in ("1", "true", "yes", "on")

CACHES = {
    "default": (
        {"BACKEND": "django.core.cache.backends.redis.RedisCache", "LOCATION": REDIS_URL}
        if _HAS_REDIS else
        {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "default"}
    )
}

CELERY_BROKER_URL = _env("CELERY_BROKER_URL", REDIS_URL if _HAS_REDIS else "memory://")
CELERY_RESULT_BACKEND = _env("CELERY_RESULT_BACKEND", REDIS_URL if _HAS_REDIS else "cache+memory://")
CELERY_TASK_ALWAYS_EAGER = (_env("CELERY_TASK_ALWAYS_EAGER", "0") or "0") == "1"

# 内存 broker（未配置 Redis 且非 eager）只在单进程内有效：另起独立 worker 时两个进程
# 各持一份队列，任务永远不会被消费且无任何报错。启动时就明确告知，避免静默停摆。
if not _HAS_REDIS and not CELERY_BROKER_URL.startswith(("redis", "rediss", "amqp")) \
        and not CELERY_TASK_ALWAYS_EAGER:
    warnings.warn(
        "CELERY_BROKER_URL 使用内存 broker（未配置 REDIS_URL）且 CELERY_TASK_ALWAYS_EAGER=0："
        "任务只会在当前进程内执行，独立 celery worker 收不到任何任务。"
        "生产/多进程部署请配置 REDIS_URL 或 CELERY_BROKER_URL。",
        stacklevel=1,
    )

# worker 被 hard time_limit / OOM / 重启杀死时，任务消息重投后会被重新执行
# （TaskRecord 仍是 RUNNING，_mark_running 允许复活）；无此设置消息已提前 ack 不会重投，
# 该集将永久停在 RUNNING 且下游派发被进行中判定阻塞。
CELERY_TASK_ACKS_LATE = True
CELERY_TASK_REJECT_ON_WORKER_LOST = True
CELERY_TASK_EAGER_PROPAGATES = True
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = "UTC"


# ─── Database ──────────────────────────────────────────────────────────────

# local 模式且未显式配置 DATABASE_NAME 时，SQLite 默认落在 LOCAL_DATA_DIR
# （整个工作台的数据单目录自包含，备份/迁移=拷目录）；显式配置时尊重用户选择
# （相对路径按 BASE_DIR 解析，与 shared 既有语义一致）。shared 默认布局不变。
if IS_LOCAL_MODE and not (_env("DATABASE_NAME", "") or "").strip():
    DATABASES = {
        "default": {
            "ENGINE": _env("DATABASE_ENGINE", "django.db.backends.sqlite3"),
            "NAME": Path(LOCAL_DATA_DIR).expanduser() / "db.sqlite3",
        }
    }
else:
    DATABASES = {
        "default": {
            "ENGINE": _env("DATABASE_ENGINE", "django.db.backends.sqlite3"),
            "NAME": BASE_DIR / _env("DATABASE_NAME", "db.sqlite3"),
        }
    }


# ─── Password validation ───────────────────────────────────────────────────

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]


# ─── Internationalization ──────────────────────────────────────────────────

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True


# ─── Static / Media ────────────────────────────────────────────────────────

STATIC_URL = "static/"
MEDIA_ROOT = BASE_DIR / "media"
MEDIA_URL = "/media/"


# ─── FFmpeg / 字体（自动查找，env 可显式指定）─────────────────────────────

def _resolve_ffmpeg_bin():
    explicit = (_env("FFMPEG_BIN", "") or "").strip()
    if explicit:
        return explicit
    import shutil

    found = shutil.which("ffmpeg") or shutil.which("ffmpeg.exe")
    if found:
        return found
    home = os.path.expanduser("~")
    search_roots = [
        r"D:\ffmpeg", r"C:\ffmpeg", r"C:\Program Files\ffmpeg",
        os.path.join(home, "ffmpeg"), os.path.join(home, "Downloads"),
        os.path.join(home, "scoop", "apps"), os.path.join(home, "AppData", "Local", "Microsoft", "WinGet"),
    ]
    for root_dir in search_roots:
        if not os.path.isdir(root_dir):
            continue
        for current, dirs, files in os.walk(root_dir):
            if "ffmpeg.exe" in files:
                return os.path.join(current, "ffmpeg.exe")
            depth = current[len(root_dir):].count(os.sep)
            if depth >= 3:
                dirs[:] = []
    return "ffmpeg"


FFMPEG_BIN = _resolve_ffmpeg_bin()


def _resolve_ffprobe_bin():
    explicit = (_env("FFPROBE_BIN", "") or "").strip()
    if explicit:
        return explicit
    import shutil

    found = shutil.which("ffprobe") or shutil.which("ffprobe.exe")
    if found:
        return found
    ff_dir = os.path.dirname(FFMPEG_BIN)
    if ff_dir:
        candidate = os.path.join(ff_dir, "ffprobe.exe" if os.name == "nt" else "ffprobe")
        if os.path.exists(candidate):
            return candidate
    return "ffprobe"


FFPROBE_BIN = _resolve_ffprobe_bin()


def _resolve_font_dir():
    candidates = [
        r"C:\Windows\Fonts",
        "/usr/share/fonts",
        "/usr/local/share/fonts",
        os.path.expanduser("~/.fonts"),
    ]
    for cand in candidates:
        if os.path.isdir(cand):
            return cand
    return ""


FONT_DIR = _resolve_font_dir()


def _resolve_font_file():
    explicit = (_env("SUBTITLE_FONT", "") or "").strip()
    if explicit and os.path.exists(explicit):
        return explicit
    candidates = [
        "C:/Windows/Fonts/msyh.ttc",
        "C:/Windows/Fonts/simhei.ttf",
        "C:/Windows/Fonts/simsun.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
    ]
    for cand in candidates:
        if os.path.exists(cand):
            return cand
    return ""


SUBTITLE_FONT_FILE = _resolve_font_file()


# ─── 对象存储 / SSRF 白名单 ────────────────────────────────────────────────

STORAGE_BACKEND = _env("STORAGE_BACKEND", "local")
STORAGE_OSS_ACCESS_KEY = _env("STORAGE_OSS_ACCESS_KEY", "")
STORAGE_OSS_SECRET_KEY = _env("STORAGE_OSS_SECRET_KEY", "")
STORAGE_OSS_BUCKET = _env("STORAGE_OSS_BUCKET", "")
STORAGE_OSS_ENDPOINT = _env("STORAGE_OSS_ENDPOINT", "")

SAFE_FETCH_ALLOWED_HOSTS = [
    h for h in (_env("SAFE_FETCH_ALLOWED_HOSTS", "") or "").split(",") if h.strip()
]
if STORAGE_OSS_ENDPOINT:
    from urllib.parse import urlparse
    host = urlparse(STORAGE_OSS_ENDPOINT).hostname
    if host and host not in SAFE_FETCH_ALLOWED_HOSTS:
        SAFE_FETCH_ALLOWED_HOSTS.append(host)


# ─── Agent成熟度升级功能开关（P0/P1/P2灰度发布）────────────────────────────
# 每个能力维度独立开关，支持逐步启用和快速回滚。
# Phase 0 max 审查修复：删掉了从未被任何代码读取的开关
# （ENABLE_WORKING_MEMORY / ENABLE_EPISODIC_MEMORY / ENABLE_SEMANTIC_MEMORY /
#   ENABLE_PLAN_AND_SOLVE / ENABLE_AB_TESTING / ENABLE_CONTEXT_MANAGEMENT /
#   ENABLE_SMART_COMPRESSION / ENABLE_TOOL_ECOSYSTEM / ENABLE_TOOL_MARKETPLACE /
#   ENABLE_REALTIME_COLLABORATION / ENABLE_PROGRESSIVE_AUTONOMY /
#   ENABLE_COST_OPTIMIZATION / ENABLE_DYNAMIC_MODEL_SELECTION / ENABLE_SMART_CACHE）
# ——它们只增运维认知负担且不生效；后续能力落地时按需重新引入（须同时接线到消费方）。
# 反向修正：以下开关有真实消费方，必须保留在配置层（否则运维无法启用/关闭）：
#   ENABLE_VISION_UNDERSTANDING（vision.py / tasks.py 视觉分析）、
#   ENABLE_SESSION_TREE（runner.py 分支历史，默认开）。
# 环境变量优先级高于此默认值，例如：ENABLE_MEMORY_SYSTEM=true

# P0: 核心认知能力（1-3个月）
ENABLE_MEMORY_SYSTEM = _env("ENABLE_MEMORY_SYSTEM", "false").lower() in ("1", "true", "yes", "on")

ENABLE_PLANNING = _env("ENABLE_PLANNING", "false").lower() in ("1", "true", "yes", "on")
ENABLE_PLANNING_TOT = _env("ENABLE_PLANNING_TOT", "false").lower() in ("1", "true", "yes", "on")
ENABLE_REFLEXION = _env("ENABLE_REFLEXION", "false").lower() in ("1", "true", "yes", "on")

ENABLE_EVALUATION = _env("ENABLE_EVALUATION", "false").lower() in ("1", "true", "yes", "on")

# P1: 协作与学习（3-6个月）
ENABLE_MULTI_AGENT = _env("ENABLE_MULTI_AGENT", "false").lower() in ("1", "true", "yes", "on")
ENABLE_AGENT_COLLABORATION = _env("ENABLE_AGENT_COLLABORATION", "false").lower() in ("1", "true", "yes", "on")
ENABLE_AGENT_COMPETITION = _env("ENABLE_AGENT_COMPETITION", "false").lower() in ("1", "true", "yes", "on")

ENABLE_LEARNING = _env("ENABLE_LEARNING", "false").lower() in ("1", "true", "yes", "on")
ENABLE_SKILL_LIBRARY = _env("ENABLE_SKILL_LIBRARY", "false").lower() in ("1", "true", "yes", "on")

ENABLE_MULTIMODAL = _env("ENABLE_MULTIMODAL", "false").lower() in ("1", "true", "yes", "on")
# 视觉分析（多模态的子能力）：与 ENABLE_MULTIMODAL 同时为真才生效（vision.py）
ENABLE_VISION_UNDERSTANDING = _env(
    "ENABLE_VISION_UNDERSTANDING", "false").lower() in ("1", "true", "yes", "on")

# 短剧 P1：多方案生成（Phase 2.4）。提案树与剧本版本 API 无额外副作用，始终可用（受权限约束）
ENABLE_MULTI_PROPOSAL = _env("ENABLE_MULTI_PROPOSAL", "false").lower() in ("1", "true", "yes", "on")

# Phase 1.3: 上下文压缩（Pi Agent式）：接近上下文上限时自动压缩历史为摘要
ENABLE_CONTEXT_COMPACTION = _env("ENABLE_CONTEXT_COMPACTION", "false").lower() in ("1", "true", "yes", "on")

# Phase 1.4: 分支摘要：导航切换分支时自动摘要被放弃段（默认随树会话启用）
ENABLE_BRANCH_SUMMARY = _env("ENABLE_BRANCH_SUMMARY", "true").lower() in ("1", "true", "yes", "on")

# 树会话：发生过分支导航时按活动分支构建模型历史（默认开；关闭则回退全量消息）
ENABLE_SESSION_TREE = _env("ENABLE_SESSION_TREE", "true").lower() in ("1", "true", "yes", "on")

# Phase 1.5: Skills机制（渐进式披露）：system prompt注入名称+描述，load_skill取全文
ENABLE_SKILLS = _env("ENABLE_SKILLS", "false").lower() in ("1", "true", "yes", "on")

# Phase 1.8: Agent辅助审查：review_episode工具附LLM主观审查（剧情逻辑/对白/节奏/情感）
ENABLE_AGENT_REVIEW = _env("ENABLE_AGENT_REVIEW", "false").lower() in ("1", "true", "yes", "on")

# Phase 3.1 轨道2: 工具注册表与沙箱执行（ToolRegistry 动态工具 + 子进程沙箱 + chain_tools）。
# 关闭时注册表零暴露（工具面/执行回退/API写路径/chain_tools），行为与现状一致。
ENABLE_TOOL_REGISTRY = _env("ENABLE_TOOL_REGISTRY", "false").lower() in ("1", "true", "yes", "on")
# 沙箱墙钟超时（秒）：注册表工具子进程执行上限（超时→确定性错误不重试）
SANDBOX_TIMEOUT_S = float(_env("SANDBOX_TIMEOUT_S", "5"))
# 试运行端点超时（秒）：比正式执行更短，防止把沙箱当免费算力
SANDBOX_TRY_TIMEOUT_S = float(_env("SANDBOX_TRY_TIMEOUT_S", "2"))
# 沙箱 stdout 输出上限（字节）：超限截断报错
SANDBOX_OUTPUT_LIMIT = int(_env("SANDBOX_OUTPUT_LIMIT", "65536"))
# chain_tools 最大步数（schema maxItems 与运行时双重约束）
CHAIN_MAX_STEPS = int(_env("CHAIN_MAX_STEPS", "5"))
# 注册表 (name, version) 进程内缓存 TTL（秒）：停用生效延迟上限
REGISTRY_CACHE_TTL = float(_env("REGISTRY_CACHE_TTL", "60"))

# Phase 3.1 市场侧: web_fetch 官方网络工具（父进程代理出口，fail-closed——
# 白名单为空时工具不注册不执行；精确 host 匹配不做后缀通配）
WEB_FETCH_ALLOWED_HOSTS = _env("WEB_FETCH_ALLOWED_HOSTS", "")
WEB_FETCH_TIMEOUT_S = float(_env("WEB_FETCH_TIMEOUT_S", "8"))
WEB_FETCH_MAX_BYTES = int(_env("WEB_FETCH_MAX_BYTES", "65536"))

# Phase 3.2 轨道2: 实时事件通道（ASGI WebSocket 事件推送 + 实时中断 + 运行中指令队列
# + local 双进程 SQLite 事件表后端）。关闭时 WSGI/长轮询行为与现状完全一致。
ENABLE_REALTIME_EVENTS = _env("ENABLE_REALTIME_EVENTS", "false").lower() in ("1", "true", "yes", "on")
# 每轮运行中指令队列上限：超出返回冲突（queue_full）
TURN_QUEUE_MAX = int(_env("TURN_QUEUE_MAX", "3"))

# Phase 3.4 轨道2: 分层记忆与项目记忆整合（重要性感知压缩 + MemoryRecord 结构化
# 跨会话层 + L1-L4 分层检索 + 轮次收尾沉淀）。关闭时各路径与现状逐项一致。
ENABLE_LAYERED_MEMORY = _env("ENABLE_LAYERED_MEMORY", "false").lower() in ("1", "true", "yes", "on")
# 压缩重要性保护阈值：组内最高条目分值 >= 此值的组不作为切割边界
MEMORY_IMPORTANT_THRESHOLD = float(_env("MEMORY_IMPORTANT_THRESHOLD", "0.8"))
# L3 注入条数分档（复杂度 <0.4 / 0.4-0.7 / >0.7）
MEMORY_L3_TIERS = _env("MEMORY_L3_TIERS", "3,5,8")
# 每轮沉淀条数上限
MEMORY_SETTLE_MAX_RECORDS = int(_env("MEMORY_SETTLE_MAX_RECORDS", "3"))

# Phase 3 可本地落地项（2026-11-21-backend-agent-feedback-inspiration）:
# 灵感库（agent 工具面开关；REST 始终可用）；反馈/决策解释挂既有协作开关。
ENABLE_INSPIRATION_LIBRARY = _env("ENABLE_INSPIRATION_LIBRARY", "false").lower() in ("1", "true", "yes", "on")
INSPIRATION_SEARCH_LIMIT = int(_env("INSPIRATION_SEARCH_LIMIT", "20"))
INSPIRATION_RECOMMEND_LIMIT = int(_env("INSPIRATION_RECOMMEND_LIMIT", "5"))
# 协作反馈注入条数（最近 N 条低分/带评论反馈进 system prompt）
FEEDBACK_PROMPT_RECENT = int(_env("FEEDBACK_PROMPT_RECENT", "3"))
# 审查反馈权重统计窗口（天）
REVIEW_FEEDBACK_WINDOW_DAYS = int(_env("REVIEW_FEEDBACK_WINDOW_DAYS", "30"))

# 成本优化（2026-11-22-backend-cost-optimization，主计划 3.3）:
# SmartCache 只缓存纯文本对话（LLM/CHAT 可重放）；关闭时零查询零写入。
ENABLE_SMART_CACHE = _env("ENABLE_SMART_CACHE", "false").lower() in ("1", "true", "yes", "on")
SMART_CACHE_TTL_S = int(_env("SMART_CACHE_TTL_S", "3600"))
SMART_CACHE_MAX_ROWS = int(_env("SMART_CACHE_MAX_ROWS", "500"))
# 语义近似缓存（字符 bigram Jaccard 零依赖；关闭时查询侧完全不执行）
ENABLE_SEMANTIC_CACHE = _env("ENABLE_SEMANTIC_CACHE", "false").lower() in ("1", "true", "yes", "on")
SEMANTIC_CACHE_THRESHOLD = float(_env("SEMANTIC_CACHE_THRESHOLD", "0.85"))
SEMANTIC_CACHE_SCAN_ROWS = int(_env("SEMANTIC_CACHE_SCAN_ROWS", "100"))
# 批量提案生成（多镜头合并单次请求；关闭时批量参数按普通单条处理）
ENABLE_BATCH_GENERATION = _env("ENABLE_BATCH_GENERATION", "false").lower() in ("1", "true", "yes", "on")
# 渐进自主（主计划 3.2.6）：高信任用户的高置信度非删除类提案创建即自动应用；
# 关闭时所有提案照旧 PENDING，零行为变化
ENABLE_PROGRESSIVE_AUTONOMY = _env("ENABLE_PROGRESSIVE_AUTONOMY", "false").lower() in ("1", "true", "yes", "on")
AUTO_APPROVE_MIN_CONFIDENCE = float(_env("AUTO_APPROVE_MIN_CONFIDENCE", "0.8"))
# 信任分级（主计划 3.2.5）：阈值与累积步长（确定性事件驱动，无 LLM 参与）
TRUST_DEFAULT_THRESHOLD = float(_env("TRUST_DEFAULT_THRESHOLD", "0.8"))
TRUST_STEP_APPLY = float(_env("TRUST_STEP_APPLY", "0.02"))
TRUST_STEP_DISCARD = float(_env("TRUST_STEP_DISCARD", "0.01"))
TRUST_INITIAL_SCORE = float(_env("TRUST_INITIAL_SCORE", "0.5"))

# 小说工作区（2026-11-23 组 1）：章节版本快照保留数量（0 = 不裁剪）
NOVEL_SNAPSHOT_KEEP = int(_env("NOVEL_SNAPSHOT_KEEP", "20"))

# 资产生成优化（2026-11-22-backend-asset-generation-optimization，主计划 3.5.3）:
# 低分辨率预览生成（关闭时预览端点 404，worker 预览分支不进——
# input_summary.preview 标记只由该端点写入，既有生成/合成路径零变化）
ENABLE_ASSET_PREVIEW = _env("ENABLE_ASSET_PREVIEW", "false").lower() in ("1", "true", "yes", "on")
# 预览图尺寸（OpenAI 兼容 size 参数；9:16 时自动换 512x896）
ASSET_PREVIEW_SIZE = _env("ASSET_PREVIEW_SIZE", "512x512")
# 预览成本估算比例（供应商按尺寸分档的近似；有 cost_config.preview_cost 时该比例不生效）
PREVIEW_COST_RATIO = float(_env("PREVIEW_COST_RATIO", "0.333"))

# ─── Agent规划与预算配置 ───────────────────────────────────────────────────
# Phase 0 max 审查修复：以下阈值此前在 planning.py 中是死字面量，
# 代码不读配置——现在接线（默认值与原字面量一致，行为不变）。

# 任务复杂度阈值：>此值触发规划流程
AGENT_PLANNING_COMPLEXITY_THRESHOLD = float(_env("AGENT_PLANNING_COMPLEXITY_THRESHOLD", "0.5"))

# 多路径探索阈值：>此值启用Tree of Thoughts
AGENT_EXPLORATION_THRESHOLD = float(_env("AGENT_EXPLORATION_THRESHOLD", "0.7"))

# 反思触发条件：工具调用失败次数
AGENT_REFLECTION_FAILURE_THRESHOLD = int(_env("AGENT_REFLECTION_FAILURE_THRESHOLD", "2"))

# ToT 候选方案数上限（成本有界）
TOT_NUM_CANDIDATES = int(_env("TOT_NUM_CANDIDATES", "3"))

# 活跃计划停滞兜底阈值（分钟；超时无进度则放弃并允许新建计划）
AGENT_PLAN_STALL_TURNS = int(_env("AGENT_PLAN_STALL_TURNS", "50"))

# ─── 剧内成员生命周期（auth/membership）───
# 成员数上限（每剧，含属主）
PLAY_MEMBER_LIMIT = int(_env("PLAY_MEMBER_LIMIT", "20"))
# 每日邀请数上限（每剧）
PLAY_INVITE_DAILY_LIMIT = int(_env("PLAY_INVITE_DAILY_LIMIT", "20"))
# 邀请过期天数
PLAY_INVITE_EXPIRY_DAYS = int(_env("PLAY_INVITE_EXPIRY_DAYS", "7"))

# ─── 向量检索配置 ──────────────────────────────────────────────────────────
# 保留：Phase 3（P2）语义检索规划使用，暂无消费方（embedding 脚手架已删）

# 向量相似度阈值（0-1，越高越严格）
VECTOR_SIMILARITY_THRESHOLD = float(_env("VECTOR_SIMILARITY_THRESHOLD", "0.7"))

# 检索返回的最大结果数
VECTOR_SEARCH_LIMIT = int(_env("VECTOR_SEARCH_LIMIT", "5"))

# 向量索引类型：ivfflat（适用于百万级）或 hnsw（适用于亿级）
VECTOR_INDEX_TYPE = _env("VECTOR_INDEX_TYPE", "ivfflat")
VECTOR_INDEX_LISTS = int(_env("VECTOR_INDEX_LISTS", "100"))  # ivfflat参数


DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
