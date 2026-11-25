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
        import hashlib
        import hmac
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


class TestCookieSecure(unittest.TestCase):
    """Session cookie Secure flag follows the video-login cookie's rule."""

    def _request(self, scheme="http", headers=None):
        from types import SimpleNamespace

        return SimpleNamespace(url=SimpleNamespace(scheme=scheme), headers=headers or {})

    def test_https_request_is_secure(self):
        from bot.web.app import _cookie_secure

        self.assertTrue(_cookie_secure(self._request("https")))

    def test_plain_http_is_not_secure(self):
        from bot.web.app import _cookie_secure

        self.assertFalse(_cookie_secure(self._request("http")))

    def test_forwarded_proto_only_counts_with_trusted_proxy(self):
        from unittest.mock import patch

        from bot.config import settings
        from bot.web.app import _cookie_secure

        req = self._request("http", {"x-forwarded-proto": "https"})
        with patch.object(settings, "trust_proxy_headers", False):
            self.assertFalse(_cookie_secure(req))
        with patch.object(settings, "trust_proxy_headers", True):
            self.assertTrue(_cookie_secure(req))


class TestSessionCookieAttributes(unittest.IsolatedAsyncioTestCase):
    """登录接口发出的会话 cookie 必须是 HttpOnly + SameSite=Strict
    （Secure 按 _cookie_secure 的规则随 scheme 变）。"""

    async def test_login_cookie_is_strict(self):
        from types import SimpleNamespace
        from unittest.mock import patch

        from fastapi import Response

        from bot.config import settings
        from bot.web import app as web_app

        request = SimpleNamespace(
            url=SimpleNamespace(scheme="http"),
            headers={},
            client=SimpleNamespace(host="127.0.0.1"),
        )
        response = Response()
        with patch.object(settings, "admin_password", "correct-horse"), \
                patch.dict(web_app.state, {"session_secret": "s" * 64}):
            result = await web_app.login(
                web_app.LoginRequest(password="correct-horse"), request, response
            )
        self.assertEqual(result, {"ok": True})
        cookie = response.headers["set-cookie"]
        self.assertIn("HttpOnly", cookie)
        self.assertIn("SameSite=strict", cookie)
        self.assertNotIn("Secure", cookie)  # http 请求不置 Secure（与既有规则一致）


if __name__ == "__main__":
    unittest.main()
