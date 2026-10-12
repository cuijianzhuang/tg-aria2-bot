"""core/notify：给 Web 后台用的 sendMessage。"""
import logging
import unittest
from unittest.mock import AsyncMock, patch

import aiohttp

from bot.config import settings
from bot.core import notify


class TestSendMessage(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._orig = (settings.bot_token, settings.bot_api_url)
        settings.bot_token = "123456:SECRET-TOKEN"
        settings.bot_api_url = "http://127.0.0.1:8081"
        self.addCleanup(lambda: (setattr(settings, "bot_token", self._orig[0]), setattr(settings, "bot_api_url", self._orig[1])))

    async def test_posts_to_the_local_bot_api(self):
        post = AsyncMock(return_value=200)
        with patch.object(notify, "post_json", post):
            self.assertTrue(await notify.send_message(5, "hi"))
        url, payload = post.await_args.args
        self.assertEqual(url, "http://127.0.0.1:8081/bot123456:SECRET-TOKEN/sendMessage")
        self.assertEqual(payload, {"chat_id": 5, "text": "hi"})

    async def test_non_200_is_false(self):
        with patch.object(notify, "post_json", AsyncMock(return_value=403)):
            self.assertFalse(await notify.send_message(5, "hi"))

    async def test_network_errors_are_false_not_exceptions(self):
        for exc in (aiohttp.ClientConnectionError("boom"), TimeoutError()):
            with patch.object(notify, "post_json", AsyncMock(side_effect=exc)):
                self.assertFalse(await notify.send_message(5, "hi"))

    async def test_the_token_never_reaches_the_log(self):
        err = aiohttp.ClientConnectionError("Cannot connect to host 127.0.0.1:8081 /bot123456:SECRET-TOKEN/sendMessage")
        for status_or_exc in (err, 500):
            mock = AsyncMock(side_effect=err) if status_or_exc is err else AsyncMock(return_value=500)
            with patch.object(notify, "post_json", mock), self.assertLogs("bot.core.notify", logging.INFO) as logs:
                await notify.send_message(5, "hi")
            self.assertNotIn("SECRET-TOKEN", "\n".join(logs.output))


class TestRealHttp(unittest.IsolatedAsyncioTestCase):
    """不 mock 网络层：起一个假的 Bot API 服务，确认真的发出了正确的请求。"""

    async def asyncSetUp(self):
        from aiohttp import web
        self.requests = []
        self.status = 200

        async def handler(request):
            self.requests.append((request.path, await request.json()))
            return web.json_response({"ok": self.status == 200}, status=self.status)

        app = web.Application()
        app.router.add_post("/bot{token}/sendMessage", handler)
        self.runner = web.AppRunner(app)
        await self.runner.setup()
        site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        self._orig = (settings.bot_token, settings.bot_api_url)
        settings.bot_token, settings.bot_api_url = "42:TOK", f"http://127.0.0.1:{port}"
        self.addCleanup(lambda: (setattr(settings, "bot_token", self._orig[0]), setattr(settings, "bot_api_url", self._orig[1])))
        self.addAsyncCleanup(self.runner.cleanup)

    async def test_request_reaches_the_server(self):
        self.assertTrue(await notify.send_message(7, "你好"))
        self.assertEqual(self.requests, [("/bot42:TOK/sendMessage", {"chat_id": 7, "text": "你好"})])

    async def test_server_rejecting_is_false(self):
        self.status = 403
        self.assertFalse(await notify.send_message(7, "x"))

    async def test_nothing_listening_is_false(self):
        settings.bot_api_url = "http://127.0.0.1:1"
        self.assertFalse(await notify.send_message(7, "x"))


if __name__ == "__main__":
    unittest.main()
