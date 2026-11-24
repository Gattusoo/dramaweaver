"""创作助手数据模型（openspec change: backend-play-assistant，D1）。

定义在 shortplay 应用内（由 shortplay/models.py 末尾导入），迁移仍属 shortplay。
"""
from django.conf import settings
from django.db import models


class SessionStatus(models.TextChoices):
    ACTIVE = "ACTIVE", "进行中"
    ARCHIVED = "ARCHIVED", "已归档"


class MessageRole(models.TextChoices):
    USER = "user", "用户"
    ASSISTANT = "assistant", "助手"
    TOOL = "tool", "工具结果"


class ProposalKind(models.TextChoices):
    OUTLINE = "OUTLINE", "大纲"
    EPISODE_SCRIPT = "EPISODE_SCRIPT", "分集剧本"
    SCENE = "SCENE", "场景"
    SHOT = "SHOT", "镜头"
    SHOT_CREATE = "SHOT_CREATE", "插入镜头"
    SHOT_DELETE = "SHOT_DELETE", "删除镜头"
    SCENE_CREATE = "SCENE_CREATE", "新增场景"
    CHARACTER_EDIT = "CHARACTER_EDIT", "修改角色"
    CHARACTER_CREATE = "CHARACTER_CREATE", "新增角色"
    GENERATION = "GENERATION", "生成请求"
    # 小说工作区（2026-11-23 组 1）：写设定提案。payload 带 action=new|update
    # 与 type/name/fields/character_id/chapter_ids；应用落 StoryAsset。
    STORY_ASSET = "STORY_ASSET", "故事设定"


class ProposalStatus(models.TextChoices):
    PENDING = "PENDING", "待处理"
    APPLIED = "APPLIED", "已应用"
    DISCARDED = "DISCARDED", "已丢弃"
    STALE = "STALE", "已过期"


