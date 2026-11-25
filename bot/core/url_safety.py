"""共享的下载地址安全校验（SSRF 防护）。

背景：机器人会把用户给的 URL 直接喂给 aria2 下载、产物还能回传 Telegram。
如果不拦内网地址，白名单用户（或被盗的账号）就能让服务器去抓云厂商的
元数据接口（169.254.169.254）、本机或局域网服务并把内容取回来。RSS 订阅
路径最早加了这层校验，这里把它抽成共享模块，RSS 与主下载路径共用。

校验口径（与原 RSS validate_url 一致）：
- 只允许调用方给定的协议；
- 主机名是 localhost / *.localhost / *.local / *.internal 直接拒；
- 主机是字面 IP（含 inet_aton 风格的整数/八进制/缩写写法）且属于私有、
  回环、链路本地、保留、组播或未指定地址时拒。

已知边界：**不做 DNS 解析校验**——域名在校验时解析到公网、连接时被
rebinding 到内网的情况拦不住（aria2 自己解析域名，这里没有连接时钩子）。
威胁模型与开关见 .env.example 里 ALLOW_PRIVATE_DOWNLOAD_URLS 的说明；
确有从局域网下载的需求时可以打开开关整体放行（自担风险）。
"""
import ipaddress
import socket
from urllib.parse import urlparse

DOWNLOAD_SCHEMES = ("http", "https", "ftp", "sftp")

_BLOCKED_SUFFIXES = (".localhost", ".local", ".internal")


class UnsafeUrlError(ValueError):
    """地址指向本机/内网等不允许下载的目标。消息可直接展示给用户。"""


def _ip_literal(host: str):
    """把主机名按「字面 IP」解析：标准写法走 ipaddress，整数/缩写等
    inet_aton 兼容写法走 inet_aton；普通域名返回 None（不做 DNS 查询）。"""
    try:
        return ipaddress.ip_address(host)
    except ValueError:
        pass
    try:
        packed = socket.inet_aton(host)
    except (OSError, UnicodeError):
        return None
    return ipaddress.IPv4Address(packed)


def is_blocked_host(host: str) -> bool:
    host = (host or "").strip().lower().rstrip(".")
    if not host:
        return True
    if host == "localhost" or host.endswith(_BLOCKED_SUFFIXES):
        return True
    ip = _ip_literal(host)
    if ip is None:
        return False
    return bool(
        ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_reserved
        or ip.is_multicast
        or ip.is_unspecified
    )


def check_url(
    url: str,
    *,
    schemes: tuple[str, ...] = DOWNLOAD_SCHEMES,
    allow_private: bool = False,
    blocked_message: str = "下载地址指向本机或内网地址，出于安全原因已拒绝。",
) -> str:
    """校验一个待下载 URL，返回 strip 后的原样 URL；不合规抛 UnsafeUrlError。"""
    url = url.strip()
    parsed = urlparse(url)
    if parsed.scheme.lower() not in schemes or not parsed.hostname:
        allowed = "/".join(schemes)
        raise UnsafeUrlError(f"下载地址必须是 {allowed}:// 开头的链接。")
    if not allow_private and is_blocked_host(parsed.hostname):
        raise UnsafeUrlError(blocked_message)
    return url
