"""Video adapters, durable tasks, credential isolation and process lifecycle."""

import asyncio
import json
import signal
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot.config import settings
from bot.db.repo import TaskRepo
from bot.video.accounts import CredentialVault, account_cookies, is_video_url, platform_for, validate_url
from bot.video.backends import PROGRESS_MARKER, VideoResolver, download_args, parse_info, progress_event
from bot.video.login_worker import LoginWorker
from bot.video.process import capture, signal_group, spawn, terminate
from bot.video.ui import video_task_keyboard
from bot.web.video import public_status

COOKIES = [dict(name="session", value="private-secret", domain=".bilibili.com", path="/", secure=True, expires=-1)]
YT_INFO = {
    "title": "Example",
    "formats": [{"height": 1080, "vcodec": "avc1"}, {"height": 2160, "vcodec": "vp9"}, {"vcodec": "none"}],
}
YG_INFO = {"title": "Example", "streams": {"hd": {"src": ["https://example.com/a"], "size": 100}}}


class TestVideoAdapters(unittest.IsolatedAsyncioTestCase):
    def test_metadata_and_quality_normalization(self):
        self.assertEqual(parse_info("yt-dlp", json.dumps(YT_INFO)).qualities, ["best", "2160", "1080"])
        self.assertEqual(parse_info("you-get", json.dumps(YG_INFO)).qualities, ["best", "hd"])

    def test_refuses_playlist_live_and_empty_results(self):
        for data in ({"_type": "playlist", "entries": []}, {"is_live": True}, {"title": "empty"}):
            with self.assertRaises(ValueError):
                parse_info("yt-dlp", json.dumps(data))

    async def test_fallback_occurs_only_during_probe(self):
        with patch(
            "bot.video.backends.capture", AsyncMock(side_effect=[ValueError("unsupported"), json.dumps(YG_INFO)])
        ) as call:
            result = await VideoResolver().probe("https://example.com/video")
        self.assertEqual(result.parser, "you-get")
        self.assertEqual(call.await_count, 2)

    async def test_explicit_parser_does_not_fallback(self):
        with patch("bot.video.backends.capture", AsyncMock(side_effect=ValueError("unsupported"))) as call:
            with self.assertRaises(ValueError):
                await VideoResolver().probe("https://example.com/video", parser="you-get")
        self.assertEqual(call.await_count, 1)

    def test_command_keeps_url_and_cookie_as_separate_arguments(self):
        url = "https://example.com/video?q=$(touch%20bad)"
        args = download_args("yt-dlp", url, "/tmp/video", "best", "/private/cookies")
        self.assertEqual(args[-2:], ["--", url])
        self.assertIn("/private/cookies", args)
        self.assertIn("bestvideo*+bestaudio/best", args)
        with self.assertRaises(ValueError):
            download_args("you-get", url, "/tmp/video", "--bad")

    def test_progress_and_unknown_output(self):
        self.assertEqual(progress_event(PROGRESS_MARKER + '{"downloaded":23}')["downloaded"], 23)
        for line in ("normal output", PROGRESS_MARKER + "{bad}", PROGRESS_MARKER + '{"downloaded":null}'):
            self.assertIsNone(progress_event(line))

    def test_platform_host_matching_is_not_substring_matching(self):
        self.assertEqual(platform_for("https://b23.tv/a"), "bilibili")
        self.assertTrue(is_video_url("https://www.youtube.com/watch?v=x"))
        self.assertFalse(is_video_url("https://youtube.com.attacker.example/video"))
        for url in ("file:///tmp/video", "https://user:secret@example.com/a", "not a URL"):
            with self.assertRaises(ValueError):
                validate_url(url)

    def test_video_keyboard_hides_aria2_only_actions(self):
        keys = video_task_keyboard("video_abc", "ACTIVE")
        callbacks = [b.callback_data for row in keys.inline_keyboard for b in row]
        self.assertIn("task:pause:video_abc", callbacks)
        self.assertNotIn("task:limit:video_abc", callbacks)
        self.assertNotIn("task:files:video_abc", callbacks)


