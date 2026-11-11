"""Configured platform cookies, sharing controls and preview handoff."""

import tempfile
import unittest
import warnings
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import settings
from bot.db.repo import TaskRepo
from bot.handlers.video import make_preview
from bot.video.accounts import CredentialVault, account_cookies, load_cookie_file, platform_for
from bot.video.backends import MediaInfo


class TestCookieFiles(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.repo = TaskRepo(str(self.root / "tasks.db"))
        await self.repo.connect()
        self.vault = CredentialVault(self.root)

    async def asyncTearDown(self):
        await self.repo.close()
        self.directory.cleanup()

    def cookie_file(self, domain="youtube.com", expires="", extra=""):
        path = self.root / "cookies.txt"
        path.write_text(
            "# Netscape HTTP Cookie File\n"
            f"#HttpOnly_.{domain}\tTRUE\t/\tTRUE\t{expires}\tsession\tprivate-secret\n" + extra
        )
        return str(path)

    def test_youtube_hosts_are_scoped(self):
        for host in ("www.youtube.com", "m.youtube.com", "youtu.be", "www.youtube-nocookie.com"):
            self.assertEqual(platform_for(f"https://{host}/video"), "youtube")
        self.assertIsNone(platform_for("https://youtube.com.attacker.example/video"))

    async def test_all_platform_files_filter_unrelated_domains(self):
        for platform, domain in (
            ("bilibili", "bilibili.com"),
            ("douyin", "douyin.com"),
            ("xhs", "xiaohongshu.com"),
            ("youtube", "youtube.com"),
        ):
            with self.subTest(platform=platform):
                path = self.cookie_file(
                    extra=f".{domain}.attacker.example\tTRUE\t/\tTRUE\t\tbad\tsecret\n", domain=domain
                )
                with patch.object(settings, "video_cookie_file_" + platform, path):
                    await self.repo.video_share(platform, True)
                    cookies = await account_cookies(self.repo, self.vault, 11, f"https://{domain}/video", "shared")
                self.assertEqual(len(cookies), 1)
                self.assertEqual(cookies[0]["value"], "private-secret")

    async def test_shared_file_requires_opening_and_explicit_mode(self):
        with patch.object(settings, "video_cookie_file_youtube", self.cookie_file()):
            with self.assertRaisesRegex(ValueError, "尚未开放"):
                await account_cookies(self.repo, self.vault, 11, "https://youtu.be/video", "shared")
            await self.repo.video_share("youtube", True)
            self.assertEqual(
                len(await account_cookies(self.repo, self.vault, 11, "https://youtu.be/video", "shared")), 1
            )
            self.assertEqual(
                await account_cookies(self.repo, self.vault, 11, "https://youtu.be/video", "anonymous"), []
            )
            with self.assertRaises(ValueError):
                await account_cookies(self.repo, self.vault, 11, "https://youtu.be/video", "personal")
            await self.repo.video_share("youtube", False)
            with self.assertRaisesRegex(ValueError, "尚未开放"):
                await account_cookies(self.repo, self.vault, 11, "https://youtu.be/video", "shared")

    async def test_file_takes_precedence_over_saved_shared_scan(self):
        await self.repo.video_put_account("bilibili", 0, "old-account", b"old-invalid-credentials")
        await self.repo.video_share("bilibili", True)
        with patch.object(settings, "video_cookie_file_bilibili", self.cookie_file("bilibili.com")):
            cookies = await account_cookies(self.repo, self.vault, 11, "https://bilibili.com/video", "shared")
        self.assertEqual(cookies[0]["value"], "private-secret")

    def test_expired_or_wrong_platform_file_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "有效 Cookie"):
            load_cookie_file(self.cookie_file(expires="1"), "youtube")
        with self.assertRaisesRegex(ValueError, "有效 Cookie"):
            load_cookie_file(self.cookie_file("bilibili.com"), "youtube")

    def test_missing_file_uses_safe_error(self):
        with self.assertRaises(ValueError) as raised:
            load_cookie_file(str(self.root / "private-file-name"), "youtube")
        self.assertNotIn("private-file-name", str(raised.exception))

    def test_malformed_cookie_is_not_exposed_in_errors_or_warnings(self):
        path = self.cookie_file(extra="private-malformed-secret\n")
        with warnings.catch_warnings(record=True) as captured:
            warnings.simplefilter("always")
            with self.assertRaises(ValueError) as raised:
                load_cookie_file(path, "youtube")
        self.assertNotIn("private-malformed-secret", str(raised.exception))
        self.assertEqual(captured, [])

    async def test_youtube_preview_hands_off_selected_file_and_cleans_snapshot(self):
        snapshots = []

        async def probe(url, **kwargs):
            path = Path(kwargs["cookies"])
            self.assertIn("private-secret", path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            snapshots.append(path)
            return MediaInfo("YouTube video", "yt-dlp", ["best", "1080"])

        resolver = SimpleNamespace(probe=AsyncMock(side_effect=probe))
        await self.repo.video_share("youtube", True)
        await self.repo.video_preference(11, "youtube", "shared")
        with (
            patch.object(settings, "video_cookie_file_youtube", self.cookie_file()),
            patch.object(settings, "video_enabled", True),
            patch.object(settings, "db_path", str(self.root / "tasks.db")),
            patch("bot.handlers.video.get_resolver", return_value=resolver),
        ):
            preview = await make_preview(self.repo, 11, 11, "https://youtu.be/video")
        self.assertEqual(preview["account_mode"], "shared")
        self.assertEqual(len(snapshots), 1)
        self.assertFalse(snapshots[0].exists())
