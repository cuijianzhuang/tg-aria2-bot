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


class CallbackTestBase(unittest.IsolatedAsyncioTestCase):
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



class TestTaskCallbacks(CallbackTestBase):
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


class RecordingTM:
    """只记录 watch/hold 调用的 TaskManager 替身。"""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []

    def watch(self, gid):
        self.calls.append(("watch", gid))

    def hold(self, gid):
        self.calls.append(("hold", gid))

    def pending_usage(self, user_id):
        return 0


class TestAttendance(CallbackTestBase):
    """按钮操作告诉 TaskManager 用户在看哪张卡片：普通操作 → 快速刷新，
    打开子菜单 → 暂停自动刷新（不然 2 秒一次的刷新会把菜单冲掉）。"""

    async def _attend_run(self, data, **kw):
        f = os.path.join(self.dl, "a.bin")
        open(f, "w").close()
        if await self.repo.get_by_gid("g1") is None:
            await self._completed("g1", f)
        self.tm = RecordingTM()
        q = FakeQuery(data)
        await callbacks.handle_task_action(q, self.repo, self.nodes, self.tm)
        return q

    async def test_refresh_button_watches(self):
        await self._attend_run("task:detail:g1")
        self.assertEqual(self.tm.calls, [("watch", "g1")])

    async def test_submenus_hold(self):
        for action in ("cancel", "delete", "limit", "files", "confirm_delete_files", "confirm_purge"):
            await self._attend_run(f"task:{action}:g1")
            self.assertEqual(self.tm.calls[0], ("hold", "g1"), action)

    async def test_back_to_card_releases_hold(self):
        await self._attend_run("task:delete:g1")
        await self._attend_run("task:detail:g1")
        self.assertEqual(self.tm.calls[-1], ("watch", "g1"))

    async def test_unauthorized_user_does_not_trigger_fast_refresh(self):
        await self._attend_run("task:detail:g1")  # 先建任务
        self.tm = RecordingTM()
        settings.allowed_user_ids = "1"
        settings.admin_user_ids = "1"
        q = FakeQuery("task:pause:g1", user_id=99)
        await callbacks.handle_task_action(q, self.repo, self.nodes, self.tm)
        self.assertEqual(self.tm.calls, [])

    async def test_new_pending_task_is_watched(self):
        token = await self.repo.create_pending(
            kind="url", user_id=1, chat_id=1, source_ref="https://x/y.bin",
            file_name="y.bin", file_size=1, payload="https://x/y.bin",
        )
        self.tm = RecordingTM()
        q = FakeQuery(f"pending:start:{token}")
        await callbacks.handle_pending(q, self.repo, self.nodes, self.tm)
        self.assertEqual(self.tm.calls, [("watch", "gid-uri")])


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


