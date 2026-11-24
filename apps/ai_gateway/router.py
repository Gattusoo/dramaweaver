"""路由决策与 A+B 混合降级链解析。

三层路由决策：
1. 从 AgentConfig 读取主模型 FK -> AIModel -> AIProvider
2. 熔断器 OPEN 的 provider 跳过
3. 限流超限的 provider 等待/降级

A+B 降级链：
- 方案 A：AgentConfig 配方级 failover（按数组顺序）
- 方案 B：failover 为空 -> 同 type 的 active AIProvider 按 priority 排序
- 均跳过已尝试 + 熔断中的 provider
"""
from .models import AIModel, AIProvider, ModelType, ProviderType

# provider_type -> (主模型 FK 字段, failover 字段)
_PROVIDER_FIELDS = {
    "LLM": ("llm_model", "llm_failover", ModelType.LLM),
    "TTS": ("tts_model", "tts_failover", ModelType.TTS),
    "IMAGE": ("image_model", "image_failover", ModelType.IMAGE),
    "VIDEO": ("video_model", "video_failover", ModelType.VIDEO),
}


def get_primary_model(agent_config, provider_type):
    field, _, _ = _PROVIDER_FIELDS[provider_type]
    return getattr(agent_config, field, None)


def resolve_failover_models(agent_config, provider_type):
    """方案 A：配方级 failover 字段 -> AIModel 列表。"""
    _, failover_field, _ = _PROVIDER_FIELDS[provider_type]
    ids = getattr(agent_config, failover_field, None) or []
    if not isinstance(ids, list):
        return []
    models = list(AIModel.objects.filter(pk__in=ids))
    # 保持 failover 数组顺序
    order = {mid: i for i, mid in enumerate(ids)}
    models.sort(key=lambda m: order.get(m.pk, 999))
    return models


def global_fallback(provider_type, user=None):
    """方案 B：同 type 的 active provider 按 priority 排序，各取其一个模型。

    user 提供时跳过属主无凭证的 custom 节点（决策 3/9）；user=None 保持旧行为（不校验凭证）。
    """
    from .credentials import resolve_credential
    from .models import ProviderVisibility

    providers = AIProvider.objects.filter(
        provider_type=provider_type, is_active=True
    ).order_by("priority")
    chain = []
    for provider in providers:
        # custom 节点必须有 user 且 user 有凭证才参与降级
        if provider.visibility == ProviderVisibility.CUSTOM:
            if user is None or resolve_credential(user, provider) is None:
                continue
        model = AIModel.objects.filter(
            provider=provider, is_active=True, model_type=provider_type
        ).first()
        if model is None:
            model = AIModel.objects.filter(provider=provider, is_active=True).first()
        if model is not None:
            chain.append((model, provider))
    return chain


def build_candidate_chain(agent_config, provider_type, breaker=None, tried_providers=None, user=None):
    """构建有序候选列表 [(AIModel, AIProvider)]，跳过熔断 OPEN、已尝试与无凭证的 custom 节点。

    user 提供时，custom 节点需 user 有启用凭证（决策 3/9）；user=None 保持旧行为。
    """
    from .credentials import resolve_credential
    from .models import ProviderVisibility

    tried = set(tried_providers or [])
    candidates = []

    def _add(model, provider):
        if model is None or provider is None:
            return
        # 管理员停用的模型/provider 不参与路由（含主模型与配方 failover）
        if not model.is_active or not provider.is_active:
            return
        if provider.pk in tried:
            return
        # custom 节点必须有 user 且 user 有凭证
        if provider.visibility == ProviderVisibility.CUSTOM:
            if user is None or resolve_credential(user, provider) is None:
                return
        # 只读检查：构建候选链不得占用 HALF_OPEN 探测名额
        if breaker is not None and not breaker.is_available(provider.pk):
            return
        candidates.append((model, provider))

    primary = get_primary_model(agent_config, provider_type)
    _add(primary, primary.provider if primary else None)

    for model in resolve_failover_models(agent_config, provider_type):
        _add(model, model.provider)

    for model, provider in global_fallback(provider_type, user=user):
        _add(model, provider)

    return _dedupe(candidates)


