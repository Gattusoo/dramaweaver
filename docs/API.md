# 短剧 AIGC 生产流水线 — API 文档

> 项目：`shortplaygenerating`（Django 5.2 + DRF + Celery）
> 本文档基于当前代码库（`shortplay/urls.py`、`views.py`、`auth_views.py`、`gateway_views.py`、`analytics_views.py`、`serializers.py`、`permissions.py`）自动整理。

---

## 0. 通用说明

- **Base URL**：`http://<host>/api/`
- **认证**：HTTP 头 `Authorization: Bearer <access_token>`（JWT）。除注册/登录外均需认证。
- **身份双层**：全局只分 `is_platform_admin`（平台管理员）/ 普通用户；业务权限只看剧内成员表 `PlayMember`（`owner` / `editor` / `viewer`，`status=active`）
  - 普通用户：注册即可建剧，创建者自动成为该剧属主
  - 剧内三层（成员表是唯一判定来源，平台管理员不参与业务接口）：
    - **view**：active 成员（任意角色）
    - **edit**：owner / editor（内容增删改、approve/reject/redo、字幕、预览/衍生品）
    - **manage**：owner（改/删短剧、成员邀请/移除、publish、分集增删、发布记录写操作、属主转让）
  - 平台管理员：管用户标记、平台目录与统计；跨剧查看走审核通道（强制审计），冻结/解冻短剧；不改用户内容
  - 属主转让后原属主降为 editor，权限随之收缩
- **统一响应格式**：所有 JSON 接口返回 `{ code, msg, data }`（见下方"响应格式与错误码"）。列表分页响应为 `{ code, msg, page, limit, total, data }`，分页参数 `page`（从 1 起）/ `limit`（默认 20，最大 200）。文件下载（字幕导出等）与 SSE 事件流不包装。
- **错误响应**：`{ code, msg, data }`，`code` 为业务错误码（见错误码表），HTTP 状态码保持真实语义。
- **JWT**：access 12h，refresh 7d，支持黑名单（`rest_framework_simplejwt`）。
- **媒体存储**：默认本地 `MEDIA_ROOT`（`media/`）；配置 `STORAGE_BACKEND=s3` 及相关环境变量可切对象存储。

---

## 0.1 响应格式与错误码（BREAKING）

自 `restructure-dvadmin-skeleton` 变更（原 dvadmin 脚手架重组）起，所有 JSON 接口统一为下述结构。前端需按此一次性适配。

### 成功响应

```
{ "code": 2000, "msg": "ok", "data": <对象|数组|null> }
```

HTTP 状态码保持真实语义（200 / 201 / 204 等）；`204` 无响应体。

### 分页响应

```
{ "code": 2000, "msg": "ok", "page": 2, "limit": 10, "total": 25, "data": [...] }
```

- 查询参数：`page`（从 1 开始）、`limit`（默认 20，最大 200）。
- 页码越界返回空数组（`data: []`），`total` 仍为总数，HTTP 200。
- `limit` 超过 200 时按 200 返回。

### 错误响应

```
{ "code": <业务错误码>, "msg": "<可展示说明>", "data": null }
```

字段校验失败时 `code` 为 4000，原校验字典放入 `data.errors`：

```
{ "code": 4000, "msg": "参数校验失败", "data": { "errors": { "title": ["该字段是必填项。"] } } }
```

服务端未预期异常返回 HTTP 500：

```
{ "code": 5000, "msg": "服务器内部错误", "data": { "request_id": "<请求ID>" } }
```

异常类型/信息/堆栈不外泄；`request_id` 同时写入服务端日志（响应头 `X-Request-ID`）。

### 业务错误码表

| code  | HTTP | 含义 |
|-------|------|------|
| 2000  | 2xx  | 成功 |
| 4000  | 400  | 通用参数错误 / 字段校验失败 |
| 4010  | 401  | 未认证或认证失效 |
| 4030  | 403  | 无权限 |
| 4040  | 404  | 资源不存在 |
| 4050  | 405  | 方法不允许 |
| 4090  | 409  | 状态冲突 |
| 4290  | 429  | 请求过于频繁 |
| 5000  | 500  | 服务器内部错误 |

业务码命名空间（与信封 `40x0` 段并存，不占用同一数值；随功能域引入）：

| code  | HTTP | 含义 |
|-------|------|------|
| 4003  | 403  | 短剧已被平台管理员冻结（生成/发布入口拒绝，附 `reason: "play_frozen"`） |
| 4001  | 402  | 积分不足（任务派发预扣失败，附 `reason: "insufficient_points"`，`data` 含 `needed`（需扣分）与 `balance`（当前余额，整数分）） |
| 4029  | 402  | 渲染额度用完（属主当日渲染时长已达上限，附 `reason: "daily_render_exhausted"`） |

> 同一含义不得复用不同的码；后续新增业务错误码须在此表登记。

#### 计费端点（billing）

| 端点 | 说明 |
|------|------|
| `GET /api/wallet/` | 当前用户钱包自查：余额（整数分）+ 全部流水（类型/金额/引用/余额快照） |
| `GET /api/admin/wallets/` | 平台管理员：全部钱包列表（余额+近期流水），分页 |
| `POST /api/admin/wallets/{user}/adjust/` | 平台管理员调整余额：`{ "amount": ±分, "note": "必填原因" }`；写 `adjust` 流水 + `wallet_adjust` 审计；amount 为 0 或缺 note 返回 400；普通用户 403 |

流水类型（`type`）：`grant` 发放 / `hold` 预扣 / `settle` 结算 / `refund` 退回 / `adjust` 管理员调整 / `fee` 服务费（自带 Key 按次）/ `meter` 存储计量（第二期结算）。流水只追加，无任何更新端点。

### 不包装的响应

- **文件下载**：字幕导出 `GET /api/subtitle-tracks/{id}/export/srt|vtt` 返回 SRT/VTT 文本附件，不被 JSON 包装。
- **SSE 事件流**：按 SSE 格式逐条推送，不被 JSON 包装。

### 新旧对照与前端迁移要点

| 旧格式 | 新格式 |
|---|---|
| `{ count, next, previous, results }` | `{ code, msg, page, limit, total, data }`（`count`→`total`，`results`→`data`） |
| `{ "detail": "..." }`（错误） | `{ code, msg, data }`（`detail`→`msg`，新增 `code`） |
| `?page=N` | `?page=N&limit=M`（`PAGE_SIZE`→`limit`） |

迁移要点：列表解析从 `response.results` 改为 `response.data`；错误判断从读 `detail` 改为先判 `code` 再展示 `msg`；分页总数从 `count` 改读 `total`；字段错误从 `detail` 改读 `data.errors`。

---

## 0.2 通用说明（旧版备注）

### 枚举取值速查

| 域 | 取值 |
|---|---|
| `provider_type` / `model_type` | `LLM` \| `TTS` \| `IMAGE` \| `VIDEO` |
| `asset_type` | `OUTLINE` \| `EPISODE_SCRIPT` \| `STORYBOARD` \| `CHARACTER_REF` \| `IMAGE` \| `AUDIO` \| `BGM` \| `VIDEO` \| `SUBTITLE` \| `TRANSITION` \| `INTRO` \| `OUTRO` \| `BG_IMAGE` |
| `asset_status` | `PENDING` \| `GENERATING` \| `READY` \| `FAILED` \| `SUPERSEDED` |
| `task_type` | `OUTLINE` \| `EPISODE_SCRIPT` \| `STORYBOARD` \| `CHARACTER_REF` \| `IMAGE_GEN` \| `TTS_GEN` \| `BGM_SELECT` \| `VIDEO_COMPOSE` \| `PUBLISH` |
| `task_status` | `PENDING` \| `RUNNING` \| `SUCCESS` \| `FAILED` \| `RETRYING` \| `CANCELLED` |
| `play_status` | `DRAFT` \| `OUTLINING` \| `SCRIPTING` \| `STORYBOARDING` \| `ASSET_GEN` \| `COMPOSING` \| `COMPLETED` \| `PUBLISHED` \| `PAUSED_AT_OUTLINE` \| `PAUSED_AT_SCRIPT` \| `PAUSED_AT_ASSETS` |
| `audit_mode` | `NONE` \| `KEY_CHECKPOINTS` \| `EVERY_STEP` |
| `redo_granularity` | `SHOT` \| `SCENE` \| `EPISODE` |
| `review_point` | `OUTLINE` \| `EPISODE_SCRIPT` \| `CHARACTER_REF` \| `ASSETS` \| `COMPLETED` |
| `review_decision` | `APPROVED` \| `REJECTED` \| `MODIFIED` |
| `subtitle_source_type` | `AUTO` \| `MANUAL` \| `TRANSLATION` |
| `publication_status` | `PENDING` \| `UPLOADING` \| `REVIEWING` \| `PUBLISHED` \| `REJECTED` \| `FAILED` |

---

## 1. 认证 `/api/auth/`

### POST /api/auth/register/
注册（无需登录）。新用户一律为普通用户（无全局角色；注册即可建剧）。

```json
{ "username": "alice", "password": "P@ssw0rd", "email": "a@x.com" }
```

→ `201` `{ user, access, refresh }`
`user` = `{ id, username, email, is_platform_admin, first_name, last_name }`

### POST /api/auth/login/
```json
{ "username": "alice", "password": "P@ssw0rd" }
```
→ `200` `{ access, refresh, user }`

### POST /api/auth/refresh/
```json
{ "refresh": "<refresh_token>" }
```
→ `200` `{ access }`

### POST /api/auth/logout/
```json
{ "refresh": "<refresh_token>" }
```
→ `200` `{ "detail": "已登出" }`（refresh token 进黑名单）

### GET /api/auth/me/
→ `200` 当前用户 `{ id, username, email, is_platform_admin, first_name, last_name }`
（全局四级角色已退役；`is_platform_admin` 是唯一的平台级身份标记）

### PUT /api/auth/users/{id}/platform-admin/
平台管理员变更管理员标记。`{ "is_platform_admin": true }` → `200` 用户信息
不能移除最后一个有效平台管理员（`400`）。原 `/role/` 端点已退役（`404`）。

---

## 2. 短剧 `/api/plays/`

标准 CRUD：`GET/POST /api/plays/`、`GET/PUT/PATCH/DELETE /api/plays/{id}/`。
创建（POST）任意认证用户（注册即创作者，创建者自动成为属主）；读需 view（active 成员）；PUT/PATCH/DELETE、publish、成员管理需 manage（属主）；approve/reject/redo 需 edit（属主/编辑）。
平台管理员不因全局标记获得业务接口访问权（跨剧查看走 `/api/admin/plays/{id}/review/` 审核通道）。

**Play 字段**：`id, title, synopsis, genre, total_episodes, status, audit_mode, redo_granularity, consistency_mode, gen_params, agent_config, agent_config_id, created_by, members[], thumbnail_url, episodes[], characters[], created_at, updated_at`
`members` 为只读回显：`[{ "user", "username", "role": "owner|editor|viewer" }]`（active 成员，含属主行）。

#### 创作参数 `gen_params`（工作台 Stage A1）

`gen_params` 为 JSON 对象，按"任务显式参数 > Play.gen_params > AgentConfig.style_params > 系统默认"解析为有效参数后进入图像/视频生成：

