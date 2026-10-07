"""Telegram 按钮交互：键盘布局、列表返回位置、删除确认流程、设置页权限。"""
import os
import tempfile
import unittest
from types import SimpleNamespace

from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, User

from bot.config import settings
from bot.core.keyboards import (
    BACK_TO_LIST_TEXT,
    limit_chooser_keyboard,
    main_inline_keyboard,
    task_delete_confirm_keyboard,
    task_keyboard,
    task_open_button,
)
from bot.core.list_view import render_task_list
from bot.db.repo import TaskRepo
from bot.handlers import callbacks, settings_menu
from bot.middlewares.auth import AdminMiddleware
from tests.fakes import FakeNodePool


def _callbacks(markup) -> list[str]:
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


def _texts(markup) -> list[str]:
    return [b.text for row in markup.inline_keyboard for b in row]


class TestMainKeyboard(unittest.TestCase):
    def test_settings_hidden_for_non_admin(self):
        self.assertIn("nav:settings", _callbacks(main_inline_keyboard({}, is_admin=True)))
        self.assertNotIn("nav:settings", _callbacks(main_inline_keyboard({}, is_admin=False)))

    def test_stats_and_refresh_always_present(self):
        cbs = _callbacks(main_inline_keyboard({}, is_admin=False))
        self.assertIn("stats:7", cbs)
        self.assertIn("nav:start", cbs)

    def test_failed_shortcut_only_when_there_are_failures(self):
        self.assertNotIn("list:FAILED:0", _callbacks(main_inline_keyboard({"FAILED": 0})))
        kb = main_inline_keyboard({"FAILED": 3})
        self.assertIn("list:FAILED:0", _callbacks(kb))
        self.assertIn("⚠️ 失败 3", _texts(kb))


class TestTaskKeyboard(unittest.TestCase):
    def test_detail_button_is_labelled_refresh(self):
        kb = task_keyboard("g1", "ACTIVE")
        labels = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
        self.assertEqual(labels["task:detail:g1"], "🔄 刷新")

    def test_select_files_hidden_only_when_known_single_file(self):
        self.assertIn("task:files:g1", _callbacks(task_keyboard("g1", "ACTIVE")))
        self.assertIn("task:files:g1", _callbacks(task_keyboard("g1", "ACTIVE", multi_file=True)))
        self.assertNotIn("task:files:g1", _callbacks(task_keyboard("g1", "ACTIVE", multi_file=False)))
        self.assertNotIn("task:files:g1", _callbacks(task_keyboard("g1", "PAUSED", multi_file=False)))

    def test_completed_with_gofile_link_gets_url_button(self):
        kb = task_keyboard("g1", "COMPLETED", link="https://gofile.io/d/abc")
        urls = [b.url for row in kb.inline_keyboard for b in row if b.url]
        self.assertEqual(urls, ["https://gofile.io/d/abc"])
        self.assertNotIn("task:link:g1", _callbacks(kb))  # 跟 URL 按钮重复，去掉了

    def test_non_http_link_is_not_rendered_as_url(self):
        kb = task_keyboard("g1", "COMPLETED", link="/downloads/x")
        self.assertFalse(any(b.url for row in kb.inline_keyboard for b in row))

    def test_cancelled_can_be_redownloaded(self):
        self.assertIn("task:retry:g1", _callbacks(task_keyboard("g1", "CANCELLED")))

    def test_back_target_is_preserved(self):
        kb = task_keyboard("g1", "PAUSED", back="list:PAUSED:2")
        last = kb.inline_keyboard[-1][0]
        self.assertEqual((last.text, last.callback_data), (BACK_TO_LIST_TEXT, "list:PAUSED:2"))

    def test_open_button_carries_list_position_and_status(self):
        btn = task_open_button(3, "g1", "movie.mkv", status="FAILED", back="FAILED:1")[0]
        self.assertEqual(btn.callback_data, "topen:FAILED:1:g1")
        self.assertTrue(btn.text.startswith("⚠️ 3."))
        # 不传 back 时退回旧的 task:open（搜索结果用）
        self.assertEqual(task_open_button(1, "g1", "x")[0].callback_data, "task:open:g1")

    def test_open_button_fits_callback_data_limit(self):
        btn = task_open_button(99, "0123456789abcdef", "x", back="COMPLETED:999")[0]
        self.assertLessEqual(len(btn.callback_data.encode()), 64)

    def test_delete_confirm_offers_file_deletion_only_when_allowed(self):
        self.assertNotIn("task:confirm_purge:g1", _callbacks(task_delete_confirm_keyboard("g1")))
        self.assertIn("task:confirm_purge:g1", _callbacks(task_delete_confirm_keyboard("g1", can_delete_files=True)))
        self.assertIn("task:purge:g1", _callbacks(task_delete_confirm_keyboard("g1", destructive=True)))


