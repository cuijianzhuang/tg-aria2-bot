"""VIP 等级：授予/顺延/撤销、兑换码、额度、功能限制、优先排队、到期提醒、命令。"""
import os
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.config import settings
from bot.core import license as lic
from bot.core import quota, vip
from bot.core.quota import GIB
from bot.core.vip import DAY, VipError
from bot.db.repo import TaskRepo
from bot.handlers import callbacks
from bot.handlers import vip as vip_handler

SETTING_KEYS = ("admin_user_ids", "allowed_user_ids", "vip_only_features", "vip_priority", "vip_max_active_tasks",
                "vip_max_task_size_gb", "vip_daily_quota_gb", "user_max_active_tasks", "user_max_task_size_gb",
                "user_daily_quota_gb", "vip_warn_days", "rss_max_feeds_vip", "rss_max_feeds_per_user")


class VipBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.addAsyncCleanup(self.repo.close)
        self._orig = {k: getattr(settings, k) for k in SETTING_KEYS}
        self.addCleanup(lambda: [setattr(settings, k, v) for k, v in self._orig.items()])
        settings.admin_user_ids = "1"
        settings.allowed_user_ids = "1,5,6"
        settings.vip_only_features = ""
        settings.user_max_active_tasks = 2
        settings.user_max_task_size_gb = 1
        settings.user_daily_quota_gb = 10
        settings.vip_max_active_tasks = 0
        settings.vip_max_task_size_gb = 0
        settings.vip_daily_quota_gb = 0
        vip.reset_rate_limits()


class TestGrant(VipBase):
    async def test_new_user_is_not_vip(self):
        self.assertFalse(await vip.is_vip(self.repo, 5))

    async def test_grant_days(self):
        exp = await vip.extend(self.repo, 5, 30, granted_by=1)
        self.assertAlmostEqual(exp, time.time() + 30 * DAY, delta=5)
        self.assertTrue(await vip.is_vip(self.repo, 5))

    async def test_extending_stacks_on_the_remaining_time(self):
        first = await vip.extend(self.repo, 5, 10, granted_by=1)
        second = await vip.extend(self.repo, 5, 10, granted_by=1)
        self.assertAlmostEqual(second, first + 10 * DAY, delta=5)

    async def test_extending_an_expired_vip_starts_from_now(self):
        await self.repo.set_vip(5, time.time() - 100 * DAY, granted_by=1)
        exp = await vip.extend(self.repo, 5, 5, granted_by=1)
        self.assertAlmostEqual(exp, time.time() + 5 * DAY, delta=5)

    async def test_forever_and_a_short_grant_never_downgrades_it(self):
        self.assertIsNone(await vip.extend(self.repo, 5, 0, granted_by=1))
        self.assertIsNone(await vip.extend(self.repo, 5, 7, granted_by=1))
        self.assertTrue(await vip.is_vip(self.repo, 5))

    async def test_expiry_takes_effect_by_itself(self):
        await self.repo.set_vip(5, time.time() - 1, granted_by=1)
        self.assertFalse(await vip.is_vip(self.repo, 5))

    async def test_revoke(self):
        await vip.extend(self.repo, 5, 30, granted_by=1)
        self.assertTrue(await self.repo.remove_vip(5))
        self.assertFalse(await vip.is_vip(self.repo, 5))
        self.assertFalse(await self.repo.remove_vip(5))

    async def test_negative_days_refused(self):
        with self.assertRaises(VipError):
            await vip.extend(self.repo, 5, -1, granted_by=1)


