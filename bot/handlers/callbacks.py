import asyncio
import logging
import os

from aiogram import F, Router
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import settings
from bot.core import quota, storage
from bot.core.aria2_client import multi_file
from bot.core.cards import (
    render_file_selection,
    render_home,
    render_node_chooser,
    render_pending_card,
    render_task_card,
    render_task_limit_chooser,
)
from bot.core.compress import remove_path
from bot.core.download_service import add_source
from bot.core.keyboards import (
    BACK_TO_LIST_TEXT,
    LIMIT_PRESETS,
    cleanup_confirm_keyboard,
    file_selection_keyboard,
    main_inline_keyboard,
    node_chooser_keyboard,
    pending_node_chooser_keyboard,
    pending_task_keyboard,
    task_cancel_confirm_keyboard,
    task_delete_confirm_keyboard,
    task_keyboard,
    task_limit_chooser_keyboard,
)
from bot.core.list_view import LIST_STATUS_MAP, render_task_list
from bot.core.node_pool import NodeUnavailable
from bot.core.stats_view import render_daily_view, render_stats_view
from bot.db.models import map_aria2_status
from bot.handlers.menu import edit_or_answer
from bot.handlers.video_tasks import handle_video_task

log = logging.getLogger(__name__)
router = Router(name="callbacks")

def _can_manage(query: CallbackQuery, owner_id: int | None) -> bool:
    """Task-level authorization: owner or admin. callback_data is forgeable and
    tasks are per-user, so every gid/token coming off a button gets checked."""
    user = query.from_user
    if user is None:
        return False
    return user.id == owner_id or settings.is_admin(user.id)


_TOAST = {
    "pause": "已暂停",
    "resume": "已继续",
    "cancel_only": "已取消任务",
    "delete_files": "已取消任务并删除文件",
}


def _client_for_row(nodes, row):
    """按任务归属节点取客户端。节点被删/停用时抛 NodeUnavailable，
    调用方给用户一个明确的提示而不是隐式落到错误节点上。"""
    return nodes.get(row["node"])


async def _current_node_label(query: CallbackQuery, repo, nodes) -> str | None:
    """主菜单节点行的显示名；单节点部署返回 None（不显示该行）。"""
    if not nodes.is_multi():
        return None
    preferred = await repo.get_current_node(query.from_user.id) if query.from_user else "default"
    return nodes.resolve(preferred).display_name


# 打开后会盖住任务卡片的子菜单：用户在里面操作时不能让自动刷新把它冲掉
_SUBMENU_ACTIONS = {"cancel", "confirm_delete_files", "delete", "confirm_purge", "files", "limit"}


def _attend(task_manager, gid: str, *, submenu: bool = False):
    """告诉 TaskManager 用户正在和这张卡片互动：普通操作 → 接下来一段时间
    快速刷新；打开子菜单 → 暂停自动刷新直到用户返回。"""
    if task_manager is None:
        return
    if submenu:
        task_manager.hold(gid)
    else:
        task_manager.watch(gid)


async def _quota_refusal(repo, task_manager, user_id: int, size: int | None, *, check_active: bool = True) -> str | None:
    """额度不允许时返回给用户看的原因，允许返回 None。"""
    return await quota.admission_error(
        repo, user_id, size,
        pending_usage=task_manager.pending_usage(user_id) if task_manager else 0,
        check_active=check_active,
    )


def _is_admin(query: CallbackQuery) -> bool:
    return settings.is_admin(query.from_user.id if query.from_user else None)


def _home_keyboard(query: CallbackQuery, counts, node_label: str | None = None) -> InlineKeyboardMarkup:
    return main_inline_keyboard(counts, node_label=node_label, is_admin=_is_admin(query))


def _back_target(query: CallbackQuery) -> str | None:
    """当前消息上「⬅️ 返回列表」按钮指向哪里（"list:<tab>:<页>"）。任务卡片
    从列表打开后，在刷新/暂停/限速/取消确认……之间来回切换时都靠这个把返回
    按钮一路带着，最后还能回到原来的 tab 和页码，而不是每次都掉回「全部」第一页。"""
    markup = query.message.reply_markup if query.message else None
    if not markup:
        return None
    for row in markup.inline_keyboard:
        for button in row:
            if button.text == BACK_TO_LIST_TEXT and (button.callback_data or "").startswith("list:"):
                return button.callback_data
    return None


def _with_back(markup: InlineKeyboardMarkup | None, back: str | None) -> InlineKeyboardMarkup | None:
    """给任务卡片的子菜单（限速、选择文件、取消/删除确认）补上返回列表按钮。"""
    if not back or markup is None:
        return markup
    return InlineKeyboardMarkup(inline_keyboard=[
        *markup.inline_keyboard,
        [InlineKeyboardButton(text=BACK_TO_LIST_TEXT, callback_data=back)],
    ])


