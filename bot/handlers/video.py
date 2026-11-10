"""Video messages, confirmations and private account controls."""

import json
from pathlib import Path

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import settings
from bot.core import quota, storage
from bot.video.accounts import CredentialVault, account_cookies, is_video_url, platform_for, validate_url
from bot.video.backends import PARSERS
from bot.video.services import get_resolver
from bot.video.ui import MODES, accounts_keyboard, preview_card, preview_keyboard, video_card, video_task_keyboard

router = Router(name="video")


def enabled():
    if not settings.video_enabled:
        raise ValueError("视频功能尚未启用，请管理员安装视频组件并设置 VIDEO_ENABLED=true")


async def make_preview(repo, user_id, chat_id, url, parser="auto", quality="best", mode=None):
    enabled()
    validate_url(url)
    platform = platform_for(url)
    if mode is None:
        mode = await repo.video_preference(user_id, platform) if platform else "anonymous"
        if mode == "personal" and not await repo.video_account(platform, user_id):
            mode = "anonymous"  # The confirmation card explicitly shows 未登录.
    vault = CredentialVault(Path(settings.db_path).parent)
    cookies = await account_cookies(repo, vault, user_id, url, mode)
    with vault.snapshot(cookies) as path:
        info = await get_resolver().probe(url, parser=parser, cookies=path, proxy=settings.proxy_url, quality=quality)
    if quality not in info.qualities:
        quality = "best"
    token = await repo.video_save_preview(
        user_id=user_id,
        chat_id=chat_id,
        url=url,
        parser=info.parser,
        quality=quality,
        account_mode=mode,
        info=info.to_dict(),
    )
    return await repo.video_preview(token, user_id)


@router.message(Command("video"))
async def video_command(message: Message, command: CommandObject, repo):
    url = (command.args or "").strip()
    await show_preview(message, repo, url)


def automatic(message: Message):
    return settings.video_enabled and bool(message.text) and is_video_url(message.text.strip())


@router.message(F.text, automatic)
async def video_message(message: Message, repo):
    await show_preview(message, repo, message.text.strip())


async def show_preview(message, repo, url):
    progress = await message.reply("🔎 正在解析视频…")
    try:
        preview = await make_preview(repo, message.from_user.id, message.chat.id, url)
        await progress.edit_text(preview_card(preview), reply_markup=preview_keyboard(preview), parse_mode="HTML")
    except ValueError as exc:
        await progress.edit_text("⚠️ " + str(exc))


@router.callback_query(F.data.startswith("vp:"))
async def video_preview_action(query: CallbackQuery, repo, task_manager):
    parts = query.data.split(":")
    if len(parts) < 3:
        return
    action, token = parts[1:3]
    preview = await repo.video_preview(token, query.from_user.id)
    if preview is None:
        await query.answer("确认已处理、过期或不属于你，请重新发送链接", show_alert=True)
        return
    if action == "start":
        await query.answer()
        info = json.loads(preview["info"])
        refusal = await quota.admission_error(
            repo, query.from_user.id, info.get("size"), pending_usage=task_manager.pending_usage(query.from_user.id)
        )
        if refusal or not storage.has_enough_space(settings.download_dir, info.get("size") or 0):
            await query.message.answer("⛔ " + (refusal or "磁盘空间不足"))
            return
        row = await repo.video_create_task(token, query.from_user.id, query.message.message_id)
        if row:
            await query.message.edit_text(
                video_card(row, await repo.video_job(row["id"])),
                reply_markup=video_task_keyboard(row["gid"], "PENDING"),
                parse_mode="HTML",
            )
        return
    if action == "cancel":
        await repo.video_discard_preview(token, query.from_user.id)
        await query.message.edit_text("已取消视频下载")
        await query.answer()
        return
    await query.answer("正在重新解析…")
    parser, quality, mode = preview["parser"], preview["quality"], preview["account_mode"]
    value = parts[3] if len(parts) > 3 else ""
    if action == "p" and value in PARSERS:
        parser, quality = value, "best"
    elif action == "q" and value.isdigit():
        qualities = json.loads(preview["info"])["qualities"]
        if int(value) >= len(qualities):
            return
        quality = qualities[int(value)]
    elif action == "a" and value in MODES:
        mode = value
    else:
        return
    try:
        new = await make_preview(repo, query.from_user.id, preview["chat_id"], preview["url"], parser, quality, mode)
        await repo.video_discard_preview(token, query.from_user.id)
        platform = platform_for(preview["url"])
        if platform:
            await repo.video_preference(query.from_user.id, platform, mode)
        await query.message.edit_text(preview_card(new), reply_markup=preview_keyboard(new), parse_mode="HTML")
    except ValueError as exc:
        await query.message.answer("⚠️ " + str(exc))


