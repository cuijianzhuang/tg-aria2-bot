import os
import tempfile
import unittest

from bot.core.conf_editor import is_safe_value, list_rclone_remotes, read_kv, write_kv


class TestSafeValue(unittest.TestCase):
    def test_allows_conservative_charset(self):
        for ok in ("", "gdrive", "My Drive/sub-dir", "a_b.c-1/2"):
            self.assertTrue(is_safe_value(ok), ok)

    def test_rejects_shell_metacharacters(self):
        for bad in ("$(reboot)", "`id`", "a;b", 'a"b', "a'b", "a\nb", "a&b"):
            self.assertFalse(is_safe_value(bad), bad)


class TestWriteKv(unittest.TestCase):
    def _tmp(self, content: str) -> str:
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as f:
            f.write(content)
        self.addCleanup(os.unlink, path)
        return path

    def test_updates_existing_key(self):
        path = self._tmp("key=old\nother=1\n")
        write_kv(path, "key", "new")
        self.assertEqual(read_kv(path, "key"), "new")
        self.assertEqual(read_kv(path, "other"), "1")

    def test_uncomments_commented_key(self):
        path = self._tmp("# docs\n#key=\n")
        write_kv(path, "key", "v")
        self.assertEqual(read_kv(path, "key"), "v")

    def test_appends_missing_key(self):
        path = self._tmp("a=1\n")
        write_kv(path, "b", "2")
        self.assertEqual(read_kv(path, "b"), "2")

    def test_none_comments_key_out(self):
        path = self._tmp("key=v\n")
        write_kv(path, "key", None)
        self.assertIsNone(read_kv(path, "key"))
        with open(path) as f:
            self.assertIn("#key=", f.read())

    def test_no_leftover_temp_files(self):
        path = self._tmp("a=1\n")
        write_kv(path, "a", "2")
        leftovers = [n for n in os.listdir(os.path.dirname(path)) if n.startswith(".conf_tmp_")]
        self.assertEqual(leftovers, [])


class TestRcloneRemotes(unittest.TestCase):
    def test_parses_section_headers(self):
        fd, path = tempfile.mkstemp()
        with os.fdopen(fd, "w") as f:
            f.write("[gdrive]\ntype = drive\n\n[onedrive]\ntype = onedrive\n")
        self.addCleanup(os.unlink, path)
        self.assertEqual(list_rclone_remotes(path), ["gdrive", "onedrive"])

    def test_missing_file_is_empty(self):
        self.assertEqual(list_rclone_remotes("/nonexistent/rclone.conf"), [])


if __name__ == "__main__":
    unittest.main()
