# 短剧Agent独特需求设计方案

> 目标：设计短剧agent的独特功能，这些功能是成熟harness agent（Pi/Claude Code/Codex）没有的，但对短剧创作至关重要。

---

## 1. 短剧Agent的独特性分析

### 1.1 与通用Coding Agent的本质区别

| 维度 | 通用Coding Agent | 短剧Agent |
|------|------------------|-----------|
| **任务类型** | 代码编辑、调试、重构 | 创意写作、资产生成、流水线编排 |
| **修改方式** | 直接修改文件 | 提案系统（人工确认后应用） |
| **状态管理** | 代码版本控制 | 短剧生产状态机 |
| **成本敏感度** | 低（主要是API调用） | 高（图片/视频生成费用高） |
| **用户群体** | 程序员 | 编剧/创作者/非技术用户 |
| **创意需求** | 低（逻辑正确即可） | 高（需要创意、多样性） |

### 1.2 短剧Agent独有的核心需求

1. **提案系统（Proposal System）** - 所有修改需要人工确认
2. **流水线集成（Pipeline Integration）** - 与生产状态机紧密耦合
3. **多模态资产生成（Multimodal Asset Generation）** - 图片、视频、音频
4. **版本控制（Version Control）** - 剧本、角色、情节的版本管理
5. **成本控制（Cost Control）** - 生成资产的费用管理
6. **创意探索（Creative Exploration）** - 支持多个创作方案
7. **审查机制（Review Mechanism）** - 人工审核与agent辅助

---

## 2. 独特需求设计方案

### 2.1 提案系统（Proposal System）

**现状**：已实现（apps/creation/assistant/proposals.py）

**设计亮点**：
- 提案类型丰富（10种：OUTLINE, EPISODE_SCRIPT, SCENE, SHOT, SHOT_CREATE, SHOT_DELETE, SCENE_CREATE, CHARACTER_EDIT, CHARACTER_CREATE, GENERATION）
- 过期判定（stale_reason）
- 受影响资产计算（impacted_assets）
- 事务性应用（apply_proposal）

**可以借鉴成熟Agent的改进**：

#### 2.1.1 提案树（Proposal Tree）

**问题**：当前提案是线性的，如果用户想比较多个方案，需要创建多个会话。

**设计**：
```
提案树结构：
├── Proposal A: 方案1（快节奏，强反转）
│   ├── status: PENDING
│   └── parent: null
├── Proposal B: 方案2（慢热型，悬念为主）
│   ├── status: PENDING
│   └── parent: null
└── Proposal C: 基于方案A的修改
    ├── status: PENDING
    └── parent: Proposal A
```

**实现**：
```python
class AssistantProposal(models.Model):
    # ... existing fields ...
    parent = models.ForeignKey('self', on_delete=models.SET_NULL, null=True, blank=True,
                               related_name='children')
    alternative_group = models.UUIDField(null=True, blank=True, 
                                         help_text='同组提案互为替代方案')
```

**API**：
```
POST /api/assistant/proposals/compare/
{
  'proposal_ids': [1, 2, 3],
  'action': 'apply_selected',
  'selected_id': 2
}
```

#### 2.1.2 提案预览（Proposal Preview）

**问题**：用户在应用提案前无法看到具体影响。

**设计**：
```
提案预览API：
GET /api/assistant/proposals/{id}/preview/

返回：
{
  'summary': '修改第3集第5场景的对白',
  'before': {
    'scene': {'scene_number': 5, 'description': '...', 'dialogue': '原对白...'},
    'affected_assets': ['image_001', 'audio_001']
  },
  'after': {
    'scene': {'scene_number': 5, 'description': '...', 'dialogue': '新对白...'},
    'diff': {
      'dialogue': {
        'old': '原对白...',
        'new': '新对白...'
      }
    }
  },
  'cost_estimate': {
    'image_regen': 2,
    'audio_regen': 1,
    'total_cost': '0.15'
  }
}
```

---

## 3. 流水线集成设计（Pipeline Integration）

**现状**：已实现基础的流水线状态机（apps/creation/pipeline/state_machine.py）

**设计亮点**：
- 状态机定义清晰（Play/Episode双层状态）
- 审核暂停态支持（PAUSED_AT_OUTLINE/SCRIPT/ASSETS）
- 重做机制（Asset SUPERSEDED + 重新派发）

**可以借鉴成熟Agent的改进**：

