import asyncio
import contextlib
import os
import tempfile
import unittest
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from bot.config import settings
from bot.core.aria2_client import Download
from bot.core.node_pool import Node
from bot.core.task_manager import TaskManager
from bot.db.repo import TaskRepo
from tests.fakes import FakeNodePool


class FakeBot:
    """记录调用参数，不真的打 Telegram API。"""

    def __init__(self):
        self.sent_documents = []  # (chat_id, caption)
        self.sent_messages = []   # (chat_id, text)
        self.fail_send_document = False

    async def send_document(self, chat_id, document, caption=None, **kwargs):
        if self.fail_send_document:
            raise RuntimeError("boom")
        self.sent_documents.append((chat_id, caption))

    async def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append((chat_id, text))


class TestRunCleanupOnce(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        # run_cleanup_once 只碰 self._repo，bot/aria2 在这个方法里用不到，传 None 即可
        self.tm = TaskManager(bot=None, nodes=None, repo=self.repo)
        self._orig_days = settings.auto_cleanup_days

    async def asyncTearDown(self):
        settings.auto_cleanup_days = self._orig_days
        await self.repo.close()
        self._dir.cleanup()

    async def _old_completed_task(self, gid: str, days: int):
        await self.repo.create_task(
            gid=gid, user_id=1, chat_id=1, reply_message_id=None,
            source_type="url", source_ref=gid, file_name="f.bin",
            file_size=10, payload="https://example.com/f.bin",
        )
        await self.repo.update_status(gid, "COMPLETED")
        finished_at = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        await self.repo._conn.execute(
            "UPDATE tasks SET finished_at = ? WHERE gid = ?", (finished_at, gid)
        )
        await self.repo._conn.commit()

    async def test_disabled_when_days_is_zero(self):
        settings.auto_cleanup_days = 0
        await self._old_completed_task("g1", days=100)
        deleted = await self.tm.run_cleanup_once()
        self.assertEqual(deleted, 0)
        self.assertIsNotNone(await self.repo.get_by_gid("g1"))

    async def test_removes_only_records_past_retention(self):
        settings.auto_cleanup_days = 7
        await self._old_completed_task("old", days=10)
        await self._old_completed_task("recent", days=1)
        deleted = await self.tm.run_cleanup_once()
        self.assertEqual(deleted, 1)
        self.assertIsNone(await self.repo.get_by_gid("old"))
        self.assertIsNotNone(await self.repo.get_by_gid("recent"))


class TestSendFileToTg(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.bot = FakeBot()
        self.tm = TaskManager(bot=self.bot, nodes=None, repo=self.repo)

    async def asyncTearDown(self):
        await self.repo.close()
        self._dir.cleanup()

    def _row(self, **overrides):
        row = {"chat_id": 42, "file_name": "movie.mkv", "save_path": None}
        row.update(overrides)
        return row

    async def test_sends_existing_file(self):
        path = os.path.join(self._dir.name, "movie.mkv")
        with open(path, "wb") as f:
            f.write(b"data")
        ok, msg = await self.tm.send_file_to_tg(self._row(), "g1", path)
        self.assertTrue(ok)
        self.assertEqual(self.bot.sent_documents, [(42, "movie.mkv")])

    async def test_falls_back_to_row_save_path(self):
        path = os.path.join(self._dir.name, "movie.mkv")
        with open(path, "wb") as f:
            f.write(b"data")
        ok, msg = await self.tm.send_file_to_tg(self._row(save_path=path), "g1")
        self.assertTrue(ok)

    async def test_rejects_missing_file(self):
        ok, msg = await self.tm.send_file_to_tg(self._row(), "g1", "/nonexistent/path.mkv")
        self.assertFalse(ok)
        self.assertIn("不存在", msg)
        self.assertEqual(self.bot.sent_documents, [])

    async def test_rejects_directory(self):
        ok, msg = await self.tm.send_file_to_tg(self._row(), "g1", self._dir.name)
        self.assertFalse(ok)
        self.assertIn("目录", msg)

    async def test_rejects_oversized_file(self):
        path = os.path.join(self._dir.name, "big.bin")
        with open(path, "wb") as f:
            f.write(b"data")
        with patch("bot.core.task_manager.TG_MAX_SEND_BYTES", 1):
            ok, msg = await self.tm.send_file_to_tg(self._row(), "g1", path)
        self.assertFalse(ok)
        self.assertIn("过大", msg)

    async def test_reports_failure_from_telegram(self):
        path = os.path.join(self._dir.name, "movie.mkv")
        with open(path, "wb") as f:
            f.write(b"data")
        self.bot.fail_send_document = True
        ok, msg = await self.tm.send_file_to_tg(self._row(), "g1", path)
        self.assertFalse(ok)
        self.assertIn("发送失败", msg)


class TestDiskAlert(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = FakeBot()
        self.tm = TaskManager(bot=self.bot, nodes=None, repo=None)
        self._orig_threshold = settings.disk_alert_threshold_gb
        self._orig_admin = settings.admin_user_ids
        self._orig_allowed = settings.allowed_user_ids
        settings.admin_user_ids = "111"
        settings.allowed_user_ids = ""

    async def asyncTearDown(self):
        settings.disk_alert_threshold_gb = self._orig_threshold
        settings.admin_user_ids = self._orig_admin
        settings.allowed_user_ids = self._orig_allowed

    def _usage(self, free_gb: float):
        return SimpleNamespace(total=100 * 1024**3, used=0, free=int(free_gb * 1024**3))

    async def test_disabled_when_threshold_is_zero(self):
        settings.disk_alert_threshold_gb = 0
        with patch("bot.core.task_manager.shutil.disk_usage", return_value=self._usage(0.1)):
            await self.tm._check_disk_space()
        self.assertEqual(self.bot.sent_messages, [])

    async def test_alerts_admin_when_below_threshold(self):
        settings.disk_alert_threshold_gb = 10
        with patch("bot.core.task_manager.shutil.disk_usage", return_value=self._usage(2.0)):
            await self.tm._check_disk_space()
        self.assertEqual(len(self.bot.sent_messages), 1)
        chat_id, text = self.bot.sent_messages[0]
        self.assertEqual(chat_id, 111)
        self.assertIn("磁盘空间告警", text)

    async def test_no_alert_when_above_threshold(self):
        settings.disk_alert_threshold_gb = 10
        with patch("bot.core.task_manager.shutil.disk_usage", return_value=self._usage(50.0)):
            await self.tm._check_disk_space()
        self.assertEqual(self.bot.sent_messages, [])

    async def test_cooldown_suppresses_repeat_alert(self):
        settings.disk_alert_threshold_gb = 10
        with patch("bot.core.task_manager.shutil.disk_usage", return_value=self._usage(2.0)):
            await self.tm._check_disk_space()
            await self.tm._check_disk_space()
        self.assertEqual(len(self.bot.sent_messages), 1)

    async def test_realerts_after_recovery(self):
        settings.disk_alert_threshold_gb = 10
        with patch("bot.core.task_manager.shutil.disk_usage") as mock_usage:
            mock_usage.return_value = self._usage(2.0)
            await self.tm._check_disk_space()  # 第一次告警
            mock_usage.return_value = self._usage(50.0)
            await self.tm._check_disk_space()  # 恢复，重置状态
            mock_usage.return_value = self._usage(2.0)
            await self.tm._check_disk_space()  # 再次跌破，应该重新提醒
        self.assertEqual(len(self.bot.sent_messages), 2)

    async def test_no_recipients_does_not_crash(self):
        settings.disk_alert_threshold_gb = 10
        settings.admin_user_ids = ""
        settings.allowed_user_ids = ""
        with patch("bot.core.task_manager.shutil.disk_usage", return_value=self._usage(2.0)):
            await self.tm._check_disk_space()  # 不应该抛异常
        self.assertEqual(self.bot.sent_messages, [])


class TestWebSocketEvents(unittest.IsolatedAsyncioTestCase):
    """WS 推送让 TaskManager 不用等 5 秒轮询就能处理下载完成/出错——测试
    覆盖事件路由（gid/节点匹配）和监听任务的动态增减，不测真实网络连接
    （那部分在 test_aria2_rpc.py 里已经覆盖）。"""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.pool = FakeNodePool()
        self.bot = FakeBot()
        self.tm = TaskManager(bot=self.bot, nodes=self.pool, repo=self.repo)

    async def asyncTearDown(self):
        self.tm.stop()
        for task in list(self.tm._ws_tasks.values()) + list(self.tm._bg_tasks):
            task.cancel()
        await self.repo.close()
        self._dir.cleanup()

    async def _create_row(self, gid: str, *, node: str = "default", status: str = "ACTIVE"):
        await self.repo.create_task(
            gid=gid, user_id=1, chat_id=1, reply_message_id=None,
            source_type="url", source_ref=gid, file_name="f.bin",
            file_size=10, payload="https://example.com/f.bin", node=node,
        )
        if status != "PENDING":
            await self.repo.update_status(gid, status)

    @staticmethod
    def _download(gid: str, status: str = "complete") -> Download:
        return Download(
            gid=gid, status=status, total_length=10, completed_length=10,
            download_speed=0, upload_speed=0, connections=0, error_message=None,
            dir=Path("/dl"), files=[],
        )

    async def test_handle_ws_event_processes_matching_row(self):
        await self._create_row("g1")
        self.pool.get("default").statuses["g1"] = self._download("g1")
        await self.tm._handle_ws_event("default", "g1")
        row = await self.repo.get_by_gid("g1")
        self.assertEqual(row["status"], "COMPLETED")

    async def test_ignores_gid_unknown_to_this_bot(self):
        await self.tm._handle_ws_event("default", "ghost")  # 不应该抛异常

    async def test_ignores_event_from_wrong_node(self):
        await self._create_row("g1", node="default")
        await self.tm._handle_ws_event("nas", "g1")  # 事件来自另一个节点，忽略
        row = await self.repo.get_by_gid("g1")
        self.assertEqual(row["status"], "ACTIVE")  # 没被处理

    async def test_ignores_already_terminal_row_without_rpc_call(self):
        await self._create_row("g1", status="COMPLETED")
        # "default" 节点的 statuses 里没配 "g1"——如果代码真的发起 get_status
        # 会直接 KeyError；能跑到断言说明确实提前 return 了，没发多余的 RPC
        await self.tm._handle_ws_event("default", "g1")

    async def test_falls_back_to_poll_when_get_status_fails(self):
        await self._create_row("g1")  # 没在 statuses 里配置 -> get_status 抛 KeyError
        await self.tm._handle_ws_event("default", "g1")  # 吞掉异常，不传播
        row = await self.repo.get_by_gid("g1")
        self.assertEqual(row["status"], "ACTIVE")  # 状态没被误改

    async def test_reconcile_tracks_node_additions_and_removals(self):
        self.tm._reconcile_ws_listeners()
        self.assertIn("default", self.tm._ws_tasks)

        self.pool._nodes["nas"] = Node(
            name="nas", rpc_url="http://nas:6800/jsonrpc", secret="s",
            download_dir="/v", is_local=False,
        )
        self.tm._reconcile_ws_listeners()
        self.assertIn("nas", self.tm._ws_tasks)

        self.pool._nodes["nas"].enabled = False
        self.tm._reconcile_ws_listeners()
        self.assertNotIn("nas", self.tm._ws_tasks)

    async def test_ws_listen_end_to_end_updates_task_on_emitted_event(self):
        await self._create_row("g1")
        client = self.pool.get("default")
        client.statuses["g1"] = self._download("g1")
        client.events_to_emit = [("g1", "complete")]

        self.tm._running = True  # 平时由 start() 置位，这里绕过 start() 直调
        task = asyncio.create_task(self.tm._ws_listen("default"))
        for _ in range(10):
            await asyncio.sleep(0)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        if self.tm._bg_tasks:
            await asyncio.gather(*self.tm._bg_tasks, return_exceptions=True)

        row = await self.repo.get_by_gid("g1")
        self.assertEqual(row["status"], "COMPLETED")


class TestTerminalDeduplication(unittest.IsolatedAsyncioTestCase):
    """接了 WS 推送之后，轮询循环和 WS 回调可能对同一个 gid 的完成/出错事件
    各跑一遍 _handle_download_state——不去重的话 gofile 上传/自动发送会被
    并发触发两次。覆盖两种场景：真正并发的一对调用，以及轮询循环拿着过期
    快照、在 WS 已经先处理完之后才轮到这个 gid 的"迟到"场景。"""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.tm = TaskManager(bot=FakeBot(), nodes=FakeNodePool(), repo=self.repo)
        await self.repo.create_task(
            gid="g1", user_id=1, chat_id=1, reply_message_id=None,
            source_type="url", source_ref="g1", file_name="f.bin",
            file_size=10, payload="https://example.com/f.bin",
        )

    async def asyncTearDown(self):
        await self.repo.close()
        self._dir.cleanup()

    def _download(self) -> Download:
        return Download(
            gid="g1", status="complete", total_length=10, completed_length=10,
            download_speed=0, upload_speed=0, connections=0, error_message=None,
            dir=Path("/dl"), files=[],
        )

    def _spy_notify(self):
        calls = []
        original = self.tm._notify

        async def spy(*args, **kwargs):
            calls.append(1)
            return await original(*args, **kwargs)

        self.tm._notify = spy
        return calls

    async def test_truly_concurrent_calls_notify_only_once(self):
        row = dict(await self.repo.get_by_gid("g1"))
        download = self._download()
        calls = self._spy_notify()

        await asyncio.gather(
            self.tm._handle_download_state(row, download, node_is_local=True),
            self.tm._handle_download_state(row, download, node_is_local=True),
        )

        self.assertEqual(len(calls), 1)
        self.assertEqual((await self.repo.get_by_gid("g1"))["status"], "COMPLETED")

    async def test_stale_snapshot_replayed_after_the_fact_is_a_noop(self):
        stale_row = dict(await self.repo.get_by_gid("g1"))  # 还是 ACTIVE 的旧快照
        download = self._download()

        await self.tm._handle_download_state(dict(stale_row), download, node_is_local=True)
        self.assertEqual((await self.repo.get_by_gid("g1"))["status"], "COMPLETED")

        calls = self._spy_notify()
        # 模拟轮询循环手里那份 rows 快照没跟上——用同一份过期的 ACTIVE 快照
        # 再处理一次，此时 DB 里其实已经是 COMPLETED 了
        await self.tm._handle_download_state(dict(stale_row), download, node_is_local=True)
        self.assertEqual(len(calls), 0)

    async def test_in_flight_guard_is_released_after_processing(self):
        """守卫只应该在处理期间生效，不能处理完之后一直占着导致这个 gid
        以后永远处理不了（比如重试之后重新变成 ACTIVE 又完成一次）。"""
        row = dict(await self.repo.get_by_gid("g1"))
        await self.tm._handle_download_state(row, self._download(), node_is_local=True)
        self.assertNotIn("g1", self.tm._terminal_in_flight)


if __name__ == "__main__":
    unittest.main()