| 字段 | 支持 | 说明 |
|---|---|---|
| `aspect_ratio` | ✅ 已生效 | 画面比例（`9:16`/`16:9`/`1:1`/`4:3`/`3:4`）。覆盖配方 `style_params.aspect_ratio`；传入图片适配器参数并决定合成画布（如 `16:9` → 1920×1080） |
| `style` | ✅ 已生效 | 风格名。仅当配方 `style_params.grade.<style>` 已配置该风格时映射生效（style → 配方已支持的调色配置）；未配置时归入 `unsupported_params` 明示，不猜测映射 |
| `create_type` | ⏳ 保留字段 | 携带时在生成结果 `metadata.unsupported_params` 中明示"未参与生成"，不静默宣称已生效 |
| `duration` | ⏳ 保留字段 | 同上 |
| `count` | ⏳ 保留字段 | 同上 |

有效参数回显：IMAGE/PREVIEW/VIDEO Asset 的 `parameters` 与 `metadata.gen_params` 记录本次生成实际使用的参数；未支持字段记录在 `metadata.unsupported_params`。缺省（三层均未配置）时保持既有行为（默认 9:16 竖画布，payload 不带 aspect_ratio）。

### 剧内成员（邀请接受制）

| 端点 | 说明 |
|---|---|
| `POST /api/plays/{id}/invite/` | 属主邀请成员。`{ "user_ids": [2], "role": "editor" }` → `201 { detail, invitations[] }`；角色仅 `editor/viewer`；超成员/每日邀请上限 `429`；待对方接受才生效 |
| `GET /api/plays/{id}/members/` | active 成员列表（成员可看） |
| `POST /api/plays/{id}/members/remove/` | 属主移除成员。`{ "user_ids": [2] }` → `200`；属主不可移除（`400`，走转让）；被移除者会话转只读、待处理提案作废 |
| `GET /api/plays/invitations/` | 我的待接受邀请（含 `expired` 标记） |
| `POST /api/plays/invitations/accept/` | 接受邀请。`{ "invitation_id": 1 }` → `200`；过期/已处理 `409`，非本人邀请 `404` |
| `POST /api/plays/{id}/transfer-ownership/` | 属主发起转让。`{ "to_user": 2 }` → `200 { invitation_id }`；仅 active editor 可接；对方通过 accept 接受后才生效（审计记录双方） |
| `POST /api/admin/plays/{id}/review/` | 平台管理员只读审核快照（每次调用写 AuditLog） |
| `POST /api/admin/plays/{id}/freeze/`、`/api/admin/plays/{id}/unfreeze/` | 平台管理员冻结/解冻。`{ "reason": "..." }`（必填）；冻结后生成/发布入口返回 `403` 业务码 `4003` |

### POST /api/plays/{id}/approve/
审核通过并恢复流水线。`{ "review_point": "OUTLINE", "comment": "ok" }` → `200` Play
`review_point` ∈ `OUTLINE | EPISODE_SCRIPT | CHARACTER_REF | ASSETS | COMPLETED`

### POST /api/plays/{id}/reject/
审核驳回并回退重派。`{ "review_point": "OUTLINE", "comment": "重写", "episode": 1 }` → `200` Play
- 仅在对应暂停态（`PAUSED_AT_OUTLINE / PAUSED_AT_SCRIPT / PAUSED_AT_ASSETS`）可驳回，否则 `409`；并发重复驳回只有一个生效。
- `episode` 必须是本剧分集的 id，否则 `400`（不再静默回退到第 1 集）。

### POST /api/plays/{id}/preview-redo/
重做影响预检（只读，读权限即可——viewer 也可查看影响范围）。`{ "granularity": "SHOT|SCENE", "target_id": 5, "asset_types": ["AUDIO"], "changes": ["dialogue"] }` → `200` 影响报告
- `granularity` 仅 SHOT/SCENE（EPISODE 无选择性重做）；`asset_types`/`changes` 可选。
- 返回：`invalidated[]`（将失效资产）、`reusable[]`（可复用资产）、`video_impact[]`（受影响成片）、`rebuild_tasks[]` + `estimated_tasks`（预计任务数）、`conservative[]`（无法精确判定的资产及原因：`legacy_asset_no_fingerprint` / `video_without_dependency_edges`——不报虚假零影响）、`preview_version`（执行 redo 时带回做版本校验）。
- `changes` 语义收窄：`["dialogue"]` 只失效 AUDIO（仅改对白不重做图片）；`["visual_prompt"/"aspect_ratio"/"style"/"character_ref"]` 只失效 IMAGE。与 `asset_types` 取交集（显式类型选择是上限）。
- 预检不写库、不派发任务、不调用模型。

### POST /api/plays/{id}/redo/
按粒度重做并级联失效（需 edit 权限）。`{ "granularity": "SHOT", "target_id": 5, "asset_types": ["AUDIO"], "preview_version": "abc123..." }` `200` Play
`granularity` ∈ `SHOT | SCENE | EPISODE`；`target_id` 为对应实体 id；参数非法返回 `400`。
- **选择性重做（Stage A3）**：SHOT/SCENE 粒度支持 `asset_types ∈ {IMAGE, AUDIO}` 子集；缺省 IMAGE+AUDIO（旧行为不变）。AUDIO-only 不调用图片模型；IMAGE 保持 READY。
- **预检版本校验**：携带 `preview_version`（来自 preview-redo）时，目标已变化返回 `409`（需重新预检确认）。
- 显式重做的生成任务跳过指纹复用（强制新版本），旧版本 SUPERSEDED 保留可回查。

### POST /api/plays/{id}/batch-generate/
智能批量生成（主计划 3.5.3.7，需 edit 权限——派发即付费）。`{ "scope": "scene|episode", "target_id": 5, "asset_types": ["IMAGE","AUDIO"] }` `200` 三桶结果
- `scope=scene` 对该场景全部镜头、`scope=episode` 对该分集全部镜头；`asset_types` 缺省 IMAGE+AUDIO。
- 逐镜头逐类型走**既有守卫链**：已有 READY 资产跳过、在跑任务去重、无对白不派配音、计费闸逐任务判定（被拒的落 FAILED TaskRecord 归 skipped 桶不吞错）。
- 响应：`{ dispatched: [{shot_id, asset_type, task_record_id}], reused: [{shot_id, asset_type, reason: "already_ready"}], skipped: [{shot_id, asset_type, reason: in_progress|no_dialogue|billing_gate, ...}], summary: {dispatched, reused, skipped, shot_count} }`。
- **不做**：跨请求合并供应商调用（IMAGE/TTS 受理即计费不可合并）、批量 redo（v1 排除——避免一次批量作废整集既有图）；批量默认不作废既有就绪资产。
- 冻结剧拒绝（`403` code 4003）；只读成员 `403`；跨剧/未知目标 `400`。

### POST /api/plays/{id}/save/
手动保存（需 edit 权限）。`{}` → `200 { saved, manifest_digest, episode_versions[], character_versions }`
- **版本快照判重**：每集调用 `snapshot_episode` 生成内容摘要，与该集 main 分支最新版本一致则复用（连续保存不制造重复版本）；有变化才创建新版本（父链指向上一版本）。角色版本同口径（无设定变化不记录）。
- **清单刷新**：保存强制原子写入项目目录 `project.json`（`schema_version=1`，字段白名单 + 稳定 UUID 引用 + 相对资源键；递归剔除凭证/token 类敏感键；不含成本/绝对路径/Agent 会话状态）。生成任务终态只标脏，延迟到保存/导出时统一刷新。
- `episode_versions[]`：`{ episode_number, version, created }`（`created=false` 表示内容未变、复用既有版本）。

### POST /api/plays/{id}/export/
项目 ZIP 导出（需 edit 权限）。`{}` → `200 application/zip`（`project-<uuid>.zip`）。
- **即时快照**：以数据库当前状态构建（不依赖可能延迟的磁盘清单）；包内含 `project.json`（`schema_version=1`）、声明的本地资产文件、`MANIFEST.sha256`（包内文件 SHA-256 清单）。
- **显式排除（响应 `X-Export-Excluded` 头明示）**：Agent 会话侧状态（会话树/提案历史/PLAY 技能库/评估指标/AgentCommunication）、cache、logs、凭证。远程 URL 资产仅保存受控元数据（`X-Export-Skipped-Remote` 计数），不请求任何 URL。
- 导出为只读操作，不改变项目状态。

### POST /api/plays/import-precheck/
导入预检（认证用户，只读零写入）。`multipart: file=<ZIP>` → `200 { project_uuid, title, schema_version, episodes, characters, assets, dependencies, files_verified, bytes_total }`
- **限额**：包内 ≤5000 文件、解压总量 ≤2GB、单文件 ≤512MB、压缩比 ≤100；超限 `400`。
- **恶意归档拒绝**：绝对路径/穿越/反斜杠/盘符/重复路径、`schema_version` 不受支持、`MANIFEST.sha256` 摘要不匹配或缺少条目、清单声明资产缺失——全部 `400` 带原因，且在 DB/目录写入**之前**失败。

### POST /api/plays/import-confirm/
确认导入为**新项目**（认证用户，新项目属主=导入者）。`multipart: file=<ZIP>` → `200 { play_id, project_uuid, title, episodes, characters, assets_local, assets_remote }`
- **同 UUID 冲突 → `409`**（v1 不覆盖既有项目；不承诺旧项目无损覆盖恢复）。
- 恢复语义：结构/角色/资产/依赖按清单白名单重建；FK 与 current 指针按 UUID 映射；新项目从 `DRAFT` 起步（不继承导出时状态）；剧本/角色版本恢复 main v1 快照。
- 远程资产导入为 `PENDING` 元数据行（不生成空文件、不请求归档 URL）；缺外部资产在 `assets_remote` 中明示。完整离线包需用户显式选择下载外部资产（走安全下载白名单）。
- DB 失败只清理新建项目目录（不破坏既有项目）。

### GET /api/health/
运行健康（认证用户只读；工作台 Stage C2）。→ `200 { deployment_mode, task_backend, database{ok}, worker{alive,active_leases,stale_leases}, queue{queued,ready_now,recovery_required,recovery_records}, media{available,writable}, ffmpeg{available,version}, ffprobe{available,version}, checked_at }`
- **脱敏契约**：只返回布尔/计数/版本号，无路径、无密钥、无完整 URL。
- worker 停止时：`queue.queued` 如实计数排队任务，`worker.active_leases=0`——排队任务**不显示为生成中**（死租约计入 `stale_leases`，等待回收/恢复）。
- `recovery_records`：待恢复确认的 TaskRecord 数（付费任务崩溃后等待用户确认）。

### POST /api/task-records/{id}/cancel/
取消任务（剧内 edit 权限）。`{}` → `200 { cancelled, task_id }`
- PENDING/RETRYING/RECOVERY_REQUIRED → CANCELLED（本地执行行同步终结，不再被领取）；RUNNING 置取消标记（渲染中由 `_run` 条件更新尊重，**取消不复活**）；已终态（SUCCESS/CANCELLED）`400`。

### POST /api/task-records/{id}/retry-failed/
失败重派（剧内 edit 权限）。`{}` → `200 { retried, new_task_id }`
- 仅 FAILED 任务可重派；创建关联新 TaskRecord（`input_summary.retry_of` 指向原记录，审计链保留）。
- RECOVERY_REQUIRED 不走此入口（须先 `issue-recovery-token` + `confirm-recovery`）。

