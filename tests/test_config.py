import unittest

from bot.config import settings


class TestDownloadDirOptions(unittest.TestCase):
    def setUp(self):
        self._orig_dir = settings.download_dir
        self._orig_presets = settings.download_dir_presets

    def tearDown(self):
        settings.download_dir = self._orig_dir
        settings.download_dir_presets = self._orig_presets

    def test_current_dir_always_first(self):
        settings.download_dir = "/downloads"
        settings.download_dir_presets = "/data/movies, /data/tv"
        self.assertEqual(
            settings.download_dir_options,
            ["/downloads", "/data/movies", "/data/tv"],
        )

    def test_dedupes_current_from_presets(self):
        settings.download_dir = "/downloads"
        settings.download_dir_presets = "/downloads, /data/movies"
        self.assertEqual(settings.download_dir_options, ["/downloads", "/data/movies"])

    def test_empty_presets_yields_single_option(self):
        settings.download_dir = "/downloads"
        settings.download_dir_presets = ""
        self.assertEqual(settings.download_dir_options, ["/downloads"])


class TestIsAdmin(unittest.TestCase):
    def setUp(self):
        self._orig_allowed = settings.allowed_user_ids
        self._orig_admin = settings.admin_user_ids

    def tearDown(self):
        settings.allowed_user_ids = self._orig_allowed
        settings.admin_user_ids = self._orig_admin

    def test_open_bot_has_no_admins(self):
        settings.allowed_user_ids = ""
        settings.admin_user_ids = ""
        self.assertFalse(settings.is_admin(12345))
        self.assertFalse(settings.is_admin(None))

    def test_falls_back_to_allowed_ids(self):
        settings.allowed_user_ids = "1,2"
        settings.admin_user_ids = ""
        self.assertTrue(settings.is_admin(1))
        self.assertFalse(settings.is_admin(3))

    def test_explicit_admin_ids_override_fallback(self):
        settings.allowed_user_ids = "1,2"
        settings.admin_user_ids = "2"
        self.assertTrue(settings.is_admin(2))
        self.assertFalse(settings.is_admin(1))  # 在白名单里但不在管理员里


if __name__ == "__main__":
    unittest.main()
