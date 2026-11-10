import unittest

from bot.core.sources import extract_links, parse_link, url_display_name


class TestExtractLinks(unittest.TestCase):
    def test_extracts_urls_and_magnets(self):
        text = "https://example.com/a.zip\nmagnet:?xt=urn:btih:abc123\nhttps://example.com/b.zip"
        links = extract_links(text)
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
        self.assertEqual(extract_links(text), [("url", "https://example.com/a.zip")])

    def test_trims_whitespace_per_line(self):
        text = "  https://example.com/a.zip  \n\thttps://example.com/b.zip\t"
        links = extract_links(text)
        self.assertEqual([link[1] for link in links], ["https://example.com/a.zip", "https://example.com/b.zip"])

    def test_magnet_parameter_order_and_encoded_xt(self):
        for uri in (
            "magnet:?dn=movie&xt=urn:btih:abc123&tr=https%3A%2F%2Ftracker.example",
            "magnet:?xt=urn%3Abtih%3Aabc123&dn=movie",
        ):
            with self.subTest(uri=uri):
                self.assertEqual(extract_links(uri), [("magnet", uri)])

    def test_magnet_in_shared_text_is_routed_as_single(self):
        text = "  下载链接：（MAGNET:?dn=movie&xt=urn:btih:abc123）\n说明文字"
        self.assertEqual(extract_links(text), [("magnet", "magnet:?dn=movie&xt=urn:btih:abc123")])

    def test_multiple_magnets_on_same_line_use_batch(self):
        text = "magnet:?xt=urn:btih:abc magnet:?dn=b&xt=urn:btih:def"
        self.assertEqual(len(extract_links(text)), 2)

    def test_invalid_magnets_are_ignored(self):
        for text in ("magnet:?dn=movie", "magnet:?xt=urn:btih:", "magnet:?xt=other:abc"):
            self.assertEqual(extract_links(text), [])

    def test_aria2_protocols_and_arbitrary_extensions(self):
        for uri in (
            "ftp://example.com/file.iso",
            "sftp://user@example.com/path/file.bin",
            "https://example.com/no-extension",
            "HTTPS://example.com/file.custom",
        ):
            with self.subTest(uri=uri):
                self.assertEqual(extract_links(uri), [("url", uri)])

    def test_no_links_returns_empty(self):
        self.assertEqual(extract_links("just chatting, no links here"), [])

    def test_bare_infohash_converted_to_magnet(self):
        text = "570AB003E4A8B22017DDE562DD2B694349C26645"
        self.assertEqual(
            extract_links(text),
            [("magnet", "magnet:?xt=urn:btih:570AB003E4A8B22017DDE562DD2B694349C26645")],
        )

    def test_lowercase_infohash_also_matches(self):
        text = "570ab003e4a8b22017dde562dd2b694349c26645"
        self.assertEqual(len(extract_links(text)), 1)
        self.assertEqual(extract_links(text)[0][0], "magnet")

    def test_wrong_length_hash_is_not_treated_as_infohash(self):
        self.assertEqual(extract_links("570AB003E4A8B22017DDE562DD2B694349C266"), [])
        self.assertEqual(extract_links("570AB003E4A8B22017DDE562DD2B694349C266450"), [])


class TestUrlDisplayName(unittest.TestCase):
    def test_extracts_filename_from_path(self):
        self.assertEqual(url_display_name("https://example.com/dir/movie.mkv"), "movie.mkv")

    def test_drops_query_string(self):
        self.assertEqual(url_display_name("https://example.com/file.zip?token=abc"), "file.zip")

    def test_falls_back_to_full_url_without_path(self):
        self.assertEqual(url_display_name("https://example.com"), "https://example.com")


class TestParseLink(unittest.TestCase):
    def test_normalizes_supported_single_sources(self):
        uri = "MAGNET:?dn=test&xt=urn%3Abtih%3Aabc"
        self.assertEqual(parse_link(uri), ("magnet", uri.replace("MAGNET:", "magnet:")))
        self.assertEqual(parse_link("a" * 40), ("magnet", "magnet:?xt=urn:btih:" + "a" * 40))
        for uri in ("https://example.com/a", "ftp://example.com/a", "sftp://example.com/a"):
            self.assertEqual(parse_link("  " + uri + "  "), ("url", uri))

    def test_rejects_prose_multiple_sources_and_invalid_magnets(self):
        for text in (
            "下载 magnet:?xt=urn:btih:abc",
            "magnet:?xt=urn:btih:",
            "magnet:?dn=missing-hash",
            "https://a.example https://b.example",
        ):
            self.assertIsNone(parse_link(text))
