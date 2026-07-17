import os
import tempfile
import unittest

from bot.core.list_view import LIST_LIMIT, render_task_list
from bot.db.repo import TaskRepo


class FakeAria2:
    """render_task_list only calls get_progress_map when an ACTIVE row is on
    the page; these tests use COMPLETED rows so it's never invoked."""
    async def get_progress_map(self):
        raise AssertionError("should not be called for a page with no ACTIVE tasks")


class ListViewTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.aria2 = FakeAria2()

    async def asyncTearDown(self):
        await self.repo.close()
        self._dir.cleanup()

    async def _seed(self, n: int):
        for i in range(n):
            gid = f"g{i}"
            await self.repo.create_task(
                gid=gid, user_id=1, chat_id=1, reply_message_id=None,
                source_type="url", source_ref=gid, file_name=f"f{i}.bin",
                file_size=10, payload="https://example.com/f.bin",
            )
            await self.repo.update_status(gid, "COMPLETED")

    def _has_next(self, markup) -> bool:
        return any(b.callback_data.endswith(":1") and b.text == "➡️" for row in markup.inline_keyboard for b in row)

    def _has_prev(self, markup) -> bool:
        return any(b.text == "⬅️" for row in markup.inline_keyboard for b in row)


class TestExactMultiple(ListViewTestCase):
    async def test_no_spurious_next_page_when_count_is_exact_multiple(self):
        # 曾经的 bug: 任务数恰好是 LIST_LIMIT 的整数倍时会多出一个空白的下一页
        await self._seed(LIST_LIMIT)
        text, markup = await render_task_list(self.repo, self.aria2, "COMPLETED", 0)
        self.assertFalse(self._has_next(markup))
        self.assertFalse(self._has_prev(markup))


class TestRealNextPage(ListViewTestCase):
    async def test_next_page_shown_when_more_rows_exist(self):
        await self._seed(LIST_LIMIT + 1)
        text, markup = await render_task_list(self.repo, self.aria2, "COMPLETED", 0)
        self.assertTrue(self._has_next(markup))
        self.assertFalse(self._has_prev(markup))

    async def test_last_page_has_no_next_but_has_prev(self):
        await self._seed(LIST_LIMIT + 1)
        text, markup = await render_task_list(self.repo, self.aria2, "COMPLETED", 1)
        self.assertFalse(self._has_next(markup))
        self.assertTrue(self._has_prev(markup))
        self.assertIn(f"{LIST_LIMIT + 1}.", text)  # 第二页第一条编号紧接第一页


class TestBelowLimit(ListViewTestCase):
    async def test_no_pagination_controls_below_limit(self):
        await self._seed(3)
        text, markup = await render_task_list(self.repo, self.aria2, "COMPLETED", 0)
        self.assertFalse(self._has_next(markup))
        self.assertFalse(self._has_prev(markup))


if __name__ == "__main__":
    unittest.main()
