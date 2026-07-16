import asyncio
import logging
import os
import time

from aiogram import Bot
from aiogram.exceptions import TelegramRetryAfter

from bot.config import settings
from bot.core.cards import render_task_card
from bot.core import gofile
from bot.core.aria2_client import Aria2Client
from bot.core.compress import compress_path, remove_path
from bot.core.keyboards import task_keyboard
from bot.db.repo import TaskRepo

log = logging.getLogger(__name__)

POLL_INTERVAL_SECONDS = 5
# Telegram allows ~20 message edits per minute per chat; with several active
# tasks in one chat a 3s floor eats the budget and starts drawing 429s.
PROGRESS_EDIT_MIN_INTERVAL = 10.0
PROGRESS_EDIT_MIN_PERCENT_DELTA = 5.0

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}


class TaskManager:
    """Polls aria2 for in-flight tasks and throttles Telegram progress edits."""

    def __init__(self, bot: Bot, aria2: Aria2Client, repo: TaskRepo):
        self._bot = bot
        self._aria2 = aria2
        self._repo = repo
        self._last_edit: dict[str, tuple[float, float]] = {}  # gid -> (timestamp, percent)
        self._chat_backoff: dict[int, float] = {}  # chat_id -> monotonic deadline after a 429
        self._poll_task: asyncio.Task | None = None
        # Strong refs to fire-and-forget pipeline tasks: the event loop only
        # keeps weak references, so an unreferenced task can be GC'd mid-flight.
        self._bg_tasks: set[asyncio.Task] = set()
        self._running = False

    async def start(self):
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())

    def stop(self):
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
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
                await self._poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll loop iteration failed")
            await asyncio.sleep(POLL_INTERVAL_SECONDS)

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
        await self._bot.send_message(chat_id=row["chat_id"], text=text, reply_markup=markup)

    async def reconcile_on_startup(self):
        rows = await self._repo.get_unfinished()
        if not rows:
            return
        remote = {d.gid: d for d in await self._aria2.get_all_downloads()}
        for row in rows:
            gid = row["gid"]
            if gid and gid not in remote:
                await self._mark_lost(row, gid)
