"""过期文件提醒上的「保留」按钮。"""
from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.config import settings

router = Router(name="files")


@router.callback_query(F.data.regexp(r"^expiry:keep:\d+$"))
async def keep_file(query: CallbackQuery, repo):
    row = await repo.get_by_id(int(query.data.split(":")[2]))
    uid = query.from_user.id if query.from_user else None
    if row is None:
        await query.answer("任务不存在", show_alert=True)
        return
    if uid != row["user_id"] and not settings.is_admin(uid):
        await query.answer("⛔ 只能操作自己的任务。", show_alert=True)
        return
    await repo.set_expiry_keep(row["id"], True)
    await query.answer("已保留")
    try:
        await query.message.edit_text("🔒 已保留，这个文件不会被自动删除。", reply_markup=None)
    except Exception:
        pass
