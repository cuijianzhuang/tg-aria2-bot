"""Lazy ParseHub adapter: result mapping and download, no Telegram imports.

ParseHub is an optional video-component dependency with two hard
constraints that shape this module:

* it requires Python >= 3.12 (the bot itself still supports 3.11), and
* importing it pulls a heavy dependency tree.

So nothing here imports ``parsehub`` at module level. The backend reports
itself unavailable (with a human-readable reason) instead of crashing,
and ``backends.VideoResolver`` skips it in auto mode or surfaces the
reason when the user explicitly picked it / the platform needs it.
"""

import asyncio
import importlib.util
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)

PARSER_NAME = "parsehub"

_REF_KINDS = {
    "VideoRef": "video",
    "MusicRef": "music",
    "ImageRef": "image",
    "AniRef": "image",
    "LivePhotoRef": "image",
}
_KIND_BY_TYPE = {
    "video": "video",
    "image": "image",
    "multimedia": "multi",
    "music": "music",
    "richtext": "rich",
}


def unavailable_reason() -> str | None:
    """Why the ParseHub backend cannot run here, or None when it can."""
    if sys.version_info < (3, 12):
        return (
            "ParseHub 后端需要 Python ≥3.12，当前环境版本较低，已自动禁用；"
            "请在 Python 3.12+ 的视频组件环境中使用，或改用 yt-dlp/you-get"
        )
    if importlib.util.find_spec("parsehub") is None:
        return "ParseHub 后端未安装（视频组件缺少 parsehub 包），已自动禁用；请管理员安装视频组件后重试"
    return None


def available() -> bool:
    return unavailable_reason() is None


def _ref_item(ref) -> dict | None:
    url = getattr(ref, "url", None)
    if not url:
        return None
    kind = _REF_KINDS.get(type(ref).__name__)
    if kind is None:
        ext = (getattr(ref, "ext", None) or "").lower()
        kind = (
            "video" if ext in ("mp4", "mkv", "webm", "mov") else "music" if ext in ("mp3", "m4a", "flac") else "image"
        )
    item = {
        "url": url,
        "kind": kind,
        "ext": getattr(ref, "ext", None),
        "width": int(getattr(ref, "width", 0) or 0),
        "height": int(getattr(ref, "height", 0) or 0),
        "duration": int(getattr(ref, "duration", 0) or 0),
    }
    return item


def map_result(result, *, parser: str = PARSER_NAME):
    """Map a ParseHub AnyParseResult onto the extended MediaInfo model.

    Duck-typed on purpose: the mapping only reads ``type``/``title``/
    ``content``/``media`` attributes, so it stays testable without the
    parsehub package installed.
    """
    from bot.video.backends import MediaInfo, MediaItem

    raw_type = getattr(result, "type", "unknown")
    raw_type = str(getattr(raw_type, "value", raw_type) or "unknown")
    kind = _KIND_BY_TYPE.get(raw_type, "rich" if raw_type == "richtext" else "multi")
    media = getattr(result, "media", None)
    refs = list(media) if isinstance(media, (list, tuple)) else [media] if media is not None else []
    items = []
    for ref in refs:
        item = _ref_item(ref)
        if item is None:
            continue
        items.append(MediaItem(**item))
        # A LivePhoto is an image plus a short video clip; keep both halves.
        video_url = getattr(ref, "video_url", None)
        if video_url:
            items.append(
                MediaItem(
                    url=video_url,
                    kind="video",
                    ext=getattr(ref, "video_ext", None) or "mp4",
                    width=item["width"],
                    height=item["height"],
                    duration=item["duration"],
                )
            )
    content = (getattr(result, "content", "") or "").strip()
    title = (getattr(result, "title", "") or "").strip()
    if not title:
        title = (content.splitlines() or ["未命名内容"])[0]
    return MediaInfo(
        title=title[:200],
        parser=parser,
        qualities=["best"],
        size=None,
        kind=kind,
        content=content,
        items=items,
    )


def cookies_to_dict(cookies) -> dict | None:
    """Flatten the account system's cookie list into parsehub's dict form.

    ``account_cookies`` returns [{name, value, domain, ...}]; ParseHub's
    ``ParseHub.parse(cookie=...)`` accepts a plain {name: value} dict.
    """
    if not cookies:
        return None
    if isinstance(cookies, dict):
        return cookies or None
    result = {}
    for item in cookies:
        name = item.get("name") if isinstance(item, dict) else None
        if name:
            result[name] = item.get("value", "")
    return result or None


def cookies_file_to_dict(path) -> dict | None:
    """Read a Netscape cookie snapshot (the vault's temp file) as a dict."""
    if not path:
        return None
    import http.cookiejar

    jar = http.cookiejar.MozillaCookieJar(str(path))
    try:
        jar.load(ignore_discard=True, ignore_expires=True)
    except (OSError, http.cookiejar.LoadError):
        return None
    return {cookie.name: cookie.value for cookie in jar} or None


class ParseHubBackend:
    """Thin async wrapper around one lazily-created ParseHub instance."""

    def __init__(self, timeout: int = 45) -> None:
        self.timeout = max(5, timeout)
        self._hub = None

    def _load(self):
        if self._hub is None:
            import parsehub

            self._hub = parsehub.ParseHub()
        return self._hub

    async def parse(self, url: str, *, cookie: dict | None = None, proxy: str | None = None):
        """Return (MediaInfo, raw ParseResult) or raise ValueError."""
        reason = unavailable_reason()
        if reason is not None:
            raise ValueError(reason)
        hub = self._load()
        try:
            result = await asyncio.wait_for(hub.parse(url, proxy=proxy, cookie=cookie), self.timeout)
        except TimeoutError:
            raise ValueError(f"解析失败：ParseHub 解析超时（{self.timeout} 秒）") from None
        except Exception as exc:  # parsehub.ParseError and friends
            raise ValueError(f"解析失败：ParseHub 无法解析该链接（{exc}）") from None
        return map_result(result), result

    async def download(self, result, directory, *, proxy: str | None = None, callback=None) -> list[Path]:
        """Download a parsed result into *directory*; return local files.

        ``callback`` follows ParseHub's progress signature
        ``async callback(current, total, unit)`` with unit "bytes"/"count".
        ParseHub creates one subdirectory per result under *directory*.
        """
        try:
            outcome = await result.download(Path(directory), callback=callback, proxy=proxy)
        except Exception as exc:
            raise ValueError(f"ParseHub 下载失败（{exc}）") from None
        media = outcome.media
        files: list[Path] = []
        for entry in media if isinstance(media, (list, tuple)) else [media]:
            if entry is None:
                continue
            path = Path(entry.path)
            if path.is_file():
                files.append(path)
            video_path = getattr(entry, "video_path", None)
            if video_path and Path(video_path).is_file():
                files.append(Path(video_path))
        if not files and getattr(result, "media", None):
            raise ValueError("ParseHub 下载结束但没有产出任何文件")
        return files
