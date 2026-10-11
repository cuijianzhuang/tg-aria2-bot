"""没有权限的人点「📨 申请使用」→ 通知管理员 → 管理员一键通过/拒绝。

这个文件是申请人这一侧（任何人都能点，AuthMiddleware 对 access:request 单独放行）；
管理员点的「通过/拒绝」在 handlers/admin.py 里，整个 router 受 AdminMiddleware 保护。
"""
import logging
import time
from html import escape

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.config import settings
from bot.core.keyboards import ACCESS_REQUEST_CALLBACK, access_decision_keyboard

log = logging.getLogger(__name__)
router = Router(name="access")

# 申请一直没人处理时，申请人再点一次会重新通知管理员，但不会比这更频繁
RENOTIFY_AFTER_SECONDS = 6 * 3600


def who_html(username: str | None, full_name: str | None) -> str:
    """'张三 @zhangsan' 形式的称呼（HTML 转义过）。"""
    parts = []
    if full_name:
        parts.append(escape(full_name, quote=False))
    if username:
        parts.append(f"@{escape(username, quote=False)}")
    return " ".join(parts) or "（未知）"


def render_request(row) -> str:
    return (
        "🙋 <b>新的使用申请</b>\n\n"
        f"昵称：{escape(row['full_name'] or '（无）', quote=False)}\n"
        f"用户名：{('@' + escape(row['username'], quote=False)) if row['username'] else '（无）'}\n"
        f"ID：<code>{row['user_id']}</code>"
    )


def admin_recipients() -> list[int]:
    # 跟 TaskManager._notify_admins 同一个口径：明确配置的管理员优先，没配就用白名单
    return sorted(settings.admin_ids or settings.allowed_ids)


async def notify_admins(bot, row) -> int:
    """把申请发给所有管理员，返回成功送达的人数（管理员必须私聊过机器人才收得到）。"""
    delivered = 0
    for admin_id in admin_recipients():
        try:
            await bot.send_message(
                chat_id=admin_id, text=render_request(row), parse_mode="HTML",
                reply_markup=access_decision_keyboard(row["user_id"]),
            )
            delivered += 1
        except Exception:
            log.warning("could not deliver access request of %s to admin %s", row["user_id"], admin_id)
    return delivered


async def _reply(query: CallbackQuery, text: str, *, alert: bool = False):
    """优先改掉那条"没有权限"的消息（按钮跟着消失，不会再点），改不了就弹窗。"""
    if alert or not query.message:
        await query.answer(text[:200], show_alert=True)
        return
    try:
        await query.message.edit_text(text, parse_mode="HTML", reply_markup=None)
    except Exception:
        await query.answer(text[:200], show_alert=True)
        return
    await query.answer()


@router.callback_query(F.data == ACCESS_REQUEST_CALLBACK)
async def request_access(query: CallbackQuery, repo, bot):
    user = query.from_user
    if user is None:
        await query.answer()
        return
    if not settings.access_request_enabled:
        await query.answer("管理员没有开放申请，请直接联系管理员。", show_alert=True)
        return

    # 已经有权限了（比如刚被管理员加进去）
    if user.id in settings.allowed_ids or user.id in settings.admin_ids or await repo.is_user_allowed(user.id):
        await _reply(query, "✅ 你已经有权限了，发送 /start 开始使用。")
        return

    now = time.time()
    row = await repo.get_access_request(user.id)
    if row is not None and row["status"] == "pending":
        # 重复申请只算一次：不再重复打扰管理员，除非上一次根本没送达或太久没人理
        notified_at = row["notified_at"]
        if notified_at and now - notified_at < RENOTIFY_AFTER_SECONDS:
            await _reply(query, "📨 你的申请已经提交，请等待管理员处理，结果会通知你。")
            return
    elif row is not None and row["status"] == "rejected" and row["decided_at"]:
        wait = row["decided_at"] + settings.access_request_cooldown_hours * 3600 - now
        if wait > 0:
            hours = max(1, round(wait / 3600))
            await _reply(query, f"⛔ 你的申请没有通过，约 {hours} 小时后可以再次申请。", alert=True)
            return
        await repo.upsert_access_request(user.id, user.username, user.full_name, now=now)
    else:
        # 新申请，或者之前通过了但后来又被移出白名单
        await repo.upsert_access_request(user.id, user.username, user.full_name, now=now)

    row = await repo.get_access_request(user.id)
    if await notify_admins(bot, row) == 0:
        await _reply(query, "暂时联系不上管理员，请稍后再试，或者直接联系管理员。", alert=True)
        return
    await repo.mark_request_notified(user.id, now=now)
    await _reply(query, "📨 <b>申请已提交</b>\n\n管理员处理后会通知你，请稍等。")
