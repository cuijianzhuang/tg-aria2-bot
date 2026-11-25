import unittest

from bot.config import settings
from bot.core import rss
from bot.core.download_service import add_source
from bot.core.metalink import split_metalink
from bot.core.url_safety import UnsafeUrlError, check_url, is_blocked_host
from tests.fakes import FakeNodePool


class TestCheckUrl(unittest.TestCase):
    def test_public_urls_pass(self):
        for url in (
            "https://example.com/file.zip",
            "http://downloads.example.org:8080/a/b.iso?token=1",
            "ftp://ftp.example.com/pub/x.tar.gz",
            "sftp://files.example.com/x.bin",
            "https://8.8.8.8/file",
            "https://1.1.1.1/file",
            "http://[2606:4700:4700::1111]/file",
        ):
            with self.subTest(url=url):
                self.assertEqual(check_url(url), url)

    def test_blocked_hosts(self):
        for url in (
            "http://169.254.169.254/latest/meta-data/",
            "http://127.0.0.1:6800/jsonrpc",
            "https://10.0.0.5/file",
            "http://172.16.3.4/file",
            "http://192.168.1.10/file",
            "http://localhost/file",
            "http://localhost:8080/admin",
            "http://nas.localhost/file",
            "http://printer.local/file",
            "http://host.internal/file",
            "http://[::1]/file",
            "http://[fe80::1]/file",
            "http://0.0.0.0/file",
            "http://224.0.0.1/file",
            # inet_aton 风格的字面 IP 编码，aria2/libc 同样会当 IP 用
            "http://2130706433/file",          # 127.0.0.1 的整数写法
            "http://0x7f000001/file",          # 十六进制写法
            "http://127.1/file",               # 缩写写法
        ):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeUrlError):
                    check_url(url)

    def test_blocked_message_is_user_facing_chinese(self):
        with self.assertRaises(UnsafeUrlError) as ctx:
            check_url("http://192.168.0.1/x")
        self.assertIn("内网", str(ctx.exception))

    def test_scheme_restrictions(self):
        for url in ("file:///etc/passwd", "gopher://example.com/x", "magnet:?xt=urn:btih:abc", "//example.com/x"):
            with self.subTest(url=url):
                with self.assertRaises(UnsafeUrlError):
                    check_url(url)

    def test_allow_private_opens_the_gate(self):
        for url in ("http://192.168.1.10/file", "http://127.0.0.1:6800/x", "http://nas.local/file"):
            with self.subTest(url=url):
                self.assertEqual(check_url(url, allow_private=True), url)

    def test_is_blocked_host(self):
        self.assertTrue(is_blocked_host("localhost"))
        self.assertTrue(is_blocked_host("169.254.169.254"))
        self.assertFalse(is_blocked_host("example.com"))
        self.assertTrue(is_blocked_host(""))  # 空主机按不安全处理


class TestRssValidateStillStrict(unittest.TestCase):
    def test_feed_url_ok_and_blocked(self):
        self.assertEqual(rss.validate_url("https://example.com/feed.xml"), "https://example.com/feed.xml")
        for bad in ("http://169.254.169.254/feed", "http://192.168.1.1/feed", "http://[::1]/feed"):
            with self.subTest(bad=bad), self.assertRaises(rss.FeedError):
                rss.validate_url(bad)

    def test_feed_validation_ignores_download_switch(self):
        # ALLOW_PRIVATE_DOWNLOAD_URLS 只放行下载目标，订阅源本身始终拒绝内网
        original = settings.allow_private_download_urls
        settings.allow_private_download_urls = True
        try:
            with self.assertRaises(rss.FeedError):
                rss.validate_url("http://192.168.1.1/feed")
        finally:
            settings.allow_private_download_urls = original


class TestAddSourceEnforcement(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = settings.allow_private_download_urls
        settings.allow_private_download_urls = False

    def tearDown(self):
        settings.allow_private_download_urls = self._orig

    async def test_private_url_rejected_before_rpc_call(self):
        pool = FakeNodePool()
        with self.assertRaises(UnsafeUrlError):
            await add_source(pool, "default", "url", "http://169.254.169.254/latest/meta-data/", None)
        for client in pool.clients.values():
            self.assertEqual(client.add_uri_calls, [])

    async def test_private_url_allowed_when_switch_on(self):
        settings.allow_private_download_urls = True
        pool = FakeNodePool()
        gid = await add_source(pool, "default", "url", "http://192.168.1.10/file.zip", None)
        self.assertEqual(gid, "gid-uri")

    async def test_public_url_unaffected(self):
        pool = FakeNodePool()
        gid = await add_source(pool, "default", "url", "https://example.com/a.zip", None)
        self.assertEqual(gid, "gid-uri")

    async def test_magnet_not_subject_to_url_check(self):
        pool = FakeNodePool()
        gid = await add_source(pool, "default", "magnet", "magnet:?xt=urn:btih:abc", None)
        self.assertTrue(gid)


class TestMetalinkUrlSafety(unittest.TestCase):
    NS = "urn:ietf:params:xml:ns:metalink"

    def _manifest(self, url: str) -> bytes:
        return (
            f'<metalink xmlns="{self.NS}"><file name="a.iso"><url>{url}</url></file></metalink>'
        ).encode()

    def test_private_member_url_rejects_whole_manifest(self):
        with self.assertRaises(ValueError) as ctx:
            split_metalink(self._manifest("http://192.168.1.5/a.iso"))
        self.assertIn("内网", str(ctx.exception))

    def test_metadata_endpoint_rejected(self):
        with self.assertRaises(ValueError):
            split_metalink(self._manifest("http://169.254.169.254/a.iso"))

    def test_private_member_url_allowed_with_switch(self):
        entries = split_metalink(self._manifest("http://192.168.1.5/a.iso"), allow_private=True)
        self.assertEqual([name for name, _ in entries], ["a.iso"])

    def test_public_manifest_unaffected(self):
        entries = split_metalink(self._manifest("https://example.com/a.iso"))
        self.assertEqual(len(entries), 1)


if __name__ == "__main__":
    unittest.main()