class AssistantSession(models.Model):
    play = models.ForeignKey("creation.Play", on_delete=models.CASCADE, related_name="assistant_sessions")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="assistant_sessions")
    agent_config = models.ForeignKey("creation.AgentConfig", on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="assistant_sessions")
    title = models.CharField("标题", max_length=200, blank=True)
    status = models.CharField("状态", max_length=20, choices=SessionStatus.choices, default=SessionStatus.ACTIVE)
    # 当前轮次：用于同一会话同时只运行一个轮次（行锁判定，见 D8）
    running_task = models.ForeignKey("creation.TaskRecord", on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    total_cost = models.DecimalField("累计费用", max_digits=12, decimal_places=6, default=0)
    total_tokens = models.PositiveIntegerField("累计 tokens", default=0)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_assistant_session"
        ordering = ["-updated_at"]
        verbose_name = "助手会话"
        verbose_name_plural = "助手会话"

    def __str__(self):
        return self.title or f"会话#{self.pk}"


class AssistantMessage(models.Model):
    session = models.ForeignKey(AssistantSession, on_delete=models.CASCADE, related_name="messages")
    seq = models.PositiveIntegerField("序号")
    role = models.CharField("角色", max_length=20, choices=MessageRole.choices)
    # Anthropic 风格 content blocks，原样回放给模型
    content = models.JSONField("内容", default=list)
    task_record = models.ForeignKey("creation.TaskRecord", on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="assistant_messages")
    # 运行中指令队列（Phase 3.2）：轮次运行中提交的指令文本归属该轮次，
    # runner 在下一迭代前注入；消费后置空。保留审计（哪条指令属于哪个轮次）。
    queued_on = models.ForeignKey("creation.TaskRecord", on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name="queued_messages")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_assistant_message"
        ordering = ["seq"]
        unique_together = ["session", "seq"]
        verbose_name = "助手消息"
        verbose_name_plural = "助手消息"


class AssistantProposal(models.Model):
    session = models.ForeignKey(AssistantSession, on_delete=models.CASCADE, related_name="proposals")
    message = models.ForeignKey(AssistantMessage, on_delete=models.SET_NULL, null=True, blank=True,
                                related_name="proposals")
    play = models.ForeignKey("creation.Play", on_delete=models.CASCADE, related_name="assistant_proposals")
    kind = models.CharField("类型", max_length=20, choices=ProposalKind.choices)
    target_asset = models.ForeignKey("creation.Asset", on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    target_episode = models.ForeignKey("creation.Episode", on_delete=models.CASCADE, null=True, blank=True,
                                       related_name="+")
    # SET_NULL：删除镜头的提案应用后，提案记录本身必须保留
    target_scene = models.ForeignKey("creation.Scene", on_delete=models.SET_NULL, null=True, blank=True,
                                     related_name="+")
    target_shot = models.ForeignKey("creation.Shot", on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="+")
    target_character = models.ForeignKey("creation.Character", on_delete=models.SET_NULL, null=True, blank=True,
                                         related_name="+")
    # 资产类目标的版本号（OUTLINE / EPISODE_SCRIPT）；Scene/Shot 目标用 base_snapshot 判定过期
    base_version = models.PositiveIntegerField("基准版本", null=True, blank=True)
    base_snapshot = models.JSONField("基准快照", default=dict)
    payload = models.JSONField("修改内容", default=dict)
    summary = models.TextField("修改说明", blank=True)
    impacted_asset_ids = models.JSONField("受影响资产", default=list)
    status = models.CharField("状态", max_length=20, choices=ProposalStatus.choices,
                              default=ProposalStatus.PENDING)
    applied_asset = models.ForeignKey("creation.Asset", on_delete=models.SET_NULL, null=True, blank=True,
                                      related_name="+")
    applied_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    applied_at = models.DateTimeField("应用时间", null=True, blank=True)
    # 生成请求 apply 后派发的任务：{"task_record_ids": [...]}
    result = models.JSONField("应用结果", default=dict, blank=True)
    # Phase 2.4 提案树：父提案（提案链）与同组备选（多方案生成）
    parent = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="children")
    alternative_group = models.UUIDField("备选组", null=True, blank=True, db_index=True)
    alternative_label = models.CharField("方案标签", max_length=100, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_assistant_proposal"
        ordering = ["-created_at"]
        verbose_name = "助手提案"
        verbose_name_plural = "助手提案"

    def target_key(self):
        """同一目标判定键：用于新提案把旧 PENDING 提案标记为 STALE。

        插入 / 新增类提案没有唯一目标（同一场景可以先后插入多个镜头），不互相失效。
        """
        k = ProposalKind
        if self.kind in (k.SHOT_CREATE, k.SCENE_CREATE, k.CHARACTER_CREATE):
            return {"pk": self.pk or 0}
        if self.kind == k.GENERATION:
            return {"kind": self.kind, "payload__action": self.payload.get("action"),
                    "payload__target_id": self.payload.get("target_id")}
        return {
            k.OUTLINE: {"kind": self.kind},
            k.EPISODE_SCRIPT: {"kind": self.kind, "target_episode": self.target_episode_id},
            k.SCENE: {"kind": self.kind, "target_scene": self.target_scene_id},
            k.SHOT: {"kind": self.kind, "target_shot": self.target_shot_id},
            k.SHOT_DELETE: {"kind": self.kind, "target_shot": self.target_shot_id},
            k.CHARACTER_EDIT: {"kind": self.kind, "target_character": self.target_character_id},
            k.STORY_ASSET: {"kind": self.kind, "payload__story_asset_id": self.payload.get("story_asset_id")},
        }.get(self.kind, {"kind": self.kind})


class ScriptVersion(models.Model):
    """剧本版本分支（Phase 2.4.3）：内容快照 + 分支名 + 父版本。

    content: {"text": 剧本正文, "scenes": {场景号: {字段: 值}}}
    """
    play = models.ForeignKey("creation.Play", on_delete=models.CASCADE, related_name="script_versions")
    episode = models.ForeignKey("creation.Episode", on_delete=models.CASCADE, null=True, blank=True,
                                related_name="script_versions")
    version = models.PositiveIntegerField("版本号")
    branch_name = models.CharField("分支名", max_length=100, default="main")
    parent = models.ForeignKey("self", on_delete=models.SET_NULL, null=True, blank=True,
                               related_name="children")
    content = models.JSONField("内容", default=dict)
    summary = models.CharField("说明", max_length=500, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_script_version"
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["play", "episode", "branch_name", "version"],
                                    name="uniq_script_branch_version"),
        ]
        indexes = [models.Index(fields=["play", "episode", "branch_name"])]
        verbose_name = "剧本版本"
        verbose_name_plural = "剧本版本"

    def __str__(self):
        return f"{self.branch_name} v{self.version}"


class CharacterVersion(models.Model):
    """角色设定版本（Phase 2.4.3.5）：记录角色设定变化与受影响的资产。"""
    character = models.ForeignKey("creation.Character", on_delete=models.CASCADE, related_name="versions")
    version = models.PositiveIntegerField("版本号")
    snapshot = models.JSONField("设定快照", default=dict)
    changed_fields = models.JSONField("变更字段", default=list, blank=True)
    impacted_asset_ids = models.JSONField("受影响资产", default=list, blank=True)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_character_version"
        ordering = ["-version"]
        constraints = [
            models.UniqueConstraint(fields=["character", "version"], name="uniq_character_version"),
        ]
        verbose_name = "角色版本"
        verbose_name_plural = "角色版本"


EMPTY_REVISION = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"  # sha256("")


class AssistantMemory(models.Model):
    """短剧级长期记忆（assistant/memory，D7）：Markdown + 内容 SHA-256 版本号。"""
    play = models.OneToOneField("creation.Play", on_delete=models.CASCADE, related_name="assistant_memory")
    content = models.TextField("内容", blank=True)
    revision = models.CharField("版本", max_length=64, default=EMPTY_REVISION)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_assistant_memory"
        verbose_name = "助手记忆"
        verbose_name_plural = "助手记忆"


class MetricName(models.TextChoices):
    """评估指标名称（assistant/evaluation）。"""
    TASK_COMPLETION = "task_completion", "任务完成率"
    USER_SATISFACTION = "user_satisfaction", "用户满意度"
    COST = "cost", "成本"
    LATENCY = "latency", "延迟"
    TOOL_SUCCESS_RATE = "tool_success_rate", "工具成功率"


class EvaluationMetric(models.Model):
    """评估指标记录（P0: 评估体系）。

    每次assistant轮次结束后自动记录多个指标，用于性能监控和优化。
    """
    session = models.ForeignKey(AssistantSession, on_delete=models.CASCADE, related_name="evaluation_metrics")
    turn_id = models.PositiveIntegerField("轮次编号")
    metric_name = models.CharField("指标名称", max_length=100, choices=MetricName.choices)
    metric_value = models.FloatField("指标值")
    metadata = models.JSONField("元数据", default=dict, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_assistant_evaluation_metric"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["session", "metric_name"]),
            models.Index(fields=["metric_name", "created_at"]),
        ]
        verbose_name = "评估指标"
        verbose_name_plural = "评估指标"

    def __str__(self):
        return f"{self.metric_name}={self.metric_value} (turn {self.turn_id})"


class PlanStatus(models.TextChoices):
    """计划状态。"""
    ACTIVE = "ACTIVE", "执行中"
    COMPLETED = "COMPLETED", "已完成"
    FAILED = "FAILED", "已失败"
    ABANDONED = "ABANDONED", "已放弃"


class Plan(models.Model):
    """执行计划（P0: 规划引擎）。

    将复杂目标分解为可执行的子任务，支持动态更新。
    """
    session = models.ForeignKey(AssistantSession, on_delete=models.CASCADE, related_name="plans")
    goal = models.TextField("目标")
    steps = models.JSONField("步骤", default=list)  # [{"id": 1, "description": "...", "status": "PENDING"}]
    status = models.CharField("状态", max_length=20, choices=PlanStatus.choices, default=PlanStatus.ACTIVE)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_assistant_plan"
        ordering = ["-created_at"]
        indexes = [
            models.Index(fields=["session", "status"]),
        ]
        verbose_name = "执行计划"
        verbose_name_plural = "执行计划"

    def __str__(self):
        return f"计划#{self.pk}: {self.goal[:50]}"

    def get_progress(self):
        """计算完成进度（0-1）。"""
        if not self.steps:
            return 0.0
        completed = sum(1 for s in self.steps if s.get("status") == "COMPLETED")
        return completed / len(self.steps)


class PlanRevision(models.Model):
    """计划变更历史。"""
    plan = models.ForeignKey(Plan, on_delete=models.CASCADE, related_name="revisions")
    revision = models.JSONField("变更后步骤", default=list)
    reason = models.TextField("变更原因", blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_plan_revision"
        ordering = ["-created_at"]
        verbose_name = "计划变更"
        verbose_name_plural = "计划变更"


class AgentRole(models.Model):
    """多 Agent 协作角色（Phase 2.1）。

    tools 为工具白名单（仅允许只读工具），model_config 可覆盖会话默认模型：
    {"model_id": <AIModel pk>, "temperature": 0.7, "max_tokens": 2000}。
    """
    name = models.SlugField("角色标识", max_length=50, unique=True)
    display_name = models.CharField("角色名称", max_length=100, blank=True)
    description = models.TextField("描述", blank=True)
    system_prompt = models.TextField("系统提示", blank=True)
    tools = models.JSONField("可用工具", default=list, blank=True)
    model_config = models.JSONField("模型配置", default=dict, blank=True)
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_agent_role"
        ordering = ["name"]
        verbose_name = "Agent角色"
        verbose_name_plural = "Agent角色"

    def __str__(self):
        return self.display_name or self.name


class CommunicationKind(models.TextChoices):
    """Agent 间通信类型（Phase 2.1）。"""
    TASK = "TASK", "任务分配"
    RESULT = "RESULT", "结果返回"
    FEEDBACK = "FEEDBACK", "评审反馈"
    COORDINATION = "COORDINATION", "协调汇总"


class SkillScope(models.TextChoices):
    """技能作用域（Phase 2.2）。"""
    GLOBAL = "GLOBAL", "全局"
    PLAY = "PLAY", "短剧级"


class SkillType(models.TextChoices):
    """技能类型（Phase 2.2）。"""
    STYLE = "STYLE", "风格"
    STRUCTURE = "STRUCTURE", "结构"
    TOOL_SEQUENCE = "TOOL_SEQUENCE", "工具序列"
    RECOVERY = "RECOVERY", "失败恢复"
    OTHER = "OTHER", "其他"


class SkillLibrary(models.Model):
    """经验学习技能库（Phase 2.2）：从成功轮次沉淀的可复用经验。

    与文件系统 Skills（assistant/skills.py，人工维护的创作方法论）互补：
    这里存的是"什么情况下、按什么步骤做、成功过多少次"的运行期经验。
    """
    name = models.CharField("技能名", max_length=200)
    scope = models.CharField("作用域", max_length=20, choices=SkillScope.choices, default=SkillScope.GLOBAL)
    play = models.ForeignKey("creation.Play", on_delete=models.CASCADE, null=True, blank=True,
                             related_name="skill_library")
    skill_type = models.CharField("类型", max_length=20, choices=SkillType.choices, default=SkillType.OTHER)
    description = models.TextField("描述", blank=True)
    trigger_conditions = models.JSONField("触发条件", default=list, blank=True)   # ["反转", "节奏"]
    execution_steps = models.JSONField("执行步骤", default=list, blank=True)      # ["读剧本", "提提案"]
    success_count = models.PositiveIntegerField("成功次数", default=0)
    failure_count = models.PositiveIntegerField("失败次数", default=0)
    confidence = models.FloatField("置信度", default=0.5)
    source_turn = models.ForeignKey("creation.TaskRecord", on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="derived_skills")
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_skill_library"
        ordering = ["-confidence", "-success_count", "name"]
        constraints = [
            models.UniqueConstraint(fields=["scope", "play", "name"], name="uniq_skill_scope_play_name"),
        ]
        indexes = [
            models.Index(fields=["scope", "play", "is_active"]),
            models.Index(fields=["skill_type", "is_active"]),
        ]
        verbose_name = "技能库"
        verbose_name_plural = "技能库"

    def __str__(self):
        return f"{self.name}（{self.success_count}/{self.success_count + self.failure_count}）"

    @property
    def samples(self):
        return self.success_count + self.failure_count


class ExperimentStatus(models.TextChoices):
    RUNNING = "RUNNING", "进行中"
    COMPLETED = "COMPLETED", "已完成"
    ABORTED = "ABORTED", "已终止"


class StrategyExperiment(models.Model):
    """A/B 策略实验（Phase 2.2）：对比不同策略成功率。

    variants: [{"key": "A", "name": "先读后写", "weight": 1, "success": 0, "failure": 0}]
    """
    name = models.CharField("实验名", max_length=200)
    description = models.TextField("描述", blank=True)
    variants = models.JSONField("变体", default=list, blank=True)
    status = models.CharField("状态", max_length=20, choices=ExperimentStatus.choices,
                              default=ExperimentStatus.RUNNING)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name="+")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_strategy_experiment"
        ordering = ["-created_at"]
        verbose_name = "策略实验"
        verbose_name_plural = "策略实验"

    def __str__(self):
        return self.name


class AgentCommunication(models.Model):
    """Agent 间通信记录（Phase 2.1）。sender/recipient 存角色名，便于角色被删后仍可追溯。"""
    session = models.ForeignKey(AssistantSession, on_delete=models.CASCADE,
                                related_name="agent_communications")
    task_record = models.ForeignKey("creation.TaskRecord", on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name="agent_communications")
    sender = models.CharField("发送方", max_length=50)
    recipient = models.CharField("接收方", max_length=50)
    kind = models.CharField("类型", max_length=20, choices=CommunicationKind.choices)
    content = models.JSONField("内容", default=dict, blank=True)
    cost = models.DecimalField("费用", max_digits=12, decimal_places=6, default=0)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_agent_communication"
        ordering = ["id"]
        indexes = [
            models.Index(fields=["session", "created_at"]),
            models.Index(fields=["task_record", "kind"]),
        ]
        verbose_name = "Agent通信"
        verbose_name_plural = "Agent通信"

    def __str__(self):
        return f"{self.sender}->{self.recipient} {self.kind}"


class ToolType(models.TextChoices):
    OFFICIAL = "OFFICIAL", "官方"
    CUSTOM = "CUSTOM", "自定义"


class ToolRegistry(models.Model):
    """工具注册表（Phase 3.1 轨道 2）：数据库存储的纯函数助手工具。

    handler 契约：顶层 run(args: dict) -> dict，JSON 进 JSON 出，仅白名单 import；
    在隔离子进程执行（registry.run_in_sandbox），不接触会话/短剧/数据库。
    创建默认停用（is_active=False），试运行通过后由作者激活。

    市场侧（Phase 3.1 收尾）：installed_from 指向安装来源（市场条目），
    复制式安装语义——副本独立生命周期，源变更/下架不追溯影响副本。
    """
    tool_name = models.CharField("工具名", max_length=50, unique=True)
    tool_type = models.CharField("类型", max_length=10, choices=ToolType.choices,
                                 default=ToolType.CUSTOM)
    category = models.CharField("分类", max_length=50, blank=True)
    description = models.CharField("描述", max_length=300)
    input_schema = models.JSONField("入参schema", default=dict, blank=True)
    handler_code = models.TextField("handler源码")
    author = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                               related_name="custom_tools")
    is_verified = models.BooleanField("已验证", default=False)
    is_active = models.BooleanField("启用", default=False)
    version = models.PositiveIntegerField("版本", default=1)
    installed_from = models.ForeignKey(
        "self", on_delete=models.SET_NULL, null=True, blank=True,
        related_name="installed_copies", verbose_name="安装来源")
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_tool_registry"
        ordering = ["tool_name"]
        indexes = [
            models.Index(fields=["tool_type", "is_verified", "is_active"]),
            models.Index(fields=["author", "is_active"]),
            models.Index(fields=["installed_from"]),
        ]
        verbose_name = "工具注册表"
        verbose_name_plural = "工具注册表"

    def __str__(self):
        return f"{self.tool_name}（{self.tool_type} v{self.version}）"


