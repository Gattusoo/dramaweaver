"""creation views —— play 树资源。"""
import logging

from django.conf import settings
from django.db import transaction
from django.db.models import Q
from django.utils import timezone
from django.http import HttpResponse
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.core.system.models import CustomUser
from apps.core.system.permissions import IsAdminOnly

from ..models import (
    Asset, AssetStatus, AssetType, Character, Episode, EpisodeStatus, Play,
    PlayStatus, Scene, Shot, TaskRecord, TaskType, VoiceProfile,
    AuditLog, InvitationStatus, MemberRole, MemberStatus, PlayInvitation, PlayMember,
)
from ..permissions import (
    IsPlayAuthorizedOrAdmin, can_manage_play, can_write_play, get_accessible_play_ids,
)
from ..serializers import (
    CharacterSerializer, EpisodeSerializer, PlaySerializer, SceneSerializer,
    ShotSerializer, VoiceProfileSerializer,
)
from .base import PlayScopedViewSet

_ACTIVE_TASK_STATUSES = ("PENDING", "RUNNING", "RETRYING")

logger = logging.getLogger(__name__)


def _member_limits():
    conf = settings.PLAY_MEMBER_LIMIT if hasattr(settings, "PLAY_MEMBER_LIMIT") else 20
    daily = settings.PLAY_INVITE_DAILY_LIMIT if hasattr(settings, "PLAY_INVITE_DAILY_LIMIT") else 20
    expiry_days = (
        settings.PLAY_INVITE_EXPIRY_DAYS if hasattr(settings, "PLAY_INVITE_EXPIRY_DAYS") else 7
    )
    return conf, daily, expiry_days


def _audit(request, action, *, play=None, target_type="", target_id="", **detail):
    AuditLog.objects.create(
        actor_id=request.user.pk, action=action, play=play,
        target_type=target_type, target_id=str(target_id), detail=detail,
    )


