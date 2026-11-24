# Harness Agent架构对比研究：Pi vs Claude Code vs Codex CLI

> 研究日期：2026-11-11
> 目的：分析成熟harness agent的架构设计，为短剧agent架构方向提供参考

---

## 1. 执行摘要

本研究对比了三个成熟的harness agent框架，提取其架构设计的核心模式和实践：

| 框架 | 开发商 | 许可 | 设计哲学 | 核心特点 |
|------|--------|------|----------|----------|
| **Pi Agent** | Earendil Inc. | MIT | "Primitives, not features" | 树状会话、极简核心、强扩展性 |
| **Claude Code** | Anthropic | 专有 | 内置完整功能 | ReAct循环、Subagent编排、乐观执行 |
| **Codex CLI** | OpenAI | Apache-2.0 | 沙箱安全、分级自主 | 三级模式、批准门控、Docker隔离 |

---

## 2. Pi Agent架构详解

**来源**：https://pi.dev, https://pi.dev/docs/latest/how-pi-works

### 2.1 设计哲学

> "There are many agent harnesses, but this one is yours."
> "Pi is a minimal agent harness. Adapt Pi to your workflows, not the other way around."

**核心原则**：
- **Primitives, not features** - 提供原语而非功能
- **Minimal core** - 核心极简，通过extensions扩展
- **User control** - 用户控制工作流，而非被工具定义

### 2.2 Agent Loop

```
┌─────────────────────────────────────────────────┐
│ Pi Agent Loop                                    │
├─────────────────────────────────────────────────┤
│ 1. User submits message → added to active branch│
│ 2. Build model request:                          │
│    - System prompt (base + context files)        │
│    - Active branch history                       │
│    - Available tools                             │
│    - Skill descriptions                          │
│    - Model settings                              │
│ 3. Send to provider (streaming)                  │
│ 4. Process response:                             │
│    - Text → record to session                    │
│    - Tool calls → execute + record results       │
│ 5. Check continuation:                           │
│    - Tool results require another request?       │
│    - Queued messages?                            │
│ 6. If yes → start another turn                   │
│    If no → run ends                              │
└─────────────────────────────────────────────────┘
```

**特殊消息处理**：
- **Steering messages**: 在当前assistant turn后进入，中断剩余工具
- **Follow-up messages**: 在agent完成所有工作后进入
- **Aborting**: 停止当前run，返回队列消息到编辑器

### 2.3 上下文组装

```
┌─────────────────────────────────────────────────┐
│ Context Assembly                                 │
├─────────────────────────────────────────────────┤
│ System Prompt:                                   │
│   ├── Base instructions (minimal)                │
│   ├── Discovered context files (AGENTS.md)       │
│   └── Extension-added instructions               │
│                                                   │
│ Conversation History:                            │
│   └── Active branch (from session tree)          │
│                                                   │
│ Tool Definitions:                                │
│   └── All enabled tools                          │
│                                                   │
│ Skill Descriptions:                              │
│   ├── Name + brief description (startup)         │
│   └── Full instructions (loaded on-demand)       │
│                                                   │
│ User Message:                                    │
│   ├── Editor input                               │
│   ├── Prompt template expansion                  │
│   ├── Selected files                             │
│   ├── Images                                     │
│   └── Shell output                               │
└─────────────────────────────────────────────────┘
```

**关键特性**：
- **Minimal system prompt** - 最小化系统提示
- **Progressive disclosure** - Skills按需加载
- **Dynamic context** - Extensions可注入/转换上下文

### 2.4 会话存储

```
格式：JSONL文件（JSON Lines）
位置：~/.pi/agent/sessions/{working-dir}/
结构：树状（每个条目有id和parent_id）

Entry Types:
├── user_message
├── assistant_message
├── tool_call
├── tool_result
├── model_change
├── compaction (摘要)
├── branch_summary (分支摘要)
└── custom (extension-defined)

Tree Structure:
entry:  0     1     2     3      4     5
      ┌─────┬─────┬─────┬──────┬─────┬─────┐
      │ usr │ ass │ tool │ usr │ ass │ tool│
      └─────┴─────┴─────┴──────┴─────┴─────┘
        │     │     │      │     │     │
        └─────┴─────┘      └─────┴─────┘
              │                  │
              └──────────────────┘
              可以从entry 3创建新分支
```

