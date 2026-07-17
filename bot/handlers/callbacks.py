import logging
import os

from aiogram import Router, F
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import CallbackQuery

from bot.config import settings
from bot.core import storage
from bot.core.cards import (
    render_cleanup_chooser,
    render_concurrent_chooser,
    render_dir_chooser,
    render_file_selection,
    render_home,
    render_limit_chooser,
    render_maxsize_chooser,
    render_settings,
    render_task_card,
)
from bot.core.conf_editor import write_kv
from bot.core.keyboards import (
    CLEANUP_PRESETS,
    CONCURRENT_PRESETS,
    MAXSIZE_PRESETS,
    cleanup_chooser_keyboard,
    cleanup_confirm_keyboard,
    concurrent_chooser_keyboard,
    dir_chooser_keyboard,
    file_selection_keyboard,
    limit_chooser_keyboard,
    main_inline_keyboard,
    maxsize_chooser_keyboard,
    settings_keyboard,
    task_cancel_confirm_keyboard,
    task_keyboard,
)
from bot.core.list_view import render_task_list

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


async def _settings_data(aria2) -> tuple[str | None, str | None]:
    """(max-overall-download-limit, max-concurrent-downloads) straight from
    aria2 — the live values, not whatever .env said at boot."""
    try:
        opts = await aria2.get_global_options()
    except Exception:
        opts = {}
    return opts.get("max-overall-download-limit"), opts.get("max-concurrent-downloads")


async def _show_settings(query: CallbackQuery, aria2):
    limit_raw, concurrent_raw = await _settings_data(aria2)
    await _edit(query, render_settings(limit_raw, concurrent_raw), reply_markup=settings_keyboard(), parse_mode="HTML")


def _persist_env(key: str, value: str):
    """Best-effort .env write-back so the choice survives a bot restart; a
    missing .env (e.g. env vars injected some other way) is not an error."""
    try:
        write_kv(".env", key, value)
    except OSError:
        log.warning("could not persist %s to .env", key)


@router.callback_query(F.data == "nav:settings")
async def nav_settings(query: CallbackQuery, aria2):
    await _show_settings(query, aria2)


@router.callback_query(F.data == "settings:limit")
async def settings_limit(query: CallbackQuery, aria2):
    try:
        limit_raw = await aria2.get_global_limit()
    except Exception:
        limit_raw = None
    await _edit(query, render_limit_chooser(limit_raw), reply_markup=limit_chooser_keyboard(), parse_mode="HTML")


@router.callback_query(F.data.startswith("setlimit:"))
async def apply_limit(query: CallbackQuery, aria2):
    value = query.data.split(":", 1)[1]
    if value not in {"0", "1M", "2M", "5M", "10M"}:
        await query.answer("无效的限速值", show_alert=True)
        return
    try:
        await aria2.set_global_limit(value)
    except Exception:
        log.exception("failed to set global limit")
        await query.answer("设置失败，请稍后再试", show_alert=True)
        return
    await query.answer("✅ 已生效" if value != "0" else "✅ 已取消限速")
    await _show_settings(query, aria2)


@router.callback_query(F.data == "settings:concurrent")
async def settings_concurrent(query: CallbackQuery, aria2):
    _, concurrent_raw = await _settings_data(aria2)
    await _edit(
        query, render_concurrent_chooser(concurrent_raw),
        reply_markup=concurrent_chooser_keyboard(concurrent_raw), parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("setconcurrent:"))
async def apply_concurrent(query: CallbackQuery, aria2):
    value = query.data.split(":", 1)[1]
    if value not in CONCURRENT_PRESETS:
        await query.answer("无效的数量", show_alert=True)
        return
    try:
        await aria2.set_max_concurrent(int(value))
    except Exception:
        log.exception("failed to set max-concurrent-downloads")
        await query.answer("设置失败，请稍后再试", show_alert=True)
        return
    settings.max_concurrent = int(value)
    _persist_env("MAX_CONCURRENT", value)  # re-applied to aria2 on bot startup
    await query.answer("✅ 已生效")
    await _show_settings(query, aria2)


@router.callback_query(F.data == "settings:notify")
async def toggle_notify(query: CallbackQuery, aria2):
    settings.notify_on_complete = not settings.notify_on_complete
    _persist_env("NOTIFY_ON_COMPLETE", "true" if settings.notify_on_complete else "false")
    await query.answer("🔔 完成通知已开启" if settings.notify_on_complete else "🔕 完成通知已关闭")
    await _show_settings(query, aria2)


