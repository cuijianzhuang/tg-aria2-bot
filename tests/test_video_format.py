import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from bot.config import settings
from bot.video.backends import download_args


class TestVideoOutputFormat(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.dir_path = Path(self.directory.name)

    def tearDown(self):
        self.directory.cleanup()

    def test_default_mp4_format(self):
        args = download_args("yt-dlp", "https://example.com/video", self.dir_path)
        self.assertIn("--merge-output-format", args)
        self.assertEqual(args[args.index("--merge-output-format") + 1], "mp4")

    def test_explicit_mkv_format(self):
        with patch.object(settings, "video_output_format", "mkv"):
            args = download_args("yt-dlp", "https://example.com/video", self.dir_path)
            self.assertIn("--merge-output-format", args)
            self.assertEqual(args[args.index("--merge-output-format") + 1], "mkv")

    def test_auto_format_omits_merge_flag(self):
        with patch.object(settings, "video_output_format", "auto"):
            args = download_args("yt-dlp", "https://example.com/video", self.dir_path)
            self.assertNotIn("--merge-output-format", args)

    def test_uppercase_format_normalized(self):
        with patch.object(settings, "video_output_format", "MP4"):
            args = download_args("yt-dlp", "https://example.com/video", self.dir_path)
            self.assertEqual(args[args.index("--merge-output-format") + 1], "mp4")

    def test_you_get_backend_unaffected(self):
        args = download_args("you-get", "https://example.com/video", self.dir_path)
        self.assertNotIn("--merge-output-format", args)


if __name__ == "__main__":
    unittest.main()
