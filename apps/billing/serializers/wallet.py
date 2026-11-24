"""billing serializers：钱包/流水只读 + adjust 写入。"""
from rest_framework import serializers

from ..models import Wallet, WalletEntry


class WalletEntrySerializer(serializers.ModelSerializer):
    """流水只读序列化器——WalletEntry 无任何更新端点（design D1 铁律）。"""

    class Meta:
        model = WalletEntry
        fields = [
            "id", "wallet", "type", "status", "amount", "balance_after",
            "ref_kind", "ref_id", "note", "expires_at", "created_at",
        ]
        read_only_fields = fields  # 全只读：无任何写端点暴露此模型


class WalletSerializer(serializers.ModelSerializer):
    username = serializers.CharField(source="user.username", read_only=True)
    entries = WalletEntrySerializer(many=True, read_only=True)

    class Meta:
        model = Wallet
        fields = ["id", "user", "username", "balance", "entries", "created_at", "updated_at"]
        read_only_fields = fields


class WalletAdjustSerializer(serializers.Serializer):
    """管理员调整入参：amount 可正负、note 必填。"""

    amount = serializers.IntegerField(
        help_text="调整额（分，可正可负，不可为 0）",
    )
    note = serializers.CharField(
        max_length=200, required=True, allow_blank=False,
        help_text="调整原因（必填，写审计）",
    )

    def validate_amount(self, value):
        if value == 0:
            raise serializers.ValidationError("调整额不可为 0")
        return value


class MyWalletSerializer(serializers.ModelSerializer):
    """用户自查钱包（余额 + 自己的流水）。"""

    entries = WalletEntrySerializer(many=True, read_only=True)

    class Meta:
        model = Wallet
        fields = ["id", "balance", "entries", "updated_at"]
        read_only_fields = fields