def _card_keyboard(row, gid: str, status: str, download, *, is_local: bool, back: str | None):
    return task_keyboard(
        gid, status, back=back, local=is_local,
        multi_file=multi_file(download), link=row["gofile_link"],
    )


def _purge_target(row, node) -> str | None:
    """「删除记录和文件」要删的路径；不满足安全条件时返回 None（按钮也就不出现）。

    只删本机节点上、确实落在下载目录之内的路径——save_path 来自数据库，
    这里不能盲信它，绝不允许删到下载目录本身或目录之外的任何东西。"""
    path = row["save_path"]
    if not path or node is None or not node.is_local:
        return None
    real = os.path.realpath(path)
    if not os.path.exists(real):
        return None
    roots = {os.path.realpath(d) for d in (node.download_dir, *settings.download_dir_options) if d}
    for root in roots:
        if real != root and real.startswith(root.rstrip(os.sep) + os.sep):
            return real
    return None


async def _leave_task_card(query: CallbackQuery, repo, nodes, back: str | None, toast: str):
    """任务记录被删掉之后：从列表打开的回到原来的列表位置，独立的任务消息直接删掉。"""
    if back and query.message:
        _, tab, page = (back.split(":") + ["0"])[:3]
        scope = settings.scope_for(query.from_user.id) if query.from_user else None
        try:
            page_n = int(page)
        except ValueError:
            page_n = 0
        text, markup = await render_task_list(repo, nodes, tab, page_n, user_id=scope)
        await edit_or_answer(query, text, answer_text=toast, reply_markup=markup, parse_mode="HTML")
        return
    await query.answer(toast)
    if query.message:
        try:
            await query.message.delete()
        except Exception:
            pass


@router.callback_query(F.data == "nav:start")
@router.callback_query(F.data == "sys:status")  # legacy alias: status page merged into home
async def nav_start(query: CallbackQuery, repo, nodes):
    scope = settings.scope_for(query.from_user.id) if query.from_user else None
    counts = await repo.count_by_status(user_id=scope)
    # 首页速度取 default 节点（本机）；多节点的分节点速度在节点选择器/统计里看，
    # 首页保持轻量不逐个节点拉 RPC
    try:
        stats = await nodes.get("default").global_stat()
    except Exception:
        stats = None
    await edit_or_answer(
        query, render_home(counts, stats),
        reply_markup=_home_keyboard(query, counts, await _current_node_label(query, repo, nodes)),
        parse_mode="HTML",
    )


@router.callback_query(F.data == "node:pick")
async def node_pick(query: CallbackQuery, repo, nodes):
    current = nodes.resolve(await repo.get_current_node(query.from_user.id)).name
    enabled = nodes.enabled_nodes()
    healthy = {n.name: nodes.is_healthy(n.name) for n in enabled}
    await edit_or_answer(
        query, render_node_chooser(current, enabled, healthy),
        reply_markup=node_chooser_keyboard(current, enabled, healthy), parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("node:use:"))
async def node_use(query: CallbackQuery, repo, nodes):
    name = query.data.split(":", 2)[2]
    node = nodes.get_node(name)
    if node is None or not node.enabled:
        await query.answer("该节点不存在或已停用", show_alert=True)
        return
    if not nodes.is_healthy(name):
        # 离线节点允许选中（可能马上就恢复了），但提示用户当前状态
        await query.answer(f"⚠️ {node.display_name} 当前离线，任务会在它恢复后才能添加", show_alert=True)
    await repo.set_current_node(query.from_user.id, name)
    scope = settings.scope_for(query.from_user.id) if query.from_user else None
    counts = await repo.count_by_status(user_id=scope)
    try:
        stats = await nodes.get("default").global_stat()
    except Exception:
        stats = None
    await edit_or_answer(
        query, render_home(counts, stats),
        answer_text=f"✅ 已切换到 {node.display_name}",
        reply_markup=_home_keyboard(query, counts, node.display_name),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("tasklimit:"))
async def apply_task_limit(query: CallbackQuery, repo, nodes, task_manager=None):
    _, gid, value = query.data.split(":", 2)
    if value not in {v for _, v in LIMIT_PRESETS}:
        await query.answer("无效的限速值", show_alert=True)
        return
    row = await repo.get_by_gid(gid)
    if row is None:
        await query.answer("任务不存在", show_alert=True)
        return
    if not _can_manage(query, row["user_id"]):
        await query.answer("⛔ 只能操作自己的任务。", show_alert=True)
        return
    try:
        aria2 = _client_for_row(nodes, row)
        await aria2.set_download_limit(gid, value)
    except Exception:
        log.exception("failed to set per-task limit for gid %s", gid)
        await query.answer("设置失败，请稍后再试", show_alert=True)
        return
    name = row["file_name"] or row["source_ref"] or gid
    _attend(task_manager, gid, submenu=True)
    await edit_or_answer(
        query, render_task_limit_chooser(name, value),
        answer_text="✅ 已生效" if value != "0" else "✅ 已取消限速",
        reply_markup=_with_back(task_limit_chooser_keyboard(gid, value), _back_target(query)), parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("list:"))
