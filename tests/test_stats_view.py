import os
import re
import tempfile
import unittest

from bot.core.stats_view import DEFAULT_PERIOD, _period_since, render_stats_view
from bot.db.repo import TaskRepo


class TestPeriodSince(unittest.TestCase):
    def test_zero_means_unbounded(self):
        self.assertIsNone(_period_since("0"))

    def test_positive_days_returns_sqlite_timestamp_format(self):
        since = _period_since("7")
        # 必须是 'YYYY-MM-DD HH:MM:SS'（跟 SQLite CURRENT_TIMESTAMP 一致），
        # 不能是带 T 和时区后缀的 ISO8601，否则字符串比较会失真
        self.assertRegex(since, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}$")


class TestRenderStatsView(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()

    async def asyncTearDown(self):
        await self.repo.close()
        self._dir.cleanup()

    async def test_default_period_is_seven_days(self):
        self.assertEqual(DEFAULT_PERIOD, "7")

    async def test_renders_text_and_keyboard_for_empty_db(self):
        text, markup = await render_stats_view(self.repo, "7")
        self.assertIn("7 天", text)
        self.assertIn("新增任务：0", text)
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("stats:7", callbacks)

    async def test_all_time_label(self):
        text, markup = await render_stats_view(self.repo, "0")
        self.assertIn("全部", text)


if __name__ == "__main__":
    unittest.main()
