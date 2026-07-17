"""业务层 aria2 客户端：在 aria2_rpc 的裸 JSON-RPC 之上，把返回的 dict 包装
成 Download/File/Stats 这几个有属性/方法的对象——接口特意保持跟旧版 aria2p
暴露的一致（.status/.progress/.files/.dir/.name/.download_speed_string() 等），
这样 cards.py/task_manager.py/callbacks.py 里读这些对象的代码不用改一行。
"""
from __future__ import annotations

import asyncio
import base64
from dataclasses import dataclass, field
from pathlib import Path

from bot.core.aria2_rpc import Aria2RpcClient, Aria2RpcError
from bot.core.compress import remove_path


def _fmt_bytes(n: int, *, postfix: str = "") -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024:
            return (f"{size:.0f} B" if unit == "B" else f"{size:.1f} {unit}") + postfix
        size /= 1024
    return f"{size:.1f} PiB{postfix}"


@dataclass
class File:
    """一个下载任务里的单个文件（aria2 tellStatus 的 files[] 条目）。"""

    index: int
    path: Path
    length: int
    completed_length: int
    selected: bool

    @classmethod
    def from_struct(cls, struct: dict) -> File:
        return cls(
            index=int(struct["index"]),
            path=Path(struct["path"]),
            length=int(struct.get("length", 0) or 0),
            completed_length=int(struct.get("completedLength", 0) or 0),
            # aria2 对每个文件条目都会带这个字段，默认按"已选中"兜底（比默认
            # 未选中更安全——不会平白让一个文件从下载列表里消失）
            selected=struct.get("selected", "true") == "true",
        )

    @property
    def is_metadata(self) -> bool:
        # BT 元数据还没抓到之前，aria2 会用这种方括号前缀的合成路径占位
        return str(self.path).startswith("[METADATA]")

    def length_string(self) -> str:
        return _fmt_bytes(self.length)


@dataclass
class Download:
    """一个下载任务（aria2 tellStatus 的返回结构）。"""

    gid: str
    status: str  # active / waiting / paused / error / complete / removed —— aria2 原生取值，直接用
    total_length: int
    completed_length: int
    download_speed: int
    upload_speed: int
    connections: int
    error_message: str | None
    dir: Path
    files: list[File] = field(default_factory=list)
    _bittorrent_name: str | None = None

    @classmethod
    def from_struct(cls, struct: dict) -> Download:
        bt = struct.get("bittorrent") or {}
        bt_info = bt.get("info") or {}
        return cls(
            gid=struct["gid"],
            status=struct["status"],
            total_length=int(struct.get("totalLength", 0) or 0),
            completed_length=int(struct.get("completedLength", 0) or 0),
            download_speed=int(struct.get("downloadSpeed", 0) or 0),
            upload_speed=int(struct.get("uploadSpeed", 0) or 0),
            connections=int(struct.get("connections", 0) or 0),
            error_message=struct.get("errorMessage") or None,
            dir=Path(struct.get("dir", "")),
            files=[File.from_struct(f) for f in struct.get("files", [])],
            _bittorrent_name=bt_info.get("name") or None,
        )

    @property
    def progress(self) -> float:
        if not self.total_length:
            return 0.0
        return self.completed_length / self.total_length * 100

    @property
    def name(self) -> str | None:
        """单文件任务是文件名；多文件种子是种子声明的顶层目录/文件名
        （bittorrent.info.name）；元数据还没抓到时退化成第一个文件的占位路径。
        跟旧版 aria2p 的推导逻辑保持一致——task_manager.py 靠 dir+name 拼出
        最终保存路径，改了这里的语义会连带影响下载完成后的落盘路径判断。"""
        if self._bittorrent_name:
            return self._bittorrent_name
        if not self.files:
            return None
        first = self.files[0]
        if first.is_metadata:
            return str(first.path)
        try:
            rel = first.path.relative_to(self.dir)
        except ValueError:
            return first.path.name
        return rel.parts[0] if rel.parts else first.path.name

    def completed_length_string(self) -> str:
        return _fmt_bytes(self.completed_length)

    def total_length_string(self) -> str:
        return _fmt_bytes(self.total_length)

    def download_speed_string(self) -> str:
        return _fmt_bytes(self.download_speed, postfix="/s")

    def upload_speed_string(self) -> str:
        return _fmt_bytes(self.upload_speed, postfix="/s")