class PlayViewSet(viewsets.ModelViewSet):
    serializer_class = PlaySerializer
    permission_classes = [IsPlayAuthorizedOrAdmin]
    manage_actions = ("update", "partial_update", "destroy", "invite", "members",
                      "remove_member", "transfer_ownership", "start", "publish",
                      "clear_cache")
    # 只读语义的自定义 action（POST 但不写库）：按读权限放行（permissions.read_actions）
    read_actions = ("preview_redo",)

    def get_queryset(self):
        user = self.request.user
        if not user.is_authenticated:
            return Play.objects.none()
        return Play.objects.filter(pk__in=get_accessible_play_ids(user))

    def get_permissions(self):
        # 决策 2：普通用户注册即可建剧
        if self.action == "create":
            return [IsAuthenticated()]
        return super().get_permissions()

    def perform_create(self, serializer):
        play = serializer.save(created_by=self.request.user)
        # 创建者即属主：同事务建 owner 成员行（spec: data-model/play-structure）
        PlayMember.objects.get_or_create(
            play=play, user_id=play.created_by_id,
            defaults={"role": MemberRole.OWNER, "status": MemberStatus.ACTIVE,
                      "accepted_at": timezone.now()},
        )

    @staticmethod
    def _parse_user_ids(request):
        user_ids = request.data.get("user_ids")
        if (not isinstance(user_ids, list) or not user_ids
                or any(isinstance(i, bool) or not isinstance(i, int) for i in user_ids)):
            raise ValidationError({"user_ids": "应为非空的用户 ID 整数列表"})
        ids = set(user_ids)
        found = set(CustomUser.objects.filter(pk__in=ids).values_list("pk", flat=True))
        missing = sorted(ids - found)
        if missing:
            raise ValidationError({"user_ids": f"用户不存在: {missing}"})
        return list(ids)

    # ---- 成员生命周期（邀请接受制）----

    @action(detail=True, methods=["post"], url_path="invite")
    def invite(self, request, pk=None):
        """POST /api/plays/{id}/invite/ — 属主邀请成员（对方接受后生效）。"""
        play = self.get_object()
        role = request.data.get("role")
        if role not in (MemberRole.EDITOR, MemberRole.VIEWER):
            raise ValidationError({"role": "邀请角色仅支持 editor / viewer"})
        user_ids = [i for i in self._parse_user_ids(request) if i != play.created_by_id]
        if not user_ids:
            raise ValidationError({"user_ids": "没有可邀请的用户"})

        member_limit, daily_limit, expiry_days = _member_limits()
        active = PlayMember.objects.filter(
            play=play, status=MemberStatus.ACTIVE
        ).count()
        if active >= member_limit:
            return Response(
                {"detail": f"成员数已达上限（{member_limit}）"},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )
        today_start = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        sent_today = PlayInvitation.objects.filter(
            play=play, created_by_id=request.user.pk, created_at__gte=today_start,
        ).count()
        if sent_today >= daily_limit:
            return Response(
                {"detail": f"今日邀请数已达上限（{daily_limit}）"},
                status=status.HTTP_429_TOO_MANY_REQUESTS,
            )

        invitations = []
        for uid in user_ids:
            invitation, created = PlayInvitation.objects.get_or_create(
                play=play, invitee_id=uid, status=InvitationStatus.PENDING,
                defaults={
                    "role": role,
                    "created_by_id": request.user.pk,
                    "expires_at": timezone.now() + timezone.timedelta(days=expiry_days),
                },
            )
            if not created:
                invitation.role = role
                invitation.expires_at = timezone.now() + timezone.timedelta(days=expiry_days)
                invitation.save(update_fields=["role", "expires_at"])
            invitations.append(invitation)
        _audit(request, "member_invite", play=play, target_type="user",
               target_id=",".join(str(i) for i in user_ids), role=role)
        return Response(
            {"detail": "邀请已发送，待对方接受", "invitations": [
                {"id": inv.pk, "invitee": inv.invitee_id, "role": inv.role,
                 "status": inv.status, "expires_at": inv.expires_at} for inv in invitations
            ]},
            status=status.HTTP_201_CREATED,
        )

    @action(detail=True, methods=["get"], url_path="members")
    def members(self, request, pk=None):
        """GET /api/plays/{id}/members/ — 成员列表（active 成员可看）。"""
        play = self.get_object()
        rows = PlayMember.objects.filter(
            play=play, status=MemberStatus.ACTIVE
        ).select_related("user").order_by("created_at")
        return Response({"data": [
            {"user": u.user_id, "username": u.user.username, "role": u.role,
             "accepted_at": u.accepted_at} for u in rows
        ]})

    @action(detail=True, methods=["post"], url_path="members/remove")
    def remove_member(self, request, pk=None):
        """POST /api/plays/{id}/members/remove/ — 属主移除成员。"""
        play = self.get_object()
        user_ids = self._parse_user_ids(request)
        if play.created_by_id in user_ids:
            raise ValidationError({"user_ids": "属主不能被移除，请走转让流程"})
        removed = PlayMember.objects.filter(
            play=play, user_id__in=user_ids
        ).exclude(status=MemberStatus.REMOVED)
        targets = list(removed.values_list("user_id", flat=True))
        with transaction.atomic():
            removed.update(status=MemberStatus.REMOVED)
            self._deactivate_removed(play, targets)
            _audit(request, "member_remove", play=play, target_type="user",
                   target_id=",".join(str(i) for i in targets))
        return Response({"detail": "已移除", "removed": targets})

    @action(detail=False, methods=["post"], url_path="invitations/accept")
    def accept_invitation(self, request):
        """POST /api/plays/invitations/accept/ — 被邀人接受邀请。"""
        invitation_id = request.data.get("invitation_id")
        if not isinstance(invitation_id, int):
            raise ValidationError({"invitation_id": "invitation_id 必填（整数）"})
        invitation = PlayInvitation.objects.filter(
            pk=invitation_id, invitee_id=request.user.pk
        ).first()
        if invitation is None:
            return Response({"detail": "邀请不存在"}, status=status.HTTP_404_NOT_FOUND)
        if invitation.status != InvitationStatus.PENDING:
            return Response(
                {"detail": f"邀请已处理（{invitation.status}）"},
                status=status.HTTP_409_CONFLICT,
            )
        if invitation.is_expired:
            invitation.status = InvitationStatus.EXPIRED
            invitation.save(update_fields=["status"])
            return Response({"detail": "邀请已过期"}, status=status.HTTP_409_CONFLICT)

        # 属主转让邀请：接受时事务内改写 created_by + 两条成员行（design D6）
        if invitation.role == MemberRole.OWNER:
            return self._accept_transfer(request, invitation)

        # 属主不能被普通邀请降级：属主转让后遗留的 viewer/editor 邀请
        # 若被接受，update_or_create 会把 OWNER 成员行改写为邀请角色，
        # 导致 created_by 仍是该用户但 can_manage_play 失败（管理权锁死）。
        existing = PlayMember.objects.filter(
            play=invitation.play, user_id=request.user.pk
        ).first()
        if invitation.play.created_by_id == request.user.pk or (
            existing is not None and existing.role == MemberRole.OWNER
        ):
            return Response(
                {"detail": "属主不能被普通邀请降级；请先转让属主"},
                status=status.HTTP_409_CONFLICT,
            )

        member_limit, _, _ = _member_limits()
        active = PlayMember.objects.filter(
            play=invitation.play, status=MemberStatus.ACTIVE
        ).count()
        if active >= member_limit:
            return Response(
                {"detail": f"成员数已达上限（{member_limit}）"},
                status=status.HTTP_409_CONFLICT,
            )

        with transaction.atomic():
            invitation.status = InvitationStatus.ACCEPTED
            invitation.save(update_fields=["status"])
            # 单行成员模型：被移除成员重新受邀时就地激活/改角色，
            # 不能先置 REMOVED 再 create（(play,user) 唯一约束冲突）。
            # 并发：未对邀请行加锁（既有行为，design D3 登记，后续如需强一致再议）。
            PlayMember.objects.update_or_create(
                play=invitation.play, user_id=request.user.pk,
                defaults={
                    "role": invitation.role,
                    "status": MemberStatus.ACTIVE,
                    "invited_at": invitation.created_at,
                    "accepted_at": timezone.now(),
                },
            )
            _audit(request, "member_accept", play=invitation.play,
                   target_type="invitation", target_id=invitation.pk, role=invitation.role)
        return Response({"detail": "已加入", "play": invitation.play_id, "role": invitation.role})

    @transaction.atomic
    def _accept_transfer(self, request, invitation):
        """接受属主转让：校验发起人仍是属主、接受人仍是 active editor，事务内改写。"""
        play = Play.objects.select_for_update().get(pk=invitation.play_id)
        if invitation.created_by_id != play.created_by_id:
            return Response({"detail": "转让发起人已不是属主，请让当前属主重新发起"},
                            status=status.HTTP_409_CONFLICT)
        member = PlayMember.objects.filter(
            play=play, user_id=request.user.pk, role=MemberRole.EDITOR,
            status=MemberStatus.ACTIVE,
        ).first()
        if member is None:
            return Response({"detail": "只有 active editor 成员可接受属主转让"},
                            status=status.HTTP_409_CONFLICT)
        old_owner_id = play.created_by_id
        invitation.status = InvitationStatus.ACCEPTED
        invitation.save(update_fields=["status"])
        # created_by 是单一事实来源；owner 行同事务改写
        play.created_by_id = request.user.pk
        play.save(update_fields=["created_by"])
        PlayMember.objects.filter(play=play, user_id=request.user.pk).update(
            role=MemberRole.OWNER
        )
        PlayMember.objects.filter(play=play, user_id=old_owner_id).update(
            role=MemberRole.EDITOR
        )
        # 其余待接受的转让请求一并撤销（避免双重转让）
        PlayInvitation.objects.filter(
            play=play, role=MemberRole.OWNER, status=InvitationStatus.PENDING,
        ).exclude(pk=invitation.pk).update(status=InvitationStatus.REVOKED)
        # 新属主名下遗留的普通邀请一并撤销：避免其接受后把 OWNER 成员行降级
        PlayInvitation.objects.filter(
            play=play, invitee_id=request.user.pk, status=InvitationStatus.PENDING,
        ).exclude(pk=invitation.pk).update(status=InvitationStatus.REVOKED)
        _audit(request, "ownership_transfer", play=play, target_type="user",
               target_id=request.user.pk, from_user=old_owner_id, to_user=request.user.pk)
        return Response({"detail": "已接受转让", "play": play.pk, "role": MemberRole.OWNER})

    @action(detail=False, methods=["get"], url_path="invitations")
    def my_invitations(self, request):
        """GET /api/plays/invitations/ — 我的待接受邀请。"""
        rows = (PlayInvitation.objects
                .filter(invitee_id=request.user.pk, status=InvitationStatus.PENDING)
                .select_related("play").order_by("-created_at"))
        return Response({"data": [
            {"id": r.pk, "play": r.play_id, "play_title": r.play.title, "role": r.role,
             "created_at": r.created_at, "expires_at": r.expires_at,
             "expired": r.is_expired} for r in rows
        ]})

    @action(detail=True, methods=["post"], url_path="transfer-ownership")
    def transfer_ownership(self, request, pk=None):
        """POST /api/plays/{id}/transfer-ownership/ — 属主发起转让（受让方需接受）。

        design D6：仅 owner 发起、仅 active editor 可接、需对方接受（邀请式确认）。
        发起时只创建 role=owner 的待接受邀请，不改 created_by；接受时才在事务内
        改写 created_by + 两条成员行并写审计（记录双方）。
        """
        play = self.get_object()
        to_user = request.data.get("to_user")
        if isinstance(to_user, bool) or not isinstance(to_user, int):
            raise ValidationError({"to_user": "to_user 必填（用户 ID）"})
        if to_user == play.created_by_id:
            raise ValidationError({"to_user": "不能转让给自己"})
        target = PlayMember.objects.filter(
            play=play, user_id=to_user, role=MemberRole.EDITOR,
            status=MemberStatus.ACTIVE,
        ).first()
        if target is None:
            return Response(
                {"detail": "只能转让给现有 active editor 成员"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        _, _, expiry_days = _member_limits()
        with transaction.atomic():
            # 同剧同时只保留一个待接受的属主转让请求（换人即撤销旧请求）
            PlayInvitation.objects.filter(
                play=play, role=MemberRole.OWNER, status=InvitationStatus.PENDING,
            ).exclude(invitee_id=to_user).update(status=InvitationStatus.REVOKED)
            invitation, _created = PlayInvitation.objects.get_or_create(
                play=play, invitee_id=to_user, role=MemberRole.OWNER,
                status=InvitationStatus.PENDING,
                defaults={
                    "created_by_id": request.user.pk,
                    "expires_at": timezone.now() + timezone.timedelta(days=expiry_days),
                },
            )
            _audit(request, "ownership_transfer_request", play=play, target_type="user",
                   target_id=to_user)
        return Response({"detail": "转让请求已发送，待对方接受",
                         "invitation_id": invitation.pk})

    @staticmethod
    def _deactivate_removed(play, user_ids):
        """移除成员的待处理提案作废；会话只读与新派发拦截由权限判定自动生效。

        决策 12：助手会话转只读——会话写操作的判定入口已收口到 can_write_play
        （成员表），移除后 status=removed → 无 active 行 → 写操作 403，历史可读。
        待处理提案置为 STALE（复用既有过期状态，design D6：不新增状态）。
        在途任务（PENDING/RUNNING/RETRYING）不中断，只拦新派发入口（同判定路径）。
        """
        from apps.creation.assistant.models import AssistantProposal, ProposalStatus
        AssistantProposal.objects.filter(
            play=play, session__user_id__in=user_ids, status=ProposalStatus.PENDING,
        ).update(status=ProposalStatus.STALE)

    def _ensure_reviewer(self, play):
        check = can_manage_play if self.action in self.manage_actions else can_write_play
        if not check(self.request.user, play):
            raise PermissionDenied("无权操作该短剧")

    @action(detail=True, methods=["post"], url_path="start")
    def start(self, request, pk=None):
        """POST /api/plays/{id}/start/ — 启动生产流水线（DRAFT -> OUTLINING）。

        status 对客户端只读（serializers.PlaySerializer），启动只能走这个显式入口：
        校验权限、要求已有分集，然后经状态机 transition 到 OUTLINING。post_save 信号
        （dispatcher._play_post_save）在 DRAFT->OUTLINING 时派发大纲任务。
        """
        from ..pipeline import state_machine
        play = self.get_object()
        self._ensure_reviewer(play)  # 启动属 manage 层（决定开拍），复用 manage_actions 门禁
        if play.frozen_reason:
            # 业务码 4003：冻结剧拒绝生成/发布（spec: auth/object-permissions 冻结场景）
            resp = Response(
                {"detail": f"短剧已被平台管理员冻结：{play.frozen_reason}",
                 "reason": "play_frozen"},
                status=status.HTTP_403_FORBIDDEN,
            )
            resp.business_code = 4003  # 渲染器读取挂载码，信封 code=4003
            return resp
        if not play.episodes.exists():
            return Response({"detail": "短剧没有分集，无法启动；请先补齐分集"},
                            status=status.HTTP_400_BAD_REQUEST)
        if play.status != PlayStatus.DRAFT:
            return Response({"detail": f"短剧当前状态为 {play.status}，只有草稿可启动"},
                            status=status.HTTP_409_CONFLICT)
        # 计费同步预检（design D2）：在状态迁移前只读校验，拒绝时 Play 保持 DRAFT、
        # 不创建 TaskRecord；异步派发闸仍会再校验一次（竞态见 design D2）。
        gate_resp = self._start_billing_preflight(play)
        if gate_resp is not None:
            return gate_resp
        try:
            # transition 只是一次 save；post_save 信号派发大纲任务
            state_machine.transition(play, PlayStatus.OUTLINING)
        except state_machine.InvalidTransition as exc:
            # 并发下状态可能已变（守卫与迁移之间），这里兜底返回 409
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(self.get_serializer(play).data)

    @staticmethod
    def _start_billing_preflight(play):
        """start 的计费预检：余额 → 402/4001；每日额度 → 402/4029；槽位 → 409/4090。

        每日额度只检查「已经用完」（OUTLINE 是 ai 任务、零渲染秒，不能按
        60s 预留，否则额度尚有余量时会被 4029 误拦）。
        估算失败记 warning 并按 0 预检（与派发闸一致：不阻断创作）。
        created_by 为空（历史/后台建剧）时跳过，与 dispatcher 计费守卫一致。
        返回 None = 通过。
        """
        from apps.billing.services.gate import (
            InsufficientPoints, QuotaExceeded, preflight,
        )
        from apps.billing.services.wallet import yuan_to_points
        from ..assistant.cost_estimator import estimate_task_cost
        from ..models import TaskType

        if play.created_by_id is None:
            return None
        estimate = 0
        try:
            est = estimate_task_cost(play, TaskType.OUTLINE)
            estimate = yuan_to_points(est["estimated_cost"])
        except Exception:  # noqa: BLE001 — 估算失败按 0 预检（不阻断创作）
            logger.warning(
                "Play#%s start 预检计价估算失败，以 0 预检", play.pk, exc_info=True
            )
        try:
            preflight(
                play.created_by, estimate, slot_kind="ai",
                daily_exhausted_only=True,
            )
        except InsufficientPoints as exc:
            resp = Response({
                "detail": f"积分余额不足，请充值后重试：{exc}",
                "reason": "insufficient_points",
                "needed": exc.needed,
                "balance": exc.balance,
            }, status=status.HTTP_402_PAYMENT_REQUIRED)
            resp.business_code = 4001
            return resp
        except QuotaExceeded as exc:
            if getattr(exc, "kind", "") == "daily_render":
                resp = Response({
                    "detail": f"今日渲染时长额度已用完：{exc}",
                    "reason": "daily_render_exhausted",
                }, status=status.HTTP_402_PAYMENT_REQUIRED)
                resp.business_code = 4029  # 每日额度：与 4001 区分
                return resp
            return Response({
                "detail": f"并发任务已达上限，请稍后重试：{exc}",
                "reason": "quota_exceeded",
            }, status=status.HTTP_409_CONFLICT)
        return None

    @action(detail=True, methods=["post"])
    def approve(self, request, pk=None):
        play = self.get_object()
        self._ensure_reviewer(play)
        review_point = request.data.get("review_point")
        if not review_point:
            return Response({"detail": "review_point 必填"}, status=status.HTTP_400_BAD_REQUEST)
        from ..pipeline.checkpoints import CheckpointConflict, approve as cp_approve
        try:
            cp_approve(play, review_point, request.user, request.data.get("comment", ""))
        except CheckpointConflict as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(self.get_serializer(play).data)

    @action(detail=True, methods=["post"])
    def reject(self, request, pk=None):
        play = self.get_object()
        self._ensure_reviewer(play)
        review_point = request.data.get("review_point")
        if not review_point:
            return Response({"detail": "review_point 必填"}, status=status.HTTP_400_BAD_REQUEST)
        episode_id = request.data.get("episode")
        episode = None
        if episode_id not in (None, ""):
            if isinstance(episode_id, bool) or not str(episode_id).isdigit():
                raise ValidationError({"episode": "应为分集 ID"})
            episode = play.episodes.filter(pk=int(episode_id)).first()
            if episode is None:
                raise ValidationError({"episode": "该短剧下不存在此分集"})
        from ..pipeline.checkpoints import CheckpointConflict, reject as cp_reject
        try:
            cp_reject(play, review_point, request.user, request.data.get("comment", ""), episode)
        except CheckpointConflict as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        return Response(self.get_serializer(play).data)

    @action(detail=True, methods=["post"], url_path="preview-redo")
    def preview_redo(self, request, pk=None):
        """重做影响预检（Stage A3，只读）：不写库、不派发、不调模型。

        {granularity: SHOT|SCENE, target_id, asset_types?: [IMAGE|AUDIO], changes?: [dialogue|visual_prompt|...]}
        → {invalidated[], reusable[], video_impact[], rebuild_tasks[], estimated_tasks, conservative[], preview_version}

        权限：读权限即可（只读成员可预检影响范围；重做本身仍需 edit）。
        """
        play = self.get_object()
        from ..permissions import can_view_play
        if not can_view_play(request.user, play):
            raise PermissionDenied("无权访问该短剧")
        granularity = request.data.get("granularity")
        target_id = request.data.get("target_id")
        if not granularity or not target_id:
            return Response(
                {"detail": "granularity 和 target_id 必填"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        from ..pipeline.preview_redo import PreviewRedoError, preview_redo
        try:
            result = preview_redo(play, granularity, target_id,
                                  asset_types=request.data.get("asset_types"),
                                  changes=request.data.get("changes"))
        except PreviewRedoError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)

    @action(detail=True, methods=["post"])
    def redo(self, request, pk=None):
        play = self.get_object()
        self._ensure_reviewer(play)
        granularity = request.data.get("granularity")
        target_id = request.data.get("target_id")
        if not granularity or not target_id:
            return Response(
                {"detail": "granularity 和 target_id 必填"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        from ..pipeline.redo import RedoError, RedoStaleError, redo as redo_action
        try:
            redo_action(play, granularity, target_id,
                        asset_types=request.data.get("asset_types"),
                        preview_version=request.data.get("preview_version"))
        except RedoStaleError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_409_CONFLICT)
        except RedoError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(self.get_serializer(play).data)

    @action(detail=True, methods=["post"], url_path="batch-generate")
    def batch_generate(self, request, pk=None):
        """智能批量生成（主计划 3.5.3.7）：按场景/分集批量派发 IMAGE/AUDIO。

        {scope: "scene"|"episode", target_id, asset_types?: [IMAGE|AUDIO]}
        → {dispatched[], reused[], skipped[], summary}——逐镜头逐类型走既有
        守卫（已就绪跳过/在跑去重/无对白不派配音/计费闸逐任务判定），
        本端点只聚合呈现，不新增守卫语义、不合并供应商调用、不作废既有
        就绪资产。权限：edit（派发即付费）；冻结剧拒绝（4003）。
        """
        play = self.get_object()
        self._ensure_reviewer(play)
        if play.frozen_reason:
            resp = Response(
                {"detail": f"短剧已被平台管理员冻结：{play.frozen_reason}",
                 "reason": "play_frozen"},
                status=status.HTTP_403_FORBIDDEN,
            )
            resp.business_code = 4003
            return resp
        from ..pipeline.batch_generate import BatchGenerateError, batch_generate
        try:
            result = batch_generate(play, request.data.get("scope"),
                                    request.data.get("target_id"),
                                    asset_types=request.data.get("asset_types"))
        except BatchGenerateError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)

    @action(detail=True, methods=["post"], url_path="novel/adapt")
    def novel_adapt(self, request, pk=None):
        """小说→剧本改编（2026-11-23 组 2）：章节范围派发改编长任务。

        {from_chapter?, to_chapter?, episode_start?=1}
        → {task_record_id, from_chapter, to_chapter, block_count}。
        权限 edit；冻结剧 4003；已有进行中改编 409；产出去提案闸
        （已有剧集保护在提案应用侧，confirm_overwrite 走提案重发路径）。
        """
        play = self.get_object()
        self._ensure_reviewer(play)
        if play.frozen_reason:
            resp = Response(
                {"detail": f"短剧已被平台管理员冻结：{play.frozen_reason}",
                 "reason": "play_frozen"},
                status=status.HTTP_403_FORBIDDEN,
            )
            resp.business_code = 4003
            return resp
        from ..pipeline.novel_adaptation import AdaptError, dispatch_adaptation
        from ..pipeline.dispatcher import PlayFrozenError
        try:
            record = dispatch_adaptation(
                play,
                request.data.get("from_chapter"),
                request.data.get("to_chapter"),
                episode_start=int(request.data.get("episode_start") or 1),
            )
        except AdaptError as exc:
            msg = str(exc)
            status_code = 409 if "进行中" in msg else 400
            return Response({"detail": msg}, status=status_code)
        except PlayFrozenError as exc:
            resp = Response({"detail": str(exc), "reason": "play_frozen"},
                            status=status.HTTP_403_FORBIDDEN)
            resp.business_code = 4003
            return resp
        summary = (record.input_summary or {}).get("adapt", {})
        return Response({
            "task_record_id": record.pk,
            "from_chapter": summary.get("from_chapter"),
            "to_chapter": summary.get("to_chapter"),
            "block_count": summary.get("block_count"),
            "record_status": record.status,
        })

    @action(detail=True, methods=["post"], url_path="novel/adapt/resume")
    def novel_adapt_resume(self, request, pk=None):
        """改编断点续跑：{task_record_id} → 失败块重跑，已完成块不重跑。"""
        play = self.get_object()
        self._ensure_reviewer(play)
        from ..models import TaskRecord
        from ..pipeline.novel_adaptation import AdaptError, resume_adaptation
        record = TaskRecord.objects.filter(
            pk=request.data.get("task_record_id"), play=play,
            task_type=TaskType.NOVEL_ADAPT).first()
        if record is None:
            return Response({"detail": "改编任务不存在"}, status=status.HTTP_404_NOT_FOUND)
        try:
            record = resume_adaptation(play, record)
        except AdaptError as exc:
            msg = str(exc)
            status_code = 409 if "进行中" in msg else 400
            return Response({"detail": msg}, status=status_code)
        return Response({"task_record_id": record.pk, "record_status": record.status})

    @action(detail=True, methods=["get"], url_path="script/export")
    def script_export(self, request, pk=None):
        """剧本专业排版导出（2026-11-23 组 3）：fountain / txt / pdf。

        ?episode_id=必填&fmt=fountain|txt|pdf&script_version_id=（历史版可选）
        → 文件流（不包 JSON 信封，与成片/SRT 下载一致的交付形态）。
        权限：剧内读成员。排版为只读渲染，内容修改仍走 redo/提案链路。
        注意参数名用 fmt：format 是 DRF 保留字（内容协商）会被吞成 404。
        """
        play = self.get_object()
        from ..models import Episode
        episode = Episode.objects.filter(pk=request.query_params.get("episode_id"),
                                         play=play).first()
        if episode is None:
            return Response({"detail": "剧集不存在"}, status=status.HTTP_404_NOT_FOUND)
        script_version = None
        vid = request.query_params.get("script_version_id")
        if vid:
            from ..assistant.models import ScriptVersion
            script_version = ScriptVersion.objects.filter(
                pk=vid, play=play, episode=episode).first()
            if script_version is None:
                return Response({"detail": "剧本版本不存在"},
                                status=status.HTTP_404_NOT_FOUND)
        from ..pipeline import script_layout
        scenes = script_layout.scenes_for_export(episode, script_version)
        title = episode.title or f"{play.title} 第{episode.episode_number}集"
        fmt = (request.query_params.get("fmt") or "fountain").lower()
        if fmt not in ("fountain", "txt", "pdf"):
            return Response({"detail": "不支持的格式"}, status=status.HTTP_400_BAD_REQUEST)
        fountain = script_layout.render_fountain(title, scenes)
        if fmt == "fountain":
            response = HttpResponse(fountain, content_type="text/plain; charset=utf-8")
            response["Content-Disposition"] = (
                f'attachment; filename="episode-{episode.episode_number}.fountain"')
            return response
        if fmt == "txt":
            text = script_layout.render_plain_text(title, scenes)
            response = HttpResponse(text, content_type="text/plain; charset=utf-8")
            response["Content-Disposition"] = (
                f'attachment; filename="episode-{episode.episode_number}.txt"')
            return response
        pdf_bytes = script_layout.render_pdf(title, fountain)
        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = (
            f'attachment; filename="episode-{episode.episode_number}.pdf"')
        return response

    @action(detail=True, methods=["post"])
    def save(self, request, pk=None):
        """手动保存（Stage B1 任务 4.3）：剧本/角色版本快照（内容摘要判重，
        连续保存不制造重复版本）+ project.json 原子刷新。权限：edit。"""
        play = self.get_object()
        self._ensure_reviewer(play)
        from ..workspace.save_service import SaveError, save_play
        try:
            result = save_play(play, user=request.user)
        except SaveError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)

    @action(detail=True, methods=["post"], url_path="clear-cache")
    def clear_cache(self, request, pk=None):
        """清理项目缓存目录（Stage B1 任务 4.4）：只作用于本项目 cache/，
        原始资产/剧本版本/导出成片不受影响。权限：manage（不可逆删除）。"""
        play = self.get_object()
        from ..permissions import can_manage_play
        if not can_manage_play(request.user, play):
            raise PermissionDenied("仅项目所有者可清理缓存")
        from ..workspace.cleanup import CleanupError, clear_cache
        try:
            result = clear_cache(play, target=request.data.get("target", "cache"))
        except CleanupError as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(result)

    @action(detail=True, methods=["post"], url_path="export")
    def export_project(self, request, pk=None):
        """项目 ZIP 导出（Stage B2 任务 5.1）：数据库即时快照。
        权限：edit（导出含创作产物）。响应 X-Export-* 头明示排除项。"""
        play = self.get_object()
        self._ensure_reviewer(play)
        from ..workspace.export_service import export_response
        return export_response(play)

    @action(detail=False, methods=["post"], url_path="import-precheck")
    def import_precheck(self, request):
        """导入预检（Stage B2 任务 5.2）：只读校验，零写入。
        multipart 字段 file=ZIP。权限：认证用户（可创建项目者）。"""
        from ..workspace.import_precheck import ImportError_, precheck_import
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "缺少 file 字段（multipart 上传 ZIP）"},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            report = precheck_import(upload)
        except ImportError_ as exc:
            return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        return Response(report)

    @action(detail=False, methods=["post"], url_path="import-confirm")
    def import_confirm(self, request):
        """确认导入为新项目（Stage B2 任务 5.3）：预检 + 恢复。
        权限：认证用户（新项目属主为导入者）。"""
        from ..workspace.import_precheck import ImportError_, precheck_import
        from ..workspace.import_service import confirm_import
        upload = request.FILES.get("file")
        if upload is None:
            return Response({"detail": "缺少 file 字段（multipart 上传 ZIP）"},
                            status=status.HTTP_400_BAD_REQUEST)
        try:
            play, summary = confirm_import(upload, request.user)
        except ImportError_ as exc:
            status_code = getattr(exc, "status_code", 400)
            return Response({"detail": str(exc)}, status=status_code)
        return Response(summary)

    @action(detail=True, methods=["post"])
    def publish(self, request, pk=None):
        play = self.get_object()
        self._ensure_reviewer(play)
        if play.frozen_reason:
            # 业务码 4003：冻结剧拒绝发布
            resp = Response(
                {"detail": f"短剧已被平台管理员冻结：{play.frozen_reason}",
                 "reason": "play_frozen"},
                status=status.HTTP_403_FORBIDDEN,
            )
            resp.business_code = 4003
            return resp
        channel_id = request.data.get("channel")
        from apps.distribution.models import DistributionChannel, Publication
        if channel_id:
            channel = DistributionChannel.objects.filter(pk=channel_id, is_active=True).first()
            if channel is None:
                return Response({"detail": "渠道不存在或未启用"}, status=status.HTTP_400_BAD_REQUEST)
        else:
            channel = DistributionChannel.objects.filter(is_active=True).first()
            if channel is None:
                return Response({"detail": "无可用的发布渠道"}, status=status.HTTP_400_BAD_REQUEST)

        if not play.episodes.exists():
            return Response({"detail": "短剧没有分集，无法发布"}, status=status.HTTP_400_BAD_REQUEST)
        incomplete = play.episodes.exclude(status=EpisodeStatus.COMPLETED)
        if incomplete.exists():
            return Response(
                {"detail": f"有 {incomplete.count()} 集未完成，无法发布"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        # COMPLETED 但 current_video_asset 被级联清空（如 redo 后被置 SUPERSEDED）的集
        # 不能发布，否则 Publication.video_asset=None，ensure_rendition 产出坏发布记录。
        no_video = play.episodes.filter(current_video_asset__isnull=True)
        if no_video.exists():
            return Response(
                {"detail": f"有 {no_video.count()} 集缺少成片视频，无法发布"},
                status=status.HTTP_400_BAD_REQUEST,
            )
        from ..pipeline import state_machine
        state_machine.summary_play_status(play)
        if play.status != PlayStatus.PUBLISHED:
            try:
                state_machine.transition(play, PlayStatus.PUBLISHED)
            except state_machine.InvalidTransition as exc:
                return Response({"detail": str(exc)}, status=status.HTTP_400_BAD_REQUEST)
        from apps.distribution.rendition import RenditionBusy, ensure_rendition
        created = []
        for ep in play.episodes.all():
            pub, created_pub = Publication.objects.get_or_create(
                episode=ep, channel=channel,
                defaults={"title": ep.title, "video_asset": ep.current_video_asset},
            )
            if not created_pub and pub.video_asset_id != ep.current_video_asset_id:
                # 重做/重新合成后当前成片已换：不同步会让 is_stale 比较旧资产而判定
                # “未失效”，继续沿用并发布已废弃的旧渠道版本。
                pub.video_asset = ep.current_video_asset
                pub.title = ep.title
                pub.save(update_fields=["video_asset", "title"])
            try:
                ensure_rendition(pub)
            except RenditionBusy:
                pass
            created.append(pub.pk)
        return Response({"publication_ids": created, "play_status": play.status})


class EpisodeViewSet(PlayScopedViewSet):
    serializer_class = EpisodeSerializer
    queryset = Episode.objects.all()
    manage_actions = ("create", "destroy")

    def _writable_episode(self, request):
        episode = self.get_object()
        if not can_write_play(request.user, episode.play):
            raise PermissionDenied("无权修改该短剧的资源")
        return episode

    def _dispatch_exclusive(self, episode, task_type, busy_filter, busy_msg, **kwargs):
        from django.db import transaction
        from ..pipeline.dispatcher import dispatch_task
        with transaction.atomic():
            Episode.objects.select_for_update().get(pk=episode.pk)
            if TaskRecord.objects.filter(task_type=task_type, status__in=_ACTIVE_TASK_STATUSES,
                                         **busy_filter).exists():
                raise ValidationError({"detail": busy_msg})
            record = dispatch_task(task_type, play=episode.play, episode=episode,
                                   defer_send=True, **kwargs)
        return Response({"task_record_id": record.pk}, status=status.HTTP_202_ACCEPTED)

    @action(detail=True, methods=["post"])
    def preview(self, request, pk=None):
        episode = self._writable_episode(request)
        return self._dispatch_exclusive(episode, TaskType.PREVIEW_RENDER, {"episode": episode},
                                        "该集已有进行中的预览任务")

    @action(detail=True, methods=["post"])
    def derivatives(self, request, pk=None):
        episode = self._writable_episode(request)
        from ..tasks import DERIVATIVE_KINDS
        kinds = request.data.get("kinds") or []
        if not isinstance(kinds, list) or any(k not in DERIVATIVE_KINDS for k in kinds):
            raise ValidationError({"kinds": f"可选值: {', '.join(DERIVATIVE_KINDS)}"})
        video = Asset.objects.filter(episode=episode, asset_type=AssetType.VIDEO,
                                     status=AssetStatus.READY).order_by("-version").first()
        if video is None:
            raise ValidationError({"detail": "该集尚无可用成片"})
        return self._dispatch_exclusive(
            episode, TaskType.DERIVATIVES, {"asset": video}, "该成片已有进行中的衍生品任务",
            asset=video, input_summary={"video_asset_id": video.pk, "kinds": kinds},
        )

    @action(detail=True, methods=["post"])
    def poster(self, request, pk=None):
        episode = self._writable_episode(request)
        from django.db import transaction
        url = request.data.get("url")
        with transaction.atomic():
            video = Asset.objects.select_for_update().filter(
                episode=episode, asset_type=AssetType.VIDEO, status=AssetStatus.READY,
            ).order_by("-version").first()
            posters = ((video.metadata or {}).get("derivatives") or {}).get("posters") or [] if video else []
            if not url or url not in {p.get("url") for p in posters}:
                raise ValidationError({"url": "只能选择封面候选中的图片"})
            meta = dict(video.metadata or {})
            meta["poster_url"] = url
            Asset.objects.filter(pk=video.pk).update(metadata=meta)
            Episode.objects.filter(pk=episode.pk).update(poster_url=url)
        episode.refresh_from_db()
        return Response(self.get_serializer(episode).data)


class SceneViewSet(PlayScopedViewSet):
    serializer_class = SceneSerializer
    queryset = Scene.objects.all()
    play_filter_field = "episode__play"


class ShotViewSet(PlayScopedViewSet):
    serializer_class = ShotSerializer
    queryset = Shot.objects.all()
    play_filter_field = "scene__episode__play"


class CharacterViewSet(PlayScopedViewSet):
    serializer_class = CharacterSerializer
    queryset = Character.objects.all()


class VoiceProfileViewSet(viewsets.ModelViewSet):
    serializer_class = VoiceProfileSerializer
    queryset = VoiceProfile.objects.all()
    permission_classes = [IsAdminOnly]
