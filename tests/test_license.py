"""授权码：签名校验、绑定 Bot ID、过期、篡改、激活命令、签发工具。"""
import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from bot.config import settings
from bot.core import license as lic
from bot.handlers import vip as vip_handler

TOOL = Path(__file__).resolve().parents[1] / "tools" / "license.py"


def b64e(raw):
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


class LicenseBase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.key = Ed25519PrivateKey.generate()
        pub = self.key.public_key().public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
        self._orig = (settings.db_path, settings.license_key, settings.bot_token, lic.PUBLIC_KEY_B64)
        settings.db_path = os.path.join(self._dir.name, "tasks.db")
        settings.license_key = ""
        settings.bot_token = "123456:ABC"
        lic.PUBLIC_KEY_B64 = b64e(pub)
        self.addCleanup(self._restore)

    def _restore(self):
        settings.db_path, settings.license_key, settings.bot_token, lic.PUBLIC_KEY_B64 = self._orig

    def token(self, **over):
        data = {"v": 1, "name": "张三", "bot_id": "123456", "iat": int(time.time()), "exp": int(time.time()) + 86400,
                "max_vip": 5}
        data.update(over)
        raw = json.dumps(data, ensure_ascii=False, separators=(",", ":")).encode()
        return f"TGA2.{b64e(raw)}.{b64e(self.key.sign(raw))}"


class TestVerify(LicenseBase):
    def test_valid(self):
        info = lic.verify(self.token())
        self.assertEqual((info.name, info.bot_id, info.max_vip), ("张三", "123456", 5))
        self.assertGreater(info.days_left, 0.9)

    def test_wildcard_bot_id_and_permanent(self):
        info = lic.verify(self.token(bot_id="*", exp=0))
        self.assertIsNone(info.days_left)

    def test_wrong_bot_is_refused(self):
        with self.assertRaises(lic.LicenseError) as cm:
            lic.verify(self.token(bot_id="999"))
        self.assertIn("别的机器人", str(cm.exception))

    def test_expired(self):
        with self.assertRaises(lic.LicenseError) as cm:
            lic.verify(self.token(exp=int(time.time()) - 10))
        self.assertIn("过期", str(cm.exception))

    def test_tampered_payload_is_rejected(self):
        prefix, payload, sig = self.token(max_vip=5).split(".")
        forged = json.loads(base64.urlsafe_b64decode(payload + "=="))
        forged["max_vip"] = 0           # 想改成不限人数
        raw = json.dumps(forged, ensure_ascii=False, separators=(",", ":")).encode()
        with self.assertRaises(lic.LicenseError) as cm:
            lic.verify(f"{prefix}.{b64e(raw)}.{sig}")
        self.assertIn("签名无效", str(cm.exception))

    def test_signed_by_someone_else_is_rejected(self):
        other = Ed25519PrivateKey.generate()
        raw = json.dumps({"v": 1, "name": "x", "bot_id": "*", "exp": 0}, separators=(",", ":")).encode()
        with self.assertRaises(lic.LicenseError):
            lic.verify(f"TGA2.{b64e(raw)}.{b64e(other.sign(raw))}")

    def test_garbage(self):
        for bad in ("", "abc", "TGA2.x", "TGA2.!!!.???", "XXXX.a.b"):
            with self.assertRaises(lic.LicenseError, msg=bad):
                lic.verify(bad)

    def test_unknown_version(self):
        with self.assertRaises(lic.LicenseError):
            lic.verify(self.token(v=2))

    def test_not_enforced_means_no_verification(self):
        lic.PUBLIC_KEY_B64 = ""
        self.assertFalse(lic.enforced())
        self.assertTrue(lic.pro_active())            # 没启用授权：功能全开
        self.assertIsNone(lic.current())


class TestStorage(LicenseBase):
    def test_no_license_is_not_pro(self):
        self.assertFalse(lic.pro_active())
        self.assertIn("未授权", lic.status_text())

    def test_saved_token_activates(self):
        lic.save_token(self.token())
        self.assertTrue(lic.pro_active())
        self.assertEqual(oct(lic.key_path().stat().st_mode & 0o777), "0o600")
        self.assertIn("Pro 已授权", lic.status_text())

    def test_env_takes_precedence_over_file(self):
        lic.save_token("garbage")
        settings.license_key = self.token()
        self.assertTrue(lic.pro_active())

    def test_expiry_takes_effect_without_restart(self):
        lic.save_token(self.token())
        self.assertTrue(lic.pro_active())
        with patch("time.time", return_value=time.time() + 3 * 86400):
            self.assertFalse(lic.pro_active())

    def test_invalid_stored_token_is_reported(self):
        lic.save_token("TGA2.bad.bad")
        self.assertFalse(lic.pro_active())
        self.assertIn("授权无效", lic.status_text())


