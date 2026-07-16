import asyncio
import logging

import aria2p

log = logging.getLogger(__name__)


class Aria2Client:
    """Thin async-friendly wrapper around aria2p (which is sync under the hood).

    All calls are pushed to a thread so the aiogram event loop never blocks on RPC I/O.
    """

    def __init__(self, rpc_url: str, secret: str):
        host, port = self._split_url(rpc_url)
        # short explicit timeout: a hung aria2 must not pin poll-loop threads
        # for the library's default 60s per call
        self._api = aria2p.API(
            aria2p.Client(host=host, port=port, secret=secret, timeout=10)
        )

    @staticmethod
    def _split_url(rpc_url: str) -> tuple[str, int]:
        # rpc_url like http://aria2:6800/jsonrpc
        from urllib.parse import urlparse

        parsed = urlparse(rpc_url)
        return f"{parsed.scheme}://{parsed.hostname}", parsed.port or 6800

    async def _run(self, func, *args, **kwargs):
        return await asyncio.to_thread(func, *args, **kwargs)

    async def add_uri(self, url: str, *, out: str | None = None, download_dir: str | None = None) -> str:
        options = {}
        if out:
            options["out"] = out
        if download_dir:
            options["dir"] = download_dir
        download = await self._run(self._api.add_uris, [url], options or None)
        return download.gid

    async def add_magnet(self, magnet_uri: str, *, download_dir: str | None = None) -> str:
        options = {"dir": download_dir} if download_dir else None
        download = await self._run(self._api.add_magnet, magnet_uri, options)
        return download.gid

    async def add_torrent(self, torrent_path: str, *, download_dir: str | None = None) -> str:
        options = {"dir": download_dir} if download_dir else None
        download = await self._run(self._api.add_torrent, torrent_path, options=options)
        return download.gid

    async def get_status(self, gid: str) -> aria2p.Download:
        return await self._run(self._api.get_download, gid)

    async def pause(self, gid: str):
        # gid-direct RPC: one roundtrip instead of get_status + pause
        await self._run(self._api.client.pause, gid)

    async def resume(self, gid: str):
        await self._run(self._api.client.unpause, gid)

    async def remove(self, gid: str, *, files: bool = False):
        download = await self.get_status(gid)
        await self._run(download.remove, force=True, files=files)

    async def get_all_downloads(self) -> list[aria2p.Download]:
        """Every download aria2 knows about (active + waiting + stopped), one RPC batch."""
        return await self._run(self._api.get_downloads)

    async def get_progress_map(self) -> dict[str, dict]:
        """gid -> {percent, speed, completed, total} for every download aria2 currently knows about."""
        downloads = await self._run(self._api.get_downloads)
        return {
            d.gid: {
                "percent": round(d.progress, 1),
                "speed": d.download_speed_string(),
                "completed": d.completed_length_string(),
                "total": d.total_length_string(),
            }
            for d in downloads
        }

    async def global_stat(self) -> aria2p.Stats:
        return await self._run(self._api.get_stats)

    async def set_global_limit(self, speed: str):
        await self._run(
            self._api.set_global_options, {"max-overall-download-limit": speed}
        )

    async def get_global_limit(self) -> str:
        """Raw max-overall-download-limit value ('0' = unlimited, else bytes/s)."""
        opts = await self._run(self._api.client.get_global_option)
        return opts.get("max-overall-download-limit", "0")

    async def set_selected_files(self, gid: str, indices: list[int]):
        """aria2 only accepts select-file changes while the download is not
        active, so pause -> change -> resume brackets the whole thing here."""
        was_active = (await self.get_status(gid)).status == "active"
        if was_active:
            await self.pause(gid)
        try:
            value = ",".join(str(i) for i in sorted(indices)) if indices else "1"
            await self._run(self._api.client.change_option, gid, {"select-file": value})
        finally:
            if was_active:
                await self.resume(gid)
