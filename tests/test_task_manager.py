import os
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import patch

from bot.config import settings
from bot.core.task_manager import TaskManager
from bot.db.repo import TaskRepo


class FakeBot:
    """记录调用参数，不真的打 Telegram API。"""

    def __init__(self):
        self.sent_documents = []  # (chat_id, caption)
        self.sent_messages = []   # (chat_id, text)
        self.fail_send_document = False

    async def send_document(self, chat_id, document, caption=None, **kwargs):
        if self.fail_send_document:
            raise RuntimeError("boom")
        self.sent_documents.append((chat_id, caption))

    async def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append((chat_id, text))


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


class TestSendFileToTg(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.bot = FakeBot()
        self.tm = TaskManager(bot=self.bot, aria2=None, repo=self.repo)

    async def asyncTearDown(self):
        await self.repo.close()
        self._dir.cleanup()

    def _row(self, **overrides):
        row = {"chat_id": 42, "file_name": "movie.mkv", "save_path": None}
        row.update(overrides)
        return row

    async def test_sends_existing_file(self):
        path = os.path.join(self._dir.name, "movie.mkv")
        with open(path, "wb") as f:
            f.write(b"data")
        ok, msg = await self.tm.send_file_to_tg(self._row(), "g1", path)
        self.assertTrue(ok)
        self.assertEqual(self.bot.sent_documents, [(42, "movie.mkv")])

    async def test_falls_back_to_row_save_path(self):
        path = os.path.join(self._dir.name, "movie.mkv")
        with open(path, "wb") as f:
            f.write(b"data")
        ok, msg = await self.tm.send_file_to_tg(self._row(save_path=path), "g1")
        self.assertTrue(ok)

    async def test_rejects_missing_file(self):
        ok, msg = await self.tm.send_file_to_tg(self._row(), "g1", "/nonexistent/path.mkv")
        self.assertFalse(ok)
        self.assertIn("不存在", msg)
        self.assertEqual(self.bot.sent_documents, [])

    async def test_rejects_directory(self):
        ok, msg = await self.tm.send_file_to_tg(self._row(), "g1", self._dir.name)
        self.assertFalse(ok)
        self.assertIn("目录", msg)

    async def test_rejects_oversized_file(self):
        path = os.path.join(self._dir.name, "big.bin")
        with open(path, "wb") as f:
            f.write(b"data")
        with patch("bot.core.task_manager.TG_MAX_SEND_BYTES", 1):
            ok, msg = await self.tm.send_file_to_tg(self._row(), "g1", path)
        self.assertFalse(ok)
        self.assertIn("过大", msg)

    async def test_reports_failure_from_telegram(self):
        path = os.path.join(self._dir.name, "movie.mkv")
        with open(path, "wb") as f:
            f.write(b"data")
        self.bot.fail_send_document = True
        ok, msg = await self.tm.send_file_to_tg(self._row(), "g1", path)
        self.assertFalse(ok)
        self.assertIn("发送失败", msg)


class TestDiskAlert(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = FakeBot()
        self.tm = TaskManager(bot=self.bot, aria2=None, repo=None)
        self._orig_threshold = settings.disk_alert_threshold_gb
        self._orig_admin = settings.admin_user_ids
        self._orig_allowed = settings.allowed_user_ids
        settings.admin_user_ids = "111"
        settings.allowed_user_ids = ""

    async def asyncTearDown(self):
        settings.disk_alert_threshold_gb = self._orig_threshold
        settings.admin_user_ids = self._orig_admin
        settings.allowed_user_ids = self._orig_allowed

    def _usage(self, free_gb: float):
        return SimpleNamespace(total=100 * 1024**3, used=0, free=int(free_gb * 1024**3))

    async def test_disabled_when_threshold_is_zero(self):
        settings.disk_alert_threshold_gb = 0
        with patch("bot.core.task_manager.shutil.disk_usage", return_value=self._usage(0.1)):
            await self.tm._check_disk_space()
        self.assertEqual(self.bot.sent_messages, [])

    async def test_alerts_admin_when_below_threshold(self):
        settings.disk_alert_threshold_gb = 10
        with patch("bot.core.task_manager.shutil.disk_usage", return_value=self._usage(2.0)):
            await self.tm._check_disk_space()
        self.assertEqual(len(self.bot.sent_messages), 1)
        chat_id, text = self.bot.sent_messages[0]
        self.assertEqual(chat_id, 111)
        self.assertIn("磁盘空间告警", text)

    async def test_no_alert_when_above_threshold(self):
        settings.disk_alert_threshold_gb = 10
        with patch("bot.core.task_manager.shutil.disk_usage", return_value=self._usage(50.0)):
            await self.tm._check_disk_space()
        self.assertEqual(self.bot.sent_messages, [])

    async def test_cooldown_suppresses_repeat_alert(self):
        settings.disk_alert_threshold_gb = 10
        with patch("bot.core.task_manager.shutil.disk_usage", return_value=self._usage(2.0)):
            await self.tm._check_disk_space()
            await self.tm._check_disk_space()
        self.assertEqual(len(self.bot.sent_messages), 1)

    async def test_realerts_after_recovery(self):
        settings.disk_alert_threshold_gb = 10
        with patch("bot.core.task_manager.shutil.disk_usage") as mock_usage:
            mock_usage.return_value = self._usage(2.0)
            await self.tm._check_disk_space()  # 第一次告警
            mock_usage.return_value = self._usage(50.0)
            await self.tm._check_disk_space()  # 恢复，重置状态
            mock_usage.return_value = self._usage(2.0)
            await self.tm._check_disk_space()  # 再次跌破，应该重新提醒
        self.assertEqual(len(self.bot.sent_messages), 2)

    async def test_no_recipients_does_not_crash(self):
        settings.disk_alert_threshold_gb = 10
        settings.admin_user_ids = ""
        settings.allowed_user_ids = ""
        with patch("bot.core.task_manager.shutil.disk_usage", return_value=self._usage(2.0)):
            await self.tm._check_disk_space()  # 不应该抛异常
        self.assertEqual(self.bot.sent_messages, [])


if __name__ == "__main__":
    unittest.main()