class TestLimitChooser(unittest.TestCase):
    def test_marks_current_from_aria2_byte_value(self):
        # aria2 回的是字节数，要能对上 "2M" 这个预设
        kb = limit_chooser_keyboard(str(2 * 1024 * 1024))
        labels = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
        self.assertEqual(labels["setlimit:2M"], "·2 MiB/s·")
        self.assertEqual(labels["setlimit:5M"], "5 MiB/s")

    def test_unlimited_marked_for_zero(self):
        labels = _texts(limit_chooser_keyboard("0"))
        self.assertIn("·🚫 不限速·", labels)


class TestListViewButtons(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.nodes = FakeNodePool()

    async def asyncTearDown(self):
        await self.repo.close()
        self._dir.cleanup()

    async def _add(self, gid, status, user_id=1):
        await self.repo.create_task(
            gid=gid, user_id=user_id, chat_id=1, reply_message_id=None,
            source_type="url", source_ref=gid, file_name=f"{gid}.bin", file_size=1, payload="https://x/y",
        )
        await self.repo.update_status(gid, status)

    async def test_bulk_buttons_match_tab(self):
        await self._add("a", "ACTIVE")
        await self._add("p", "PAUSED")
        _, active = await render_task_list(self.repo, self.nodes, "ACTIVE", 0)
        self.assertIn("bulk:pause:ACTIVE", _callbacks(active))
        self.assertNotIn("bulk:resume:PAUSED", _callbacks(active))
        _, paused = await render_task_list(self.repo, self.nodes, "PAUSED", 0)
        self.assertIn("bulk:resume:PAUSED", _callbacks(paused))
        self.assertNotIn("bulk:pause:ACTIVE", _callbacks(paused))

    async def test_rows_open_with_list_position(self):
        await self._add("a", "ACTIVE")
        _, markup = await render_task_list(self.repo, self.nodes, "ACTIVE", 0)
        self.assertIn("topen:ACTIVE:0:a", _callbacks(markup))
        self.assertIn("list:ACTIVE:0", _callbacks(markup))  # 刷新按钮

    async def test_cleanup_button_admin_only(self):
        await self._add("c", "COMPLETED")
        _, admin_view = await render_task_list(self.repo, self.nodes, "ALL", 0, user_id=None)
        self.assertIn("list:cleanup:0", _callbacks(admin_view))
        _, user_view = await render_task_list(self.repo, self.nodes, "ALL", 0, user_id=1)
        self.assertNotIn("list:cleanup:0", _callbacks(user_view))

    async def test_unknown_tab_falls_back_to_all(self):
        await self._add("a", "ACTIVE")
        text, _ = await render_task_list(self.repo, self.nodes, "BOGUS", 0)
        self.assertIn("全部任务", text)


# ---------------------------------------------------------------- 回调处理


class FakeMessage:
    def __init__(self, reply_markup=None):
        self.message_id = 100
        self.reply_markup = reply_markup
        self.edits: list[tuple[str, InlineKeyboardMarkup | None]] = []
        self.deleted = False

    async def edit_text(self, text, reply_markup=None, **_):
        self.edits.append((text, reply_markup))
        self.reply_markup = reply_markup

    async def answer(self, text, reply_markup=None, **_):
        self.edits.append((text, reply_markup))

    async def delete(self):
        self.deleted = True


class FakeQuery:
    def __init__(self, data, *, user_id=1, reply_markup=None):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeMessage(reply_markup)
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False, **_):
        self.answers.append((text, show_alert))


