"""ASGI-level tests of personal QR scopes, admin boundaries and replay protection."""

import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fastapi import FastAPI, HTTPException, Request

from bot.config import settings
from bot.db.repo import TaskRepo
from bot.web.video import register


async def request(app, path, method="GET", body=None, headers=None):
    events = []
    sent = False

    async def receive():
        nonlocal sent
        if sent:
            await asyncio.Future()
        sent = True
        return {
            "type": "http.request",
            "body": json.dumps(body).encode() if body is not None else b"",
            "more_body": False,
        }

    async def send(event):
        events.append(event)

    encoded = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    encoded += [(b"host", b"testserver"), (b"content-type", b"application/json")]
    await app(
        {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": method,
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": encoded,
            "client": ("127.0.0.1", 1),
            "server": ("testserver", 80),
        },
        receive,
        send,
    )
    status = next(e["status"] for e in events if e["type"] == "http.response.start")
    result = b"".join(e.get("body", b"") for e in events if e["type"] == "http.response.body")
    response_headers = next(e["headers"] for e in events if e["type"] == "http.response.start")
    return status, json.loads(result or "{}"), dict(response_headers)


class TestVideoWeb(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.repo = TaskRepo(str(Path(self.directory.name) / "tasks.db"))
        await self.repo.connect()
        self.setting = patch.object(settings, "video_login_enabled", True)
        self.setting.start()
        self.app = FastAPI()

        def admin(request: Request):
            if request.headers.get("x-test-admin") != "yes":
                raise HTTPException(401, "not admin")

        register(self.app, {"repo": self.repo}, admin)

    async def asyncTearDown(self):
        self.setting.stop()
        await self.repo.close()
        self.directory.cleanup()

    async def session(self):
        login = await self.repo.video_new_login("bilibili", 123, 123)
        token = await self.repo.video_grant(login)
        status, _, headers = await request(self.app, "/api/video-login/exchange", "POST", {"token": token})
        self.assertEqual(status, 200)
        return login, token, headers[b"set-cookie"].decode().split(";", 1)[0]

    async def test_personal_session_cannot_access_admin_endpoints(self):
        _, _, cookie = await self.session()
        status, data, _ = await request(self.app, "/api/video-login/status", headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual(data["platform"], "bilibili")
        self.assertNotIn("cookies", data)
        self.assertEqual((await request(self.app, "/api/video/accounts", headers={"Cookie": cookie}))[0], 401)

    async def test_link_replay_and_forged_session_are_rejected(self):
        _, token, _ = await self.session()
        self.assertEqual((await request(self.app, "/api/video-login/exchange", "POST", {"token": token}))[0], 401)
        self.assertEqual(
            (await request(self.app, "/api/video-login/status", headers={"Cookie": "video_login_session=forged"}))[0],
            401,
        )

    async def test_refresh_only_rebinds_callers_session(self):
        login, _, cookie = await self.session()
        unrelated = await self.repo.video_new_login("xhs", 999, 999)
        status, _, _ = await request(self.app, "/api/video-login/refresh", "POST", {}, headers={"Cookie": cookie})
        self.assertEqual(status, 200)
        self.assertEqual((await self.repo.video_login(login))["state"], "cancelled")
        self.assertEqual((await self.repo.video_login(unrelated))["state"], "queued")
        _, data, _ = await request(self.app, "/api/video-login/status", headers={"Cookie": cookie})
        self.assertEqual(data["platform"], "bilibili")

    async def test_cross_origin_mutations_are_rejected(self):
        _, _, cookie = await self.session()
        self.assertEqual(
            (
                await request(
                    self.app,
                    "/api/video-login/cancel",
                    "POST",
                    {},
                    headers={"Cookie": cookie, "Origin": "https://other.example"},
                )
            )[0],
            403,
        )

    async def test_shared_account_ui_never_returns_credentials(self):
        await self.repo.video_put_account("xhs", 0, "test-account", b"encrypted-secret")
        status, data, _ = await request(self.app, "/api/video/accounts", headers={"X-Test-Admin": "yes"})
        self.assertEqual(status, 200)
        self.assertNotIn("encrypted-secret", json.dumps(data))
