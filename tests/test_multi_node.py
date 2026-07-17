import os
import sqlite3
import tempfile
import unittest

from bot.config import settings
from bot.core.cards import render_node_chooser, render_node_manage, render_pending_card, render_task_card
from bot.core.keyboards import (
    main_inline_keyboard,
    node_chooser_keyboard,
    node_manage_keyboard,
    pending_node_chooser_keyboard,
    pending_task_keyboard,
    task_keyboard,
)
from bot.core.node_pool import Node, NodePool, NodeUnavailable
from bot.db.repo import TaskRepo


class PoolTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.pool = NodePool(self.repo)
        await self.pool.load()

    async def asyncTearDown(self):
        await self.repo.close()
        self._dir.cleanup()

    async def _add_nas(self):
        await self.pool.add(
            name="nas", rpc_url="http://nas:6800/jsonrpc", secret="s3",
            download_dir="/volume1/downloads",
        )


class TestNodePool(PoolTestCase):
    async def test_default_node_from_env(self):
        node = self.pool.get_node("default")
        self.assertIsNotNone(node)
        self.assertTrue(node.is_local)
        self.assertEqual(node.rpc_url, settings.aria2_rpc)
        self.assertEqual(node.display_name, "本机")

    async def test_single_node_label_is_none(self):
        # 单节点部署：卡片/列表不显示节点标注，界面跟旧版一致
        self.assertFalse(self.pool.is_multi())
        self.assertIsNone(self.pool.label("default"))

    async def test_multi_node_label(self):
        await self._add_nas()
        self.assertTrue(self.pool.is_multi())
        self.assertEqual(self.pool.label("nas"), "nas")
        self.assertEqual(self.pool.label("default"), "本机")

    async def test_get_unknown_or_disabled_raises(self):
        with self.assertRaises(NodeUnavailable):
            self.pool.get("ghost")
        await self._add_nas()
        await self.pool.set_enabled("nas", False)
        with self.assertRaises(NodeUnavailable):
            self.pool.get("nas")

    async def test_resolve_falls_back_to_default(self):
        self.assertEqual(self.pool.resolve("ghost").name, "default")
        await self._add_nas()
        self.assertEqual(self.pool.resolve("nas").name, "nas")
        await self.pool.set_enabled("nas", False)
        self.assertEqual(self.pool.resolve("nas").name, "default")

    async def test_default_cannot_be_removed_or_disabled(self):
        with self.assertRaises(ValueError):
            await self.pool.remove("default")
        with self.assertRaises(ValueError):
            await self.pool.set_enabled("default", False)

    async def test_nodes_persist_across_reload(self):
        await self._add_nas()
        fresh = NodePool(self.repo)
        await fresh.load()
        node = fresh.get_node("nas")
        self.assertIsNotNone(node)
        self.assertEqual(node.download_dir, "/volume1/downloads")

    async def test_remove_clears_node(self):
        await self._add_nas()
        await self.pool.remove("nas")
        self.assertIsNone(self.pool.get_node("nas"))
        self.assertFalse(self.pool.is_multi())

    async def test_health_cache(self):
        self.assertTrue(self.pool.is_healthy("default"))  # 未探测过按在线算
        self.pool.mark_health("default", False)
        self.assertFalse(self.pool.is_healthy("default"))

    async def test_remove_closes_the_client_session(self):
        """节点被删除时要把它的 aiohttp session 关掉，不然连接池/WS 连接
        一直挂到进程退出（不发真实请求，只强制 session 惰性创建来验证）。"""
        await self._add_nas()
        client = self.pool.get("nas")
        session = client._rpc._get_session()
        self.assertFalse(session.closed)
        await self.pool.remove("nas")
        self.assertTrue(session.closed)

    async def test_remove_without_ever_fetching_client_is_safe(self):
        await self._add_nas()
        await self.pool.remove("nas")  # 从没调用过 get("nas")，不应该抛异常

    async def test_close_closes_every_client_session(self):
        await self._add_nas()
        default_session = self.pool.get("default")._rpc._get_session()
        nas_session = self.pool.get("nas")._rpc._get_session()
        await self.pool.close()
        self.assertTrue(default_session.closed)
        self.assertTrue(nas_session.closed)

    async def test_reload_closes_sessions_of_nodes_no_longer_present(self):
        await self._add_nas()
        nas_session = self.pool.get("nas")._rpc._get_session()
        await self.repo.delete_node("nas")  # 绕过 pool，模拟节点在别处被删掉
        await self.pool.load()
        self.assertTrue(nas_session.closed)


