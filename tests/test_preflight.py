import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

from bot import preflight


class TestPreflight(unittest.IsolatedAsyncioTestCase):
    async def test_permission_error_is_actionable(self):
        with patch("tempfile.TemporaryFile", side_effect=PermissionError), \
                patch.object(Path, "mkdir"):
            with self.assertRaisesRegex(RuntimeError, "UID/GID 1000"):
                await preflight.check()

    def test_invalid_settings_do_not_leak_values_or_traceback(self):
        result = subprocess.run(
            [sys.executable, "-m", "bot.preflight"],
            env={**os.environ, "API_ID": "sensitive-invalid-value"},
            capture_output=True, text=True, timeout=15,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("api_id", result.stdout)
        self.assertNotIn("sensitive-invalid-value", result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stdout + result.stderr)
