"""Edit an existing text menu; acknowledge callbacks before slow work."""

from aiogram.exceptions import TelegramBadRequest


async def edit_menu(query, text, reply_markup=None):
    message = query.message
    try:
        await message.edit_text(text, reply_markup=reply_markup, parse_mode="HTML")
    except TelegramBadRequest as exc:
        if "message is not modified" in str(exc).lower():
            return
        # QR notifications are photos and cannot be edited as text. Keep the
        # photo lifecycle separate, but make navigation from its buttons work.
        if not getattr(message, "text", None):
            await message.answer(text, reply_markup=reply_markup, parse_mode="HTML")
        else:
            raise


def preview_back_token(message):
    markup = getattr(message, "reply_markup", None)
    for row in markup.inline_keyboard if markup else []:
        for button in row:
            data = button.callback_data or ""
            if data.startswith("vp:back:"):
                return data.split(":", 2)[2]
    return None


_active_actions = set()


def single_menu_action(handler):
    """Ignore overlapping operations on the same message, release on failure."""
    from functools import wraps

    @wraps(handler)
    async def wrapped(query, *args, **kwargs):
        key = (query.message.chat.id, query.message.message_id)
        if key in _active_actions:
            await query.answer("正在处理，请稍候")
            return
        _active_actions.add(key)
        try:
            return await handler(query, *args, **kwargs)
        finally:
            _active_actions.discard(key)

    return wrapped