### 3.1 Agent感知的流水线控制

**问题**：Agent目前只能通过提案间接影响流水线，无法主动查询流水线状态。

**设计**：
```
新增Agent工具：
- pipeline_status: 查询当前流水线状态
- pipeline_next_steps: 查询下一步建议
- pipeline_blockers: 查询阻塞原因

示例：
用户：'下一步该做什么？'
Agent调用：pipeline_status()
返回：{
  'play_status': 'PAUSED_AT_SCRIPT',
  'episode_progress': [
    {'episode': 1, 'status': 'COMPLETED'},
    {'episode': 2, 'status': 'STORYBOARDING'},
    {'episode': 3, 'status': 'SCRIPTING'}
  ],
  'pending_reviews': ['episode_2_script', 'episode_3_script'],
  'next_suggestions': [
    '完成第2集分镜审查',
    '继续第3集剧本创作'
  ]
}
```

### 3.2 智能任务调度

**问题**：当前任务调度是固定的，没有根据成本/优先级优化。

**设计**：
```
任务优先级系统：
1. 用户主动请求（最高优先级）
2. 审核节点（高优先级，阻塞流程）
3. 批量生成（中优先级，可延迟）
4. 预览生成（低优先级，可跳过）

成本感知调度：
- 高成本任务（视频生成）需要用户确认
- 低成本任务（文本生成）可以自动执行
- 批量任务可以合并优化
```

### 3.3 流水线断点恢复

**问题**：流水线中断后，Agent需要知道从哪里继续。

**设计**：
```python
class PipelineCheckpoint(models.Model):
    play = models.ForeignKey(Play)
    episode = models.ForeignKey(Episode, null=True)
    stage = models.CharField()  # SCRIPTING, STORYBOARDING, etc.
    checkpoint_data = models.JSONField()  # 保存中间状态
    created_at = models.DateTimeField()

Agent工具：
- pipeline_resume: 从断点恢复
- pipeline_save_checkpoint: 保存当前状态
```

---

## 4. 多模态资产生成设计（Multimodal Asset Generation）

**现状**：已实现基础的资产生成（图片、音频、视频）

**设计亮点**：
- Asset模型支持多种类型（IMAGE, AUDIO, VIDEO, TRAILER, PREVIEW）
- AssetDependency追踪资产依赖关系
- 版本控制（Asset.version）

**可以借鉴成熟Agent的改进**：

### 4.1 资产生成策略优化

**问题**：当前资产生成是简单的重试，没有智能优化。

**设计**：
```
智能生成策略：

1. 批量优化：
   - 相同场景的镜头可以批量生成
   - 相同角色的定妆照可以复用
   
2. 成本优化：
   - 低分辨率预览 + 高分辨率最终版
   - 缓存相似prompt的结果
   
3. 质量优化：
   - 多次生成选择最佳
   - A/B测试不同prompt
   
4. 一致性优化：
   - 角色一致性（已实现reference_image/lora）
   - 场景一致性（光照、色调）
   - 风格一致性（整个短剧统一风格）
```

### 4.2 资产版本对比

**问题**：用户无法直观对比不同版本的资产。

**设计**：
```
资产对比API：
GET /api/assets/{id}/compare/?version1=1&version2=2

返回：
{
  'asset_id': 123,
  'versions': [
    {
      'version': 1,
      'url': 'https://...',
      'created_at': '2026-11-01',
      'metadata': {...}
    },
    {
      'version': 2,
      'url': 'https://...',
      'created_at': '2026-11-05',
      'metadata': {...}
    }
  ],
  'diff': {
    'prompt_changed': true,
    'cost_diff': '0.02'
  }
}
```

### 4.3 资产生成预览

**问题**：生成高成本资产前，用户无法预览效果。

**设计**：
```
预览生成：
POST /api/assets/preview/
{
  'type': 'IMAGE',
  'prompt': '...',
  'preview_only': true,
  'low_resolution': true
}

返回低分辨率预览，用户确认后再生成高分辨率版本
```

---

## 5. 版本控制设计（Version Control）

**现状**：已实现基础的版本控制（Asset.version, AssistantMemory.revision）

**设计亮点**：
- 资产版本递增（Asset._bump_version）
- 记忆版本控制（SHA-256）
- 提案过期判定（stale_reason）

**可以借鉴成熟Agent的改进**：

### 5.1 剧本版本分支

**问题**：当前剧本修改是线性的，无法保留多个版本。

