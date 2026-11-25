"""CLI adapters with normalized metadata; no Telegram or persistence imports."""

import asyncio
import copy
import json
import logging
import re
import shutil
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bot.config import settings
from bot.video.accounts import parsehub_platform_for, platform_for, validate_url
from bot.video.process import capture

log = logging.getLogger(__name__)

PARSERS = ("yt-dlp", "you-get")
# CLI parsers plus the in-process ParseHub backend. Handlers validate the
# user-facing parser choice against ALL_PARSERS; command() only knows CLI.
ALL_PARSERS = (*PARSERS, "parsehub")
PROGRESS_MARKER = "__VIDEO_PROGRESS__"
GENERIC_PROBE_ERRORS = ("解析失败：", "没有可下载的视频格式", "解析器没有返回有效的视频信息")


def useful_probe_error(error: Exception) -> bool:
    return isinstance(error, ValueError) and not str(error).startswith(GENERIC_PROBE_ERRORS)


@dataclass
class MediaItem:
    """One downloadable media entry of a (possibly multi-file) result."""

    url: str
    kind: str  # video / image / music
    ext: str | None = None
    width: int = 0
    height: int = 0
    duration: int = 0


@dataclass
class MediaInfo:
    title: str
    parser: str
    qualities: list[str] = field(default_factory=lambda: ["best"])
    size: int | None = None
    # ParseHub-era extension; CLI parsers leave the defaults untouched, so
    # every existing video result is kind="video" with no items/content.
    kind: str = "video"  # video / image / multi / music / rich
    content: str = ""
    items: list[MediaItem] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def command(parser: str) -> list[str]:
    if parser not in PARSERS:
        raise ValueError("无效的视频解析器")
    return (
        [sys.executable, "-m", "yt_dlp"]
        if parser == "yt-dlp"
        else [sys.executable, "-c", "from you_get.__main__ import main; main()"]
    )


def common_args(parser: str, cookies: str | None = None, proxy: str | None = None) -> list[str]:
    result = command(parser)
    if parser == "yt-dlp":
        result += ["--ignore-config"]
        local_deno = Path(sys.executable).with_name("deno")
        if local_deno.is_file():
            result += ["--js-runtimes", f"deno:{local_deno}"]
        elif not shutil.which("deno") and shutil.which("node"):
            result += ["--js-runtimes", "node"]
        # Enable browser impersonation for Cloudflare-protected sites (X/Twitter)
        try:
            import curl_cffi  # noqa: F401

            result += ["--extractor-args", "generic:impersonate=chrome"]
        except ImportError:
            pass
    if cookies:
        result += ["--cookies", cookies]
    if proxy:
        result += ["--proxy" if parser == "yt-dlp" else "--http-proxy", proxy]
    return result


def parse_info(parser: str, output: str) -> MediaInfo:
    try:
        data = json.loads(output)
    except (ValueError, TypeError):
        raise ValueError("解析器没有返回有效的视频信息") from None
    if not isinstance(data, dict) or data.get("_type") in ("playlist", "multi_video") or data.get("entries"):
        raise ValueError("首版仅支持单视频，请发送具体视频页面")
    if data.get("is_live") or data.get("live_status") == "is_live":
        raise ValueError("暂不支持直播录制")
    title = str(data.get("title") or "视频")[:200]
    if parser == "yt-dlp":
        formats = data.get("formats") or []
        heights = sorted(
            {int(f["height"]) for f in formats if f.get("height") and f.get("vcodec") != "none"}, reverse=True
        )
        if not formats and not data.get("url"):
            raise ValueError("没有可下载的视频格式")
        return MediaInfo(
            title,
            parser,
            ["best", *[str(h) for h in heights[:10]]],
            data.get("filesize")
            or data.get("filesize_approx")
            or (
                sum(f.get("filesize") or f.get("filesize_approx") or 0 for f in data.get("requested_formats", []))
                or None
            ),
        )
    streams = data.get("streams") or {}
    if not isinstance(streams, dict) or not streams:
        raise ValueError("没有可下载的视频格式")
    return MediaInfo(title, parser, ["best", *list(streams)[:10]], None)


PROBE_CACHE_TTL = 300  # seconds; mirrors parse_hub_bot's parse_cache
PROBE_CACHE_MAXSIZE = 1000


