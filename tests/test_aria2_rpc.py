"""针对 Aria2RpcClient 传输层的测试：起一个本地 aiohttp 假 aria2 服务器，
覆盖 HTTP JSON-RPC 调用（含 token 拼接、错误响应）和 WebSocket 事件订阅
（含过滤不关心的通知、忽略非文本帧、断线让 listen_events 自然结束）。
不连真实 aria2，全部在本机内存里跑。
"""
import json
import unittest

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from bot.core.aria2_rpc import Aria2RpcClient, Aria2RpcError


class FakeAria2Server:
    """记录收到的 JSON-RPC 请求，按预设脚本回应；WS 端点能按需推送几条通知
    后主动断开，模拟真实 aria2 的行为。"""

    def __init__(self):
        self.received_payloads: list[dict] = []
        self.rpc_result = "ok-result"
        self.rpc_error: dict | None = None
        self.ws_messages_to_send: list[dict] = []

    async def rpc_handler(self, request: web.Request) -> web.Response:
        payload = await request.json()
        self.received_payloads.append(payload)
        body = {"jsonrpc": "2.0", "id": payload["id"]}
        if self.rpc_error:
            body["error"] = self.rpc_error
        else:
            body["result"] = self.rpc_result
        return web.json_response(body)

    async def ws_handler(self, request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        for msg in self.ws_messages_to_send:
            await ws.send_str(json.dumps(msg))
        await ws.close()
        return ws

    def app(self) -> web.Application:
        app = web.Application()
        app.router.add_post("/jsonrpc", self.rpc_handler)
        app.router.add_get("/jsonrpc", self.ws_handler)
        return app


class Aria2RpcTestCase(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.fake = FakeAria2Server()
        self.server = TestServer(self.fake.app())
        self.test_client = TestClient(self.server)
        await self.test_client.start_server()
        self.rpc_url = f"http://{self.server.host}:{self.server.port}/jsonrpc"

    async def asyncTearDown(self):
        await self.test_client.close()


class TestCall(Aria2RpcTestCase):
    async def test_prepends_token_when_secret_given(self):
        client = Aria2RpcClient(self.rpc_url, "s3cret")
        result = await client.call("tellStatus", "gid123")
        self.assertEqual(result, "ok-result")
        sent = self.fake.received_payloads[0]
        self.assertEqual(sent["method"], "aria2.tellStatus")
        self.assertEqual(sent["params"], ["token:s3cret", "gid123"])
        await client.close()

    async def test_omits_token_when_no_secret(self):
        client = Aria2RpcClient(self.rpc_url, "")
        await client.call("getVersion")
        sent = self.fake.received_payloads[0]
        self.assertEqual(sent["params"], [])
        await client.close()

    async def test_raises_on_error_response(self):
        self.fake.rpc_error = {"code": 1, "message": "GID not found"}
        client = Aria2RpcClient(self.rpc_url, "s")
        with self.assertRaises(Aria2RpcError) as ctx:
            await client.call("tellStatus", "ghost")
        self.assertEqual(ctx.exception.code, 1)
        self.assertIn("GID not found", str(ctx.exception))
        await client.close()

    async def test_session_reused_across_calls(self):
        client = Aria2RpcClient(self.rpc_url, "s")
        await client.call("getVersion")
        session1 = client._session
        await client.call("getVersion")
        self.assertIs(client._session, session1)
        await client.close()

    async def test_close_is_idempotent_and_safe_before_use(self):
        client = Aria2RpcClient(self.rpc_url, "s")
        await client.close()  # 从没发起过请求就关闭
        await client.call("getVersion")
        await client.close()
        await client.close()  # 第二次关闭不应该抛异常


class TestListenEvents(Aria2RpcTestCase):
    async def test_yields_known_events_and_filters_others(self):
        self.fake.ws_messages_to_send = [
            {"jsonrpc": "2.0", "method": "aria2.onDownloadStart", "params": [{"gid": "ignored"}]},
            {"jsonrpc": "2.0", "method": "aria2.onDownloadComplete", "params": [{"gid": "g1"}]},
            {"jsonrpc": "2.0", "method": "aria2.onDownloadError", "params": [{"gid": "g2"}]},
            {"jsonrpc": "2.0", "method": "aria2.onBtDownloadComplete", "params": [{"gid": "g3"}]},
        ]
        client = Aria2RpcClient(self.rpc_url, "s")
        events = [e async for e in client.listen_events()]
        self.assertEqual(events, [("g1", "complete"), ("g2", "error"), ("g3", "complete")])
        await client.close()

    async def test_stream_ends_when_server_closes_connection(self):
        self.fake.ws_messages_to_send = [
            {"jsonrpc": "2.0", "method": "aria2.onDownloadComplete", "params": [{"gid": "g1"}]},
        ]
        client = Aria2RpcClient(self.rpc_url, "s")
        count = 0
        async for _ in client.listen_events():
            count += 1
        self.assertEqual(count, 1)  # 服务器关闭后 async for 正常结束，不挂起
        await client.close()

    async def test_ignores_malformed_json_frame(self):
        # 直接用底层 handler 发一条非 JSON 文本帧，混在正常事件前后
        async def ws_handler(request):
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            await ws.send_str("not json at all")
            await ws.send_str(json.dumps({"method": "aria2.onDownloadComplete", "params": [{"gid": "g1"}]}))
            await ws.close()
            return ws

        app = web.Application()
        app.router.add_get("/jsonrpc", ws_handler)
        server = TestServer(app)
        tc = TestClient(server)
        await tc.start_server()
        try:
            client = Aria2RpcClient(f"http://{server.host}:{server.port}/jsonrpc", "s")
            events = [e async for e in client.listen_events()]
            self.assertEqual(events, [("g1", "complete")])
            await client.close()
        finally:
            await tc.close()


if __name__ == "__main__":
    unittest.main()
