import unittest
from unittest.mock import AsyncMock, patch

from bot.video.backends import VideoResolver


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
        with patch("bot.video.backends.capture", AsyncMock(side_effect=ValueError("yt-dlp fail"))):
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