class TestCodes(VipBase):
    async def test_code_format_and_uniqueness(self):
        codes = await vip.create_codes(self.repo, 30, 20, created_by=1)
        self.assertEqual(len(set(codes)), 20)
        for c in codes:
            self.assertRegex(c, r"^VIP-[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}-[A-HJKMNP-Z2-9]{4}$")

    async def test_redeem_grants_vip(self):
        (code,) = await vip.create_codes(self.repo, 30, 1, created_by=1)
        exp = await vip.redeem(self.repo, 5, code)
        self.assertAlmostEqual(exp, time.time() + 30 * DAY, delta=5)
        self.assertTrue(await vip.is_vip(self.repo, 5))

    async def test_sloppy_input_is_accepted(self):
        (code,) = await vip.create_codes(self.repo, 30, 1, created_by=1)
        sloppy = " " + code.lower().replace("-", " ") + " "
        await vip.redeem(self.repo, 5, sloppy)
        self.assertTrue(await vip.is_vip(self.repo, 5))

    async def test_a_code_works_only_once(self):
        (code,) = await vip.create_codes(self.repo, 30, 1, created_by=1)
        await vip.redeem(self.repo, 5, code)
        with self.assertRaises(VipError):
            await vip.redeem(self.repo, 6, code)
        self.assertFalse(await vip.is_vip(self.repo, 6))

    async def test_two_people_racing_for_one_code(self):
        (code,) = await vip.create_codes(self.repo, 30, 1, created_by=1)
        now = time.time()
        results = [await self.repo.claim_vip_code(code, 5, now), await self.repo.claim_vip_code(code, 6, now)]
        self.assertEqual(results, [True, False])

    async def test_wrong_codes_are_rate_limited(self):
        for _ in range(vip.REDEEM_MAX_FAILS):
            with self.assertRaises(VipError):
                await vip.redeem(self.repo, 5, "VIP-AAAA-BBBB-CCCC")
        (code,) = await vip.create_codes(self.repo, 30, 1, created_by=1)
        with self.assertRaises(VipError) as cm:
            await vip.redeem(self.repo, 5, code)           # 即使这次是对的也要等
        self.assertIn("太多", str(cm.exception))
        await vip.redeem(self.repo, 6, code)               # 别人不受影响

    async def test_expired_code_is_refused(self):
        (code,) = await vip.create_codes(self.repo, 30, 1, created_by=1, valid_days=1)
        await self.repo._conn.execute("UPDATE vip_codes SET expires_at = ?", (time.time() - 1,))
        await self.repo._conn.commit()
        with self.assertRaises(VipError):
            await vip.redeem(self.repo, 5, code)

    async def test_permanent_code(self):
        (code,) = await vip.create_codes(self.repo, 0, 1, created_by=1)
        self.assertIsNone(await vip.redeem(self.repo, 5, code))

    async def test_deleting_unused_codes_only(self):
        a, b = await vip.create_codes(self.repo, 30, 2, created_by=1)
        await vip.redeem(self.repo, 5, a)
        self.assertFalse(await self.repo.delete_vip_code(a))
        self.assertTrue(await self.repo.delete_vip_code(b))
        with self.assertRaises(VipError):
            await vip.redeem(self.repo, 6, b)

    async def test_batch_size_limits(self):
        for n in (0, vip.MAX_CODES_PER_BATCH + 1):
            with self.assertRaises(VipError):
                await vip.create_codes(self.repo, 30, n, created_by=1)

    def test_normalize(self):
        self.assertEqual(vip.normalize_code("vip-aaaa-bbbb-cccc"), "VIP-AAAA-BBBB-CCCC")     # 大小写无所谓
        self.assertEqual(vip.normalize_code("VIP-ABCD-EFGH-JKMI"), "")           # I/L/O 不在字母表里
        self.assertEqual(vip.normalize_code("garbage"), "")
        self.assertEqual(vip.normalize_code(""), "")
        self.assertEqual(vip.normalize_code("VIP-ABCD-EFGH-JKMN"), "VIP-ABCD-EFGH-JKMN")
        self.assertEqual(vip.normalize_code("abcdefghjkmn"), "VIP-ABCD-EFGH-JKMN")
        self.assertEqual(vip.normalize_code("VIP-ABCD-EFGH-JKM0"), "")           # 0 不在字母表里


