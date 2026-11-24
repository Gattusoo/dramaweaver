# 部署与配置说明

> 项目骨架自 `restructure-dvadmin-skeleton` 变更（原 dvadmin 脚手架，现位于 apps/core/）起改为 `application/` 包，配置从环境变量 / `conf/env.py` 读取。

## 1. 启动入口

- **Django**：`DJANGO_SETTINGS_MODULE=application.settings`
  - `python manage.py runserver`（开发）
  - WSGI：`application.wsgi:application`
  - ASGI：`application.asgi:application`
- **Celery**：`celery -A application worker`（broker/backend 由配置决定）
  - Celery 应用名改为 `application`，`autodiscover_tasks` 扫描 `apps.*`。
  - **注意**：任务的显式 `name="shortplay.tasks.xxx"` 保持不变（消息路由键与模块路径解耦），broker 中残留消息仍可路由。

### Windows 本地模式启动（工作台 Stage C1）

`DEPLOYMENT_MODE=local` + `CREATION_TASK_BACKEND=local` 时**无需 Redis/Celery**，两个终端各起一条命令：

```bat
:: 终端 1 —— API 服务（回环监听）
.venv\Scripts\python.exe manage.py runserver

:: 终端 2 —— 本地任务 worker（单逻辑 worker，数据库队列）
.venv\Scripts\python.exe manage.py run_local_worker
```

- **启动顺序**：先 API 后 worker（或反之均可——执行记录持久化在 SQLite，API 派发后 worker 随时领取；worker 未启动时任务保持 PENDING，不丢失）。
- **停止**：两个终端分别 `Ctrl+C`。API 退出不影响已入队任务；worker 中断时未完成任务按恢复分类处理（确定性任务重启后自动重领，付费任务进入 `RECOVERY_REQUIRED` 等待确认，绝不自动重放付费请求）。
- **数据目录备份**：直接复制 `LOCAL_DATA_DIR` 整个目录（含 `db.sqlite3`、`projects/` 清单与资产）——停服后复制即可获得完整备份；或用 `POST /api/plays/{id}/export/` 导出单项目 ZIP 跨环境迁移。
- **健康检查**（worker 运行中）：Stage C2 的健康端点报告 worker 心跳/队列深度/FFmpeg 可用性。
- 冒烟验证：`python manage.py run_local_worker --once`（处理完当前可领取任务后退出）。

## 2. 配置来源

优先级（高 → 低）：

1. 环境变量
2. 本地配置文件 `conf/env.py`（不入库；复制 `conf/env.example.py` 获得）
3. `conf/env.example.py` 中的开发默认值

### 配置项清单

| 配置项 | 说明 | 默认（env.example.py） |
|---|---|---|
| `SECRET_KEY` | Django 密钥；生产必填，留空 + DEBUG=False 启动失败 | `""` |
| `DEBUG` | 调试模式；生产必须 False | `False` |
| `ALLOWED_HOSTS` | 允许的主机（逗号分隔） | `[]` |
| `DEPLOYMENT_MODE` | 部署模式 `local`/`shared`（见下"双模式部署"） | `shared` |
| `CREATION_TASK_BACKEND` | 任务传输后端 `celery`/`local`；留空按模式选择 | `""` |
| `LOCAL_DATA_DIR` | 本地数据目录；local 模式必填（SQLite/项目清单/资产/导出都落在此） | `""` |
| `DATABASE_ENGINE` / `DATABASE_NAME` | 数据库引擎与名称 | sqlite3 / `db.sqlite3` |
| `REDIS_URL` | Redis 地址（熔断/限流/Celery/Cache）；留空走内存 | `""` |
| `AI_GATEWAY_STORE` | AI Gateway 存储后端 `memory`/`redis`/`auto` | `auto` |
| `CELERY_BROKER_URL` / `CELERY_RESULT_BACKEND` | Celery broker/backend；留空按 REDIS_URL 或内存 | `""` |
| `CELERY_TASK_ALWAYS_EAGER` | `1` 时任务同步执行（测试） | `0` |
| `STORAGE_BACKEND` | 对象存储 `local`/`s3` | `local` |
| `STORAGE_OSS_*` | S3 兼容存储凭证/桶/endpoint | 空 |
| `FFMPEG_BIN` / `FFPROBE_BIN` | ffmpeg/ffprobe 路径；留空自动查找 | `""` |
| `SUBTITLE_FONT` | 字幕字体文件；留空自动查找 | `""` |
| `SAFE_FETCH_ALLOWED_HOSTS` | 出站下载 SSRF 白名单（逗号分隔内网主机） | `""` |

### 双模式部署（工作台 Stage A1）

`DEPLOYMENT_MODE=shared`（默认）：现有 Celery/Redis、多用户认证（JWT + PlayMember）、对象存储布局全部保持，不切换本地队列、不放宽鉴权。

`DEPLOYMENT_MODE=local`：个人创作工作台模式。

```python
# conf/env.py —— local 最小配置
DEBUG = True
DEPLOYMENT_MODE = "local"
LOCAL_DATA_DIR = r"D:\my-plays"   # 必填：SQLite/项目清单/内容寻址资产/导出/缓存
# REDIS_URL / CELERY_BROKER_URL 均不配置 —— 不要求 Redis，任务走数据库持久化队列
```

