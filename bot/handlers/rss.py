"""RSS 订阅命令：/rss。每个人管理自己的订阅，管理员能看到并删除所有人的。"""
import logging
from html import escape

from aiogram import F, Router
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import settings
from bot.core import rss, vip
from bot.core.rss import FeedError

log = logging.getLogger(__name__)
router = Router(name="rss")

HELP = (
    "用法：\n"
    "<code>/rss add 订阅地址 [关键词…] [-排除词…]</code>　添加订阅\n"
    "<code>/rss</code>　查看、暂停、删除订阅\n\n"
    "例：<code>/rss add https://example.com/feed.xml 1080p 简体 -720p</code>\n"
    "标题里要<b>同时包含</b>所有关键词、且不含任何排除词的新条目才会自动下载。"
    "添加时源里已有的条目不会下载，只下载之后出现的。"
)


def _can_manage(user_id: int | None, owner_id: int) -> bool:
    return user_id is not None and (user_id == owner_id or settings.is_admin(user_id))


def _is_admin(user_id: int | None) -> bool:
    return settings.is_admin(user_id)


def render_feeds(feeds, *, show_owner: bool) -> tuple[str, InlineKeyboardMarkup]:
    if not feeds:
        return f"📡 <b>RSS 订阅</b>\n\n还没有订阅。\n\n{HELP}", InlineKeyboardMarkup(inline_keyboard=[])
    lines = ["📡 <b>RSS 订阅</b>", ""]
    rows = []
    for f in feeds:
        name = escape(f["title"] or f["url"], quote=False)[:60]
        state = "" if f["enabled"] else " ⏸"
        lines.append(f"<b>#{f['id']}</b> {name}{state}")
        if f["keyword"] or f["exclude"]:
            parts = ([f"含 {escape(f['keyword'], quote=False)}"] if f["keyword"] else []) + \
                    ([f"不含 {escape(f['exclude'], quote=False)}"] if f["exclude"] else [])
            lines.append("　" + "，".join(parts))
        if show_owner:
            lines.append(f"　订阅人 <code>{f['user_id']}</code>")
        if f["last_error"]:
            lines.append(f"　⚠️ {escape(f['last_error'], quote=False)}")
        rows.append([
            InlineKeyboardButton(text=f"{'▶️ 恢复' if not f['enabled'] else '⏸ 暂停'} #{f['id']}", callback_data=f"rss:toggle:{f['id']}"),
            InlineKeyboardButton(text=f"🗑 删除 #{f['id']}", callback_data=f"rss:del:{f['id']}"),
        ])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=rows)


async def _list_view(repo, user_id: int):
    feeds = await repo.list_feeds(user_id=None if _is_admin(user_id) else user_id)
    return render_feeds(feeds, show_owner=_is_admin(user_id))


@router.message(Command("rss"))
async def cmd_rss(message: Message, command: CommandObject, repo, nodes, task_manager=None):
    user = message.from_user
    args = (command.args or "").split()
    if not args:
        text, markup = await _list_view(repo, user.id)
        await message.reply(text, reply_markup=markup, parse_mode="HTML")
        return
    if args[0].lower() != "add" or len(args) < 2:
        await message.reply(HELP, parse_mode="HTML")
        return
    try:
        url = rss.validate_url(args[1])
    except FeedError as e:
        await message.reply(f"⚠️ {escape(str(e), quote=False)}")
        return
    words = args[2:]
    exclude = " ".join(w[1:] for w in words if w.startswith("-") and len(w) > 1)
    keyword = " ".join(w for w in words if not w.startswith("-"))

    if not await vip.feature_allowed(repo, user.id, "rss"):
        await message.reply(vip.denied_text("rss"))
        return
    limit = vip.max_feeds(await vip.is_vip(repo, user.id))
    if not _is_admin(user.id) and len(await repo.list_feeds(user_id=user.id)) >= limit:
        await message.reply(f"⚠️ 你最多订阅 {limit} 个源，请先删除不用的。")
        return
    node = nodes.resolve(await repo.get_current_node(user.id)).name
    feed_id = await repo.add_feed(
        user_id=user.id, chat_id=message.chat.id, url=url, keyword=keyword, exclude=exclude, node=node,
    )
    if feed_id is None:
        await message.reply("ℹ️ 你已经订阅过这个地址了，发 /rss 管理。")
        return
    # 立刻抓一次：验证地址可用，同时把源里现有的条目记为"见过"
    await rss.poll_feed(await repo.get_feed(feed_id), bot=message.bot, repo=repo, nodes=nodes, task_manager=task_manager)
    feed = await repo.get_feed(feed_id)
    if feed["last_error"]:
        await repo.delete_feed(feed_id)
        await message.reply(f"⚠️ 订阅失败：{escape(feed['last_error'], quote=False)}")
        return
    seen = len(await repo.seen_guids(feed_id))
    flt = f"\n过滤：含「{escape(keyword, quote=False)}」" if keyword else ""
    flt += f"，不含「{escape(exclude, quote=False)}」" if exclude else ""
    await message.reply(
        f"✅ 已订阅 <b>{escape(feed['title'] or url, quote=False)}</b>（#{feed_id}）{flt}\n"
        f"源里现有的 {seen} 条不会下载，之后出现的新条目会自动加入下载"
        f"（约每 {settings.rss_check_minutes} 分钟检查一次）。",
        parse_mode="HTML",
    )


@router.callback_query(F.data.regexp(r"^rss:(toggle|del):\d+$"))
async def rss_action(query: CallbackQuery, repo):
    _, action, raw = query.data.split(":")
    feed = await repo.get_feed(int(raw))
    if feed is None:
        await query.answer("这个订阅已经不存在了。", show_alert=True)
    elif not _can_manage(query.from_user.id if query.from_user else None, feed["user_id"]):
        await query.answer("⛔ 只能管理自己的订阅。", show_alert=True)
        return
    elif action == "del":
        await repo.delete_feed(feed["id"])
        await query.answer("已删除")
    else:
        await repo.set_feed_enabled(feed["id"], not feed["enabled"])
        await query.answer("已恢复" if not feed["enabled"] else "已暂停")
    text, markup = await _list_view(repo, query.from_user.id)
    try:
        await query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    except Exception:
        pass
