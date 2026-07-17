import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

from bot.config import settings
from bot.core.task_manager import TaskManager
from bot.db.repo import TaskRepo


class TestRunCleanupOnce(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        # run_cleanup_once 只碰 self._repo，bot/aria2 在这个方法里用不到，传 None 即可
        self.tm = TaskManager(bot=None, aria2=None, repo=self.repo)
        self._orig_days = settings.auto_cleanup_days

    async def asyncTearDown(self):
        settings.auto_cleanup_days = self._orig_days
        await self.repo.close()
        self._dir.cleanup()

    async def _old_completed_task(self, gid: str, days: int):
        await self.repo.create_task(
            gid=gid, user_id=1, chat_id=1, reply_message_id=None,
            source_type="url", source_ref=gid, file_name="f.bin",
            file_size=10, payload="https://example.com/f.bin",
        )
        await self.repo.update_status(gid, "COMPLETED")
        finished_at = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
        await self.repo._conn.execute(
            "UPDATE tasks SET finished_at = ? WHERE gid = ?", (finished_at, gid)
        )
        await self.repo._conn.commit()

    async def test_disabled_when_days_is_zero(self):
        settings.auto_cleanup_days = 0
        await self._old_completed_task("g1", days=100)
        deleted = await self.tm.run_cleanup_once()
        self.assertEqual(deleted, 0)
        self.assertIsNotNone(await self.repo.get_by_gid("g1"))

    async def test_removes_only_records_past_retention(self):
        settings.auto_cleanup_days = 7
        await self._old_completed_task("old", days=10)
        await self._old_completed_task("recent", days=1)
        deleted = await self.tm.run_cleanup_once()
        self.assertEqual(deleted, 1)
        self.assertIsNone(await self.repo.get_by_gid("old"))
        self.assertIsNotNone(await self.repo.get_by_gid("recent"))


if __name__ == "__main__":
    unittest.main()