- **启动安全检查**（settings 加载时拒绝启动，不是运行期报错）：
  - 未配置 `LOCAL_DATA_DIR` → 启动失败（数据目录无静默回退）；
  - `ALLOWED_HOSTS` 含非回环地址（`0.0.0.0`/局域网/公网域名）→ 启动失败并提示改用 `shared` 模式配置认证与访问控制；仅允许 `127.0.0.1`/`::1`/`localhost`，未配置时自动补回环；
  - SQLite 默认落在 `LOCAL_DATA_DIR/db.sqlite3`（显式配置 `DATABASE_NAME` 时尊重用户选择）。
- **持久密钥**：DEBUG 下未配置 `SECRET_KEY` 时，密钥写入 `LOCAL_DATA_DIR/.secret_key`，重启不换密钥（JWT/session 不失效）；写失败回退进程内临时密钥并告警。
- **初始化本机用户**：`python manage.py init --admin-username <用户名> --admin-password <密码>` 创建本机管理员后登录（复用既有 init 命令，无匿名/无密码模式）。
- **shared 模式不读取 local 快捷身份配置**，鉴权行为与既有部署完全一致。

### 项目目录与导入导出（工作台 Stage B1/B2）

`LOCAL_DATA_DIR/projects/<play_uuid>/` 项目目录布局：

```
project.json     # 项目清单（schema_version=1，保存/导出时原子刷新；延迟快照）
assets/sha256/…  # 原始资产（内容寻址，按 SHA-256 命名——绝不清理）
exports/         # 导出 ZIP（如需落盘）
cache/           # 可整体删除（清理接口仅作用于该子树）
logs/
```

- **数据目录即备份边界**：`LOCAL_DATA_DIR` 包含 SQLite、清单、全部资产——复制该目录即获得可迁移项目数据；导出 ZIP（`POST /api/plays/{id}/export/`）为跨环境交换格式。
- **导入流程**：`import-precheck`（只读校验：路径/重复/版本/摘要/炸弹限额，DB 与目录零写入）→ `import-confirm`（确认后新建项目；同 UUID 已存在返回 409 不覆盖；DB 失败只清理新建目录）。
- **换机迁移**：新机器装好后导入 ZIP 即可重建项目（结构/资产/依赖/版本按 UUID 映射）；远程 URL 资产仅恢复受控元数据行，需显式选择离线下载才拉取。
- **Agent 会话侧状态不随项目导出**（会话树/提案历史/技能库/评估指标——v1 明示排除，导出响应 `X-Export-Excluded` 头明示，不静默发生）。

### 密钥与调试开关

- 生产（`DEBUG=False`）下未配置 `SECRET_KEY` 时，进程启动失败并提示缺少密钥。
- 开发（`DEBUG=True`）下未配置 `SECRET_KEY` 时，生成进程内临时密钥并告警。

### 计费派发闸（shared 部署）

- `ENABLE_BILLING_HOLD=true`（默认）：任务派发经过"槽位-预扣顺序闸"——渲染类任务受
  并发槽位（默认 2）与每日渲染时长（默认 3600s）约束，生成/助手类任务受 AI 并发槽位
  （默认 10）约束；预扣按 `estimate_task_cost` 估算，余额不足时 TaskRecord 置 FAILED
  （error_message 记原因），助手轮次返回 `402 insufficient_points`。local 模式闸短路
  （个人本机无计费主体），`ENABLE_BILLING_HOLD=false` 为应急旁路。
- **hold 过期回收需调度**：`expire_stale_holds`（billing 服务）负责回收异常退出后
  挂死的预扣，shared 部署应配置 Celery beat 定期执行（如每 15 分钟一次）：

  ```python
  # application/celery.py 的 beat_schedule 示例（部署侧配置，项目不带默认调度）
  "expire-stale-holds": {
      "task": "shortplay.billing.expire_stale_holds",
      "schedule": 900.0,
  }
  ```

  local 模式无 beat 进程、不做回收——hold 不会消失但 settle/refund 幂等，任务终态
  对账不受影响（余额显示长期冻结，重启数据库可清）。
- 供应商并发限流已租约化（`RATE_LIMIT_LEASE_SECONDS`，默认 600s）：进程崩溃泄漏的
  限流槽位到租约期由后续请求自动回收，不再依赖 600s 空闲清零。

## 3. 初始化基础数据

迁移完成后执行（可重复）：

```bash
python manage.py migrate
python manage.py init --admin-username <用户名> --admin-password <密码>
```

- 创建超级管理员（`is_staff`/`is_superuser`/`role=admin`）。
- 创建系统配方模板（`AgentConfig is_template=True`）。
- 已存在用户不重复创建、不改密码；已存在模板按唯一键更新。

## 4. 迁移重置

本期重置了迁移：旧 `shortplay/migrations/0001~0009` 已删除，各新应用（`system`、`ai_gateway`、`creation`、`distribution`）重新生成 `0001_initial`（部分含 `0002_initial` 处理跨应用外键依赖）。表名（`shortplay_*`）不变。
