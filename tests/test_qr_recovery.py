"""Safe QR failure classification and cancellation/recovery behavior."""

import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from bot.db.repo import TaskRepo
from bot.video.accounts import CredentialVault
from bot.video.login_worker import LoginWorker
from bot.video.qr_browser import QRLoginError, failure_reason
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
