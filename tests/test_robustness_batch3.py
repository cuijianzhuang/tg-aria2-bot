"""第 3 批（性能/健壮）的回归测试：
- 轮询不再每轮拉 tellStopped：常规轮只拉 active+waiting，终态靠 60s 对账捡漏，
  非对账轮不把任务误判为丢失；
- /list 多节点进度改 gather 并发且单节点失败不拖垮整页；
- 429 退避期内键盘编辑跳过、通知编辑不硬撞；
- GoFile 上传带 sock_read 超时（保留 total=None 的大文件长传）；
- RSS session 复用 + 预抓取有限并发、单源失败不影响其它源；
- Web rclone/gofile 设置端点的同步文件 IO 走 to_thread（不在事件循环线程上执行）。
"""
import asyncio
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import aiohttp

from bot.core import gofile, rss
from bot.core.aria2_client import Aria2Client, Download
from bot.core.list_view import _progress_for
from bot.core.task_manager import RECONCILE_INTERVAL_SECONDS, TaskManager
from bot.db.repo import TaskRepo
from tests.fakes import FakeNodePool


def _struct(gid, status):
    return {
        "gid": gid, "status": status, "totalLength": "1", "completedLength": "0",
        "downloadSpeed": "0", "uploadSpeed": "0", "connections": "0", "dir": "/dl", "files": [],
    }


class TestClientPollingQueries(unittest.IsolatedAsyncioTestCase):
    """aria2_client 层：方法与 RPC 的对应关系（mock 传输层，记录调用）。"""

    def _client(self):
        client = Aria2Client("http://x:6800/jsonrpc", "s")
        calls = []

        async def fake_call(method, *params):
            calls.append((method, params))
            return {
                "tellActive": [_struct("a1", "active")],
                "tellWaiting": [_struct("w1", "waiting")],
                "tellStopped": [_struct("s1", "complete")],
            }[method]

        client._rpc.call = fake_call
        return client, calls

    async def test_active_downloads_never_touches_stopped(self):
        client, calls = self._client()
        downloads = await client.get_active_downloads()
        self.assertEqual({d.gid for d in downloads}, {"a1", "w1"})
        self.assertNotIn("tellStopped", [m for m, _ in calls])

    async def test_stopped_downloads_is_capped_at_recent_100_by_default(self):
        client, calls = self._client()
        downloads = await client.get_stopped_downloads()
        self.assertEqual([d.gid for d in downloads], ["s1"])
        self.assertEqual(calls, [("tellStopped", (0, 100))])

    async def test_progress_map_only_needs_active(self):
        client, calls = self._client()
        progress = await client.get_progress_map()
        self.assertEqual(set(progress), {"a1", "w1"})
        self.assertNotIn("tellStopped", [m for m, _ in calls])


class _FakeBot:
    def __init__(self):
        self.edits = []
        self.markup_edits = []
        self.sent_messages = []

    async def edit_message_text(self, chat_id, message_id, text, **kwargs):
        self.edits.append((chat_id, message_id))

    async def edit_message_reply_markup(self, chat_id, message_id, reply_markup=None, **kwargs):
        self.markup_edits.append((chat_id, message_id))

    async def send_message(self, chat_id, text, **kwargs):
        self.sent_messages.append((chat_id, text))


def _download(gid, status):
    return Download(
        gid=gid, status=status, total_length=10, completed_length=10 if status == "complete" else 1,
        download_speed=0, upload_speed=0, connections=1, error_message=None,
        dir=Path("/dl"), files=[],
    )