### POST /api/task-records/{id}/issue-recovery-token/
签发一次性恢复令牌（剧内 edit 权限）。仅 RECOVERY_REQUIRED 记录。→ `200 { task_record_id, recovery_token, expires_at, disclosure[] }`
- **令牌只回显一次**（库里只存 SHA-256 hash 与到期时间）；有效期内重复签发拒绝（避免多活令牌）；24h 有效。
- `disclosure` 披露中断轮次已写入的部分产物：ASSISTANT_TURN 披露已写入的会话消息（`session_id`）；生成类披露付费请求已受理但产物未落库。

### POST /api/task-records/{id}/confirm-recovery/
确认恢复重试（剧内 edit 权限）。`{ "recovery_token": "..." }` → `200 { recovered, original_record_id, new_record_id, new_execution_uuid, disclosure[] }`
- 令牌校验：不匹配/过期/重复使用/跨用户 → `400`（权限不足 `403`）；确认即作废（一次性）。
- 创建**关联新执行**（新 TaskRecord `recovery_of` + 新 LocalTaskExecution PENDING，worker 自动领取）；原记录保留审计（FAILED + 恢复说明），不复用原 TaskRecord。

### POST /api/plays/{id}/clear-cache/
清理项目缓存（需 manage 权限/owner）。`{ "target": "cache" }`（缺省 cache）→ `200 { cleared, files_removed, bytes_freed }`
- 仅接受 `cache`（白名单）；路径逃逸 / 其他目录（exports/assets/logs）返回 `400`。
- 只作用于本项目 `projects/<play_uuid>/cache/` 子树；原始资产、剧本版本、已导出成片不受影响。

### POST /api/plays/{id}/publish/
全剧发布触发。`{ "channel": 1 }`（可选，缺省取首个启用渠道）→ `200` `{ publication_ids[], play_status }`
要求所有集已完成，否则 `400` `{ "detail": "有 N 集未完成，无法发布" }`。

---

## 3. 生产树资源（PlayScoped，按归属隔离）

标准 CRUD。权限 `IsPlayAuthorizedOrAdmin`：GET 需 view，写需 edit；分集的 POST/DELETE 需 manage。
子实体通过向上解析到所属 Play 继承访问权限；写入时请求体中每个父级外键指向的短剧都要校验。

| 资源 | 路径 | 关键字段 |
|---|---|---|
| 分集 | `/api/episodes/` | play, episode_number, title, synopsis, status, duration_seconds, video_url, current_video_asset, scenes[], subtitle_tracks[], created_at, updated_at |
| 场景 | `/api/scenes/` | episode, scene_number, location, time_of_day, description, mood, status, shots[], created_at, updated_at |
| 分镜 | `/api/shots/` | scene, shot_number, shot_type, visual_prompt, dialogue, dialogue_character, action_note, emotion_tag, duration_estimate, status, current_image_asset, current_audio_asset, created_at, updated_at |
| 角色 | `/api/characters/` | play, name, role, description, appearance_prompt, reference_image, voice_profile, voice_profile_id, lora_model_id, created_at, updated_at |

---

## 4. 资产 `/api/assets/`、依赖、素材库

### 资产 `/api/assets/`
标准 CRUD（PlayScoped）。
字段：`id, play, episode, scene, shot, character, asset_type, version(只读自增), status, artifact_url, thumbnail_url, metadata, ai_provider, ai_model, prompt, parameters, cost, depends_on[](只读), depends_on_ids[](写), created_at, updated_at`
- 版本自动递增：同位置新 READY 自动 SUPERSEDED 旧版本，并级联失效下游依赖。
- `depends_on_ids`：写入时重建 `AssetDependency` 关系，`relation_type` 自动推断。

### POST /api/assets/preview-generate/
低分辨率预览草稿图生成（主计划 3.5.3.5，需 edit 权限）。`{ "shot_id": 5 }` → `201 { task_record_id, shot_id, preview: true }`
- **低分辨率参数下发**：`size` 取 `ASSET_PREVIEW_SIZE`（默认 512x512，9:16 自动换 512x896）、`quality=low`（OpenAI 兼容适配器透传）——供用户低成本确认画面方向后再决定正式重做。
- **预览行完全隔离**：产出 IMAGE 资产行**不挂 shot/character/episode FK**（`metadata.preview_of_shot` 记目标）、空指纹、`metadata.preview=true`——不进镜头版本链、不动 `current_image_asset` 指针、不被合成取用、不触发下游派发。
- 派发复用 IMAGE_GEN 通道：计费闸/在跑去重/冻结剧拒绝全部生效；`input_summary` 带 `preview=True` 标记。
- 同镜头已有进行中预览任务 `409`；`ENABLE_ASSET_PREVIEW` 关闭 `404`（默认关）；他剧镜头 `404`（不泄露存在性）；冻结剧 `403` code 4003；只读成员 `403`。
- 正式生成入口不变：预览满意后走既有 redo 或提案确认。

### GET /api/assets/{id}/compare/
资产版本对比（主计划 3.5.3.6，剧内读权限——viewer 可用）。
- `?against={other_id}`：同位置（play/episode/scene/shot/character/asset_type 六元组）两版本结构化对比 → `{ identical, version_a, version_b, asset_a, asset_b, status_a, status_b, cost_a, cost_b, cost_delta, created_at_a/b, field_diffs[] }`。
- `field_diffs`：`artifact_url/prompt/ai_model/parameters/metadata/cost` 字段级差异（`metadata` 摘要级比较不展开深层——媒体探针 JSON 可能很大）；**status 排除**（同位置旧版本被自动 SUPERSEDED，status 必然不同属版本机制噪声；顶层单独给 status_a/b）。
- **无 against 参数** → 该位置全部版本清单 `{ versions: [{id, version, status, cost, created_at, artifact_url, prompt}] }`（含 SUPERSEDED 历史版本），前端先列后选。
- 跨位置/跨剧 `400`（不泄露他剧存在性）；对比目标不存在 `404`。
- 前端消费入口：资产版本对比面板（先列后选）、预览确认流（预览行 vs 正式图对比）。

### GET /api/assets/{id}/download/
成片/预览鉴权下载（剧内读权限；工作台 Stage D1）。仅 `VIDEO` / `PREVIEW`。
- **本地存储（storage_key）**：`200` MP4 `FileResponse`（不包 JSON 信封）；文件名 `video-<uuid>.mp4` / `preview-<uuid>.mp4`（无用户可控字符）。
- **shared 私有 S3**：`302` 短期只读签名 URL（300s，受控 SDK 生成，仅对项目存储键签名）；服务器不代理/拉取任何外部 URL。
- **REMOTE 无受控副本**：`410 { reason: remote_no_object }`——明确不可下载，不静默替换。
- `?version=<n>`：仅 VIDEO 支持显式历史版本（含 SUPERSEDED 可回查）；不存在 `404`、非法值 `400`。**历史版本不自动跳转**：不带 version 的已取代资产返回 `410 not_ready`。
- PREVIEW 响应带 `X-Artifact-Kind: preview`（预览不冒充正式成片）；VIDEO 不带。
- 非 VIDEO/PREVIEW `400`；非成员 `404`（归属隔离）；匿名 `401`；路径穿越/缺失文件 `410`（不泄露服务器路径）。
- 前端消费入口：成片下载按钮（`asset_type=VIDEO` 且 READY）、预览下载（带 `X-Artifact-Kind` 识别）、历史版本对比下载（`?version=`）。

### 资产依赖 `/api/asset-dependencies/`
标准 CRUD（按 `downstream__play` 隔离）。字段：`upstream, downstream, relation_type`

### 素材库 `/api/asset-library/`
标准 CRUD。字段：`name, asset_type, file_url, thumbnail_url, metadata, is_public, owner`
可见性：公共条目所有认证用户可读，私有条目仅 owner 可读。修改/删除仅 owner（无管理员放行）；所有认证用户均可上传。

### 声线 `/api/voice-profiles/`
标准 CRUD（仅 admin）。字段：`name, gender, provider, voice_id, parameters, preview_url`

---

## 5. AI 配置 `/api/`

### 供应商 `/api/ai-providers/`（仅 admin）
字段：`id, name, provider_type, adapter_type, base_url, api_key_encrypted, is_active, priority, rate_limit, timeout_seconds, retry_count, models[], created_at`

### 模型 `/api/ai-models/`（仅 admin）
字段：`id, provider, model_name, display_name, model_type, capabilities, cost_config, is_active, created_at`

### 配方 `/api/agent-configs/`（认证用户；平台管理员可见全量，普通用户仅自己 + 模板）
字段：`id, name, description, llm_model, tts_model, image_model, video_model, llm_model_id, tts_model_id, image_model_id, video_model_id, llm_failover, tts_failover, image_failover, video_failover, retry_count, retry_backoff, fallback_on_limit, default_audit_mode, default_redo_granularity, consistency_mode, prompt_templates, style_params, is_template, created_by, created_at`
- `*_model_id`：写侧外键；`*_failover`：降级链 JSON（模型 id 列表）。
- 创作助手：`assistant_model`（读）/ `assistant_model_id`（写）、`assistant_failover`、`assistant_budget` `{max_cost, max_iterations, max_tool_calls}`（默认不限费用 / 8 次迭代 / 20 次工具调用）。为空时回退 `llm_model` / `llm_failover`。所选模型须在 `capabilities` 中设置 `supports_tools: true`，否则 `400`。
- **多级助手模型（Phase 3.3 成本优化）**：`assistant_models` `{small, medium, large}`（AIModel pk）。
  助手轮按本轮任务复杂度选档：<0.4 → small、0.4-0.7 → medium、>0.7 → large；
  档位未配置回退 `assistant_model`（行为与单档一致）。摘要类辅助调用
  （上下文压缩/分支摘要）优先用 small 档（`assistant_compaction.summary_model` pk 配置优先级更高）。

#### POST /api/agent-configs/{id}/clone/
克隆配方为私有副本（`is_template=False`，归属当前用户）→ `201` 新 AgentConfig

---

## 6. 运营记录

| 资源 | 路径 | 关键字段 |
|---|---|---|
| 任务记录 | `/api/task-records/` | play, episode, scene, shot, asset, task_type, celery_task_id, status, ai_provider, ai_model, input_summary, output_summary, error_message, retry_count, cost, started_at, completed_at |
| 审核记录 | `/api/review-logs/` | play, review_point, episode, reviewer, decision, comment, modifications, created_at |
| 成本记录 | `/api/cost-records/` | play, task_record, provider, model, provider_type, usage, unit_cost, total_cost, currency, created_at |

均为只读（PlayScoped，仅 GET）；由系统写入，POST/PUT/PATCH/DELETE 返回 `405`。

---

## 7. 字幕 `/api/subtitle-tracks/`

标准 CRUD（PlayScoped）。字段：`id, episode, language, source_type, status, asset, subtitle_data, review_count, created_at, updated_at`
- 更新 `subtitle_data` 时自动 `review_count + 1` 且状态置 `REVIEW`。

### POST /api/subtitle-tracks/{id}/approve/
审核通过为 READY → `200` Track

### GET /api/subtitle-tracks/{id}/export/srt
导出 SRT（`Content-Type: application/x-subrip`，附件下载）

