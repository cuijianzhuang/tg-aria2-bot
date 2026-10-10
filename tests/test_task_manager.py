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

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edits = getattr(self, "edits", [])
        self.edits.append((chat_id, message_id))

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None, **kwargs):
        self.markup_edits = getattr(self, "markup_edits", [])
        self.markup_edits.append((chat_id, message_id))


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


class TestNodeOfflineAlert(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.bot = FakeBot()
        self.nodes = FakeNodePool()
        self.tm = TaskManager(bot=self.bot, nodes=self.nodes, repo=None)
        self.node = self.nodes.get_node("default")
        self._orig_admin = settings.admin_user_ids
        self._orig_allowed = settings.allowed_user_ids
        settings.admin_user_ids = "111"
        settings.allowed_user_ids = ""

    async def asyncTearDown(self):
        settings.admin_user_ids = self._orig_admin
        settings.allowed_user_ids = self._orig_allowed

    async def test_no_alert_before_threshold(self):
        with patch("bot.core.task_manager.time.monotonic", side_effect=[0.0, 100.0]):
            await self.tm._handle_node_health(self.node, False)
            await self.tm._handle_node_health(self.node, False)
        self.assertEqual(self.bot.sent_messages, [])
        self.assertFalse(self.nodes.is_healthy("default"))

    async def test_alerts_after_sustained_outage(self):
        # _handle_node_health(..., False) 每次调用只取一次 time.monotonic()
        # （见实现里的注释：setdefault 的默认值参数会无条件求值，所以要先存
        # 局部变量复用，不能直接调两次）——每个 False 调用对应一个时间戳
        with patch("bot.core.task_manager.time.monotonic", side_effect=[0.0, 601.0]):
            await self.tm._handle_node_health(self.node, False)
            await self.tm._handle_node_health(self.node, False)
        self.assertEqual(len(self.bot.sent_messages), 1)
        chat_id, text = self.bot.sent_messages[0]
        self.assertEqual(chat_id, 111)
        self.assertIn("节点离线告警", text)

    async def test_no_repeat_alert_while_still_down(self):
        with patch("bot.core.task_manager.time.monotonic", side_effect=[0.0, 601.0, 900.0]):
            await self.tm._handle_node_health(self.node, False)
            await self.tm._handle_node_health(self.node, False)  # 触发告警
            await self.tm._handle_node_health(self.node, False)  # 仍然离线，不重复
        self.assertEqual(len(self.bot.sent_messages), 1)

    async def test_realerts_after_recovery(self):
        with patch(
            "bot.core.task_manager.time.monotonic",
            side_effect=[1000.0, 1601.0, 2000.0, 2601.0],
        ):
            await self.tm._handle_node_health(self.node, False)
            await self.tm._handle_node_health(self.node, False)  # 第一次告警
            await self.tm._handle_node_health(self.node, True)   # 恢复，重置状态（不耗时间戳）
            await self.tm._handle_node_health(self.node, False)
            await self.tm._handle_node_health(self.node, False)  # 再次跌破满 10 分钟，重新告警
        self.assertEqual(len(self.bot.sent_messages), 2)

    async def test_recovery_clears_unhealthy_state_without_notifying(self):
        with patch("bot.core.task_manager.time.monotonic", side_effect=[0.0, 100.0]):
            await self.tm._handle_node_health(self.node, False)
            await self.tm._handle_node_health(self.node, True)
        self.assertEqual(self.bot.sent_messages, [])
        self.assertTrue(self.nodes.is_healthy("default"))
        self.assertNotIn("default", self.tm._node_unhealthy_since)


class TestPollOnceIsolation(unittest.IsolatedAsyncioTestCase):
    """_poll_once 现在并发展开各节点（见 task_manager.py 里的改动说明）：
    一个节点在处理某个任务行时抛出意料之外的异常，不该连带打断同一轮里
    其它节点的处理——旧的串行 for 循环版本会。"""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        remote = Node(
            name="remote", rpc_url="http://remote:6800/jsonrpc", secret="s",
            download_dir="/dl", is_local=False,
        )
        self.pool = FakeNodePool(extra_nodes=[remote])
        self.bot = FakeBot()
        self.tm = TaskManager(bot=self.bot, nodes=self.pool, repo=self.repo)

    async def asyncTearDown(self):
        await self.repo.close()
        self._dir.cleanup()

    async def _create_row(self, gid: str, *, node: str):
        await self.repo.create_task(
            gid=gid, user_id=1, chat_id=1, reply_message_id=None,
            source_type="url", source_ref=gid, file_name="f.bin",
            file_size=10, payload="https://example.com/f.bin", node=node,
        )
        await self.repo.update_status(gid, "ACTIVE")

    @staticmethod
    def _download(gid: str) -> Download:
        return Download(
            gid=gid, status="active", total_length=10, completed_length=1,
            download_speed=1, upload_speed=0, connections=1, error_message=None,
            dir=Path("/dl"), files=[],
        )

    async def test_one_node_exception_does_not_block_other_nodes(self):
        await self._create_row("bad", node="default")
        await self._create_row("good", node="remote")
        self.pool.get("default").statuses["bad"] = self._download("bad")
        self.pool.get("remote").statuses["good"] = self._download("good")

        processed = []

        async def flaky(row, download, *, node_is_local=True):
            if row["gid"] == "bad":
                raise RuntimeError("boom")
            processed.append(row["gid"])

        self.tm._handle_download_state = flaky
        await self.tm._poll_once()  # 不应该抛出去，也不该漏掉 remote 节点

        # remote 节点没有因为 default 节点抛异常而被跳过
        self.assertEqual(processed, ["good"])


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


class TestMagnetMetadataFollow(unittest.IsolatedAsyncioTestCase):
    """磁力/裸 infohash 任务的元数据下载阶段 complete 时带 followedBy——
    不能当成真正完成（旧 bug：会误触发 gofile/发送 TG，且真正的文件内容
    下载完全没人跟踪，见用户反馈"下载完后没有继续下载文件"）。"""

    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.bot = FakeBot()
        self.tm = TaskManager(bot=self.bot, nodes=FakeNodePool(), repo=self.repo)
        self.task_id = await self.repo.create_task(
            gid="meta-gid", user_id=1, chat_id=1, reply_message_id=None,
            source_type="magnet", source_ref="m1", file_name="磁力链接任务",
            file_size=None, payload="magnet:?xt=urn:btih:abc",
        )
        await self.repo.update_status("meta-gid", "ACTIVE")

    async def asyncTearDown(self):
        await self.repo.close()
        self._dir.cleanup()

    @staticmethod
    def _metadata_download(followed_by: list[str]) -> Download:
        return Download(
            gid="meta-gid", status="complete", total_length=24000, completed_length=24000,
            download_speed=0, upload_speed=0, connections=0, error_message=None,
            dir=Path("/dl"), files=[], followed_by=followed_by,
        )

    async def test_metadata_completion_follows_to_new_gid_instead_of_finishing(self):
        row = dict(await self.repo.get_by_gid("meta-gid"))
        await self.tm._handle_download_state(row, self._metadata_download(["real-gid"]), node_is_local=True)

        # gid 接到了真正的内容下载上，状态打回 PENDING（不是 COMPLETED），
        # 交给下一轮轮询/WS 继续追踪
        updated = await self.repo.get_by_gid("real-gid")
        self.assertIsNotNone(updated)
        self.assertEqual(updated["status"], "PENDING")
        self.assertIsNone(await self.repo.get_by_gid("meta-gid"))

    async def test_metadata_completion_does_not_trigger_notify_or_gofile(self):
        row = dict(await self.repo.get_by_gid("meta-gid"))
        await self.tm._handle_download_state(row, self._metadata_download(["real-gid"]), node_is_local=True)
        # 元数据阶段不该推送"下载完成"消息——那是留给真正内容下载完成时的
        self.assertEqual(self.bot.sent_messages, [])

    async def test_real_completion_without_followed_by_finishes_normally(self):
        row = dict(await self.repo.get_by_gid("meta-gid"))
        real = Download(
            gid="meta-gid", status="complete", total_length=10, completed_length=10,
            download_speed=0, upload_speed=0, connections=0, error_message=None,
            dir=Path("/dl"), files=[], followed_by=[],
        )
        await self.tm._handle_download_state(row, real, node_is_local=True)
        self.assertEqual((await self.repo.get_by_gid("meta-gid"))["status"], "COMPLETED")


class ProgressTestBase(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.bot = FakeBot()
        self.tm = TaskManager(bot=self.bot, nodes=None, repo=None)
        self._orig = (settings.progress_interval, settings.progress_idle_interval, settings.progress_watch_seconds)
        settings.progress_interval = 2
        settings.progress_idle_interval = 30
        settings.progress_watch_seconds = 120
        self.now = 1000.0

    def tearDown(self):
        settings.progress_interval, settings.progress_idle_interval, settings.progress_watch_seconds = self._orig

    def _row(self, gid, chat_id=1):
        return {"gid": gid, "chat_id": chat_id, "reply_message_id": 7, "id": 1, "node": "default",
                "file_name": "f", "source_ref": "u", "status": "ACTIVE", "save_path": None,
                "error": None, "gofile_link": None, "file_size": 100, "source_type": "url"}

    def _dl(self, completed):
        return SimpleNamespace(progress=completed, completed_length=completed, total_length=100,
                               files=[], download_speed=1, connections=1, dir="/d", name="f",
                               completed_length_string=lambda: "x", total_length_string=lambda: "y",
                               download_speed_string=lambda: "1 B/s", upload_speed_string=lambda: "0 B/s")

    async def _tick(self, row, completed, advance):
        """推进时间 advance 秒后模拟一轮轮询；返回累计编辑次数。"""
        self.now += advance
        with patch("bot.core.task_manager.time.monotonic", return_value=self.now):
            await self.tm._maybe_report_progress(row, self._dl(completed))
        return len(getattr(self.bot, "edits", []))

    def _at(self, fn, *args):
        """在模拟的当前时间点调用 watch/hold 这类读 time.monotonic 的方法。"""
        with patch("bot.core.task_manager.time.monotonic", return_value=self.now):
            return fn(*args)


class TestIdleVsWatchedRefresh(ProgressTestBase):
    """没人看的卡片慢速刷新（省编辑次数），用户互动过的卡片快速刷新。"""

    async def test_idle_card_refreshes_every_30s(self):
        row = self._row("g1")
        self.assertEqual(await self._tick(row, 1, 0), 1)     # 第一次立即刷新
        self.assertEqual(await self._tick(row, 2, 10), 1)    # 10 秒：不刷新
        self.assertEqual(await self._tick(row, 3, 19), 1)    # 29 秒：不刷新
        self.assertEqual(await self._tick(row, 4, 1), 2)     # 30 秒：刷新

    async def test_watched_card_refreshes_every_2s(self):
        row = self._row("g1")
        await self._tick(row, 1, 0)
        self._at(self.tm.watch, "g1")
        self.assertEqual(await self._tick(row, 2, 1), 1)     # 1 秒：还没到
        self.assertEqual(await self._tick(row, 3, 1), 2)     # 2 秒：刷新
        self.assertEqual(await self._tick(row, 4, 2), 3)

    async def test_watch_expires_and_card_goes_back_to_idle(self):
        row = self._row("g1")
        self._at(self.tm.watch, "g1")
        await self._tick(row, 1, 0)
        self.assertEqual(await self._tick(row, 2, 100), 2)   # 100 秒，仍在 120 秒观察期内：快速档，刷新
        self.assertEqual(await self._tick(row, 3, 25), 2)    # 125 秒，观察期已过 → 慢速档，距上次才 25 秒：不刷新
        self.assertEqual(await self._tick(row, 4, 5), 3)     # 距上次 30 秒：刷新

    async def test_stalled_watched_download_refreshes_slowly(self):
        row = self._row("g1")
        self._at(self.tm.watch, "g1")
        await self._tick(row, 5, 0)
        self.assertEqual(await self._tick(row, 5, 3), 1)     # 字节没变：不刷新
        self.assertEqual(await self._tick(row, 5, 27), 2)    # 30 秒后照样刷新一次

    async def test_poll_goes_fast_only_while_someone_is_watching(self):
        self.assertFalse(self._at(self.tm._any_watched))
        self._at(self.tm.watch, "g1")
        self.assertTrue(self._at(self.tm._any_watched))
        self.now += 121
        self.assertFalse(self._at(self.tm._any_watched))     # 过期自动清掉

    async def test_finished_task_stops_being_watched(self):
        self._at(self.tm.watch, "g1")
        self.tm._forget_progress("g1")
        self.assertFalse(self._at(self.tm._any_watched))


class TestChatBudget(ProgressTestBase):
    """同一聊天里的多张卡片分摊编辑预算，避免撞 Telegram 限速。"""

    async def test_many_watched_cards_in_one_chat_slow_down(self):
        rows = [self._row(f"g{i}") for i in range(5)]
        for r in rows:
            self._at(self.tm.watch, r["gid"])
            await self._tick(r, 1, 0)
        start = len(self.bot.edits)
        # 5 张都在被看 → 每张至少 10 秒
        self.assertEqual(await self._tick(rows[0], 2, 3), start)
        self.assertEqual(await self._tick(rows[0], 3, 7), start + 1)

    async def test_idle_cards_do_not_slow_down_a_watched_card(self):
        for i in range(5):
            await self._tick(self._row(f"idle{i}"), 1, 0)
        row = self._row("mine")
        self._at(self.tm.watch, "mine")
        before = await self._tick(row, 1, 0)
        self.assertEqual(await self._tick(row, 2, 2), before + 1)   # 仍然是 2 秒

    async def test_other_chats_do_not_slow_each_other(self):
        for i in range(5):
            r = self._row(f"a{i}", chat_id=1)
            self._at(self.tm.watch, r["gid"])
            await self._tick(r, 1, 0)
        solo = self._row("b1", chat_id=2)
        self._at(self.tm.watch, "b1")
        before = await self._tick(solo, 1, 0)
        self.assertEqual(await self._tick(solo, 2, 2), before + 1)

    async def test_finished_task_frees_budget(self):
        a, b = self._row("a"), self._row("b")
        for r in (a, b):
            self._at(self.tm.watch, r["gid"])
        await self._tick(a, 1, 0)
        await self._tick(b, 1, 0)
        self.tm._forget_progress("b")
        before = len(self.bot.edits)
        self.assertEqual(await self._tick(a, 2, 2), before + 1)     # 只剩一张：回到 2 秒


class TestMenuHold(ProgressTestBase):
    """用户打开子菜单（限速/取消确认/选择文件）时不能被自动刷新冲掉。"""

    async def test_hold_suppresses_auto_refresh(self):
        row = self._row("g1")
        self._at(self.tm.watch, "g1")
        await self._tick(row, 1, 0)
        self._at(self.tm.hold, "g1")
        self.assertEqual(await self._tick(row, 2, 10), 1)           # 本该刷新，但被暂停
        self.assertEqual(await self._tick(row, 3, 60), 1)

    async def test_watch_releases_the_hold(self):
        row = self._row("g1")
        await self._tick(row, 1, 0)
        self._at(self.tm.hold, "g1")
        self._at(self.tm.watch, "g1")                                # 用户点了返回/刷新
        self.assertEqual(await self._tick(row, 2, 3), 2)

    async def test_hold_expires_on_its_own(self):
        row = self._row("g1")
        await self._tick(row, 1, 0)
        self._at(self.tm.hold, "g1")
        self.assertEqual(await self._tick(row, 2, 130), 2)          # 菜单被遗弃，暂停到期后恢复刷新

    async def test_hold_blocks_keyboard_swap_too(self):
        self._at(self.tm.hold, "g1")
        with patch("bot.core.task_manager.time.monotonic", return_value=self.now):
            await self.tm._update_keyboard(self._row("g1"), "g1", "PAUSED")
        self.assertFalse(getattr(self.bot, "markup_edits", []))

    async def test_keyboard_updates_when_not_held(self):
        with patch("bot.core.task_manager.time.monotonic", return_value=self.now):
            await self.tm._update_keyboard(self._row("g1"), "g1", "PAUSED")
        self.assertEqual(len(self.bot.markup_edits), 1)


class TestRealNameSync(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.tm = TaskManager(bot=FakeBot(), nodes=None, repo=self.repo)

    async def asyncTearDown(self):
        await self.repo.close()
        self._dir.cleanup()

    async def _task(self, kind, name):
        await self.repo.create_task(
            gid="g1", user_id=1, chat_id=1, reply_message_id=None, source_type=kind,
            source_ref="r", file_name=name, file_size=None, payload="p",
        )
        return await self.repo.get_by_gid("g1")

    async def test_torrent_gets_real_content_name(self):
        row = await self._task("torrent", "x.torrent")
        await self.tm._sync_real_name(row, "g1", SimpleNamespace(name="IPZZ-961"))
        self.assertEqual((await self.repo.get_by_gid("g1"))["file_name"], "IPZZ-961")

    async def test_metadata_placeholder_is_not_written(self):
        row = await self._task("magnet", "magnet:?xt=urn:btih:abc")
        await self.tm._sync_real_name(row, "g1", SimpleNamespace(name="[METADATA]abc"))
        self.assertEqual((await self.repo.get_by_gid("g1"))["file_name"], "magnet:?xt=urn:btih:abc")

    async def test_url_task_name_is_left_alone(self):
        row = await self._task("url", "mine.zip")
        await self.tm._sync_real_name(row, "g1", SimpleNamespace(name="server-chosen.zip"))
        self.assertEqual((await self.repo.get_by_gid("g1"))["file_name"], "mine.zip")


if __name__ == "__main__":
    unittest.main()