class VideoResolver:
    def __init__(self, concurrency: int = 2, timeout: int = 45) -> None:
        self.slots = asyncio.Semaphore(max(1, concurrency))
        self.timeout = max(5, timeout)
        # Short-TTL probe cache: re-sending the same link (or re-opening
        # its confirmation card) must not re-run a 45s probe subprocess.
        # Keyed by url + parser + quality + account mode; failures are
        # never cached. ParseHub raw results ride along for download().
        self._cache: dict[tuple, tuple[float, MediaInfo]] = {}
        self._result_cache: dict[tuple, object] = {}
        self._parsehub = None

    def _cache_key(self, url: str, parser: str, quality: str, account_mode: str) -> tuple:
        return (url, parser, quality, account_mode or "")

    def _cache_get(self, key: tuple) -> MediaInfo | None:
        entry = self._cache.get(key)
        if entry is None:
            return None
        stored_at, info = entry
        if time.monotonic() - stored_at > PROBE_CACHE_TTL:
            self._cache.pop(key, None)
            self._result_cache.pop(key, None)
            return None
        return copy.deepcopy(info)

    def _cache_put(self, key: tuple, info: MediaInfo, result: object = None) -> None:
        if len(self._cache) >= PROBE_CACHE_MAXSIZE:
            oldest = min(self._cache, key=lambda k: self._cache[k][0])
            self._cache.pop(oldest, None)
            self._result_cache.pop(oldest, None)
        self._cache[key] = (time.monotonic(), copy.deepcopy(info))
        if result is not None:
            self._result_cache[key] = result

    def _parsehub_backend(self) -> object:
        if self._parsehub is None:
            from bot.video.parsehub_backend import ParseHubBackend

            self._parsehub = ParseHubBackend(self.timeout)
        return self._parsehub

    @staticmethod
    def _parser_order(url: str, parser: str) -> tuple[str, ...]:
        if parser != "auto":
            return (parser,)
        # ParseHub-only platforms (图文/文章/视频号…) go to ParseHub first:
        # the CLI parsers cannot handle them and would only burn a probe
        # timeout before failing generically. Everywhere else the proven
        # CLI order stays and ParseHub is the last-resort extension.
        if parsehub_platform_for(url):
            return ("parsehub", *PARSERS)
        return (*PARSERS, "parsehub")

    async def probe(
        self,
        url: str,
        *,
        parser: str = "auto",
        cookies: str | None = None,
        proxy: str | None = None,
        quality: str = "best",
        account_mode: str = "",
    ) -> MediaInfo:
        validate_url(url)
        key = self._cache_key(url, parser, quality, account_mode)
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        info = await self._probe_uncached(url, parser=parser, cookies=cookies, proxy=proxy, quality=quality, key=key)
        self._cache_put(key, info, self._result_cache.get(key))
        return info

    async def _probe_uncached(
        self,
        url: str,
        *,
        parser: str = "auto",
        cookies: str | None = None,
        proxy: str | None = None,
        quality: str = "best",
        key: tuple | None = None,
    ) -> MediaInfo:
        parsers = self._parser_order(url, parser)
        async with self.slots:
            error = None
            for name in parsers:
                if name == "parsehub":
                    try:
                        info, result = await self._probe_parsehub(url, cookies=cookies, proxy=proxy)
                    except ValueError as exc:
                        # In auto mode on CLI-covered platforms an
                        # unavailable ParseHub must stay invisible; only
                        # surface it when nothing else can serve the URL.
                        if parser == "auto" and parsers[0] != "parsehub" and "已自动禁用" in str(exc):
                            log.debug("parsehub skipped for %s: %s", url, exc)
                            continue
                        if error is None or (not useful_probe_error(error) and useful_probe_error(exc)):
                            error = exc
                        continue
                    if key is not None:
                        self._result_cache[key] = result
                    return info
                args = common_args(name, cookies, proxy)
                if name == "yt-dlp":
                    args += ["-f", format_selector(quality)]
                args += (
                    ["--dump-single-json", "--skip-download", "--no-playlist", "--no-warnings", "--", url]
                    if name == "yt-dlp"
                    else ["--json", url]
                )
                try:
                    return parse_info(name, await capture(args, self.timeout))
                except (ValueError, FileNotFoundError) as exc:
                    # Preserve a useful yt-dlp failure when the fallback has only a generic error.
                    if error is None or (not useful_probe_error(error) and useful_probe_error(exc)):
                        error = exc
            if isinstance(error, ValueError):
                if platform_for(url) == "twitter" and str(error) == "解析失败：站点不支持、需登录或服务暂不可用":
                    raise ValueError(
                        "X 解析失败：请尝试导入 X Cookie 并选择个人/共享账号，或更新 yt-dlp 后重试"
                    ) from None
                raise ValueError(str(error)) from None
            raise ValueError("解析器未安装，请启用视频组件")

    async def _probe_parsehub(
        self, url: str, *, cookies: str | None = None, proxy: str | None = None
    ) -> tuple[MediaInfo, object]:
        from bot.video.parsehub_backend import cookies_file_to_dict

        return await self._parsehub_backend().parse(url, cookie=cookies_file_to_dict(cookies), proxy=proxy)

    async def download_parsehub(
        self,
        url: str,
        directory: str | Path,
        *,
        cookies: list | dict | None = None,
        proxy: str | None = None,
        quality: str = "best",
        account_mode: str = "",
        callback: object = None,
    ) -> list[Path]:
        """Download via the ParseHub result cached by probe() when possible.

        ``cookies`` is the account system's cookie list (or dict); it is
        only needed on a cache miss, where the URL is parsed again.
        """
        from bot.video.parsehub_backend import cookies_to_dict

        key = self._cache_key(url, "parsehub", quality, account_mode)
        result = self._result_cache.get(key)
        if result is None:
            # The worker probes with parser=<job parser>; also accept the
            # "auto" cache entry before paying for a second parse.
            result = self._result_cache.get(self._cache_key(url, "auto", quality, account_mode))
        if result is None:
            _, result = await self._parsehub_backend().parse(url, cookie=cookies_to_dict(cookies), proxy=proxy)
        return await self._parsehub_backend().download(result, directory, proxy=proxy, callback=callback)


