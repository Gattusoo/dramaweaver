"""出站请求的 SSRF 防护：只允许访问公网地址，或显式加白名单的内部主机。

服务端会按用户可控的 URL（音效、水印、LUT、AI 供应商返回的结果地址等）发起请求，
不加限制时可被用来读取云主机元数据（169.254.169.254）或探测内网服务。
"""
import ipaddress
import socket
from urllib.parse import urlsplit

from django.conf import settings


class UnsafeURLError(ValueError):
    """URL 指向不允许访问的地址。"""


def _allowed_internal_hosts():
    hosts = {h.strip().lower() for h in getattr(settings, "SAFE_FETCH_ALLOWED_HOSTS", []) if h.strip()}
    endpoint = getattr(settings, "STORAGE_OSS_ENDPOINT", "")
    if endpoint:
        # 自建对象存储（如内网 MinIO）是平台自己的地址，允许访问
        host = urlsplit(endpoint).hostname
        if host:
            hosts.add(host.lower())
    return hosts


# 标准库里 is_private/is_reserved 未覆盖、但实际属于内网/保留的网段：
# - 100.64.0.0/10 运营商级 NAT 共享地址（含阿里云 metadata 100.100.100.200）
# - 198.18.0.0/15 基准测试网段
# - fec0::/10 IPv6 site-local（已废弃，仍可能出现在内网配置中）
_EXTRA_BLOCKED_NETWORKS = (
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("198.18.0.0/15"),
    ipaddress.ip_network("fec0::/10"),
)


def _is_public_ip(ip):
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast
            or ip.is_reserved or ip.is_unspecified):
        return False
    return not any(ip in net for net in _EXTRA_BLOCKED_NETWORKS)


def check_url(url):
    """校验 URL 可安全访问；不安全时抛 UnsafeURLError。返回解析出的主机名。"""
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https"):
        raise UnsafeURLError(f"不支持的协议: {parts.scheme or '(空)'}")
    host = (parts.hostname or "").lower()
    if not host:
        raise UnsafeURLError("URL 缺少主机名")
    if host in _allowed_internal_hosts():
        return host
    try:
        infos = socket.getaddrinfo(host, parts.port or (443 if parts.scheme == "https" else 80),
                                   proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UnsafeURLError(f"无法解析主机 {host}: {exc}") from exc
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%", 1)[0])
        if not _is_public_ip(ip):
            raise UnsafeURLError(f"禁止访问内网或保留地址: {host} -> {ip}")
    return host


def check_peer(host, peer):
    """连接建立后校验实际对端 IP（防 DNS rebinding 的检查-使用时间差）。

    check_url 的 getaddrinfo 校验与 httpx 实际连接之间域名可能被重解析为内网 IP
    （DNS rebinding）。本函数在连接建立后、读取响应体前，用已建立 socket 的
    getpeername() 校验真实对端：白名单主机放行；非公网 IP 抛 UnsafeURLError，
    使跨剧/内网请求在读响应前被中断（拿不到响应体）。

    peer 为 socket.getpeername() 返回值 (ip, port)；拿不到对端信息（None）时不拦截，
    交由连接前 check_url 把关（保守不误伤）。
    """
    if host in _allowed_internal_hosts():
        return
    if not peer:
        return
    try:
        ip = ipaddress.ip_address(str(peer[0]).split("%", 1)[0])
    except ValueError:
        return
    if not _is_public_ip(ip):
        raise UnsafeURLError(f"实际连接目标非公网: {host} -> {ip}")
