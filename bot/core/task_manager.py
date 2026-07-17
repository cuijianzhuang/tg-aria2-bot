import asyncio
import logging
import os
import shutil
import time
from datetime import UTC, datetime, timedelta

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter
from aiogram.types import FSInputFile

from bot.config import settings
from bot.core import gofile
from bot.core.aria2_client import Aria2Client
from bot.core.cards import render_task_card
from bot.core.compress import compress_path, remove_path
from bot.core.keyboards import task_keyboard
from bot.db.repo import TaskRepo

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
# 自动清理检查间隔：不需要很频繁，一天查一次即可（用户改天数后手动触发的
# run_cleanup_once 会立即生效，不必等这个周期）
CLEANUP_CHECK_INTERVAL_SECONDS = 24 * 3600
# Telegram allows ~20 message edits per minute per chat; with several active
# tasks in one chat a 3s floor eats the budget and starts drawing 429s.
PROGRESS_EDIT_MIN_INTERVAL = 10.0
PROGRESS_EDIT_MIN_PERCENT_DELTA = 5.0

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}

# 自建 telegram-bot-api（--local 模式）发送文件的上限，比公有 Bot API 的 50MB
# 宽松得多。这是 Telegram 本地服务器自身的硬限制，跟 settings.max_file_size
# （控制接收文件时拒绝的上限）是两回事，不要混用。
TG_MAX_SEND_BYTES = 2 * 1000 * 1024 * 1024

# 磁盘告警冷却时间：跌破阈值后先提醒一次，之后在冷却期内即使仍然低于阈值也不
# 重复刷屏；只有回升到阈值以上再次跌破时才会重新计时。
DISK_ALERT_COOLDOWN_SECONDS = 6 * 3600