async def list_filter(query: CallbackQuery, repo, nodes):
    parts = query.data.split(":")
    if parts[1] == "overview":  # legacy alias from old messages
        parts = ["list", "ALL", "0"]
    if parts[1] == "cleanup":
        if not settings.is_admin(query.from_user.id if query.from_user else None):
            await query.answer("⛔ 清理记录仅限管理员。", show_alert=True)
            return
        n = await repo.count_tasks("COMPLETED")
        if not n:
            await query.answer("没有可清理的已完成记录")
            return
        await edit_or_answer(
            query,
            f"🧹 确认清理 <b>{n}</b> 条已完成任务记录？\n\n只删除机器人里的记录，不影响磁盘上的文件。",
            reply_markup=cleanup_confirm_keyboard(),
            parse_mode="HTML",
        )
        return
    if parts[1] == "cleanup_yes":
        if not settings.is_admin(query.from_user.id if query.from_user else None):
            await query.answer("⛔ 清理记录仅限管理员。", show_alert=True)
            return
        deleted = await repo.delete_by_status("COMPLETED")
        text, markup = await render_task_list(repo, nodes, "ALL", 0)
        await edit_or_answer(query, text, answer_text=f"已清理 {deleted} 条记录", reply_markup=markup, parse_mode="HTML")
        return
    if len(parts) < 3 or parts[1] == "noop":
        await query.answer()
        return
    status_key = parts[1]
    try:
        page = int(parts[2])
    except ValueError:
        page = 0
    scope = settings.scope_for(query.from_user.id) if query.from_user else None
    text, markup = await render_task_list(repo, nodes, status_key, page, user_id=scope)
    await edit_or_answer(query, text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data == "stats:daily")
async def show_daily_stats(query: CallbackQuery, repo):
    scope = settings.scope_for(query.from_user.id) if query.from_user else None
    text, markup = await render_daily_view(repo, user_id=scope)
    await edit_or_answer(query, text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("stats:"))
async def show_stats(query: CallbackQuery, repo):
    days = query.data.split(":", 1)[1]
    scope = settings.scope_for(query.from_user.id) if query.from_user else None
    text, markup = await render_stats_view(repo, days, user_id=scope)
    await edit_or_answer(query, text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("pending:"))
async def handle_pending(query: CallbackQuery, repo, nodes, task_manager=None):
    _, action, token = query.data.split(":", 2)

    # 批量确认（一条消息贴了多条链接）走独立分支 —— token 这里实际是 batch_id，
    # 跟下面单条确认的逻辑不共用，提前分流
    if action == "startall":
        await _handle_batch_start(query, repo, nodes, task_manager, batch_id=token)
        return
    if action == "cancelall":
        await _handle_batch_cancel(query, repo, batch_id=token)
        return

    pending = await repo.get_pending(token)
    if pending is None:
        await query.answer("这个待确认任务已过期，请重新发送。", show_alert=True)
        return
    if not _can_manage(query, pending.user_id):
        await query.answer("⛔ 只能操作自己添加的任务。", show_alert=True)
        return

    if action == "cancel":
        await repo.delete_pending(token)
        scope = settings.scope_for(query.from_user.id) if query.from_user else None
        await edit_or_answer(query, "已取消添加任务。", reply_markup=_home_keyboard(query, await repo.count_by_status(user_id=scope)))
        return
    if action == "nodes":
        # 确认卡片上的临时切换：只改这一条任务的目标节点
        if pending.kind == "tg_media":
            await query.answer("Telegram 文件转存只能在本机节点下载。", show_alert=True)
            return
        enabled = nodes.enabled_nodes()
        healthy = {n.name: nodes.is_healthy(n.name) for n in enabled}
        await edit_or_answer(
            query, "🖥 选择这个任务要下载到的节点：",
            reply_markup=pending_node_chooser_keyboard(token, pending.node, enabled, healthy),
        )
        return
    if action in {"dir", "files", "settings"}:
        node = nodes.resolve(pending.node)
        await query.answer(f"当前使用目录：{node.download_dir}", show_alert=True)
        return
    if action != "start":
        await query.answer("未知操作", show_alert=True)
        return

    target_node = nodes.get_node(pending.node)
    if target_node is None or not target_node.enabled:
        await query.answer("⛔ 目标节点已被删除或停用，请重新发送任务。", show_alert=True)
        return
    # 磁盘预检只对本机节点有意义（aria2 RPC 拿不到远端磁盘信息）；
    # 远程节点靠 aria2 自己下载失败兜底
    space_error = (
        storage.download_space_error(target_node.download_dir, pending.file_size or 0)
        if target_node.is_local else None
    )
    if space_error:
        await query.answer("⛔ " + space_error, show_alert=True)
        return

    # 额度检查放在认领之前：被拒时确认卡片原样保留，用户可以先去处理别的任务再回来点
    refusal = await _quota_refusal(repo, task_manager, pending.user_id, pending.file_size)
    if refusal:
        await query.answer(f"⛔ {refusal}"[:200], show_alert=True)
        return

    # atomically claim the confirmation BEFORE adding to aria2 — a rapid double
    # tap would otherwise pass the checks twice and add the download twice
    pending = await repo.pop_pending(token)
    if pending is None:
        await query.answer("任务正在处理中。")
        return

    try:
        gid = await add_source(nodes, pending.node, pending.kind, pending.payload, pending.file_name)
    except ValueError:
        await query.answer("未知任务类型", show_alert=True)
        return
    except Exception:
        # put the claim back so the button still works on the next tap
        await repo.restore_pending(pending)
        log.exception("failed to start pending task")
        await query.answer("添加任务失败，请稍后重试。", show_alert=True)
        return

    task_id = await repo.create_task(
        gid=gid,
        user_id=pending.user_id,
        chat_id=pending.chat_id,
        reply_message_id=query.message.message_id if query.message else None,
        source_type=pending.kind,
        source_ref=pending.source_ref,
        file_name=pending.file_name,
        file_size=pending.file_size,
        payload=pending.payload,
        node=pending.node,
    )
    row = await repo.get_by_id(task_id)
    _attend(task_manager, gid)  # 刚添加的任务：用户多半在盯着看，先快速刷新一阵
    await edit_or_answer(
        query,
        render_task_card(row, status="PENDING", node_label=nodes.label(pending.node)),
        reply_markup=task_keyboard(gid, "PENDING", local=target_node.is_local),
        parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("pnode:"))
