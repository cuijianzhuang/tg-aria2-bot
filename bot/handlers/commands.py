from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.core.cards import render_home
from bot.core.keyboards import main_inline_keyboard
from bot.core.list_view import render_task_overview

router = Router(name="commands")


@router.message(Command("start"))
@router.message(Command("status"))  # status page merged into the home dashboard
async def cmd_start(message: Message, repo, aria2):
    counts = await repo.count_by_status()
    try:
        stats = await aria2.global_stat()
    except Exception:
        stats = None
    await message.reply(render_home(counts, stats), reply_markup=main_inline_keyboard(counts), parse_mode="HTML")


@router.message(Command("list"))
async def cmd_list(message: Message, repo, aria2):
    text, markup = await render_task_overview(repo)
    await message.reply(text, reply_markup=markup, parse_mode="HTML")


@router.message(Command("pause"))
async def cmd_pause(message: Message, command: CommandObject, aria2, repo):
    gid = await _resolve_gid(command, repo)
    if not gid:
        await message.reply("用法: /pause <任务id>")
        return
    await aria2.pause(gid)
    await message.reply("⏸ 已暂停")


@router.message(Command("resume"))
async def cmd_resume(message: Message, command: CommandObject, aria2, repo):
    gid = await _resolve_gid(command, repo)
    if not gid:
        await message.reply("用法: /resume <任务id>")
        return
    await aria2.resume(gid)
    await message.reply("▶️ 已恢复")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, command: CommandObject, aria2, repo):
    gid = await _resolve_gid(command, repo)
    if not gid:
        await message.reply("用法: /cancel <任务id>")
        return
    await aria2.remove(gid)
    await repo.update_status(gid, "CANCELLED")
    await message.reply("🗑 已取消，控制文件由 aria2 钩子脚本自动清理")


@router.message(Command("limit"))
async def cmd_limit(message: Message, command: CommandObject, aria2):
    if not command.args:
        await message.reply("用法: /limit 2M  (0 表示不限速)")
        return
    await aria2.set_global_limit(command.args.strip())
    await message.reply(f"✅ 全局限速已设置为 {command.args.strip()}")


async def _resolve_gid(command: CommandObject, repo) -> str | None:
    if not command.args:
        return None
    try:
        task_id = int(command.args.strip())
    except ValueError:
        return None
    row = await repo.get_by_id(task_id)
    return row["gid"] if row else None
