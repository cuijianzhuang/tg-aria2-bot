"""VIP 命令（/vip /redeem）和授权命令（/license）。

/vip 和 /redeem 所有白名单用户可用；/vip 的管理子命令和 /license 的设置在函数里检查管理员，
不整个 router 挂 AdminMiddleware，因为同一个命令对普通用户和管理员行为不同。
"""
import logging
from datetime import datetime
from html import escape

from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.config import settings
from bot.core import license as lic
from bot.core import vip
from bot.core.vip import VipError

log = logging.getLogger(__name__)
router = Router(name="vip")

ADMIN_HELP = (
    "<b>管理员命令</b>\n"
    "<code>/vip grant 用户ID 天数|forever</code>　授予或顺延 VIP\n"
    "<code>/vip revoke 用户ID</code>　撤销\n"
    "<code>/vip list</code>　VIP 用户\n"
    "<code>/vip code 天数|forever [个数]</code>　生成兑换码（一次最多 20 个）\n"
    "<code>/vip codes</code>　还没用的兑换码\n"
    "<code>/vip delcode 兑换码</code>　作废一个没用过的码"
)


def _fmt(ts: float | None) -> str:
    return "永久" if ts is None else f"{datetime.fromtimestamp(ts):%Y-%m-%d %H:%M}"


def _parse_days(text: str) -> int:
    t = text.strip().lower()
    if t in ("forever", "永久", "0"):
        return 0
    if not t.isdigit() or not 0 < int(t) <= 3650:
        raise VipError("天数请写 1–3650 的整数，或 forever（永久）。")
    return int(t)


def _is_admin(message: Message) -> bool:
    return settings.is_admin(message.from_user.id if message.from_user else None)


async def _notify(bot, user_id: int, text: str):
    try:
        await bot.send_message(chat_id=user_id, text=text, parse_mode="HTML")
    except Exception:
        log.info("could not notify user %s about VIP change", user_id)


def _invalidate(task_manager):
    if task_manager is not None:
        task_manager.invalidate_limits()     # 额度缓存里还是旧的等级，立刻丢掉


async def _status_text(repo, user_id: int) -> str:
    if not vip.system_active():
        return "ℹ️ VIP 功能当前不可用（机器人没有 Pro 授权）。"
    is_vip, exp = await vip.get_status(repo, user_id)
    if settings.is_admin(user_id):
        return "👑 你是管理员，不受任何额度和功能限制，不需要 VIP。"
    gated = vip.gated_features()
    perks = ["• 更高的下载额度（/quota 查看）", "• 新任务排在等待队列最前面" if settings.vip_priority else None,
             f"• RSS 订阅最多 {settings.rss_max_feeds_vip} 个（普通 {settings.rss_max_feeds_per_user} 个）"]
    perks += [f"• {vip.FEATURES[f]}" for f in sorted(gated)]
    perk_text = "\n".join(p for p in perks if p)
    if is_vip:
        return f"👑 <b>你是 VIP</b>\n有效期：{_fmt(exp)}\n\n{perk_text}"
    contact = f"，或联系管理员 {escape(settings.admin_contact)}" if settings.admin_contact else "，或联系管理员"
    return (
        f"你现在是普通用户。\n\n<b>VIP 权益</b>\n{perk_text}\n\n"
        f"有兑换码的话发 <code>/redeem 兑换码</code>{contact}。"
    )


@router.message(Command("vip"))
async def cmd_vip(message: Message, command: CommandObject, repo, task_manager=None):
    user = message.from_user
    if user is None:
        return
    args = (command.args or "").split()
    if not args:
        text = await _status_text(repo, user.id)
        if settings.is_admin(user.id):
            text += "\n\n" + ADMIN_HELP
        await message.reply(text, parse_mode="HTML")
        return
    if not _is_admin(message):
        await message.reply("只有管理员可以管理 VIP。想兑换请发 /redeem 兑换码。")
        return
    sub, rest = args[0].lower(), args[1:]
    try:
        await _admin(message, sub, rest, repo, task_manager)
    except VipError as e:
        await message.reply(f"⚠️ {escape(str(e), quote=False)}")


