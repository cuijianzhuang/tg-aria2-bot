import os
import tempfile
import unittest

from bot.core.list_view import LIST_LIMIT, SEARCH_LIMIT, render_search_results, render_task_list
from bot.db.repo import TaskRepo
from tests.fakes import FakeNodePool


class ListViewTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.nodes = FakeNodePool()

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
        text, markup = await render_task_list(self.repo, self.nodes, "COMPLETED", 0)
        self.assertFalse(self._has_next(markup))
        self.assertFalse(self._has_prev(markup))


class TestRealNextPage(ListViewTestCase):
    async def test_next_page_shown_when_more_rows_exist(self):
        await self._seed(LIST_LIMIT + 1)
        text, markup = await render_task_list(self.repo, self.nodes, "COMPLETED", 0)
        self.assertTrue(self._has_next(markup))
        self.assertFalse(self._has_prev(markup))

    async def test_last_page_has_no_next_but_has_prev(self):
        await self._seed(LIST_LIMIT + 1)
        text, markup = await render_task_list(self.repo, self.nodes, "COMPLETED", 1)
        self.assertFalse(self._has_next(markup))
        self.assertTrue(self._has_prev(markup))
        self.assertIn(f"{LIST_LIMIT + 1}.", text)  # 第二页第一条编号紧接第一页


class TestBelowLimit(ListViewTestCase):
    async def test_no_pagination_controls_below_limit(self):
        await self._seed(3)
        text, markup = await render_task_list(self.repo, self.nodes, "COMPLETED", 0)
        self.assertFalse(self._has_next(markup))
        self.assertFalse(self._has_prev(markup))


class TestSearchResults(ListViewTestCase):
    async def _seed_named(self, names: list[str]):
        for i, name in enumerate(names):
            gid = f"g{i}"
            await self.repo.create_task(
                gid=gid, user_id=1, chat_id=1, reply_message_id=None,
                source_type="url", source_ref=gid, file_name=name,
                file_size=10, payload="https://example.com/f.bin",
            )
            await self.repo.update_status(gid, "COMPLETED")

    async def test_finds_matching_tasks(self):
        await self._seed_named(["movie.2024.mkv", "show.s01e01.mp4"])
        text, markup = await render_search_results(self.repo, self.nodes, "movie")
        self.assertIn("movie.2024.mkv", text)
        self.assertNotIn("show.s01e01.mp4", text)
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        self.assertIn("task:open:g0", callbacks)

    async def test_no_match_shows_empty_message(self):
        await self._seed_named(["movie.mkv"])
        text, markup = await render_search_results(self.repo, self.nodes, "nonexistent")
        self.assertIn("没有找到匹配的任务", text)

    async def test_truncation_hint_when_results_exceed_limit(self):
        await self._seed_named([f"dup{i}.mkv" for i in range(SEARCH_LIMIT + 2)])
        text, markup = await render_search_results(self.repo, self.nodes, "dup")
        self.assertIn("结果超过", text)
        # 只渲染 SEARCH_LIMIT 条 open 按钮，不是全部匹配数
        callbacks = [b.callback_data for row in markup.inline_keyboard for b in row]
        open_buttons = [c for c in callbacks if c.startswith("task:open:")]
        self.assertEqual(len(open_buttons), SEARCH_LIMIT)

    async def test_keyword_is_escaped_in_title(self):
        await self._seed_named(["a.mkv"])
        text, markup = await render_search_results(self.repo, self.nodes, "<script>")
        self.assertIn("&lt;script&gt;", text)
        self.assertNotIn("<script>", text)


if __name__ == "__main__":
    unittest.main()
