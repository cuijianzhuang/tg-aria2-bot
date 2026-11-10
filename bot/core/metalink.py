"""Split Metalink v3/v4 manifests into independently retryable downloads."""
import copy
import xml.etree.ElementTree as ET

MAX_METALINK_BYTES = 2 * 1024 * 1024
MAX_METALINK_FILES = 20
NAMESPACES = {"http://www.metalinker.org/", "urn:ietf:params:xml:ns:metalink"}


def split_metalink(content: bytes) -> list[tuple[str, bytes]]:
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