async def apply_pending_node(query: CallbackQuery, repo, nodes):
    _, token, name = query.data.split(":", 2)
    pending = await repo.get_pending(token)
    if pending is None:
        await query.answer("这个待确认任务已过期，请重新发送。", show_alert=True)
        return
    if not _can_manage(query, pending.user_id):
        await query.answer("⛔ 只能操作自己添加的任务。", show_alert=True)
        return
    node = nodes.get_node(name)
    if node is None or not node.enabled:
        await query.answer("该节点不存在或已停用", show_alert=True)
        return

    await repo.update_pending_node(token, name)
    await query.answer(f"✅ 目标节点：{node.display_name}")
    await edit_or_answer(
        query,
        render_pending_card(
            pending.kind, pending.file_name or "任务",
            size=pending.file_size,
            node_label=nodes.label(name), download_dir=node.download_dir,
        ),
        reply_markup=pending_task_keyboard(token, show_node_switch=nodes.is_multi()),
        parse_mode="HTML",
    )


async def _handle_batch_start(query: CallbackQuery, repo, nodes, task_manager, *, batch_id: str):
    pendings = await repo.get_pending_batch(batch_id)
    if not pendings:
        await query.answer("批量任务已过期或已处理。", show_alert=True)
        return
    if not _can_manage(query, pendings[0].user_id):
        await query.answer("⛔ 只能操作自己创建的批量任务。", show_alert=True)
        return

    # 批量任务不挂 reply_message_id：逐个任务都去编辑同一条汇总消息会互相打架，
    # 干脆不接，进度只能在任务列表里看；完成/失败时仍然会按"完成通知"设置补发新消息
    started, skipped = 0, 0
    quota_reason = None
    for claimed in pendings:
        claimed = await repo.pop_pending(claimed.token)
        if claimed is None:
            continue
        node = nodes.get_node(claimed.node)
        if node is None or not node.enabled:
            skipped += 1
            continue
        # 磁盘预检只对本机节点做（远端磁盘摸不到），与单条确认的逻辑一致
        space_error = (
            storage.download_space_error(node.download_dir, claimed.file_size or 0)
            if node.is_local else None
        )
        if space_error:
            await repo.restore_pending(claimed)
            quota_reason = quota_reason or space_error
            skipped += 1
            continue
        # 额度：前面刚加的任务已经写进库了，所以"同时进行数"会随着循环实时累计
        refusal = await _quota_refusal(repo, task_manager, claimed.user_id, claimed.file_size)
        if refusal:
            quota_reason = quota_reason or refusal
            skipped += 1
            continue
        try:
            gid = await add_source(nodes, claimed.node, claimed.kind, claimed.payload, claimed.file_name)
        except Exception:
            log.exception("batch start failed for token %s", claimed.token)
            skipped += 1
            continue
        await repo.create_task(
            gid=gid, user_id=claimed.user_id, chat_id=claimed.chat_id, reply_message_id=None,
            source_type=claimed.kind, source_ref=claimed.source_ref,
            file_name=claimed.file_name, file_size=claimed.file_size, payload=claimed.payload,
            node=claimed.node,
        )
        started += 1

    text = f"✅ 批量任务已处理：成功启动 {started} 个"
    if skipped:
        text += f"，跳过 {skipped} 个（磁盘空间不足、额度限制或添加失败）"
        if quota_reason:
            text += f"\n⛔ {quota_reason}"
    text += "\n\n可在 📋 任务列表 里查看进度。"
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📋 任务列表", callback_data="list:ALL:0"),
        InlineKeyboardButton(text="⬅️ 主菜单", callback_data="nav:start"),
    ]])
    if await repo.get_pending_batch(batch_id):
        markup.inline_keyboard.insert(0, [
            InlineKeyboardButton(text="🔄 重试未启动任务", callback_data=f"pending:startall:{batch_id}"),
        ])
    await edit_or_answer(query, text, reply_markup=markup)


