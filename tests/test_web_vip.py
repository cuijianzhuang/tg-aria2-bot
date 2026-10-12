"""Web 后台的授权 / VIP 接口：都要登录；授权码校验；授予、撤销、兑换码；授权失效时的行为。
接口函数直接调用（没有装 httpx，用不了 TestClient），登录依赖单独检查挂在每个路由上。"""
import base64
import json
import os
import tempfile
import time
import unittest
from unittest.mock import AsyncMock, patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from fastapi import HTTPException
from fastapi.routing import APIRoute

from bot.config import settings
from bot.core import license as lic
from bot.core import notify, vip
from bot.db.repo import TaskRepo
from bot.web import app as web


def b64e(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class WebVipBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.addAsyncCleanup(self.repo.close)
        web.state["repo"] = self.repo
        self.addCleanup(web.state.clear)
        self.key = Ed25519PrivateKey.generate()
        pub = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self._orig = (settings.db_path, settings.license_key, settings.bot_token, lic.PUBLIC_KEY_B64)
        settings.db_path = os.path.join(self._dir.name, "tasks.db")
        settings.license_key = ""
        settings.bot_token = "123456:ABC"
        lic.PUBLIC_KEY_B64 = b64e(pub)
        self.addCleanup(lambda: (setattr(settings, "db_path", self._orig[0]), setattr(settings, "license_key", self._orig[1]),
                                 setattr(settings, "bot_token", self._orig[2]), setattr(lic, "PUBLIC_KEY_B64", self._orig[3])))
        vip.reset_rate_limits()
        # 授予 VIP 会通知用户：测试里不能真的去连 Bot API
        p = patch.object(notify, "send_message", AsyncMock(return_value=True))
        self.notify = p.start()
        self.addCleanup(p.stop)

    def token(self, **over):
        data = {"v": 1, "name": "张三", "bot_id": "123456", "iat": int(time.time()),
                "exp": int(time.time()) + 86400, "max_vip": 2}
        data.update(over)
        raw = json.dumps(data, separators=(",", ":")).encode()
        return f"TGA2.{b64e(raw)}.{b64e(self.key.sign(raw))}"


class TestAuthRequired(unittest.TestCase):
    def test_every_license_and_vip_route_requires_login(self):
        routes = [r for r in web.app.routes if isinstance(r, APIRoute)
                  and (r.path.startswith("/api/license") or r.path.startswith("/api/vip"))]
        self.assertGreaterEqual(len(routes), 9)
        for r in routes:
            deps = [d.call for d in r.dependant.dependencies]
            self.assertIn(web.require_login, deps, f"{r.methods} {r.path} 没有登录保护")


class TestLicenseApi(WebVipBase):
    async def test_unlicensed_status(self):
        s = await web.get_license()
        self.assertEqual((s["enforced"], s["active"], s["bot_id"], s["has_token"]), (True, False, "123456", False))

    async def test_activate_and_remove(self):
        s = await web.set_license(web.LicenseRequest(token=self.token()))
        self.assertTrue(s["active"])
        self.assertEqual((s["name"], s["max_vip"]), ("张三", 2))
        s = await web.remove_license()
        self.assertFalse(s["active"])
        self.assertFalse(lic.key_path().exists())

    async def test_bad_token_is_rejected_and_not_saved(self):
        for bad in ("garbage", self.token(bot_id="999"), self.token(exp=int(time.time()) - 5)):
            with self.assertRaises(HTTPException) as cm:
                await web.set_license(web.LicenseRequest(token=bad))
            self.assertEqual(cm.exception.status_code, 400)
        self.assertFalse(lic.key_path().exists())

    async def test_invalid_stored_token_shows_the_reason(self):
        lic.save_token(self.token(exp=int(time.time()) - 5))
        s = await web.get_license()
        self.assertFalse(s["active"])
        self.assertIn("过期", s["error"])

    async def test_env_license_cannot_be_removed_from_the_web(self):
        settings.license_key = self.token()
        with self.assertRaises(HTTPException):
            await web.remove_license()

    async def test_not_enforced_edition(self):
        lic.PUBLIC_KEY_B64 = ""
        s = await web.get_license()
        self.assertEqual((s["enforced"], s["active"]), (False, True))


class TestVipApi(WebVipBase):
    async def _license(self):
        lic.save_token(self.token())

    async def test_grant_list_revoke(self):
        await self._license()
        r = await web.grant_vip(web.VipGrantRequest(user_id=5, days=30))
        self.assertAlmostEqual(r["expires_at"], time.time() + 30 * 86400, delta=5)
        data = await web.list_vips()
        self.assertEqual((data["system_active"], data["active_count"]), (True, 1))
        self.assertEqual(data["users"][0]["user_id"], 5)
        self.assertTrue(data["users"][0]["active"])
        self.assertTrue((await web.revoke_vip(5))["ok"])
        self.assertEqual((await web.list_vips())["users"], [])

    async def test_grant_notifies_the_user(self):
        await self._license()
        r = await web.grant_vip(web.VipGrantRequest(user_id=5, days=30))
        self.assertTrue(r["notified"])
        chat_id, text = self.notify.await_args.args
        self.assertEqual(chat_id, 5)
        self.assertIn("VIP", text)
        self.assertIn("/vip", text)

    async def test_failed_notification_does_not_undo_the_grant(self):
        await self._license()
        self.notify.return_value = False
        r = await web.grant_vip(web.VipGrantRequest(user_id=5, days=30))
        self.assertTrue(r["ok"])
        self.assertFalse(r["notified"])
        self.assertTrue(await vip.is_vip(self.repo, 5))

    async def test_refused_grants_send_nothing(self):
        # 没授权：被拒绝，当然不能通知用户"已开通"
        with self.assertRaises(HTTPException):
            await web.grant_vip(web.VipGrantRequest(user_id=5, days=30))
        self.notify.assert_not_awaited()

    async def test_permanent_grant_message(self):
        await self._license()
        await web.grant_vip(web.VipGrantRequest(user_id=5, days=0))
        self.assertIn("永久", self.notify.await_args.args[1])

    async def test_permanent(self):
        await self._license()
        r = await web.grant_vip(web.VipGrantRequest(user_id=5, days=0))
        self.assertIsNone(r["expires_at"])

    async def test_expired_vip_is_listed_as_inactive(self):
        await self._license()
        await self.repo.set_vip(5, time.time() - 10, granted_by=None)
        u = (await web.list_vips())["users"][0]
        self.assertFalse(u["active"])

    async def test_without_license_everything_is_refused(self):
        with self.assertRaises(HTTPException) as cm:
            await web.grant_vip(web.VipGrantRequest(user_id=5, days=30))
        self.assertEqual(cm.exception.status_code, 400)
        with self.assertRaises(HTTPException):
            await web.create_vip_codes(web.VipCodesRequest(days=30, count=1))
        self.assertFalse((await web.list_vips())["system_active"])

    async def test_license_vip_cap_is_enforced(self):
        await self._license()                                  # max_vip = 2
        await web.grant_vip(web.VipGrantRequest(user_id=5, days=30))
        await web.grant_vip(web.VipGrantRequest(user_id=6, days=30))
        with self.assertRaises(HTTPException) as cm:
            await web.grant_vip(web.VipGrantRequest(user_id=7, days=30))
        self.assertIn("上限", cm.exception.detail)

    async def test_bad_days(self):
        await self._license()
        with self.assertRaises(HTTPException):
            await web.grant_vip(web.VipGrantRequest(user_id=5, days=-1))

    async def test_codes_flow(self):
        await self._license()
        codes = (await web.create_vip_codes(web.VipCodesRequest(days=30, count=3)))["codes"]
        self.assertEqual(len(codes), 3)
        listed = await web.list_vip_codes()
        self.assertEqual({c["code"] for c in listed}, set(codes))
        self.assertTrue((await web.delete_vip_code(codes[0]))["ok"])
        self.assertEqual(len(await web.list_vip_codes()), 2)
        await vip.redeem(self.repo, 5, codes[1])               # 机器人那边兑换后，这里就不再列出
        self.assertEqual([c["code"] for c in await web.list_vip_codes()], [codes[2]])
        self.assertFalse((await web.delete_vip_code(codes[1]))["ok"])      # 用过的不能作废
        self.assertFalse((await web.delete_vip_code("garbage"))["ok"])

    async def test_code_batch_limit(self):
        await self._license()
        with self.assertRaises(HTTPException):
            await web.create_vip_codes(web.VipCodesRequest(days=30, count=21))


if __name__ == "__main__":
    unittest.main()
