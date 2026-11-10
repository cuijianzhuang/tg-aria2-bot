import asyncio
import os
import shutil
from pathlib import Path
from uuid import uuid4

from aiogram import F, Router
from aiogram.types import Message

from bot.config import settings
from bot.core import storage
from bot.core.cards import render_batch_pending, render_pending_card
from bot.core.keyboards import batch_pending_keyboard, pending_task_keyboard, redownload_keyboard
from bot.core.metalink import MAX_METALINK_BYTES, split_metalink
from bot.core.sources import INFOHASH_RE, URL_RE, extract_links, extract_magnets, url_display_name
from bot.core.telegram_files import to_local_path

router = Router(name="links")

# 一条消息最多同时处理这么多条链接，超出的部分提示用户分批发送，避免一条
# 消息就把待确认队列灌满
MAX_BATCH_LINKS = 20


def _torrent_store_path(file_unique_id: str) -> str:
    """Persistent copy of an uploaded .torrent, keyed by Telegram's unique file id
    (resending the same torrent reuses the same path — no unbounded growth).
    Kept so 重试 can re-add the download after the original message is long gone."""
    store = os.path.join(os.path.dirname(settings.db_path), "torrents")
    os.makedirs(store, exist_ok=True)
    return os.path.join(store, f"{file_unique_id}.torrent")


async def _user_node(message: Message, repo, nodes) -> str:
    """发起人的当前节点（已删除/停用的偏好静默回退 default）。"""
    preferred = await repo.get_current_node(message.from_user.id)
    return nodes.resolve(preferred).name


async def _create_url_pending(message: Message, repo, url: str, *, node: str, batch_id: str | None = None) -> str | None:
    """建一条 url 类型的待确认任务；已经下载过的链接返回 None（不建 pending，
    调用方决定要不要提示"已下载过"——单条发送时提示，批量场景里静默跳过）。"""
    ref = storage.url_hash(url)
    if await repo.get_completed_by_source("url", ref):
        return None
    display_name = url_display_name(url)
    token = await repo.create_pending(
        kind="url",
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        source_ref=ref,
        file_name=display_name if display_name != url else None,
        file_size=None,
        payload=url,
        batch_id=batch_id,
        node=node,
    )
    return token


async def _create_magnet_pending(message: Message, repo, magnet: str, *, node: str, batch_id: str | None = None) -> str | None:
    ref = storage.url_hash(magnet)
    if await repo.get_completed_by_source("magnet", ref):
        return None
    token = await repo.create_pending(
        kind="magnet",
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        source_ref=ref,
        file_name="磁力链接任务",
        file_size=None,
        payload=magnet,
        batch_id=batch_id,
        node=node,
    )
    return token


@router.message(F.text.strip().regexp(URL_RE))
async def handle_url(message: Message, repo, nodes):
    url = message.text.strip()
    existing = await repo.get_completed_by_source("url", storage.url_hash(url))
    if existing:
        await message.reply(
            f"ℹ️ 该链接已下载过：{existing['save_path']}",
            reply_markup=redownload_keyboard(existing["gid"]),
        )
        return

    node = await _user_node(message, repo, nodes)
    token = await _create_url_pending(message, repo, url, node=node)
    await message.reply(
        render_pending_card(
            "url", url_display_name(url),
            node_label=nodes.label(node), download_dir=nodes.get_node(node).download_dir,
        ),
        reply_markup=pending_task_keyboard(token, show_node_switch=nodes.is_multi()),
        parse_mode="HTML",
    )


async def _handle_single_magnet(message: Message, repo, nodes, magnet: str):
    existing = await repo.get_completed_by_source("magnet", storage.url_hash(magnet))
    if existing:
        await message.reply(
            f"ℹ️ 该磁力链接已下载过：{existing['save_path']}",
            reply_markup=redownload_keyboard(existing["gid"]),
        )
        return

    node = await _user_node(message, repo, nodes)
    token = await _create_magnet_pending(message, repo, magnet, node=node)
    await message.reply(
        render_pending_card(
            "magnet", "磁力链接任务",
            node_label=nodes.label(node), download_dir=nodes.get_node(node).download_dir,
        ),
        reply_markup=pending_task_keyboard(token, show_node_switch=nodes.is_multi()),
        parse_mode="HTML",
    )


def _is_single_magnet_message(message: Message) -> bool:
    links = extract_links(message.text or "")
    return len(links) == 1 and bool(extract_magnets(message.text or ""))


@router.message(F.text, _is_single_magnet_message)
async def handle_magnet(message: Message, repo, nodes):
    await _handle_single_magnet(message, repo, nodes, extract_links(message.text)[0][1])


@router.message(F.text.regexp(INFOHASH_RE.pattern))
async def handle_infohash(message: Message, repo, nodes):
    magnet = f"magnet:?xt=urn:btih:{message.text.strip()}"
    await _handle_single_magnet(message, repo, nodes, magnet)


