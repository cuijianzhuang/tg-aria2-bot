import unittest
from unittest.mock import AsyncMock, patch

from bot.video.backends import VideoResolver
from bot.video.parsehub_backend import ParseHubBackend


class TestChinesePlatformsPriority(unittest.IsolatedAsyncioTestCase):
    async def test_bilibili_prefers_ytdlp(self):
        with patch("bot.video.backends.capture", AsyncMock(side_effect=ValueError("yt-dlp fail"))):
            resolver = VideoResolver()
            with self.assertRaisesRegex(ValueError, "yt-dlp fail"):
                await resolver.probe("https://www.bilibili.com/video/BV1xx411c7XD")

    async def test_douyin_prefers_ytdlp(self):
        with patch("bot.video.backends.capture", AsyncMock(side_effect=ValueError("yt-dlp fail"))):
            resolver = VideoResolver()
            with self.assertRaisesRegex(ValueError, "yt-dlp fail"):
                await resolver.probe("https://www.douyin.com/video/7123456789012345678")

    async def test_xhs_prefers_ytdlp(self):
        with patch("bot.video.backends.capture", AsyncMock(side_effect=ValueError("yt-dlp fail"))):
            resolver = VideoResolver()
            with self.assertRaisesRegex(ValueError, "yt-dlp fail"):
                await resolver.probe("https://www.xiaohongshu.com/explore/123456")

    async def test_youtube_uses_default_order(self):
        # YouTube is CLI-covered, so ParseHub is only the last-resort
        # fallback. Pin it unavailable (same mock as test_parsehub) so the
        # case does not depend on the parsehub package being absent: on
        # Python >= 3.12 environments it is installed and would parse
        # YouTube itself instead of surfacing the yt-dlp failure.
        with (
            patch("bot.video.backends.capture", AsyncMock(side_effect=ValueError("yt-dlp fail"))),
            patch.object(
                ParseHubBackend,
                "parse",
                AsyncMock(
                    side_effect=ValueError("ParseHub 后端未安装（视频组件缺少 parsehub 包），已自动禁用")
                ),
            ),
        ):
            resolver = VideoResolver()
            with self.assertRaisesRegex(ValueError, "yt-dlp fail"):
                await resolver.probe("https://www.youtube.com/watch?v=dQw4w9WgXcQ")

    async def test_explicit_parser_bypasses_priority(self):
        with patch("bot.video.backends.capture", AsyncMock(side_effect=ValueError("you-get fail"))):
            resolver = VideoResolver()
            with self.assertRaisesRegex(ValueError, "you-get fail"):
                await resolver.probe("https://www.bilibili.com/video/BV1xx411c7XD", parser="you-get")


if __name__ == "__main__":
    unittest.main()