class TestQuotas(VipBase):
    async def test_normal_user_gets_normal_limits(self):
        limits = await quota.limits_for(self.repo, 5)
        self.assertEqual((limits.max_active, limits.max_task_bytes, limits.daily_bytes), (2, GIB, 10 * GIB))

    async def test_vip_gets_vip_limits(self):
        settings.vip_max_active_tasks = 10
        settings.vip_daily_quota_gb = 100
        await vip.extend(self.repo, 5, 30, granted_by=1)
        limits = await quota.limits_for(self.repo, 5)
        self.assertEqual((limits.max_active, limits.max_task_bytes, limits.daily_bytes), (10, 0, 100 * GIB))

    async def test_personal_override_beats_the_vip_default(self):
        settings.vip_max_active_tasks = 10
        await vip.extend(self.repo, 5, 30, granted_by=1)
        await self.repo.set_user_quota(5, {"max_active": 4})
        self.assertEqual((await quota.limits_for(self.repo, 5)).max_active, 4)

    async def test_expired_vip_falls_back(self):
        await self.repo.set_vip(5, time.time() - 1, granted_by=1)
        self.assertEqual((await quota.limits_for(self.repo, 5)).max_active, 2)

    async def test_admin_still_unlimited(self):
        self.assertTrue((await quota.limits_for(self.repo, 1)).unlimited)

    async def test_quota_text_shows_vip(self):
        await vip.extend(self.repo, 5, 30, granted_by=1)
        self.assertIn("👑 VIP", await quota.render_quota(self.repo, 5))


class TestFeatures(VipBase):
    async def test_nothing_is_gated_by_default(self):
        for f in vip.FEATURES:
            self.assertTrue(await vip.feature_allowed(self.repo, 5, f))

    async def test_gated_feature(self):
        settings.vip_only_features = "rss, node,bogus"
        self.assertEqual(vip.gated_features(), {"rss", "node"})
        self.assertFalse(await vip.feature_allowed(self.repo, 5, "rss"))
        self.assertTrue(await vip.feature_allowed(self.repo, 5, "gofile"))     # 没被限制的不受影响
        await vip.extend(self.repo, 5, 30, granted_by=1)
        self.assertTrue(await vip.feature_allowed(self.repo, 5, "rss"))

    async def test_admin_always_allowed(self):
        settings.vip_only_features = "rss"
        self.assertTrue(await vip.feature_allowed(self.repo, 1, "rss"))

    async def test_unknown_user_is_denied_when_gated(self):
        settings.vip_only_features = "rss"
        self.assertFalse(await vip.feature_allowed(self.repo, None, "rss"))

    async def test_rss_feed_limit_is_higher_for_vip(self):
        settings.rss_max_feeds_per_user, settings.rss_max_feeds_vip = 2, 7
        self.assertEqual((vip.max_feeds(False), vip.max_feeds(True)), (2, 7))

    async def test_node_buttons_are_blocked_for_non_vip(self):
        settings.vip_only_features = "node"
        answers = []

        class Q:
            data = "node:use:tokyo"
            from_user = SimpleNamespace(id=5)

            async def answer(self, text=None, show_alert=False, **kw):
                answers.append((text, show_alert))

        class Nodes:
            def get_node(self, name):
                raise AssertionError("不该走到这里")

        await callbacks.node_use(Q(), self.repo, Nodes())
        self.assertIn("仅限 VIP", answers[0][0])
        self.assertEqual(await self.repo.get_current_node(5), "default")          # 没有被改


class FakeClient:
    def __init__(self, fail=False):
        self.calls, self.fail = [], fail

    async def change_position(self, gid, pos, how):
        if self.fail:
            raise RuntimeError("not waiting")
        self.calls.append((gid, pos, how))


class FakeNodes:
    def __init__(self, client):
        self.client = client

    def get(self, name):
        return self.client


class TestPriority(VipBase):
    async def test_vip_task_goes_to_the_front(self):
        await vip.extend(self.repo, 5, 30, granted_by=1)
        client = FakeClient()
        await vip.prioritize(self.repo, FakeNodes(client), "default", "gid1", 5)
        self.assertEqual(client.calls, [("gid1", 0, "POS_SET")])

    async def test_normal_user_is_not_moved(self):
        client = FakeClient()
        await vip.prioritize(self.repo, FakeNodes(client), "default", "gid1", 5)
        self.assertEqual(client.calls, [])

    async def test_can_be_switched_off(self):
        await vip.extend(self.repo, 5, 30, granted_by=1)
        settings.vip_priority = False
        client = FakeClient()
        await vip.prioritize(self.repo, FakeNodes(client), "default", "gid1", 5)
        self.assertEqual(client.calls, [])

    async def test_failure_is_swallowed(self):
        await vip.extend(self.repo, 5, 30, granted_by=1)
        await vip.prioritize(self.repo, FakeNodes(FakeClient(fail=True)), "default", "gid1", 5)


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kw):
        self.sent.append(kw)


