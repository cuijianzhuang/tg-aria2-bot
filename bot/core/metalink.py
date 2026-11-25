"""Split Metalink v3/v4 manifests into independently retryable downloads."""
import copy
import xml.etree.ElementTree as ET

from bot.core.url_safety import UnsafeUrlError, check_url

MAX_METALINK_BYTES = 2 * 1024 * 1024
MAX_METALINK_FILES = 20
NAMESPACES = {"http://www.metalinker.org/", "urn:ietf:params:xml:ns:metalink"}


def split_metalink(content: bytes, *, allow_private: bool = False) -> list[tuple[str, bytes]]:
    if len(content) > MAX_METALINK_BYTES:
        raise ValueError("Metalink 清单不能超过 2 MiB。")
    # Reject DTDs in UTF-8 and UTF-16/32 before passing untrusted XML to ET.
    probe = content.replace(b"\x00", b"").upper()
    if b"<!DOCTYPE" in probe or b"<!ENTITY" in probe:
        raise ValueError("Metalink 不允许包含 DTD 或实体声明。")
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise ValueError("Metalink XML 格式无效。") from exc
    if not any(root.tag == f"{{{ns}}}metalink" for ns in NAMESPACES):
        raise ValueError("仅支持 Metalink v3 / v4 清单。")
    ns = root.tag[1:].split("}", 1)[0]
    tag = f"{{{ns}}}file"
    # 清单里的 <url> 是 aria2 真正会去抓的地址，跟普通下载链接走同一套
    # SSRF 校验：任一条指向本机/内网就整份清单拒绝（拆分后逐条放行会让
    # 用户以为清单被完整接受）。magnet:/torrent 等非 HTTP 类资源不在此列。
    for el in root.iter():
        if el.tag.rsplit("}", 1)[-1] == "url" and el.text and el.text.strip():
            target = el.text.strip()
            if target.split(":", 1)[0].lower() not in ("http", "https", "ftp", "sftp"):
                continue
            try:
                check_url(target, allow_private=allow_private)
            except UnsafeUrlError:
                raise ValueError("Metalink 包含指向本机或内网的下载地址，已拒绝整份清单。") from None
    files = list(root.iter(tag))
    if not 1 <= len(files) <= MAX_METALINK_FILES:
        raise ValueError(f"Metalink 需包含 1–{MAX_METALINK_FILES} 个文件，请分批发送。")
    result = []
    for index, selected in enumerate(files):
        single = copy.deepcopy(root)
        seen = 0
        for parent in single.iter():
            for child in list(parent):
                if child.tag == tag:
                    if seen != index:
                        parent.remove(child)
                    seen += 1
        name = selected.get("name") or f"文件 {index + 1}"
        result.append((name, ET.tostring(single, encoding="utf-8", xml_declaration=True)))
    return result
