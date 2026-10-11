"""过期文件自动清理：只删下载目录内部的东西，先提醒、给保留机会，满期才删。"""
import os
import tempfile
import time
import unittest
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from bot.config import settings
from bot.core import expiry
from bot.db.repo import TaskRepo
from bot.handlers import files


class TestSafeTarget(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = os.path.join(self._dir.name, "dl")
        os.makedirs(os.path.join(self.root, "video"))
        self.outside = os.path.join(self._dir.name, "secret")
        os.makedirs(self.outside)

    def test_inside_is_ok(self):
        p = os.path.join(self.root, "video", "a.mkv")
        open(p, "w").close()
        self.assertEqual(expiry.safe_target(p, self.root), os.path.realpath(p))

    def test_root_itself_and_parents_are_refused(self):
        self.assertIsNone(expiry.safe_target(self.root, self.root))
        self.assertIsNone(expiry.safe_target(self._dir.name, self.root))

    def test_outside_and_dotdot_are_refused(self):
        self.assertIsNone(expiry.safe_target(self.outside, self.root))
        self.assertIsNone(expiry.safe_target(os.path.join(self.root, "..", "secret"), self.root))

    def test_prefix_sibling_is_refused(self):
        sibling = self.root + "-backup"
        os.makedirs(sibling)
        self.assertIsNone(expiry.safe_target(sibling, self.root))

    def test_symlink_pointing_outside_only_removes_the_link(self):
        link = os.path.join(self.root, "evil")
        os.symlink(self.outside, link)
        target = expiry.safe_target(link, self.root)
        self.assertEqual(target, os.path.join(os.path.realpath(self.root), "evil"))
        self.assertTrue(expiry.delete_path(target))
        self.assertFalse(os.path.lexists(link))
        self.assertTrue(os.path.isdir(self.outside))        # 目标原封不动

    def test_empty_values(self):
        self.assertIsNone(expiry.safe_target(None, self.root))
        self.assertIsNone(expiry.safe_target("", self.root))

    def test_delete_path_handles_file_dir_and_missing(self):
        f = os.path.join(self.root, "f")
        open(f, "w").close()
        d = os.path.join(self.root, "d")
        os.makedirs(os.path.join(d, "sub"))
        open(os.path.join(d, "sub", "x"), "w").close()
        self.assertTrue(expiry.delete_path(f))
        self.assertTrue(expiry.delete_path(d))
        self.assertTrue(expiry.delete_path(os.path.join(self.root, "nope")))
        self.assertEqual(os.listdir(self.root), ["video"])


class FakeBot:
    def __init__(self):
        self.sent = []

    async def send_message(self, **kw):
        self.sent.append(kw)


class FakeNodes:
    def __init__(self, root, is_local=True):
        self.node = SimpleNamespace(name="default", download_dir=root, is_local=is_local, enabled=True)

    def get_node(self, name):
        return self.node if name == "default" else None


class ExpiryBase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.root = os.path.join(self._dir.name, "dl")
        os.makedirs(self.root)
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.addAsyncCleanup(self.repo.close)
        self._orig = (settings.file_expiry_days, settings.file_expiry_warn_days)
        self.addCleanup(lambda: (setattr(settings, "file_expiry_days", self._orig[0]),
                                 setattr(settings, "file_expiry_warn_days", self._orig[1])))
        settings.file_expiry_days = 7
        settings.file_expiry_warn_days = 1
        self.bot = FakeBot()
        self.nodes = FakeNodes(self.root)
        self.n = 0

    async def _task(self, age_days, *, name="a.bin", path=None, is_dir=False):
        """建一个已完成 age_days 天的任务，磁盘上有对应的文件。"""
        self.n += 1
        path = path or os.path.join(self.root, name)
        if is_dir:
            os.makedirs(path, exist_ok=True)
            open(os.path.join(path, "x"), "w").close()
        elif not os.path.lexists(path):
            open(path, "w").close()
        gid = f"g{self.n}"
        task_id = await self.repo.create_task(gid=gid, user_id=5, chat_id=50, reply_message_id=None,
                                              source_type="url", source_ref=gid, file_name=name, file_size=1)
        await self.repo.update_status(gid, "COMPLETED", save_path=path)
        finished = (datetime.now(UTC) - timedelta(days=age_days)).isoformat()
        await self.repo._conn.execute("UPDATE tasks SET finished_at = ? WHERE id = ?", (finished, task_id))
        await self.repo._conn.commit()
        return task_id, path

    async def _run(self):
        return await expiry.run_expiry_once(self.bot, self.repo, self.nodes)


class TestRun(ExpiryBase):
    async def test_disabled_does_nothing(self):
        settings.file_expiry_days = 0
        _, path = await self._task(100)
        self.assertEqual(await self._run(), (0, 0))
        self.assertTrue(os.path.exists(path))

    async def test_fresh_files_are_left_alone(self):
        _, path = await self._task(1)
        self.assertEqual(await self._run(), (0, 0))
        self.assertTrue(os.path.exists(path))

    async def test_warns_first_with_a_keep_button_and_does_not_delete(self):
        task_id, path = await self._task(6.5)
        self.assertEqual(await self._run(), (1, 0))
        self.assertTrue(os.path.exists(path))
        msg = self.bot.sent[0]
        self.assertEqual(msg["chat_id"], 50)
        self.assertEqual(msg["reply_markup"].inline_keyboard[0][0].callback_data, f"expiry:keep:{task_id}")
        self.assertEqual(await self._run(), (0, 0))              # 不重复提醒
        self.assertEqual(len(self.bot.sent), 1)

    async def test_old_files_are_warned_not_deleted_on_first_pass(self):
        """刚开启功能时存量的旧文件也要先提醒、给足时间，不能一次性删光。"""
        _, path = await self._task(100)
        self.assertEqual(await self._run(), (1, 0))
        self.assertTrue(os.path.exists(path))

    async def test_deleted_only_after_the_warning_period(self):
        task_id, path = await self._task(100)
        await self._run()
        await self.repo._conn.execute("UPDATE tasks SET expiry_warned = ? WHERE id = ?", (time.time() - 3600, task_id))
        await self.repo._conn.commit()
        self.assertEqual(await self._run(), (0, 0))              # 才提醒 1 小时
        self.assertTrue(os.path.exists(path))
        await self.repo._conn.execute("UPDATE tasks SET expiry_warned = ? WHERE id = ?", (time.time() - 90000, task_id))
        await self.repo._conn.commit()
        self.assertEqual(await self._run(), (0, 1))
        self.assertFalse(os.path.exists(path))
        self.assertIn("已自动删除", self.bot.sent[-1]["text"])
        row = await self.repo.get_by_id(task_id)
        self.assertEqual((row["status"], row["files_deleted"]), ("COMPLETED", 1))   # 记录还在

    async def test_kept_files_are_never_deleted(self):
        task_id, path = await self._task(100)
        await self.repo.set_expiry_keep(task_id, True)
        self.assertEqual(await self._run(), (0, 0))
        self.assertTrue(os.path.exists(path))

    async def test_directory_tasks_are_deleted_recursively(self):
        task_id, path = await self._task(100, name="pack", is_dir=True)
        await self.repo._conn.execute("UPDATE tasks SET expiry_warned = ? WHERE id = ?", (time.time() - 90000, task_id))
        await self.repo._conn.commit()
        await self._run()
        self.assertFalse(os.path.exists(path))

    async def test_path_outside_download_dir_is_never_touched(self):
        outside = os.path.join(self._dir.name, "precious.txt")
        open(outside, "w").close()
        task_id, _ = await self._task(100, name="precious.txt", path=outside)
        await self.repo._conn.execute("UPDATE tasks SET expiry_warned = ? WHERE id = ?", (time.time() - 90000, task_id))
        await self.repo._conn.commit()
        self.assertEqual(await self._run(), (0, 0))
        self.assertTrue(os.path.exists(outside))
        self.assertEqual((await self.repo.get_by_id(task_id))["files_deleted"], 1)   # 不再重复尝试

    async def test_remote_node_files_are_skipped_and_stop_being_considered(self):
        self.nodes = FakeNodes(self.root, is_local=False)
        task_id, path = await self._task(100)
        self.assertEqual(await self._run(), (0, 0))
        self.assertTrue(os.path.exists(path))
        self.assertEqual((await self.repo.get_by_id(task_id))["expiry_keep"], 1)

    async def test_unmanageable_rows_do_not_starve_real_ones(self):
        outside = os.path.join(self._dir.name, "x")
        open(outside, "w").close()
        for _ in range(3):
            await self._task(200, name="x", path=outside)
        _, real = await self._task(100)
        await self._run()                    # 前三条被标成不管理，不会每轮都占位
        rows = await self.repo.get_expiring(expiry._cutoff(0))
        self.assertEqual([r["save_path"] for r in rows], [real])

    async def test_warning_not_delivered_is_retried_and_blocks_deletion(self):
        task_id, path = await self._task(100)

        class Down(FakeBot):
            async def send_message(self, **kw):
                raise RuntimeError("blocked by user")

        self.bot = Down()
        self.assertEqual(await self._run(), (0, 0))
        self.assertEqual((await self.repo.get_by_id(task_id))["expiry_warned"], 0)
        self.assertTrue(os.path.exists(path))

    async def test_without_warning_period_deletes_directly(self):
        settings.file_expiry_warn_days = 0
        _, path = await self._task(8)
        self.assertEqual(await self._run(), (0, 1))
        self.assertFalse(os.path.exists(path))

    async def test_one_day_expiry_has_no_warning_phase(self):
        settings.file_expiry_days = 1
        _, path = await self._task(2)
        self.assertEqual(await self._run(), (0, 1))
        self.assertFalse(os.path.exists(path))


class FakeQuery:
    def __init__(self, data, user_id):
        self.data = data
        self.from_user = SimpleNamespace(id=user_id)
        self.message = SimpleNamespace(edits=[])
        self.message.edit_text = self._edit
        self.answers = []

    async def _edit(self, text, **kw):
        self.message.edits.append(text)

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))


class TestKeepButton(ExpiryBase):
    async def test_owner_can_keep(self):
        task_id, _ = await self._task(100)
        q = FakeQuery(f"expiry:keep:{task_id}", 5)
        await files.keep_file(q, self.repo)
        self.assertEqual((await self.repo.get_by_id(task_id))["expiry_keep"], 1)

    async def test_stranger_cannot(self):
        task_id, _ = await self._task(100)
        q = FakeQuery(f"expiry:keep:{task_id}", 99)
        await files.keep_file(q, self.repo)
        self.assertEqual((await self.repo.get_by_id(task_id))["expiry_keep"], 0)
        self.assertTrue(q.answers[-1][1])

    async def test_missing_task(self):
        q = FakeQuery("expiry:keep:9999", 5)
        await files.keep_file(q, self.repo)
        self.assertTrue(q.answers[-1][1])


if __name__ == "__main__":
    unittest.main()