class ToolReviewAction(models.TextChoices):
    """审核动作（Phase 3.1 市场侧审核流，append-only 审计）。"""
    SUBMIT = "SUBMIT", "送审"
    VERIFY = "VERIFY", "验证"
    REJECT = "REJECT", "驳回"
    REVOKE = "REVOKE", "下架"


class ToolReviewLog(models.Model):
    """工具审核日志（Phase 3.1 市场侧）：append-only，不提供修改/删除端点。

    REJECT/REVOKE 必填 note（驳回原因须可达作者）；动作序列即状态轨迹
    （submit→reject→submit→verify 全程可追溯），审核队列按最新动作推断待审态。
    """
    target = models.ForeignKey(ToolRegistry, on_delete=models.CASCADE,
                               related_name="review_logs")
    actor = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                              related_name="tool_review_actions")
    action = models.CharField("动作", max_length=10,
                              choices=ToolReviewAction.choices)
    note = models.CharField("附注", max_length=500, blank=True)
    created_at = models.DateTimeField("时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_tool_review_log"
        ordering = ["target_id", "id"]
        indexes = [models.Index(fields=["target", "-id"])]
        verbose_name = "工具审核日志"
        verbose_name_plural = "工具审核日志"

    def __str__(self):
        return f"{self.target.tool_name} {self.action} @{self.created_at:%m-%d %H:%M}"


class ToolRating(models.Model):
    """工具评分评论（Phase 3.1 市场侧）：(tool, user) 唯一，重复评分 upsert 覆盖。

    rating 1-5；comment ≤200 可撤（置空仅留分）；聚合走查询期 annotate，
    不做冗余列（本地单机量级）。作者不能评自己的工具。
    """
    tool = models.ForeignKey(ToolRegistry, on_delete=models.CASCADE,
                             related_name="ratings")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="tool_ratings")
    rating = models.PositiveSmallIntegerField("评分")
    comment = models.CharField("评论", max_length=200, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_tool_rating"
        constraints = [
            models.UniqueConstraint(fields=["tool", "user"],
                                     name="uniq_tool_rating_per_user"),
        ]
        verbose_name = "工具评分"
        verbose_name_plural = "工具评分"

    def __str__(self):
        return f"{self.tool.tool_name} {self.rating}★ by {self.user_id}"


class AssistantEvent(models.Model):
    """助手事件表（Phase 3.2 / registry D1）：local 双进程跨进程事件后端。

    stream_id 为"毫秒-序号"形状（对齐 Redis Stream ID），写入单调递增；
    读取按字典序 cursor 续读。与 RedisStream 语义一致：按会话截断保留
    MAX_LEN（1000）+ TTL（24h）。仅实时推送，持久数据以 AssistantMessage 为准。
    """
    session = models.ForeignKey(AssistantSession, on_delete=models.CASCADE,
                                related_name="assistant_events")
    stream_id = models.CharField("事件ID", max_length=32)
    data = models.JSONField("事件数据", default=dict)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_assistant_event"
        ordering = ["stream_id"]
        indexes = [
            models.Index(fields=["session", "stream_id"]),
            models.Index(fields=["created_at"]),
        ]
        unique_together = ["session", "stream_id"]
        verbose_name = "助手事件"
        verbose_name_plural = "助手事件"

    def __str__(self):
        return f"{self.stream_id} {self.data.get('type', '')}"


class MemoryKind(models.TextChoices):
    """结构化记忆条目类型（Phase 3.4 轨道 2）。"""
    DECISION = "DECISION", "决策"
    PREFERENCE = "PREFERENCE", "偏好"
    FACT = "FACT", "事实"


class MemoryRecord(models.Model):
    """短剧级结构化记忆条目（Phase 3.4 轨道 2）：agent 维护的可检索跨会话层。

    与 AssistantMemory（用户可编辑 Markdown 主文档）分工：主文档是给人看的
    全文，MemoryRecord 是按类型/重要性组织的机器可检索条目。同内容重复
    沉淀靠 content_sha256 唯一约束去重（不建长文本索引）。
    """
    play = models.ForeignKey("creation.Play", on_delete=models.CASCADE,
                             related_name="memory_records")
    kind = models.CharField("类型", max_length=20, choices=MemoryKind.choices,
                            default=MemoryKind.FACT)
    content = models.TextField("内容", max_length=2000)
    content_sha256 = models.CharField("内容哈希", max_length=64)
    importance = models.FloatField("重要性", default=0.5)
    source_session = models.ForeignKey(AssistantSession, on_delete=models.SET_NULL,
                                       null=True, blank=True, related_name="settled_records")
    is_active = models.BooleanField("启用", default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "shortplay_memory_record"
        ordering = ["-importance", "-updated_at"]
        indexes = [models.Index(fields=["play", "is_active"])]
        unique_together = [("play", "kind", "content_sha256")]
        verbose_name = "结构化记忆条目"
        verbose_name_plural = "结构化记忆条目"

    def __str__(self):
        return f"[{self.get_kind_display()}] {self.content[:30]}"


# ── Phase 3 可本地落地项（2026-11-21-backend-agent-feedback-inspiration）─────

class AgentFeedback(models.Model):
    """用户对协作轮次择优决策的反馈（Phase 3.2.8）。

    (turn, user) 唯一——同轮重复反馈 upsert 覆盖；低分反馈注入该会话
    后续轮次 system prompt（runner 反馈段）。不进 project.json 导出
    （工作台 D3：Agent 会话侧状态不导出）。
    """
    session = models.ForeignKey(AssistantSession, on_delete=models.CASCADE,
                                related_name="agent_feedbacks")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="agent_feedbacks")
    turn = models.ForeignKey("creation.TaskRecord", on_delete=models.SET_NULL,
                            null=True, related_name="agent_feedbacks")
    rating = models.PositiveSmallIntegerField("评分")
    comment = models.CharField("评论", max_length=500, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_agent_feedback"
        constraints = [
            models.UniqueConstraint(fields=["turn", "user"],
                                     name="uniq_agent_feedback_turn_user"),
        ]
        verbose_name = "协作轮反馈"
        verbose_name_plural = "协作轮反馈"

    def __str__(self):
        return f"会话{self.session_id} {self.rating}星"


class InspirationCategory(models.TextChoices):
    """灵感分类（Phase 3.5.1）。"""
    PLOT_TWIST = "plot_twist", "剧情反转"
    DIALOGUE = "dialogue", "对白"
    SCENE = "scene", "场景"
    CHARACTER = "character", "角色"
    OTHER = "other", "其他"


class InspirationSource(models.TextChoices):
    MANUAL = "manual", "手动"
    AGENT = "agent", "Agent"


class Inspiration(models.Model):
    """创作灵感条目（Phase 3.5.1）：跨会话可复用的创意素材。

    与 SkillLibrary（可复用方法论：怎么写）、MemoryRecord（本剧工作状态）
    正交：灵感是"写什么"的素材。play 为空 = GLOBAL（全员可读）；
    agent 经工具保存（source=agent），用户经 REST 保存（source=manual）。
    """
    play = models.ForeignKey("creation.Play", on_delete=models.CASCADE,
                            null=True, blank=True, related_name="inspirations")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="inspirations")
    category = models.CharField("分类", max_length=20,
                                choices=InspirationCategory.choices,
                                default=InspirationCategory.OTHER)
    content = models.CharField("内容", max_length=2000)
    source = models.CharField("来源", max_length=10,
                              choices=InspirationSource.choices,
                              default=InspirationSource.MANUAL)
    rating = models.PositiveSmallIntegerField("评分", default=3)
    used_count = models.PositiveIntegerField("使用次数", default=0)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_inspiration"
        ordering = ["-rating", "-created_at"]
        indexes = [models.Index(fields=["play", "category"])]
        verbose_name = "灵感"
        verbose_name_plural = "灵感"

    def __str__(self):
        return f"{self.get_category_display()}：{self.content[:20]}"


class ReviewFeedback(models.Model):
    """用户对审查报告的反馈（Phase 3.5.2）。

    issues_feedback = [{issue_key, agree, suggestion?}]（issue_key 为报告
    issues 数组的位置性键：det-{i} / {dimension}-{i}）。同报告重复反馈
    upsert；维度 disagree 统计注入后续审查 prompt（agent_review）。
    """
    episode = models.ForeignKey("creation.Episode", on_delete=models.CASCADE,
                                related_name="review_feedbacks")
    review_log = models.ForeignKey("creation.ReviewLog", on_delete=models.SET_NULL,
                                   null=True, blank=True,
                                   related_name="agent_feedbacks")
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                             related_name="review_feedbacks")
    rating = models.PositiveSmallIntegerField("评分")
    issues_feedback = models.JSONField("逐条反馈", default=list, blank=True)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_review_feedback"
        constraints = [
            models.UniqueConstraint(fields=["episode", "user", "review_log"],
                                    name="uniq_review_feedback_per_report"),
        ]
        verbose_name = "审查反馈"
        verbose_name_plural = "审查反馈"

    def __str__(self):
        return f"分集{self.episode_id} {self.rating}星"


class TrustLevel(models.Model):
    """用户信任分级（主计划 3.2.5 / design D1）。

    信任分由提案采纳事件确定性累积（apply→+ / discard|stale→-，幂等夹紧 [0,1]），
    无手工设置端点。渐进自主（3.2.6）按 trust_score ≥ auto_approve_threshold 且
    提案置信度达标判定自动批准（trust.py，design D3）。
    """

    user = models.OneToOneField(settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
                                related_name="trust_levels")
    trust_score = models.FloatField("信任分", default=0.5)
    auto_approve_threshold = models.FloatField(
        "自动批准阈值", null=True, blank=True,
        help_text="空取 settings.TRUST_DEFAULT_THRESHOLD")
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_trust_level"
        verbose_name = "信任分级"
        verbose_name_plural = "信任分级"

    def __str__(self):
        return f"用户{self.user_id} 信任{self.trust_score:.2f}"