def format_selector(quality: str) -> str:
    if quality != "best" and (not quality.isdigit() or not 1 <= int(quality) <= 16384):
        raise ValueError("无效的画质选择")
    return (
        "bestvideo*+bestaudio/best"
        if quality == "best"
        else f"bestvideo*[height<={quality}]+bestaudio/best[height<={quality}]"
    )


def download_args(
    parser: str,
    url: str,
    directory: str | Path,
    quality: str = "best",
    cookies: str | None = None,
    proxy: str | None = None,
) -> list[str]:
    validate_url(url)
    args = common_args(parser, cookies, proxy)
    if parser == "yt-dlp":
        selector = format_selector(quality)
        # Serialize the entire native progress dict. Interpolating missing
        # fields separately emits yt-dlp's NA sentinel, which is not JSON.
        template = PROGRESS_MARKER + "%(progress)j"
        args += [
            "--no-playlist",
            "--no-warnings",
            "--no-colors",
            "--socket-timeout",
            "20",
            "--retries",
            "3",
            "--fragment-retries",
            "3",
            "--retry-sleep",
            "2",
            "--newline",
            "--progress",
            "--continue",
            "--keep-fragments",
            "--progress-template",
            template,
            "--print",
            "after_move:__VIDEO_OUTPUT__%(filepath)s",
            "-f",
            selector,
        ]
        # Allow explicit format control: mp4 (best compatibility), mkv (all codecs),
        # auto (yt-dlp decides based on stream format).
        output_format = getattr(settings, "video_output_format", "mp4").lower()
        if output_format in ("mp4", "mkv"):
            args += ["--merge-output-format", output_format]
        args += [
            "-o",
            str(Path(directory) / "media.%(ext)s"),
            "--",
            url,
        ]
        return args
    if quality != "best":
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", quality):
            raise ValueError("无效的格式编号")
        args += ["--format", quality]
    return args + ["--output-dir", str(directory), "--output-filename", "media", url]


def progress_event(line: str) -> dict | None:
    if PROGRESS_MARKER not in line:
        return None
    try:
        data = json.loads(line.split(PROGRESS_MARKER, 1)[1])
    except ValueError:
        return None
    if not isinstance(data, dict):
        return None
    downloaded = data.get("downloaded_bytes", data.get("downloaded"))
    if not isinstance(downloaded, (int, float)) or isinstance(downloaded, bool) or downloaded < 0:
        return None
    import math

    if not math.isfinite(downloaded):
        return None
    return {
        "downloaded": int(downloaded),
        "total": data.get("total_bytes", data.get("total")),
        "estimate": data.get("total_bytes_estimate", data.get("estimate")),
        "filename": data.get("filename", "stream"),
        "status": data.get("status"),
    }
