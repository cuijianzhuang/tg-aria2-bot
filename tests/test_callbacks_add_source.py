import unittest

from bot.config import settings
from bot.handlers.callbacks import _add_source


class FakeAria2:
    def __init__(self):
        self.add_uri_calls = []
        self.add_magnet_calls = []
        self.add_torrent_calls = []

    async def add_uri(self, uri, *, out=None, download_dir=None):
        self.add_uri_calls.append((uri, out, download_dir))
        return "gid-uri"

    async def add_magnet(self, magnet, *, download_dir=None):
        self.add_magnet_calls.append((magnet, download_dir))
        return "gid-magnet"

    async def add_torrent(self, path, *, download_dir=None):
        self.add_torrent_calls.append((path, download_dir))
        return "gid-torrent"


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
        aria2 = FakeAria2()
        # payload is the raw Telegram file_path media.py now persists — no
        # token in it; _add_source must stitch the token in itself
        gid = await _add_source(aria2, "tg_media", "documents/movie.mkv", "movie.mkv")
        self.assertEqual(gid, "gid-uri")
        uri, out, _ = aria2.add_uri_calls[0]
        self.assertEqual(uri, "http://tba:8081/file/bot123:SECRET/documents/movie.mkv")
        self.assertEqual(out, "movie.mkv")

    async def test_tg_media_local_path_becomes_file_uri(self):
        aria2 = FakeAria2()
        await _add_source(aria2, "tg_media", "/var/lib/tba/documents/movie.mkv", "movie.mkv")
        uri, _, _ = aria2.add_uri_calls[0]
        self.assertEqual(uri, "file:///var/lib/tba/documents/movie.mkv")
        self.assertNotIn("SECRET", uri)

    async def test_url_kind_passes_payload_through_unchanged(self):
        aria2 = FakeAria2()
        await _add_source(aria2, "url", "https://example.com/a.zip", None)
        uri, out, _ = aria2.add_uri_calls[0]
        self.assertEqual(uri, "https://example.com/a.zip")
        self.assertIsNone(out)

    async def test_magnet_and_torrent_kinds(self):
        aria2 = FakeAria2()
        await _add_source(aria2, "magnet", "magnet:?xt=urn:btih:abc", None)
        self.assertEqual(aria2.add_magnet_calls[0][0], "magnet:?xt=urn:btih:abc")

        aria2 = FakeAria2()
        await _add_source(aria2, "torrent", "/data/torrents/x.torrent", None)
        self.assertEqual(aria2.add_torrent_calls[0][0], "/data/torrents/x.torrent")

    async def test_unknown_kind_raises(self):
        with self.assertRaises(ValueError):
            await _add_source(FakeAria2(), "nonsense", "x", None)


if __name__ == "__main__":
    unittest.main()
