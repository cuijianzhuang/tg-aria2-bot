import os
import re
import shutil
from urllib.parse import unquote, urlparse
from uuid import uuid4

from aiogram import F, Router
from aiogram.types import Message

from bot.config import settings
from bot.core import storage
from bot.core.cards import render_batch_pending, render_pending_card
from bot.core.keyboards import batch_pending_keyboard, pending_task_keyboard, redownload_keyboard
from bot.core.telegram_files import to_local_path

router = Router(name="links")

URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
MAGNET_RE = re.compile(r"^magnet:\?xt=urn:btih:\S+$", re.IGNORECASE)

# 一条消息最多同时处理这么多条链接，超出的部分提示用户分批发送，避免一条
# 消息就把待确认队列灌满
MAX_BATCH_LINKS = 20


def _url_display_name(url: str) -> str:
    """Filename from the URL *path* only — basename(url) would drag the query
    string into the name (file.zip?key=abc)."""
    name = os.path.basename(unquote(urlparse(url).path))
    return name or url


def _torrent_store_path(file_unique_id: str) -> str:
    """Persistent copy of an uploaded .torrent, keyed by Telegram's unique file id
    (resending the same torrent reuses the same path — no unbounded growth).
    Kept so 重试 can re-add the download after the original message is long gone."""
    store = os.path.join(os.path.dirname(settings.db_path), "torrents")
    os.makedirs(store, exist_ok=True)
    return os.path.join(store, f"{file_unique_id}.torrent")


async def _create_url_pending(message: Message, repo, url: str, *, batch_id: str | None = None) -> str | None:
    """建一条 url 类型的待确认任务；已经下载过的链接返回 None（不建 pending，
    调用方决定要不要提示"已下载过"——单条发送时提示，批量场景里静默跳过）。"""
    ref = storage.url_hash(url)
    if await repo.get_completed_by_source("url", ref):
        return None
    display_name = _url_display_name(url)
    token = await repo.create_pending(
        kind="url",
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        source_ref=ref,
        file_name=display_name if display_name != url else None,
        file_size=None,
        payload=url,
        batch_id=batch_id,
    )
    return token


async def _create_magnet_pending(message: Message, repo, magnet: str, *, batch_id: str | None = None) -> str | None:
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
    )
    return token


@router.message(F.text.regexp(URL_RE.pattern))
async def handle_url(message: Message, aria2, repo):
    url = message.text.strip()
    existing = await repo.get_completed_by_source("url", storage.url_hash(url))
    if existing:
        await message.reply(
            f"ℹ️ 该链接已下载过：{existing['save_path']}",
            reply_markup=redownload_keyboard(existing["gid"]),
        )
        return

    token = await _create_url_pending(message, repo, url)
    await message.reply(
        render_pending_card("url", _url_display_name(url)),
        reply_markup=pending_task_keyboard(token),
        parse_mode="HTML",
    )


@router.message(F.text.regexp(MAGNET_RE.pattern))
async def handle_magnet(message: Message, aria2, repo):
    magnet = message.text.strip()
    existing = await repo.get_completed_by_source("magnet", storage.url_hash(magnet))
    if existing:
        await message.reply(
            f"ℹ️ 该磁力链接已下载过：{existing['save_path']}",
            reply_markup=redownload_keyboard(existing["gid"]),
        )
        return

    token = await _create_magnet_pending(message, repo, magnet)
    await message.reply(
        render_pending_card("magnet", "磁力链接任务"),
        reply_markup=pending_task_keyboard(token),
        parse_mode="HTML",
    )


def _extract_links(text: str) -> list[tuple[str, str]]:
    """把消息按行拆开，挑出能识别成 url/magnet 的行；顺序无关的其它行
    （空行、说明文字等）直接忽略，不当错误处理。"""
    links = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        if MAGNET_RE.match(line):
            links.append(("magnet", line))
        elif URL_RE.match(line):
            links.append(("url", line))
    return links


def _is_multi_link_message(message: Message) -> bool:
    """只有真正识别出 >=2 条链接才算批量消息 —— 恰好 1 条的情况已经被上面两个
    精确匹配的单行 handler 接管（包括末尾带换行符的边界情况），这里不用重复处理，
    避免误吞普通聊天消息。"""
    if not message.text:
        return False
    return len(_extract_links(message.text)) >= 2


@router.message(F.text, _is_multi_link_message)
async def handle_batch_links(message: Message, repo):
    all_links = _extract_links(message.text)
    overflow = max(0, len(all_links) - MAX_BATCH_LINKS)
    links = all_links[:MAX_BATCH_LINKS]

    batch_id = uuid4().hex[:12]
    names: list[str] = []
    duplicates = 0
    for kind, payload in links:
        if kind == "magnet":
            token = await _create_magnet_pending(message, repo, payload, batch_id=batch_id)
            name = "磁力链接任务"
        else:
            token = await _create_url_pending(message, repo, payload, batch_id=batch_id)
            name = _url_display_name(payload)
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


@router.message(F.document.file_name.endswith(".torrent"))
async def handle_torrent(message: Message, aria2, repo):
    tg_file = await message.bot.get_file(message.document.file_id)

    # Always copy into our own persistent store: temp files leaked, and both the
    # bot-api's local path and a temp path die before a later 重试 needs them.
    torrent_path = _torrent_store_path(message.document.file_unique_id)
    if not os.path.exists(torrent_path):
        local = to_local_path(tg_file.file_path)
        if local is not None:
            shutil.copyfile(local, torrent_path)
        else:
            await message.bot.download_file(tg_file.file_path, destination=torrent_path)

    token = await repo.create_pending(
        kind="torrent",
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        source_ref=message.document.file_unique_id,
        file_name=message.document.file_name,
        file_size=message.document.file_size,
        payload=torrent_path,
    )
    await message.reply(
        render_pending_card("torrent", message.document.file_name, size=message.document.file_size),
        reply_markup=pending_task_keyboard(token),
        parse_mode="HTML",
    )
