import logging
import os

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from bot.config import settings
from bot.core import storage
from bot.core.cards import (
    render_home,
    render_settings,
    render_task_card,
)
from bot.core.keyboards import (
    main_inline_keyboard,
    settings_keyboard,
    task_cancel_confirm_keyboard,
    task_keyboard,
)
from bot.core.list_view import render_task_list, render_task_overview
from bot.core.pending_tasks import delete_pending, get_pending

log = logging.getLogger(__name__)
router = Router(name="callbacks")

_TOAST = {
    "pause": "已暂停",
    "resume": "已继续",
    "cancel_only": "已取消任务",
    "delete_files": "已取消任务并删除文件",
    "delete": "已删除记录",
}


@router.callback_query(F.data == "nav:start")
@router.callback_query(F.data == "sys:status")  # legacy alias: status page merged into home
async def nav_start(query: CallbackQuery, repo, aria2):
    counts = await repo.count_by_status()
    try:
        stats = await aria2.global_stat()
    except Exception:
        stats = None
    await _edit(query, render_home(counts, stats), reply_markup=main_inline_keyboard(counts), parse_mode="HTML")


@router.callback_query(F.data == "nav:settings")
async def nav_settings(query: CallbackQuery):
    await _edit(query, render_settings(), reply_markup=settings_keyboard(), parse_mode="HTML")


@router.callback_query(F.data.startswith("settings:"))
async def settings_placeholder(query: CallbackQuery):
    action = query.data.split(":", 1)[1]
    if action == "dir":
        await query.answer(f"默认目录：{settings.download_dir}", show_alert=True)
    elif action == "download_limit":
        await query.answer("发送 /limit 2M 设置全局下载限速；发送 /limit 0 取消限速。", show_alert=True)
    elif action == "upload_limit":
        await query.answer("上传限速沿用 aria2 配置文件。", show_alert=True)
    elif action == "concurrent":
        await query.answer(f"当前最大同时下载：{settings.max_concurrent}", show_alert=True)
    else:
        await query.answer("该设置暂未接入。", show_alert=True)


@router.callback_query(F.data == "list:overview")
async def list_overview(query: CallbackQuery, repo):
    text, markup = await render_task_overview(repo)
    await _edit(query, text, reply_markup=markup, parse_mode="HTML")


@router.callback_query(F.data.startswith("list:"))
async def list_filter(query: CallbackQuery, repo, aria2):
    parts = query.data.split(":")
    if len(parts) >= 2 and parts[1] == "cleanup":
        deleted = await repo.delete_by_status("COMPLETED")
        text, markup = await render_task_overview(repo)
        await _edit(query, text, answer_text=f"已清理 {deleted} 条完成记录", reply_markup=markup, parse_mode="HTML")
        return
    if len(parts) < 3 or parts[1] == "noop":
        await query.answer()
        return
    status_key = parts[1]
    try:
        page = int(parts[2])
    except ValueError:
        page = 0
    text, markup = await render_task_list(repo, aria2, status_key, page)
    await _edit(query, text, reply_markup=markup, parse_mode="HTML")


async def _add_source(aria2, kind: str, payload: str, file_name: str | None) -> str:
    """Add a download to aria2 from its original source; returns the new gid.
    Shared by pending:start and task:retry so both stay in sync."""
    if kind in {"url", "tg_media"}:
        subdir = storage.build_subdir(settings.download_dir, file_name or payload)
        return await aria2.add_uri(
            payload,
            out=file_name if kind == "tg_media" else None,
            download_dir=subdir,
        )
    if kind == "magnet":
        return await aria2.add_magnet(payload, download_dir=settings.download_dir)
    if kind == "torrent":
        return await aria2.add_torrent(payload, download_dir=settings.download_dir)
    raise ValueError(f"unknown source kind: {kind}")


@router.callback_query(F.data.startswith("pending:"))
async def handle_pending(query: CallbackQuery, aria2, repo):
    _, action, token = query.data.split(":", 2)
    pending = get_pending(token)
    if pending is None:
        await query.answer("这个待确认任务已过期，请重新发送。", show_alert=True)
        return

    if action == "cancel":
        delete_pending(token)
        await _edit(query, "已取消添加任务。", reply_markup=main_inline_keyboard(await repo.count_by_status()))
        return
    if action in {"dir", "files", "settings"}:
        await query.answer(f"当前使用默认目录：{settings.download_dir}", show_alert=True)
        return
    if action != "start":
        await query.answer("未知操作", show_alert=True)
        return

    if not storage.has_enough_space(settings.download_dir, pending.file_size or 0):
        await query.answer("⛔ 服务器磁盘空间不足，已拒绝该任务。", show_alert=True)
        return

    try:
        gid = await _add_source(aria2, pending.kind, pending.payload, pending.file_name)
    except ValueError:
        await query.answer("未知任务类型", show_alert=True)
        return
    except Exception:
        # keep the pending entry so the button still works on the next tap
        log.exception("failed to start pending task")
        await query.answer("添加任务失败，请稍后重试。", show_alert=True)
        return
    delete_pending(token)

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
    )
    row = await repo.get_by_id(task_id)
    await _edit(query, render_task_card(row, status="PENDING"), reply_markup=task_keyboard(gid, "PENDING"), parse_mode="HTML")


