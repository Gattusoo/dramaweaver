"""AI Gateway 出站统一入口（SSRF 加固）。

适配器全部 `httpx.Client(...)` 调用点收口到本模块的 `safe_request` / `safe_stream`：

- 连接前 `check_url`（拒绝内网/保留地址/云 metadata，白名单豁免）
- 连接后 `check_peer`（DNS rebinding 防护：check_url 校验与实际连接之间
  域名被重解析时，用 socket.getpeername() 校验真实对端 IP）
- `follow_redirects=False` + 逐跳校验重定向 Location（302 跳内网被拒）
- 重定向次数上限 `_MAX_REDIRECTS`（与 `apps/creation/tasks.py` 一致）

豁免策略：provider.base_url 的 host 在 `settings.SAFE_FETCH_ALLOWED_HOSTS`
时跳过公网校验（平台内网供应商如 Ollama/ComfyUI 合法场景）。
"""
from __future__ import annotations

import logging
from contextlib import contextmanager
from urllib.parse import urlsplit

import httpx
from django.conf import settings

from dvadmin.utils.url_safety import UnsafeURLError, check_peer, check_url

logger = logging.getLogger(__name__)

# 与 apps/creation/tasks.py 的 _download 保持一致
_MAX_REDIRECTS = 3


def _allowed_internal_hosts() -> set[str]:
    return {h.strip().lower() for h in getattr(settings, "SAFE_FETCH_ALLOWED_HOSTS", []) if h.strip()}


def _provider_whitelisted(provider) -> bool:
    """provider.base_url 的 host 命中白名单即视为平台内部供应商（豁免公网校验）。"""
    base_url = (getattr(provider, "base_url", "") or "").strip()
    if not base_url:
        return False
    try:
        host = (urlsplit(base_url).hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and host in _allowed_internal_hosts()


def _peer_of(response) -> tuple | None:
    """从 httpx.Response 取底层对端 (ip, port)；取不到返回 None（保守不误伤）。"""
    stream = response.extensions.get("network_stream")
    if stream is None:
        return None
    try:
        return stream.get_extra_info("server_addr")
    except Exception:  # noqa: BLE001 — httpx 后端差异兜底
        return None


def _check_response_peer(provider, response) -> None:
    """白名单豁免时跳过 peer 校验；否则按响应 URL 的 host 校验真实对端 IP。"""
    if _provider_whitelisted(provider):
        return
    host = (response.url.host or "").lower()
    check_peer(host, _peer_of(response))


def _next_location(response) -> str:
    """取 3xx 响应的下一跳 URL；next_request 缺失视为协议违规。"""
    nxt = response.next_request
    if nxt is None or not str(nxt.url):
        raise UnsafeURLError("重定向缺少 Location")
    return str(nxt.url)


def safe_request(provider, method: str, url: str, *, timeout: float | None = None, **kw) -> httpx.Response:
    """非流式安全请求：连接前 check_url、连接后 check_peer、禁自动重定向 + 逐跳校验。

    - 白名单 provider 直接放行（不走 check_url/check_peer）
    - 其余场景：每一跳（含首跳与所有 3xx 重定向）都做 check_url + check_peer
    - 超过 _MAX_REDIRECTS 次重定向抛 UnsafeURLError
    - 调用方自行 resp.raise_for_status() 与 resp.json()/resp.content
    """
    whitelisted = _provider_whitelisted(provider)
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        if not whitelisted:
            check_url(current)
        with httpx.Client(timeout=timeout, follow_redirects=False) as client:
            resp = client.request(method, current, **kw)
            _check_response_peer(provider, resp)
            if resp.is_redirect:
                current = _next_location(resp)
                continue
            return resp
    raise UnsafeURLError(f"重定向次数超过 {_MAX_REDIRECTS}")


@contextmanager
def safe_stream(provider, method: str, url: str, *, timeout: float | None = None, **kw):
    """流式安全请求：与 safe_request 同型，但返回 response context manager。

    用法：
        with safe_stream(provider, "POST", url, json=..., headers=...) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                ...

    校验时机：拿到响应头后立即 check_peer（读响应体前），与 `_download` 同型；
    重定向逐跳校验在拿到最终非 3xx 响应前完成。
    """
    whitelisted = _provider_whitelisted(provider)
    current = url
    for _ in range(_MAX_REDIRECTS + 1):
        if not whitelisted:
            check_url(current)
        with httpx.stream(method, current, timeout=timeout, follow_redirects=False, **kw) as resp:
            _check_response_peer(provider, resp)
            if resp.is_redirect:
                current = _next_location(resp)
                continue
            yield resp
            return
    raise UnsafeURLError(f"重定向次数超过 {_MAX_REDIRECTS}")


def safe_get(provider, url: str, *, timeout: float | None = None, **kw) -> httpx.Response:
    return safe_request(provider, "GET", url, timeout=timeout, **kw)


def safe_post(provider, url: str, *, timeout: float | None = None, **kw) -> httpx.Response:
    return safe_request(provider, "POST", url, timeout=timeout, **kw)
