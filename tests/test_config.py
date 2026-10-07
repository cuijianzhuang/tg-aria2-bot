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


class TestScopeFor(unittest.TestCase):
    def setUp(self):
        self._orig_allowed = settings.allowed_user_ids
        self._orig_admin = settings.admin_user_ids

    def tearDown(self):
        settings.allowed_user_ids = self._orig_allowed
        settings.admin_user_ids = self._orig_admin

    def test_admin_sees_all(self):
        settings.allowed_user_ids = ""
        settings.admin_user_ids = "1"
        self.assertIsNone(settings.scope_for(1))

    def test_regular_user_scoped_to_self(self):
        settings.allowed_user_ids = ""
        settings.admin_user_ids = "1"
        self.assertEqual(settings.scope_for(2), 2)

    def test_none_user_id_stays_none(self):
        settings.allowed_user_ids = ""
        settings.admin_user_ids = ""
        self.assertIsNone(settings.scope_for(None))


class TestUnknownEnvKeys(unittest.TestCase):
    def test_unknown_keys_in_env_file_are_ignored(self):
        # 升级/回滚后 .env 里常会多出当前版本不认识的键（COMPOSE_PROFILES、
        # HOST_DOWNLOAD_DIR、新版本的配置项），不能因此启动即崩
        import os
        import tempfile

        from bot.config import Settings

        with tempfile.TemporaryDirectory() as d:
            env_path = os.path.join(d, ".env")
            with open(env_path, "w") as f:
                f.write("COMPOSE_PROFILES=web\nHOST_DOWNLOAD_DIR=/srv/dl\nSOME_FUTURE_OPTION=1\n")
            s = Settings(_env_file=env_path)
        self.assertFalse(hasattr(s, "compose_profiles"))


if __name__ == "__main__":
    unittest.main()
