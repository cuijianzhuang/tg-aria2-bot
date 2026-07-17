import unittest

from bot.config import settings
from bot.core.node_pool import Node, NodeUnavailable
from bot.handlers.callbacks import _add_source
from tests.fakes import FakeNodePool


def _remote_pool() -> FakeNodePool:
    return FakeNodePool(extra_nodes=[
        Node(name="nas", rpc_url="http://nas:6800/jsonrpc", secret="s",
             download_dir="/volume1/downloads", is_local=False),
    ])


class TestAddSource(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig_url = settings.bot_api_url
        self._orig_token = settings.bot_token
        settings.bot_api_url = "http://tba:8081"
        settings.bot_token = "123:SECRET"

    def tearDown(self):
        settings.bot_api_url = self._orig_url
        settings.bot_token = self._orig_token

    async def test_tg_media_rebuilds_uri_with_token_at_call_time(self):
        pool = FakeNodePool()
        # payload is the raw Telegram file_path media.py now persists — no
        # token in it; _add_source must stitch the token in itself
        gid = await _add_source(pool, "default", "tg_media", "documents/movie.mkv", "movie.mkv")
        self.assertEqual(gid, "gid-uri")
        uri, out, _ = pool.clients["default"].add_uri_calls[0]
        self.assertEqual(uri, "http://tba:8081/file/bot123:SECRET/documents/movie.mkv")
        self.assertEqual(out, "movie.mkv")

    async def test_tg_media_local_path_becomes_file_uri(self):
        pool = FakeNodePool()
        await _add_source(pool, "default", "tg_media", "/var/lib/tba/documents/movie.mkv", "movie.mkv")
        uri, _, _ = pool.clients["default"].add_uri_calls[0]
        self.assertEqual(uri, "file:///var/lib/tba/documents/movie.mkv")
        self.assertNotIn("SECRET", uri)

    async def test_url_kind_passes_payload_through_unchanged(self):
        pool = FakeNodePool()
        await _add_source(pool, "default", "url", "https://example.com/a.zip", None)
        uri, out, _ = pool.clients["default"].add_uri_calls[0]
        self.assertEqual(uri, "https://example.com/a.zip")
        self.assertIsNone(out)

    async def test_magnet_and_torrent_kinds(self):
        pool = FakeNodePool()
        await _add_source(pool, "default", "magnet", "magnet:?xt=urn:btih:abc", None)
        self.assertEqual(pool.clients["default"].add_magnet_calls[0][0], "magnet:?xt=urn:btih:abc")

        pool = FakeNodePool()
        await _add_source(pool, "default", "torrent", "/data/torrents/x.torrent", None)
        self.assertEqual(pool.clients["default"].add_torrent_calls[0][0], "/data/torrents/x.torrent")

    async def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            await _add_source(FakeNodePool(), "default", "nonsense", "x", None)

    # ---- 多节点路由 ----

    async def test_routes_to_named_node_with_its_download_dir(self):
        pool = _remote_pool()
        await _add_source(pool, "nas", "url", "https://example.com/a.zip", "a.zip")
        self.assertNotIn("default", pool.clients)  # 不应该碰 default 节点
        uri, _, download_dir = pool.clients["nas"].add_uri_calls[0]
        # 远程节点的分类子目录基于它自己的 download_dir 拼出来，且没有在本地创建
        self.assertTrue(download_dir.startswith("/volume1/downloads"))

    async def test_unknown_node_raises_unavailable(self):
        with self.assertRaises(NodeUnavailable):
            await _add_source(FakeNodePool(), "ghost", "url", "https://example.com/a.zip", None)

    async def test_disabled_node_raises_unavailable(self):
        pool = FakeNodePool(extra_nodes=[
            Node(name="off", rpc_url="http://x:6800/jsonrpc", secret="s",
                 download_dir="/d", is_local=False, enabled=False),
        ])
        with self.assertRaises(NodeUnavailable):
            await _add_source(pool, "off", "url", "https://example.com/a.zip", None)


if __name__ == "__main__":
    unittest.main()
