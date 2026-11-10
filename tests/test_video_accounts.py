"""Saved-account presentation, identity selection and destructive-action guards."""

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from bot.config import settings
from bot.db.repo import TaskRepo
from bot.handlers.video import account_action
from bot.video.account_views import overview, platform_view


class TestSavedVideoAccounts(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(str(Path(self.temp.name) / "tasks.db"))
        await self.repo.connect()
        await self.repo.video_put_account("bilibili", 11, "<personal-11>", b"personal-secret")
        await self.repo.video_put_account("bilibili", 22, "other-personal", b"other-secret")
        await self.repo.video_put_account("bilibili", 0, "shared-id", b"shared-secret")

    async def asyncTearDown(self):
        await self.repo.close()
        self.temp.cleanup()

    def query(self, action, user=11):
        return SimpleNamespace(
            data=f"vacc:{action}:bilibili",
            from_user=SimpleNamespace(id=user),
            message=SimpleNamespace(
                chat=SimpleNamespace(type="private", id=11),
                message_id=99,
                text="menu",
                reply_markup=None,
                answer=AsyncMock(),
                edit_text=AsyncMock(),
            ),
            answer=AsyncMock(),
        )

    async def test_overview_lists_only_own_personal_accounts(self):
        text, keyboard = await overview(self.repo, 11, False)
        self.assertIn("&lt;personal-11&gt;", text)
        self.assertIn("北京时间", text)
        self.assertNotIn("other-personal", text)
        self.assertNotIn("personal-secret", text)
        self.assertNotIn("shared-id", text)  # Closed shared identity stays private.
        self.assertIn("vacc:platform:bilibili", [b.callback_data for row in keyboard.inline_keyboard for b in row])

    async def test_admin_sees_shared_identity_and_management(self):
        text, keyboard = await platform_view(self.repo, 11, "bilibili", True)
        self.assertIn("shared-id", text)
        self.assertIn("最近登录", text)
        callbacks = [b.callback_data for row in keyboard.inline_keyboard for b in row]
        self.assertIn("vacc:remove-shared:bilibili", callbacks)
        self.assertIn("vacc:share:bilibili", callbacks)
        self.assertNotIn("other-personal", text)

    async def test_remove_requires_confirmation_and_only_removes_own_account(self):
        with patch.object(settings, "admin_user_ids", "99"), patch.object(settings, "allowed_user_ids", "99"):
            query = self.query("remove")
            await account_action(query, self.repo)
            self.assertIsNotNone(await self.repo.video_account("bilibili", 11))
            confirm = self.query("remove-confirmed")
            await account_action(confirm, self.repo)
            self.assertIsNone(await self.repo.video_account("bilibili", 11))
            self.assertIsNotNone(await self.repo.video_account("bilibili", 22))

    async def test_non_admin_cannot_forge_shared_confirmation(self):
        with patch.object(settings, "admin_user_ids", "99"), patch.object(settings, "allowed_user_ids", "99"):
            query = self.query("remove-shared-confirmed")
            await account_action(query, self.repo)
        self.assertIsNotNone(await self.repo.video_account("bilibili", 0))
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])

    async def test_user_can_select_only_available_account_modes(self):
        query = self.query("use-shared")
        await account_action(query, self.repo)
        self.assertEqual(await self.repo.video_preference(11, "bilibili"), "personal")
        await self.repo.video_share("bilibili", True)
        await account_action(query, self.repo)
        self.assertEqual(await self.repo.video_preference(11, "bilibili"), "shared")
        await account_action(self.query("use-personal"), self.repo)
        self.assertEqual(await self.repo.video_preference(11, "bilibili"), "personal")

    async def test_group_cannot_show_account_details(self):
        query = self.query("platform")
        query.message.chat.type = "group"
        await account_action(query, self.repo)
        query.message.answer.assert_not_awaited()
        self.assertTrue(query.answer.await_args.kwargs["show_alert"])