**设计**：
```python
class ScriptVersion(models.Model):
    play = models.ForeignKey(Play)
    episode = models.ForeignKey(Episode)
    version = models.PositiveIntegerField()
    content = models.TextField()  # 完整剧本内容
    parent_version = models.ForeignKey('self', null=True)
    branch_name = models.CharField()  # 'main', 'alternative_1', etc.
    created_by = models.ForeignKey(User)
    created_at = models.DateTimeField()

Agent工具：
- script_branch: 创建新分支
- script_merge: 合并分支
- script_diff: 对比版本
```

### 5.2 角色版本管理

**问题**：角色设定修改后，已生成的资产需要重新生成。

**设计**：
```python
class CharacterVersion(models.Model):
    character = models.ForeignKey(Character)
    version = models.PositiveIntegerField()
    appearance_prompt = models.TextField()
    description = models.TextField()
    affected_assets = models.JSONField()  # 需要重新生成的资产列表
    created_at = models.DateTimeField()

自动影响分析：
- 角色外貌修改 → 定妆照需要重新生成
- 角色性格修改 → 对白风格可能需要调整
- 角色关系修改 → 互动场景可能需要调整
```

### 5.3 提案历史

**问题**：用户无法查看历史提案及其结果。

**设计**：
```
提案历史API：
GET /api/assistant/proposals/history/?play_id=1

返回：
{
  'proposals': [
    {
      'id': 1,
      'kind': 'EPISODE_SCRIPT',
      'status': 'APPLIED',
      'summary': '修改第3集对白',
      'applied_at': '2026-11-10',
      'applied_by': 'user123'
    },
    ...
  ],
  'statistics': {
    'total': 50,
    'applied': 30,
    'discarded': 15,
    'stale': 5
  }
}
```

---

## 6. 成本控制设计（Cost Control）

**现状**：已实现基础的成本追踪（CostRecord, TaskRecord.cost）

**设计亮点**：
- 成本记录（CostRecord）
- 预算控制（assistant_budget.max_cost）
- 成本汇总（session_cost）

**可以借鉴成熟Agent的改进**：

### 6.1 成本预测

**问题**：用户在生成资产前不知道会花多少钱。

**设计**：
```
成本预测API：
POST /api/cost/estimate/
{
  'action': 'generate_episode_assets',
  'episode_id': 3,
  'options': {
    'resolution': '1080p',
    'frame_rate': 30
  }
}

返回：
{
  'estimated_cost': '2.50',
  'breakdown': {
    'images': {'count': 10, 'cost': '1.00'},
    'audio': {'count': 10, 'cost': '0.50'},
    'video': {'count': 1, 'cost': '1.00'}
  },
  'budget_remaining': '10.00'
}
```

### 6.2 成本优化建议

**问题**：Agent没有主动建议如何降低成本。

**设计**：
```
成本优化建议：

1. 分辨率优化：
   - 预览用720p，最终用1080p
   - 省成本：30%

2. 批量生成：
   - 批量生成10张图片比单独生成便宜20%

3. 缓存复用：
   - 相同场景的背景可以复用
   - 相同角色的定妆照可以复用

4. 智能重试：
   - 失败后降低分辨率重试
   - 避免高成本的无效重试
```

### 6.3 成本告警

**问题**：成本超支时没有及时告警。

**设计**：
```
成本告警规则：
- 单次任务成本 > .00 → 告警
- 单日成本 > 0.00 → 告警
- 单月成本 > 00.00 → 告警
- 预算剩余 < 20% → 告警

告警方式：
- 系统通知
- 邮件通知
- Agent主动提醒
```

---

## 7. 创意探索设计（Creative Exploration）

**现状**：已实现基础的提案系统

**设计亮点**：
- 提案类型丰富
- 提案过期判定
- 提案比较（target_key）

**可以借鉴成熟Agent的改进**：

### 7.1 多方案生成

**问题**：Agent通常只生成一个方案，缺乏多样性。

**设计**：
```
多方案生成工具：
propose_multiple_solutions(
    task='修改第3集开头',
    count=3,
    diversity='high'  # high/medium/low
)

返回3个不同的提案：
- 方案A：快节奏，强反转
- 方案B：慢热型，悬念为主
- 方案C：喜剧风格，轻松幽默

用户可以：
- 选择其中一个应用
- 组合多个方案的元素
- 要求Agent基于某个方案继续修改
```

