"""Safe QR failure classification and cancellation/recovery behavior."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from bot.db.repo import TaskRepo
from bot.video.accounts import CredentialVault
from bot.video.login_worker import LoginWorker
from bot.video.qr_browser import QRLoginError, failure_reason, verification_required, visible_locator
from bot.web.video import public_status


class TestQRFailureReasons(unittest.TestCase):
    def test_dependency_failures_have_specific_safe_codes(self):
        self.assertEqual(failure_reason(ImportError("private-token"), "import"), "browser_missing")
        self.assertEqual(
            failure_reason(RuntimeError("Executable doesn't exist /private-token"), "launch"), "browser_missing"
        )
        self.assertEqual(
            failure_reason(
                RuntimeError("error while loading shared libraries: libatomic.so.1 private-token"), "launch"
            ),
            "browser_dependency",
        )
        self.assertEqual(failure_reason(TimeoutError("private-token"), "navigation"), "network_timeout")
        self.assertEqual(failure_reason(QRLoginError("qr_missing"), "discovery"), "qr_missing")


class TestQRRecovery(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(str(Path(self.directory.name) / "tasks.db"))
        await self.repo.connect()
        self.worker = LoginWorker(self.repo, CredentialVault(self.directory.name))
        self.login_id = await self.repo.video_new_login("xhs", 11, 11)

    async def asyncTearDown(self):
        await self.repo.close()
        self.directory.cleanup()

    async def test_failure_reason_is_persisted_without_raw_secrets(self):
        self.worker.browser_login = AsyncMock(side_effect=RuntimeError("Executable doesn't exist /private-token"))
        with self.assertLogs("bot.video.login_worker", level="WARNING") as logs:
            await self.worker.execute(await self.repo.video_login(self.login_id))
        row = await self.repo.video_login(self.login_id)
        self.assertEqual(row["state"], "failed")
        self.assertEqual(row["failure_reason"], "browser_missing")
        self.assertIn("未安装", public_status(row)["message"])
        self.assertNotIn("private-token", str(dict(row)))
        self.assertIn("browser_missing", str(logs.output))
        self.assertNotIn("private-token", str(logs.output))

    async def test_cancellation_is_not_replaced_by_late_browser_failure(self):
        async def cancelled(row):
            await self.repo.video_login_update(row["id"], state="cancelled")
            raise QRLoginError("qr_missing")

        self.worker.browser_login = cancelled
        await self.worker.execute(await self.repo.video_login(self.login_id))
        self.assertEqual((await self.repo.video_login(self.login_id))["state"], "cancelled")

    async def test_refresh_replaces_a_preparing_session(self):
        await self.repo.video_login_update(self.login_id, state="preparing")
        await self.repo.video_new_login("xhs", 11, 11)
        self.assertEqual((await self.repo.video_login(self.login_id))["state"], "cancelled")


@unittest.skipUnless(os.environ.get("RUN_VIDEO_BROWSER_TESTS") == "1", "opt-in Chromium regression")
class TestBrowserVerification(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright

        self.playwright = await async_playwright().start()
        self.addAsyncCleanup(self.playwright.stop)
        self.browser = await self.playwright.chromium.launch(headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.page = await self.browser.new_page()

    async def test_visible_captcha_container_is_not_a_verification_prompt(self):
        await self.page.set_content('<div class="captcha">普通登录组件</div>')
        self.assertFalse(await verification_required(self.page, confirmation_delay=0))

    async def test_hidden_prompt_does_not_require_verification(self):
        await self.page.set_content('<div style="display:none">请完成验证</div>')
        self.assertFalse(await verification_required(self.page, confirmation_delay=0))

    async def test_transient_prompt_is_allowed_to_disappear(self):
        await self.page.set_content('<div id="challenge">请完成验证</div>')
        await self.page.evaluate("setTimeout(() => document.querySelector('#challenge').remove(), 100)")
        self.assertFalse(await verification_required(self.page, confirmation_delay=0.3))

    async def test_persistent_prompt_requires_verification(self):
        await self.page.set_content("<div>请拖动滑块完成拼图</div>")
        self.assertTrue(await verification_required(self.page, confirmation_delay=0.02))

    async def test_visible_prompt_inside_iframe_requires_verification(self):
        await self.page.set_content('<iframe srcdoc="<div>请完成验证</div>"></iframe>')
        await self.page.frames[1].wait_for_selector("text=请完成验证")
        self.assertTrue(await verification_required(self.page, confirmation_delay=0.02))

    async def test_hidden_first_qr_does_not_hide_visible_qr(self):
        await self.page.set_content(
            '<div class="qrcode" style="display:none"></div>'
            '<div class="qrcode" id="visible-qr" style="width:180px;height:180px"></div>'
        )
        qr = await visible_locator(self.page, [".qrcode"], minimum_size=60)
        self.assertIsNotNone(qr)
        self.assertEqual(await qr.get_attribute("id"), "visible-qr")
