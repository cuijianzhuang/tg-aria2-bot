"""Validated downloads survive completion-hook errors in private and group chats."""

import asyncio
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import AsyncMock, patch

from bot.config import settings
from bot.core.task_manager import TaskManager
from bot.db.repo import TaskRepo
from bot.video.backends import PROGRESS_MARKER, MediaInfo
from bot.video.process import spawn as real_spawn
from bot.video.worker import VideoWorker


class TestVideoCompletion(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.stack = ExitStack()
        for name, value in (
            ("db_path", str(Path(self.directory.name) / "tasks.db")),
            ("download_dir", str(Path(self.directory.name) / "downloads")),
            ("gofile_enabled", False),
            ("auto_send_to_tg", False),
            ("notify_on_complete", True),
            ("user_max_task_size_gb", 0),
            ("user_daily_quota_gb", 0),
            ("user_max_active_tasks", 0),
        ):
            self.stack.enter_context(patch.object(settings, name, value))
        self.stack.enter_context(patch("bot.video.worker.storage.has_enough_space", return_value=True))
        self.repo = TaskRepo(settings.db_path)
        await self.repo.connect()
        self.bot = AsyncMock()
        self.manager = TaskManager(self.bot, None, self.repo)
        self.worker = VideoWorker(self.bot, self.repo, self.manager)
        self.worker.resolver.probe = AsyncMock(return_value=MediaInfo("GIF video", "yt-dlp", ["best"]))

    async def asyncTearDown(self):
        await self.repo.close()
        self.stack.close()
        self.directory.cleanup()

    async def new_task(self, chat_id=11):
        token = await self.repo.video_save_preview(
            user_id=11,
            chat_id=chat_id,
            url="https://example.org/video",
            parser="yt-dlp",
            quality="best",
            account_mode="anonymous",
            info={"title": "GIF video", "qualities": ["best"]},
        )
        task = await self.repo.video_create_task(token, 11, 50)
        return dict(next(row for row in await self.repo.video_jobs() if row["id"] == task["id"]))

    def artifact(self, row):
        return Path(settings.download_dir) / "video" / row["gid"] / "media.mp4"

    async def execute(self, row, *, returncode=0, verification='{"streams":[{"codec_type":"video"}]}'):
        async def fixture(args):
            path = self.artifact(row)
            script = (
                f'from pathlib import Path;Path({str(path)!r}).write_bytes(b"v"*100);'
                f"print({(PROGRESS_MARKER + json.dumps({'downloaded': 100, 'filename': 'stream'}))!r});"
                f"print({('__VIDEO_OUTPUT__' + str(path))!r});raise SystemExit({returncode})"
            )
            return await real_spawn([sys.executable, "-u", "-c", script])

        capture = (
            AsyncMock(side_effect=verification)
            if isinstance(verification, Exception)
            else AsyncMock(return_value=verification)
        )
        with patch("bot.video.worker.spawn", fixture), patch("bot.video.worker.capture", capture):
            await self.worker._execute(row)
        return await self.repo.get_by_gid(row["gid"])

    async def test_failed_notifications_keep_private_and_group_downloads_completed(self):
        self.bot.edit_message_text.side_effect = RuntimeError("private-message-secret")
        self.bot.send_message.side_effect = RuntimeError("private-message-secret")
        for chat_id in (11, -10012345):
            with self.subTest(chat_id=chat_id):
                row = await self.new_task(chat_id)
                with self.assertLogs("bot.core.task_manager", level="WARNING") as logs:
                    result = await self.execute(row)
                self.assertEqual(result["status"], "COMPLETED")
                self.assertIsNone(result["error"])
                self.assertEqual(result["save_path"], str(self.artifact(row)))
                self.assertTrue(self.artifact(row).exists())
                job = await self.repo.video_job(row["id"])
                self.assertEqual(job["phase"], "已完成")
                self.assertEqual((job["completed"], job["total"]), (100, 100))
                self.assertNotIn("private-message-secret", str(logs.output))

    async def test_fallback_notification_retains_html_mode(self):
        self.bot.edit_message_text.side_effect = RuntimeError("message cannot be edited")
        result = await self.execute(await self.new_task())
        self.assertEqual(result["status"], "COMPLETED")
        self.assertEqual(self.bot.send_message.call_args.kwargs["parse_mode"], "HTML")

    async def test_post_commit_render_exception_does_not_reverse_completion(self):
        row = await self.new_task()
        with patch.object(self.manager, "_render_card", side_effect=RuntimeError("private-render-secret")):
            with self.assertLogs("bot.video.worker", level="WARNING") as logs:
                result = await self.execute(row)
        self.assertEqual(result["status"], "COMPLETED")
        self.assertIsNone(result["error"])
        self.assertNotIn("private-render-secret", str(logs.output))
        self.assertIn("完成处理", str(logs.output))

    async def test_missing_ffprobe_reports_verification_failure(self):
        row = await self.new_task()
        result = await self.execute(row, verification=FileNotFoundError("private-executable-path"))
        self.assertEqual(result["status"], "FAILED")
        self.assertIn("ffprobe", result["error"])
        self.assertNotIn("private-executable-path", result["error"])
        self.assertEqual((await self.repo.video_job(row["id"]))["phase"], "校验失败")

    async def test_actual_downloader_failure_is_not_mistaken_for_completion(self):
        row = await self.new_task()
        result = await self.execute(row, returncode=1)
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual((await self.repo.video_job(row["id"]))["phase"], "下载失败")

    async def test_invalid_video_stream_is_not_marked_completed(self):
        row = await self.new_task()
        result = await self.execute(row, verification='{"streams":[]}')
        self.assertEqual(result["status"], "FAILED")
        self.assertEqual((await self.repo.video_job(row["id"]))["phase"], "校验失败")

    async def test_retry_restores_existing_file_without_downloading_or_resending(self):
        previous = await self.new_task()
        await self.execute(previous)
        self.worker.resolver.probe.reset_mock()
        row = await self.new_task(-10012345)
        path = self.artifact(row)
        path.parent.mkdir(parents=True)
        path.write_bytes(b"already-downloaded")
        await self.repo.video_update(row["id"], work_dir=str(path.parent), output=str(path), phase="已完成")
        await self.repo.update_status(row["gid"], "FAILED", error="old completion failure")
        await self.repo.video_command(row["gid"], "retry")
        with (
            patch("bot.video.worker.capture", AsyncMock(return_value='{"streams":[{"codec_type":"video"}]}')),
            patch("bot.video.worker.spawn", AsyncMock()) as spawn,
            patch.object(self.manager, "complete_local_video", AsyncMock()) as completion,
        ):
            await self.worker._tick((await self.repo.video_jobs())[0])
        latest = await self.repo.get_by_gid(row["gid"])
        self.assertEqual(latest["status"], "COMPLETED")
        self.assertIsNone(latest["error"])
        self.assertEqual(path.read_bytes(), b"already-downloaded")
        self.worker.resolver.probe.assert_not_awaited()
        spawn.assert_not_awaited()
        completion.assert_not_awaited()

    async def test_shutdown_during_postprocessing_keeps_completed_file(self):
        row = await self.new_task()

        async def stopping(row, download):
            self.worker.stopping = True
            raise asyncio.CancelledError()

        with patch.object(self.manager, "complete_local_video", stopping):
            with self.assertRaises(asyncio.CancelledError):
                await self.execute(row)
        self.assertEqual((await self.repo.get_by_gid(row["gid"]))["status"], "COMPLETED")

    async def test_recovery_rejects_outside_paths_symlinks_and_invalid_files(self):
        row = await self.new_task()
        path = self.artifact(row)
        path.parent.mkdir(parents=True)
        outside = Path(self.directory.name) / "outside.mp4"
        outside.write_bytes(b"outside")
        row.update(work_dir=str(path.parent), output=str(outside))
        self.assertFalse(await self.worker.restore_completed_file(row))
        path.symlink_to(outside)
        row["output"] = str(path)
        self.assertFalse(await self.worker.restore_completed_file(row))
        path.unlink()
        path.write_bytes(b"invalid")
        with patch("bot.video.worker.capture", AsyncMock(return_value='{"streams":[]}')):
            self.assertFalse(await self.worker.restore_completed_file(row))


class TestCompletedSourceMigration(unittest.IsolatedAsyncioTestCase):
    async def test_old_unique_index_is_removed_and_history_can_finish_same_source_again(self):
        with tempfile.TemporaryDirectory() as directory:
            path = str(Path(directory) / "tasks.db")
            repo = TaskRepo(path)
            await repo.connect()
            try:
                await repo._conn.execute("DROP INDEX idx_dedup_source")
                await repo._conn.execute(
                    "CREATE UNIQUE INDEX idx_dedup_source ON tasks(source_type,source_ref) WHERE status='COMPLETED'"
                )
                await repo._conn.commit()
            finally:
                await repo.close()
            for _ in range(2):  # Repeated connect/migration must remain safe.
                repo = TaskRepo(path)
                await repo.connect()
                await repo.close()
            repo = TaskRepo(path)
            await repo.connect()
            try:
                for gid in ("first", "second"):
                    await repo.create_task(
                        gid=gid,
                        user_id=11,
                        chat_id=11,
                        reply_message_id=None,
                        source_type="url",
                        source_ref="same-source",
                        file_name="file.mp4",
                        file_size=100,
                    )
                    await repo.update_status(gid, "COMPLETED")
                self.assertEqual((await repo.get_by_gid("first"))["status"], "COMPLETED")
                self.assertEqual((await repo.get_by_gid("second"))["status"], "COMPLETED")
                self.assertEqual((await repo.get_completed_by_source("url", "same-source"))["gid"], "second")
                # An older release's idempotent schema must still open this DB.
                await repo._conn.execute(
                    "CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup_source ON tasks(source_type,source_ref) WHERE status='COMPLETED'"
                )
                indexes = await (await repo._conn.execute("PRAGMA index_list(tasks)")).fetchall()
                self.assertFalse(next(row["unique"] for row in indexes if row["name"] == "idx_dedup_source"))
            finally:
                await repo.close()