class TestVideoDatabase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(str(Path(self.directory.name) / "tasks.db"))
        await self.repo.connect()
        self.vault = CredentialVault(self.directory.name)

    async def asyncTearDown(self):
        await self.repo.close()
        self.directory.cleanup()

    async def preview(self):
        return await self.repo.video_save_preview(
            user_id=1,
            chat_id=1,
            url="https://example.com/video",
            parser="yt-dlp",
            quality="best",
            account_mode="anonymous",
            info={"title": "Video", "qualities": ["best"]},
        )

    async def test_preview_claim_is_owned_and_single_use(self):
        token = await self.preview()
        self.assertIsNone(await self.repo.video_create_task(token, 2, 1))
        row = await self.repo.video_create_task(token, 1, 1)
        self.assertEqual(row["backend"], "video")
        self.assertEqual(row["node"], "default")
        self.assertIsNone(await self.repo.video_create_task(token, 1, 1))
        self.assertEqual((await self.repo.video_job(row["id"]))["parser"], "yt-dlp")

    async def test_previous_tasks_default_to_aria2(self):
        task_id = await self.repo.create_task(
            gid="old",
            user_id=1,
            chat_id=1,
            reply_message_id=None,
            source_type="url",
            source_ref="old",
            file_name="f",
            file_size=None,
        )
        self.assertEqual((await self.repo.get_by_id(task_id))["backend"], "aria2")

    async def test_terminal_tasks_still_accept_retry_commands(self):
        row = await self.repo.video_create_task(await self.preview(), 1, 1)
        await self.repo.update_status(row["gid"], "FAILED")
        self.assertEqual(await self.repo.video_jobs(), [])
        await self.repo.video_command(row["gid"], "retry")
        self.assertEqual(len(await self.repo.video_jobs()), 1)

    async def test_personal_cookies_never_fall_back_to_shared(self):
        await self.repo.video_put_account("bilibili", 0, "shared", self.vault.encrypt(COOKIES))
        await self.repo.video_share("bilibili", True)
        with self.assertRaises(ValueError):
            await account_cookies(self.repo, self.vault, 123, "https://bilibili.com/a", "personal")
        actual = await account_cookies(self.repo, self.vault, 123, "https://bilibili.com/a", "shared")
        self.assertEqual(actual, COOKIES)
        await self.repo.video_share("bilibili", False)
        with self.assertRaises(ValueError):
            await account_cookies(self.repo, self.vault, 123, "https://bilibili.com/a", "shared")

    async def test_configured_cookie_file_respects_shared_switch(self):
        with self.vault.snapshot(COOKIES) as path, patch.object(settings, "video_cookie_file_bilibili", path):
            with self.assertRaises(ValueError):
                await account_cookies(self.repo, self.vault, 123, "https://bilibili.com/a", "shared")
            await self.repo.video_share("bilibili", True)
            self.assertEqual(
                (await account_cookies(self.repo, self.vault, 123, "https://bilibili.com/a", "shared"))[0]["value"],
                "private-secret",
            )
            await self.repo.video_share("bilibili", False)
            with self.assertRaises(ValueError):
                await account_cookies(self.repo, self.vault, 123, "https://bilibili.com/a", "shared")

    async def test_encrypted_storage_and_snapshot_cleanup(self):
        encrypted = self.vault.encrypt(COOKIES)
        self.assertNotIn(b"private-secret", encrypted)
        self.assertEqual(self.vault.decrypt(encrypted), COOKIES)
        self.assertEqual((self.vault.root / "key").stat().st_mode & 0o777, 0o600)
        with self.vault.snapshot(COOKIES) as name:
            path = Path(name)
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertIn("private-secret", path.read_text())
        self.assertFalse(path.exists())

    async def test_one_time_grant_is_bound_to_one_login(self):
        login = await self.repo.video_new_login("bilibili", 1, 1)
        other = await self.repo.video_new_login("douyin", 2, 2)
        token = await self.repo.video_grant(login)
        session = await self.repo.video_exchange(token)
        self.assertIsNotNone(session)
        self.assertIsNone(await self.repo.video_exchange(token))
        self.assertEqual((await self.repo.video_web_login(session))["id"], login)
        self.assertNotEqual((await self.repo.video_web_login(session))["id"], other)
        self.assertIsNone(await self.repo.video_web_login("forged"))

    async def test_refresh_cancels_previous_login(self):
        first = await self.repo.video_new_login("xhs", 1, 1)
        second = await self.repo.video_new_login("xhs", 1, 1)
        self.assertEqual((await self.repo.video_login(first))["state"], "cancelled")
        self.assertEqual((await self.repo.video_login(second))["state"], "queued")

    async def test_cancelled_login_cannot_overwrite_credentials(self):
        login = await self.repo.video_new_login("bilibili", 1, 1)
        worker = LoginWorker(self.repo, vault=self.vault)
        with patch.object(settings, "db_path", str(Path(self.directory.name) / "tasks.db")):
            worker.vault = self.vault
            await self.repo.video_login_update(login, state="cancelled")
            await worker.success(await self.repo.video_login(login), COOKIES, "identity")
        self.assertIsNone(await self.repo.video_account("bilibili", 1))

    async def test_qr_success_filters_foreign_domains(self):
        login = await self.repo.video_new_login("bilibili", 1, 1)
        worker = LoginWorker(self.repo, vault=self.vault)
        worker.vault = self.vault
        await worker.success(
            await self.repo.video_login(login), COOKIES + [dict(COOKIES[0], domain=".example.com")], "identity"
        )
        stored = await self.repo.video_account("bilibili", 1)
        self.assertEqual(self.vault.decrypt(stored["cookies"]), COOKIES)
        self.assertEqual((await self.repo.video_login(login))["state"], "success")

    async def test_expired_status_never_returns_cookie_material(self):
        login = await self.repo.video_new_login("xhs", 1, 1)
        row = dict(await self.repo.video_login(login))
        row["expires"] = time.time() - 1
        row["qr_path"] = "/private/qr.png"
        result = public_status(row)
        self.assertEqual(result["state"], "expired")
        self.assertFalse(result["qr_ready"])
        self.assertNotIn("qr_path", result)