class TaskManager:
    """Polls aria2 for in-flight tasks and throttles Telegram progress edits."""

    def __init__(self, bot: Bot, aria2: Aria2Client, repo: TaskRepo):
        self._bot = bot
        self._aria2 = aria2
        self._repo = repo
        self._last_edit: dict[str, tuple[float, float]] = {}  # gid -> (timestamp, percent)
        self._chat_backoff: dict[int, float] = {}  # chat_id -> monotonic deadline after a 429
        self._poll_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        # monotonic 时间戳；None 表示当前不处于告警状态。不能用 0.0 当哨兵值——
        # time.monotonic() 的起点是系统/容器启动时刻，刚启动时它本身就可能小于
        # 冷却时长，会导致 `now - 0.0 < COOLDOWN` 恒为真，把第一次告警也吞掉。
        self._last_disk_alert: float | None = None
        # Strong refs to fire-and-forget pipeline tasks: the event loop only
        # keeps weak references, so an unreferenced task can be GC'd mid-flight.
        self._bg_tasks: set[asyncio.Task] = set()
        self._running = False

    async def start(self):
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())

    def stop(self):
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
        if self._cleanup_task:
            self._cleanup_task.cancel()
        for task in self._bg_tasks:
            task.cancel()

    def _spawn(self, coro):
        task = asyncio.create_task(coro)
        self._bg_tasks.add(task)
        task.add_done_callback(self._bg_tasks.discard)
        return task

    async def _poll_loop(self):
        while self._running:
            try:
                await self._check_disk_space()
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll loop iteration failed")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

    async def _check_disk_space(self):
        """磁盘剩余空间低于阈值时主动提醒管理员。disk_usage 只是一次 stat 调用，
        跟着 5 秒轮询一起查代价可以忽略；用冷却时间避免在阈值附近反复刷屏。"""
        threshold = settings.disk_alert_threshold_gb
        if threshold <= 0:
            return
        try:
            usage = await asyncio.to_thread(shutil.disk_usage, settings.download_dir)
        except OSError:
            return

        free_gb = usage.free / 1024**3
        now = time.monotonic()
        if free_gb >= threshold:
            self._last_disk_alert = None  # 恢复正常，下次再跌破会重新提醒
            return
        if self._last_disk_alert is not None and now - self._last_disk_alert < DISK_ALERT_COOLDOWN_SECONDS:
            return  # 仍处于告警冷却期，不重复发

        self._last_disk_alert = now
        await self._notify_admins(
            f"⚠️ <b>磁盘空间告警</b>\n剩余 {free_gb:.1f} GB，低于设置的 {threshold} GB 阈值。"
        )

    async def _notify_admins(self, text: str):
        # 明确配置的 ADMIN_USER_IDS 优先；没配就退回 ALLOWED_USER_IDS（跟
        # settings.is_admin 的判定逻辑保持一致）。两者都空说明没人可通知。
        recipients = settings.admin_ids or settings.allowed_ids
        if not recipients:
            log.warning("disk alert triggered but no admin/allowed ids configured: %s", text)
            return
        for uid in recipients:
            try:
                await self._bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
            except Exception:
                log.warning("failed to send disk alert to user %s", uid)

    async def _poll_once(self):
        rows = await self._repo.get_unfinished()
        if not rows:
            return
        # one RPC batch for everything instead of a get_status roundtrip per task
        downloads = {d.gid: d for d in await self._aria2.get_all_downloads()}
        for row in rows:
            gid = row["gid"]
            if not gid:
                continue
            download = downloads.get(gid)
            if download is None:
                await self._mark_lost(row, gid)
                continue
            await self._handle_download_state(row, download)

    async def _mark_lost(self, row, gid: str):
        """aria2 no longer knows this gid. Usually a real loss (restart without a
        session file), but a completed task purged by the cleanup hook between
        polls looks identical — disambiguate cheaply by checking the disk."""
        target = row["save_path"] or (
            os.path.join(settings.download_dir, row["file_name"]) if row["file_name"] else None
        )
        if target and os.path.exists(target):
            log.info("gid %s gone from aria2 but file exists on disk, marking COMPLETED", gid)
            await self._repo.update_status(gid, "COMPLETED", save_path=target)
            await self._notify(row, render_task_card(row, status="COMPLETED"),
                               gid=gid, status="COMPLETED", parse_mode="HTML")
        else:
            log.warning("gid %s not found in aria2, marking FAILED", gid)
            await self._repo.update_status(
                gid, "FAILED", error="任务在 aria2 中丢失（服务重启或已被清理）"
            )
        self._last_edit.pop(gid, None)

    async def _handle_download_state(self, row, download):
        gid = row["gid"]
        status = download.status.upper()

        if status == "COMPLETE":
            self._last_edit.pop(gid, None)
            save_path = str(download.files[0].path) if download.files else None
            # download.dir + download.name covers multi-file torrents too (the
            # first file alone would just be one piece of the whole download)
            target_path = os.path.join(download.dir, download.name) if download.name else save_path
            await self._repo.update_status(gid, "COMPLETED", save_path=target_path or save_path)

            if settings.gofile_enabled and target_path and os.path.exists(target_path):
                # background task: a multi-GB compress+upload must not stall the
                # poll loop (it would freeze progress edits for every other task)
                self._spawn(self._run_gofile_pipeline(row, gid, target_path))
            else:
                await self._notify(
                    row, render_task_card(row, download, status="COMPLETED"),
                    gid=gid, status="COMPLETED", parse_mode="HTML",
                )
            # 跟 gofile 流水线是否启用无关，独立触发；目录任务和超限文件在
            # send_file_to_tg 内部直接跳过，这里不用重复判断
            if settings.auto_send_to_tg and target_path:
                self._spawn(self._auto_send_to_tg(row, gid, target_path))
            return

        if status == "ERROR":
            self._last_edit.pop(gid, None)
            await self._repo.update_status(gid, "FAILED", error=download.error_message)
            await self._notify(
                row, render_task_card(row, download, status="FAILED"),
                gid=gid, status="FAILED", parse_mode="HTML",
            )
            return

        if status in ("ACTIVE", "PAUSED", "WAITING"):
            mapped = "ACTIVE" if status == "ACTIVE" else ("PAUSED" if status == "PAUSED" else "PENDING")
            if mapped != row["status"]:
                await self._repo.update_status(gid, mapped)
                if mapped != "ACTIVE":  # ACTIVE keyboard refresh piggybacks on the progress edit below
                    await self._update_keyboard(row, gid, mapped)
            if status == "ACTIVE":
                await self._maybe_report_progress(row, download)

    async def _run_gofile_pipeline(self, row, gid, path: str):
        """compress (required for multi-file torrent directories, optional
        otherwise) -> upload to gofile.io -> delete the local copy if configured.
        Deletion only happens after a confirmed successful upload."""
        try:
            need_compress = settings.gofile_compress or os.path.isdir(path)
            if need_compress:
                await self._notify(
                    row, f"📦 下载完成: {row['file_name'] or gid}\n🗜 正在压缩，请稍候…",
                )
            upload_path = await asyncio.to_thread(compress_path, path) if need_compress else path
            archive_created = upload_path if upload_path != path else None

            await self._notify(
                row, f"📦 下载完成: {row['file_name'] or gid}\n☁️ 正在上传 GoFile，请稍候…",
            )
            data = await gofile.upload_file(upload_path, settings.gofile_token or None)
            link = data.get("downloadPage", "")
            await self._repo.update_gofile_link(gid, link)

            deleted = False
            if settings.gofile_delete_local:
                await asyncio.to_thread(remove_path, path)
                if archive_created:
                    await asyncio.to_thread(remove_path, archive_created)
                deleted = True

            text = f"✅ 下载完成: {row['file_name'] or gid}\n☁️ 已上传: {link}"
            if deleted:
                text += "\n🗑 本地文件已删除"
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("gofile pipeline failed for gid %s", gid)
            text = f"✅ 下载完成: {row['file_name'] or gid}\n⚠️ 上传 gofile 失败: {e}"

        await self._notify(row, text, gid=gid, status="COMPLETED")

    async def _auto_send_to_tg(self, row, gid: str, path: str):
        ok, msg = await self.send_file_to_tg(row, gid, path)
        if not ok:
            # 自动发送场景下静默跳过失败（多半是目录/超限），完成卡片本身已经
            # 通知过用户了，不用再额外弹一条失败提示制造噪音
            log.info("auto-send-to-tg skipped for gid %s: %s", gid, msg)

    async def send_file_to_tg(self, row, gid: str, path: str | None = None) -> tuple[bool, str]:
        """把已完成任务的文件发回 Telegram。目录任务、以及超过本地 Bot API
        发送上限的文件直接拒绝，不会去尝试（避免卡住或占满带宽）。
        供自动发送和任务卡片上的"发送到 TG"按钮共用。"""
        target = path or row["save_path"]
        if not target or not os.path.isfile(target):
            return False, "文件不存在或是目录，无法发送"
        size = os.path.getsize(target)
        if size > TG_MAX_SEND_BYTES:
            return False, f"文件过大（{size / 1024**3:.1f} GB），超过 Telegram 发送上限"
        try:
            await self._bot.send_document(
                chat_id=row["chat_id"],
                document=FSInputFile(target),
                caption=row["file_name"] or os.path.basename(target),
            )
            return True, "已发送"
        except Exception as e:
            log.exception("failed to send file to telegram for gid %s", gid)
            return False, f"发送失败: {e}"

    async def _maybe_report_progress(self, row, download):
        gid = row["gid"]
        chat_id = row["chat_id"]
        percent = download.progress
        now = time.monotonic()

        if now < self._chat_backoff.get(chat_id, 0.0):
            return  # still inside a Telegram flood-control window for this chat

        last_time, last_percent = self._last_edit.get(gid, (0.0, -100.0))
        if (now - last_time) < PROGRESS_EDIT_MIN_INTERVAL and (percent - last_percent) < PROGRESS_EDIT_MIN_PERCENT_DELTA:
            return

        self._last_edit[gid] = (now, percent)
        text = render_task_card(row, download, status="ACTIVE")
        if row["reply_message_id"]:
            try:
                await self._bot.edit_message_text(
                    chat_id=chat_id, message_id=row["reply_message_id"], text=text,
                    reply_markup=task_keyboard(gid, "ACTIVE"),
                    parse_mode="HTML",
                )
            except TelegramRetryAfter as e:
                # honor flood control instead of hammering through it
                self._chat_backoff[chat_id] = now + e.retry_after
                log.info("telegram 429 for chat %s, backing off %ss", chat_id, e.retry_after)
            except Exception:
                pass  # message unchanged or transient error; safe to skip this tick

    async def _update_keyboard(self, row, gid: str, status: str):
        if not row["reply_message_id"]:
            return
        try:
            await self._bot.edit_message_reply_markup(
                chat_id=row["chat_id"], message_id=row["reply_message_id"],
                reply_markup=task_keyboard(gid, status),
            )
        except Exception:
            pass

    async def _notify(
        self,
        row,
        text: str,
        *,
        gid: str | None = None,
        status: str | None = None,
        parse_mode: str | None = None,
    ):
        markup = task_keyboard(gid, status) if gid and status else None
        if row["reply_message_id"]:
            try:
                await self._bot.edit_message_text(
                    chat_id=row["chat_id"], message_id=row["reply_message_id"], text=text,
                    reply_markup=markup,
                    parse_mode=parse_mode,
                )
                return
            except Exception:
                pass
        # editing the existing card is always fine (edits don't push-notify);
        # only a brand-new message actually notifies, so that's what the
        # 完成通知 toggle gates
        if settings.notify_on_complete:
            await self._bot.send_message(chat_id=row["chat_id"], text=text, reply_markup=markup)

    async def _cleanup_loop(self):
        # 每天检查一次是否需要清理，比 5 秒轮询低频得多，避免无意义的空跑
        while self._running:
            try:
                await self.run_cleanup_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("auto cleanup failed")
            await asyncio.sleep(CLEANUP_CHECK_INTERVAL_SECONDS)

    async def run_cleanup_once(self) -> int:
        """按 AUTO_CLEANUP_DAYS 清理过期的已完成任务记录，返回删除条数。
        AUTO_CLEANUP_DAYS <= 0 表示关闭，直接跳过。设置菜单里改天数后会立即
        调用一次这个方法，不用等下一个 24 小时周期。"""
        days = settings.auto_cleanup_days
        if days <= 0:
            return 0
        cutoff = (datetime.now(UTC) - timedelta(days=days)).isoformat()
        deleted = await self._repo.delete_old_completed(cutoff)
        if deleted:
            log.info("auto cleanup removed %d completed task records older than %d days", deleted, days)
        return deleted

    async def reconcile_on_startup(self):
        rows = await self._repo.get_unfinished()
        if not rows:
            return
        remote = {d.gid: d for d in await self._aria2.get_all_downloads()}
        for row in rows:
            gid = row["gid"]
            if gid and gid not in remote:
                await self._mark_lost(row, gid)