async def _admin(message: Message, sub: str, rest: list[str], repo, task_manager):
    admin_id = message.from_user.id
    if sub == "grant" and len(rest) == 2 and rest[0].isdigit():
        uid = int(rest[0])
        exp = await vip.extend(repo, uid, _parse_days(rest[1]), granted_by=admin_id)
        _invalidate(task_manager)
        await message.reply(f"✅ <code>{uid}</code> 的 VIP：{_fmt(exp)}", parse_mode="HTML")
        await _notify(message.bot, uid, f"👑 管理员给你开通了 VIP，有效期：{_fmt(exp)}。发 /vip 查看权益。")
    elif sub == "revoke" and len(rest) == 1 and rest[0].isdigit():
        uid = int(rest[0])
        ok = await repo.remove_vip(uid)
        _invalidate(task_manager)
        await message.reply(f"✅ 已撤销 <code>{uid}</code> 的 VIP。" if ok else "这个用户本来就不是 VIP。", parse_mode="HTML")
    elif sub == "list":
        rows = await repo.list_vips()
        now = datetime.now().timestamp()
        if not rows:
            await message.reply("还没有 VIP 用户。")
            return
        lines = [f"👑 <b>VIP 用户</b>（{len(rows)}）", ""]
        for r in rows[:40]:
            expired = r["expires_at"] is not None and r["expires_at"] <= now
            lines.append(f"<code>{r['user_id']}</code>　{_fmt(r['expires_at'])}{'　（已过期）' if expired else ''}")
        if len(rows) > 40:
            lines.append(f"…还有 {len(rows) - 40} 个")
        await message.reply("\n".join(lines), parse_mode="HTML")
    elif sub == "code" and 1 <= len(rest) <= 2:
        days = _parse_days(rest[0])
        count = int(rest[1]) if len(rest) == 2 and rest[1].isdigit() else 1
        codes = await vip.create_codes(repo, days, count, created_by=admin_id)
        label = "永久" if days == 0 else f"{days} 天"
        body = "\n".join(f"<code>{c}</code>" for c in codes)
        await message.reply(f"🎟 已生成 {len(codes)} 个 {label} 兑换码（一次性）：\n\n{body}\n\n"
                            "发给用户，让他发 /redeem 兑换码 即可。", parse_mode="HTML")
    elif sub == "codes":
        rows = await repo.list_vip_codes()
        if not rows:
            await message.reply("没有未使用的兑换码。")
            return
        lines = ["🎟 <b>未使用的兑换码</b>", ""]
        lines += [f"<code>{r['code']}</code>　{'永久' if r['days'] == 0 else str(r['days']) + ' 天'}" for r in rows]
        await message.reply("\n".join(lines), parse_mode="HTML")
    elif sub == "delcode" and len(rest) == 1:
        code = vip.normalize_code(rest[0])
        ok = bool(code) and await repo.delete_vip_code(code)
        await message.reply("✅ 已作废。" if ok else "没有找到这个码，或者它已经被使用了。")
    else:
        await message.reply(ADMIN_HELP, parse_mode="HTML")


@router.message(Command("redeem"))
async def cmd_redeem(message: Message, command: CommandObject, repo, task_manager=None):
    user = message.from_user
    if user is None:
        return
    if not command.args:
        await message.reply("用法：<code>/redeem 兑换码</code>", parse_mode="HTML")
        return
    try:
        exp = await vip.redeem(repo, user.id, command.args)
    except VipError as e:
        await message.reply(f"⚠️ {escape(str(e), quote=False)}")
        return
    _invalidate(task_manager)
    try:
        await message.delete()      # 码已经失效了，删掉只是不留在聊天记录里
    except Exception:
        pass
    await message.answer(f"🎉 兑换成功！你现在是 VIP，有效期：{_fmt(exp)}。发 /vip 查看权益。")


@router.message(Command("license"))
async def cmd_license(message: Message, command: CommandObject):
    if not _is_admin(message):
        await message.reply("只有管理员可以查看或设置授权。")
        return
    token = (command.args or "").strip()
    if not token:
        extra = f"\n\nBot ID：<code>{lic.bot_id()}</code>（申请授权时把它发给发行方）" if lic.enforced() else ""
        await message.reply(lic.status_text() + extra + ("\n\n激活：<code>/license 授权码</code>" if lic.enforced() else ""),
                            parse_mode="HTML")
        return
    try:
        info = lic.verify(token)
    except lic.LicenseError as e:
        await message.reply(f"⚠️ {escape(str(e), quote=False)}")
        return
    lic.save_token(token)
    try:
        await message.delete()
    except Exception:
        pass
    await message.answer("✅ 授权已激活。\n\n" + lic.status_text(), parse_mode="HTML")
    log.info("license activated for %r (bot %s)", info.name, info.bot_id)
