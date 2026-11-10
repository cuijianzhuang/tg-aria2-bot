"""Submit download sources to aria2 without Telegram UI or database mutations."""

from bot.core import storage
from bot.core.node_pool import NodePool, NodeUnavailable
from bot.core.telegram_files import to_download_uri


async def add_source(nodes: NodePool, node_name: str, kind: str, payload: str, file_name: str | None) -> str:
    """Add a download to the task's node; returns the new gid.
    Shared by pending:start and task:retry so both stay in sync."""
    node = nodes.get_node(node_name)
    if node is None or not node.enabled:
        raise NodeUnavailable(node_name)
    client = nodes.get(node_name)
    # 远程节点的 download_dir 是远端路径，绝不能在本地 makedirs（只会在 bot
    # 机器上造垃圾目录）；路径作为 dir 选项传给 aria2，由它在自己那边创建
    if kind == "tg_media":
        # payload is Telegram's raw getFile() file_path (no bot token baked in —
        # that's deliberate, see the comment where it's persisted in media.py).
        # The token only gets stitched into the URI here, right before the RPC
        # call, so it never touches the database.
        subdir = storage.build_subdir(node.download_dir, file_name or payload, create=node.is_local)
        return await client.add_uri(
            to_download_uri(payload),
            out=file_name,
            download_dir=subdir,
        )
    if kind == "url":
        subdir = storage.build_subdir(node.download_dir, file_name or payload, create=node.is_local)
        return await client.add_uri(payload, download_dir=subdir)
    if kind == "magnet":
        return await client.add_magnet(payload, download_dir=node.download_dir)
    if kind == "metalink":
        return await client.add_metalink(payload, download_dir=node.download_dir)
    if kind == "torrent":
        # add_torrent 读 bot 本地的种子副本、以 base64 走 RPC 传给目标
        # aria2 —— 不要求目标节点能访问这个文件路径，天然跨节点
        return await client.add_torrent(payload, download_dir=node.download_dir)
    raise ValueError(f"unknown source kind: {kind}")
