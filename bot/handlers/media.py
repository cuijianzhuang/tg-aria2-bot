import logging

from aiogram import F, Router
from aiogram.types import Message

from bot.config import settings
from bot.core import storage
from bot.core.cards import render_pending_card
from bot.core.keyboards import pending_task_keyboard, redownload_keyboard

log = logging.getLogger(__name__)
router = Router(name="media")


def _extract_file(message: Message):
    """Return (file_id, file_unique_id, file_name, file_size) for the first media found."""
    if message.document:
        d = message.document
        return d.file_id, d.file_unique_id, d.file_name, d.file_size
    if message.video:
        v = message.video
        return v.file_id, v.file_unique_id, v.file_name or f"{v.file_unique_id}.mp4", v.file_size
    if message.audio:
        a = message.audio
        return a.file_id, a.file_unique_id, a.file_name or f"{a.file_unique_id}.mp3", a.file_size
    if message.photo:
        p = message.photo[-1]
        return p.file_id, p.file_unique_id, f"{p.file_unique_id}.jpg", p.file_size
    return None


@router.message(F.document | F.video | F.audio | F.photo)
async def handle_media(message: Message, aria2, repo):
    extracted = _extract_file(message)
    if not extracted:
        return
    file_id, file_unique_id, file_name, file_size = extracted

    # settings.max_file_size == 0 表示不限制（设置菜单里的"不限"选项）
    if file_size and settings.max_file_size and file_size > settings.max_file_size:
        await message.reply(
            f"⛔ 文件过大 ({file_size / 1024 / 1024:.1f} MB)，超过 "
            f"{settings.max_file_size / 1024 / 1024:.0f} MB 上限。"
        )
        return

    existing = await repo.get_completed_by_source("tg_media", file_unique_id)
    if existing:
        await message.reply(
            f"ℹ️ 该文件已下载过：{existing['save_path']}",
            reply_markup=redownload_keyboard(existing["gid"]),
        )
        return

    if not storage.has_enough_space(settings.download_dir, file_size or 0):
        await message.reply("⛔ 服务器磁盘空间不足，已拒绝该任务。")
        return

    tg_file = await message.bot.get_file(file_id)

    token = await repo.create_pending(
        kind="tg_media",
        user_id=message.from_user.id,
        chat_id=message.chat.id,
        source_ref=file_unique_id,
        file_name=file_name,
        file_size=file_size,
        # 存 Telegram 自己的 file_path，不在这里就拼成带 bot token 的下载
        # URI —— payload 会落库（tasks.payload，供"重试"复用），如果这里就
        # 拼好 URI，token 就跟着明文写进数据库了。真正的 URI 只在
        # _add_source 里、真的要喂给 aria2 的那一刻才现拼现用。
        payload=tg_file.file_path,
    )
    await message.reply(
        render_pending_card("tg_media", file_name, size=file_size),
        reply_markup=pending_task_keyboard(token),
        parse_mode="HTML",
    )