### GET /api/subtitle-tracks/{id}/export/vtt
导出 VTT（`Content-Type: text/vtt`，附件下载）

---

## 8. 分发 `/api/`

### 发布渠道 `/api/distribution-channels/`（仅 admin）
标准 CRUD。字段：`id, name, platform, api_config, format_requirements, is_active, created_at`

### 发布记录 `/api/publications/`（PlayScoped）
读需 view；创建、修改、删除、`sync`、`rendition` 均需 manage（editor `403`）。
标准 CRUD。字段：`id, episode, channel, status, video_asset, subtitle_track, title, description, tags, cover_url, platform_video_id, platform_url, reject_reason, published_at, created_at, updated_at`

#### POST /api/publications/
创建发布并做格式校验（不合法返回 `400` `{ "detail": "<原因>" }`）。

#### POST /api/publications/{id}/sync/
模拟平台状态轮询/回调：
```json
{ "status": "reviewing", "platform_video_id": "vid_123" }
```
`status` ∈ `uploading | reviewing | published | rejected`
- `published` 带 `platform_url`
- `rejected` 带 `reject_reason`
- 未知状态 / 非法转换返回 `400`

---

## 9. AI Gateway `/api/ai-gateway/`（仅 admin）

### GET /api/ai-gateway/status/
Provider 健康看板：
```json
{
  "providers": [
    {
      "id": 1, "name": "DeepSeek", "type": "LLM",
      "state": "CLOSED", "healthy": true,
      "supports_tools": true, "tool_models": ["deepseek-chat"],
      "stats": { "total_calls": 10, "success_rate": 0.9, "avg_latency_ms": 1200, "total_cost": 0.12 },
      "rate_limit": { "current_rpm": 3, "max_rpm": 60, "current_concurrent": 1, "max_concurrent": 4 }
    }
  ],
  "failover_events_24h": [ { "time": 1700000000.0, "from": 1, "to": null, "reason": "timeout" } ]
}
```
`state` ∈ `CLOSED | OPEN | HALF_OPEN`（三态熔断器）

### GET /api/ai-gateway/adapters/
可选适配器列表：`{ count, adapters: [ { "adapter_type": "deepseek", "label": "DeepSeek 深度求索" } ] }`

#### SmartCache 模型响应缓存（Phase 3.3，开关 `ENABLE_SMART_CACHE` 默认关闭）
- **只缓存纯文本对话（CHAT）**：LLM/CHAT 可重放；IMAGE/TTS/VIDEO 受理即计费不可缓存。
- 精确命中：`cache_key = SHA-256(规范化 model+messages+system)`；TTL 过期惰性清理（`SMART_CACHE_TTL_S=3600`），
  行数上限 `SMART_CACHE_MAX_ROWS=500`（写时顺带清理最旧）。命中返回原响应并递增 `hit_count`，不产生新 CostRecord。
- 语义近似命中（开关 `ENABLE_SEMANTIC_CACHE` 默认关闭）：同模型下字符 bigram Jaccard 相似度
  >`SEMANTIC_CACHE_THRESHOLD`（默认 0.85）命中。零依赖近似方案（不引入向量化模型）；
  中文近重复实测 0.75-0.82、无关文本 ≈0，0.85 为干净分界。关闭时查询侧完全不执行。
- 失败安全：缓存层任何异常降级为直连（不阻断调用）；两开关关闭时零查询零写入，行为与建设前一致。

支持的供应商（按模态）：
- **LLM**：OpenAI 兼容（openai / deepseek / qwen / sensenova / zhipu / kimi / wenxin / doubao / hunyuan / minimax / gemini / ollama / siliconflow / openrouter / groq / custom_openai）+ Claude（专有协议）。填好 base_url + api_key 后可实时拉取模型列表。
- **图像**：DALL·E 3 / gpt-image-1（OpenAI 兼容）、即梦 Seedream（火山 Ark）、Flux（BFL）、自定义 OpenAI 兼容图像端点；SD（ComfyUI）暂为占位。
- **语音**：CosyVoice、Azure Speech、Edge TTS（免 Key）、OpenAI TTS、火山豆包（Ark）、MiniMax 海螺、自定义 OpenAI 兼容语音端点。
- **视频**：可灵 Kling（JWT 鉴权，api_key 存 `access_key:secret_key`）、海螺 MiniMax（base_url 需带 `?GroupId=xxx`）。两者均为异步提交 + 内部轮询，同步返回结果。

### GET /api/ai-gateway/providers/{id}/models/
拉取该供应商可用模型 → `200` `{ source, models, existing }`
- `source`：`live`（LLM 通过 `GET {base_url}/models` 实时拉取）| `static`（图像/语音/视频用内置已知清单；LLM 拉取失败也回退此项）。
- `models`：`[ { model_name, display_name, model_type, capabilities?, cost_config? } ]`。
- `existing`：该供应商下已导入的 model_name 列表，供前端标记。
- 始终可手动填 `model_name` 兜底，本端点只是可选下拉。

### POST /api/ai-gateway/providers/{id}/models/import/
批量导入模型为 `AIModel` → `200` `{ imported, skipped }`
- body `{ "model_names": ["kling-v1", ...] }`；缺省 `model_names` 时导入全部「尚未导入」的已知/实时模型。
- 幂等：已存在的 model_name 跳过（进入 `skipped`），不会重复创建。
- 元数据（display_name / model_type / cost_config）优先取内置清单，缺失时用 provider 的 `provider_type` 兜底。

### POST /api/ai-gateway/providers/{id}/test/
连通性测试 → `200` `{ success, latency_ms, error }`
- 适配器未实现 / 调用失败时 `success=false` 且附 `error`。
- LLM / 图像使用该供应商第一个启用的模型测试；没有启用模型时 `success=false`。
- 图像 / 视频测试会产生一次真实生成费用，需传 `{"confirm_cost": true}`，否则返回 `success=false, requires_confirm=true`。
- 视频测试传 `wait=false`，只验证任务提交、不阻塞轮询（避免挂起）。
- body `{"mode": "tools"}`（仅 LLM）：用该供应商首个 `supports_tools` 模型做一次工具调用往返，返回附 `supports_tools`、`stop_reason`。

---

## 9.5 创作助手 `/api/assistant/`

对话式修改大纲 / 剧本 / 场景 / 镜头。agent 只能读取数据和生成**提案**，提案经用户 apply 后才落库。每轮对话是一条 `ASSISTANT_TURN` 任务记录，费用计入 `CostRecord`（`provider_type=LLM`），在费用看板可见。

### 会话 `/api/assistant/sessions/`
- 仅创建者可读写，admin 可读；创建与发消息需对该短剧有写权限（viewer `403`）
- `POST /` `{play_id, title?}` → `201`；`GET /?play=` 列表
- `GET /{id}/` → 会话字段 + `messages`（content blocks）+ `running_task_id` + `last_cursor`。刷新页面后用 `last_cursor` 继续订阅事件
- 字段：`id, play, user, title, status, running_task_id, total_cost, total_tokens, created_at, updated_at`

#### POST /api/assistant/sessions/{id}/messages/
`{text}` → `202` `{task_record_id, cursor, seq}`
- 上一轮未结束 → `409`；会话费用达 `max_cost` → `409` `{code: "budget_exceeded"}`；超过 10 条/分钟 → `429`
- 计费派发闸（`ENABLE_BILLING_HOLD=true` 时）：钱包余额不足 → `402` `{reason: "insufficient_points"}`；并发槽位满 → `409` `{reason: "quota_exceeded"}`。local 模式闸短路，不返回这两个错误。collaborate 端点同语义。

#### GET /api/assistant/sessions/{id}/events/?cursor=&wait=
长轮询，`wait` 最多 25 秒 → `{events: [{id, type, data, ts}], cursor}`。下一次请求带上返回的 `cursor`。
`type` ∈ `message | tool_call | tool_result | proposal | span | eval | tool_error | error | budget_exceeded | cancelled | done`，每轮以 `done` 结束。
其中 `span`（model_call/tool_call 关键 span，含 usage/cost 与韧性画像）、`eval`（eval 钩子结果）、`tool_error`（工具确定性终止，含 error_class/stop_reason）为可观测性事件，前端可忽略而不影响核心对话流。
事件流保留 24 小时，持久数据以 `messages` 为准。未配置 Redis 时事件流为进程内实现，仅在 Celery eager（同进程）模式下可跨请求推送。

#### POST /api/assistant/sessions/{id}/cancel/
取消运行中的轮次 → `200`；没有运行中的轮次 → `409`。循环在下一次迭代前退出。

#### POST /api/assistant/sessions/{id}/feedback/（Phase 3.2.8 协作决策反馈）
`{turn?, rating, comment?}` → `200 {rating, comment, turn}`
- `rating` 1-5 整数必填；`comment` ≤500；`turn` 缺省取该会话最近一个协作轮（无 → `404`）
- 同轮重复提交覆盖（upsert）；仅会话属主（非属主 `404`，按属主隔离与既有会话端点一致）
- **注入语义**：本会话最近 `FEEDBACK_PROMPT_RECENT`（默认 3）条 rating ≤2 或带评论的反馈，
  确定性拼接进后续轮次 system prompt（"用户对近期协作的反馈"段）；无反馈时 prompt 与建设前逐字节一致
- 反馈不进 `project.json` 导出（工作台边界：Agent 会话侧状态不导出）

#### GET /api/assistant/sessions/{id}/turns/{record_id}/trace/
取某轮的可观测性 trace（会话属主或 admin；轮次须属于该会话）→ `200` `{task_record_id, status, trace}`。
- `trace = {trace_id, started_at, finished_at, outcome, totals, spans, evals}`
- `totals = {model_calls, tool_calls, tool_retries, tool_repairs, input_tokens, output_tokens, cost}` —— 按工具调用拆解的 token/费用归因
- `spans` 为扁平列表（`parent_id` 指向所属 turn/model_call），`kind ∈ turn | model_call | tool_call`；`model_call` 的 `attributes.triggered_by_tool` 标明该次调用由哪个工具结果触发
- `tool_call` 的 `attributes` 含 `attempts / retries / repaired / error_class / fallback_chain`（韧性层画像）
- 轮次未结束或尚未产出 trace → `200` `{task_record_id, status}`（无 `trace`，不视为错误）

