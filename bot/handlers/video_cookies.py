"""Private, reply-scoped personal Cookie uploads; never create download tasks."""

import io
import time
from pathlib import Path

from aiogram import F, Router
from aiogram.dispatcher.event.bases import SkipHandler
from aiogram.filters import Command
from aiogram.types import ForceReply, Message

from bot.config import settings
from bot.video.account_views import PLATFORM_LABELS, platform_view
from bot.video.accounts import MAX_COOKIE_BYTES, CredentialVault, parse_cookie_upload

router = Router(name="video_cookies")
PROMPT_PREFIX = "📄 个人 Cookie 导入 · "


class CookieUploadTooLarge(ValueError):
    pass


class CookieBuffer(io.BytesIO):
    def write(self, data):
        if self.tell() + len(data) > MAX_COOKIE_BYTES:
            raise CookieUploadTooLarge("Cookie 文件不能超过 1 MiB")
        return super().write(data)


async def start_import(query, repo, platform):
    prompt = await query.message.answer(
        PROMPT_PREFIX + PLATFORM_LABELS[platform] + "\n"
        "请在五分钟内回复本消息，发送 Netscape 格式的 cookies.txt 文件（最大 1 MiB）。\n"
        "导入后仅供本人使用，默认切换为个人账号；已有个人凭据会被替换。\n"
        "文件将加密保存，并尝试删除聊天中的原附件。输入 /cancelcookie 可取消。",
        reply_markup=ForceReply(force_reply=True, selective=True, input_field_placeholder="回复此消息发送 Cookie 文件"),
    )
    await repo.video_cookie_import_start(query.from_user.id, platform, query.message.chat.id, prompt.message_id)


@router.message(Command("cancelcookie"))
async def cancel_import(message: Message, repo):
    if message.chat.type != "private":
        await message.answer("请在与机器人的私聊中管理 Cookie")
        return
    await repo.video_cookie_import_cancel(message.from_user.id)
    await message.answer("已取消个人 Cookie 导入，已保存的账号保持不变")


@router.message(F.reply_to_message)
async def import_document(message: Message, repo):
    reply = message.reply_to_message
    row = await repo.video_cookie_import(message.chat.id, reply.message_id)
    if row is None:
        # Retain this guard even if request history was removed or creating the
        # request failed, so a credential reply cannot become a media task.
        if reply.from_user and reply.from_user.id == message.bot.id and (reply.text or "").startswith(PROMPT_PREFIX):
            try:
                await message.answer("导入请求已失效，请从平台账号页重新点击导入")
            finally:
                await delete_attachment(message)
            return
        raise SkipHandler()
    if message.chat.type != "private" or row["user_id"] != message.from_user.id:
        await message.answer("只能在本人私聊中导入 Cookie")
        return
    try:
        if row["state"] != "pending" or row["expires"] <= time.time():
            await message.answer("导入请求已处理、取消或过期，请从平台账号页重新点击导入")
            return
        if not message.document:
            await message.answer("请使用文件附件回复本消息，发送 Netscape 格式的 cookies.txt")
            return
        if message.document.file_size and message.document.file_size > MAX_COOKIE_BYTES:
            await message.answer("Cookie 文件不能超过 1 MiB，请重新回复本消息发送文件")
            return
        with CookieBuffer() as buffer:
            try:
                await message.bot.download(message.document, destination=buffer, timeout=30)
            except CookieUploadTooLarge:
                await message.answer("Cookie 文件不能超过 1 MiB，请重新回复本消息发送文件")
                return
            except Exception:
                await message.answer("读取 Cookie 附件失败，请重新回复本消息发送文件")
                return
            try:
                cookies = parse_cookie_upload(buffer.getvalue(), row["platform"])
            except ValueError as exc:
                await message.answer("⚠️ " + str(exc) + "；未替换原有账号，可重新回复本消息发送文件")
                return
        try:
            vault = CredentialVault(Path(settings.db_path).parent)
            saved = await repo.video_cookie_import_complete(
                message.from_user.id, message.chat.id, reply.message_id, vault.encrypt(cookies)
            )
        except Exception:
            await message.answer("暂时无法保存 Cookie，请检查服务状态后重新导入")
            return
        if not saved:
            await message.answer("导入请求已被处理或取消，请从平台账号页重新点击导入")
            return
        text, keyboard = await platform_view(
            repo, message.from_user.id, row["platform"], settings.is_admin(message.from_user.id)
        )
        await message.answer(
            "✅ 个人 Cookie 已导入，默认使用个人账号。账号身份和可用性仍需在解析/下载时验证。\n\n" + text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )
    finally:
        await delete_attachment(message)


async def delete_attachment(message):
    try:
        await message.delete()
    except Exception:
        pass
