"""配置模板：列出全部运行配置项及开发默认值。

部署时复制本文件为 ``conf/env.py`` 并填写真实值；环境变量可覆盖任意一项。
注意：``SECRET_KEY`` 在模板中留空——生产（DEBUG=false）下必须显式提供，
否则启动会因缺少密钥而失败。
"""

# 调试模式：生产必须为 False
DEBUG = False

# Django 密钥：留空时由 settings 按 DEBUG 决策（生产缺密钥则拒绝启动）
SECRET_KEY = ""

# ─── 部署模式（local 创作工作台 / shared 共享平台）──────────────────────────
# shared（默认）：现有 Celery/Redis、多用户认证、对象存储布局全部不变。
# local：个人创作工作台——SQLite + 本地数据目录 + 数据库持久化任务队列 +
#        单逻辑 worker（python manage.py run_local_worker），不要求 Redis；
#        仅允许回环监听（ALLOWED_HOSTS ⊆ 127.0.0.1/::1/localhost），
#        首次使用需 python manage.py init --admin-username ... --admin-password ...
#        创建本机管理员后登录。
DEPLOYMENT_MODE = "shared"
# 任务传输后端：celery | local；留空按模式选择（shared→celery，local→local）
CREATION_TASK_BACKEND = ""
# 本地数据目录：local 模式必填（SQLite/项目清单/内容寻址资产/导出/缓存都落在此）
LOCAL_DATA_DIR = ""

# 允许的主机
ALLOWED_HOSTS = []

# 数据库
DATABASE_ENGINE = "django.db.backends.sqlite3"
DATABASE_NAME = "db.sqlite3"  # 相对 BASE_DIR；可填绝对路径

# Redis（AI Gateway 熔断 / 限流、Celery broker、Cache 共用；留空走本地内存）
# 生产/多进程部署必须配置：留空时 Celery 用进程内内存 broker，独立 worker 收不到任务
REDIS_URL = ""

# AI Gateway 存储后端：memory | redis | auto
AI_GATEWAY_STORE = "auto"

# 供应商并发限流租约时长（秒）：到租约期未释放的槽位自动回收，兜底进程崩溃泄漏
RATE_LIMIT_LEASE_SECONDS = "600"

# Celery
# 留空则回退 REDIS_URL；两者都空 → memory://（仅单进程可用，独立 worker 无效）
CELERY_BROKER_URL = ""
CELERY_RESULT_BACKEND = ""
# 单进程开发可设 1：任务在 web 进程内同步执行，无需 worker
CELERY_TASK_ALWAYS_EAGER = "0"

# 对象存储（可选，兼容 S3 协议；未配置时使用本地 MEDIA_ROOT）
STORAGE_BACKEND = "local"
STORAGE_OSS_ACCESS_KEY = ""
STORAGE_OSS_SECRET_KEY = ""
STORAGE_OSS_BUCKET = ""
STORAGE_OSS_ENDPOINT = ""

# FFmpeg / ffprobe 可执行路径（留空走自动查找）
FFMPEG_BIN = ""
FFPROBE_BIN = ""

# 字幕字体（留空走自动查找）
SUBTITLE_FONT = ""

# 出站下载 SSRF 白名单（逗号分隔的内网主机）
SAFE_FETCH_ALLOWED_HOSTS = ""

# 服务费埋点（决策 18，单位：分/次，默认 0 = 关闭）。
# 用户自带凭证（custom provider + UserProviderCredential）每次成功调用 emit
# `service_fee` 事件，由 billing 变更消费落流水；本变更只埋事件，不做扣费。
SERVICE_FEE_PER_CALL = "0"

