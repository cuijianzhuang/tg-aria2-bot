"""CLI adapters with normalized metadata; no Telegram or persistence imports."""

import asyncio
import json
import re
import shutil
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path

from bot.config import settings
from bot.video.accounts import platform_for, validate_url
from bot.video.process import capture

PARSERS = ("yt-dlp", "you-get")
PROGRESS_MARKER = "__VIDEO_PROGRESS__"
GENERIC_PROBE_ERRORS = ("解析失败：", "没有可下载的视频格式", "解析器没有返回有效的视频信息")


def useful_probe_error(error: Exception) -> bool:
    return isinstance(error, ValueError) and not str(error).startswith(GENERIC_PROBE_ERRORS)


@dataclass
class MediaInfo:
    title: str
    parser: str
    qualities: list[str] = field(default_factory=lambda: ["best"])
    size: int | None = None

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


class VideoResolver:
    def __init__(self, concurrency: int = 2, timeout: int = 45) -> None:
        self.slots = asyncio.Semaphore(max(1, concurrency))
        self.timeout = max(5, timeout)

    async def probe(
        self,
        url: str,
        *,
        parser: str = "auto",
        cookies: str | None = None,
        proxy: str | None = None,
        quality: str = "best",
    ) -> MediaInfo:
        validate_url(url)
        # Force yt-dlp for Chinese platforms: you-get support is outdated or missing
        platform = platform_for(url)
        if parser == "auto" and platform in ("bilibili", "douyin", "xhs"):
            parsers = ("yt-dlp", "you-get")
        else:
            parsers = PARSERS if parser == "auto" else (parser,)
        async with self.slots:
            error = None
            for name in parsers:
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
