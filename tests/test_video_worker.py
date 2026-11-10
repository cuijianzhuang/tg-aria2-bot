"""Exercise the worker with a real child process and mocked site metadata."""

import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import settings
from bot.core import quota
from bot.db.repo import TaskRepo
from bot.video.backends import PROGRESS_MARKER, MediaInfo
from bot.video.process import spawn as real_spawn
from bot.video.worker import VideoWorker


class TestVideoWorker(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        root = Path(self.directory.name)
        self.stack = ExitStack()
        self.stack.enter_context(patch("bot.video.worker.storage.has_enough_space", return_value=True))
        self.stack.enter_context(patch.object(settings, "db_path", str(root / "tasks.db")))
        self.stack.enter_context(patch.object(settings, "download_dir", str(root / "downloads")))
        self.stack.enter_context(patch.object(settings, "user_max_task_size_gb", 0))
        (root / "downloads").mkdir()
        self.repo = TaskRepo(settings.db_path)
        await self.repo.connect()
        token = await self.repo.video_save_preview(
            user_id=1,
            chat_id=1,
            url="https://example.com/video",
            parser="yt-dlp",
            quality="best",
            account_mode="anonymous",
            info={"title": "Video", "qualities": ["best"]},
        )
        self.row = await self.repo.video_create_task(token, 1, 1)

        async def complete(row, download):
            await self.repo.update_status(row["gid"], "COMPLETED", save_path=str(download.files[0].path))

        manager = SimpleNamespace(
            pending_usage=lambda user: 0,
            usage_today=lambda user: self.repo.get_usage(user, quota.today()),
            complete_local_video=AsyncMock(side_effect=complete),
        )
        self.worker = VideoWorker(AsyncMock(), self.repo, manager)
        self.worker.resolver.probe = AsyncMock(return_value=MediaInfo("Real title", "yt-dlp", ["best"]))

    async def asyncTearDown(self):
        self.stack.close()
        await self.repo.close()
        self.directory.cleanup()

    async def fixture_spawn(self, args):
        directory = Path(settings.download_dir) / "video" / self.row["gid"]
        output = directory / "media.mp4"
        script = (
            f'from pathlib import Path;Path({str(output)!r}).write_bytes(b"v"*100);'
            f"print({(PROGRESS_MARKER + json.dumps({'downloaded': 100, 'filename': 'stream'}))!r});"
            f"print({('__VIDEO_OUTPUT__' + str(output))!r})"
        )
        return await real_spawn([sys.executable, "-u", "-c", script])

    async def test_success_accounts_usage_and_calls_common_completion(self):
        row = dict((await self.repo.video_jobs())[0])
        with (
            patch("bot.video.worker.spawn", self.fixture_spawn),
            patch("bot.video.worker.capture", AsyncMock(return_value='{"streams":[{"codec_type":"video"}]}')),
        ):
            await self.worker._execute(row)
        latest = await self.repo.get_by_gid(self.row["gid"])
        self.assertEqual(latest["status"], "COMPLETED", latest["error"])
        self.assertEqual(latest["file_size"], 100)
        self.assertEqual(await self.repo.get_usage(1, quota.today()), 100)
        self.assertTrue(Path(latest["save_path"]).is_file())
        self.worker.task_manager.complete_local_video.assert_awaited_once()
        self.assertFalse(list(self.worker.vault.root.glob("cookies-*.txt")))

    async def test_selected_quality_is_used_when_refreshing_metadata(self):
        row = dict((await self.repo.video_jobs())[0])
        row["quality"] = "1080"
        self.worker.resolver.probe.return_value = MediaInfo("Title", "yt-dlp", ["best", "1080"])
        with (
            patch("bot.video.worker.spawn", self.fixture_spawn),
            patch("bot.video.worker.capture", AsyncMock(return_value='{"streams":[{"codec_type":"video"}]}')),
        ):
            await self.worker._execute(row)
        self.assertEqual(self.worker.resolver.probe.await_args.kwargs["quality"], "1080")

    async def test_invalid_video_never_runs_completion(self):
        row = dict((await self.repo.video_jobs())[0])
        with (
            patch("bot.video.worker.spawn", self.fixture_spawn),
            patch("bot.video.worker.capture", AsyncMock(return_value='{"streams":[]}')),
        ):
            await self.worker._execute(row)
        self.assertEqual((await self.repo.get_by_gid(self.row["gid"]))["status"], "FAILED")
        self.worker.task_manager.complete_local_video.assert_not_awaited()

    async def test_restart_recovery_leaves_existing_task_paused(self):
        import asyncio

        task = asyncio.create_task(self.worker.run())
        await asyncio.sleep(0.05)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        self.assertEqual((await self.repo.get_by_gid(self.row["gid"]))["status"], "PAUSED")
