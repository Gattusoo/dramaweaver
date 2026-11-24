"""积分钱包与只追加流水（决策 8/17）。

- 积分 = 分（¥ 的百分之一），整数存储，全程禁 float
- WalletEntry 只追加：无 UPDATE/DELETE 端点，balance 由 entry 链推
  （balance_after 冗余存储，事务内累加写入，可重放校验）
- amount 符号约定：加余额为正（grant/adjust+/refund/hold 释放），
  减余额为负（hold/settle 扣/fee）
"""
from django.conf import settings
from django.db import models


class Wallet(models.Model):
    """用户钱包：一用户一钱包，余额为整数分。"""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="wallet", verbose_name="用户",
    )
    balance = models.IntegerField("余额(分)", default=0)
    created_at = models.DateTimeField("创建时间", auto_now_add=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_wallet"
        verbose_name = "钱包"
        verbose_name_plural = "钱包"

    def __str__(self):
        return f"Wallet(user={self.user_id}, balance={self.balance})"


class EntryType(models.TextChoices):
    GRANT = "grant", "发放（注册赠金/管理员发放）"
    HOLD = "hold", "预扣"
    SETTLE = "settle", "结算（按实际对账）"
    REFUND = "refund", "退回（差额/失败整额/过期回收）"
    ADJUST = "adjust", "管理员调整（可正负）"
    FEE = "fee", "服务费（自带 Key 按次）"
    METER = "meter", "计量（存储 GB·月，第二期结算）"


class EntryStatus(models.TextChoices):
    """hold/settle 生命周期状态（仅 hold 类条目使用；其余类型恒 DONE）。"""

    ACTIVE = "ACTIVE", "预扣中"
    SETTLED = "SETTLED", "已结算"
    RELEASED = "RELEASED", "已释放（退款/过期）"


class WalletEntry(models.Model):
    """只追加流水：类型 + 带符号金额 + 引用 + 余额快照。

    - ref_kind+ref_id：幂等键（同任务不重复扣）；grant 的注册赠金
      以 (signup, user_id) 唯一——重复信号不重复发
    - balance_after：写入后余额（事务内累加），重放全链可得最终余额
    - hold 条目带 expires_at：过期由 Celery 回收任务转 refund
    """

    wallet = models.ForeignKey(
        Wallet, on_delete=models.CASCADE, related_name="entries", verbose_name="钱包",
    )
    type = models.CharField("类型", max_length=10, choices=EntryType.choices, db_index=True)
    status = models.CharField(
        "状态", max_length=10, choices=EntryStatus.choices,
        default=EntryStatus.SETTLED,
        help_text="仅 hold 类条目有生命周期（ACTIVE→SETTLED/RELEASED）；其余类型恒为 SETTLED 终态",
    )
    amount = models.IntegerField("金额(分，带符号)")
    balance_after = models.IntegerField("写入后余额(分)")
    ref_kind = models.CharField("引用类型", max_length=30, blank=True, default="")
    ref_id = models.CharField("引用ID", max_length=64, blank=True, default="")
    note = models.CharField("备注", max_length=200, blank=True, default="")
    expires_at = models.DateTimeField("过期时间", null=True, blank=True)
    created_at = models.DateTimeField("时间", auto_now_add=True)

    class Meta:
        db_table = "shortplay_wallet_entry"
        indexes = [
            models.Index(fields=["wallet", "created_at"], name="idx_we_wallet_time"),
            models.Index(fields=["ref_kind", "ref_id"], name="idx_we_ref"),
            models.Index(fields=["type", "status", "expires_at"], name="idx_we_hold_expiry"),
        ]
        ordering = ["-created_at"]
        verbose_name = "钱包流水"
        verbose_name_plural = "钱包流水"

    def __str__(self):
        return f"{self.wallet_id}:{self.type}:{self.amount}#{self.ref_kind}/{self.ref_id}"


class UserQuotaOverride(models.Model):
    """按用户覆盖资源限额（决策 7）：管理员可调；不启用回退全局默认。"""

    user = models.OneToOneField(
        settings.AUTH_USER_MODEL, on_delete=models.CASCADE,
        related_name="quota_override", verbose_name="用户",
    )
    max_concurrent_render = models.PositiveIntegerField(
        "并发渲染槽位", null=True, blank=True,
        help_text="空=回退全局默认",
    )
    max_concurrent_ai = models.PositiveIntegerField(
        "并发AI生成槽位", null=True, blank=True,
        help_text="空=回退全局默认",
    )
    daily_render_seconds = models.PositiveIntegerField(
        "每日渲染时长上限(秒)", null=True, blank=True,
        help_text="空=回退全局默认",
    )
    override_enabled = models.BooleanField("启用覆盖", default=True)
    updated_at = models.DateTimeField("更新时间", auto_now=True)

    class Meta:
        db_table = "shortplay_user_quota_override"
        verbose_name = "用户限额覆盖"
        verbose_name_plural = "用户限额覆盖"

    def __str__(self):
        return f"QuotaOverride(user={self.user_id}, enabled={self.override_enabled})"
