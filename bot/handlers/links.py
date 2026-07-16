import os
import re
import shutil
from urllib.parse import unquote, urlparse

from aiogram import Router, F
from aiogram.types import Message

from bot.config import settings
from bot.core import storage
from bot.core.cards import render_pending_card
from bot.core.keyboards import pending_task_keyboard, redownload_keyboard
from bot.core.telegram_files import to_local_path

router = Router(name="links")

URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
MAGNET_RE = re.compile(r"^magnet:\?xt=urn:btih:\S+$", re.IGNORECASE)


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


@router.message(F.text.regexp(URL_RE.pattern))
async def handle_url(message: Message, aria2, repo):
    url = message.text.strip()
    ref = storage.url_hash(url)
    display_name = _url_display_name(url)

    existing = await repo.get_completed_by_source("url", ref)
    if existing:
        await message.reply(
            f"ℹ️ 该链接已下载过：{existing['save_path']}",
            reply_markup=redownload_keyboard(existing["gid"]),
        )
        return

    token = await repo.create_pending(
        kind="url",
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        source_ref=ref,
        file_name=display_name if display_name != url else None,
        file_size=None,
        payload=url,
    )
    await message.reply(
        render_pending_card("url", display_name),
        reply_markup=pending_task_keyboard(token),
        parse_mode="HTML",
    )


@router.message(F.text.regexp(MAGNET_RE.pattern))
async def handle_magnet(message: Message, aria2, repo):
    magnet = message.text.strip()
    ref = storage.url_hash(magnet)

    existing = await repo.get_completed_by_source("magnet", ref)
    if existing:
        await message.reply(
            f"ℹ️ 该磁力链接已下载过：{existing['save_path']}",
            reply_markup=redownload_keyboard(existing["gid"]),
        )
        return

    token = await repo.create_pending(
        kind="magnet",
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        source_ref=ref,
        file_name="磁力链接任务",
        file_size=None,
        payload=magnet,
    )
    await message.reply(
        render_pending_card("magnet", "磁力链接任务"),
        reply_markup=pending_task_keyboard(token),
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