### 工具（29 个）
| 类别 | 工具 |
|---|---|
| 查询 | `get_play_overview` `get_outline` `get_episode_script` `get_scene` `get_shot` `get_characters` `search_asset_library` `get_task_status` `get_cost_summary` |
| 网络 | `web_fetch`（抓取白名单网站文本，开关 `ENABLE_TOOL_REGISTRY` 且 `WEB_FETCH_ALLOWED_HOSTS` 非空时注册；fail-closed：未配白名单即不出现不执行） |
| 流水线感知 | `pipeline_status`（状态快照：当前阶段/分集进度与素材就绪度/待审核/进行中任务/恢复提示）；`pipeline_next_steps`（下一步建议，描述性不自动执行） |
| 字段提案 | `propose_outline_edit` `propose_episode_script_edit` `propose_scene_edit` `propose_shot_edit`（含对白角色、景别、时长、音效）；`propose_episode_script_edit` 另支持 `batch_shot_descriptions`（批量镜头改写，开关 `ENABLE_BATCH_GENERATION` 开启时合并为单次请求生成、各镜头拆为独立提案并带 `payload.batch_info` 元数据；关闭时降级为拼接单条） |
| 结构提案 | `propose_shot_insert` `propose_shot_delete` `propose_scene_create` |
| 角色提案 | `propose_character_edit` `propose_character_create` |
| 生成请求 | `request_generation`（`redo_shot` / `redo_scene` / `preview_episode`，需用户 apply 才派发） |
| 审查 | `review_episode`（确定性检查，返回问题清单） |
| 交互 | `ask_user`（结构化提问，本轮随即结束） |
| 记忆 | `memory_read` `memory_save`（带 revision）；`memory_record_save` `memory_record_search`（结构化条目，开关 `ENABLE_LAYERED_MEMORY` 开启时） |
| 灵感库 | `inspiration_save` `inspiration_search` `inspiration_recommend`（Phase 3.5.1，开关 `ENABLE_INSPIRATION_LIBRARY` 开启时；关闭时零暴露） |
| 多方案 | `propose_multiple`（Phase 2.4.2，开关 `ENABLE_MULTI_PROPOSAL` 开启时一次生成 ≤3 个风格差异方案、落同一备选组提案；竞争开关 `ENABLE_AGENT_COMPETITION` 同开时每方案 critic 审稿打分，**组内最高分**者带 `payload.confidence`（/100），供渐进自主判定，其余方案不带分不自动应用） |

事件 `type` 另有 `question`：`data = {tool_use_id, questions: [{id, prompt, type: text|single|multi, options?, required?}]}`。收到后本轮以 `done {outcome: "awaiting_user"}` 结束，用户回答作为下一条普通消息发送。

### 提案 `/api/assistant/proposals/`
- 短剧成员可见；`GET /?play=&session=&status=`
- 字段：`id, session, message, play, kind, target_asset, target_episode, target_scene, target_shot, target_character, target_label, base_version, base_snapshot, payload, summary, impacted_asset_ids, impacted_summary, status, applied_asset, applied_by, applied_at, result, created_at`
- `kind` ∈ `OUTLINE | EPISODE_SCRIPT | SCENE | SHOT | SHOT_CREATE | SHOT_DELETE | SCENE_CREATE | CHARACTER_EDIT | CHARACTER_CREATE | GENERATION`；`status` ∈ `PENDING | APPLIED | DISCARDED | STALE`
- `result`：apply 后的结果。`SHOT_CREATE` → `{shot_id, shot_number}`；`SCENE_CREATE` → `{scene_id, scene_number}`；`CHARACTER_CREATE` → `{character_id}`；`SHOT_DELETE` → `{deleted_shot_number}`；`GENERATION` → `{task_record_ids}`
- `GENERATION` 的 `payload = {action, target_id, estimated_tasks}`，`estimated_tasks` 为预计派发任务数（每镜头图片 1 + 有对白时配音 1；预览 1）
- `payload.confidence`（0-1，可空）：多方案竞争择优分数换算，唯一确定性置信度来源——仅在渐进自主判定中消费
- `payload.auto_applied`（bool，仅自动路径）：渐进自主自动应用标记；用户手动 apply 不带（见"信任分级与渐进自主"小节）

#### POST /api/assistant/proposals/{id}/apply/
`{redo?: bool}` → `200` 提案
- 无写权限 `403`；短剧不在 `PAUSED_AT_*` / `COMPLETED` → `409`；提案非 PENDING → `409`
- 目标在提案后被修改 → 提案置 `STALE`，`409`
- 大纲 / 剧本提案生成新版本资产（旧版本 SUPERSEDED）；场景 / 镜头提案同步结构化字段（镜头有分镜资产时一并出新版本）
- 应用**不会**触发重新生成，也不会自动通过审核；继续流水线仍用原审核按钮
- 插入镜头：其后镜头编号顺延；删除镜头：其素材标记 SUPERSEDED（保留历史）后删除，其后编号前移；结构在提案后有变化 → STALE `409`
- `redo=true`（会产生费用）：镜头 / 插入镜头 → 重做该镜头素材；场景 → 重做该场景；新增场景 → 生成分镜；剧本 → 逐场景重做；角色 → 失效旧定妆照并重新生成。大纲、删除镜头、生成请求不支持 → `400`
- 生成请求 apply 即派发任务；重做类受上述状态门禁，`preview_episode` 不受门禁，但同集已有进行中的预览 → `409`

### 长期记忆 `/api/assistant/memories/{play_id}/`
每部短剧一份，跨会话共享；不会自动注入 system prompt。
- `GET` → `{content, revision, updated_at}`（短剧成员可读；尚无记忆时 content 为空）
- `PUT {content, revision}` → `200`；需写权限（viewer `403`）；`revision` 必须是最近一次读取的值，否则 `409` 并返回当前 `{content, revision}`；content 上限 20000 字

### 结构化记忆条目（Phase 3.4，开关 `ENABLE_LAYERED_MEMORY` 默认关闭）

主文档记忆之外的机器可检索层：agent 维护的单条跨会话记忆（类型/内容/重要性），
按关键词命中 + 重要性加权检索。v1 无独立 REST 端点，全部经助手工具消费
（`memory_record_save` / `memory_record_search`，关闭时工具零暴露）。

**与 memory_save 分工**：单条可检索的偏好/决策/事实用 `memory_record_save`（结构化，
自动注入 system prompt 的 L3 段）；需用户查看与编辑的全文主文档用 `memory_save`（现有契约不变）。

- `memory_record_save {kind, content, importance?}`：`kind ∈ PREFERENCE|DECISION|FACT`，
  content ≤2000 字，importance 0-1（默认 0.5）；同 `(play, kind, content哈希)` 重复保存自动去重（仅刷新）
- `memory_record_search {query, limit?}`：检索本剧启用条目，返回 `{records: [{id, kind, content, importance}], count}`
- **L3 注入**：system prompt 的「结构化记忆条目」段按任务复杂度分档注入（`MEMORY_L3_TIERS` 默认 3/5/8 条，
  复杂度 <0.4 / 0.4-0.7 / >0.7），随机定界符隔离（正文围栏不可逃逸，与主文档记忆同标准）
- **自动沉淀**：轮次成功结束且含用户确认信号（提案被 apply / memory_save 成功 / 用户消息含确认词）时，
  从对话提取 ≤`MEMORY_SETTLE_MAX_RECORDS`（默认 3）条记忆条目（LLM→启发式降级→静默失败，提取调用计入轮次预算）
- **压缩重要性保护**：上下文压缩时，重要性 ≥`MEMORY_IMPORTANT_THRESHOLD`（默认 0.8）的组
  （含 memory_save 写回、提案 apply、用户消息等）不进入摘要区（划入保留区，宁可多保留 token）；
  全组高分的极端情况退回纯位置语义保证压缩可行；压缩条目记录 `protected_groups` 计数（可观测）
- 关闭时：工具零暴露、L3 空段、沉淀不执行、压缩纯位置语义——与建设前逐项一致

#### POST /api/assistant/proposals/{id}/discard/
→ `200`；无写权限 `403`；非 PENDING `409`

### 灵感库 `/api/inspirations/`（Phase 3.5.1）

跨会话可复用的创意素材（"写什么"），与 SkillLibrary（方法论"怎么写"）、
MemoryRecord（本剧工作状态）正交。`play` 为空 = GLOBAL 条目（全员可读）。

- `GET /?play=&category=&q=`：GLOBAL 全员可读；PLAY 条目本剧成员可读。
  `q` 对 content/category 模糊匹配
- `POST /` `{play?, category, content, rating?}`：PLAY 条目需本剧写权限（viewer `403`）；
  GLOBAL 条目仅平台管理员可建；`user` 由服务端注入（`source=manual`）
- `PUT/PATCH /{id}/`、`DELETE /{id}/`：本人或本剧写权限（GLOBAL 条目限创建者与平台管理员）
- 字段：`id, play, user, category, content, source(manual|agent), rating(1-5 默认 3), used_count, created_at, updated_at`
- `category` ∈ `plot_twist | dialogue | scene | character | other`；content ≤2000
- REST 不受 `ENABLE_INSPIRATION_LIBRARY` 开关限制（开关只控 agent 工具面，与 skills API 同型）
- agent 侧三工具（开关开启时）：`inspiration_save`（本剧会话内保存，`source=agent`）、
  `inspiration_search`（GLOBAL+本剧合并，命中数×10+评分加权排序）、
  `inspiration_recommend`（按当前内容推荐，引用传 `mark_used=true` 计数递增）

### 审查反馈 `/api/assistant/review/feedback/`（Phase 3.5.2，开关 `ENABLE_AGENT_REVIEW` 默认关闭）

对 Agent 审查报告的逐条反馈，驱动后续审查的维度权重调整。

#### POST /api/assistant/review/feedback/
`{episode, rating, issues_feedback?, review_log?}` → `201`
- `episode` 必填；本剧成员可提交（非成员 `403`）；`rating` 1-5
- `issues_feedback = [{issue_key, agree, suggestion?}]`：`issue_key` 为报告 issues 数组的
  位置性键（`det-{i}` / `{dimension}-{i}`）；`agree` 布尔必填
- 同报告重复提交覆盖（upsert `(episode, user, review_log)`）
- 开关关闭时 `404`
- **权重注入语义**：`review_report` 前聚合本剧近 `REVIEW_FEEDBACK_WINDOW_DAYS`（默认 30）天
  反馈，disagree ≥ agree 的维度注入审查 prompt（"仅在明确时报告"提示）；无反馈空串，
  prompt 与建设前逐字节一致

#### GET /api/assistant/review/feedback/trends/?play_id=
本剧成员可读 → `{play, window_days, dimensions: {dim: {agree, disagree}}, recent: [≤10 条]}`
- 开关关闭时 `404`；非成员 `404`
- `review_report` 报告尾部附同口径 `feedback_trends` 段

### 工具注册表 `/api/assistant/tools/`（Phase 3.1，开关 `ENABLE_TOOL_REGISTRY` 默认关闭）

数据库存储的纯函数自定义工具：handler 契约为顶层 `run(args: dict) -> dict`（JSON 进 JSON 出），
仅允许 import 白名单（`json math re random statistics itertools collections datetime unicodedata textwrap`），
在隔离子进程执行（`python -I` + builtins 裁剪 + 墙钟超时 5s + 输出 64KB 上限）。
**沙箱无网络、无文件、无数据库访问、无派生进程能力（白名单不含相应模块）；
Windows 下无内存/CPU 配额，隔离强度以墙钟超时与输出上限为准。**
注册表工具不接触会话/短剧数据（纯函数），调用计入既有工具预算与韧性计数；
沙箱失败（超时/崩溃/超限）为确定性错误，不重试。

