"""用户凭证解析链（决策 3/9 的凭证侧）。

- platform 节点 → 平台 Key（AIProvider.api_key_encrypted）
- custom 节点 → 属主的 UserProviderCredential（user=属主, provider, is_active=True）
- 属主无凭证 → 该节点不可用（返回 None），由降级链跳过

凭证属用户个人（非剧资源）：属主也不能读成员的凭证；解析链只看属主。
"""
from __future__ import annotations

import logging
from typing import Optional

from .models import AIModel, AIProvider, ProviderVisibility, UserProviderCredential

logger = logging.getLogger(__name__)


def resolve_credential(user, provider: AIProvider) -> Optional[UserProviderCredential]:
    """取 user 对 provider 的可用凭证；无则返回 None（调用方按"节点不可用跳过"处理）。

    - platform 行不查凭证表（平台 Key 走 AIProvider.api_key_encrypted）
    - custom 行：provider 必须由 user 本人创建，再查 (user, provider, is_active=True)
      唯一凭证。运行期同样校验属主，避免历史越权绑定行（写侧已拒绝新建）继续生效。
    """
    if provider.visibility == ProviderVisibility.PLATFORM:
        return None
    if user is None or provider.created_by_id != user.id:
        return None
    return UserProviderCredential.objects.filter(
        user=user, provider=provider, is_active=True,
    ).first()


def resolve_api_key(user, provider: AIProvider) -> tuple[Optional[str], str]:
    """取调用 provider 用的 API Key。

    返回 (key, source)；source ∈ {"platform", "credential", ""}。
    - platform 节点 → (平台 Key, "platform")，无 Key 返回 (None, "")
    - custom 节点 → 属主凭证解密 → (key, "credential")；无凭证 → (None, "")
    """
    if provider.visibility == ProviderVisibility.PLATFORM:
        key = (provider.api_key_encrypted or "").strip()
        return (key, "platform") if key else (None, "")
    cred = resolve_credential(user, provider)
    if not cred:
        return None, ""
    try:
        return cred.get_key(), "credential"
    except Exception:  # noqa: BLE001 — 密文损坏视为无凭证
        logger.warning("credential decrypt failed user=%s provider=%s", user.pk, provider.pk)
        return None, ""


def is_model_usable_for_user(user, model: AIModel) -> tuple[bool, str]:
    """配方引用校验：用户是否可使用指定模型。

    - platform 节点：任何已认证用户均可用（平台付费）
    - custom 节点：仅当 (user, provider) 有启用凭证时可用

    返回 (ok, reason)；ok=False 时 reason 为中文拒绝原因。
    """
    provider = model.provider
    if not provider.is_active or not model.is_active:
        return False, "模型或供应商已停用"
    if provider.visibility == ProviderVisibility.PLATFORM:
        return True, ""
    # custom 节点：provider 必须是用户自己创建的，且用户有启用凭证
    if provider.created_by_id != user.id:
        return False, f"模型 {model.display_name} 属于其他用户的私有供应商"
    cred = resolve_credential(user, provider)
    if not cred:
        return False, f"自定义供应商 {provider.name} 缺少凭证，请先在用户凭证中配置"
    return True, ""