class FakeMsg:
    def __init__(self, user_id=1):
        self.from_user = SimpleNamespace(id=user_id)
        self.replies, self.deleted = [], False
        self.bot = None

    async def reply(self, text, **kw):
        self.replies.append(text)

    answer = reply

    async def delete(self):
        self.deleted = True


class TestCommand(LicenseBase, unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        LicenseBase.setUp(self)
        self._ids = (settings.admin_user_ids, settings.allowed_user_ids)
        settings.admin_user_ids = "1"
        settings.allowed_user_ids = "1,2"
        self.addCleanup(lambda: (setattr(settings, "admin_user_ids", self._ids[0]),
                                 setattr(settings, "allowed_user_ids", self._ids[1])))

    async def test_shows_bot_id_for_the_buyer_to_send_to_the_issuer(self):
        m = FakeMsg()
        await vip_handler.cmd_license(m, SimpleNamespace(args=None))
        self.assertIn("123456", m.replies[0])

    async def test_activates_with_a_valid_token_and_deletes_the_message(self):
        m = FakeMsg()
        await vip_handler.cmd_license(m, SimpleNamespace(args=self.token()))
        self.assertTrue(lic.pro_active())
        self.assertTrue(m.deleted)
        self.assertIn("已激活", m.replies[0])

    async def test_bad_token_changes_nothing(self):
        m = FakeMsg()
        await vip_handler.cmd_license(m, SimpleNamespace(args=self.token(bot_id="999")))
        self.assertFalse(lic.pro_active())
        self.assertFalse(lic.key_path().exists())

    async def test_non_admin_cannot_activate(self):
        m = FakeMsg(user_id=2)
        await vip_handler.cmd_license(m, SimpleNamespace(args=self.token()))
        self.assertFalse(lic.pro_active())
        self.assertIn("只有管理员", m.replies[0])


class TestTool(unittest.TestCase):
    """发行方工具：keygen → issue → 机器人能验证。"""

    def _run(self, *args):
        return subprocess.run([sys.executable, str(TOOL), *args], capture_output=True, text=True, timeout=60)

    def test_keygen_issue_and_verify_round_trip(self):
        with tempfile.TemporaryDirectory() as d:
            keyfile = os.path.join(d, "k.key")
            r = self._run("keygen", "--out", keyfile)
            self.assertEqual(r.returncode, 0, r.stderr)
            pub = r.stdout.strip().splitlines()[-1]
            self.assertEqual(oct(os.stat(keyfile).st_mode & 0o777), "0o600")
            self.assertNotEqual(self._run("keygen", "--out", keyfile).returncode, 0)     # 不覆盖已有私钥
            tok = self._run("issue", "--key", keyfile, "--bot-id", "42", "--name", "李四", "--days", "30", "--max-vip", "3")
            self.assertEqual(tok.returncode, 0, tok.stderr)
            old = (lic.PUBLIC_KEY_B64, settings.bot_token)
            self.addCleanup(lambda: (setattr(lic, "PUBLIC_KEY_B64", old[0]), setattr(settings, "bot_token", old[1])))
            lic.PUBLIC_KEY_B64, settings.bot_token = pub, "42:XYZ"
            info = lic.verify(tok.stdout.strip())
            self.assertEqual((info.name, info.max_vip), ("李四", 3))
            self.assertIn("name: 李四", self._run("inspect", tok.stdout.strip()).stdout)

    def test_issue_rejects_bad_input(self):
        with tempfile.TemporaryDirectory() as d:
            keyfile = os.path.join(d, "k.key")
            self._run("keygen", "--out", keyfile)
            self.assertNotEqual(self._run("issue", "--key", keyfile, "--bot-id", "abc", "--name", "x").returncode, 0)
            self.assertNotEqual(self._run("issue", "--key", keyfile, "--bot-id", "1", "--name", "x", "--days", "-1").returncode, 0)

    def test_shipped_public_key_is_empty_and_private_keys_are_ignored(self):
        src = (Path(__file__).resolve().parents[1] / "bot" / "core" / "license.py").read_text()
        self.assertIn('\nPUBLIC_KEY_B64 = ""\n', src)       # 源码默认不启用授权校验，没有夹带任何密钥
        gi = (Path(__file__).resolve().parents[1] / ".gitignore").read_text()
        self.assertIn("*.key", gi)


if __name__ == "__main__":
    unittest.main()