**会话操作**：
- `/tree` - 浏览会话树，导航到历史条目
- `/fork` - 从某个条目创建新会话
- `/clone` - 复制活动分支到新会话
- `/export` - 导出为HTML或JSONL
- `/share` - 上传并获取分享链接

### 2.5 上下文压缩（Compaction）

**触发条件**：
```
context_tokens > context_window - reserve_tokens
默认 reserve_tokens = 16384
默认 keep_recent_tokens = 20000
```

**工作流程**：
```
1. Find cut point
   └── Walk backwards, accumulate tokens until keep_recent_tokens

2. Extract messages
   └── From previous kept boundary to cut point

3. Generate summary
   └── Call LLM with structured prompt
   └── Pass previous summary as iterative context

4. Append entry
   └── Save CompactionEntry with summary + firstKeptEntryId

5. Rebuild context
   └── Summary + messages from firstKeptEntryId onwards
```

**CompactionEntry结构**：
```typescript
interface CompactionEntry {
  type: "compaction";
  id: string;
  parentId: string | null;
  timestamp: string;
  summary: string;
  firstKeptEntryId: string;
  tokensBefore: number;
  usage?: Usage;
  details?: {
    readFiles: string[];
    modifiedFiles: string[];
  };
}
```

**自定义压缩**：
- Extension hooks: `session_before_compact`, `session_compact_failed`
- 可实现：topic-based compaction, code-aware summaries
- 可使用不同的摘要模型

### 2.6 扩展系统

**Extensions**（TypeScript模块）：
```typescript
// Extension factory function可以注册：
- Tools          // 自定义工具
- Commands       // 斜杠命令
- Shortcuts      // 键盘快捷键
- Providers      // 模型提供商
- Event handlers // 事件处理器
- Renderers      // 自定义渲染
- Terminal UI    // TUI组件
```

**Skills**：
```
media/skills/{skill-name}/
├── skill.md        // 指令
└── tools.py        // 工具（可选）

加载策略：
- Startup: 只加载name + description
- On-demand: 加载完整instructions + tools
- 好处: Progressive disclosure without busting prompt cache
```

**Packages**（分发）：
```bash
# 安装扩展包
pi install npm:@foo/pi-tools
pi install git:github.com/badlogic/pi-doom
```

### 2.7 接口模式

```
1. Interactive Mode
   └── 完整TUI体验

2. Print/JSON Mode
   ├── pi -p "query"      // 脚本用
   └── --mode json        // 事件流

3. RPC Mode
   ├── stdin: JSONL commands
   └── stdout: responses + events

4. SDK Mode
   └── TypeScript SDK嵌入到应用
```

### 2.8 Pi的设计取舍

**没有内置的功能**（但可以通过extensions构建）：
- ❌ No sub-agents (use tmux to spawn Pi instances)
- ❌ No plan mode (write plans to files)
- ❌ No permission popups (build your own confirmation flow)
- ❌ No built-in to-dos (use TODO.md)
- ❌ No background bash (use tmux)

**理由**：
> "Features that other agents bake in, you can build yourself. This keeps the core minimal while letting you shape Pi to fit how you work."

---

## 3. Claude Code架构详解

**来源**：https://github.com/anthropics/claude-code, 搜索结果

### 3.1 设计哲学

> "An agentic coding tool that lives in your terminal, understands your codebase, and helps you code faster."

**核心原则**：
- **ReAct-style loop** - Reason + Act循环
- **Built-in features** - 内置完整功能
- **Safety first** - 沙箱执行、权限系统
- **Optimistic execution** - 乐观执行减少延迟

### 3.2 Agent Loop

```
┌─────────────────────────────────────────────────┐
│ Claude Code Agent Loop (ReAct)                   │
├─────────────────────────────────────────────────┤
│ 1. Receive user intent/task                      │
│ 2. Plan approach using extended thinking         │
│ 3. Select appropriate tools from toolkit         │
│ 4. Execute tools in sandboxed environment        │
│    - Read-only tools: can parallelize            │
│    - Mutating tools: serialize to avoid races    │
│ 5. Observe and evaluate results                  │
│ 6. Iterate or present final answer               │
│                                                  │
│ Stop condition:                                  │
│   Assistant produces completion without          │
│   tool calls                                     │
└─────────────────────────────────────────────────┘
```

