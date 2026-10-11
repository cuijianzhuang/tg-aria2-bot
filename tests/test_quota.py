"""用户额度：解析、默认值与个别覆盖、放行判断、数据库读写。"""
import os
import tempfile
import unittest
from unittest.mock import patch

from bot.config import settings
from bot.core import quota
from bot.core.quota import GIB, MIB, Limits
from bot.db.repo import TaskRepo


class QuotaTestBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self._orig = (
            settings.allowed_user_ids, settings.admin_user_ids,
            settings.user_max_active_tasks, settings.user_max_task_size_gb, settings.user_daily_quota_gb,
        )
        settings.allowed_user_ids = "1,2"
        settings.admin_user_ids = "1"      # 1 是管理员，2 是普通用户
        settings.user_max_active_tasks = 0
        settings.user_max_task_size_gb = 0
        settings.user_daily_quota_gb = 0

    async def asyncTearDown(self):
        (settings.allowed_user_ids, settings.admin_user_ids, settings.user_max_active_tasks,
         settings.user_max_task_size_gb, settings.user_daily_quota_gb) = self._orig
        await self.repo.close()
        self._dir.cleanup()

    async def _add_task(self, user_id, status="ACTIVE", gid=None):
        gid = gid or f"g{user_id}-{status}-{os.urandom(2).hex()}"
        await self.repo.create_task(
            gid=gid, user_id=user_id, chat_id=1, reply_message_id=None, source_type="url",
            source_ref=gid, file_name="f", file_size=None, payload="u",
        )
        await self.repo.update_status(gid, status)
        return gid


class TestParsing(unittest.TestCase):
    def test_parse_size_units(self):
        self.assertEqual(quota.parse_size("2"), 2 * GIB)          # 不带单位按 GB
        self.assertEqual(quota.parse_size("2G"), 2 * GIB)
        self.assertEqual(quota.parse_size("500M"), 500 * MIB)
        self.assertEqual(quota.parse_size("1.5g"), int(1.5 * GIB))
        self.assertEqual(quota.parse_size("1T"), 1024 * GIB)
        self.assertEqual(quota.parse_size("0"), 0)

    def test_parse_size_rejects_garbage(self):
        for bad in ("abc", "2x", "G", "", "--1", "-1"):
            with self.assertRaises(ValueError, msg=bad):
                quota.parse_size(bad)

    def test_parse_setquota_all_fields(self):
        uid, changes = quota.parse_setquota("123 active=3 size=2G daily=20G")
        self.assertEqual(uid, 123)
        self.assertEqual(changes, {"max_active": 3, "max_task_bytes": 2 * GIB, "daily_bytes": 20 * GIB})

    def test_parse_setquota_default_resets_one_field(self):
        _, changes = quota.parse_setquota("5 daily=default")
        self.assertEqual(changes, {"daily_bytes": None})

    def test_parse_setquota_zero_means_unlimited(self):
        _, changes = quota.parse_setquota("5 active=0 size=0")
        self.assertEqual(changes, {"max_active": 0, "max_task_bytes": 0})

    def test_parse_setquota_reset(self):
        self.assertEqual(quota.parse_setquota("5 reset"), (5, None))

    def test_parse_setquota_errors(self):
        for bad in ("", "5", "x active=1", "5 active=abc", "5 foo=1", "5 active", "5 size=zzz"):
            with self.assertRaises(ValueError, msg=bad):
                quota.parse_setquota(bad)