# ─── 计费与限额（决策 7/8/16/17/18）─────────────────────────────────────
# 注册冷启动赠金（分，默认 1000 = ¥10；0 = 关闭）
REGISTER_GRANT_POINTS = "1000"
# 单任务结算大额告警阈值（分；0 = 关闭）
OWNER_ALERT_THRESHOLD_POINTS = "5000"
# 并发槽位默认（每属主：渲染 2 / AI 生成 10；UserQuotaOverride 可按用户覆盖）
MAX_CONCURRENT_RENDER = "2"
MAX_CONCURRENT_AI = "10"
# 每日渲染时长上限（秒，按属主累计）
DAILY_RENDER_SECONDS_LIMIT = "3600"
# 预扣闸应急开关（默认开；关闭 = 跳过槽位与预扣直接派发）
ENABLE_BILLING_HOLD = "true"
# 降级链跨路线回退开关（决策 9：渠道→回退平台默认拒绝；显式开启才允许）
ENABLE_CROSS_ROUTE_FALLBACK = "false"

# ─── Agent 功能开关（Phase 灰度发布；字符串 "true"/"false"，环境变量可覆盖）───
# P0 核心认知能力
ENABLE_MEMORY_SYSTEM = "false"        # 项目记忆注入/保存
ENABLE_PLANNING = "false"             # 任务复杂度超阈值时自动规划
ENABLE_PLANNING_TOT = "false"         # 多路径探索（Tree of Thoughts，成本高）
ENABLE_REFLEXION = "false"            # 工具连续失败后生成反思
ENABLE_EVALUATION = "false"           # turn_end 等评估钩子与指标落库
# P1 协作与学习
ENABLE_MULTI_AGENT = "false"          # 多 Agent 协作总开关
ENABLE_AGENT_COLLABORATION = "false"  # 复杂任务自动进入协作
ENABLE_AGENT_COMPETITION = "false"    # 竞争模式（同一目标并行多次模型调用）
ENABLE_LEARNING = "false"             # 经验学习
ENABLE_SKILL_LIBRARY = "false"        # 技能库
ENABLE_MULTIMODAL = "false"           # 多模态总开关
ENABLE_VISION_UNDERSTANDING = "false" # 视觉分析（须同时开启 ENABLE_MULTIMODAL）
ENABLE_MULTI_PROPOSAL = "false"       # 多方案生成
# 上下文与工具
ENABLE_CONTEXT_COMPACTION = "false"   # 接近上下文上限时自动压缩历史
ENABLE_BRANCH_SUMMARY = "true"        # 导航切换分支时摘要被放弃段（默认开）
ENABLE_SESSION_TREE = "true"          # 导航后按活动分支构建模型历史（默认开）
ENABLE_SKILLS = "false"               # Skills 渐进式披露
ENABLE_AGENT_REVIEW = "false"         # review_episode 附 LLM 主观审查
ENABLE_TOOL_REGISTRY = "false"        # 工具注册表+沙箱（Phase 3.1 轨道2）
SANDBOX_TIMEOUT_S = "5"               # 沙箱执行超时（秒）
SANDBOX_TRY_TIMEOUT_S = "2"           # 试运行超时（秒）
SANDBOX_OUTPUT_LIMIT = "65536"        # 沙箱输出上限（字节）
CHAIN_MAX_STEPS = "5"                 # chain_tools 最大步数
REGISTRY_CACHE_TTL = "60"             # 注册表缓存 TTL（秒）
WEB_FETCH_ALLOWED_HOSTS = ""          # web_fetch 出站域名白名单（逗号分隔，精确 host；空=禁用）
WEB_FETCH_TIMEOUT_S = "8"             # web_fetch 出站超时（秒）
WEB_FETCH_MAX_BYTES = "65536"         # web_fetch 响应字节上限（超限截断）
ENABLE_REALTIME_EVENTS = "false"      # 实时事件通道（WS/中断/指令队列，Phase 3.2 轨道2）
TURN_QUEUE_MAX = "3"                  # 每轮运行中指令队列上限
ENABLE_LAYERED_MEMORY = "false"       # 分层记忆（重要性压缩保护+MemoryRecord，Phase 3.4 轨道2）
MEMORY_IMPORTANT_THRESHOLD = "0.8"    # 压缩重要性保护阈值
MEMORY_L3_TIERS = "3,5,8"             # L3 注入条数分档（低/中/高复杂度）
MEMORY_SETTLE_MAX_RECORDS = "3"       # 每轮沉淀条数上限
ENABLE_INSPIRATION_LIBRARY = "false"  # 灵感库 agent 工具面（REST 不受此开关限制）
INSPIRATION_SEARCH_LIMIT = "20"       # 灵感检索返回上限
INSPIRATION_RECOMMEND_LIMIT = "5"     # 灵感推荐返回上限
FEEDBACK_PROMPT_RECENT = "3"          # 协作反馈注入条数（最近 N 条）
REVIEW_FEEDBACK_WINDOW_DAYS = "30"    # 审查反馈权重统计窗口（天）

