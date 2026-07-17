"""多节点注册与客户端池。

default 节点固定来自 .env（ARIA2_RPC/ARIA2_SECRET/DOWNLOAD_DIR，is_local=True，
不可删）；额外节点存 nodes 表，管理员通过 /addnode 添加。不配置额外节点时
整个池只有 default，行为与单节点时代完全一致。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from bot.config import settings
from bot.core.aria2_client import Aria2Client

log = logging.getLogger(__name__)

DEFAULT_NODE = "default"
# 节点名进 callback_data（如 nodeuse:<name>，上限 64 字节），限制字节长度保证拼出来不超
MAX_NODE_NAME_BYTES = 32


class NodeUnavailable(Exception):
    """节点不存在或已停用。"""


@dataclass
class Node:
    name: str
    rpc_url: str
    secret: str
    download_dir: str
    is_local: bool
    enabled: bool = True

    @property
    def display_name(self) -> str:
        # default 是内部标识，界面上统一显示"本机"
        return "本机" if self.name == DEFAULT_NODE else self.name


class NodePool:
    def __init__(self, repo):
        self._repo = repo
        self._nodes: dict[str, Node] = {}
        self._clients: dict[str, Aria2Client] = {}
        # 轮询循环维护的健康缓存；None 表示还没探测过（启动初期），
        # 选择器上显示为在线（乐观），第一轮轮询后就是真实状态
        self._healthy: dict[str, bool] = {}

    async def load(self):
        """启动时装配：default(.env) + nodes 表。重复调用会全量重建。"""
        self._nodes = {
            DEFAULT_NODE: Node(
                name=DEFAULT_NODE,
                rpc_url=settings.aria2_rpc,
                secret=settings.aria2_secret,
                download_dir=settings.download_dir,
                is_local=True,
            )
        }
        for row in await self._repo.list_nodes():
            self._nodes[row["name"]] = Node(
                name=row["name"],
                rpc_url=row["rpc_url"],
                secret=row["secret"],
                download_dir=row["download_dir"],
                is_local=bool(row["is_local"]),
                enabled=bool(row["enabled"]),
            )
        # 客户端惰性创建（get 时）；这里把已不存在节点的旧客户端关掉再清出去，
        # 不然它们各自持有的 aiohttp session（HTTP 连接池 + 可能还挂着的 WS
        # 连接）就没人管了，一直挂到进程退出
        stale = [k for k in self._clients if k not in self._nodes]
        for name in stale:
            await self._clients.pop(name).close()

    # ---- 查询 ----

    def get_node(self, name: str) -> Node | None:
        return self._nodes.get(name)

    def get(self, name: str) -> Aria2Client:
        node = self._nodes.get(name)
        if node is None or not node.enabled:
            raise NodeUnavailable(name)
        if name not in self._clients:
            self._clients[name] = Aria2Client(node.rpc_url, node.secret)
        return self._clients[name]

    def all_nodes(self) -> list[Node]:
        return list(self._nodes.values())

    def enabled_nodes(self) -> list[Node]:
        return [n for n in self._nodes.values() if n.enabled]

    def is_multi(self) -> bool:
        return len(self.enabled_nodes()) > 1

    def label(self, name: str) -> str | None:
        """卡片/列表上的节点标注：单节点部署返回 None（不显示，界面不变啰嗦）。"""
        if not self.is_multi():
            return None
        node = self._nodes.get(name)
        return node.display_name if node else name

    def resolve(self, name: str) -> Node:
        """用户偏好里的节点可能已被删除/停用——静默回退 default 而不是报错，
        用户下次打开选择器自然会看到当前是本机。"""
        node = self._nodes.get(name)
        if node is None or not node.enabled:
            return self._nodes[DEFAULT_NODE]
        return node

    # ---- 管理（同步写库 + 更新内存） ----

    async def add(self, *, name: str, rpc_url: str, secret: str, download_dir: str, is_local: bool = False):
        await self._repo.add_node(
            name=name, rpc_url=rpc_url, secret=secret, download_dir=download_dir, is_local=is_local
        )
        self._nodes[name] = Node(
            name=name, rpc_url=rpc_url, secret=secret, download_dir=download_dir, is_local=is_local
        )

    async def remove(self, name: str):
        if name == DEFAULT_NODE:
            raise ValueError("default node cannot be removed")
        await self._repo.delete_node(name)
        self._nodes.pop(name, None)
        client = self._clients.pop(name, None)
        if client is not None:
            await client.close()
        self._healthy.pop(name, None)

    async def set_enabled(self, name: str, enabled: bool):
        if name == DEFAULT_NODE:
            raise ValueError("default node cannot be disabled")
        await self._repo.set_node_enabled(name, enabled)
        if name in self._nodes:
            self._nodes[name].enabled = enabled

    # ---- 健康状态 ----

    def mark_health(self, name: str, ok: bool):
        self._healthy[name] = ok

    def is_healthy(self, name: str) -> bool:
        return self._healthy.get(name, True)  # 未探测过按在线算，见 __init__ 注释

    async def check(self, client: Aria2Client) -> tuple[bool, str]:
        """现场探活（/addnode 入库前校验用），返回 (ok, 版本或错误信息)。"""
        try:
            version = await client.version()
            return True, version
        except Exception as e:
            return False, str(e)

    async def close(self):
        """进程关闭时把所有节点的 aiohttp session 都关掉，避免每个残留一条
        "Unclosed client session" 警告和一个没释放的连接池/WS 连接。"""
        for client in self._clients.values():
            await client.close()
        self._clients.clear()