**乐观执行（Optimistic Execution）**：
```
传统模式：
  Tool 1 → Approve → Execute → Tool 2 → Approve → Execute

乐观模式：
  Tool 1 → Approve → Execute
                        ↓
                    Tool 2 (optimistic) → Approve → Execute
                    Tool 3 (optimistic) → Approve → Execute
```
**好处**：显著减少往返延迟

### 3.3 工具系统

**内置工具**：
```typescript
// Read-only (safe to parallelize)
- read_file          // 读取文件
- search_codebase    // 搜索代码库
- web_search         // 网络搜索
- bash (read-only)   // 只读bash命令

// Mutating (serialize)
- create_file        // 创建文件
- str_replace        // 字符串替换
- bash (mutating)    // 修改性bash命令
- browser            // 浏览器交互
- python_repl        // Python REPL
```

**工具分类策略**：
```
Read-only tools:
  - 可以并行执行
  - 不需要用户批准（可配置）
  
Mutating tools:
  - 必须串行执行
  - 需要用户批准（可配置）
  - 使用乐观执行优化延迟
```

### 3.4 上下文管理

**系统提示结构**（~3,500行XML）：
```
System Prompt:
├── Tone & Style
├── Environment Info
├── Tool Usage Guidelines
├── Communication Safety
├── Response Modes
└── Per-tool Detailed Docs
```

**自动压缩（Auto-compact）**：
```
触发条件：
  Free context window space < ~9%

压缩提示（8步结构化）：
  1. Primary request/intent
  2. Key technical concepts
  3. Files/sections touched
  4. Errors and fixes
  5. Problem-solving journey
  6. Pending tasks
  7. Current work
  8. Next steps
```

**Microcompact（新版本）**：
```
策略：
  - 清除旧工具结果（从最大的开始）
  - 保留最近的工具结果
  - 保留所有用户消息
  
vs 传统压缩：
  - 单一压缩事件
  - 生成一个摘要替换所有旧消息
```

**分层CLAUDE.md**：
```
├── /etc/claude-code/CLAUDE.md  (Enterprise)
├── ~/.claude/CLAUDE.md          (User)
└── ./CLAUDE.md                  (Project)

优先级：Project > User > Enterprise
```

### 3.5 Subagent编排

**MapReduce模式**：
```
Main Agent:
  1. Decompose task into subtasks
  2. Spawn subagents in parallel
  3. Each subagent gets isolated 200k-token context
  4. Aggregate results
  5. Synthesize final answer

Subagent:
  - Independent context window (200k tokens)
  - Focused on specific subtask
  - Returns results to main agent
```

**使用场景**：
- 大规模代码探索
- 并行文件分析
- 复杂重构任务

### 3.6 安全与权限

```
权限层级：
├── Read-only tools: 通常自动批准
├── Mutating tools: 需要用户批准
├── Bash commands: 根据安全性分级
└── Network access: 可配置

沙箱执行：
├── 文件系统隔离
├── 网络访问控制
└── 进程隔离
```

### 3.7 Claude Code的设计取舍

**内置功能**（与Pi对比）：
- ✅ Sub-agents (MapReduce orchestration)
- ✅ Plan mode (extended thinking)
- ✅ Permission popups (safety)
- ✅ Built-in to-dos
- ✅ Background bash

**优点**：
- 开箱即用，功能完整
- 安全性强（沙箱、权限）
- 优化好（乐观执行、microcompact）

**缺点**：
- 核心较重
- 自定义性不如Pi
- 依赖Anthropic生态

---

## 4. Codex CLI架构详解

**来源**：https://github.com/openai/codex, 搜索结果

### 4.1 设计哲学

> "A coding agent from OpenAI that runs locally on your computer."

**核心原则**：
- **Local-first** - 本地运行
- **Sandboxed** - Docker或macOS Seatbelt隔离
- **Graduated autonomy** - 三级自主模式
- **Approval gates** - 敏感操作需要批准

### 4.2 Agent Loop