### 7.2 风格探索

**问题**：Agent倾向于生成相似的内容，缺乏风格多样性。

**设计**：
```
风格参数：
- pacing: fast/medium/slow
- tone: serious/comedic/dramatic
- structure: linear/non-linear/flashback
- ending: happy/sad/ambiguous

Agent工具：
- explore_style(play_id, style_params)
- generate_with_style(prompt, style)
```

### 7.3 灵感库

**问题**：Agent没有积累创意灵感。

**设计**：
```python
class Inspiration(models.Model):
    play = models.ForeignKey(Play, null=True)  # null表示全局灵感
    category = models.CharField()  # 'plot_twist', 'dialogue', 'scene', etc.
    content = models.TextField()
    source = models.CharField()  # 'agent_generated', 'user_provided', 'external'
    rating = models.FloatField()  # 用户评分
    used_count = models.PositiveIntegerField()
    created_at = models.DateTimeField()

Agent工具：
- inspiration_search(query, category)
- inspiration_save(content, category)
- inspiration_rate(id, rating)
```

---

## 8. 审查机制设计（Review Mechanism）

**现状**：已实现基础的审查（review_episode）

**设计亮点**：
- 确定性审查（review.py）
- ReviewLog记录
- 审核暂停态

**可以借鉴成熟Agent的改进**：

### 8.1 Agent辅助审查

**问题**：审查完全依赖确定性规则，缺乏主观判断。

**设计**：
```
Agent辅助审查：

1. 剧情逻辑审查：
   - 角色动机是否合理
   - 剧情转折是否突兀
   - 时间线是否一致

2. 对白质量审查：
   - 对白是否自然
   - 角色语言风格是否一致
   - 对白是否推动剧情

3. 节奏审查：
   - 开场是否吸引人
   - 高潮是否足够强
   - 结尾是否留下悬念

4. 情感审查：
   - 情感渲染是否到位
   - 观众是否会产生共鸣
   - 情感曲线是否合理
```

### 8.2 协作审查

**问题**：审查是单向的，缺乏协作。

**设计**：
```
协作审查流程：

1. Agent初审：
   - 自动检查确定性问题
   - 提出主观问题
   
2. 用户反馈：
   - 用户对Agent的审查结果进行反馈
   - 用户可以提出自己的看法
   
3. Agent学习：
   - Agent根据用户反馈调整审查标准
   - Agent记住用户的偏好

4. 持续改进：
   - 审查标准随时间演进
   - 形成团队的审查规范
```

### 8.3 审查报告

**问题**：审查结果分散，缺乏汇总。

**设计**：
```
审查报告API：
GET /api/review/report/?play_id=1&episode=3

返回：
{
  'episode': 3,
  'overall_score': 8.5,
  'issues': [
    {
      'category': 'dialogue',
      'severity': 'warning',
      'message': '第5场景对白稍显生硬',
      'suggestion': '可以增加一些口语化表达'
    },
    ...
  ],
  'strengths': [
    '开场钩子设计优秀',
    '角色关系刻画细腻',
    '节奏把控得当'
  ],
  'recommendations': [
    '建议加强第3场景的冲突',
    '可以考虑增加一个反转'
  ]
}
```

---

## 9. 实施优先级

### P0（立即实施，1-2个月）

1. **提案预览** - 用户体验提升大，实施成本低
2. **成本预测** - 避免意外费用，实施成本低
3. **Agent辅助审查** - 提升审查质量，实施成本中

### P1（中期实施，3-6个月）

4. **提案树** - 支持创意探索，实施成本中
5. **多方案生成** - 提升创意多样性，实施成本中
6. **版本分支** - 支持复杂创作流程，实施成本高

### P2（长期实施，6-12个月）

7. **灵感库** - 长期积累创意资产，实施成本高
8. **协作审查** - 团队协作支持，实施成本高
9. **智能调度** - 成本优化，实施成本高

---

## 10. 总结

短剧Agent的独特需求设计遵循以下原则：

1. **以用户为中心** - 所有设计都围绕编剧/创作者的需求
2. **成本敏感** - 时刻关注生成成本，提供优化建议
3. **创意优先** - 支持多样性和探索，而非单一方案
4. **渐进增强** - 先实现核心功能，再逐步完善
5. **学习能力** - Agent能够从用户反馈中学习

这些独特设计使短剧Agent区别于通用Coding Agent，成为专业的短剧创作工具。
