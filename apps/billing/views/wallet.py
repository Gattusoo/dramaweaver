"""billing 管理端点 + 用户自查钱包。"""
import logging

from django.contrib.auth import get_user_model
from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from dvadmin.utils.pagination import CustomPagination

from apps.creation.models import AuditLog

from ..models import Wallet
from ..serializers import MyWalletSerializer, WalletAdjustSerializer, WalletSerializer
from ..services import adjust as adjust_service
from ..services import get_or_create_wallet

logger = logging.getLogger(__name__)
User = get_user_model()


def _is_platform_admin(user):
    return bool(user and getattr(user, "is_platform_admin", False))


class AdminWalletListView(APIView):
    """GET /api/admin/wallets/：管理员查看全部钱包（余额 + 近期流水）。"""

    permission_classes = [IsAuthenticated]
    pagination_class = CustomPagination

    def get(self, request):
        if not _is_platform_admin(request.user):
            return Response(status=status.HTTP_403_FORBIDDEN)
        qs = Wallet.objects.select_related("user").order_by("-updated_at")
        paginator = self.pagination_class()
        page = paginator.paginate_queryset(qs, request)
        serializer = WalletSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class AdminWalletAdjustView(APIView):
    """POST /api/admin/wallets/{user_pk}/adjust/：管理员调整余额（写流水 + 审计）。

    - amount 可正负、不可为 0；note 必填（无 note 400）
    - 普通用户 403
    - adjust 不幂等（每次调用都是新流水）——审计要求可追溯
    """

    permission_classes = [IsAuthenticated]

    def post(self, request, user_pk):
        if not _is_platform_admin(request.user):
            return Response(status=status.HTTP_403_FORBIDDEN)
        target = User.objects.filter(pk=user_pk).first()
        if target is None:
            return Response(status=status.HTTP_404_NOT_FOUND)
        ser = WalletAdjustSerializer(data=request.data)
        if not ser.is_valid():
            return Response(ser.errors, status=status.HTTP_400_BAD_REQUEST)
        amount = ser.validated_data["amount"]
        note = ser.validated_data["note"]
        entry = adjust_service(target, amount, note=note)
        # 审计：actor=管理员，action=wallet_adjust，target=user，detail 含 amount/note/entry
        AuditLog.objects.create(
            actor=request.user, action="wallet_adjust",
            target_type="user", target_id=str(target.pk),
            detail={"amount": amount, "note": note,
                    "wallet_entry_id": entry.pk,
                    "balance_after": entry.balance_after},
        )
        wallet = Wallet.objects.get(pk=entry.wallet_id)
        return Response({
            "balance": wallet.balance,
            "entry_id": entry.pk,
            "amount": entry.amount,
            "balance_after": entry.balance_after,
        })


class MyWalletView(APIView):
    """GET /api/wallet/：当前用户钱包与流水（自查）。"""

    permission_classes = [IsAuthenticated]

    def get(self, request):
        wallet = get_or_create_wallet(request.user)
        serializer = MyWalletSerializer(wallet)
        return Response(serializer.data)