@dataclass
class Stats:
    num_active: int
    num_waiting: int
    num_stopped: int
    download_speed: int
    upload_speed: int

    @classmethod
    def from_struct(cls, struct: dict) -> Stats:
        return cls(
            num_active=int(struct.get("numActive", 0) or 0),
            num_waiting=int(struct.get("numWaiting", 0) or 0),
            num_stopped=int(struct.get("numStopped", 0) or 0),
            download_speed=int(struct.get("downloadSpeed", 0) or 0),
            upload_speed=int(struct.get("uploadSpeed", 0) or 0),
        )

    def download_speed_string(self) -> str:
        return _fmt_bytes(self.download_speed, postfix="/s")

    def upload_speed_string(self) -> str:
        return _fmt_bytes(self.upload_speed, postfix="/s")


def _root_paths(files: list[File], download_dir: Path) -> list[Path]:
    """download_dir 下这个任务涉及的顶层文件/目录去重列表——多文件种子只
    删一次外层文件夹，不逐文件删。跟旧版 aria2p 的 root_files_paths 语义
    一致，"取消并删除文件" 靠这个定位要删的东西。"""
    seen: list[Path] = []
    for f in files:
        if f.is_metadata:
            continue
        try:
            rel = f.path.relative_to(download_dir)
        except ValueError:
            continue
        top = download_dir / rel.parts[0]
        if top not in seen:
            seen.append(top)
    return seen