- `GET /`：管理员见全部；普通用户见自己的 + verified 官方工具。开关关闭时 `{"tools": [], "enabled": false}`
- 字段：`id, tool_name, tool_type(OFFICIAL|CUSTOM), category, description, input_schema, is_verified, is_active, version, mine, created_at, updated_at`；`handler_code` 仅作者与平台管理员可读（他人不回源码）
- `POST /`：创建（登录用户，`tool_type` 固定 CUSTOM，非管理员请求 OFFICIAL `403`）；创建即静态检查（语法/白名单/危险调用/run 契约/schema 结构），失败 `400`；重名（注册表或内置工具）`400`；默认停用；限流
- `PATCH /{id}/`：更新（作者/管理员）；须带 `expected_version`，不匹配 `409`；检查失败整体拒绝
- `DELETE /{id}/`：删除（作者/管理员）
- `POST /{id}/activate/` / `deactivate/`：启停（作者/管理员）。停用生效延迟 ≤ `REGISTRY_CACHE_TTL`（默认 60s，缓存过期后工具面/执行不再可见）
- `POST /{id}/verify/`：置 verified（仅平台管理员；verified 是企业期完整审核流的基础）
- `POST /{id}/try_run/`：试运行 `{args}`（作者/管理员）；走同一沙箱（超时减半 2s），返回 `{ok, result|error}`，不产生提案、不接触会话；限流

**可见性**：对模型可见的注册表工具仅为"自己的 CUSTOM（已激活）"或"verified OFFICIAL（已激活）"；
他人未验证工具不可见，调用按未知工具处理（不泄露存在性）。
**注入边界**：工具 description 会进入模型上下文，作者自伤风险由作者自负；verified 流程是企业期人工审查闸门。

#### chain_tools 工具（开关开启时注册进内置工具面）

模型可调用 `chain_tools` 串联执行注册表工具：`{chain: [{tool, args?}, ...], stop_on_error}`，
至多 `CHAIN_MAX_STEPS`（默认 5）步；步骤只能引用注册表工具（引用自身/重复引用被拒绝）；
每步收到上一步结果（`prev` 键，预定义 prev 的步骤被拒绝）；链计 1 次工具调用、
各步各计一次预算（不可绕过 `max_tool_calls`）；任一步失败返回已完成的部分结果与 `failed_at`。
开关关闭时该工具不出现、注册表零暴露（工具面/执行回退/API 写路径均不生效）。

### 工具市场与生态（Phase 3.1 市场侧，复用开关 `ENABLE_TOOL_REGISTRY`）

市场 = verified OFFICIAL 条目的公开目录；安装为**复制式**（副本独立生命周期，
源变更/下架不追溯影响副本；取新版 = 删除副本重装）。市场源即本地 verified
OFFICIAL 条目（跨机市场同步属企业扩展期）。

#### 市场列表 `GET /api/assistant/tools/marketplace/?category=&q=`
- 全部 verified OFFICIAL 条目（`category` 精确过滤、`q` 对 tool_name/description 模糊）；
  每条附 `avg_rating`（两位小数字符串，无评分为 null）/ `rating_count` / `install_count`
  / `has_docs`；不含 handler_code
- 开关关闭时 `{"tools": [], "enabled": false}`

#### 安装 `POST /api/assistant/tools/{id}/marketplace/install/`
- 市场条目 → 用户本地 CUSTOM 副本：复制 handler/schema/描述/分类，溯源
  `installed_from` 指向源；**副本名 = 源名 + `__u{用户id}`**（tool_name 全局唯一，
  装后可 PATCH 改名）；默认停用，`activate/` 后对模型可见
- 同源重复安装幂等返回既有副本（200 `{installed: false, id}`）；副本名被占用 `409`
- 源非 verified OFFICIAL `409`；副本走既有管理端点（启停/更新/删除/试运行）无特权路径

#### 评分 `POST /api/assistant/tools/{id}/rate/` body `{rating: 1-5, comment?}`
- (tool, user) 唯一，重复评分 upsert 全量覆盖；comment ≤200 可置空撤评
- 自评 `400`；非可见工具（他人未验证/未激活）`404` 不泄露存在性；限流同消息节流
- 响应 `{rating, comment, avg_rating}`；工具详情回显 `my_rating`（未评 null）

#### 审核流（仅平台管理员；动作全部 append-only 落审计 `ToolReviewLog`）
- `POST /{id}/submit/`（作者，note 可选）：送审表达意图，进审核队列
- `POST /{id}/verify/` `{note?}`：置 verified + 缓存失效
- `POST /{id}/reject/` `{note}`（**必填**，仅未 verified 条目）：停用 + 驳回原因回显作者
  （响应与详情的 `review_action`/`review_note`，仅作者/管理员可见）
- `POST /{id}/revoke/` `{note}`（**必填**，仅已 verified 条目）：下架——清 verified + 停用 +
  缓存失效；已安装副本不受影响
- `GET /api/assistant/tools/reviews/`（仅平台管理员）：审核队列 = 最新动作为 SUBMIT
  且其后无 VERIFY/REJECT/REVOKE 的条目（附送审时间与送审附注）

#### 文档 `GET /api/assistant/tools/{id}/docs/`
- 从元数据确定性生成 Markdown 使用文档（参数表/示例入参/执行边界），
  零 LLM 调用、不含 handler 源码；返回 `{"doc": "<markdown>"}`
- 权限：作者/管理员任意自己可管理的条目；verified OFFICIAL 对全部登录用户；
  他人未验证条目 `404`

#### 官方种子 `manage.py seed_official_tools`
- 预填充 10 个纯函数官方工具（文本/格式/数据处理类，全部走既有沙箱，
  OFFICIAL + verified + 默认停用）；幂等（tool_name 存在即跳过不更新）
- Web 搜索/GitHub/Notion 专用适配器与社区工具联网属企业扩展期（web_fetch 是地基）

#### web_fetch 出站安全链
`scheme 校验 → 域名白名单精确匹配 → check_url（拒内网/保留/云 metadata）→
请求 + check_peer（DNS rebinding 防护）→ 重定向逐跳重复全部校验（≤3 跳）→
字节上限截断（`truncated` 标记）`。失败一律确定性错误不重试。白名单配置：
`WEB_FETCH_ALLOWED_HOSTS`（逗号分隔精确 host，空 = 禁用）、`WEB_FETCH_TIMEOUT_S=8`、
`WEB_FETCH_MAX_BYTES=65536`。

### 实时事件通道（Phase 3.2，开关 `ENABLE_REALTIME_EVENTS` 默认关闭）

开启后提供 WebSocket 推送、运行中中断与指令队列三项能力；关闭时所有行为与建设前一致
（WS 路由不挂载、messages 运行中保持 `409`、事件仍走长轮询）。WSGI 部署无 WS 路由，
需以 ASGI（daphne/uvicorn 等）启动；local 模式下事件跨进程可见（SQLite 事件表），
shared 模式跨进程推送仍需 Redis（无 Redis 时退化为进程内事件流 + 长轮询）。

#### WebSocket `/api/assistant/sessions/{id}/ws/?token=<JWT>&cursor=<id>`
- 连接：`wss://.../api/assistant/sessions/{id}/ws/?token=<access_token>[&cursor=<事件id>]`
- 鉴权：JWT access token 通过 query 参数传入（浏览器 WS 无法自定义 header）。
  **注意 token 会出现在反代/访问日志中**：生产环境应缩短 access token 有效期、
  避免日志落盘 query string，或仅在受信网络启用该开关
- 归属：仅会话创建者可连接。开关关闭 `4404`；token 无效 `4403`；非本人/会话不存在 `4403/4404`
- cursor：从指定事件 id 之后开始回放；缺省（或非法值）从头回放 backlog。事件格式与
  长轮询一致：`{id, type, data, ts}`（type 集合见 events 端点）。回放保证不丢不重——
  断线后用最后收到的事件 `id` 作为 cursor 重连即可
- 推送节奏：实时事件 50ms 级到达（SQLite 轮询分片），空批每秒静默轮询，不发心跳帧
- 允许同一会话多连接（刷新页面场景）；读取幂等，cursor 各自独立

#### WS 客户端消息：interrupt
```json
{"type": "interrupt"}
```
→ `{"type": "interrupt_ack", "task_record_id": 1, "status": "CANCELLED"}`，与
`POST /sessions/{id}/cancel/` 同一语义（复用 TaskRecord 取消，不抢占进行中的模型调用）：
- 轮次 RUNNING → 置 CANCELLED，循环在下一次迭代点退出
- 轮次 PENDING → 直接收尾，随后推送 `cancelled` 与 `done {outcome: "cancelled"}`
- 无运行中轮次 → `{"type": "error", "detail": "没有运行中的轮次", "code": 409}`
- 未知消息类型 → `{"type": "error", "detail": "未知消息类型 ..."}`

#### 运行中指令队列（messages 的运行中分支）
轮次运行中提交 `POST /sessions/{id}/messages/` 时：
- 开关开启且队列未满 → `202` `{queued: true, task_record_id, seq, message}`，
  落库为 `queued_on` 当前轮次的 USER 消息（不派发新轮次），同时事件流推送
  `queued {seq, text}`；当前轮次下一次模型迭代前并入用户内容（与首条消息合并，不产生连续 user 块）
- 队列已满（`TURN_QUEUE_MAX`，默认 3，只数未消费指令）→ `409` `{reason: "queue_full"}`
- 开关关闭 → 保持既有 `409 "上一轮对话尚未结束"`
- 写权限/限流校验与普通发消息一致；轮次被取消后未消费指令不进入后续轮次历史（保留审计标记）

#### SQLite 事件后端语义（local 模式）
- 事件持久化到 `AssistantEvent` 表：`(session, stream_id)` 唯一，ID 为"毫秒-序号"（对齐 Redis Stream）
- 保留策略与 Redis 相同：每会话最多 1000 条（MAXLEN 截断）、24 小时 TTL（写入时顺带清理）
- 读侧为 50ms 分片轮询（SQLite 无跨进程通知原语）：实时事件端到端延迟为数百毫秒级，
  比长轮询 25s 批次显著更及时，但不承诺亚 50ms 推送
- Redis 配置（`AI_GATEWAY_STORE=redis`）时优先 Redis，连接失败降级进程内实现（记日志，不影响轮次）

#### 前端消费清单（对接 `shortplayweb`）
1. WS 连接管理：token query 鉴权、断线用最后事件 id 重连（不丢不重）、开关关闭回退长轮询
2. 事件渲染：`queued`（指令入队提示）、`cancelled`/`done {outcome: "cancelled"}`（中断反馈）
3. interrupt 按钮：发送 `{"type": "interrupt"}`，按 `interrupt_ack` / `error {code: 409}` 更新 UI
4. 运行中输入框：`202 {queued: true}` 时 UI 显示"已排队"，`409 queue_full` 提示队列已满

#### 前端消费清单：反馈与灵感库（对接 `shortplayweb`，Phase 3.2.8 / 3.5.1 / 3.5.2）
1. 协作轮反馈表单：轮次结束后展示 1-5 星 + 评论输入，`POST /sessions/{id}/feedback/`；
   重复提交覆盖（无需先查再改）
2. 灵感库管理页：`GET/POST/PATCH/DELETE /api/inspirations/`（?play=&category=&q= 过滤）；
   GLOBAL 条目仅平台管理员可建（UI 对普通用户隐藏全局新建入口）
3. 审查报告逐条反馈：报告 issues 旁 agree/disagree + 建议输入，
   `POST /api/assistant/review/feedback/`；趋势图读 `GET .../trends/?play_id=`
4. 权重提示文案：低分反馈注入 system prompt 属后端行为，前端无需展示注入内容

### 信任分级与渐进自主（Phase 3.2.5 / 3.2.6，开关 `ENABLE_PROGRESSIVE_AUTONOMY` 默认关闭）