```
┌─────────────────────────────────────────────────┐
│ Codex CLI Agent Loop (ReAct)                     │
├─────────────────────────────────────────────────┤
│ 1. Parse user prompt from terminal               │
│ 2. Plan subtasks using chain-of-thought          │
│ 3. Execute shell commands in sandbox             │
│ 4. Observe output → reason → act (iterate)       │
│ 5. Return final result or ask for clarification  │
└─────────────────────────────────────────────────┘
```

### 4.3 三级自主模式

```
┌─────────────────────────────────────────────────┐
│ Autonomy Levels                                  │
├─────────────────────────────────────────────────┤
│ 1. Suggest Mode                                  │
│    └── Proposes changes for approval             │
│         - Show diff                              │
│         - Wait for user approval                 │
│         - Apply if approved                      │
│                                                   │
│ 2. Auto-edit Mode                                │
│    └── Applies edits automatically               │
│         - File edits: automatic                  │
│         - Bash commands: require approval        │
│                                                   │
│ 3. Full-auto Mode                                │
│    └── Executes without confirmation             │
│         - File edits: automatic                  │
│         - Bash commands: automatic               │
│         - 仍在沙箱内执行                          │
└─────────────────────────────────────────────────┘
```

### 4.4 工具系统

**内置工具**：
```typescript
- read_file       // 读取文件
- write_file      // 写入文件
- execute_bash    // 执行bash命令
- search_web      // 网络搜索
```

**工具调用**：
- 使用function calling
- 沙箱执行（Docker或macOS Seatbelt）
- 批准门控（根据模式）

### 4.5 沙箱执行

```
Docker Sandbox:
├── 容器隔离
├── 文件系统挂载
├── 网络控制
└── 资源限制

macOS Seatbelt:
├── 系统级沙箱
├── 文件访问控制
├── 网络访问控制
└── 进程限制
```

### 4.6 认证方式

```
1. ChatGPT Account
   └── Sign in with ChatGPT (Plus, Pro, Business, Edu, Enterprise)

2. API Key
   └── 直接使用OpenAI API
```

### 4.7 Codex CLI的设计取舍

**优点**：
- 沙箱安全（Docker/Seatbelt）
- 三级自主模式（用户控制）
- 本地运行（隐私）
- 开源（Apache-2.0）

**缺点**：
- 功能相对简单
- 没有sub-agents
- 没有会话分支
- 文档相对少

---

## 5. 架构对比总结

### 5.1 核心架构对比

| 维度 | Pi Agent | Claude Code | Codex CLI |
|------|----------|-------------|-----------|
| **设计哲学** | Primitives | Built-in features | Sandboxed autonomy |
| **核心大小** | 极简 | 重量级 | 中等 |
| **扩展性** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐ |
| **开箱即用** | ⭐⭐ | ⭐⭐⭐⭐⭐ | ⭐⭐⭐⭐ |
| **安全性** | ⭐⭐⭐ | ⭐⭐⭐⭐ | ⭐⭐⭐⭐⭐ |
| **可定制性** | ⭐⭐⭐⭐⭐ | ⭐⭐⭐ | ⭐⭐ |

### 5.2 Agent Loop对比

| 特性 | Pi Agent | Claude Code | Codex CLI |
|------|----------|-------------|-----------|
| **模式** | Custom loop | ReAct | ReAct |
| **Streaming** | ✅ | ✅ | ✅ |
| **Tool parallelization** | ❌ | ✅ (read-only) | ❌ |
| **Optimistic execution** | ❌ | ✅ | ❌ |
| **Steering messages** | ✅ | ❌ | ❌ |
| **Follow-up messages** | ✅ | ❌ | ❌ |

### 5.3 上下文管理对比

| 特性 | Pi Agent | Claude Code | Codex CLI |
|------|----------|-------------|-----------|
| **System prompt** | Minimal | 3500-line XML | Standard |
| **Context files** | AGENTS.md | CLAUDE.md (3级) | - |
| **Compression** | ✅ 可自定义 | ✅ 8步结构化 | ❓ |
| **Microcompact** | ❌ | ✅ | ❌ |
| **Compaction hooks** | ✅ | ❌ | ❌ |

### 5.4 会话管理对比

