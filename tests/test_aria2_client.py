"""Aria2Client 业务层测试：Download/File/Stats 从裸 RPC dict 解析的正确性，
以及依赖这些解析结果的 remove()/get_all_downloads() 等方法逻辑。不连真实
aria2——用一个记录调用参数、按脚本回应的假 Aria2RpcClient 代替传输层。
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from bot.core.aria2_client import Aria2Client, Download, File, Stats, _root_paths

# ---------------- 纯解析/格式化：不需要网络，直接构造 struct ----------------

class TestFileParsing(unittest.TestCase):
    def test_basic_fields(self):
        f = File.from_struct({"index": "3", "path": "/dl/a.mkv", "length": "1000", "completedLength": "500", "selected": "true"})
        self.assertEqual(f.index, 3)
        self.assertEqual(f.path, Path("/dl/a.mkv"))
        self.assertEqual(f.length, 1000)
        self.assertEqual(f.completed_length, 500)
        self.assertTrue(f.selected)
        self.assertFalse(f.is_metadata)

    def test_selected_false(self):
        f = File.from_struct({"index": "1", "path": "/dl/a.mkv", "length": "1", "selected": "false"})
        self.assertFalse(f.selected)

    def test_missing_selected_defaults_true(self):
        f = File.from_struct({"index": "1", "path": "/dl/a.mkv", "length": "1"})
        self.assertTrue(f.selected)

    def test_metadata_detection(self):
        f = File.from_struct({"index": "1", "path": "[METADATA]abcd1234.torrent", "length": "0"})
        self.assertTrue(f.is_metadata)

    def test_length_string(self):
        f = File.from_struct({"index": "1", "path": "/a", "length": str(2 * 1024 * 1024)})
        self.assertEqual(f.length_string(), "2.0 MiB")


class TestDownloadParsing(unittest.TestCase):
    def _struct(self, **overrides):
        base = {
            "gid": "abc123",
            "status": "active",
            "totalLength": "1000",
            "completedLength": "250",
            "downloadSpeed": "1048576",
            "uploadSpeed": "0",
            "connections": "4",
            "dir": "/downloads/video",
            "files": [{"index": "1", "path": "/downloads/video/movie.mkv", "length": "1000", "selected": "true"}],
        }
        base.update(overrides)
        return base

    def test_basic_fields_and_progress(self):
        d = Download.from_struct(self._struct())
        self.assertEqual(d.gid, "abc123")
        self.assertEqual(d.status, "active")
        self.assertEqual(d.progress, 25.0)
        self.assertEqual(d.dir, Path("/downloads/video"))
        self.assertIsNone(d.error_message)

    def test_progress_zero_when_total_length_unknown(self):
        d = Download.from_struct(self._struct(totalLength="0", completedLength="0"))
        self.assertEqual(d.progress, 0.0)

    def test_error_message_present(self):
        d = Download.from_struct(self._struct(status="error", errorMessage="Connection refused"))
        self.assertEqual(d.error_message, "Connection refused")

    def test_empty_error_message_becomes_none(self):
        d = Download.from_struct(self._struct(errorMessage=""))
        self.assertIsNone(d.error_message)

    def test_name_single_file_http_download(self):
        d = Download.from_struct(self._struct())
        self.assertEqual(d.name, "movie.mkv")

    def test_name_multi_file_torrent_uses_bittorrent_info_name(self):
        d = Download.from_struct(self._struct(
            files=[
                {"index": "1", "path": "/downloads/video/MyShow/ep1.mkv", "length": "1", "selected": "true"},
                {"index": "2", "path": "/downloads/video/MyShow/ep2.mkv", "length": "1", "selected": "true"},
            ],
            bittorrent={"info": {"name": "MyShow"}},
        ))
        self.assertEqual(d.name, "MyShow")

    def test_name_metadata_pending_falls_back_to_placeholder_path(self):
        d = Download.from_struct(self._struct(
            files=[{"index": "1", "path": "[METADATA]deadbeef.torrent", "length": "0"}],
        ))
        self.assertEqual(d.name, "[METADATA]deadbeef.torrent")

    def test_name_none_when_no_files(self):
        d = Download.from_struct(self._struct(files=[]))
        self.assertIsNone(d.name)

    def test_formatted_strings(self):
        d = Download.from_struct(self._struct())
        self.assertEqual(d.completed_length_string(), "250 B")
        self.assertEqual(d.total_length_string(), "1000 B")
        self.assertEqual(d.download_speed_string(), "1.0 MiB/s")
        self.assertEqual(d.upload_speed_string(), "0 B/s")


class TestStatsParsing(unittest.TestCase):
    def test_from_struct(self):
        s = Stats.from_struct({
            "numActive": "2", "numWaiting": "1", "numStopped": "5",
            "downloadSpeed": "2097152", "uploadSpeed": "0",
        })
        self.assertEqual((s.num_active, s.num_waiting, s.num_stopped), (2, 1, 5))
        self.assertEqual(s.download_speed_string(), "2.0 MiB/s")


class TestRootPaths(unittest.TestCase):
    def test_dedupes_multi_file_torrent_to_top_folder(self):
        files = [
            File.from_struct({"index": "1", "path": "/dl/Show/ep1.mkv", "length": "1"}),
            File.from_struct({"index": "2", "path": "/dl/Show/ep2.mkv", "length": "1"}),
        ]
        self.assertEqual(_root_paths(files, Path("/dl")), [Path("/dl/Show")])

    def test_single_file_returns_itself(self):
        files = [File.from_struct({"index": "1", "path": "/dl/a.mkv", "length": "1"})]
        self.assertEqual(_root_paths(files, Path("/dl")), [Path("/dl/a.mkv")])

    def test_skips_metadata_and_unrelated_paths(self):
        files = [
            File.from_struct({"index": "1", "path": "[METADATA]x.torrent", "length": "0"}),
            File.from_struct({"index": "2", "path": "/other/place/f.bin", "length": "1"}),
        ]
        self.assertEqual(_root_paths(files, Path("/dl")), [])


# ---------------- 依赖 RPC 调用的方法：mock 传输层 ----------------

class Aria2ClientRpcTestCase(unittest.IsolatedAsyncioTestCase):
    def _client_with_mock_rpc(self) -> tuple[Aria2Client, AsyncMock]:
        client = Aria2Client("http://x:6800/jsonrpc", "s")
        mock_call = AsyncMock()
        client._rpc.call = mock_call
        return client, mock_call


class TestAddMethods(Aria2ClientRpcTestCase):
    async def test_add_uri_returns_gid(self):
        client, mock_call = self._client_with_mock_rpc()
        mock_call.return_value = "gid1"
        gid = await client.add_uri("https://x/f.zip", out="f.zip", download_dir="/d")
        self.assertEqual(gid, "gid1")
        mock_call.assert_awaited_once_with("addUri", ["https://x/f.zip"], {"out": "f.zip", "dir": "/d"})

    async def test_add_magnet_uses_addUri_rpc(self):
        client, mock_call = self._client_with_mock_rpc()
        mock_call.return_value = "gid2"
        gid = await client.add_magnet("magnet:?xt=urn:btih:abc", download_dir="/d")
        self.assertEqual(gid, "gid2")
        mock_call.assert_awaited_once_with("addUri", ["magnet:?xt=urn:btih:abc"], {"dir": "/d"})

    async def test_add_torrent_sends_base64_content(self):
        client, mock_call = self._client_with_mock_rpc()
        mock_call.return_value = "gid3"
        with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as f:
            f.write(b"fake torrent bytes")
            path = f.name
        try:
            gid = await client.add_torrent(path, download_dir="/d")
        finally:
            os.unlink(path)
        self.assertEqual(gid, "gid3")
        import base64
        method, content, uris, options = mock_call.await_args.args
        self.assertEqual(method, "addTorrent")
        self.assertEqual(base64.b64decode(content), b"fake torrent bytes")
        self.assertEqual(uris, [])
        self.assertEqual(options, {"dir": "/d"})


class TestRemove(Aria2ClientRpcTestCase):
    async def test_active_download_uses_force_remove(self):
        client, mock_call = self._client_with_mock_rpc()
        status_struct = {
            "gid": "g1", "status": "active", "totalLength": "10", "completedLength": "5",
            "downloadSpeed": "0", "uploadSpeed": "0", "connections": "1", "dir": "/dl",
            "files": [{"index": "1", "path": "/dl/a.mkv", "length": "10"}],
        }

        async def fake_call(method, *params):
            if method == "tellStatus":
                return status_struct
            return "OK"

        mock_call.side_effect = fake_call
        await client.remove("g1", files=False)
        called_methods = [c.args[0] for c in mock_call.await_args_list]
        self.assertIn("forceRemove", called_methods)
        self.assertNotIn("removeDownloadResult", called_methods)

    async def test_terminal_download_falls_back_to_remove_download_result(self):
        from bot.core.aria2_rpc import Aria2RpcError

        client, mock_call = self._client_with_mock_rpc()
        status_struct = {
            "gid": "g1", "status": "complete", "totalLength": "10", "completedLength": "10",
            "downloadSpeed": "0", "uploadSpeed": "0", "connections": "0", "dir": "/dl",
            "files": [{"index": "1", "path": "/dl/a.mkv", "length": "10"}],
        }

        async def fake_call(method, *params):
            if method == "tellStatus":
                return status_struct
            if method == "forceRemove":
                raise Aria2RpcError(1, "not found")
            return "OK"

        mock_call.side_effect = fake_call
        await client.remove("g1", files=False)
        called_methods = [c.args[0] for c in mock_call.await_args_list]
        self.assertIn("removeDownloadResult", called_methods)

    async def test_local_node_deletes_files_when_requested(self):
        client, mock_call = self._client_with_mock_rpc()
        with tempfile.TemporaryDirectory() as d:
            target = os.path.join(d, "a.mkv")
            with open(target, "wb") as f:
                f.write(b"x")
            status_struct = {
                "gid": "g1", "status": "active", "totalLength": "1", "completedLength": "1",
                "downloadSpeed": "0", "uploadSpeed": "0", "connections": "0", "dir": d,
                "files": [{"index": "1", "path": target, "length": "1"}],
            }

            async def fake_call(method, *params):
                return status_struct if method == "tellStatus" else "OK"

            mock_call.side_effect = fake_call
            await client.remove("g1", files=True, is_local=True)
            self.assertFalse(os.path.exists(target))

    async def test_remote_node_never_touches_local_disk_even_with_files_true(self):
        client, mock_call = self._client_with_mock_rpc()
        with tempfile.TemporaryDirectory() as d:
            # 远程节点的 path 恰好在本地也存在同名文件——必须证明它安然无恙，
            # 这正是旧版 aria2p 在远程节点上会误删的场景
            target = os.path.join(d, "a.mkv")
            with open(target, "wb") as f:
                f.write(b"do not delete me")
            status_struct = {
                "gid": "g1", "status": "active", "totalLength": "1", "completedLength": "1",
                "downloadSpeed": "0", "uploadSpeed": "0", "connections": "0", "dir": d,
                "files": [{"index": "1", "path": target, "length": "1"}],
            }

            async def fake_call(method, *params):
                return status_struct if method == "tellStatus" else "OK"

            mock_call.side_effect = fake_call
            await client.remove("g1", files=True, is_local=False)
            self.assertTrue(os.path.exists(target))


class TestGetAllDownloads(Aria2ClientRpcTestCase):
    async def test_combines_active_waiting_stopped(self):
        client, mock_call = self._client_with_mock_rpc()

        def make(gid, status):
            return {
                "gid": gid, "status": status, "totalLength": "1", "completedLength": "0",
                "downloadSpeed": "0", "uploadSpeed": "0", "connections": "0", "dir": "/dl", "files": [],
            }

        async def fake_call(method, *params):
            return {
                "tellActive": [make("a1", "active")],
                "tellWaiting": [make("w1", "waiting")],
                "tellStopped": [make("s1", "complete")],
            }[method]

        mock_call.side_effect = fake_call
        downloads = await client.get_all_downloads()
        self.assertEqual({d.gid for d in downloads}, {"a1", "w1", "s1"})


class TestOptionMethods(Aria2ClientRpcTestCase):
    async def test_get_global_limit_defaults_to_zero(self):
        client, mock_call = self._client_with_mock_rpc()
        mock_call.return_value = {}
        self.assertEqual(await client.get_global_limit(), "0")

    async def test_version(self):
        client, mock_call = self._client_with_mock_rpc()
        mock_call.return_value = {"version": "1.36.0"}
        self.assertEqual(await client.version(), "1.36.0")


class TestSetSelectedFiles(Aria2ClientRpcTestCase):
    async def test_pauses_and_resumes_around_change_when_active(self):
        client, mock_call = self._client_with_mock_rpc()
        status_struct = {
            "gid": "g1", "status": "active", "totalLength": "1", "completedLength": "0",
            "downloadSpeed": "0", "uploadSpeed": "0", "connections": "0", "dir": "/dl", "files": [],
        }

        async def fake_call(method, *params):
            return status_struct if method == "tellStatus" else "OK"

        mock_call.side_effect = fake_call
        await client.set_selected_files("g1", [1, 3])
        methods = [c.args[0] for c in mock_call.await_args_list]
        self.assertEqual(methods, ["tellStatus", "pause", "changeOption", "unpause"])

    async def test_skips_pause_resume_when_not_active(self):
        client, mock_call = self._client_with_mock_rpc()
        status_struct = {
            "gid": "g1", "status": "paused", "totalLength": "1", "completedLength": "0",
            "downloadSpeed": "0", "uploadSpeed": "0", "connections": "0", "dir": "/dl", "files": [],
        }

        async def fake_call(method, *params):
            return status_struct if method == "tellStatus" else "OK"

        mock_call.side_effect = fake_call
        await client.set_selected_files("g1", [1])
        methods = [c.args[0] for c in mock_call.await_args_list]
        self.assertEqual(methods, ["tellStatus", "changeOption"])


if __name__ == "__main__":
    unittest.main()