信任分由提案采纳事件确定性累积（无 LLM 参与）：提案被采纳 `+0.02`、被丢弃或
过期 `-0.01`（`TRUST_STEP_APPLY` / `TRUST_STEP_DISCARD`），幂等（同一提案仅首次
终态迁移计一次）且夹紧 `[0,1]`，初始 `0.5`（`TRUST_INITIAL_SCORE`）。无手工
设置端点——刷分路径的唯一入口是提案生命周期本身。

#### GET /api/assistant/trust/
本人信任分级（无记录时返回默认初值，不落库）：
```json
{"trust_score": 0.52, "auto_approve_threshold": 0.8, "level": "standard"}
```
- `level` ∈ `cautious`(<0.3) / `standard`(0.3-0.7) / `trusted`(>0.7)——纯展示分档，无行为差异
- `auto_approve_threshold`：行级阈值优先，空取 `TRUST_DEFAULT_THRESHOLD`（0.8）

#### 渐进自主语义
开关开启时，提案创建即评估自动批准，四条件全部满足才直接应用（省略的只是
"用户点击"这一步，全部既有校验——状态门禁/过期判定/版本一致性——照常执行）：
1. `ENABLE_PROGRESSIVE_AUTONOMY` 开启
2. `payload.confidence ≥ AUTO_APPROVE_MIN_CONFIDENCE`（0.8；confidence 唯一确定性
   来源是多方案竞争择优分数 `/100`，普通单路径提案不带 → 不自动批准）
3. 用户 `trust_score ≥ auto_approve_threshold`
4. 提案非删除类（`SHOT_DELETE` 排除——不可逆操作不自动化）

行为细节：
- 自动应用成功 → `status=APPLIED` 且 `payload.auto_applied=true`（用户手动 apply
  **不**带此标记，前端据此显示"已自动应用"角标）
- 被状态门禁拒 → 保持 `PENDING` 等待用户确认（不丢提案）
- 因过期被拒 → 按既有过期语义置 `STALE`（过期提案本就不该再应用）
- 自动应用照常累积信任分（正反馈闭环）
- 组内多方案：仅最高分者带 confidence——自动应用不会打断"多方案对比待选"
- 开关关闭 → 所有提案照旧 `PENDING`，行为与建设前完全一致

#### GET /api/assistant/realtime/config/
实时通道降级判据（主计划 3.2.12）：
```json
{"enabled": true, "mode": "ws"}
```
- `enabled=ENABLE_REALTIME_EVENTS`；`mode = enabled ? "ws" : "polling"`
- 对所有认证用户可用，**不受**实时通道开关限制（关闭时恰是前端要探测的场景）

#### 前端消费清单：信任分级与降级探测（对接 `shortplayweb`）
1. 启动时 `GET /api/assistant/realtime/config/`：`enabled=true` 先试 WS（失败自行
   回退长轮询）；`false` 直接长轮询
2. 个人页/会话侧栏展示信任分与分档标签（`GET /api/assistant/trust/`，只读）
3. 提案列表对 `payload.auto_applied=true` 的 APPLIED 提案显示"已自动应用"角标；
   其余 APPLIED 提案显示正常"已应用"

#### 前端消费清单：资产生成优化（对接 `shortplayweb`，Phase 3.5.3）
1. 镜头编辑面板"预览画面"按钮（`POST /api/assets/preview-generate/`）：低分辨率
   草稿图返回后按 `metadata.preview_of_shot` 关联展示（预览行不挂 shot FK，
   不能靠 shot 导航）；确认满意后引导用户走既有 redo 或提案
2. 资产版本对比面板：先 `GET /api/assets/{id}/compare/`（无 against 拿版本清单
   含 SUPERSEDED），用户选定后带 `?against=` 拿字段级 diff 与费用差额
3. 场景/分集"批量生成素材"入口（`POST /api/plays/{id}/batch-generate/`）：
   三桶结果分列展示（已派发任务 id 可跳转任务记录 / 已就绪跳过 / 各守卫原因）
4. 开关探测：`ENABLE_ASSET_PREVIEW` 关闭时预览按钮隐藏（端点 `404` 即未启用）

#### 前端消费清单：小说工作区（对接 `shortplayweb` `frontend-creative-workbench-expansion` 一期）
1. 卷章树导航 + 章节编辑器：自动保存状态指示 + `snapshots` 列表做版本回看侧栏；
   `outline` 锚点渲染为可点击跳转正文字符区间
2. 软删除交互：DELETE 响应的 `adaptation_refs > 0` 时弹"该章被改编引用过"确认；回收站视图用 `include_deleted=true`
3. 设定库页：五类 tab + 类型化字段动态表单（后端 400 按字段定位错误）；PERSON 绑定既有角色选择器
4. 关系图谱：`graph/?chapter_number=N` 做时点滑杆，边与事件随章节切换重渲染
5. 助手提案卡：`STORY_ASSET` kind 的提案（新建/更新设定）在设定库页可直接应用（复用既有 ProposalCard）

#### 前端消费清单：工作台扩展（对接 `shortplayweb` `frontend-creative-workbench-expansion` 二至五期）
1. 剧本排版：导出按钮三格式（`fmt=fountain|txt|pdf`，参数名是 `fmt` 非 `format`）；
   历史版本下拉（`script_version_id`）导出快照
2. 无限画布：React Flow 节点拖拽（`PATCH x/y` 节流保存）、`versions` 渲染生图流
   侧栏版本列表、`generate` 触发后按 `dispatched/skipped` 分列提示
3. 导演台：俯视布景平面按 `ground_plan` 渲染网格；机位/摆位/运镜拖放
   （`path` polyline 画布绘制）；镜头绑定面板 `camera_description` 实时预览；
   构图标注抽帧检查（`composition-check` append 追加模式）
4. 剪辑时间线：六轨固定轨道面（`GET /api/tracks/?composition=`，轨空也渲染）；
   片段拖动改 `start`、边缘拉伸改 `in_point/out_point`；分割按钮
   （`clips/split` 播放头位置）；素材版本切换下拉（`switch-version`，六元组同位置
   才可选）；字幕轨双击编辑 `text`
5. 剪辑导出：导出前先拿 `problems` 明细（400 时逐片段定位展示）；通过后
   `task_record_id` 轮询（render 槽位时长参考合成任务）；完成后从
   `Asset(VIDEO, metadata.edited_from_composition)` 取产物播放/下载
6. 导入入口：剧集详情页"导入自动剪辑"按钮（`POST /api/compositions/import/`，
   幂等可重复点击）→ 跳转剪辑时间线

#### 小说→剧本改编（2026-11-23 组 2 novel-adaptation）

改编长任务走 TaskRecord（进度轮询既有任务记录端点）+ 提案闸（产出不静默落库）。

