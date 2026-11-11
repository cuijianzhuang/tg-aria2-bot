"""Downloads recover a missing directory without losing confirmation cards."""

import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import settings
from bot.core import rss, storage
from bot.core.node_pool import Node
from bot.db.repo import TaskRepo
from bot.handlers import callbacks, links, settings_menu, video
from bot.video.backends import MediaInfo
from tests.fakes import FakeNodePool


class TestStorageChecks(unittest.TestCase):
    def test_missing_nested_directory_is_created_before_disk_check(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "missing" / "downloads"
            self.assertFalse(path.exists())
            # Use a real statvfs: this raised FileNotFoundError before the fix.
            with patch.object(storage, "MIN_FREE_BYTES", 0):
                self.assertTrue(storage.has_enough_space(str(path), 0))
            self.assertTrue(path.is_dir())

    def test_existing_files_are_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            existing = Path(directory) / "keep.txt"
            existing.write_text("keep")
            storage.download_space_error(directory, 0)
            self.assertEqual(existing.read_text(), "keep")

    def test_uncreatable_directory_is_distinct_from_low_space(self):
        with patch("bot.core.storage.os.makedirs", side_effect=PermissionError("private-path")):
            message = storage.download_space_error("/private", 0)
        self.assertIn("下载目录不可用", message)
        self.assertNotIn("private-path", message)
        with (
            tempfile.TemporaryDirectory() as directory,
            patch("bot.core.storage.shutil.disk_usage", return_value=SimpleNamespace(free=0)),
        ):
            self.assertIn("磁盘空间不足", storage.download_space_error(directory, 0))

    def test_rss_does_not_admit_an_unusable_directory(self):
        with patch("bot.core.storage.os.makedirs", side_effect=PermissionError()):
            self.assertFalse(rss._has_space("/unusable"))


class TestDownloadDirectoryRecovery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.root = Path(self.directory.name)
        self.downloads = self.root / "missing" / "downloads"
        self.stack = ExitStack()
        for name, value in (
            ("db_path", str(self.root / "tasks.db")),
            ("download_dir", str(self.downloads)),
            ("download_dir_presets", ""),
            ("video_enabled", True),
            ("admin_user_ids", "11"),
            ("allowed_user_ids", ""),
        ):
            self.stack.enter_context(patch.object(settings, name, value))
        self.stack.enter_context(patch.object(storage, "MIN_FREE_BYTES", 0))
        self.repo = TaskRepo(settings.db_path)
        await self.repo.connect()
        self.nodes = FakeNodePool(download_dir=str(self.downloads))

    async def asyncTearDown(self):
        await self.repo.close()
        self.stack.close()
        self.directory.cleanup()

    def query(self, data):
        return SimpleNamespace(
            data=data,
            from_user=SimpleNamespace(id=11),
            answer=AsyncMock(),
            message=SimpleNamespace(
                message_id=50,
                chat=SimpleNamespace(id=11, type="private"),
                text="confirmation",
                reply_markup=None,
                edit_text=AsyncMock(),
                answer=AsyncMock(),
            ),
        )

    def message(self, url):
        return SimpleNamespace(
            text=url,
            from_user=SimpleNamespace(id=11),
            chat=SimpleNamespace(id=11),
            reply=AsyncMock(return_value=SimpleNamespace(edit_text=AsyncMock())),
        )

    async def pending(self, batch=None, node="default"):
        return await self.repo.create_pending(
            kind="url",
            user_id=11,
            chat_id=11,
            source_ref="file",
            file_name="file.zip",
            file_size=None,
            payload="https://example.org/file.zip",
            batch_id=batch,
            node=node,
        )

    async def test_ordinary_url_confirmation_and_start_recreate_missing_directory(self):
        message = self.message("https://example.org/file.zip")
        await links.handle_url(message, self.repo, self.nodes)
        markup = message.reply.call_args.kwargs["reply_markup"]
        start = next(
            b.callback_data
            for row in markup.inline_keyboard
            for b in row
            if b.callback_data.startswith("pending:start:")
        )
        self.assertFalse(self.downloads.exists())
        await callbacks.handle_pending(self.query(start), self.repo, self.nodes)
        self.assertTrue(self.downloads.is_dir())
        self.assertIsNotNone(await self.repo.get_by_gid("gid-uri"))

    async def test_video_auto_preview_and_start_recreate_missing_directory(self):
        message = self.message("https://www.youtube.com/watch?v=example")
        self.assertTrue(video.automatic(message))
        resolver = SimpleNamespace(probe=AsyncMock(return_value=MediaInfo("Example", "yt-dlp", ["best"])))
        with patch("bot.handlers.video.get_resolver", return_value=resolver):
            await video.video_message(message, self.repo)
        markup = message.reply.return_value.edit_text.call_args.kwargs["reply_markup"]
        start = next(
            b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data.startswith("vp:start:")
        )
        self.assertFalse(self.downloads.exists())
        manager = SimpleNamespace(pending_usage=lambda user_id: 0)
        await video.video_preview_action(self.query(start), self.repo, manager)
        self.assertTrue(self.downloads.is_dir())
        self.assertEqual(len(await self.repo.video_jobs()), 1)

    async def test_unusable_ordinary_directory_retains_confirmation_for_retry(self):
        token = await self.pending()
        self.downloads.parent.mkdir(parents=True)
        self.downloads.write_text("a file, not a directory")
        query = self.query("pending:start:" + token)
        await callbacks.handle_pending(query, self.repo, self.nodes)
        self.assertIn("下载目录不可用", query.answer.call_args.args[0])
        self.assertIsNotNone(await self.repo.get_pending(token))
        self.assertEqual(self.nodes.get("default").add_uri_calls, [])
        self.downloads.unlink()
        await callbacks.handle_pending(query, self.repo, self.nodes)
        self.assertIsNotNone(await self.repo.get_by_gid("gid-uri"))

    async def test_unusable_video_directory_retains_preview(self):
        token = await self.repo.video_save_preview(
            user_id=11,
            chat_id=11,
            url="https://youtu.be/example",
            parser="yt-dlp",
            quality="best",
            account_mode="anonymous",
            info={"title": "Example", "qualities": ["best"]},
        )
        query = self.query("vp:start:" + token)
        with patch("bot.core.storage.os.makedirs", side_effect=PermissionError()):
            await video.video_preview_action(query, self.repo, SimpleNamespace(pending_usage=lambda user_id: 0))
        self.assertIn("下载目录不可用", query.message.answer.call_args.args[0])
        self.assertIsNotNone(await self.repo.video_preview(token, 11))

    async def test_batch_failure_retains_pending_and_retry_button(self):
        token = await self.pending(batch="batch")
        query = self.query("pending:startall:batch")
        with patch("bot.core.storage.os.makedirs", side_effect=PermissionError()):
            await callbacks.handle_pending(query, self.repo, self.nodes)
        self.assertIsNotNone(await self.repo.get_pending(token))
        markup = query.message.edit_text.call_args.kwargs["reply_markup"]
        self.assertIn("pending:startall:batch", [b.callback_data for row in markup.inline_keyboard for b in row])

    async def test_remote_download_does_not_create_local_remote_directory(self):
        remote = str(self.root / "remote-only")
        self.nodes = FakeNodePool(
            extra_nodes=[Node("remote", "https://example.org/rpc", "secret", remote, False)],
            download_dir=str(self.downloads),
        )
        token = await self.pending(node="remote")
        with patch("bot.core.storage.has_enough_space", side_effect=AssertionError("remote disk checked locally")):
            await callbacks.handle_pending(self.query("pending:start:" + token), self.repo, self.nodes)
        self.assertFalse(Path(remote).exists())
        self.assertIsNotNone(await self.repo.get_by_gid("gid-uri"))

    async def test_invalid_directory_selection_preserves_original_setting(self):
        original = settings.download_dir
        with (
            patch.object(settings, "download_dir_presets", str(self.root / "new")),
            patch("bot.handlers.settings_menu.os.makedirs", side_effect=PermissionError()),
            patch("bot.handlers.settings_menu._persist_env") as persist,
        ):
            query = self.query("setdir:1")
            await settings_menu.apply_dir(query, None)
        self.assertEqual(settings.download_dir, original)
        persist.assert_not_called()
        self.assertTrue(query.answer.call_args.kwargs["show_alert"])