class TestNotices(VipBase):
    async def test_warns_once_then_announces_expiry_once(self):
        settings.vip_warn_days = 3
        await self.repo.set_vip(5, time.time() + 2 * DAY, granted_by=1)
        bot = FakeBot()
        self.assertEqual(await vip.send_notices(bot, self.repo), (1, 0))
        self.assertEqual(await vip.send_notices(bot, self.repo), (0, 0))        # 不重复
        await self.repo._conn.execute("UPDATE vip_users SET expires_at = ?", (time.time() - 1,))
        await self.repo._conn.commit()
        self.assertEqual(await vip.send_notices(bot, self.repo), (0, 1))
        self.assertEqual(await vip.send_notices(bot, self.repo), (0, 0))
        self.assertEqual(len(bot.sent), 2)
        self.assertIn("已到期", bot.sent[1]["text"])

    async def test_far_off_and_permanent_get_nothing(self):
        await self.repo.set_vip(5, time.time() + 30 * DAY, granted_by=1)
        await self.repo.set_vip(6, None, granted_by=1)
        self.assertEqual(await vip.send_notices(FakeBot(), self.repo), (0, 0))

    async def test_renewing_resets_the_flags(self):
        await self.repo.set_vip(5, time.time() + 1 * DAY, granted_by=1)
        bot = FakeBot()
        await vip.send_notices(bot, self.repo)
        await vip.extend(self.repo, 5, 1, granted_by=1)          # 续期后离到期还是不到 3 天
        self.assertEqual(await vip.send_notices(bot, self.repo), (1, 0))

    async def test_expiry_drops_the_limits_cache(self):
        await self.repo.set_vip(5, time.time() - 1, granted_by=1)

        class TM:
            invalidated = 0

            def invalidate_limits(self):
                TM.invalidated += 1
        await vip.send_notices(FakeBot(), self.repo, TM())
        self.assertEqual(TM.invalidated, 1)

    async def test_blocked_user_does_not_cause_retries(self):
        await self.repo.set_vip(5, time.time() - 1, granted_by=1)

        class Blocked:
            async def send_message(self, **kw):
                raise RuntimeError("bot was blocked by the user")
        await vip.send_notices(Blocked(), self.repo)
        self.assertEqual(await vip.send_notices(FakeBot(), self.repo), (0, 0))


class TestLicenseGate(VipBase):
    """启用了授权校验又没有有效授权时，VIP 系统整个关闭，不会把普通用户锁在外面。"""

    def setUp(self):
        self._p = patch.object(lic, "pro_active", return_value=False)
        self._p.start()
        self.addCleanup(self._p.stop)

    async def test_nobody_is_vip(self):
        await self.repo.set_vip(5, None, granted_by=1)
        self.assertFalse(await vip.is_vip(self.repo, 5))

    async def test_gated_features_open_up_instead_of_locking_everyone_out(self):
        settings.vip_only_features = "rss,node"
        self.assertTrue(await vip.feature_allowed(self.repo, 5, "rss"))

    async def test_limits_are_the_normal_ones(self):
        await self.repo.set_vip(5, None, granted_by=1)
        settings.vip_max_active_tasks = 99
        self.assertEqual((await quota.limits_for(self.repo, 5)).max_active, 2)

    async def test_cannot_grant_or_redeem_or_create_codes(self):
        with self.assertRaises(VipError):
            await vip.extend(self.repo, 5, 30, granted_by=1)
        with self.assertRaises(VipError):
            await vip.redeem(self.repo, 5, "VIP-ABCD-EFGH-JKMN")
        with self.assertRaises(VipError):
            await vip.create_codes(self.repo, 30, 1, created_by=1)

    async def test_no_priority_and_no_notices(self):
        client = FakeClient()
        await self.repo.set_vip(5, None, granted_by=1)
        await vip.prioritize(self.repo, FakeNodes(client), "default", "g", 5)
        self.assertEqual(client.calls, [])
        self.assertEqual(await vip.send_notices(FakeBot(), self.repo), (0, 0))


