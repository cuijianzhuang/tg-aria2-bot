from typing import Any, Awaitable, Callable

from aiogram import BaseMiddleware
from aiogram.types import CallbackQuery, TelegramObject, Update

from bot.config import settings


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
            allowed = user.id in settings.allowed_ids
            if not allowed:
                repo = data.get("repo")
                if repo is not None:
                    allowed = await repo.is_user_allowed(user.id)
            if not allowed:
                if isinstance(event, CallbackQuery):
                    await event.answer("⛔ 你没有权限使用这个机器人。", show_alert=True)
                else:
                    message = getattr(event, "message", None)
                    if message:
                        await message.reply("⛔ 你没有权限使用这个机器人。")
                return None

        return await handler(event, data)


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