class Aria2Client:
    """单个 aria2 节点的业务层客户端。"""

    def __init__(self, rpc_url: str, secret: str):
        self._rpc = Aria2RpcClient(rpc_url, secret, timeout=10.0)

    async def close(self):
        await self._rpc.close()

    async def listen_events(self):
        """转发给传输层；TaskManager 直接消费 (gid, event) 元组。"""
        async for gid, event in self._rpc.listen_events():
            yield gid, event

    async def add_uri(self, url: str, *, out: str | None = None, download_dir: str | None = None) -> str:
        options = {}
        if out:
            options["out"] = out
        if download_dir:
            options["dir"] = download_dir
        return await self._rpc.call("addUri", [url], options)

    async def add_magnet(self, magnet_uri: str, *, download_dir: str | None = None) -> str:
        # aria2 的 addUri 本身就吃 magnet: 链接，不需要单独的 RPC 方法
        options = {"dir": download_dir} if download_dir else {}
        return await self._rpc.call("addUri", [magnet_uri], options)

    async def add_torrent(self, torrent_path: str, *, download_dir: str | None = None) -> str:
        # addTorrent 要的是种子文件内容的 base64，不是路径——磁盘读取丢进
        # 线程池，避免大种子文件（虽然种子本身通常很小）阻塞事件循环
        def _read_b64() -> str:
            with open(torrent_path, "rb") as f:
                return base64.b64encode(f.read()).decode("ascii")

        content = await asyncio.to_thread(_read_b64)
        options = {"dir": download_dir} if download_dir else {}
        return await self._rpc.call("addTorrent", content, [], options)

    async def get_status(self, gid: str) -> Download:
        struct = await self._rpc.call("tellStatus", gid)
        return Download.from_struct(struct)

    async def pause(self, gid: str):
        await self._rpc.call("pause", gid)

    async def resume(self, gid: str):
        await self._rpc.call("unpause", gid)

    async def remove(self, gid: str, *, files: bool = False, is_local: bool = True):
        """取消任务；files=True 额外删本地磁盘上已下载的部分。

        is_local=False（远程节点）时即使 files=True 也不会删文件——aria2 的
        JSON-RPC 协议本身就没有"删除已下载文件"这个方法，旧版一直是 aria2p
        在客户端这边直接对本地路径做 unlink/rmtree；对远程节点这样做完全是
        错的（会尝试删 bot 机器上偶然同名的路径），所以这里显式按节点归属
        禁用，而不是延续这个潜在的误删风险。
        """
        status = await self.get_status(gid)
        try:
            await self._rpc.call("forceRemove", gid)
        except Aria2RpcError:
            # 已经是终止状态（complete/error/removed）时 forceRemove 对它无效，
            # 要用 removeDownloadResult 把结果条目从 aria2 内部列表里清掉
            try:
                await self._rpc.call("removeDownloadResult", gid)
            except Aria2RpcError:
                pass  # 两个都失败：gid 已经彻底不存在了，没什么好清的

        if files and is_local:
            for path in _root_paths(status.files, status.dir):
                await asyncio.to_thread(remove_path, str(path))

    async def get_all_downloads(self) -> list[Download]:
        """Every download aria2 knows about (active + waiting + stopped), one RPC batch."""
        active, waiting, stopped = await asyncio.gather(
            self._rpc.call("tellActive"),
            self._rpc.call("tellWaiting", 0, 1000),
            self._rpc.call("tellStopped", 0, 1000),
        )
        return [Download.from_struct(s) for s in (*active, *waiting, *stopped)]

    async def get_progress_map(self) -> dict[str, dict]:
        """gid -> {percent, speed, completed, total} for every download aria2 currently knows about."""
        downloads = await self.get_all_downloads()
        return {
            d.gid: {
                "percent": round(d.progress, 1),
                "speed": d.download_speed_string(),
                "completed": d.completed_length_string(),
                "total": d.total_length_string(),
            }
            for d in downloads
        }

    async def global_stat(self) -> Stats:
        return Stats.from_struct(await self._rpc.call("getGlobalStat"))

    async def set_global_limit(self, speed: str):
        await self._rpc.call("changeGlobalOption", {"max-overall-download-limit": speed})

    async def get_global_options(self) -> dict:
        return await self._rpc.call("getGlobalOption")

    async def get_global_limit(self) -> str:
        """Raw max-overall-download-limit value ('0' = unlimited, else bytes/s)."""
        opts = await self.get_global_options()
        return opts.get("max-overall-download-limit", "0")

    async def set_max_concurrent(self, n: int):
        await self._rpc.call("changeGlobalOption", {"max-concurrent-downloads": str(n)})

    async def version(self) -> str:
        """探活用：getVersion 是 aria2 最轻的 RPC 之一，连不上/密钥错都会在这里抛。"""
        info = await self._rpc.call("getVersion")
        return info.get("version", "unknown")

    async def set_download_limit(self, gid: str, speed: str):
        """单任务限速（max-download-limit）。跟全局限速不同，aria2 允许在下载
        进行中直接改这个选项，不需要像 select-file 那样先暂停。"""
        await self._rpc.call("changeOption", gid, {"max-download-limit": speed})

    async def get_download_limit(self, gid: str) -> str:
        opts = await self._rpc.call("getOption", gid)
        return opts.get("max-download-limit", "0")

    async def set_selected_files(self, gid: str, indices: list[int]):
        """aria2 only accepts select-file changes while the download is not
        active, so pause -> change -> resume brackets the whole thing here."""
        was_active = (await self.get_status(gid)).status == "active"
        if was_active:
            await self.pause(gid)
        try:
            value = ",".join(str(i) for i in sorted(indices)) if indices else "1"
            await self._rpc.call("changeOption", gid, {"select-file": value})
        finally:
            if was_active:
                await self.resume(gid)