@router.message(Command("videoaccounts"))
async def account_command(message: Message):
    if message.chat.type != "private":
        await message.reply("请在与机器人的私聊中管理平台账号")
        return
    await message.reply("🔑 视频平台账号：个人账号默认优先；共享账号需主动选择。", reply_markup=accounts_keyboard())


@router.callback_query(F.data.startswith("vacc:"))
async def account_action(query: CallbackQuery, repo):
    if query.message.chat.type != "private":
        await query.answer("请在私聊管理账号", show_alert=True)
        return
    parts = query.data.split(":")
    if parts[1] == "menu":
        await query.message.answer("🔑 视频平台账号", reply_markup=accounts_keyboard())
        await query.answer()
        return
    if len(parts) != 3 or parts[2] not in ("bilibili", "douyin", "xhs"):
        return
    action, platform = parts[1:]
    admin = settings.is_admin(query.from_user.id)
    if action in ("shared", "share", "unshare", "remove-shared") and not admin:
        await query.answer("仅管理员可管理共享账号", show_alert=True)
        return
    if action in ("personal", "shared"):
        if not settings.video_login_enabled:
            await query.answer("扫码服务未启用，请管理员启动 video-login 服务", show_alert=True)
            return
        login_id = await repo.video_new_login(
            platform, 0 if action == "shared" else query.from_user.id, query.from_user.id, query.message.chat.id
        )
        await query.message.answer("🔑 正在生成二维码，最长等待五分钟。")
        if settings.video_public_url:
            token = await repo.video_grant(login_id)
            # Fragment tokens are not sent in HTTP access logs or Referer.
            await query.message.answer(
                "也可打开专属 Web 扫码页（五分钟内使用一次）",
                reply_markup=InlineKeyboardMarkup(
                    inline_keyboard=[
                        [
                            InlineKeyboardButton(
                                text="🌐 打开扫码页",
                                url=settings.video_public_url.rstrip("/") + "/video-login#" + token,
                            )
                        ]
                    ]
                ),
            )
    elif action in ("remove", "remove-shared"):
        await repo.video_remove_account(platform, 0 if action == "remove-shared" else query.from_user.id)
        await query.message.answer("已移除本机登录状态；正在执行的任务结束后清理临时凭据。")
    elif action in ("share", "unshare"):
        await repo.video_share(platform, action == "share")
    elif action == "platform":
        from html import escape

        personal = await repo.video_account(platform, query.from_user.id)
        shared = await repo.video_account(platform, 0)
        text = f"🔑 {platform}\n个人：{escape(personal['identity']) if personal else '未登录'}\n共享：{'已开放' if shared and shared['enabled'] else '未开放'}"
        buttons = [
            [
                InlineKeyboardButton(text="📱 个人扫码登录", callback_data=f"vacc:personal:{platform}"),
                InlineKeyboardButton(text="🗑 移除个人登录", callback_data=f"vacc:remove:{platform}"),
            ]
        ]
        if admin:
            buttons += [
                [
                    InlineKeyboardButton(text="👥 共享账号扫码", callback_data=f"vacc:shared:{platform}"),
                    InlineKeyboardButton(text="🗑 移除共享", callback_data=f"vacc:remove-shared:{platform}"),
                ],
                [
                    InlineKeyboardButton(text="✅ 开放共享", callback_data=f"vacc:share:{platform}"),
                    InlineKeyboardButton(text="⛔ 关闭共享", callback_data=f"vacc:unshare:{platform}"),
                ],
            ]
        await query.message.answer(text, reply_markup=InlineKeyboardMarkup(inline_keyboard=buttons), parse_mode="HTML")
    await query.answer()


@router.callback_query(F.data.startswith("vlogin:"))
async def login_action(query: CallbackQuery, repo):
    _, action, login_id = query.data.split(":", 2)
    row = await repo.video_login(login_id)
    if row is None or row["requester_id"] != query.from_user.id or query.message.chat.type != "private":
        await query.answer("无权管理该登录会话", show_alert=True)
        return
    await repo.video_login_update(login_id, state="cancelled")
    if action == "refresh":
        await repo.video_new_login(row["platform"], row["owner_id"], row["requester_id"], row["chat_id"])
    await query.answer("已刷新" if action == "refresh" else "已取消")