@router.callback_query(F.data.startswith("task:"))
async def handle_task_action(query: CallbackQuery, aria2, repo):
    _, action, gid = query.data.split(":", 2)
    row = await repo.get_by_gid(gid)
    if row is None:
        await query.answer("任务不存在", show_alert=True)
        return

    if action == "detail":
        download = await _download_or_none(aria2, gid)
        status = _mapped_status(download, row["status"])
        await _edit(query, render_task_card(row, download, status=status), reply_markup=task_keyboard(gid, status), parse_mode="HTML")
        return

    if action == "retry":
        payload = row["payload"]
        if not payload or (row["source_type"] == "torrent" and not os.path.exists(payload)):
            await query.answer("缺少原始下载来源，无法重试。请重新发送链接或文件。", show_alert=True)
            return
        try:
            new_gid = await _add_source(aria2, row["source_type"], payload, row["file_name"])
        except Exception:
            log.exception("retry failed for task %s", row["id"])
            await query.answer("重试失败，请稍后再试。", show_alert=True)
            return
        await repo.retry_task(
            row["id"], new_gid,
            reply_message_id=query.message.message_id if query.message else None,
        )
        row = await repo.get_by_id(row["id"])
        await _edit(query, render_task_card(row, status="PENDING"), reply_markup=task_keyboard(new_gid, "PENDING"), parse_mode="HTML")
        return

    if action == "cancel":
        text = (
            "⚠️ 确认取消任务？\n\n"
            f"任务：{row['file_name'] or row['source_ref'] or gid}\n"
            f"已下载：{_completed_text(await _download_or_none(aria2, gid))}\n\n"
            "请选择是否同时删除已经下载的数据。"
        )
        await _edit(query, text, reply_markup=task_cancel_confirm_keyboard(gid))
        return

    if action == "confirm_delete_files":
        await _edit(query, "⚠️ 该操作会永久删除已下载文件。", reply_markup=task_cancel_confirm_keyboard(gid, destructive=True))
        return

    if action == "files":
        path = row["save_path"] or settings.download_dir
        link = row["gofile_link"] or "暂无下载链接"
        await query.answer(f"保存位置：{path}\n链接：{link}", show_alert=True)
        return

    if action == "settings":
        await query.answer("当前任务可直接暂停、继续或取消；限速使用 /limit 2M。", show_alert=True)
        return

    if action == "limit":
        await query.answer("全局限速仍使用原命令：/limit 2M；/limit 0 表示不限速。", show_alert=True)
        return

    if action == "link":
        link = row["gofile_link"] or row["save_path"] or "当前没有可用链接。"
        await query.answer(link, show_alert=True)
        return

    new_status = await _apply_action(query, aria2, repo, action, gid)
    if new_status is None or not query.message:
        return
    try:
        if new_status == "DELETED":
            await query.message.delete()
        else:
            row = await repo.get_by_gid(gid)
            download = await _download_or_none(aria2, gid)
            await query.message.edit_text(
                render_task_card(row, download, status=new_status),
                reply_markup=task_keyboard(gid, new_status),
                parse_mode="HTML",
            )
    except Exception:
        pass


@router.callback_query(F.data.startswith("bulk:"))
async def bulk_action(query: CallbackQuery, aria2, repo):
    _, action, status = query.data.split(":", 2)
    rows = await repo.list_recent(1000, status=status)
    changed = 0
    for row in rows:
        gid = row["gid"]
        if not gid:
            continue
        try:
            if action == "pause":
                await aria2.pause(gid)
                await repo.update_status(gid, "PAUSED")
                changed += 1
            elif action == "resume":
                await aria2.resume(gid)
                await repo.update_status(gid, "ACTIVE")
                changed += 1
        except Exception:
            log.exception("bulk task action failed: %s %s", action, gid)
    text, markup = await render_task_list(repo, aria2, "ACTIVE" if action == "pause" else "PAUSED", 0)
    await _edit(query, text, answer_text=f"已处理 {changed} 个任务", reply_markup=markup, parse_mode="HTML")


async def _apply_action(query: CallbackQuery, aria2, repo, action: str, gid: str) -> str | None:
    try:
        if action == "delete":
            await repo.delete_task(gid)
            await query.answer(_TOAST["delete"])
            return "DELETED"
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
            await aria2.remove(gid, files=action == "delete_files")
            await repo.update_status(gid, "CANCELLED")
            await query.answer(_TOAST[action])
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
    if not download:
        return fallback
    status = download.status.upper()
    if status == "COMPLETE":
        return "COMPLETED"
    if status == "ERROR":
        return "FAILED"
    if status == "PAUSED":
        return "PAUSED"
    if status == "WAITING":
        return "PENDING"
    if status == "ACTIVE":
        return "ACTIVE"
    return fallback


def _completed_text(download) -> str:
    if not download:
        return "未知"
    try:
        return download.completed_length_string()
    except Exception:
        return "未知"


async def _edit(query: CallbackQuery, text: str, answer_text: str | None = None, **kwargs):
    if not query.message:
        await query.answer(answer_text)
        return
    try:
        await query.message.edit_text(text, **kwargs)
    except TelegramBadRequest as e:
        # "message is not modified" = user tapped the same button twice;
        # a silent toast is correct there, a duplicate message is not
        if "message is not modified" not in str(e):
            await query.message.answer(text, **kwargs)
    except Exception:
        await query.message.answer(text, **kwargs)
    await query.answer(answer_text)