class TestUserPrefs(PoolTestCase):
    async def test_default_when_never_set(self):
        self.assertEqual(await self.repo.get_current_node(42), "default")

    async def test_set_and_get(self):
        await self.repo.set_current_node(42, "nas")
        self.assertEqual(await self.repo.get_current_node(42), "nas")
        # 覆盖更新而不是插入重复行
        await self.repo.set_current_node(42, "default")
        self.assertEqual(await self.repo.get_current_node(42), "default")


class TestTaskNodeColumn(PoolTestCase):
    async def test_create_task_records_node(self):
        await self.repo.create_task(
            gid="g1", user_id=1, chat_id=1, reply_message_id=None,
            source_type="url", source_ref="r", file_name="f", file_size=1,
            payload="u", node="nas",
        )
        row = await self.repo.get_by_gid("g1")
        self.assertEqual(row["node"], "nas")

    async def test_get_unfinished_filters_by_node(self):
        for gid, node in (("g1", "default"), ("g2", "nas")):
            await self.repo.create_task(
                gid=gid, user_id=1, chat_id=1, reply_message_id=None,
                source_type="url", source_ref=gid, file_name="f", file_size=1,
                payload="u", node=node,
            )
        self.assertEqual([r["gid"] for r in await self.repo.get_unfinished(node="nas")], ["g2"])
        self.assertEqual(len(await self.repo.get_unfinished()), 2)

    async def test_pending_node_roundtrip_and_update(self):
        token = await self.repo.create_pending(
            kind="url", user_id=1, chat_id=1, source_ref="r", file_name="f",
            file_size=1, payload="u", node="nas",
        )
        self.assertEqual((await self.repo.get_pending(token)).node, "nas")
        await self.repo.update_pending_node(token, "default")
        self.assertEqual((await self.repo.get_pending(token)).node, "default")


class TestMigrationFromOldDb(unittest.IsolatedAsyncioTestCase):
    async def test_old_rows_get_default_node(self):
        """模拟旧版数据库（无 node 列）升级：ALTER TABLE 补列后旧行归 default。"""
        d = tempfile.TemporaryDirectory()
        self.addCleanup(d.cleanup)
        db_path = os.path.join(d.name, "old.db")
        conn = sqlite3.connect(db_path)
        conn.executescript("""
            CREATE TABLE tasks (
                id INTEGER PRIMARY KEY AUTOINCREMENT, gid TEXT UNIQUE,
                user_id INTEGER NOT NULL, chat_id INTEGER NOT NULL,
                reply_message_id INTEGER, source_type TEXT NOT NULL,
                source_ref TEXT, file_name TEXT, file_size INTEGER,
                status TEXT NOT NULL DEFAULT 'PENDING', save_path TEXT, error TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, finished_at TIMESTAMP
            );
            CREATE TABLE pending_tasks (
                token TEXT PRIMARY KEY, kind TEXT NOT NULL, user_id INTEGER NOT NULL,
                chat_id INTEGER NOT NULL, source_ref TEXT, file_name TEXT,
                file_size INTEGER, payload TEXT NOT NULL, created_at REAL NOT NULL
            );
            INSERT INTO tasks (gid, user_id, chat_id, source_type, status)
                VALUES ('old-gid', 1, 1, 'url', 'ACTIVE');
        """)
        conn.commit()
        conn.close()

        repo = TaskRepo(db_path)
        await repo.connect()
        try:
            row = await repo.get_by_gid("old-gid")
            self.assertEqual(row["node"], "default")
            self.assertEqual([r["gid"] for r in await repo.get_unfinished(node="default")], ["old-gid"])
        finally:
            await repo.close()