async def _handle_batch_cancel(query: CallbackQuery, repo, *, batch_id: str):
    pendings = await repo.get_pending_batch(batch_id)
    if pendings and not _can_manage(query, pendings[0].user_id):
        await query.answer("⛔ 只能操作自己创建的批量任务。", show_alert=True)
        return
    deleted = await repo.delete_pending_batch(batch_id)
    scope = settings.scope_for(query.from_user.id) if query.from_user else None
    await edit_or_answer(
        query, f"已取消批量任务（{deleted} 个）。",
        reply_markup=_home_keyboard(query, await repo.count_by_status(user_id=scope)),
    )


@router.callback_query(F.data.startswith("topen:"))
async def open_from_list(query: CallbackQuery, repo, nodes, task_manager):
    """列表里点开一个任务：topen:<tab>:<页>:<gid>。跟 task:open 一样渲染任务
    卡片，只是返回按钮回到点进来的那个 tab/页，而不是固定回「全部」第一页。"""
    try:
        _, tab, page, gid = query.data.split(":", 3)
        page_n = int(page)
    except ValueError:
        await query.answer("无效的操作", show_alert=True)
        return
    if tab not in LIST_STATUS_MAP:
        tab = "ALL"
    await _task_action(query, repo, nodes, task_manager, "detail", gid, back=f"list:{tab}:{max(0, page_n)}")


@router.callback_query(F.data.startswith("task:"))
async def handle_task_action(query: CallbackQuery, repo, nodes, task_manager):
    _, action, gid = query.data.split(":", 2)
    # task:open 是旧版列表按钮/搜索结果的入口，返回固定到「全部」；其它操作
    # 沿用当前消息上已有的返回按钮
    back = "list:ALL:0" if action == "open" else _back_target(query)
    await _task_action(query, repo, nodes, task_manager, "detail" if action == "open" else action, gid, back=back)


class _TaskActionContext:
    """_task_action 分发给各子处理函数时共用的上下文：回调、任务行与归属
    节点信息在主函数里统一备齐，子函数只管自己那个 action 的逻辑。"""

    def __init__(self, query, repo, nodes, task_manager, action, gid, *, back, row, aria2, node):
        self.query = query
        self.repo = repo
        self.nodes = nodes
        self.task_manager = task_manager
        self.action = action
        self.gid = gid
        self.back = back
        self.row = row
        self.aria2 = aria2
        self.node = node
        self.is_local = node.is_local if node else True
        self.node_label = nodes.label(row["node"])
        self.name = row["file_name"] or row["source_ref"] or gid


async def _action_detail(ctx: _TaskActionContext):
    download = await _download_or_none(ctx.aria2, ctx.gid)
    status = _mapped_status(download, ctx.row["status"])
    await edit_or_answer(
        ctx.query,
        render_task_card(ctx.row, download, status=status, node_label=ctx.node_label),
        reply_markup=_card_keyboard(ctx.row, ctx.gid, status, download, is_local=ctx.is_local, back=ctx.back),
        parse_mode="HTML",
    )