# ─── 成本优化（SmartCache / 语义缓存 / 批量生成）───
ENABLE_SMART_CACHE = "false"          # 模型响应缓存（只覆盖纯文本对话；关闭零查询零写入）
SMART_CACHE_TTL_S = "3600"            # 缓存过期秒数
SMART_CACHE_MAX_ROWS = "500"          # 缓存行数上限（超出清理最旧）
ENABLE_SEMANTIC_CACHE = "false"       # 语义近似缓存（bigram Jaccard；关闭时查询侧不执行）
SEMANTIC_CACHE_THRESHOLD = "0.85"     # 语义命中相似度阈值（bigram Jaccard 近似）
SEMANTIC_CACHE_SCAN_ROWS = "100"      # 语义比对扫描行数上限
ENABLE_BATCH_GENERATION = "false"     # 批量提案生成（关闭时批量参数按普通单条处理）
ENABLE_PROGRESSIVE_AUTONOMY = "false"  # 渐进自主（关闭时提案照旧 PENDING，零行为变化）
AUTO_APPROVE_MIN_CONFIDENCE = "0.8"    # 自动批准的提案置信度下限（payload.confidence 0-1）
TRUST_DEFAULT_THRESHOLD = "0.8"        # 自动批准的默认信任分阈值（行级可覆盖）
TRUST_STEP_APPLY = "0.02"              # 提案被采纳的信任分增量
TRUST_STEP_DISCARD = "0.01"            # 提案被丢弃/过期的信任分扣减
TRUST_INITIAL_SCORE = "0.5"            # 信任分初始值
NOVEL_SNAPSHOT_KEEP = "20"             # 小说章节版本快照保留数量（0=不裁剪）

# ─── 资产生成优化（低分辨率预览）───
ENABLE_ASSET_PREVIEW = "false"         # 资产预览生成（关闭时预览端点 404，生成路径零变化）
ASSET_PREVIEW_SIZE = "512x512"         # 预览图尺寸（9:16 时自动换 512x896）
PREVIEW_COST_RATIO = "0.333"           # 预览成本估算比例（有 preview_cost 显式配置时不生效）

# ─── Agent 阈值（成本与行为调参；默认值与代码回退一致）───
AGENT_PLANNING_COMPLEXITY_THRESHOLD = "0.5"  # 复杂度 > 此值触发规划
AGENT_EXPLORATION_THRESHOLD = "0.7"          # 复杂度 > 此值启用多路径探索
AGENT_REFLECTION_FAILURE_THRESHOLD = "2"     # 同一工具连续失败次数触发反思
TOT_NUM_CANDIDATES = "3"                     # 多路径探索候选数上限
AGENT_PLAN_STALL_TURNS = "50"                # 活跃计划停滞（分钟）后放弃

# ─── 剧内成员生命周期（auth/membership）───
PLAY_MEMBER_LIMIT = "20"                     # 每剧成员数上限（含属主）
PLAY_INVITE_DAILY_LIMIT = "20"               # 每剧每日邀请数上限
PLAY_INVITE_EXPIRY_DAYS = "7"                # 邀请过期天数
