from aiogram import Router
from aiogram.filters import Command, CommandObject
from aiogram.types import Message

from bot.core.cards import render_home
from bot.core.keyboards import main_inline_keyboard
from bot.core.list_view import render_search_results, render_task_list
from bot.core.node_pool import NodeUnavailable
from bot.core.stats_view import DEFAULT_PERIOD, render_stats_view

router = Router(name="commands")


@router.message(Command("start"))
@router.message(Command("status"))  # status page merged into the home dashboard
async def cmd_start(message: Message, repo, nodes):
    counts = await repo.count_by_status()
    # 首页速度取 default 节点，跟 nav:start 保持一致（多节点速度看统计/选择器）
    try:
        stats = await nodes.get("default").global_stat()
    except Exception:
        stats = None
    node_label = None
    if nodes.is_multi() and message.from_user:
        preferred = await repo.get_current_node(message.from_user.id)
        node_label = nodes.resolve(preferred).display_name
    await message.reply(
        render_home(counts, stats),
        reply_markup=main_inline_keyboard(counts, node_label=node_label),
        parse_mode="HTML",
    )


@router.message(Command("list"))
async def cmd_list(message: Message, repo, nodes):
    text, markup = await render_task_list(repo, nodes, "ALL", 0)
    await message.reply(text, reply_markup=markup, parse_mode="HTML")


@router.message(Command("find"))
async def cmd_find(message: Message, command: CommandObject, repo, nodes):
    if not command.args:
        await message.reply("用法: /find 关键词（按文件名模糊搜索历史任务）")
        return
    text, markup = await render_search_results(repo, nodes, command.args.strip())
    await message.reply(text, reply_markup=markup, parse_mode="HTML")


@router.message(Command("stats"))
async def cmd_stats(message: Message, repo):
    text, markup = await render_stats_view(repo, DEFAULT_PERIOD)
    await message.reply(text, reply_markup=markup, parse_mode="HTML")


async def _resolve_task(command: CommandObject, repo):
    """/pause 等命令用任务 id 定位任务行（None = 参数无效或任务不存在）。"""
    if not command.args:
        return None
    try:
        task_id = int(command.args.strip())
    except ValueError:
        return None
    return await repo.get_by_id(task_id)


def _client_for(nodes, row):
    return nodes.get(row["node"])


@router.message(Command("pause"))
async def cmd_pause(message: Message, command: CommandObject, repo, nodes):
    row = await _resolve_task(command, repo)
    if row is None or not row["gid"]:
        await message.reply("用法: /pause <任务id>")
        return
    try:
        await _client_for(nodes, row).pause(row["gid"])
    except NodeUnavailable:
        await message.reply("⛔ 该任务所在节点已被删除或停用。")
        return
    await repo.update_status(row["gid"], "PAUSED")
    await message.reply("⏸ 已暂停")


@router.message(Command("resume"))
async def cmd_resume(message: Message, command: CommandObject, repo, nodes):
    row = await _resolve_task(command, repo)
    if row is None or not row["gid"]:
        await message.reply("用法: /resume <任务id>")
        return
    try:
        await _client_for(nodes, row).resume(row["gid"])
    except NodeUnavailable:
        await message.reply("⛔ 该任务所在节点已被删除或停用。")
        return
    await repo.update_status(row["gid"], "ACTIVE")
    await message.reply("▶️ 已恢复")


@router.message(Command("cancel"))
async def cmd_cancel(message: Message, command: CommandObject, repo, nodes):
    row = await _resolve_task(command, repo)
    if row is None or not row["gid"]:
        await message.reply("用法: /cancel <任务id>")
        return
    try:
        await _client_for(nodes, row).remove(row["gid"])
    except NodeUnavailable:
        await message.reply("⛔ 该任务所在节点已被删除或停用。")
        return
    await repo.update_status(row["gid"], "CANCELLED")
    await message.reply("🗑 已取消，控制文件由 aria2 钩子脚本自动清理")


@router.message(Command("limit"))
async def cmd_limit(message: Message, command: CommandObject, aria2):
    # 全局限速仍作用于 default 节点（跟设置菜单一致）；各节点限速去设置里按节点调
    if not command.args:
        await message.reply("用法: /limit 2M  (0 表示不限速)")
        return
    await aria2.set_global_limit(command.args.strip())
    await message.reply(f"✅ 全局限速已设置为 {command.args.strip()}")