async def _action_retry(ctx: _TaskActionContext):
    row = ctx.row
    payload = row["payload"]
    if not payload or (row["source_type"] in ("torrent", "metalink") and not os.path.exists(payload)):
        await ctx.query.answer("缺少原始下载来源，无法重试。请重新发送链接或文件。", show_alert=True)
        return
    refusal = await _quota_refusal(ctx.repo, ctx.task_manager, row["user_id"], row["file_size"])
    if refusal:
        await ctx.query.answer(f"⛔ {refusal}"[:200], show_alert=True)
        return
    try:
        new_gid = await add_source(ctx.nodes, row["node"], row["source_type"], payload, row["file_name"])
    except Exception:
        log.exception("retry failed for task %s", row["id"])
        await ctx.query.answer("重试失败，请稍后再试。", show_alert=True)
        return
    await ctx.repo.retry_task(
        row["id"], new_gid,
        reply_message_id=ctx.query.message.message_id if ctx.query.message else None,
    )
    row = await ctx.repo.get_by_id(row["id"])
    _attend(ctx.task_manager, new_gid)
    await edit_or_answer(
        ctx.query, render_task_card(row, status="PENDING", node_label=ctx.node_label),
        answer_text="🔄 已重新加入下载",
        reply_markup=task_keyboard(new_gid, "PENDING", local=ctx.is_local, back=ctx.back), parse_mode="HTML",
    )


async def _action_cancel(ctx: _TaskActionContext):
    text = (
        "⚠️ 确认取消任务？\n\n"
        f"任务：{ctx.name}\n"
        f"已下载：{_completed_text(await _download_or_none(ctx.aria2, ctx.gid))}\n\n"
        "请选择是否同时删除已经下载的数据。"
    )
    await edit_or_answer(ctx.query, text, reply_markup=_with_back(task_cancel_confirm_keyboard(ctx.gid), ctx.back))


async def _action_confirm_delete_files(ctx: _TaskActionContext):
    await edit_or_answer(
        ctx.query, "⚠️ 该操作会永久删除已下载文件。",
        reply_markup=_with_back(task_cancel_confirm_keyboard(ctx.gid, destructive=True), ctx.back),
    )


async def _action_delete(ctx: _TaskActionContext):
    target = _purge_target(ctx.row, ctx.node)
    text = f"🗑 删除任务记录？\n\n任务：{ctx.name}"
    if target:
        text += f"\n文件：{target}\n\n可以只删记录（文件保留在磁盘上），也可以连文件一起删除。"
    else:
        text += "\n\n只删除机器人里的记录，不影响磁盘上的文件。"
    await edit_or_answer(
        ctx.query, text,
        reply_markup=_with_back(task_delete_confirm_keyboard(ctx.gid, can_delete_files=bool(target)), ctx.back),
    )


async def _action_confirm_purge(ctx: _TaskActionContext):
    target = _purge_target(ctx.row, ctx.node)
    if not target:
        await ctx.query.answer("文件已不存在或不在下载目录内，只能删除记录。", show_alert=True)
        return
    await edit_or_answer(
        ctx.query, f"⚠️ 将永久删除：\n{target}\n\n此操作不可恢复。",
        reply_markup=_with_back(task_delete_confirm_keyboard(ctx.gid, destructive=True), ctx.back),
    )


async def _action_delete_record(ctx: _TaskActionContext):
    """delete_record / purge：删 DB 记录；purge 先删文件，删不掉就保留记录。"""
    toast = "已删除记录"
    if ctx.action == "purge":
        target = _purge_target(ctx.row, ctx.node)
        if target:
            try:
                await asyncio.to_thread(remove_path, target)
                toast = "已删除记录和文件"
            except OSError:
                log.exception("failed to delete files for gid %s", ctx.gid)
                await ctx.query.answer("删除文件失败，记录已保留。", show_alert=True)
                return
    if ctx.aria2 is not None and ctx.row["status"] not in ("COMPLETED", "FAILED", "CANCELLED"):
        # 还在 aria2 里跑的任务（旧消息上的删除按钮）先停掉，不然记录没了下载还在继续
        try:
            await ctx.aria2.remove(ctx.gid, files=False, is_local=ctx.is_local)
        except Exception:
            pass
    await ctx.repo.delete_task(ctx.gid)
    await _leave_task_card(ctx.query, ctx.repo, ctx.nodes, ctx.back, toast)


async def _action_files(ctx: _TaskActionContext):
    row = ctx.row
    if row["status"] == "COMPLETED":
        path = row["save_path"] or settings.download_dir
        link = row["gofile_link"] or "暂无下载链接"
        await ctx.query.answer(f"保存位置：{path}\n链接：{link}", show_alert=True)
        return

    download = await _download_or_none(ctx.aria2, ctx.gid)
    real_files = [f for f in download.files if not f.is_metadata] if download else []
    if not download or len(real_files) < 2:
        await ctx.query.answer(
            "单文件任务或元数据尚未就绪，无法选择文件。" if download else "任务信息暂不可用。",
            show_alert=True,
        )
        return
    await edit_or_answer(
        ctx.query, render_file_selection(download),
        reply_markup=_with_back(file_selection_keyboard(ctx.gid, download), ctx.back), parse_mode="HTML",
    )


