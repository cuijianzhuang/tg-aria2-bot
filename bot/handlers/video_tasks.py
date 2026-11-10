"""Video task controls reuse callback identifiers, with no aria2 RPC calls."""

import asyncio

from bot.config import settings
from bot.core.compress import remove_path
from bot.core.expiry import safe_target
from bot.core.keyboards import task_cancel_confirm_keyboard, task_delete_confirm_keyboard
from bot.video.ui import video_card, video_task_keyboard


async def handle_video_task(query, repo, task_manager, row, action, back):
    gid = row["gid"]
    job = await repo.video_job(row["id"])
    if not job:
        await query.answer("视频任务信息不存在", show_alert=True)
        return
    if action in ("pause", "resume", "retry", "cancel_only", "delete_files"):
        if not settings.video_enabled:
            await query.answer("视频执行器未启用", show_alert=True)
            return
        expected = {
            "pause": ("PENDING", "ACTIVE"),
            "resume": ("PAUSED",),
            "retry": ("FAILED", "CANCELLED", "COMPLETED"),
            "cancel_only": ("PENDING", "ACTIVE", "PAUSED"),
            "delete_files": ("PENDING", "ACTIVE", "PAUSED"),
        }
        if row["status"] not in expected[action]:
            await query.answer("当前状态不支持该操作", show_alert=True)
            return
        await repo.video_command(gid, {"cancel_only": "cancel", "delete_files": "purge"}.get(action, action))
        await query.answer("操作已排队")
        return
    if action == "cancel":
        await query.message.edit_text("⚠️ 是否取消视频任务？", reply_markup=task_cancel_confirm_keyboard(gid))
    elif action == "confirm_delete_files":
        await query.message.edit_text(
            "⚠️ 确认永久删除已下载的视频及分片？", reply_markup=task_cancel_confirm_keyboard(gid, destructive=True)
        )
    elif action == "delete":
        if row["status"] in ("PENDING", "ACTIVE", "PAUSED"):
            await query.answer("请先取消任务再删除记录", show_alert=True)
            return
        await query.message.edit_text(
            "🗑 删除视频记录？", reply_markup=task_delete_confirm_keyboard(gid, can_delete_files=bool(job["work_dir"]))
        )
    elif action == "confirm_purge":
        await query.message.edit_text(
            "⚠️ 确认删除视频文件及分片？", reply_markup=task_delete_confirm_keyboard(gid, destructive=True)
        )
    elif action in ("delete_record", "purge"):
        if row["status"] in ("PENDING", "ACTIVE", "PAUSED"):
            await query.answer("请先取消任务", show_alert=True)
            return
        if action == "purge":
            target = safe_target(job["work_dir"], settings.download_dir)
            if target:
                await asyncio.to_thread(remove_path, target)
        await repo.delete_task(gid)
        await query.message.edit_text("已删除视频任务记录")
    elif action == "files" and row["status"] == "COMPLETED":
        await query.answer(row["save_path"] or "文件位置未知", show_alert=True)
        return
    elif action == "sendtg" and row["status"] == "COMPLETED":
        await query.answer("正在发送…")
        await task_manager.send_file_to_tg(row, gid)
        return
    elif action in ("detail", "open"):
        await query.message.edit_text(
            video_card(row, job), reply_markup=video_task_keyboard(gid, row["status"], back), parse_mode="HTML"
        )
    else:
        await query.answer("视频任务不支持此操作", show_alert=True)
        return
    await query.answer()
