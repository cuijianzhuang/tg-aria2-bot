import os
import re
import tempfile

from aiogram import Router, F
from aiogram.types import Message

from bot.config import settings
from bot.core import storage
from bot.core.cards import render_pending_card
from bot.core.keyboards import pending_task_keyboard
from bot.core.pending_tasks import create_pending
from bot.core.telegram_files import to_local_path

router = Router(name="links")

URL_RE = re.compile(r"^https?://\S+$", re.IGNORECASE)
MAGNET_RE = re.compile(r"^magnet:\?xt=urn:btih:\S+$", re.IGNORECASE)


@router.message(F.text.regexp(URL_RE.pattern))
async def handle_url(message: Message, aria2, repo):
    url = message.text.strip()
    ref = storage.url_hash(url)

    existing = await repo.get_completed_by_source("url", ref)
    if existing:
        await message.reply(f"ℹ️ 该链接已下载过：{existing['save_path']}")
        return

    token = create_pending(
        kind="url",
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        source_ref=ref,
        file_name=os.path.basename(url),
        file_size=None,
        payload=url,
    )
    await message.reply(
        render_pending_card("url", os.path.basename(url) or url),
        reply_markup=pending_task_keyboard(token),
        parse_mode="HTML",
    )


@router.message(F.text.regexp(MAGNET_RE.pattern))
async def handle_magnet(message: Message, aria2, repo):
    magnet = message.text.strip()
    ref = storage.url_hash(magnet)

    existing = await repo.get_completed_by_source("magnet", ref)
    if existing:
        await message.reply(f"ℹ️ 该磁力链接已下载过：{existing['save_path']}")
        return

    token = create_pending(
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

    torrent_path = to_local_path(tg_file.file_path)
    if torrent_path is None:
        with tempfile.NamedTemporaryFile(suffix=".torrent", delete=False) as tmp:
            await message.bot.download_file(tg_file.file_path, destination=tmp.name)
            torrent_path = tmp.name

    token = create_pending(
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
