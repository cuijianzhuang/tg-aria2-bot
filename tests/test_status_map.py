"""第 4 批去重后的单一来源断言：aria2 状态映射、列表 tab 映射、
_multi_file 收编、storage 磁盘下限可注入。"""
import os
import unittest
from types import SimpleNamespace
from unittest.mock import patch

from bot.core import storage
from bot.core.aria2_client import multi_file
from bot.core.list_view import LIST_STATUS_MAP
from bot.db.models import STATUSES, map_aria2_status


class TestMapAria2Status(unittest.TestCase):
    def test_known_statuses(self):
        self.assertEqual(map_aria2_status("complete"), "COMPLETED")
        self.assertEqual(map_aria2_status("error"), "FAILED")
        self.assertEqual(map_aria2_status("active"), "ACTIVE")
        self.assertEqual(map_aria2_status("paused"), "PAUSED")
        self.assertEqual(map_aria2_status("waiting"), "PENDING")

    def test_case_insensitive_and_unmapped(self):
        self.assertEqual(map_aria2_status("COMPLETE"), "COMPLETED")
        self.assertIsNone(map_aria2_status("removed"))  # 故意不映射，见 models 注释
        self.assertIsNone(map_aria2_status("bogus"))
        self.assertIsNone(map_aria2_status(None))
        self.assertIsNone(map_aria2_status(""))

    def test_all_mapped_values_are_valid_statuses(self):
        for raw in ("complete", "error", "active", "paused", "waiting"):
            self.assertIn(map_aria2_status(raw), STATUSES)


class TestListStatusMap(unittest.TestCase):
    def test_derived_from_statuses(self):
        self.assertIsNone(LIST_STATUS_MAP["ALL"])
        self.assertEqual(set(LIST_STATUS_MAP) - {"ALL"}, set(STATUSES))
        for key, value in LIST_STATUS_MAP.items():
            if key != "ALL":
                self.assertEqual(key, value)


def _download(*, files):
    return SimpleNamespace(files=[SimpleNamespace(is_metadata=m) for m in files])


class TestMultiFile(unittest.TestCase):
    def test_none_download(self):
        self.assertIsNone(multi_file(None))

    def test_single_and_multi(self):
        self.assertFalse(multi_file(_download(files=[False])))
        self.assertTrue(multi_file(_download(files=[False, False])))

    def test_metadata_only_is_unknown(self):
        self.assertIsNone(multi_file(_download(files=[True])))
        self.assertFalse(multi_file(_download(files=[True, False])))


class TestMinFreeBytes(unittest.TestCase):
    def test_default_is_constant(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TG_ARIA2_MIN_FREE_BYTES", None)
            self.assertEqual(storage.min_free_bytes(), storage.MIN_FREE_BYTES)

    def test_env_override(self):
        with patch.dict(os.environ, {"TG_ARIA2_MIN_FREE_BYTES": "0"}):
            self.assertEqual(storage.min_free_bytes(), 0)
        with patch.dict(os.environ, {"TG_ARIA2_MIN_FREE_BYTES": "12345"}):
            self.assertEqual(storage.min_free_bytes(), 12345)

    def test_invalid_env_falls_back(self):
        with patch.dict(os.environ, {"TG_ARIA2_MIN_FREE_BYTES": "abc"}):
            self.assertEqual(storage.min_free_bytes(), storage.MIN_FREE_BYTES)


if __name__ == "__main__":
    unittest.main()
