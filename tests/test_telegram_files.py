import unittest

from bot.config import settings
from bot.core.telegram_files import to_download_uri, to_local_path


class TestToDownloadUri(unittest.TestCase):
    def test_absolute_path_becomes_file_uri(self):
        # --local 模式下 telegram-bot-api 返回的 file_path 已经是本地绝对路径
        self.assertEqual(to_download_uri("/var/lib/tba/documents/x.zip"), "file:///var/lib/tba/documents/x.zip")

    def test_relative_path_gets_bot_api_url_and_token(self):
        original_url, original_token = settings.bot_api_url, settings.bot_token
        try:
            settings.bot_api_url = "http://tba:8081"
            settings.bot_token = "123:SECRET"
            uri = to_download_uri("documents/x.zip")
            self.assertEqual(uri, "http://tba:8081/file/bot123:SECRET/documents/x.zip")
        finally:
            settings.bot_api_url = original_url
            settings.bot_token = original_token


class TestToLocalPath(unittest.TestCase):
    def test_absolute_path_returned_as_is(self):
        self.assertEqual(to_local_path("/var/lib/tba/x.torrent"), "/var/lib/tba/x.torrent")

    def test_relative_path_returns_none(self):
        self.assertIsNone(to_local_path("documents/x.torrent"))


if __name__ == "__main__":
    unittest.main()
