"""Menu navigation should preserve context and not create chat spam."""

import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from aiogram.exceptions import TelegramBadRequest
from aiogram.methods import EditMessageText
from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.db.repo import TaskRepo
from bot.handlers.menu import edit_menu, single_menu_action
from bot.handlers.video import account_action, video_preview_action


def query(data, markup=None):
    return SimpleNamespace(
        data=data,
        from_user=SimpleNamespace(id=11),
        answer=AsyncMock(),
        message=SimpleNamespace(
            text="menu",
            message_id=8,
            reply_markup=markup,
            chat=SimpleNamespace(id=11, type="private"),
            edit_text=AsyncMock(),
            answer=AsyncMock(),
        ),
    )


class TestMenuHelpers(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_content_does_not_send_another_message(self):
        q = query("vacc:menu")
        q.message.edit_text.side_effect = TelegramBadRequest(
            method=EditMessageText(chat_id=11, message_id=8, text="menu"),
            message="Bad Request: message is not modified",
        )
        await edit_menu(q, "menu")
        q.message.answer.assert_not_awaited()

    async def test_photo_navigation_falls_back_to_text(self):
        q = query("vacc:menu")
        q.message.text = None
        q.message.edit_text.side_effect = TelegramBadRequest(
            method=EditMessageText(chat_id=11, message_id=8, text="menu"),
            message="There is no text in the message to edit",
        )
        await edit_menu(q, "menu")
        q.message.answer.assert_awaited_once()

    async def test_overlap_is_rejected_and_lock_released_after_error(self):
        entered = asyncio.Event()
        release = asyncio.Event()
        calls = []

        @single_menu_action
        async def handler(q):
            calls.append(1)
            entered.set()
            await release.wait()
            raise ValueError("failed")

        first = query("x")
        task = asyncio.create_task(handler(first))
        await entered.wait()
        second = query("x")
        await handler(second)
        self.assertEqual(len(calls), 1)
        second.answer.assert_awaited_once_with("正在处理，请稍候")
        release.set()
        with self.assertRaises(ValueError):
            await task
        with self.assertRaises(ValueError):
            await handler(query("x"))
        self.assertEqual(len(calls), 2)


class TestAccountNavigation(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(str(Path(self.temp.name) / "tasks.db"))
        await self.repo.connect()
        self.token = await self.repo.video_save_preview(
            user_id=11,
            chat_id=11,
            url="https://example.com/v",
            parser="yt-dlp",
            quality="1080",
            account_mode="anonymous",
            info={"title": "Video", "qualities": ["best", "1080"]},
        )

    async def asyncTearDown(self):
        await self.repo.close()
        self.temp.cleanup()

    async def test_accounts_open_in_place_with_return_to_preview(self):
        q = query("vacc:menu:" + self.token)
        await account_action(q, self.repo)
        q.message.answer.assert_not_awaited()
        q.answer.assert_awaited_once()
        markup = q.message.edit_text.await_args.kwargs["reply_markup"]
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("vp:back:" + self.token, callbacks)
        self.assertIsNotNone(await self.repo.video_preview(self.token, 11))

    async def test_platform_navigation_retains_preview_return(self):
        markup = InlineKeyboardMarkup(
            inline_keyboard=[[InlineKeyboardButton(text="返回", callback_data="vp:back:" + self.token)]]
        )
        q = query("vacc:platform:bilibili", markup)
        await account_action(q, self.repo)
        callbacks = [
            b.callback_data
            for row in q.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
            for b in row
        ]
        self.assertIn("vp:back:" + self.token, callbacks)
        q.message.answer.assert_not_awaited()

    async def test_selected_parser_does_not_reset_quality_or_probe_again(self):
        q = query("vp:p:" + self.token + ":yt-dlp")
        with patch("bot.handlers.video.make_preview", AsyncMock()) as probe:
            await video_preview_action(q, self.repo, None)
        probe.assert_not_awaited()
        q.answer.assert_awaited_once_with("当前已选中")
        self.assertEqual((await self.repo.video_preview(self.token, 11))["quality"], "1080")

    async def test_back_restores_confirmation_without_reparsing(self):
        q = query("vp:back:" + self.token)
        with patch("bot.handlers.video.make_preview", AsyncMock()) as probe:
            await video_preview_action(q, self.repo, None)
        probe.assert_not_awaited()
        self.assertIn("Video", q.message.edit_text.await_args.args[0])
        self.assertIn(
            "vp:start:" + self.token,
            [
                b.callback_data
                for row in q.message.edit_text.await_args.kwargs["reply_markup"].inline_keyboard
                for b in row
            ],
        )

    async def test_video_confirmation_keeps_list_location(self):
        from bot.handlers.video_tasks import handle_video_task
        from bot.video.ui import video_task_keyboard

        row = await self.repo.video_create_task(self.token, 11, 8)
        q = query("task:cancel:" + row["gid"])
        await handle_video_task(q, self.repo, None, row, "cancel", "list:ACTIVE:2")
        job = await self.repo.video_job(row["id"])
        self.assertEqual(job["back"], "list:ACTIVE:2")
        markup = q.message.edit_text.await_args.kwargs["reply_markup"]
        self.assertIn("list:ACTIVE:2", [b.callback_data for r in markup.inline_keyboard for b in r])
        refresh = video_task_keyboard(row["gid"], "ACTIVE", job["back"])
        self.assertIn("list:ACTIVE:2", [b.callback_data for r in refresh.inline_keyboard for b in r])

    async def test_completion_notification_keeps_list_location(self):
        from bot.core.task_manager import TaskManager

        row = await self.repo.video_create_task(self.token, 11, 8)
        await self.repo.video_update(row["id"], back="list:ALL:3")
        bot = AsyncMock()
        manager = TaskManager(bot, None, self.repo)
        await manager._notify(row, "done", gid=row["gid"], status="COMPLETED")
        markup = bot.edit_message_text.await_args.kwargs["reply_markup"]
        self.assertIn("list:ALL:3", [b.callback_data for r in markup.inline_keyboard for b in r])

    async def test_existing_video_table_is_migrated(self):
        import sqlite3

        db_path = str(Path(self.temp.name) / "tasks.db")
        await self.repo.close()
        with sqlite3.connect(db_path) as connection:
            connection.execute("ALTER TABLE video_jobs DROP COLUMN back")
        await self.repo.connect()
        row = await self.repo.video_create_task(self.token, 11, 8)
        await self.repo.video_update(row["id"], back="list:ALL:1")
        self.assertEqual((await self.repo.video_job(row["id"]))["back"], "list:ALL:1")
