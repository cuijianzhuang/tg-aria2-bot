"""限速时段：解析、跨午夜、规则优先级、/schedule 命令、循环只在变化时才改 aria2。"""
import asyncio
import unittest
from datetime import time
from types import SimpleNamespace
from unittest.mock import patch

from bot.config import settings
from bot.core import schedule
from bot.handlers import ops
from bot.middlewares.auth import AdminMiddleware


class TestParse(unittest.TestCase):
    def test_parses_rules(self):
        rules = schedule.parse("23:00-07:00=0;07:00-23:00=2M")
        self.assertEqual([str(r) for r in rules], ["23:00-07:00=0", "07:00-23:00=2M"])

    def test_empty_means_no_rules(self):
        self.assertEqual(schedule.parse(""), [])
        self.assertEqual(schedule.parse("  ; "), [])

    def test_spaces_chinese_semicolon_and_lowercase_unit(self):
        rules = schedule.parse("9:00 - 18:30 = 500k；18:30-9:00=0")
        self.assertEqual(rules[0].limit, "500K")
        self.assertEqual(rules[0].start, time(9, 0))

    def test_bad_input_is_rejected_with_a_readable_message(self):
        for bad in ("hello", "25:00-07:00=1M", "07:00-08:00", "07:00-08:00=fast", "07:00-08:61=1M"):
            with self.assertRaises(ValueError, msg=bad):
                schedule.parse(bad)

    def test_too_many_rules(self):
        with self.assertRaises(ValueError):
            schedule.parse(";".join(f"0{i}:00-0{i}:30=1M" for i in range(9)))


class TestLimitAt(unittest.TestCase):
    def setUp(self):
        self.rules = schedule.parse("23:00-07:00=0;07:00-23:00=2M")

    def test_across_midnight(self):
        for t in (time(23, 0), time(23, 59), time(0, 0), time(6, 59)):
            self.assertEqual(schedule.limit_at(self.rules, t), "0", t)

    def test_daytime(self):
        for t in (time(7, 0), time(12, 0), time(22, 59)):
            self.assertEqual(schedule.limit_at(self.rules, t), "2M", t)

    def test_outside_all_windows_is_unlimited(self):
        self.assertEqual(schedule.limit_at(schedule.parse("09:00-10:00=1M"), time(12, 0)), "0")

    def test_first_matching_rule_wins(self):
        rules = schedule.parse("09:00-12:00=1M;10:00-11:00=5M")
        self.assertEqual(schedule.limit_at(rules, time(10, 30)), "1M")

    def test_end_is_exclusive(self):
        self.assertEqual(schedule.limit_at(schedule.parse("09:00-10:00=1M"), time(10, 0)), "0")

    def test_describe(self):
        text = schedule.describe(self.rules, time(12, 0))
        self.assertIn("2M/s", text)
        self.assertIn("此刻生效：2M/s", text)
        self.assertIn("未设置", schedule.describe([]))


class FakeAria2:
    def __init__(self):
        self.limits = []

    async def set_global_limit(self, value):
        self.limits.append(value)


class TestLoop(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = settings.speed_schedule
        self.addCleanup(lambda: setattr(settings, "speed_schedule", self._orig))

    async def _run(self, aria2, seconds=0.05):
        with patch.object(schedule, "CHECK_INTERVAL_SECONDS", 0.01):
            task = asyncio.create_task(schedule.schedule_loop(aria2))
            await asyncio.sleep(seconds)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task

    async def test_applies_once_not_every_tick(self):
        settings.speed_schedule = "00:00-00:00=3M"      # 全天
        aria2 = FakeAria2()
        await self._run(aria2)
        self.assertEqual(aria2.limits, ["3M"])          # 几十个 tick 只改了一次

    async def test_no_rules_never_touches_aria2(self):
        settings.speed_schedule = ""
        aria2 = FakeAria2()
        await self._run(aria2)
        self.assertEqual(aria2.limits, [])

    async def test_invalid_rules_do_not_crash_the_loop(self):
        settings.speed_schedule = "garbage"
        aria2 = FakeAria2()
        await self._run(aria2)
        self.assertEqual(aria2.limits, [])

    async def test_aria2_down_is_retried(self):
        settings.speed_schedule = "00:00-00:00=3M"

        class Flaky(FakeAria2):
            async def set_global_limit(self, value):
                if not self.limits and not getattr(self, "failed", False):
                    self.failed = True
                    raise ConnectionError
                await super().set_global_limit(value)

        aria2 = Flaky()
        await self._run(aria2)
        self.assertEqual(aria2.limits, ["3M"])

    async def test_changing_the_rules_reapplies(self):
        settings.speed_schedule = "00:00-00:00=3M"
        aria2 = FakeAria2()
        with patch.object(schedule, "CHECK_INTERVAL_SECONDS", 0.01):
            task = asyncio.create_task(schedule.schedule_loop(aria2))
            await asyncio.sleep(0.04)
            settings.speed_schedule = "00:00-00:00=1M"
            await asyncio.sleep(0.04)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertEqual(aria2.limits, ["3M", "1M"])


class FakeMsg:
    def __init__(self):
        self.replies = []

    async def reply(self, text, **kw):
        self.replies.append(text)


class TestCommand(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = settings.speed_schedule
        self.addCleanup(lambda: setattr(settings, "speed_schedule", self._orig))
        p = patch.object(ops, "_persist")
        self.persist = p.start()
        self.addCleanup(p.stop)

    async def _cmd(self, args):
        msg = FakeMsg()
        await ops.cmd_schedule(msg, SimpleNamespace(args=args))
        return msg.replies[0]

    async def test_set_applies_and_persists(self):
        reply = await self._cmd("set 23:00-07:00=0;07:00-23:00=2M")
        self.assertIn("已设置", reply)
        self.assertEqual(settings.speed_schedule, "23:00-07:00=0;07:00-23:00=2M")
        self.persist.assert_called_with("23:00-07:00=0;07:00-23:00=2M")

    async def test_bad_rule_changes_nothing(self):
        settings.speed_schedule = ""
        reply = await self._cmd("set nonsense")
        self.assertIn("看不懂", reply)
        self.assertEqual(settings.speed_schedule, "")
        self.persist.assert_not_called()

    async def test_off(self):
        settings.speed_schedule = "00:00-00:00=1M"
        await self._cmd("off")
        self.assertEqual(settings.speed_schedule, "")

    async def test_show_current(self):
        settings.speed_schedule = "09:00-10:00=1M"
        self.assertIn("09:00–10:00", await self._cmd(None))

    async def test_html_in_error_is_escaped(self):
        self.assertNotIn("<b>x", await self._cmd("set <b>x"))

    def test_router_is_admin_only(self):
        self.assertTrue(any(isinstance(m, AdminMiddleware) for m in ops.router.message.middleware))


class TestSettingsFile(unittest.TestCase):
    def test_value_survives_a_dotenv_round_trip(self):
        import os
        import tempfile

        from bot.core.conf_editor import write_kv
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, ".env")
            open(path, "w").write("BOT_TOKEN=t\nAPI_ID=1\nAPI_HASH=h\nARIA2_SECRET=s\n")
            write_kv(path, "SPEED_SCHEDULE", "23:00-07:00=0;07:00-23:00=2M")
            from bot.config import Settings
            loaded = Settings(_env_file=path)
            self.assertEqual(loaded.speed_schedule, "23:00-07:00=0;07:00-23:00=2M")
            self.assertEqual(len(schedule.parse(loaded.speed_schedule)), 2)


if __name__ == "__main__":
    unittest.main()