class TestQuotaAtTheDoor(CallbackTestBase):
    """额度在添加/重试/恢复任务的按钮入口生效：被拒时给出原因，且确认卡片保留。"""

    GIB = 1024**3

    def setUp(self):
        from bot.core import quota as q
        self._quota_orig = (settings.user_max_active_tasks, settings.user_max_task_size_gb, settings.user_daily_quota_gb)
        self.quota = q

    def tearDown(self):
        settings.user_max_active_tasks, settings.user_max_task_size_gb, settings.user_daily_quota_gb = self._quota_orig

    async def _task(self, gid, status, user_id=2):
        await self.repo.create_task(
            gid=gid, user_id=user_id, chat_id=1, reply_message_id=None, source_type="url",
            source_ref=gid, file_name="f", file_size=None, payload="https://x/y.bin",
        )
        await self.repo.update_status(gid, status)

    async def _pending(self, user_id=2, size=None, batch_id=None):
        return await self.repo.create_pending(
            kind="url", user_id=user_id, chat_id=1, source_ref=f"r{os.urandom(2).hex()}",
            file_name="y.bin", file_size=size, payload="https://x/y.bin", batch_id=batch_id,
        )

    async def _start(self, token, user_id=2):
        q = FakeQuery(f"pending:start:{token}", user_id=user_id)
        await callbacks.handle_pending(q, self.repo, self.nodes, RecordingTM())
        return q

    async def test_start_refused_when_too_many_active_tasks_and_card_is_kept(self):
        settings.user_max_active_tasks = 1
        await self._task("g0", "ACTIVE")
        token = await self._pending()
        q = await self._start(token)
        text, alert = q.answers[-1]
        self.assertTrue(alert)
        self.assertIn("最多同时进行 1 个任务", text)
        self.assertIsNotNone(await self.repo.get_pending(token))       # 卡片还在，可以稍后再点
        self.assertEqual(self.nodes.get("default").add_uri_calls, [])  # 没有真的去 aria2 添加

    async def test_start_refused_when_file_is_too_big(self):
        settings.user_max_task_size_gb = 1
        token = await self._pending(size=3 * self.GIB)
        q = await self._start(token)
        self.assertIn("单任务上限", q.answers[-1][0])
        self.assertIsNotNone(await self.repo.get_pending(token))

    async def test_start_refused_when_daily_quota_is_used_up(self):
        settings.user_daily_quota_gb = 1
        await self.repo.add_usage(2, self.quota.today(), 1 * self.GIB)
        token = await self._pending()
        q = await self._start(token)
        self.assertIn("今天的下载额度已用完", q.answers[-1][0])

    async def test_start_allowed_within_limits(self):
        settings.user_max_active_tasks = 2
        await self._task("g0", "ACTIVE")
        token = await self._pending()
        await self._start(token)
        self.assertIsNone(await self.repo.get_pending(token))
        self.assertEqual(len(self.nodes.get("default").add_uri_calls), 1)

    async def test_admin_is_not_limited(self):
        settings.user_max_active_tasks = 1
        await self._task("g0", "ACTIVE", user_id=1)
        token = await self._pending(user_id=1)
        await self._start(token, user_id=1)
        self.assertEqual(len(self.nodes.get("default").add_uri_calls), 1)

    async def test_retry_is_refused_over_quota(self):
        settings.user_max_active_tasks = 1
        await self._task("g0", "ACTIVE")
        await self._task("g1", "FAILED")
        q = FakeQuery("task:retry:g1", user_id=2)
        await callbacks.handle_task_action(q, self.repo, self.nodes, RecordingTM())
        self.assertIn("最多同时进行", q.answers[-1][0])
        self.assertEqual(self.nodes.get("default").add_uri_calls, [])

    async def test_resume_is_refused_only_when_todays_quota_is_gone(self):
        settings.user_max_active_tasks = 1      # 它自己占着唯一的名额——恢复不应该因此被拒
        settings.user_daily_quota_gb = 1
        await self._task("g1", "PAUSED")
        q = FakeQuery("task:resume:g1", user_id=2)
        await callbacks.handle_task_action(q, self.repo, self.nodes, RecordingTM())
        self.assertEqual(self.nodes.get("default").resumed, ["g1"])

        await self._task("g2", "PAUSED")
        await self.repo.add_usage(2, self.quota.today(), 1 * self.GIB)
        q = FakeQuery("task:resume:g2", user_id=2)
        await callbacks.handle_task_action(q, self.repo, self.nodes, RecordingTM())
        self.assertIn("额度已用完", q.answers[-1][0])
        self.assertEqual(self.nodes.get("default").resumed, ["g1"])    # g2 没有被恢复

    async def test_batch_start_stops_at_the_active_limit_and_says_why(self):
        settings.user_max_active_tasks = 2
        fake = self.nodes.get("default")
        counter = iter(range(100))

        async def add_uri(uri, *, out=None, download_dir=None):   # 每次返回不同的 gid（默认替身是固定值）
            fake.add_uri_calls.append((uri, out, download_dir))
            return f"batch-gid-{next(counter)}"

        fake.add_uri = add_uri
        for _ in range(4):
            await self._pending(batch_id="b1")
        q = FakeQuery("pending:startall:b1", user_id=2)
        await callbacks.handle_pending(q, self.repo, self.nodes, RecordingTM())
        self.assertEqual(len(self.nodes.get("default").add_uri_calls), 2)
        text = q.message.edits[-1][0]
        self.assertIn("成功启动 2 个", text)
        self.assertIn("跳过 2 个", text)
        self.assertIn("最多同时进行 2 个任务", text)

    async def test_bulk_resume_skips_users_whose_quota_is_gone(self):
        settings.user_daily_quota_gb = 1
        await self.repo.add_usage(2, self.quota.today(), 1 * self.GIB)
        await self._task("g1", "PAUSED", user_id=2)
        await self._task("g2", "PAUSED", user_id=1)      # 管理员的不受限
        q = FakeQuery("bulk:resume:PAUSED", user_id=1)   # 管理员点"全部继续"
        await callbacks.bulk_action(q, self.repo, self.nodes, RecordingTM())
        self.assertEqual(self.nodes.get("default").resumed, ["g2"])


if __name__ == "__main__":
    unittest.main()