async def _action_limit(ctx: _TaskActionContext):
    try:
        limit_raw = await ctx.aria2.get_download_limit(ctx.gid)
    except Exception:
        limit_raw = None
    await edit_or_answer(
        ctx.query, render_task_limit_chooser(ctx.name, limit_raw),
        reply_markup=_with_back(task_limit_chooser_keyboard(ctx.gid, limit_raw), ctx.back), parse_mode="HTML",
    )


async def _action_sendtg(ctx: _TaskActionContext):
    if ctx.row["status"] != "COMPLETED":
        await ctx.query.answer("任务未完成，无法发送。", show_alert=True)
        return
    if not ctx.is_local:
        # 远程任务不渲染这个按钮，走到这说明是旧消息/伪造数据 —— 兜底拦截
        await ctx.query.answer("文件在远程节点上，无法从这里发送。", show_alert=True)
        return
    await ctx.query.answer("正在发送…")
    ok, msg = await ctx.task_manager.send_file_to_tg(ctx.row, ctx.gid)
    if not ok:
        await ctx.query.answer(msg, show_alert=True)


# 自成一体的 action 分发到子函数；detail 在节点不可用时也要能展示、settings /
# link 只是一行 toast、pause/resume/cancel_only/delete_files 走后面的通用
# _apply_action 尾巴，都留在 _task_action 主流程里不进这张表。
_TASK_ACTION_HANDLERS = {
    "retry": _action_retry,
    "cancel": _action_cancel,
    "confirm_delete_files": _action_confirm_delete_files,
    "delete": _action_delete,
    "confirm_purge": _action_confirm_purge,
    "delete_record": _action_delete_record,
    "purge": _action_delete_record,
    "files": _action_files,
    "limit": _action_limit,
    "sendtg": _action_sendtg,
}


async def _task_action(query: CallbackQuery, repo, nodes, task_manager, action: str, gid: str, *, back: str | None):
    row = await repo.get_by_gid(gid)
    if row is None:
        await query.answer("任务不存在（可能已被删除）", show_alert=True)
        return
    # viewing (detail/link/files-info) is fine for any whitelisted user;
    # anything that mutates the task requires owner-or-admin
    if action not in {"detail", "link"} and not _can_manage(query, row["user_id"]):
        await query.answer("⛔ 只能操作自己的任务。", show_alert=True)
        return

    _attend(task_manager, gid, submenu=action in _SUBMENU_ACTIONS)

    if dict(row).get("backend") == "video":
        await handle_video_task(query, repo, task_manager, row, action, back)
        return

    # 所有 RPC 操作走任务自己的归属节点；节点被删/停用时 aria2 为 None，
    # 查看类操作降级为纯 DB 展示，操作类直接提示
    try:
        aria2 = _client_for_row(nodes, row)
    except NodeUnavailable:
        aria2 = None
    node = nodes.get_node(row["node"])
    ctx = _TaskActionContext(
        query, repo, nodes, task_manager, action, gid, back=back, row=row, aria2=aria2, node=node,
    )

    if action == "detail":
        await _action_detail(ctx)
        return

    if aria2 is None and action in {"retry", "pause", "resume", "cancel_only", "delete_files", "limit"}:
        await query.answer("⛔ 该任务所在节点已被删除或停用，无法操作。", show_alert=True)
        return

    handler = _TASK_ACTION_HANDLERS.get(action)
    if handler is not None:
        await handler(ctx)
        return

    if action == "settings":  # 旧消息上的按钮
        await query.answer("当前任务可直接暂停、继续或取消；限速见「🚀 限速」按钮。", show_alert=True)
        return

    if action == "link":  # 旧消息上的按钮；新卡片直接给 GoFile 的 URL 按钮
        link = row["gofile_link"] or row["save_path"] or "当前没有可用链接。"
        await query.answer(link, show_alert=True)
        return

    if action == "resume":
        # 恢复暂停的任务：它本来就占着名额，只需要看今天的流量是否还有余量，
        # 否则刚恢复就会被下一轮轮询重新暂停
        refusal = await _quota_refusal(repo, task_manager, row["user_id"], None, check_active=False)
        if refusal:
            await query.answer(f"⛔ {refusal}"[:200], show_alert=True)
            return

    new_status = await _apply_action(query, aria2, repo, action, gid, is_local=ctx.is_local)
    if new_status is None or not query.message:
        return
    try:
        row = await repo.get_by_gid(gid)
        download = await _download_or_none(aria2, gid)
        await query.message.edit_text(
            render_task_card(row, download, status=new_status, node_label=ctx.node_label),
            reply_markup=_card_keyboard(row, gid, new_status, download, is_local=ctx.is_local, back=back),
            parse_mode="HTML",
        )
    except Exception:
        pass