def _dedupe(candidates):
    """去重（同 provider 仅保留首个候选）。"""
    seen = set()
    deduped = []
    for model, provider in candidates:
        if provider.pk in seen:
            continue
        seen.add(provider.pk)
        deduped.append((model, provider))
    return deduped


def supports_tools(model):
    return bool((getattr(model, "capabilities", None) or {}).get("supports_tools"))


def supports_vision(model):
    return bool((getattr(model, "capabilities", None) or {}).get("supports_vision"))


def build_vision_chain(agent_config, breaker=None, user=None):
    """视觉分析候选链（Phase 2.3）：只保留 capabilities.supports_vision 为真的 LLM。

    顺序：assistant_model → assistant_failover/llm_failover → 全局 LLM 备用。
    与 build_chat_chain 分开：视觉模型可以不支持工具调用，但必须支持图片输入。
    user 提供时跳过属主无凭证的 custom 节点。
    """
    from .credentials import resolve_credential
    from .models import ProviderVisibility

    primary = getattr(agent_config, "assistant_model", None) or getattr(agent_config, "llm_model", None)
    ids = getattr(agent_config, "assistant_failover", None) or getattr(agent_config, "llm_failover", None) or []
    failover = []
    if isinstance(ids, list) and ids:
        order = {mid: i for i, mid in enumerate(ids)}
        failover = sorted(AIModel.objects.filter(pk__in=ids).select_related("provider"),
                          key=lambda m: order.get(m.pk, 999))
    ordered = ([(primary, primary.provider)] if primary else [])
    ordered += [(m, m.provider) for m in failover]
    ordered += global_fallback(ProviderType.LLM, user=user)
    out = []
    for model, provider in ordered:
        if not (model and provider and model.is_active and provider.is_active and supports_vision(model)):
            continue
        if provider.visibility == ProviderVisibility.CUSTOM:
            if user is None or resolve_credential(user, provider) is None:
                continue
        if breaker is not None and not breaker.is_available(provider.pk):
            continue
        out.append((model, provider))
    return _dedupe(out)


def build_chat_chain(agent_config, breaker=None, user=None):
    """助手（工具调用）候选链：assistant_model → assistant_failover（未配置时 llm_model /
    llm_failover）→ 全局 LLM 备用；只保留 capabilities.supports_tools 为真的模型。
    user 提供时跳过属主无凭证的 custom 节点。"""
    from .credentials import resolve_credential
    from .models import ProviderVisibility

    primary = getattr(agent_config, "assistant_model", None) or getattr(agent_config, "llm_model", None)
    ids = getattr(agent_config, "assistant_failover", None) or getattr(agent_config, "llm_failover", None) or []
    failover = []
    if isinstance(ids, list) and ids:
        order = {mid: i for i, mid in enumerate(ids)}
        failover = sorted(AIModel.objects.filter(pk__in=ids).select_related("provider"),
                          key=lambda m: order.get(m.pk, 999))
    ordered = ([(primary, primary.provider)] if primary else [])
    ordered += [(m, m.provider) for m in failover]
    ordered += global_fallback(ProviderType.LLM, user=user)
    out = []
    for model, provider in ordered:
        if not (model and provider and model.is_active and provider.is_active and supports_tools(model)):
            continue
        if provider.visibility == ProviderVisibility.CUSTOM:
            if user is None or resolve_credential(user, provider) is None:
                continue
        if breaker is not None and not breaker.is_available(provider.pk):
            continue
        out.append((model, provider))
    return _dedupe(out)


# ─── 多级模型选择（主计划 3.3.2/3.3.3 成本优化）────────────────────────────