class TestPollReconcileCadence(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.pool = FakeNodePool(download_dir=os.path.join(self._dir.name, "dl"))
        self.bot = _FakeBot()
        self.tm = TaskManager(bot=self.bot, nodes=self.pool, repo=self.repo)
        self.client = self.pool.get("default")

    async def asyncTearDown(self):
        await self.repo.close()
        self._dir.cleanup()

    async def _row(self, gid):
        await self.repo.create_task(
            gid=gid, user_id=1, chat_id=1, reply_message_id=None,
            source_type="url", source_ref=gid, file_name="f.bin",
            file_size=10, payload="https://example.com/f.bin", node="default",
        )
        await self.repo.update_status(gid, "ACTIVE")

    async def _status(self, gid):
        row = await self.repo.get_by_gid(gid)
        return row["status"]

    async def test_first_poll_reconciles_and_terminal_is_found(self):
        # WS 漏掉的完成事件：首轮对账拉 tellStopped 并把任务收尾
        await self._row("g1")
        self.client.statuses["g1"] = _download("g1", "complete")
        await self.tm._poll_once()
        self.assertEqual(self.client.stopped_calls, 1)
        self.assertEqual(await self._status("g1"), "COMPLETED")

    async def test_regular_polls_skip_stopped_until_interval(self):
        await self._row("g1")
        self.client.statuses["g1"] = _download("g1", "active")
        await self.tm._poll_once()  # 首轮：对账
        self.assertEqual(self.client.stopped_calls, 1)
        await self.tm._poll_once()  # 紧接着的常规轮：只拉 active
        self.assertEqual(self.client.active_calls, 2)
        self.assertEqual(self.client.stopped_calls, 1)
        # 对账间隔到点后恢复拉 tellStopped
        self.tm._last_reconcile["default"] = time.monotonic() - RECONCILE_INTERVAL_SECONDS - 1
        await self.tm._poll_once()
        self.assertEqual(self.client.stopped_calls, 2)

    async def test_non_reconcile_round_does_not_mark_missing_task_lost(self):
        await self.tm._poll_once()  # 无任务的首轮：只登记对账时间
        await self._row("ghost")
        # ghost 不在 aria2 的任何列表里，但这一轮不是对账轮，不能判丢失
        await self.tm._poll_once()
        self.assertEqual(await self._status("ghost"), "ACTIVE")
        # 到对账轮才判定：文件不在磁盘上 → FAILED
        self.tm._last_reconcile["default"] = time.monotonic() - RECONCILE_INTERVAL_SECONDS - 1
        await self.tm._poll_once()
        self.assertEqual(await self._status("ghost"), "FAILED")


class TestBackoffEdits(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(os.path.join(self._dir.name, "t.db"))
        await self.repo.connect()
        self.bot = _FakeBot()
        self.tm = TaskManager(bot=self.bot, nodes=None, repo=self.repo)

    async def asyncTearDown(self):
        await self.repo.close()
        self._dir.cleanup()

    def _row(self):
        return {"gid": "g1", "chat_id": 42, "reply_message_id": 5, "backend": None}

    async def test_keyboard_update_skipped_during_backoff(self):
        self.tm._chat_backoff[42] = time.monotonic() + 60
        await self.tm._update_keyboard(self._row(), "g1", "PAUSED")
        self.assertEqual(self.bot.markup_edits, [])

    async def test_keyboard_update_runs_without_backoff(self):
        await self.tm._update_keyboard(self._row(), "g1", "PAUSED")
        self.assertEqual(len(self.bot.markup_edits), 1)

    async def test_notify_does_not_edit_during_backoff_but_still_delivers(self):
        self.tm._chat_backoff[42] = time.monotonic() + 60
        await self.tm._notify(self._row(), "done", gid="g1", status="COMPLETED")
        self.assertEqual(self.bot.edits, [])          # 不硬撞编辑
        self.assertEqual(len(self.bot.sent_messages), 1)  # 完成通知走新消息兜底，不丢


class _StubClient:
    def __init__(self, name, gate, progress=None, error=None):
        self._name = name
        self._gate = gate
        self._progress = progress or {}
        self._error = error
        self.called = False

    async def get_progress_map(self):
        self.called = True
        self._gate.note(self._name)
        await self._gate.wait_both()
        if self._error:
            raise self._error
        return self._progress


class _Gate:
    """两个节点的进度调用都进来后才放行：串行实现会死等第一个，必然超时。"""

    def __init__(self):
        self.seen = set()
        self._event = asyncio.Event()

    def note(self, name):
        self.seen.add(name)
        if len(self.seen) >= 2:
            self._event.set()

    async def wait_both(self):
        await asyncio.wait_for(self._event.wait(), timeout=5)


class _StubNodes:
    def __init__(self, clients):
        self._clients = clients

    def get(self, name):
        return self._clients[name]

    def label(self, name):
        return None


class TestListProgressGather(unittest.IsolatedAsyncioTestCase):
    async def test_nodes_are_queried_concurrently(self):
        gate = _Gate()
        nodes = _StubNodes({
            "a": _StubClient("a", gate, {"g1": {"percent": 10}}),
            "b": _StubClient("b", gate, {"g2": {"percent": 20}}),
        })
        rows = [
            {"node": "a", "status": "ACTIVE", "gid": "g1"},
            {"node": "b", "status": "ACTIVE", "gid": "g2"},
        ]
        progress = await _progress_for(nodes, rows)
        self.assertEqual(set(progress), {"g1", "g2"})

    async def test_one_node_failure_keeps_other_progress(self):
        gate = _Gate()
        nodes = _StubNodes({
            "a": _StubClient("a", gate, {"g1": {"percent": 10}}),
            "b": _StubClient("b", gate, error=RuntimeError("down")),
        })
        rows = [
            {"node": "a", "status": "ACTIVE", "gid": "g1"},
            {"node": "b", "status": "ACTIVE", "gid": "g2"},
        ]
        progress = await _progress_for(nodes, rows)
        self.assertEqual(set(progress), {"g1"})


class TestGofileUploadTimeout(unittest.IsolatedAsyncioTestCase):
    async def test_upload_uses_sock_read_timeout_without_total(self):
        captured = {}

        class _Resp:
            async def json(self):
                return {"status": "ok", "data": {"downloadPage": "https://gofile.io/d/x"}}

        class _Ctx:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *exc):
                return False

        class _Session:
            def post(self, url, data=None, timeout=None):
                captured["timeout"] = timeout
                return _Ctx()

        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(b"payload")
            path = f.name
        try:
            with patch.object(gofile, "_pick_server", AsyncMock(return_value="srv")), \
                    patch.object(gofile, "_get_session", return_value=_Session()):
                await gofile.upload_file(path, token="tok")
        finally:
            os.unlink(path)
        timeout = captured["timeout"]
        self.assertIsInstance(timeout, aiohttp.ClientTimeout)
        self.assertIsNone(timeout.total)  # 大文件长传不设总时限
        self.assertEqual(timeout.sock_read, gofile.UPLOAD_SOCK_READ_TIMEOUT_SECONDS)
        self.assertEqual(timeout.sock_read, 120)


class TestRssSessionAndPrefetch(unittest.IsolatedAsyncioTestCase):
    async def asyncTearDown(self):
        await rss.close_session()

    async def test_session_is_reused_and_recreated_after_close(self):
        first = rss._get_session()
        self.assertIs(first, rss._get_session())
        await rss.close_session()
        self.assertIsNot(first, rss._get_session())

    async def test_fetch_goes_through_shared_session(self):
        calls = []

        class _Resp:
            status = 200

            def __init__(self):
                self.content = SimpleNamespace(read=AsyncMock(return_value=b"body"))

        class _Ctx:
            async def __aenter__(self):
                return _Resp()

            async def __aexit__(self, *exc):
                return False

        class _Session:
            def get(self, url, **kwargs):
                calls.append(url)
                return _Ctx()

        with patch.object(rss, "_get_session", return_value=_Session()) as get_session:
            self.assertEqual(await rss.fetch("https://x.test/a"), b"body")
            self.assertEqual(await rss.fetch("https://x.test/b"), b"body")
        self.assertEqual(get_session.call_count, 2)
        self.assertEqual(calls, ["https://x.test/a", "https://x.test/b"])

    async def test_prefetch_is_concurrent_and_isolates_failures(self):
        running = 0
        peak = 0

        async def fake_fetch(url):
            nonlocal running, peak
            running += 1
            peak = max(peak, running)
            await asyncio.sleep(0.02)
            running -= 1
            if url.endswith("bad"):
                raise rss.FeedError("boom")
            return b"ok"

        feeds = [{"url": f"https://x.test/{i}"} for i in range(6)] + [{"url": "https://x.test/bad"}]
        with patch.object(rss, "fetch", fake_fetch):
            results = await rss._prefetch_all(feeds)
        self.assertEqual(len(results), len(feeds))
        self.assertGreaterEqual(peak, 2)  # 确实并发了
        self.assertLessEqual(peak, rss.FETCH_CONCURRENCY)  # 且受 Semaphore 上限约束
        errors = [body for _, body in results if isinstance(body, rss.FeedError)]
        self.assertEqual(len(errors), 1)  # 失败被带回而不是中断整批
        self.assertEqual(sum(1 for _, body in results if body == b"ok"), 6)


class TestWebSettingsOffLoopIO(unittest.IsolatedAsyncioTestCase):
    """rclone/gofile 设置端点：write_kv/read_kv 必须在非事件循环线程执行。"""

    async def test_gofile_settings_write_off_event_loop(self):
        from bot.web import app as web_app

        threads = []

        def recording_write(*args):
            threads.append(threading.get_ident())

        body = SimpleNamespace(enabled=True, token="t", compress=False, delete_local=False)
        with patch.object(web_app, "write_kv", recording_write):
            result = await web_app.set_gofile_settings(body)
        self.assertTrue(result["ok"])
        self.assertEqual(len(threads), 4)
        self.assertNotIn(threading.get_ident(), threads)

    async def test_rclone_settings_roundtrip_off_event_loop(self):
        from bot.web import app as web_app

        threads = []

        def recording_write(*args):
            threads.append(threading.get_ident())

        def recording_read(*args):
            threads.append(threading.get_ident())
            return ""

        body = SimpleNamespace(drive_name="d", drive_dir="/x", enabled=True)
        with patch.object(web_app, "write_kv", recording_write):
            result = await web_app.set_rclone_settings(body)
        self.assertTrue(result["ok"])
        with (
            patch.object(web_app, "read_kv", recording_read),
            patch.object(web_app, "list_rclone_remotes", lambda path: []),
        ):
            data = await web_app.get_rclone_settings()
        self.assertIn("remotes", data)
        self.assertTrue(threads)
        self.assertNotIn(threading.get_ident(), threads)


if __name__ == "__main__":
    unittest.main()