@unittest.skipUnless(sys.platform.startswith("linux"), "Linux process groups")
class TestVideoProcesses(unittest.IsolatedAsyncioTestCase):
    async def test_capture_is_bounded_and_redacts_failure_output(self):
        self.assertEqual((await capture([sys.executable, "-c", 'print("ok")'])).strip(), "ok")
        with self.assertRaisesRegex(ValueError, "过大"):
            await capture([sys.executable, "-c", 'print("x"*2000)'], maximum=100)
        with self.assertRaises(ValueError) as error:
            await capture([sys.executable, "-c", 'print("private-secret");raise SystemExit(1)'])
        self.assertNotIn("private-secret", str(error.exception))

    async def test_timeout_terminates_process(self):
        with self.assertRaisesRegex(ValueError, "超时"):
            await capture([sys.executable, "-c", "import time;time.sleep(30)"], timeout=0.1)

    async def test_pause_resume_and_terminate_stopped_group(self):
        process = await spawn([sys.executable, "-u", "-c", 'import time;print("ready");time.sleep(30)'])
        try:
            self.assertEqual((await process.stdout.readline()).strip(), b"ready")
            signal_group(process, signal.SIGSTOP)
            await asyncio.sleep(0.05)
            state = Path(f"/proc/{process.pid}/status").read_text()
            self.assertIn("T (stopped)", state)
            signal_group(process, signal.SIGCONT)
            signal_group(process, signal.SIGSTOP)
            await terminate(process)
            self.assertIsNotNone(process.returncode)
        finally:
            await terminate(process)
