"""运维类管理员命令：/schedule（限速时段）。整个 router 挂 AdminMiddleware。"""
import logging
from datetime import datetime
from html import escape

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.config import settings
from bot.core import schedule
from bot.core.conf_editor import write_kv
from bot.middlewares.auth import AdminMiddleware

log = logging.getLogger(__name__)
router = Router(name="ops")
router.message.middleware(AdminMiddleware())

SCHEDULE_HELP = (
    "用法：\n"
    "<code>/schedule set 23:00-07:00=0;07:00-23:00=2M</code>　设置\n"
    "<code>/schedule off</code>　关闭\n\n"
    "每条规则是「开始-结束=限速」，限速 <code>0</code> 表示不限，也可以写 <code>500K</code>、<code>2M</code>；"
    "时段可以跨午夜，不在任何时段内时不限速；时间是服务器本地时间。"
)


def _persist(value: str):
    try:
        write_kv(".env", "SPEED_SCHEDULE", value)
    except OSError:
        log.warning("could not persist SPEED_SCHEDULE to .env")


@router.message(Command("schedule"))
async def cmd_schedule(message: Message, command: CommandObject):
    args = (command.args or "").strip()
    if not args:
        try:
            rules = schedule.parse(settings.speed_schedule)
        except ValueError:
            rules = []
        await message.reply(
            f"⏱ <b>限速时段</b>\n\n{schedule.describe(rules, datetime.now().time())}\n\n{SCHEDULE_HELP}",
            parse_mode="HTML",
        )
        return
    if args.lower() in ("off", "关闭", "clear"):
        settings.speed_schedule = ""
        _persist("")
        await message.reply("✅ 限速时段已关闭。当前的全局限速保持不变，可在 设置 → 全局限速 里调整。")
        return
    if args.lower().startswith("set "):
        args = args[4:].strip()
    try:
        rules = schedule.parse(args)
    except ValueError as e:
        await message.reply(f"⚠️ {escape(str(e), quote=False)}\n\n{SCHEDULE_HELP}", parse_mode="HTML")
        return
    if not rules:
        await message.reply(SCHEDULE_HELP, parse_mode="HTML")
        return
    value = ";".join(str(r) for r in rules)
    settings.speed_schedule = value
    _persist(value)
    await message.reply(
        f"✅ 已设置，30 秒内生效。\n\n{schedule.describe(rules, datetime.now().time())}", parse_mode="HTML",
    )
