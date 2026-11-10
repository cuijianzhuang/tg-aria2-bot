"""Download source parsing without Telegram, configuration or database dependencies."""

import os
import re
from typing import Literal, TypeAlias
from urllib.parse import parse_qsl, unquote, urlparse

LinkKind: TypeAlias = Literal["url", "magnet"]
Link: TypeAlias = tuple[LinkKind, str]

URL_RE = re.compile(r"^(?:https?|ftp|sftp)://\S+$", re.IGNORECASE)
# Match the complete URI, including optional parameters in any order.
MAGNET_RE = re.compile(r"\bmagnet:\?[^\s<>\"'，。；）】]+", re.IGNORECASE)


def extract_magnets(text: str) -> list[str]:
    magnets = []
    for match in MAGNET_RE.finditer(text):
        uri = match.group().rstrip(")]")
        params = parse_qsl(uri.split("?", 1)[1], keep_blank_values=True)
        if any(
            key.lower() == "xt" and value.lower().startswith("urn:btih:") and value[len("urn:btih:") :]
            for key, value in params
        ):
            # aria2 expects the standard lowercase scheme; preserve query data.
            magnets.append("magnet:" + uri.split(":", 1)[1])
    return magnets


# 裸 BT infohash（种子站常见的复制粘贴格式，没有 magnet: 前缀）：40 位十六进制。
# 只认这一种格式——BEP3 也允许 32 位 base32，但那跟普通随机字符串太像，
# 误判风险不值得，用户真要用 base32 hash 自己拼成完整 magnet 链接即可。
INFOHASH_RE = re.compile(r"^[a-fA-F0-9]{40}$")


def url_display_name(url: str) -> str:
    """Filename from the URL *path* only — basename(url) would drag the query
    string into the name (file.zip?key=abc)."""
    name = os.path.basename(unquote(urlparse(url).path))
    return name or url


def parse_link(text: str) -> Link | None:
    """Parse exactly one source; unlike message extraction, reject surrounding prose."""
    text = text.strip()
    if URL_RE.fullmatch(text):
        return "url", text
    if INFOHASH_RE.fullmatch(text):
        return "magnet", f"magnet:?xt=urn:btih:{text}"
    if MAGNET_RE.fullmatch(text):
        magnets = extract_magnets(text)
        if len(magnets) == 1:
            return "magnet", magnets[0]
    return None


def extract_links(text: str) -> list[Link]:
    """把消息按行拆开，挑出能识别成 url/magnet 的行；顺序无关的其它行
    （空行、说明文字等）直接忽略，不当错误处理。"""
    links = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if magnets := extract_magnets(line):
            links.extend(("magnet", uri) for uri in magnets)
        elif INFOHASH_RE.match(line):
            links.append(("magnet", f"magnet:?xt=urn:btih:{line}"))
        elif URL_RE.match(line):
            links.append(("url", line))
    return links
