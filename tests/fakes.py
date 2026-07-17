"""测试共用的假对象：不连真实 aria2 的 NodePool/客户端替身。"""
import tempfile

from bot.core.node_pool import Node, NodeUnavailable


class FakeAria2:
    def __init__(self):
        self.add_uri_calls = []
        self.add_magnet_calls = []
        self.add_torrent_calls = []
        self.paused = []
        self.resumed = []
        self.removed = []
        # gid -> Download-like 对象；get_status 对未配置的 gid 抛异常，
        # 模拟"aria2 不认识这个 gid 了"
        self.statuses: dict = {}
        # (gid, event) 元组列表；listen_events 按顺序 yield 完就正常结束
        # （模拟服务器主动断开），空列表 = 一条事件都不推送
        self.events_to_emit: list[tuple[str, str]] = []

    async def add_uri(self, uri, *, out=None, download_dir=None):
        self.add_uri_calls.append((uri, out, download_dir))
        return "gid-uri"

    async def add_magnet(self, magnet, *, download_dir=None):
        self.add_magnet_calls.append((magnet, download_dir))
        return "gid-magnet"

    async def add_torrent(self, path, *, download_dir=None):
        self.add_torrent_calls.append((path, download_dir))
        return "gid-torrent"

    async def get_progress_map(self):
        return {}

    async def pause(self, gid):
        self.paused.append(gid)

    async def resume(self, gid):
        self.resumed.append(gid)

    async def get_status(self, gid):
        if gid not in self.statuses:
            raise KeyError(f"unknown gid: {gid}")
        return self.statuses[gid]

    async def remove(self, gid, *, files=False, is_local=True):
        self.removed.append((gid, files, is_local))

    async def listen_events(self):
        for gid, event in self.events_to_emit:
            yield gid, event


class FakeNodePool:
    """行为对齐 node_pool.NodePool 的查询接口，客户端全部用 FakeAria2。
    默认单节点（default/本机）；传入 extra_nodes 模拟多节点部署。

    default 节点的下载目录默认用临时目录而不是 /downloads —— 本机节点的
    _add_source 会真的 makedirs，CI runner 上非 root 建不了 /downloads
    （PermissionError），本地 root 沙箱却能建成，这类失败只在 CI 暴露。"""

    def __init__(self, extra_nodes: list[Node] | None = None, download_dir: str | None = None):
        if download_dir is None:
            download_dir = tempfile.mkdtemp(prefix="fake-node-dl-")
        self._nodes: dict[str, Node] = {
            "default": Node(
                name="default", rpc_url="http://localhost:6800/jsonrpc",
                secret="s", download_dir=download_dir, is_local=True,
            )
        }
        for node in extra_nodes or []:
            self._nodes[node.name] = node
        self.clients: dict[str, FakeAria2] = {}
        self._healthy: dict[str, bool] = {}

    def get_node(self, name):
        return self._nodes.get(name)

    def get(self, name):
        node = self._nodes.get(name)
        if node is None or not node.enabled:
            raise NodeUnavailable(name)
        if name not in self.clients:
            self.clients[name] = FakeAria2()
        return self.clients[name]

    def all_nodes(self):
        return list(self._nodes.values())

    def enabled_nodes(self):
        return [n for n in self._nodes.values() if n.enabled]

    def is_multi(self):
        return len(self.enabled_nodes()) > 1

    def label(self, name):
        if not self.is_multi():
            return None
        node = self._nodes.get(name)
        return node.display_name if node else name

    def resolve(self, name):
        node = self._nodes.get(name)
        if node is None or not node.enabled:
            return self._nodes["default"]
        return node

    def mark_health(self, name, ok):
        self._healthy[name] = ok

    def is_healthy(self, name):
        return self._healthy.get(name, True)