class TestLimits(QuotaTestBase):
    async def test_defaults_come_from_settings(self):
        settings.user_max_active_tasks = 3
        settings.user_max_task_size_gb = 2
        settings.user_daily_quota_gb = 20
        self.assertEqual(await quota.limits_for(self.repo, 2), Limits(3, 2 * GIB, 20 * GIB))

    async def test_admin_is_never_limited(self):
        settings.user_max_active_tasks = 1
        await self.repo.set_user_quota(1, {"max_active": 1})
        self.assertTrue((await quota.limits_for(self.repo, 1)).unlimited)

    async def test_override_beats_default_and_zero_means_unlimited(self):
        settings.user_max_active_tasks = 3
        settings.user_daily_quota_gb = 20
        await self.repo.set_user_quota(2, {"max_active": 10, "daily_bytes": 0})
        self.assertEqual(await quota.limits_for(self.repo, 2), Limits(10, 0, 0))

    async def test_null_override_falls_back_to_default(self):
        settings.user_max_active_tasks = 3
        await self.repo.set_user_quota(2, {"max_active": 10})
        await self.repo.set_user_quota(2, {"max_active": None})
        self.assertEqual((await quota.limits_for(self.repo, 2)).max_active, 3)

    async def test_default_is_unlimited_so_existing_deployments_are_unchanged(self):
        self.assertTrue((await quota.limits_for(self.repo, 2)).unlimited)


class TestRepoQuota(QuotaTestBase):
    async def test_set_partial_fields_keeps_the_others(self):
        await self.repo.set_user_quota(2, {"max_active": 4})
        await self.repo.set_user_quota(2, {"daily_bytes": 5})
        row = await self.repo.get_user_quota(2)
        self.assertEqual((row["max_active"], row["max_task_bytes"], row["daily_bytes"]), (4, None, 5))

    async def test_unknown_field_is_rejected_not_interpolated_into_sql(self):
        with self.assertRaises(ValueError):
            await self.repo.set_user_quota(2, {"max_active; DROP TABLE tasks; --": 1})

    async def test_clear(self):
        await self.repo.set_user_quota(2, {"max_active": 4})
        await self.repo.clear_user_quota(2)
        self.assertIsNone(await self.repo.get_user_quota(2))

    async def test_count_active_counts_pending_active_paused_only(self):
        for st in ("PENDING", "ACTIVE", "PAUSED", "COMPLETED", "FAILED", "CANCELLED"):
            await self._add_task(2, st)
        await self._add_task(3, "ACTIVE")  # 别人的不算
        self.assertEqual(await self.repo.count_active_by_user(2), 3)

    async def test_usage_accumulates_per_user_per_day(self):
        await self.repo.add_usage(2, "2026-10-10", 100)
        await self.repo.add_usage(2, "2026-10-10", 50)
        await self.repo.add_usage(2, "2026-10-11", 7)
        await self.repo.add_usage(3, "2026-10-10", 999)
        self.assertEqual(await self.repo.get_usage(2, "2026-10-10"), 150)
        self.assertEqual(await self.repo.get_usage(2, "2026-10-11"), 7)
        self.assertEqual(await self.repo.get_usage(2, "2026-10-12"), 0)

    async def test_prune_usage(self):
        await self.repo.add_usage(2, "2026-01-01", 1)
        await self.repo.add_usage(2, "2026-10-10", 1)
        self.assertEqual(await self.repo.prune_usage("2026-06-01"), 1)
        self.assertEqual(await self.repo.get_usage(2, "2026-01-01"), 0)
        self.assertEqual(await self.repo.get_usage(2, "2026-10-10"), 1)


