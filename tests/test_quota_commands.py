"""/quota（所有人看自己的，管理员可看别人的）和 /setquota（仅管理员）。"""
import os
import tempfile
import unittest
from types import SimpleNamespace

from bot.config import settings
from bot.core import quota
from bot.core.quota import GIB
from bot.db.repo import TaskRepo
from bot.handlers import admin, commands
from bot.middlewares.auth import AdminMiddleware


class FakeMsg:
    def __init__(self, user_id):
        self.from_user = SimpleNamespace(id=user_id)
        self.replies: list[str] = []

    async def reply(self, text, **kw):
        self.replies.append(text)


class FakeTM:
    def __init__(self):
        self.invalidated = 0
        self.pending = 0

    def invalidate_limits(self):
        self.invalidated += 1

    def pending_usage(self, user_id):
        return self.pending


class CommandTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self._orig = (settings.allowed_user_ids, settings.admin_user_ids,
                      settings.user_max_active_tasks, settings.user_max_task_size_gb, settings.user_daily_quota_gb)
        settings.allowed_user_ids = "1,2"
        settings.admin_user_ids = "1"
        settings.user_max_active_tasks = 3
        settings.user_max_task_size_gb = 0
        settings.user_daily_quota_gb = 20
        self.tm = FakeTM()

    async def asyncTearDown(self):
        (settings.allowed_user_ids, settings.admin_user_ids, settings.user_max_active_tasks,
         settings.user_max_task_size_gb, settings.user_daily_quota_gb) = self._orig
        await self.repo.close()
        self._dir.cleanup()


class TestQuotaCommand(CommandTestBase):
    async def test_user_sees_own_limits_and_usage(self):
        await self.repo.add_usage(2, quota.today(), 5 * GIB)
        msg = FakeMsg(2)
        await commands.cmd_quota(msg, SimpleNamespace(args=None), self.repo, self.tm)
        self.assertIn("我的额度", msg.replies[0])
        self.assertIn("5.0 GiB / 20.0 GiB", msg.replies[0])

    async def test_in_memory_usage_is_included(self):
        self.tm.pending = 2 * GIB
        msg = FakeMsg(2)
        await commands.cmd_quota(msg, SimpleNamespace(args=None), self.repo, self.tm)
        self.assertIn("2.0 GiB / 20.0 GiB", msg.replies[0])

    async def test_normal_user_cannot_look_at_someone_else(self):
        msg = FakeMsg(2)
        await commands.cmd_quota(msg, SimpleNamespace(args="3"), self.repo, self.tm)
        self.assertIn("只有管理员", msg.replies[0])

    async def test_admin_can_look_at_a_user(self):
        await self.repo.add_usage(2, quota.today(), 7 * GIB)
        msg = FakeMsg(1)
        await commands.cmd_quota(msg, SimpleNamespace(args="2"), self.repo, self.tm)
        self.assertIn("<code>2</code>", msg.replies[0])
        self.assertIn("7.0 GiB", msg.replies[0])

    async def test_admin_own_quota(self):
        msg = FakeMsg(1)
        await commands.cmd_quota(msg, SimpleNamespace(args=None), self.repo, self.tm)
        self.assertIn("不受任何额度限制", msg.replies[0])

    async def test_bad_argument(self):
        msg = FakeMsg(1)
        await commands.cmd_quota(msg, SimpleNamespace(args="abc"), self.repo, self.tm)
        self.assertIn("用法", msg.replies[0])


class TestSetQuotaCommand(CommandTestBase):
    async def _set(self, args, user_id=1):
        msg = FakeMsg(user_id)
        await admin.cmd_setquota(msg, SimpleNamespace(args=args), self.repo, self.tm)
        return msg.replies[0]

    async def test_sets_fields_and_shows_the_result(self):
        reply = await self._set("2 active=5 size=2G daily=50G")
        row = await self.repo.get_user_quota(2)
        self.assertEqual((row["max_active"], row["max_task_bytes"], row["daily_bytes"]), (5, 2 * GIB, 50 * GIB))
        self.assertIn("已更新", reply)
        self.assertIn("0 / 5", reply)
        self.assertIn("50.0 GiB", reply)

    async def test_takes_effect_immediately_by_dropping_the_cache(self):
        await self._set("2 active=5")
        self.assertEqual(self.tm.invalidated, 1)

    async def test_default_restores_the_env_default(self):
        await self._set("2 daily=1G")
        reply = await self._set("2 daily=default")
        self.assertIn("20.0 GiB", reply)          # 回到 .env 默认的 20GB

    async def test_zero_means_unlimited(self):
        reply = await self._set("2 daily=0")
        self.assertIn("今日流量：不限", reply)

    async def test_reset_clears_everything(self):
        await self._set("2 active=9 size=1G")
        await self._set("2 reset")
        self.assertIsNone(await self.repo.get_user_quota(2))

    async def test_bad_input_is_explained_and_changes_nothing(self):
        reply = await self._set("2 size=lots")
        self.assertIn("看不懂", reply)
        self.assertIn("/setquota", reply)
        self.assertIsNone(await self.repo.get_user_quota(2))

    async def test_no_args_shows_usage(self):
        msg = FakeMsg(1)
        await admin.cmd_setquota(msg, SimpleNamespace(args=None), self.repo, self.tm)
        self.assertIn("/setquota", msg.replies[0])

    async def test_setting_an_admins_quota_is_declined(self):
        reply = await self._set("1 active=1")
        self.assertIn("不受额度限制", reply)
        self.assertIsNone(await self.repo.get_user_quota(1))

    async def test_html_in_error_is_escaped(self):
        reply = await self._set("2 <b>=1")
        self.assertNotIn("<b>=", reply)

    async def test_command_lives_on_the_admin_only_router(self):
        self.assertTrue(any(isinstance(m, AdminMiddleware) for m in admin.router.message.middleware))
        names = {h.callback.__name__ for h in admin.router.message.handlers}
        self.assertIn("cmd_setquota", names)
        self.assertNotIn("cmd_setquota", {h.callback.__name__ for h in commands.router.message.handlers})
        self.assertIn("cmd_quota", {h.callback.__name__ for h in commands.router.message.handlers})


class TestBotCommandMenu(unittest.TestCase):
    def test_quota_is_listed_in_the_command_menu(self):
        from bot.main import BOT_COMMANDS
        self.assertIn("quota", [c.command for c in BOT_COMMANDS])


if __name__ == "__main__":
    unittest.main()
