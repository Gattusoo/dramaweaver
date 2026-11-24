"""creation views —— 智能体配方。"""
from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from dvadmin.system.permissions import is_platform_admin

from ..models import AgentConfig
from ..serializers import AgentConfigSerializer


class AgentConfigViewSet(viewsets.ModelViewSet):
    serializer_class = AgentConfigSerializer
    queryset = AgentConfig.objects.all()
    # 配方是用户资源（非剧资源）：所有认证用户可自建，模板公共可读
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        qs = super().get_queryset()
        user = self.request.user
        if not user.is_authenticated:
            return qs.none()
        if is_platform_admin(user):
            return qs
        return qs.filter(Q(created_by=user) | Q(is_template=True))

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def check_object_permissions(self, request, obj):
        super().check_object_permissions(request, obj)
        if not is_platform_admin(request.user):
            if self.action == "clone":
                if not (obj.created_by == request.user or obj.is_template):
                    raise PermissionDenied("无权克隆其他用户的智能体配方")
                return
            if obj.is_template and request.method not in ("GET", "HEAD", "OPTIONS"):
                raise PermissionDenied("系统预置模板仅供查看")
            if not (obj.created_by == request.user or obj.is_template):
                raise PermissionDenied("无权访问其他用户的智能体配方")

    @action(detail=True, methods=["post"], url_path="clone")
    def clone(self, request, pk=None):
        src = self.get_object()
        clone = AgentConfig.objects.create(
            name=f"{src.name} (副本)",
            description=src.description,
            llm_model=src.llm_model,
            tts_model=src.tts_model,
            image_model=src.image_model,
            video_model=src.video_model,
            llm_failover=src.llm_failover,
            tts_failover=src.tts_failover,
            image_failover=src.image_failover,
            video_failover=src.video_failover,
            retry_count=src.retry_count,
            retry_backoff=src.retry_backoff,
            fallback_on_limit=src.fallback_on_limit,
            default_audit_mode=src.default_audit_mode,
            default_redo_granularity=src.default_redo_granularity,
            consistency_mode=src.consistency_mode,
            prompt_templates=src.prompt_templates,
            style_params=src.style_params,
            is_template=False,
            created_by=request.user,
        )
        return Response(
            self.get_serializer(clone).data, status=status.HTTP_201_CREATED
        )
