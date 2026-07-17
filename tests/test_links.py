import unittest
from types import SimpleNamespace

from bot.handlers.links import _extract_links, _is_multi_link_message, _url_display_name


class TestExtractLinks(unittest.TestCase):
    def test_extracts_urls_and_magnets(self):
        text = "https://example.com/a.zip\nmagnet:?xt=urn:btih:abc123\nhttps://example.com/b.zip"
        links = _extract_links(text)
        self.assertEqual(
            links,
            [
                ("url", "https://example.com/a.zip"),
                ("magnet", "magnet:?xt=urn:btih:abc123"),
                ("url", "https://example.com/b.zip"),
            ],
        )

    def test_ignores_blank_lines_and_garbage_text(self):
        text = "https://example.com/a.zip\n\n这是一段说明文字\nnot a link"
        self.assertEqual(_extract_links(text), [("url", "https://example.com/a.zip")])

    def test_trims_whitespace_per_line(self):
        text = "  https://example.com/a.zip  \n\thttps://example.com/b.zip\t"
        links = _extract_links(text)
        self.assertEqual([l[1] for l in links], ["https://example.com/a.zip", "https://example.com/b.zip"])

    def test_no_links_returns_empty(self):
        self.assertEqual(_extract_links("just chatting, no links here"), [])


class TestIsMultiLinkMessage(unittest.TestCase):
    def _msg(self, text):
        return SimpleNamespace(text=text)

    def test_two_or_more_links_is_batch(self):
        text = "https://example.com/a.zip\nhttps://example.com/b.zip"
        self.assertTrue(_is_multi_link_message(self._msg(text)))

    def test_single_link_is_not_batch(self):
        # 单条链接交给上面精确匹配的单行 handler，不进批量流程
        self.assertFalse(_is_multi_link_message(self._msg("https://example.com/a.zip")))

    def test_plain_text_is_not_batch(self):
        self.assertFalse(_is_multi_link_message(self._msg("你好，在吗？")))

    def test_no_text_is_not_batch(self):
        self.assertFalse(_is_multi_link_message(self._msg(None)))


class TestUrlDisplayName(unittest.TestCase):
    def test_extracts_filename_from_path(self):
        self.assertEqual(_url_display_name("https://example.com/dir/movie.mkv"), "movie.mkv")

    def test_drops_query_string(self):
        self.assertEqual(_url_display_name("https://example.com/file.zip?token=abc"), "file.zip")

    def test_falls_back_to_full_url_without_path(self):
        self.assertEqual(_url_display_name("https://example.com"), "https://example.com")


if __name__ == "__main__":
    unittest.main()
