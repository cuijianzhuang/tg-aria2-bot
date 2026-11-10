import unittest
from types import SimpleNamespace

from bot.handlers.links import _is_multi_link_message, _is_single_magnet_message


class TestIsMultiLinkMessage(unittest.TestCase):
    def _msg(self, text):
        return SimpleNamespace(text=text)

    def test_two_or_more_links_is_batch(self):
        text = "https://example.com/a.zip\nhttps://example.com/b.zip"
        self.assertTrue(_is_multi_link_message(self._msg(text)))

    def test_single_link_is_not_batch(self):
        # 单条链接交给上面精确匹配的单行 handler，不进批量流程
        self.assertFalse(_is_multi_link_message(self._msg("https://example.com/a.zip")))

    def test_single_infohash_is_not_batch(self):
        self.assertFalse(_is_multi_link_message(self._msg("570AB003E4A8B22017DDE562DD2B694349C26645")))

    def test_url_and_infohash_together_is_batch(self):
        text = "https://example.com/a.zip\n570AB003E4A8B22017DDE562DD2B694349C26645"
        self.assertTrue(_is_multi_link_message(self._msg(text)))

    def test_plain_text_is_not_batch(self):
        self.assertFalse(_is_multi_link_message(self._msg("你好，在吗？")))

    def test_no_text_is_not_batch(self):
        self.assertFalse(_is_multi_link_message(self._msg(None)))

    def test_magnet_in_prose_is_single(self):
        self.assertTrue(_is_single_magnet_message(self._msg("下载：（MAGNET:?dn=a&xt=urn:btih:abc）")))

    def test_multiple_magnets_use_batch(self):
        message = self._msg("magnet:?xt=urn:btih:abc magnet:?dn=b&xt=urn:btih:def")
        self.assertTrue(_is_multi_link_message(message))
        self.assertFalse(_is_single_magnet_message(message))
