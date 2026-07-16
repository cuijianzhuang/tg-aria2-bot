import os
import tempfile
import time
import unittest

from bot.web.auth import (
    create_session_token,
    load_or_create_secret,
    rotate_secret,
    verify_session_token,
)


class TestSessionTokens(unittest.TestCase):
    SECRET = "a" * 64

    def test_roundtrip(self):
        token = create_session_token(self.SECRET)
        self.assertTrue(verify_session_token(self.SECRET, token))

    def test_rejects_wrong_secret(self):
        token = create_session_token(self.SECRET)
        self.assertFalse(verify_session_token("b" * 64, token))

    def test_rejects_tampered_expiry(self):
        token = create_session_token(self.SECRET)
        expiry, sig = token.split(".", 1)
        forged = f"{int(expiry) + 999999}.{sig}"
        self.assertFalse(verify_session_token(self.SECRET, forged))

    def test_rejects_garbage(self):
        for bad in (None, "", "x", "notdigits.abc", "123"):
            self.assertFalse(verify_session_token(self.SECRET, bad))

    def test_rejects_expired(self):
        expired = str(int(time.time()) - 10)
        import hmac, hashlib
        sig = hmac.new(self.SECRET.encode(), expired.encode(), hashlib.sha256).hexdigest()
        self.assertFalse(verify_session_token(self.SECRET, f"{expired}.{sig}"))


class TestSecretPersistence(unittest.TestCase):
    def test_load_creates_then_reuses(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "secret")
            first = load_or_create_secret(path)
            self.assertEqual(len(first), 64)
            self.assertEqual(load_or_create_secret(path), first)

    def test_rotate_invalidates_old_tokens(self):
        with tempfile.TemporaryDirectory() as d:
            path = os.path.join(d, "secret")
            old = load_or_create_secret(path)
            token = create_session_token(old)
            new = rotate_secret(path)
            self.assertNotEqual(old, new)
            self.assertFalse(verify_session_token(new, token))


if __name__ == "__main__":
    unittest.main()