@router.callback_query(F.data.startswith("filesel:"))
async def toggle_file_selection(query: CallbackQuery, repo, nodes, task_manager=None):
    _, gid, index_raw = query.data.split(":", 2)
    row = await repo.get_by_gid(gid)
    if row is None:
        await query.answer("任务不存在", show_alert=True)
        return
    if not _can_manage(query, row["user_id"]):
        await query.answer("⛔ 只能操作自己的任务。", show_alert=True)
        return

    try:
        aria2 = _client_for_row(nodes, row)
    except NodeUnavailable:
        await query.answer("⛔ 该任务所在节点已被删除或停用。", show_alert=True)
        return

    download = await _download_or_none(aria2, gid)
    real_files = [f for f in download.files if not f.is_metadata] if download else []
    try:
        index = int(index_raw)
        target = next(f for f in real_files if f.index == index)
    except (ValueError, StopIteration):
        await query.answer("文件不存在", show_alert=True)
        return

    currently_selected = [f.index for f in real_files if f.selected]
    if target.selected and len(currently_selected) <= 1:
        await query.answer("至少要保留一个文件被选中", show_alert=True)
        return

    new_selection = (
        [i for i in currently_selected if i != index]
        if target.selected
        else currently_selected + [index]
    )
    try:
        await aria2.set_selected_files(gid, new_selection)
    except Exception:
        log.exception("failed to change file selection for gid %s", gid)
        await query.answer("切换失败，请稍后再试", show_alert=True)
        return

    download = await _download_or_none(aria2, gid)
    _attend(task_manager, gid, submenu=True)
    await edit_or_answer(
        query, render_file_selection(download),
        reply_markup=_with_back(file_selection_keyboard(gid, download), _back_target(query)), parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("bulk:"))
async def bulk_action(query: CallbackQuery, repo, nodes, task_manager=None):
    _, action, status = query.data.split(":", 2)
    rows = await repo.list_recent(1000, status=status)
    changed = 0
    for row in rows:
        gid = row["gid"]
        if not gid:
            continue
        if not _can_manage(query, row["user_id"]):
            continue  # bulk ops only touch your own tasks (admins touch all)
        try:
            if dict(row).get("backend") == "video" and action in ("pause", "resume"):
                await repo.video_command(gid, action)
                changed += 1
                continue
            aria2 = _client_for_row(nodes, row)  # 每行按自己的归属节点路由
            if action == "pause":
                await aria2.pause(gid)
                await repo.update_status(gid, "PAUSED")
                changed += 1
            elif action == "resume":
                if await _quota_refusal(repo, task_manager, row["user_id"], None, check_active=False):
                    continue  # 今日额度用完的用户，批量恢复时跳过他的任务
                await aria2.resume(gid)
                await repo.update_status(gid, "ACTIVE")
                changed += 1
        except Exception:
            log.exception("bulk task action failed: %s %s", action, gid)
    scope = settings.scope_for(query.from_user.id) if query.from_user else None
    text, markup = await render_task_list(
        repo, nodes, "ACTIVE" if action == "pause" else "PAUSED", 0, user_id=scope
    )
    await edit_or_answer(query, text, answer_text=f"已处理 {changed} 个任务", reply_markup=markup, parse_mode="HTML")


async def _apply_action(query: CallbackQuery, aria2, repo, action: str, gid: str, *, is_local: bool = True) -> str | None:
    try:
        if action == "pause":
            await aria2.pause(gid)
            await repo.update_status(gid, "PAUSED")
            await query.answer(_TOAST["pause"])
            return "PAUSED"
        if action == "resume":
            await aria2.resume(gid)
            await repo.update_status(gid, "ACTIVE")
            await query.answer(_TOAST["resume"])
            return "ACTIVE"
        if action in {"cancel_only", "delete_files"}:
            want_files = action == "delete_files"
            await aria2.remove(gid, files=want_files, is_local=is_local)
            await repo.update_status(gid, "CANCELLED")
            # 远程节点没法从这里删文件（aria2 RPC 本身没有这个能力，删本地
            # 磁盘只对本机节点有意义），toast 如实反映有没有真的删成
            toast = _TOAST[action] if not want_files or is_local else "已取消任务（远程节点，文件未删除）"
            await query.answer(toast)
            return "CANCELLED"
    except Exception:
        log.exception("task action failed: %s %s", action, gid)
        await query.answer("操作失败，请稍后重试", show_alert=True)
        return None

    await query.answer("未知操作", show_alert=True)
    return None


async def _download_or_none(aria2, gid: str):
    try:
        return await aria2.get_status(gid)
    except Exception:
        return None


def _mapped_status(download, fallback: str) -> str:
    """展示用状态：aria2 状态经 models.map_aria2_status 统一映射，
    映射不到（如 removed）或查不到任务时回退到 DB 里的状态。"""
    if not download:
        return fallback
    return map_aria2_status(download.status) or fallback


def _completed_text(download) -> str:
    if not download:
        return "未知"
    try:
        return download.completed_length_string()
    except Exception:
        return "未知"
