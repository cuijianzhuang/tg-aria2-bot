"""Personal Cookie imports: reply scope, bounded uploads and account isolation."""

import asyncio
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.dispatcher.event.bases import SkipHandler

from bot.config import settings
from bot.db.repo import TaskRepo
from bot.handlers.video_cookies import PROMPT_PREFIX, cancel_import, import_document, start_import
from bot.video.account_views import platform_view
from bot.video.accounts import MAX_COOKIE_BYTES, CredentialVault, account_cookies, parse_cookie_upload
from bot.video.ui import preview_keyboard


def contents(domain="youtube.com", value="personal-secret", expires="0"):
    return (f"# Netscape HTTP Cookie File\n#HttpOnly_.{domain}\tTRUE\t/\tTRUE\t{expires}\tsession\t{value}\n").encode()


class TestCookieImports(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.repo = TaskRepo(str(self.root / "tasks.db"))
        await self.repo.connect()
        self.setting = patch.object(settings, "db_path", str(self.root / "tasks.db"))
        self.setting.start()
        self.vault = CredentialVault(self.root)
        await self.repo.video_cookie_import_start(11, "youtube", 11, 88)

    async def asyncTearDown(self):
        self.setting.stop()
        await self.repo.close()
        self.directory.cleanup()

    def message(self, data=None, user=11, prompt=88):
        data = contents() if data is None else data

        async def download(document, *, destination, timeout):
            destination.write(data)

        return SimpleNamespace(
            chat=SimpleNamespace(id=11, type="private"),
            from_user=SimpleNamespace(id=user),
            reply_to_message=SimpleNamespace(
                message_id=prompt, from_user=SimpleNamespace(id=123), text=PROMPT_PREFIX + "YouTube"
            ),
            document=SimpleNamespace(file_size=len(data)),
            bot=SimpleNamespace(id=123, download=AsyncMock(side_effect=download)),
            answer=AsyncMock(),
            delete=AsyncMock(),
        )

    async def test_success_is_encrypted_personal_and_default_without_shared_changes(self):
        await self.repo.video_put_account(
            "youtube", 0, "shared", self.vault.encrypt(parse_cookie_upload(contents(value="shared-secret"), "youtube"))
        )
        await self.repo.video_share("youtube", True)
        message = self.message()
        await import_document(message, self.repo)
        row = await self.repo.video_account("youtube", 11)
        self.assertNotIn(b"personal-secret", row["cookies"])
        self.assertEqual(self.vault.decrypt(row["cookies"])[0]["value"], "personal-secret")
        self.assertEqual(await self.repo.video_preference(11, "youtube"), "personal")
        self.assertEqual(
            (await account_cookies(self.repo, self.vault, 11, "https://youtu.be/x", "personal"))[0]["value"],
            "personal-secret",
        )
        self.assertEqual(
            (await account_cookies(self.repo, self.vault, 11, "https://youtu.be/x", "shared"))[0]["value"],
            "shared-secret",
        )
        with self.assertRaises(ValueError):
            await account_cookies(self.repo, self.vault, 22, "https://youtu.be/x", "personal")
        self.assertTrue((await self.repo.video_account("youtube", 0))["enabled"])
        self.assertNotIn("personal-secret", str(message.answer.call_args_list))
        self.assertEqual(sorted(p.name for p in self.vault.root.iterdir()), ["key"])
        message.delete.assert_awaited_once()
        tasks = await (await self.repo._conn.execute("SELECT COUNT(*) FROM tasks")).fetchone()
        self.assertEqual(tasks[0], 0)

    async def test_each_platform_has_personal_import_controls(self):
        for platform in ("bilibili", "douyin", "xhs", "youtube"):
            _, keyboard = await platform_view(self.repo, 11, platform, False)
            self.assertIn(f"vacc:import:{platform}", [b.callback_data for row in keyboard.inline_keyboard for b in row])

    async def test_prompt_is_private_reply_scoped_and_survives_restart(self):
        query = SimpleNamespace(from_user=SimpleNamespace(id=11), message=self.message())
        query.message.answer.return_value = SimpleNamespace(message_id=99)
        await start_import(query, self.repo, "douyin")
        self.assertTrue(query.message.answer.call_args.kwargs["reply_markup"].force_reply)
        await self.repo.close()
        self.repo = TaskRepo(str(self.root / "tasks.db"))
        await self.repo.connect()
        self.assertEqual((await self.repo.video_cookie_import(11, 99))["platform"], "douyin")
        self.assertEqual((await self.repo.video_cookie_import(11, 88))["state"], "cancelled")

    async def test_invalid_file_preserves_existing_account_and_can_be_retried(self):
        original = self.vault.encrypt(parse_cookie_upload(contents(value="old-secret"), "youtube"))
        await self.repo.video_put_account("youtube", 11, "old-account", original)
        bad = self.message(b"# Netscape HTTP Cookie File\nmalformed-secret\n")
        await import_document(bad, self.repo)
        self.assertEqual((await self.repo.video_account("youtube", 11))["cookies"], original)
        self.assertNotIn("malformed-secret", str(bad.answer.call_args_list))
        self.assertEqual((await self.repo.video_cookie_import(11, 88))["state"], "pending")
        await import_document(self.message(), self.repo)
        self.assertNotEqual((await self.repo.video_account("youtube", 11))["cookies"], original)

    async def test_wrong_platform_cookie_is_rejected(self):
        await import_document(self.message(contents("bilibili.com")), self.repo)
        self.assertIsNone(await self.repo.video_account("youtube", 11))

    async def test_oversize_declared_file_is_not_downloaded(self):
        message = self.message()
        message.document.file_size = MAX_COOKIE_BYTES + 1
        await import_document(message, self.repo)
        message.bot.download.assert_not_awaited()
        self.assertIsNone(await self.repo.video_account("youtube", 11))

    async def test_actual_stream_size_is_bounded_when_size_is_unknown(self):
        message = self.message(b"x" * (MAX_COOKIE_BYTES + 1))
        message.document.file_size = None
        await import_document(message, self.repo)
        self.assertIn("1 MiB", message.answer.call_args.args[0])
        self.assertIsNone(await self.repo.video_account("youtube", 11))

    async def test_other_user_and_groups_cannot_import(self):
        for message in (self.message(user=22), self.message()):
            if message.from_user.id == 11:
                message.chat.type = "group"
            await import_document(message, self.repo)
            message.bot.download.assert_not_awaited()
        self.assertIsNone(await self.repo.video_account("youtube", 11))

    async def test_expired_cancelled_and_consumed_replies_do_not_fall_through(self):
        await self.repo._conn.execute("UPDATE video_cookie_imports SET expires=?", (time.time() - 1,))
        await self.repo._conn.commit()
        expired = self.message()
        await import_document(expired, self.repo)
        expired.bot.download.assert_not_awaited()
        await self.repo.video_cookie_import_start(11, "youtube", 11, 99)
        await cancel_import(self.message(), self.repo)
        cancelled = self.message(prompt=99)
        await import_document(cancelled, self.repo)
        cancelled.bot.download.assert_not_awaited()
        await self.repo.video_cookie_import_start(11, "youtube", 11, 100)
        await import_document(self.message(prompt=100), self.repo)
        consumed = self.message(prompt=100)
        await import_document(consumed, self.repo)
        consumed.bot.download.assert_not_awaited()

    async def test_unrecorded_cookie_prompt_is_rejected_and_ordinary_reply_passes(self):
        message = self.message(prompt=999)
        await import_document(message, self.repo)
        message.bot.download.assert_not_awaited()
        message.delete.assert_awaited_once()
        ordinary = self.message(prompt=999)
        ordinary.reply_to_message.text = "An ordinary message"
        with self.assertRaises(SkipHandler):
            await import_document(ordinary, self.repo)

    async def test_cancel_during_upload_prevents_save(self):
        message = self.message()

        async def download(document, *, destination, timeout):
            destination.write(contents())
            await self.repo.video_cookie_import_cancel(11)

        message.bot.download.side_effect = download
        await import_document(message, self.repo)
        self.assertIsNone(await self.repo.video_account("youtube", 11))

    async def test_remove_during_upload_cannot_restore_account(self):
        message = self.message()
        await self.repo.video_put_account("youtube", 11, "old-account", b"old-encrypted")

        async def download(document, *, destination, timeout):
            destination.write(contents())
            await self.repo.video_remove_account("youtube", 11)

        message.bot.download.side_effect = download
        await import_document(message, self.repo)
        self.assertIsNone(await self.repo.video_account("youtube", 11))

    async def test_remove_during_commit_wins_after_import_finishes(self):
        claimed, release, removing = asyncio.Event(), asyncio.Event(), asyncio.Event()
        execute = self.repo._conn.execute

        async def paused(sql, parameters=()):
            result = await execute(sql, parameters)
            if sql.startswith("UPDATE video_cookie_imports SET state='used'"):
                claimed.set()
                await release.wait()
            return result

        async def remove():
            removing.set()
            await self.repo.video_remove_account("youtube", 11)

        with patch.object(self.repo._conn, "execute", side_effect=paused):
            importing = asyncio.create_task(self.repo.video_cookie_import_complete(11, 11, 88, b"encrypted"))
            await asyncio.wait_for(claimed.wait(), 2)
            deleting = asyncio.create_task(remove())
            await removing.wait()
            await asyncio.sleep(0)
            release.set()
            await asyncio.wait_for(asyncio.gather(importing, deleting), 2)
        self.assertIsNone(await self.repo.video_account("youtube", 11))

    async def test_non_document_reply_is_not_a_download_task(self):
        message = self.message()
        message.document = None
        await import_document(message, self.repo)
        message.bot.download.assert_not_awaited()
        self.assertIn("文件附件", message.answer.call_args.args[0])
        self.assertIsNone(await self.repo.video_account("youtube", 11))

    async def test_import_and_new_scan_supersede_older_attempts(self):
        scan = await self.repo.video_new_login("douyin", 11, 11)
        await self.repo.video_cookie_import_start(11, "douyin", 11, 99)
        await import_document(self.message(contents("douyin.com"), prompt=99), self.repo)
        self.assertEqual((await self.repo.video_login(scan))["state"], "cancelled")
        self.assertFalse(await self.repo.video_complete_login(scan, "old", b"old-encrypted"))
        await self.repo.video_cookie_import_start(11, "douyin", 11, 100)
        await self.repo.video_new_login("douyin", 11, 11)
        self.assertFalse(await self.repo.video_cookie_import_complete(11, 11, 100, b"late-encrypted"))

    async def test_duplicate_complete_does_not_replace_first_account(self):
        self.assertTrue(await self.repo.video_cookie_import_complete(11, 11, 88, b"first-encrypted"))
        self.assertFalse(await self.repo.video_cookie_import_complete(11, 11, 88, b"second-encrypted"))
        self.assertEqual((await self.repo.video_account("youtube", 11))["cookies"], b"first-encrypted")

    async def test_download_error_is_safe_and_chat_delete_failure_is_tolerated(self):
        message = self.message()
        message.bot.download.side_effect = RuntimeError("proxy://private-secret")
        message.delete.side_effect = RuntimeError("no permission")
        await import_document(message, self.repo)
        self.assertNotIn("private-secret", str(message.answer.call_args_list))
        self.assertIsNone(await self.repo.video_account("youtube", 11))

    def test_uploaded_bom_httponly_and_session_zero_are_supported(self):
        cookie = parse_cookie_upload(b"\xef\xbb\xbf" + contents(), "youtube")[0]
        self.assertEqual(cookie["expires"], -1)
        self.assertEqual(cookie["domain"], ".youtube.com")
        self.assertTrue(cookie["secure"])

    def test_youtube_confirmation_offers_personal_identity(self):
        keyboard = preview_keyboard(
            {"url": "https://youtu.be/x", "token": "token", "quality": "best", "info": '{"qualities":["best"]}'}
        )
        self.assertIn("vp:a:token:personal", [b.callback_data for row in keyboard.inline_keyboard for b in row])