- **POST /api/plays/{id}/novel/adapt/** `{from_chapter?, to_chapter?, episode_start?=1}`
  → `{task_record_id, from_chapter, to_chapter, block_count, record_status}`。
  权限 edit；冻结剧 4003；已有进行中改编 409；无小说/空范围 400。
  分块改编（前块摘要保连贯 + 出场人物设定注入），进度事件 `novel_adapt_progress`
  推到本剧最近助手会话的事件流（`{task_id, block, done_blocks, total_blocks}`）。
- **POST /api/plays/{id}/novel/adapt/resume/** `{task_record_id}` —— 断点续跑：
  FAILED 记录置 PENDING 重派，已完成块（`output_summary.blocks`）不重跑 LLM。
  他剧记录 404。
- **产出提案**（kind `EPISODE_SCRIPT`、payload.adapt 结构化剧集）：应用时若改编
  范围内已有剧本内容且未带 `confirm_overwrite` 则拒绝（已有剧集保护）；确认覆盖
  后按 `episode_start` 落 Episode/Scene/Shot（视觉描述/对白/动作指示）。
  改编引用的章节 `adaptation_refs` 置位（软删提示）。

#### 剧本专业排版导出（2026-11-23 组 3 script-layout）

- **GET /api/plays/{id}/script/export/?episode_id=&fmt=fountain|txt|pdf&script_version_id=**
  → 文件流（不包 JSON 信封，与成片/SRT 下载一致）。权限：剧内读成员。
  Fountain 五要素（场次标题 EXT/INT、角色大写行、对话、动作、CUT TO: 转场）；
  txt 为中文分镜式排版；pdf 走 fpdf2（中文字体自动探测系统 TTF，缺失回退拉丁）。
  `script_version_id` 给定时按历史版本快照渲染（非当前结构）。
  **注意：格式参数名是 `fmt`**（`format` 为 DRF 保留字会被内容协商吞成 404）。
  排版只读——内容修改仍走 redo/提案链路。

#### 无限画布（2026-11-23 组 4 canvas-board）

节点式画布：四类节点（分镜段落/角色定妆/资产引用/生图流）+ 坐标 + 布局父链。
节点引用既有对象（FK 不复制数据）；数据依赖复用 AssetDependency，画布 parent 仅表达布局层级。
权限复用剧内三层（读成员可看 / edit 可写 / 非成员 404）。

- **GET|POST /api/canvases/** 画布 CRUD（`{play, title}`；响应含 `node_count`）
- **GET|POST|PATCH /api/canvas-nodes/** 节点 CRUD
  `{canvas, type ∈ SCENE_BLOCK|CHARACTER_STYLING|ASSET_REF|IMAGE_FLOW, title?, x, y,
  parent?, scene?|character?|asset?, params?}`——锚与类型匹配校验（分镜段落须引用场景、
  定妆须引用角色、资产引用/生图流须引用资产）；`parent` 跨画布拒绝；
  响应含 `anchor_label`（锚名）与 `versions`（锚位置版本序列，新→旧，
  与 Asset 六元组版本链一致——生图流侧栏数据源）
- **POST /api/canvas-nodes/{id}/generate/** 节点触发生成（走既有派发闸——计费闸/
  冻结剧 4003/在跑去重/已就绪跳过，不新增守卫语义）：
  - SCENE_BLOCK → 引用场景逐镜头 `dispatch_image_and_tts`
  - CHARACTER_STYLING → 无既有定妆时 `dispatch_character_ref`
  - IMAGE_FLOW → 锚镜头 `dispatch_image_and_tts`
  - ASSET_REF → 400（无生成语义）
  → `{dispatched: [task_record_id], skipped: [{shot_id, reason: in_progress|already_ready}]}`

#### 导演台 2D（2026-11-23 组 5 director-stage）

2D 布景平面（机位点/角色摆位/运镜路径 polyline）+ 镜头绑定 + 构图检查标注。
数据形状对 3D 升级开放（平面图 JSON）；权限复用剧内三层。

- **GET|POST /api/director-scenes/** 导演台场景 CRUD
  `{play, title?, ground_plan?}`；`ground_plan` 为尺寸网格 JSON
  `{width, height, grid?}`（单位米，正数校验）；响应含 `element_count`。
- **GET|POST|PATCH /api/director-elements/** 元素 CRUD
  `{scene, type ∈ CAMERA|PLACEMENT|CAMERA_MOVE, label?, x, y, heading?, path?, move_instruction?, character?}`
  - CAMERA：`heading` 朝向角（度，0=正北顺时针）
  - PLACEMENT：须引 `character`（仅此类型可引）
  - CAMERA_MOVE：`path` 为 `[[x, y], ...]` polyline + `move_instruction` 运镜指令文本
  - 过滤：`?scene=&type=`
- **GET|POST|PATCH /api/shot-directing/** 镜头绑定（每镜头一条，OneToOne）
  `{shot, camera?, camera_move?, composition_notes?, reference_frames?}`
  - `camera`/`camera_move` 类型匹配校验（错类型 400）且与镜头同剧
  - `composition_notes` 构图检查标注（字符串列表）
  - `reference_frames` 参考帧 `{"first": url, "last": url}`（first 即镜头图本身，一般只配 last）
  - 响应含 `camera_description`（机位视角+运镜合并文本）
  - **POST /api/shot-directing/{id}/composition-check/** 构图标注
    `{notes: [...], append?: true}`——append 省略为覆盖
- **参考帧注入视频生成**（自动，无新端点）：镜头有绑定时，合成内的图生视频
  调用自动注入机位视角/运镜指令（`prompt` 参数）与尾帧（`last_frame_image`），
  走既有 VIDEO 派发闸与计费闸，不改变降级 Ken Burns 语义。

#### 多轨剪辑（2026-11-24 组 6 timeline-editing）

六轨剪辑工程（视频/叠加/对白/音效/音乐/字幕）——自动合成导入为初始态、
手动精修后 FFmpeg 重导出（复用既有渲染原语，产物落 Asset 体系）。权限复用剧内三层。

- **GET|POST /api/compositions/** 工程 CRUD
  `{play, episode, title?}`；响应含 `track_kinds`（已有轨类型）与 `clip_count`；
  `import_source` 只读（自动导入来源 VIDEO Asset id）。
- **POST /api/compositions/import/** 自动合成导入
  `{episode_id}` → 取该集最新 READY 合成产物一键生成六轨工程初始态（视频轨=镜头图、
  对白轨=配音、字幕轨=对白文本条目、音乐轨=BGM；片头片尾不上轨）。
  幂等：同一来源资产重复导入返回既有工程。无产物 400；冻结 4003。
- **POST /api/compositions/{id}/clips/split/** 片段分割
  `{clip_id, offset}`（素材内秒）→ 原片段改写为左段，右段新建挂 `parent`
  （分割追溯链完整）；同轨后续片段序自动顺延。分割点越界 400。
- **GET|POST /api/tracks/** 轨 CRUD——每工程每类轨至多一条（重复 400）；
  `?composition=` 过滤；响应含 `clips` 嵌套。
- **GET|POST|PATCH /api/clips/** 片段 CRUD
  `{track, asset?, parent?, label?, start, in_point, out_point, text?, order?}`
  - 非字幕轨须引素材；字幕轨 asset / `text` 二选一
  - `out_point < in_point` 400；`parent` 跨轨拒绝
  - **POST /api/clips/{id}/switch-version/** 素材版本切换 `{asset_id}`——
    目标须同六元组位置且 READY；父引用链保留（响应含 `asset_version`）
- **POST /api/compositions/{id}/export/** 手动精修导出
  先跑导出前校验：空引用 / 引用素材非 READY（含 SUPERSEDED/FAILED）/ 出点越界
  → 400 + `{problems: [{clip_id, track, code, detail}]}` 明细阻断。
  通过则走既有派发闸派 `TIMELINE_EXPORT`（render 槽位；在跑 409；冻结 4003），
  → `{task_record_id, status}`；worker 侧再次校验后 FFmpeg 按工程渲染
  （视频轨 cut 拼接 + 对白/BGM 总线 + 字幕烧录），产物落 `Asset(VIDEO)`
  （`metadata.edited_from_composition` 溯源），走既有成片下载端点。
  进度轮询走既有 TaskRecord 端点。

---

### 小说工作区（2026-11-23 组 1 novel-workspace）

小说卷章树 + 章节自动版本快照 + 多维设定库 + 实体关系图谱。权限复用剧内三层（读成员可看 / edit 成员可写 / 非成员 404）。

#### POST /api/novels/ — 建小说容器（一剧一小说 OneToOne）
`{play, title}` → 201；重复创建同剧小说 400。

#### GET|POST /api/novel-volumes/ — 卷 CRUD
`{novel, title, order?}`；`order` 缺省自动排（该小说最大卷序 + 1）。响应含 `chapter_count`（不含软删章节）。

#### GET|POST|PATCH|DELETE /api/novel-chapters/ — 章节 CRUD
`{volume, chapter_number?, title, content, outline}`；`chapter_number` 缺省自动排全书连续序号（跨卷最大 + 1）。
- `outline` 为大纲锚点列表 `[{text, start, end}]`（正文字符区间，含 end；结构校验）
- DELETE 为**软删除**：响应 `{id, is_deleted: true, adaptation_refs}`——被改编引用过的章节保留引用计数提示
- `include_deleted=true` 查询参数可列出软删章节；`POST /api/novel-chapters/{id}/restore/` 恢复
- 正文变化保存时**自动产生版本快照**（保留数量 `NOVEL_SNAPSHOT_KEEP` 默认 20，0=不裁剪）
- `GET /api/novel-chapters/{id}/snapshots/` — 快照列表（新→旧）；`GET /api/chapter-snapshots/{id}/` 单条回看

#### GET|POST /api/story-assets/ — 设定库（五类）
`{play, type, name, fields, character?, chapter_refs?}`；`type ∈ PERSON|FACTION|SKILL|FORESHADOWING|LOCATION`。
- `fields` 类型化校验：键受该类型 schema 约束（如 PERSON 必填 `identity`，允许 `identity/personality/appearance/goal/arc/notes`）
- 仅 `PERSON` 可绑 `character`（既有角色，供定妆/一致性消费）；`chapter_refs` 关联章节
- 过滤：`?type=PERSON&q=关键词`（名称模糊）

#### GET|POST /api/entity-relations/ — 关系图谱
`{play, from_character, to_character, relation_type, start_chapter?, end_chapter?, note}`；事件子资源 `RelationEvent`（`{relation, chapter, change, detail}`）。
- `GET /api/entity-relations/graph/?chapter_number=N` — **时点过滤**：仅返回该时点生效的关系（起始章 ≤ N 且未终止或终止章 ≥ N），事件也截到 ≤ N；不传则全量

#### 助手工具三件套（自动注册）
`story_asset_read`（按名/类型检索设定）/ `story_asset_search_chapters`（按关键词检索章节正文，可限章节区间）/ `story_asset_propose`（写设定提案：`action=new|update`，走既有提案/审核闸，应用后落 StoryAsset——提案 kind `STORY_ASSET`）

---

## 10. 分析统计 `/api/analytics/`（仅 admin）

### GET /api/analytics/plays/{id}/cost/
单剧成本分解

### GET /api/analytics/cost/by-provider/?start_date=&end_date=
按供应商分组（可选日期过滤）

### GET /api/analytics/cost/by-model/?start_date=&end_date=
按模型分组（可选日期过滤）

### GET /api/analytics/cost-optimization/report/?month=YYYY-MM（Phase 3.3 成本优化）
月度成本优化报告（缺省当月）。数据全部来自既有表，无数据返回零计数：
- `{month, total_calls, total_cost, avg_cost_per_call}`
- `cache`: `{entries, hit_count, hit_rate}` —— SmartCache 累计命中数（月度精确增量需每日快照，留企业扩展期）
- `models`: 按模型聚合的 `[{model, call_count, total_cost}]`（降序）
- `batch`: `{batch_count, shot_count, estimated_saved_calls, estimated_saved_cost}` —— 批量提案节省估算

### GET /api/analytics/production/stats/
生产统计（短剧/集/镜头/素材/任务完成率等）

### GET /api/analytics/publications/summary/
发布状态汇总

---

## 11. 公开区 `/api/public/`（匿名只读）

面向未登录访客（作品广场 `/explore`、观看页 `/watch/:playId`）。**不鉴权**：`AllowAny` + `authentication_classes=[]`，本地存着过期 token 也不会被 401 拦下。一律只读 **PUBLISHED** 数据，走统一信封与 `CustomPagination`。序列化器为独立白名单，不复用业务序列化器（绝不含 `created_by` / 成本 / 内部状态）。

### GET /api/public/plays/?genre=&search=&ordering=&page=&limit=
已发布短剧列表（分页信封 `data` 为数组）。
- `genre` 按题材过滤；`search` 命中标题/梗概（不区分大小写）；`ordering` ∈ `-published_at`（默认）| `-play_count`
- `published_at` / `play_count` 后端暂无来源：分别恒为 `null` / `0`（前端为 0 不显示）；`ordering` 用 `-id` 做确定性兜底，避免刷新顺序抖动
- `PublicPlay` 字段：`id, title, genre, synopsis, total_episodes, cover_url, published_at, play_count`

### GET /api/public/plays/{id}/
单部短剧公开详情 = `PublicPlay` 全字段 + `episodes` + `characters`。
- `episodes` 仅已完成集（`status=COMPLETED`），按 `episode_number` 升序；字段 `id, episode_number, title, duration_seconds, video_url, cover_url`
- `characters` 字段 `id, name, role, description`（`role` 为 `CharacterRole` 枚举）
- 不存在或**未发布** → **404**（非 403），不向匿名访客泄露「内容存在但你看不到」

### GET /api/public/stats/
平台公开统计，口径仅 PUBLISHED：`{plays, episodes, total_duration_seconds, genres:[{genre, count}]}`。
- `episodes` / `total_duration_seconds` 仅统计已完成集

---

## 12. Django Admin `/admin/`

内置后台，管理所有模型与用户角色。

---

## 13. 端到端生产流程示例

1. **注册/登录**：`POST /api/auth/register/` → 拿 `access`。
2. **建配方**：`POST /api/agent-configs/`（绑定 llm/image/tts/video 模型）。
3. **建短剧**：`POST /api/plays/`（`agent_config_id` 传入，触发 Play 状态机）。
4. **触发生产**：Play 进入 `OUTLINING`，Dispatcher 自动派发大纲任务（Celery）。
5. **查进度**：`GET /api/plays/{id}/` 看 `status`；`GET /api/task-records/?play={id}` 看各任务状态。
6. **审核**（若 `audit_mode` 为 KEY_CHECKPOINTS/EVERY_STEP）：`POST /api/plays/{id}/approve/` 或 `/reject/`。
7. **重做**：`POST /api/plays/{id}/redo/` 按粒度回退。
8. **发布**：`POST /api/plays/{id}/publish/` → 各集创建 Publication，再 `POST /api/publications/{id}/sync/` 推进平台状态。
9. **统计**：`GET /api/analytics/production/stats/`、`/api/analytics/cost/by-provider/`。

---

## 附：基础设施说明

- **Celery**：broker/result backend 默认 Redis（`REDIS_URL`，默认 `redis://127.0.0.1:6379/0`）。
- **熔断/限流状态**：存于 Redis，跨 worker 共享；**Redis 不可用时自动降级为进程内内存存储**（`ai_gateway/redis_client.py`），保证本地开发/单测可用。
- **任务派发**：`shortplay/pipeline/dispatcher.py` 通过 Django signal 事件驱动，逐任务 `send_task` 扇出下游。
- **视频合成**：`ffmpeg`（Ken Burns zoompan + BGM + 1080×1920 竖屏），路径见 `settings.FFMPEG_BIN`。
