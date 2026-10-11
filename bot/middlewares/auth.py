import logging
import time
from collections.abc import Awaitable, Callable
from html import escape
from typing import Any

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject, Update

from bot.config import settings
from bot.core.keyboards import ACCESS_REQUEST_CALLBACK, access_request_keyboard

log = logging.getLogger(__name__)

# 自动解析管理员用户名的缓存：(解析时间, 联系方式或 None)。成功的缓存 1 小时
# （改了用户名最多一小时后生效）；失败（管理员没私聊过机器人等）只缓存 5 分钟
# 就重试，同时避免陌生人反复发消息时每次都去调 Telegram API
_CONTACT_TTL_OK = 3600.0
_CONTACT_TTL_FAIL = 300.0
_contact_cache: tuple[float, str | None] | None = None

# 同一个陌生人发消息，一分钟内只回复一次——否则谁都能让机器人对着他刷屏
_DENY_REPLY_COOLDOWN = 60.0
_last_denied: dict[int, float] = {}


async def resolve_admin_contact(bot) -> str | None:
    """没有权限的用户该联系谁：.env 的 ADMIN_CONTACT 优先；没配就用管理员
    账号的 Telegram 用户名（get_chat 取，管理员必须私聊过机器人）。"""
    global _contact_cache
    if settings.admin_contact.strip():
        return settings.admin_contact.strip()
    now = time.monotonic()
    if _contact_cache is not None:
        fetched_at, cached = _contact_cache
        if now - fetched_at < (_CONTACT_TTL_OK if cached else _CONTACT_TTL_FAIL):
            return cached
    names: list[str] = []
    ids = sorted(settings.admin_ids or settings.allowed_ids)[:3]
    for uid in ids:
        try:
            chat = await bot.get_chat(uid)
        except Exception:
            log.debug("could not resolve username of admin %s", uid)
            continue
        username = getattr(chat, "username", None)
        if username:
            names.append(f"@{username}")
    contact = " 或 ".join(names) or None
    _contact_cache = (now, contact)
    return contact


def denied_text(user_id: int, contact: str | None, *, html: bool, can_apply: bool = False) -> str:
    """没有权限时给用户看的提示：他自己的 ID + 怎么拿到权限。
    html=True 用于普通消息（ID 放进 <code>，点一下就能复制）；False 用于按钮
    弹窗（纯文本，Telegram 限制 200 字符）。can_apply=True 表示消息下面带
    「申请使用」按钮，文案相应改成"点按钮申请"。"""
    if html:
        who = f"管理员 {escape(contact, quote=False)}" if contact else "管理员"
        if can_apply:
            how = f"点下面的「📨 申请使用」，管理员通过后会通知你。\n也可以把这个 ID 发给{who}。"
        else:
            how = f"请把这个 ID 发给{who}，加入白名单后即可使用。"
        return f"⛔ <b>你没有权限使用这个机器人</b>\n\n你的 ID：<code>{user_id}</code>\n{how}"
    who = f"请联系管理员 {contact}" if contact else "请联系管理员"
    return f"⛔ 你没有权限使用这个机器人\n你的 ID：{user_id}\n{who}"[:200]


class AuthMiddleware(BaseMiddleware):
    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None and isinstance(event, Update):
            user = getattr(event, "message", None) and event.message.from_user

        # ALLOWED_USER_IDS (env) seeds the whitelist; empty means whitelist mode is off
        # entirely (bot open to everyone), matching pre-web-admin behavior. When it's
        # non-empty, allowed_users (DB, manageable from the web admin) adds more users
        # on top without needing a redeploy.
        if user and settings.allowed_ids:
            # 管理员始终放行：ADMIN_USER_IDS 里的人不一定也写在 ALLOWED_USER_IDS 里，
            # 以前这种管理员会被自己的机器人拒之门外（也就收不到使用申请的通知）
            allowed = user.id in settings.allowed_ids or user.id in settings.admin_ids
            if not allowed:
                repo = data.get("repo")
                if repo is not None:
                    allowed = await repo.is_user_allowed(user.id)
            if not allowed:
                # 「申请使用」按钮是没权限的人唯一能点的东西，放行到 access 路由
                if isinstance(event, CallbackQuery) and event.data == ACCESS_REQUEST_CALLBACK:
                    return await handler(event, data)
                await self._deny(event, user.id, data.get("bot"))
                return None

        return await handler(event, data)

    @staticmethod
    async def _deny(event: TelegramObject, user_id: int, bot) -> None:
        contact = await resolve_admin_contact(bot) if bot is not None else (settings.admin_contact.strip() or None)
        if isinstance(event, CallbackQuery):
            await event.answer(denied_text(user_id, contact, html=False), show_alert=True)
            return
        # 普通消息：事件本身就是 Message（以前这里取 event.message，对 Message
        # 永远是 None，陌生人发消息根本收不到任何回复）
        reply = getattr(event, "reply", None)
        if not callable(reply):
            return
        now = time.monotonic()
        if now - _last_denied.get(user_id, -_DENY_REPLY_COOLDOWN) < _DENY_REPLY_COOLDOWN:
            return
        if len(_last_denied) > 1000:  # 别让陌生人把这个表撑大
            for uid in [u for u, t in _last_denied.items() if now - t > _DENY_REPLY_COOLDOWN]:
                del _last_denied[uid]
        _last_denied[user_id] = now
        can_apply = settings.access_request_enabled
        await reply(
            denied_text(user_id, contact, html=True, can_apply=can_apply),
            parse_mode="HTML",
            reply_markup=access_request_keyboard() if can_apply else None,
        )


class AdminMiddleware(BaseMiddleware):
    """Gate for admin-only routers (restart, whitelist, gofile/rclone toggles).

    Unlike AuthMiddleware, this never falls back to "everyone allowed": with no
    admin ids configured anywhere, admin features are simply locked.
    """

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        user = data.get("event_from_user")
        if user is None or not settings.is_admin(user.id):
            if isinstance(event, CallbackQuery):
                await event.answer("⛔ 该操作仅限管理员。", show_alert=True)
            else:
                message = getattr(event, "message", None) or event
                reply = getattr(message, "reply", None)
                if callable(reply):
                    await reply("⛔ 该操作仅限管理员。")
            return None
        return await handler(event, data)