NAS = Node(name="nas", rpc_url="http://nas:6800/jsonrpc", secret="s", download_dir="/v1", is_local=False)


class TestNodeUi(unittest.TestCase):
    def test_task_keyboard_remote_hides_sendtg(self):
        local_cbs = [b.callback_data for row in task_keyboard("g1", "COMPLETED", local=True).inline_keyboard for b in row]
        remote_cbs = [b.callback_data for row in task_keyboard("g1", "COMPLETED", local=False).inline_keyboard for b in row]
        self.assertIn("task:sendtg:g1", local_cbs)
        self.assertNotIn("task:sendtg:g1", remote_cbs)
        self.assertIn("task:delete:g1", remote_cbs)  # 其它按钮不受影响

    def test_main_keyboard_node_row_only_when_label_given(self):
        without = [b.callback_data for row in main_inline_keyboard({}).inline_keyboard for b in row]
        with_node = [b.callback_data for row in main_inline_keyboard({}, node_label="本机").inline_keyboard for b in row]
        self.assertNotIn("node:pick", without)
        self.assertIn("node:pick", with_node)

    def test_node_chooser_marks_current_and_health(self):
        default = Node(name="default", rpc_url="u", secret="s", download_dir="/d", is_local=True)
        kb = node_chooser_keyboard("default", [default, NAS], {"default": True, "nas": False})
        labels = {b.callback_data: b.text for row in kb.inline_keyboard for b in row}
        self.assertEqual(labels["node:use:default"], "·🟢 本机·")
        self.assertEqual(labels["node:use:nas"], "🔴 nas")

    def test_pending_keyboard_switch_button_gated(self):
        without = [b.callback_data for row in pending_task_keyboard("t1").inline_keyboard for b in row]
        with_switch = [b.callback_data for row in pending_task_keyboard("t1", show_node_switch=True).inline_keyboard for b in row]
        self.assertNotIn("pending:nodes:t1", without)
        self.assertIn("pending:nodes:t1", with_switch)

    def test_pending_node_chooser_callbacks(self):
        kb = pending_node_chooser_keyboard("t1", "default", [NAS], {"nas": True})
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("pnode:t1:nas", callbacks)

    def test_node_manage_keyboard_skips_default(self):
        default = Node(name="default", rpc_url="u", secret="s", download_dir="/d", is_local=True)
        kb = node_manage_keyboard([default, NAS])
        callbacks = [b.callback_data for row in kb.inline_keyboard for b in row]
        self.assertIn("admin:node:t:nas", callbacks)
        self.assertIn("admin:node:d:nas", callbacks)
        self.assertNotIn("admin:node:t:default", callbacks)


class TestNodeCards(unittest.TestCase):
    def _row(self):
        return {
            "id": 1, "gid": "g1", "status": "ACTIVE", "file_name": "f.mkv",
            "file_size": 1, "source_ref": "r", "save_path": None, "error": None,
            "gofile_link": None, "payload": "u", "source_type": "url", "node": "nas",
        }

    def test_task_card_shows_node_label_when_given(self):
        with_label = render_task_card(self._row(), status="ACTIVE", node_label="nas")
        without = render_task_card(self._row(), status="ACTIVE")
        self.assertIn("📍 节点：nas", with_label)
        self.assertNotIn("📍", without)

    def test_pending_card_node_line_and_custom_dir(self):
        text = render_pending_card("url", "f.zip", node_label="nas", download_dir="/volume1/downloads")
        self.assertIn("📍 节点：nas", text)
        self.assertIn("/volume1/downloads", text)

    def test_node_chooser_render(self):
        default = Node(name="default", rpc_url="u", secret="s", download_dir="/d", is_local=True)
        text = render_node_chooser("default", [default, NAS], {"default": True, "nas": False})
        self.assertIn("🟢 本机 ←当前", text)
        self.assertIn("🔴 nas", text)

    def test_node_manage_render(self):
        text = render_node_manage([NAS], {"nas": True})
        self.assertIn("nas", text)
        self.assertIn("http://nas:6800/jsonrpc", text)
        self.assertIn("/addnode", text)


if __name__ == "__main__":
    unittest.main()
