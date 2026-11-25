"""_task_action 分发特征测试（第 4 批拆分后的回归网）。

_task_action 已按 action 拆为子函数 + 分发表，本文件逐个 action 前缀断言
拆分后的走向与拆分前一致：已有专门覆盖的（detail/delete/purge/delete_record
等，见 test_buttons）不重复，只补此前没有走向断言的前缀。
"""
import os
import tempfile
import unittest
from types import SimpleNamespace

from aiogram.types import InlineKeyboardMarkup

from bot.config import settings
from bot.db.repo import TaskRepo
from bot.handlers import callbacks
from tests.fakes import FakeNodePool


def _cb(markup) -> list[str]:
    if markup is None:
        return []
    return [b.callback_data for row in markup.inline_keyboard for b in row if b.callback_data]


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
    def __init__(self, data, *, user_id=1):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = FakeMessage()
        self.answers: list[tuple[str | None, bool]] = []

    async def answer(self, text=None, show_alert=False, **_):
        self.answers.append((text, show_alert))


class StubTM:
    """sendtg 走向需要的最小 TaskManager 替身。"""

    def __init__(self, send_result=(True, "")):
        self.send_result = send_result
        self.sent: list[tuple[str, str]] = []

    def watch(self, gid):
        pass

    def hold(self, gid):
        pass

    def pending_usage(self, user_id):
        return 0

    async def send_file_to_tg(self, row, gid):
        self.sent.append((row["gid"], gid))
        return self.send_result


class TestTaskActionDispatch(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.dl = os.path.join(self._dir.name, "downloads")
        os.makedirs(self.dl)
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.nodes = FakeNodePool(download_dir=self.dl)
        self.client = self.nodes.get("default")
        self._orig = (settings.allowed_user_ids, settings.admin_user_ids, settings.download_dir)
        settings.allowed_user_ids = "1"
        settings.admin_user_ids = "1"
        settings.download_dir = self.dl

    async def asyncTearDown(self):
        settings.allowed_user_ids, settings.admin_user_ids, settings.download_dir = self._orig
        await self.repo.close()
        self._dir.cleanup()

    async def _add(self, gid="g1", status="ACTIVE", **kw):
        await self.repo.create_task(
            gid=gid, user_id=1, chat_id=1, reply_message_id=None,
            source_type="url", source_ref=gid, file_name=f"{gid}.bin", file_size=1,
            payload="https://x/y", **kw,
        )
        await self.repo.update_status(gid, status)

    async def _run(self, data, task_manager=None):
        q = FakeQuery(data)
        await callbacks.handle_task_action(q, self.repo, self.nodes, task_manager)
        return q

    async def test_missing_task_answers_not_found(self):
        q = await self._run("task:detail:nope")
        self.assertEqual(q.answers, [("任务不存在（可能已被删除）", True)])

    async def test_pause_goes_through_apply_tail(self):
        await self._add(status="ACTIVE")
        q = await self._run("task:pause:g1")
        self.assertEqual(self.client.paused, ["g1"])
        self.assertEqual((await self.repo.get_by_gid("g1"))["status"], "PAUSED")
        self.assertIn(("已暂停", False), q.answers)
        self.assertTrue(q.message.edits)  # 尾巴会重渲染任务卡片

    async def test_resume_goes_through_apply_tail(self):
        await self._add(status="PAUSED")
        q = await self._run("task:resume:g1")
        self.assertEqual(self.client.resumed, ["g1"])
        self.assertEqual((await self.repo.get_by_gid("g1"))["status"], "ACTIVE")
        self.assertIn(("已继续", False), q.answers)

    async def test_cancel_only_goes_through_apply_tail(self):
        await self._add(status="ACTIVE")
        await self._run("task:cancel_only:g1")
        self.assertEqual(self.client.removed, [("g1", False, True)])
        self.assertEqual((await self.repo.get_by_gid("g1"))["status"], "CANCELLED")

    async def test_unknown_action_reports_unknown(self):
        await self._add(status="ACTIVE")
        q = await self._run("task:bogus:g1")
        self.assertIn(("未知操作", True), q.answers)

    async def test_retry_readds_source_with_new_gid(self):
        await self._add(status="FAILED")
        q = await self._run("task:retry:g1")
        row = await self.repo.get_by_gid("gid-uri")  # FakeAria2.add_uri 的返回值
        self.assertIsNotNone(row)
        self.assertIn(("🔄 已重新加入下载", False), q.answers)

    async def test_retry_without_payload_alerts(self):
        await self.repo.create_task(
            gid="g1", user_id=1, chat_id=1, reply_message_id=None,
            source_type="url", source_ref="g1", file_name="g1.bin", file_size=1, payload=None,
        )
        await self.repo.update_status("g1", "FAILED")
        q = await self._run("task:retry:g1")
        self.assertIn(("缺少原始下载来源，无法重试。请重新发送链接或文件。", True), q.answers)

    async def test_cancel_shows_confirm_card(self):
        await self._add(status="ACTIVE")
        q = await self._run("task:cancel:g1")
        text, markup = q.message.edits[-1]
        self.assertIn("确认取消任务", text)
        self.assertIn("task:cancel_only:g1", _cb(markup))
        self.assertIn("task:confirm_delete_files:g1", _cb(markup))

    async def test_confirm_delete_files_shows_destructive_card(self):
        await self._add(status="ACTIVE")
        q = await self._run("task:confirm_delete_files:g1")
        text, markup = q.message.edits[-1]
        self.assertIn("永久删除已下载文件", text)
        self.assertIn("task:delete_files:g1", _cb(markup))

    async def test_limit_shows_chooser(self):
        await self._add(status="ACTIVE")
        q = await self._run("task:limit:g1")
        text, markup = q.message.edits[-1]
        self.assertIn("单任务限速", text)
        self.assertTrue(any(c.startswith("tasklimit:g1:") for c in _cb(markup)))

    async def test_files_on_completed_answers_location(self):
        save = os.path.join(self.dl, "a.bin")
        await self._add(status="COMPLETED")
        await self.repo.update_status("g1", "COMPLETED", save_path=save)
        q = await self._run("task:files:g1")
        self.assertTrue(any(t and t.startswith(f"保存位置：{save}") for t, _ in q.answers))

    async def test_link_answers_saved_path(self):
        save = os.path.join(self.dl, "a.bin")
        await self._add(status="COMPLETED")
        await self.repo.update_status("g1", "COMPLETED", save_path=save)
        q = await self._run("task:link:g1")
        self.assertIn((save, True), q.answers)

    async def test_settings_answers_hint(self):
        await self._add(status="ACTIVE")
        q = await self._run("task:settings:g1")
        self.assertTrue(any(t and "限速见" in t for t, _ in q.answers))

    async def test_sendtg_rejected_when_not_completed(self):
        await self._add(status="ACTIVE")
        q = await self._run("task:sendtg:g1", task_manager=StubTM())
        self.assertIn(("任务未完成，无法发送。", True), q.answers)

    async def test_sendtg_completed_delegates_to_task_manager(self):
        await self._add(status="COMPLETED")
        tm = StubTM()
        q = await self._run("task:sendtg:g1", task_manager=tm)
        self.assertEqual(tm.sent, [("g1", "g1")])
        self.assertIn(("正在发送…", False), q.answers)


if __name__ == "__main__":
    unittest.main()
