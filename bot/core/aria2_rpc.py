"""aria2 的最小异步 JSON-RPC 2.0 客户端 + WebSocket 事件订阅。

替换掉原来的 aria2p（同步库，每次调用都要 asyncio.to_thread 扔进线程池）：
纯 aiohttp 实现，方法调用直接是原生协程，没有线程切换开销；WebSocket 那部分
是新增能力，用来让 TaskManager 在下载完成/出错的瞬间就拿到通知，而不是等
最多 5 秒的轮询周期——这是这次重构真正的收益所在，光换个同步库为异步库本身
省下的线程切换时间可以忽略不计。

这一层只管传输，不解析业务字段；aria2_client.py 在这之上把裸 dict 包装成
Download/File/Stats 这些有属性的对象，跟旧版 aria2p 暴露的接口保持一致，
这样上层 handler/task_manager 代码几乎不用改。
"""
from __future__ import annotations

import itertools
import json
import logging
from collections.abc import AsyncIterator
from urllib.parse import urlparse

import aiohttp

log = logging.getLogger(__name__)

_id_counter = itertools.count(1)

# aria2 WebSocket 连接建立后会自动推送这些通知（不需要显式订阅），method 名
# 映射到我们关心的粗粒度事件。onDownloadStart/onDownloadPause 不订阅——那些
# 状态变化已经由发起操作的用户自己驱动了 UI 更新，不需要额外推送触发。
NOTIFICATION_EVENTS = {
    "aria2.onDownloadComplete": "complete",
    "aria2.onBtDownloadComplete": "complete",
    "aria2.onDownloadError": "error",
}


class Aria2RpcError(Exception):
    """aria2 返回的 JSON-RPC error 对象（密钥错误、gid 不存在等）。"""

    def __init__(self, code, message):
        self.code = code
        self.rpc_message = message
        super().__init__(f"aria2 rpc error {code}: {message}")


class Aria2RpcClient:
    """单个 aria2 实例的传输层。session 进程内复用（同 gofile.py 的做法），
    不用 async with 包一次性 ClientSession —— 高频轮询场景下每次都开关连接
    才是真正的浪费。"""

    def __init__(self, rpc_url: str, secret: str, *, timeout: float = 10.0):
        self._url = rpc_url
        self._token = f"token:{secret}" if secret else None
        self._timeout_s = timeout
        self._timeout = aiohttp.ClientTimeout(total=timeout)
        self._session: aiohttp.ClientSession | None = None

    def _ws_url(self) -> str:
        parsed = urlparse(self._url)
        scheme = "wss" if parsed.scheme == "https" else "ws"
        return parsed._replace(scheme=scheme).geturl()

    def _get_session(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._session is not None and not self._session.closed:
            await self._session.close()
        self._session = None

    async def call(self, method: str, *params):
        """单次 JSON-RPC 调用。method 不带 "aria2." 前缀，这里统一加上。"""
        full_params = ([self._token] if self._token else []) + list(params)
        payload = {
            "jsonrpc": "2.0",
            "id": next(_id_counter),
            "method": f"aria2.{method}",
            "params": full_params,
        }
        session = self._get_session()
        async with session.post(self._url, json=payload, timeout=self._timeout) as resp:
            # aria2 的响应 Content-Type 不一定是 application/json（部分版本/
            # 反代会改写成 text/plain），content_type=None 跳过校验直接解析
            data = await resp.json(content_type=None)
        if "error" in data:
            err = data["error"]
            raise Aria2RpcError(err.get("code"), err.get("message"))
        return data["result"]

    async def listen_events(self) -> AsyncIterator[tuple[str, str]]:
        """常驻一条 WebSocket 连接，yield (gid, event) 直到连接断开为止。
        断线不在这里重连——一次只跑完一条连接的生命周期，重连策略交给调用方
        （TaskManager 按固定退避重新调用这个方法）。"""
        session = self._get_session()
        async with session.ws_connect(self._ws_url(), timeout=self._timeout_s) as ws:
            async for msg in ws:
                if msg.type == aiohttp.WSMsgType.TEXT:
                    pass
                elif msg.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSING):
                    break
                else:
                    continue
                try:
                    data = json.loads(msg.data)
                except (ValueError, TypeError):
                    continue
                event = NOTIFICATION_EVENTS.get(data.get("method"))
                if not event:
                    continue
                for param in data.get("params") or []:
                    gid = param.get("gid")
                    if gid:
                        yield gid, event
