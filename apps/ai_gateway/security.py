"""ai_gateway 安全工具：base_url SSRF 防护与连通性测试错误脱敏。"""

from __future__ import annotations

import ipaddress
import logging
import socket
from urllib.parse import urlsplit

from django.conf import settings

logger = logging.getLogger(__name__)

# 已知云 metadata 端点域名（解析结果多为链路本地，但按名一并硬封）
METADATA_HOSTNAMES = {
    "metadata.google.internal",
    "metadata.azure.internal",
    "metadata.oraclecloud.com",
    "metadata.tencentcloud.internal",
}

ALLOW_LOCAL_FLAG = "AI_GATEWAY_ALLOW_LOCAL_BASE_URL"


def _resolve_ips(host: str) -> list:
    """解析主机名为 IP 列表：字面量 IP 直接解析；域名走 getaddrinfo，失败返回空（不拒绝）。"""
    try:
        return [ipaddress.ip_address(host)]
    except ValueError:
        pass
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return []  # 无法解析（离线/占位域名）——放行，由适配器调用时报错
    out = []
    for info in infos:
        try:
            out.append(ipaddress.ip_address(info[4][0]))
        except ValueError:
            continue
    return out


def validate_base_url(url) -> tuple[bool, str]:
    """校验 base_url 防 SSRF：仅 http/https；硬封链路本地与云 metadata；私网/回环需开关放行。

    返回 (ok, reason)；ok=False 时 reason 为中文拒绝原因。
    AI_GATEWAY_ALLOW_LOCAL_BASE_URL=True 时允许私网/回环（本地 Ollama/ComfyUI 场景），
    链路本地（含 169.254.169.254 云 metadata）任何时候都拒绝。
    """
    if not url:
        return True, ""
    try:
        parsed = urlsplit(str(url).strip())
    except ValueError:
        return False, "URL 格式错误"
    if parsed.scheme not in ("http", "https"):
        return False, "仅支持 http/https 协议地址"
    host = parsed.hostname
    if not host:
        return False, "URL 缺少主机名"
    if host.lower() in METADATA_HOSTNAMES:
        return False, "不允许云 metadata 地址"
    allow_local = bool(getattr(settings, ALLOW_LOCAL_FLAG, False))
    for ip in _resolve_ips(host):
        if ip.is_link_local:
            return False, "不允许链路本地地址（含云 metadata）"
        if not ip.is_global and not allow_local:
            return False, ("不允许内网/回环地址；本地模型（Ollama/ComfyUI）"
                           f"可设 settings.{ALLOW_LOCAL_FLAG}=True 放行")
    return True, ""


def classify_test_error(exc: Exception) -> str:
    """把连通性测试异常分类为脱敏文案，不回显原始异常字符串/响应体（防内网信息泄露）。"""
    import httpx

    if isinstance(exc, NotImplementedError):
        return f"适配器未实现: {exc}"
    if isinstance(exc, httpx.TimeoutException):
        return "连接超时（目标服务无响应）"
    if isinstance(exc, httpx.ConnectError):
        return "无法连接到目标服务（地址不可达或 DNS 解析失败）"
    if isinstance(exc, httpx.HTTPStatusError):
        status_code = exc.response.status_code
        if status_code in (401, 403):
            return "鉴权失败（API Key 无效或无权限）"
        if status_code == 404:
            return "路径不存在（base_url 可能不完整）"
        if status_code == 429:
            return "目标服务限流"
        return f"目标服务返回 HTTP {status_code}"
    if isinstance(exc, (httpx.HTTPError,)):
        return "网络请求失败"
    return f"连通性测试失败（{type(exc).__name__}，详见服务端日志）"