| 特性 | Pi Agent | Claude Code | Codex CLI |
|------|----------|-------------|-----------|
| **存储格式** | JSONL | JSONL | - |
| **树状结构** | ✅ | ❌ | ❌ |
| **分支支持** | ✅ | ❌ | ❌ |
| **分支摘要** | ✅ | ❌ | ❌ |
| **会话导航** | /tree, /fork, /clone | /resume | --continue |

### 5.5 扩展机制对比

| 特性 | Pi Agent | Claude Code | Codex CLI |
|------|----------|-------------|-----------|
| **Extensions** | ✅ TypeScript | ✅ Plugins | ❌ |
| **Skills** | ✅ 按需加载 | ❌ | ❌ |
| **Packages** | ✅ npm/git | ✅ npm | ❌ |
| **Custom tools** | ✅ | ✅ | ❌ |
| **Custom providers** | ✅ 15+ | ❌ | ❌ |

### 5.6 安全性对比

| 特性 | Pi Agent | Claude Code | Codex CLI |
|------|----------|-------------|-----------|
| **沙箱** | ❌ (OS permissions) | ✅ | ✅ Docker/Seatbelt |
| **权限弹窗** | ❌ (build your own) | ✅ | ✅ 批准门控 |
| **自主模式** | - | - | ✅ 3级 |
| **本地运行** | ✅ | ✅ | ✅ |

---

## 6. 对短剧agent的启示

### 6.1 从Pi学习

**值得借鉴**：
1. ✅ **树状会话历史** - 支持探索替代方案（已采纳）
2. ✅ **可自定义Compaction** - 灵活的上下文管理（已采纳）
3. ✅ **Skills机制** - 按需加载能力（已采纳）
4. ✅ **Extensions系统** - 强扩展性（中期可考虑）
5. ✅ **多种接口模式** - Interactive/Print/JSON/RPC/SDK
6. ✅ **Minimal system prompt** - 减少token消耗

**不适合借鉴**：
- ❌ **过度极简** - 短剧agent需要一些内置功能
- ❌ **无沙箱** - 需要考虑安全性

### 6.2 从Claude Code学习

**值得借鉴**：
1. ✅ **乐观执行** - 减少延迟
2. ✅ **工具分类** - Read-only vs Mutating
3. ✅ **Microcompact** - 精细化上下文管理
4. ✅ **分层记忆文件** - 企业/用户/项目
5. ✅ **Subagent编排** - 复杂任务分解（中期可考虑）

**不适合借鉴**：
- ❌ **重量级核心** - 短剧agent应保持轻量
- ❌ **3500行系统提示** - 过于庞大
- ❌ **专有生态** - 应保持开放性

### 6.3 从Codex CLI学习

**值得借鉴**：
1. ✅ **三级自主模式** - 用户控制自主性
2. ✅ **沙箱执行** - 安全性
3. ✅ **批准门控** - 敏感操作需批准
4. ✅ **Local-first** - 隐私保护

**不适合借鉴**：
- ❌ **功能简单** - 短剧agent需要更多功能
- ❌ **无扩展性** - 应支持扩展

### 6.4 短剧agent架构建议

基于三个框架的研究，建议短剧agent采用：

```
┌─────────────────────────────────────────────────┐
│ 短剧agent架构（混合方案）                         │
├─────────────────────────────────────────────────┤
│ 从Pi学习：                                       │
│ ├── 树状会话历史 ✅                               │
│ ├── 可自定义Compaction ✅                         │
│ ├── Skills机制 ✅                                 │
│ ├── 项目记忆文件 ✅                               │
│ ├── Minimal system prompt ✅                      │
│ └── 多种接口模式（Interactive/API/SDK）           │
│                                                   │
│ 从Claude Code学习：                              │
│ ├── 工具分类（Read-only vs Mutating）             │
│ ├── 乐观执行（减少延迟）                          │
│ ├── Microcompact（精细化上下文管理）              │
│ └── Subagent编排（中期，复杂任务）                │
│                                                   │
│ 从Codex CLI学习：                                │
│ ├── 三级自主模式（用户控制）                      │
│ ├── 批准门控（敏感操作）                          │
│ └── 沙箱执行（如需要）                            │
│                                                   │
│ 短剧特有：                                       │
│ ├── 提案系统（人工确认流程）                      │
│ ├── 流水线引擎（state_machine + dispatcher）      │
│ └── 多模态资产生成（图片、视频、音频）            │
└─────────────────────────────────────────────────┘
```

