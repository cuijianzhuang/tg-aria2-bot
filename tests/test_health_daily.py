"""反复重启告警、按天下载统计、新增的存储分类。"""
import os
import tempfile
import unittest
from datetime import date, timedelta

from bot.config import settings
from bot.core import health, storage
from bot.core.stats_view import render_daily_view
from bot.db.repo import TaskRepo


class TestRestartAlert(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self._orig = (settings.db_path, settings.restart_alert_enabled)
        settings.db_path = os.path.join(self._dir.name, "tasks.db")
        settings.restart_alert_enabled = True
        self.addCleanup(lambda: (setattr(settings, "db_path", self._orig[0]),
                                 setattr(settings, "restart_alert_enabled", self._orig[1])))

    def test_normal_restarts_do_not_alert(self):
        self.assertFalse(health.record_start(1000))
        self.assertFalse(health.record_start(1000 + 3600))      # 隔得远
        self.assertFalse(health.record_start(1000 + 7200))

    def test_three_starts_in_ten_minutes_alert(self):
        health.record_start(1000)
        self.assertFalse(health.record_start(1100))
        self.assertTrue(health.record_start(1200))

    def test_alert_only_once_per_crash_loop(self):
        for t in (1000, 1050):
            health.record_start(t)
        self.assertTrue(health.record_start(1100))
        self.assertFalse(health.record_start(1150))
        self.assertFalse(health.record_start(1200))
        later = 1100 + health.ALERT_COOLDOWN_SECONDS + 10
        health.record_start(later - 20)
        health.record_start(later - 10)
        self.assertTrue(health.record_start(later))           # 冷却过后还在崩，再提醒

    def test_disabled(self):
        settings.restart_alert_enabled = False
        for t in (1000, 1050, 1100):
            self.assertFalse(health.record_start(t))

    def test_corrupt_state_file_is_tolerated(self):
        with open(health._path(), "w") as f:
            f.write("{not json")
        self.assertFalse(health.record_start(1000))


class TestDaily(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.addAsyncCleanup(self.repo.close)
        t = date.today()
        self.today, self.yesterday = t.isoformat(), (t - timedelta(days=1)).isoformat()
        await self.repo.add_usage(5, self.today, 3 * 1024**3)
        await self.repo.add_usage(6, self.today, 1024**3)
        await self.repo.add_usage(5, self.yesterday, 2 * 1024**3)
        await self.repo.add_usage(5, (t - timedelta(days=40)).isoformat(), 99 * 1024**3)   # 窗口外

    async def test_admin_sees_totals_and_top_users(self):
        text, markup = await render_daily_view(self.repo, user_id=None)
        self.assertIn(self.today[5:], text)
        self.assertIn("4.0 GiB", text)                          # 今天两个人合计
        self.assertIn("合计 6.0 GiB", text)                     # 窗口外的 99GiB 不算
        self.assertIn("下载最多的用户", text)
        self.assertIn("<code>5</code>", text)
        self.assertEqual(len(markup.inline_keyboard[0]), 2)

    async def test_user_sees_only_their_own_and_no_ranking(self):
        text, _ = await render_daily_view(self.repo, user_id=6)
        self.assertIn("（我的）", text)
        self.assertIn("合计 1.0 GiB", text)
        self.assertNotIn("下载最多的用户", text)

    async def test_empty(self):
        text, _ = await render_daily_view(self.repo, user_id=12345)
        self.assertIn("合计 0 B", text)

    async def test_stats_keyboard_links_to_daily_view(self):
        from bot.core.keyboards import stats_period_keyboard
        data = [b.callback_data for row in stats_period_keyboard("7").inline_keyboard for b in row]
        self.assertIn("stats:daily", data)

    def test_daily_handler_is_registered_before_the_generic_stats_handler(self):
        from bot.handlers import callbacks
        names = [h.callback.__name__ for h in callbacks.router.callback_query.handlers]
        self.assertLess(names.index("show_daily_stats"), names.index("show_stats"))


class TestCategories(unittest.TestCase):
    def test_new_categories(self):
        for name, cat in (("a.pdf", "document"), ("b.EPUB", "document"), ("c.iso", "software"), ("d.apk", "software"),
                          ("e.tar", "archive"), ("f.webm", "video"), ("g.opus", "audio"), ("h.gif", "photo")):
            self.assertEqual(storage.category_for(name), cat, name)

    def test_old_categories_unchanged(self):
        for name, cat in (("a.mp4", "video"), ("a.mkv", "video"), ("a.mp3", "audio"), ("a.jpg", "photo"),
                          ("a.zip", "archive"), ("a.unknown", "other"), (None, "other")):
            self.assertEqual(storage.category_for(name), cat, name)


if __name__ == "__main__":
    unittest.main()