class TestAdmission(QuotaTestBase):
    async def test_unlimited_always_passes(self):
        self.assertIsNone(await quota.admission_error(self.repo, 2, 10**12))

    async def test_admin_always_passes(self):
        settings.user_max_active_tasks = 1
        await self._add_task(1)
        self.assertIsNone(await quota.admission_error(self.repo, 1, None))

    async def test_active_task_limit(self):
        settings.user_max_active_tasks = 2
        await self._add_task(2, "ACTIVE")
        self.assertIsNone(await quota.admission_error(self.repo, 2, None))
        await self._add_task(2, "PAUSED")
        err = await quota.admission_error(self.repo, 2, None)
        self.assertIn("最多同时进行 2 个任务", err)

    async def test_finished_tasks_free_up_slots(self):
        settings.user_max_active_tasks = 1
        gid = await self._add_task(2, "ACTIVE")
        self.assertIsNotNone(await quota.admission_error(self.repo, 2, None))
        await self.repo.update_status(gid, "COMPLETED")
        self.assertIsNone(await quota.admission_error(self.repo, 2, None))

    async def test_resume_does_not_count_its_own_slot(self):
        settings.user_max_active_tasks = 1
        await self._add_task(2, "PAUSED")
        self.assertIsNotNone(await quota.admission_error(self.repo, 2, None))
        self.assertIsNone(await quota.admission_error(self.repo, 2, None, check_active=False))

    async def test_task_size_limit(self):
        settings.user_max_task_size_gb = 2
        self.assertIsNone(await quota.admission_error(self.repo, 2, 2 * GIB))
        err = await quota.admission_error(self.repo, 2, 3 * GIB)
        self.assertIn("单任务上限", err)
        self.assertIn("2.0 GiB", err)

    async def test_unknown_size_is_not_refused_on_size(self):
        settings.user_max_task_size_gb = 1
        self.assertIsNone(await quota.admission_error(self.repo, 2, None))  # 大小要等下载时才知道

    async def test_daily_quota_exhausted(self):
        settings.user_daily_quota_gb = 10
        await self.repo.add_usage(2, quota.today(), 10 * GIB)
        err = await quota.admission_error(self.repo, 2, None)
        self.assertIn("今天的下载额度已用完", err)

    async def test_daily_quota_file_would_exceed_remaining(self):
        settings.user_daily_quota_gb = 10
        await self.repo.add_usage(2, quota.today(), 8 * GIB)
        err = await quota.admission_error(self.repo, 2, 3 * GIB)
        self.assertIn("还剩 2.0 GiB", err)
        self.assertIsNone(await quota.admission_error(self.repo, 2, 2 * GIB))

    async def test_pending_in_memory_usage_counts(self):
        settings.user_daily_quota_gb = 10
        await self.repo.add_usage(2, quota.today(), 6 * GIB)
        self.assertIsNone(await quota.admission_error(self.repo, 2, None))
        self.assertIsNotNone(await quota.admission_error(self.repo, 2, None, pending_usage=4 * GIB))

    async def test_yesterdays_usage_does_not_count(self):
        settings.user_daily_quota_gb = 10
        await self.repo.add_usage(2, "2000-01-01", 100 * GIB)
        self.assertIsNone(await quota.admission_error(self.repo, 2, None))

    async def test_day_rollover_resets_quota(self):
        settings.user_daily_quota_gb = 10
        await self.repo.add_usage(2, "2026-10-10", 10 * GIB)
        with patch("bot.core.quota.today", return_value="2026-10-10"):
            self.assertIsNotNone(await quota.admission_error(self.repo, 2, None))
        with patch("bot.core.quota.today", return_value="2026-10-11"):
            self.assertIsNone(await quota.admission_error(self.repo, 2, None))


class TestRenderQuota(QuotaTestBase):
    async def test_admin(self):
        self.assertIn("不受任何额度限制", await quota.render_quota(self.repo, 1))

    async def test_no_limits_configured(self):
        self.assertIn("没有任何限制", await quota.render_quota(self.repo, 2))

    async def test_shows_usage_against_each_limit(self):
        settings.user_max_active_tasks = 3
        settings.user_daily_quota_gb = 20
        settings.user_max_task_size_gb = 2
        await self._add_task(2, "ACTIVE")
        await self.repo.add_usage(2, quota.today(), 5 * GIB)
        text = await quota.render_quota(self.repo, 2)
        self.assertIn("进行中任务：1 / 3", text)
        self.assertIn("今日流量：5.0 GiB / 20.0 GiB（剩余 15.0 GiB）", text)
        self.assertIn("单任务上限：2.0 GiB", text)

    async def test_unlimited_fields_say_so(self):
        settings.user_daily_quota_gb = 20
        text = await quota.render_quota(self.repo, 2)
        self.assertIn("进行中任务：不限", text)
        self.assertIn("单任务上限：不限", text)


if __name__ == "__main__":
    unittest.main()