---

## 7. 关键决策点

### 7.1 核心大小

**选项A：极简核心（Pi风格）**
- 优点：灵活、可定制
- 缺点：需要用户自己构建很多功能
- 适合：技术用户、需要高度定制

**选项B：内置功能（Claude Code风格）**
- 优点：开箱即用、功能完整
- 缺点：核心重、自定义性差
- 适合：非技术用户、快速上手

**选项C：中等核心（推荐）**
- 内置：工具调用、会话管理、上下文压缩、规划引擎
- 可选：多Agent协作、Skills、Subagents
- 适合：短剧创作的平衡需求

### 7.2 扩展机制

**选项A：Extensions系统（Pi风格）**
- TypeScript模块
- 可注册工具、命令、事件处理器
- 优点：强扩展性
- 缺点：开发成本高

**选项B：Plugins系统（Claude Code风格）**
- npm包分发
- 有限的扩展点
- 优点：易于分发
- 缺点：扩展性有限

**选项C：简单工具注册（推荐）**
- Python函数注册
- 支持自定义工具
- 优点：低门槛
- 缺点：扩展性有限（中期可升级）

### 7.3 自主性控制

**选项A：固定流程（当前）**
- 提案系统 + 人工确认
- 优点：可控性强
- 缺点：灵活性差

**选项B：三级自主（Codex风格）**
- Suggest / Auto-edit / Full-auto
- 优点：用户可控
- 缺点：实现复杂

**选项C：渐进式自主（推荐）**
- 基于信任度的自主性
- 新用户：更多确认
- 老用户：更多自主
- 优点：平衡安全性和效率

---

## 8. 结论

### 8.1 三个框架的共同点

1. **ReAct-style loop** - 所有框架都使用类似的agent循环
2. **Tool use** - 都支持工具调用
3. **Context management** - 都有上下文管理机制
4. **Session persistence** - 都支持会话持久化
5. **Local execution** - 都支持本地运行

### 8.2 三个框架的不同点

1. **设计哲学**：
   - Pi: Primitives（极简）
   - Claude Code: Built-in features（内置）
   - Codex: Sandboxed autonomy（沙箱自主）

2. **核心大小**：
   - Pi: 最小
   - Claude Code: 最大
   - Codex: 中等

3. **扩展性**：
   - Pi: 最强
   - Claude Code: 中等
   - Codex: 最弱

4. **安全性**：
   - Pi: 基础
   - Claude Code: 中等
   - Codex: 最强（Docker/Seatbelt）

### 8.3 对短剧agent的最终建议

**推荐架构**：混合方案

1. **核心**：中等大小（不是极简，也不是重量级）
2. **会话管理**：学习Pi（树状历史、分支支持）
3. **上下文管理**：学习Pi + Claude Code（可配置压缩 + microcompact）
4. **工具系统**：学习Claude Code（分类、乐观执行）
5. **扩展机制**：从简单开始（Python函数注册），中期升级
6. **自主性**：渐进式（基于信任度）
7. **安全性**：学习Codex（批准门控、沙箱可选）

**下一步**：
1. 确认架构方向
2. 更新openspec设计
3. 开始实施Phase 1

---

## 9. 参考资源

### Pi Agent
- 官网：https://pi.dev
- 文档：https://pi.dev/docs/latest
- How Pi Works：https://pi.dev/docs/latest/how-pi-works
- GitHub：https://github.com/earendil-works/pi

### Claude Code
- GitHub：https://github.com/anthropics/claude-code
- 官方文档：https://code.claude.com/docs/en/overview
- Building Effective Agents：https://www.anthropic.com/engineering/building-effective-agents

### Codex CLI
- GitHub：https://github.com/openai/codex
- 文档：https://developers.openai.com/codex
- npm：https://www.npmjs.com/package/@openai/codex

### 相关研究
- 7 LLM Agent Architectures That Scale：https://www.ibm.com/think/topics/llm-agent-architecture
- How we built our multi-agent research system：https://www.anthropic.com/engineering/built-multi-agent-research-system