@router.callback_query(F.data == "settings:maxsize")
async def settings_maxsize(query: CallbackQuery):
    current_mb = str(settings.max_file_size // (1024 * 1024))
    await _edit(
        query, render_maxsize_chooser(settings.max_file_size),
        reply_markup=maxsize_chooser_keyboard(current_mb), parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("setmaxsize:"))
async def apply_maxsize(query: CallbackQuery, aria2):
    value = query.data.split(":", 1)[1]
    presets = {v for _, v in MAXSIZE_PRESETS}
    if value not in presets:
        await query.answer("无效的大小", show_alert=True)
        return
    # "0" 约定为不限；其余预设单位是 MB，换算成字节存进 settings
    settings.max_file_size = int(value) * 1024 * 1024 if value != "0" else 0
    _persist_env("MAX_FILE_SIZE", str(settings.max_file_size))
    await query.answer("✅ 已生效")
    await _show_settings(query, aria2)


@router.callback_query(F.data == "settings:cleanup")
async def settings_cleanup(query: CallbackQuery):
    await _edit(
        query, render_cleanup_chooser(),
        reply_markup=cleanup_chooser_keyboard(settings.auto_cleanup_days), parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("setcleanup:"))
async def apply_cleanup(query: CallbackQuery, aria2, task_manager):
    value = query.data.split(":", 1)[1]
    presets = {v for _, v in CLEANUP_PRESETS}
    if value not in presets:
        await query.answer("无效的天数", show_alert=True)
        return
    settings.auto_cleanup_days = int(value)
    _persist_env("AUTO_CLEANUP_DAYS", value)
    # 立即按新设置跑一次，不用等下一个 24 小时周期才看到效果
    deleted = await task_manager.run_cleanup_once()
    toast = "✅ 已关闭自动清理" if settings.auto_cleanup_days == 0 else f"✅ 已生效，本次清理了 {deleted} 条记录"
    await query.answer(toast)
    await _show_settings(query, aria2)


@router.callback_query(F.data == "settings:dir")
async def settings_dir(query: CallbackQuery):
    options = settings.download_dir_options
    await _edit(
        query, render_dir_chooser(options),
        reply_markup=dir_chooser_keyboard(options), parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("setdir:"))
async def apply_dir(query: CallbackQuery, aria2):
    try:
        index = int(query.data.split(":", 1)[1])
        chosen = settings.download_dir_options[index]
    except (ValueError, IndexError):
        await query.answer("无效的目录", show_alert=True)
        return
    # 新目录此刻可能还不存在（比如刚在 .env 里配的预设），提前建好，
    # 避免用户切完目录第一次下载才发现目录不存在
    os.makedirs(chosen, exist_ok=True)
    settings.download_dir = chosen
    _persist_env("DOWNLOAD_DIR", chosen)
    await query.answer("✅ 已切换（不影响已下载文件的位置）")
    await _show_settings(query, aria2)


@router.callback_query(F.data.startswith("settings:"))
async def settings_fallback(query: CallbackQuery):
    await query.answer("该设置暂未接入。", show_alert=True)


@router.callback_query(F.data.startswith("list:"))
async def list_filter(query: CallbackQuery, repo, aria2):
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
        await _edit(
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
        text, markup = await render_task_list(repo, aria2, "ALL", 0)
        await _edit(query, text, answer_text=f"已清理 {deleted} 条记录", reply_markup=markup, parse_mode="HTML")
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
    pending = await repo.get_pending(token)
    if pending is None:
        await query.answer("这个待确认任务已过期，请重新发送。", show_alert=True)
        return
    if not _can_manage(query, pending.user_id):
        await query.answer("⛔ 只能操作自己添加的任务。", show_alert=True)
        return

    if action == "cancel":
        await repo.delete_pending(token)
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

    # atomically claim the confirmation BEFORE adding to aria2 — a rapid double
    # tap would otherwise pass the checks twice and add the download twice
    pending = await repo.pop_pending(token)
    if pending is None:
        await query.answer("任务正在处理中。")
        return

    try:
        gid = await _add_source(aria2, pending.kind, pending.payload, pending.file_name)
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
    # viewing (detail/open/link/files-info) is fine for any whitelisted user;
    # anything that mutates the task requires owner-or-admin
    if action not in {"detail", "open", "link"} and not _can_manage(query, row["user_id"]):
        await query.answer("⛔ 只能操作自己的任务。", show_alert=True)
        return

    if action in {"detail", "open"}:
        download = await _download_or_none(aria2, gid)
        status = _mapped_status(download, row["status"])
        await _edit(
            query,
            render_task_card(row, download, status=status),
            reply_markup=task_keyboard(gid, status, with_back=action == "open"),
            parse_mode="HTML",
        )
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
        if row["status"] == "COMPLETED":
            path = row["save_path"] or settings.download_dir
            link = row["gofile_link"] or "暂无下载链接"
            await query.answer(f"保存位置：{path}\n链接：{link}", show_alert=True)
            return

        download = await _download_or_none(aria2, gid)
        real_files = [f for f in download.files if not f.is_metadata] if download else []
        if not download or len(real_files) < 2:
            await query.answer(
                "单文件任务或元数据尚未就绪，无法选择文件。" if download else "任务信息暂不可用。",
                show_alert=True,
            )
            return
        await _edit(
            query, render_file_selection(download),
            reply_markup=file_selection_keyboard(gid, download), parse_mode="HTML",
        )
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


@router.callback_query(F.data.startswith("filesel:"))
async def toggle_file_selection(query: CallbackQuery, aria2, repo):
    _, gid, index_raw = query.data.split(":", 2)
    row = await repo.get_by_gid(gid)
    if row is None:
        await query.answer("任务不存在", show_alert=True)
        return
    if not _can_manage(query, row["user_id"]):
        await query.answer("⛔ 只能操作自己的任务。", show_alert=True)
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
    await _edit(
        query, render_file_selection(download),
        reply_markup=file_selection_keyboard(gid, download), parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("bulk:"))
async def bulk_action(query: CallbackQuery, aria2, repo):
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