class TestMaxVip(VipBase):
    async def test_license_vip_cap(self):
        info = lic.LicenseInfo("x", "*", 0, 2, 0)
        with patch.object(lic, "current", return_value=info):
            await vip.extend(self.repo, 5, 30, granted_by=1)
            await vip.extend(self.repo, 6, 30, granted_by=1)
            with self.assertRaises(VipError) as cm:
                await vip.extend(self.repo, 7, 30, granted_by=1)
            self.assertIn("上限", str(cm.exception))
            await vip.extend(self.repo, 5, 30, granted_by=1)          # 已经是 VIP 的续期不占新名额

    async def test_full_house_does_not_burn_the_code(self):
        info = lic.LicenseInfo("x", "*", 0, 1, 0)
        with patch.object(lic, "current", return_value=info):
            await vip.extend(self.repo, 5, 30, granted_by=1)
            (code,) = await vip.create_codes(self.repo, 30, 1, created_by=1)
            with self.assertRaises(VipError):
                await vip.redeem(self.repo, 6, code)
            self.assertIsNone((await self.repo.get_vip_code(code))["redeemed_by"])      # 码还在


class FakeMsg:
    def __init__(self, user_id):
        self.from_user = SimpleNamespace(id=user_id)
        self.replies = []
        self.bot = FakeBot()
        self.deleted = False

    async def reply(self, text, **kw):
        self.replies.append(text)

    answer = reply

    async def delete(self):
        self.deleted = True


class TestCommands(VipBase):
    async def _vip(self, args, user_id=1):
        m = FakeMsg(user_id)
        await vip_handler.cmd_vip(m, SimpleNamespace(args=args), self.repo)
        return m

    async def test_admin_grant_notifies_the_user_and_takes_effect(self):
        m = await self._vip("grant 5 30")
        self.assertIn("5", m.replies[0])
        self.assertTrue(await vip.is_vip(self.repo, 5))
        self.assertEqual(m.bot.sent[0]["chat_id"], 5)

    async def test_non_admin_cannot_manage(self):
        m = await self._vip("grant 5 30", user_id=6)
        self.assertIn("只有管理员", m.replies[0])
        self.assertFalse(await vip.is_vip(self.repo, 5))
        m = await self._vip("code 30", user_id=6)
        self.assertIn("只有管理员", m.replies[0])

    async def test_codes_flow_end_to_end(self):
        m = await self._vip("code 30 2")
        codes = [line.strip("<code>/ ") for line in m.replies[0].splitlines() if line.startswith("<code>VIP-")]
        self.assertEqual(len(codes), 2)
        r = FakeMsg(5)
        await vip_handler.cmd_redeem(r, SimpleNamespace(args=codes[0]), self.repo)
        self.assertIn("兑换成功", r.replies[0])
        self.assertTrue(r.deleted)
        self.assertTrue(await vip.is_vip(self.repo, 5))
        listing = await self._vip("codes")
        self.assertIn(codes[1], listing.replies[0])
        self.assertNotIn(codes[0], listing.replies[0])

    async def test_redeem_errors_are_shown(self):
        r = FakeMsg(5)
        await vip_handler.cmd_redeem(r, SimpleNamespace(args="VIP-ABCD-EFGH-JKMN"), self.repo)
        self.assertIn("无效", r.replies[0])
        r = FakeMsg(5)
        await vip_handler.cmd_redeem(r, SimpleNamespace(args=None), self.repo)
        self.assertIn("用法", r.replies[0])

    async def test_revoke_list_and_bad_input(self):
        await self._vip("grant 5 forever")
        self.assertIn("5", (await self._vip("list")).replies[0])
        self.assertIn("已撤销", (await self._vip("revoke 5")).replies[0])
        self.assertIn("天数", (await self._vip("grant 5 abc")).replies[0])
        self.assertIn("/vip grant", (await self._vip("bogus")).replies[0])

    async def test_status_for_users(self):
        self.assertIn("普通用户", (await self._vip(None, user_id=5)).replies[0])
        await vip.extend(self.repo, 5, 30, granted_by=1)
        self.assertIn("你是 VIP", (await self._vip(None, user_id=5)).replies[0])
        admin = (await self._vip(None, user_id=1)).replies[0]
        self.assertIn("管理员", admin)
        self.assertIn("/vip grant", admin)

    async def test_command_list_includes_vip_and_redeem(self):
        from bot.main import BOT_COMMANDS
        names = {c.command for c in BOT_COMMANDS}
        self.assertTrue({"vip", "redeem"} <= names)


if __name__ == "__main__":
    unittest.main()