def select_model_for_task(agent_config, complexity, *, supports_tool_calls=True):
    """按任务复杂度选档（design D1）：<0.4 → small、0.4-0.7 → medium、>0.7 → large。

    返回 AIModel 或 None（档位未配置/无效时 None，调用方回退 assistant 候选链——
    行为与单档配置一致）。supports_tool_calls=False 供摘要等无需工具的辅助调用
    （可命中未开 supports_tools 的小模型）。"""
    tiers = getattr(agent_config, "assistant_models", None) or {}
    if not isinstance(tiers, dict):
        return None
    try:
        complexity = float(complexity)
    except (TypeError, ValueError):
        return None
    tier_name = "small" if complexity < 0.4 else ("medium" if complexity <= 0.7 else "large")
    pk = tiers.get(tier_name)
    if not pk:
        return None
    from .models import AIModel
    model = AIModel.objects.select_related("provider").filter(
        pk=pk, is_active=True, provider__is_active=True).first()
    if model is None:
        return None
    if supports_tool_calls and not supports_tools(model):
        return None
    return model


def _price_points_of(provider) -> int | None:
    """节点展示单价（分）：platform → None（按用量计价，前端读 cost_config）；
    custom → SERVICE_FEE_PER_CALL 服务费率。"""
    from django.conf import settings as dj_settings

    from .models import ProviderVisibility

    if provider.visibility == ProviderVisibility.PLATFORM:
        return None
    return int(getattr(dj_settings, "SERVICE_FEE_PER_CALL", 0) or 0)


def annotate_chain_with_source(chain, user=None):
    """给降级链节点加 source ∈ {"platform", "credential"}（由 provider.visibility 推导）。

    - platform 节点 → source="platform"，price_points=None（按用量计价）
    - custom 节点 + user 有凭证 → source="credential"，price_points=服务费率
    - custom 节点 + user 无凭证 / user=None → 节点剔除（调用方据此跳过）

    返回新列表，元素为 dict：{model, provider, source, price_points}。
    """
    from .credentials import resolve_credential
    from .models import ProviderVisibility

    out = []
    for model, provider in chain:
        if provider.visibility == ProviderVisibility.PLATFORM:
            out.append({"model": model, "provider": provider,
                        "source": "platform", "price_points": None})
            continue
        # custom
        cred = resolve_credential(user, provider) if user is not None else None
        if cred is None:
            continue  # 无凭证节点跳过
        out.append({"model": model, "provider": provider,
                    "source": "credential", "price_points": _price_points_of(provider)})
    return out


def filter_cross_route_fallback(annotated_chain, enabled=None):
    """跨路线回退开关（决策 9）：默认拒绝"平台→我的渠道"方向的回退。

    链上"我的渠道（credential）之后又出现平台模型（platform）"即视为
    平台→我的渠道方向的回退——ENABLE_CROSS_ROUTE_FALLBACK=False（默认）时
    该平台回退节点被剔除；反方向（platform 在前 custom 在后）始终允许。

    返回过滤后的新列表；被剔除节点记录调用方可见的 cross_route_blocked 标记
    （append 到返回列表的 attached 属性，事件层消费）。
    """
    from django.conf import settings as dj_settings

    if enabled is None:
        enabled = getattr(dj_settings, "ENABLE_CROSS_ROUTE_FALLBACK", False)
    seen_credential = False
    out = []
    blocked = []
    for node in annotated_chain:
        source = node.get("source", "platform") if isinstance(node, dict) else "platform"
        if source == "credential":
            seen_credential = True
            out.append(node)
            continue
        # platform 节点出现在 credential 之后 = 平台 → 我的渠道方向的回退
        if seen_credential and not enabled:
            blocked.append(node)
            continue
        out.append(node)
    result = _FilteredChain(out)
    result.cross_route_blocked = blocked
    return result


class _FilteredChain(list):
    """带跨路线拦截标记的链表。"""

    def __init__(self, items):
        super().__init__(items)
        self.cross_route_blocked = []