def _is_multi_link_message(message: Message) -> bool:
    """识别出至少两条链接才进入批量流程，单条由对应 handler 处理。"""
    if not message.text:
        return False
    return len(extract_links(message.text)) >= 2


@router.message(F.text, _is_multi_link_message)
async def handle_batch_links(message: Message, repo, nodes):
    all_links = extract_links(message.text)
    overflow = max(0, len(all_links) - MAX_BATCH_LINKS)
    links = all_links[:MAX_BATCH_LINKS]

    node = await _user_node(message, repo, nodes)
    batch_id = uuid4().hex[:12]
    names: list[str] = []
    duplicates = 0
    for kind, payload in links:
        if kind == "magnet":
            token = await _create_magnet_pending(message, repo, payload, node=node, batch_id=batch_id)
            name = "磁力链接任务"
        else:
            token = await _create_url_pending(message, repo, payload, node=node, batch_id=batch_id)
            name = url_display_name(payload)
        if token is None:
            duplicates += 1
            continue
        names.append(name)

    if not names:
        await message.reply("这些链接都已经下载过了，没有新增任务。")
        return

    await message.reply(
        render_batch_pending(names, duplicates, overflow),
        reply_markup=batch_pending_keyboard(batch_id),
        parse_mode="HTML",
    )


@router.message(F.document.file_name.lower().endswith((".metalink", ".meta4")))
async def handle_metalink(message: Message, repo, nodes):
    if (message.document.file_size or 0) > MAX_METALINK_BYTES:
        await message.reply("⛔ Metalink 清单不能超过 2 MiB。")
        return
    tg_file = await message.bot.get_file(message.document.file_id)
    local = to_local_path(tg_file.file_path)
    if local is not None:
        def read_manifest():
            with open(local, "rb") as stream:
                return stream.read(MAX_METALINK_BYTES + 1)
        content = await asyncio.to_thread(read_manifest)
    else:
        stream = await message.bot.download_file(tg_file.file_path)
        content = stream.getvalue()
    try:
        entries = split_metalink(content)
    except ValueError as exc:
        await message.reply(f"⛔ {exc}")
        return

    node = await _user_node(message, repo, nodes)
    batch_id = uuid4().hex[:12] if len(entries) > 1 else None
    store = os.path.join(os.path.dirname(settings.db_path), "metalinks")
    os.makedirs(store, exist_ok=True)
    names, tokens = [], []
    duplicates = 0
    for index, (name, data) in enumerate(entries):
        ref = f"{message.document.file_unique_id}:{index}"
        if await repo.get_completed_by_source("metalink", ref):
            duplicates += 1
            continue
        path = os.path.join(store, storage.url_hash(ref) + ".meta4")
        await asyncio.to_thread(Path(path).write_bytes, data)
        token = await repo.create_pending(
            kind="metalink", user_id=message.from_user.id, chat_id=message.chat.id,
            source_ref=ref, file_name=name, file_size=None, payload=path,
            batch_id=batch_id, node=node,
        )
        tokens.append(token)
        names.append(name)
    if not names:
        await message.reply("这些文件都已经下载过了，没有新增任务。")
    elif batch_id:
        await message.reply(
            render_batch_pending(names, duplicates, 0),
            reply_markup=batch_pending_keyboard(batch_id), parse_mode="HTML",
        )
    else:
        await message.reply(
            render_pending_card("metalink", names[0], node_label=nodes.label(node),
                                download_dir=nodes.get_node(node).download_dir),
            reply_markup=pending_task_keyboard(tokens[0], show_node_switch=nodes.is_multi()),
            parse_mode="HTML",
        )


@router.message(F.document.file_name.lower().endswith(".torrent"))
async def handle_torrent(message: Message, repo, nodes):
    tg_file = await message.bot.get_file(message.document.file_id)

    # Always copy into our own persistent store: temp files leaked, and both the
    # bot-api's local path and a temp path die before a later 重试 needs them.
    # 种子文件天然跨节点：add_torrent 读的是 bot 本地这份副本、以
    # base64 走 RPC 传给目标 aria2，不要求目标节点能访问这个路径。
    torrent_path = _torrent_store_path(message.document.file_unique_id)
    if not os.path.exists(torrent_path):
        local = to_local_path(tg_file.file_path)
        if local is not None:
            shutil.copyfile(local, torrent_path)
        else:
            await message.bot.download_file(tg_file.file_path, destination=torrent_path)

    node = await _user_node(message, repo, nodes)
    token = await repo.create_pending(
        kind="torrent",
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        source_ref=message.document.file_unique_id,
        file_name=message.document.file_name,
        file_size=message.document.file_size,
        payload=torrent_path,
        node=node,
    )
    await message.reply(
        render_pending_card(
            "torrent", message.document.file_name, size=message.document.file_size,
            node_label=nodes.label(node), download_dir=nodes.get_node(node).download_dir,
        ),
        reply_markup=pending_task_keyboard(token, show_node_switch=nodes.is_multi()),
        parse_mode="HTML",
    )