class TestTaskCallbacks(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dl = os.path.join(self._dir.name, "downloads")
        os.makedirs(self.dl)
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.nodes = FakeNodePool(download_dir=self.dl)
        self._orig = (settings.allowed_user_ids, settings.admin_user_ids, settings.download_dir)
        settings.allowed_user_ids = "1"
        settings.admin_user_ids = "1"
        settings.download_dir = self.dl

    async def asyncTearDown(self):
        settings.allowed_user_ids, settings.admin_user_ids, settings.download_dir = self._orig
        await self.repo.close()
        self._dir.cleanup()

    async def _completed(self, gid, save_path):
        await self.repo.create_task(
            gid=gid, user_id=1, chat_id=1, reply_message_id=None,
            source_type="url", source_ref=gid, file_name=os.path.basename(save_path), file_size=1,
            payload="https://x/y",
        )
        await self.repo.update_status(gid, "COMPLETED", save_path=save_path)

    async def _run(self, data, *, back=None):
        markup = None
        if back:
            markup = InlineKeyboardMarkup(inline_keyboard=[[
                InlineKeyboardButton(text=BACK_TO_LIST_TEXT, callback_data=back),
            ]])
        q = FakeQuery(data, reply_markup=markup)
        if data.startswith("topen:"):
            await callbacks.open_from_list(q, self.repo, self.nodes, task_manager=None)
        else:
            await callbacks.handle_task_action(q, self.repo, self.nodes, task_manager=None)
        return q

    async def test_open_from_list_keeps_tab_and_page(self):
        f = os.path.join(self.dl, "a.bin")
        open(f, "w").close()
        await self._completed("g1", f)
        q = await self._run("topen:COMPLETED:2:g1")
        _, markup = q.message.edits[-1]
        self.assertIn("list:COMPLETED:2", _callbacks(markup))

    async def test_back_button_survives_submenus(self):
        f = os.path.join(self.dl, "a.bin")
        open(f, "w").close()
        await self._completed("g1", f)
        q = await self._run("task:delete:g1", back="list:COMPLETED:2")
        _, markup = q.message.edits[-1]
        self.assertIn("list:COMPLETED:2", _callbacks(markup))
        self.assertIn("task:confirm_purge:g1", _callbacks(markup))

    async def test_delete_record_keeps_file_and_returns_to_list(self):
        f = os.path.join(self.dl, "a.bin")
        open(f, "w").close()
        await self._completed("g1", f)
        q = await self._run("task:delete_record:g1", back="list:COMPLETED:0")
        self.assertIsNone(await self.repo.get_by_gid("g1"))
        self.assertTrue(os.path.exists(f))
        self.assertFalse(q.message.deleted)
        text, _ = q.message.edits[-1]
        self.assertIn("已完成", text)  # 回到了「已完成」列表

    async def test_delete_without_list_context_deletes_message(self):
        f = os.path.join(self.dl, "a.bin")
        open(f, "w").close()
        await self._completed("g1", f)
        q = await self._run("task:delete_record:g1")
        self.assertTrue(q.message.deleted)

    async def test_purge_removes_file_inside_download_dir(self):
        d = os.path.join(self.dl, "show")
        os.makedirs(d)
        open(os.path.join(d, "ep1.mkv"), "w").close()
        await self._completed("g1", d)
        await self._run("task:purge:g1")
        self.assertFalse(os.path.exists(d))
        self.assertIsNone(await self.repo.get_by_gid("g1"))

    async def test_purge_refuses_paths_outside_download_dir(self):
        outside = os.path.join(self._dir.name, "precious.txt")
        open(outside, "w").close()
        await self._completed("g1", outside)
        q = await self._run("task:delete:g1")
        _, markup = q.message.edits[-1]
        self.assertNotIn("task:confirm_purge:g1", _callbacks(markup))
        # 伪造的 purge 回调同样不能删到下载目录之外
        await self._run("task:purge:g1")
        self.assertTrue(os.path.exists(outside))

    async def test_purge_refuses_download_dir_itself(self):
        await self._completed("g1", self.dl)
        await self._run("task:purge:g1")
        self.assertTrue(os.path.isdir(self.dl))


class TestSettingsAreAdminOnly(unittest.IsolatedAsyncioTestCase):
    def _query(self, data):
        return CallbackQuery(
            id="1", from_user=User(id=2, is_bot=False, first_name="u"), chat_instance="c", data=data,
        )

    async def _matches(self, router, data) -> bool:
        for handler in router.callback_query.handlers:
            ok, _ = await handler.check(self._query(data))
            if ok:
                return True
        return False

    async def test_settings_callbacks_live_in_admin_router(self):
        for data in ("nav:settings", "settings:limit", "setlimit:2M", "setconcurrent:3",
                     "setmaxsize:0", "setcleanup:7", "setdir:0", "settings:notify", "settings:sendtg"):
            self.assertTrue(await self._matches(settings_menu.router, data), data)
            self.assertFalse(await self._matches(callbacks.router, data), data)

    def test_settings_router_is_gated_by_admin_middleware(self):
        self.assertTrue(any(isinstance(m, AdminMiddleware) for m in settings_menu.router.callback_query.middleware))


if __name__ == "__main__":
    unittest.main()
