"""creation views —— 智能体配方。"""
from django.db.models import Q
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response

from apps.ai_gateway.credentials import is_model_usable_for_user
from apps.ai_gateway.models import AIModel
from apps.ai_gateway.router import supports_tools
from apps.core.system.permissions import is_platform_admin

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
        self._assert_source_models_usable(request.user, src)
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
            assistant_model=src.assistant_model,
            assistant_failover=src.assistant_failover,
            assistant_models=src.assistant_models,
            assistant_budget=src.assistant_budget,
            assistant_compaction=src.assistant_compaction,
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

    @staticmethod
    def _assert_source_models_usable(user, src):
        """克隆前校验源配方全部模型引用（类型/存在性/助手不变量/可用性）。

        写路径（AgentConfigSerializer）会拒绝缺失 id、非列表降级链、
        非 LLM/不支持工具调用的助手模型；clone 不能把这类配置复制成
        写路径无法再编辑的副本。模板含他人 custom 模型同样拒绝。
        """
        invalid = []
        model_ids = set()

        def _collect_chain(field_name, chain):
            if chain is None:
                return []
            if not isinstance(chain, list):
                invalid.append(f"{field_name} 应为模型 id 列表")
                return []
            collected = []
            for mid in chain:
                if isinstance(mid, bool) or not isinstance(mid, int):
                    invalid.append(f"{field_name} 含非法模型 id: {mid!r}")
                    continue
                collected.append(mid)
                model_ids.add(mid)
            return collected

        for model in (src.llm_model, src.tts_model, src.image_model,
                      src.video_model, src.assistant_model):
            if model is not None:
                model_ids.add(model.pk)

        chains = {}
        for field_name, chain in (
            ("llm_failover", src.llm_failover),
            ("tts_failover", src.tts_failover),
            ("image_failover", src.image_failover),
            ("video_failover", src.video_failover),
            ("assistant_failover", src.assistant_failover),
        ):
            chains[field_name] = _collect_chain(field_name, chain)

        tier_ids = {}
        if src.assistant_models is not None:
            if not isinstance(src.assistant_models, dict):
                invalid.append("assistant_models 应为对象")
            else:
                for tier, mid in src.assistant_models.items():
                    if tier not in ("small", "medium", "large"):
                        invalid.append(f"assistant_models 含未知档位: {tier}")
                        continue
                    if mid is None:
                        continue
                    if isinstance(mid, bool) or not isinstance(mid, int):
                        invalid.append(f"assistant_models.{tier} 应为模型 id 或 null")
                        continue
                    tier_ids[tier] = mid
                    model_ids.add(mid)

        # 主模型对象已在内存；其余 id 一次查回
        known = {}
        for model in (src.llm_model, src.tts_model, src.image_model,
                      src.video_model, src.assistant_model):
            if model is not None:
                known[model.pk] = model
        missing = model_ids - set(known)
        if missing:
            known.update({
                m.pk: m
                for m in AIModel.objects.select_related("provider").filter(pk__in=missing)
            })
        for mid in sorted(missing):
            if mid not in known:
                invalid.append(f"模型 {mid} 不存在")

        # 助手字段写侧不变量：assistant_model/assistant_failover 须支持工具调用；
        # assistant_models 只要求 LLM（small 档允许无工具，供压缩摘要）。
        if src.assistant_model is not None and (
            src.assistant_model.model_type != "LLM"
            or not supports_tools(src.assistant_model)
        ):
            invalid.append(
                f"助手模型 {src.assistant_model.display_name} 须为支持工具调用的 LLM"
            )
        for tier, mid in tier_ids.items():
            model = known.get(mid)
            if model is None:
                continue
            if model.model_type != "LLM":
                invalid.append(f"assistant_models.{tier} 须为 LLM")
            elif tier != "small" and not supports_tools(model):
                invalid.append(f"assistant_models.{tier} 须支持工具调用")
        for mid in chains.get("assistant_failover", []):
            model = known.get(mid)
            if model is not None and not supports_tools(model):
                invalid.append(
                    f"助手降级模型 {model.display_name} 不支持工具调用"
                )

        problems = list(invalid)
        for mid in sorted(model_ids):
            model = known.get(mid)
            if model is None:
                continue
            ok, reason = is_model_usable_for_user(user, model)
            if not ok:
                problems.append(reason or f"模型 {model.display_name} 不可用")
        if problems:
            raise ValidationError(
                {"detail": "源配方包含当前用户不可用的模型：" + "；".join(problems)}
            )
