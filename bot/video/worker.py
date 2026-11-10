"""Persistent local video queue, process controls and shared quota integration."""

import asyncio
import json
import logging
import signal
import time
from pathlib import Path

from bot.config import settings
from bot.core import quota, storage
from bot.core.aria2_client import Download, File
from bot.core.expiry import safe_target
from bot.video.accounts import CredentialVault, account_cookies
from bot.video.backends import download_args, progress_event
from bot.video.process import capture, signal_group, spawn, terminate
from bot.video.services import get_resolver

log = logging.getLogger(__name__)


class VideoWorker:
    def __init__(self, bot, repo, task_manager):
        self.bot, self.repo, self.task_manager = bot, repo, task_manager
        self.vault = CredentialVault(Path(settings.db_path).parent)
        self.resolver = get_resolver()
        self.running = {}
        self.tasks = {}
        self.stopping = False
        self._last_report = {}

    async def run(self):
        for path in self.vault.root.glob("cookies-*.txt"):
            path.unlink(missing_ok=True)
        for row in await self.repo.video_jobs():
            if row["status"] in ("ACTIVE", "PENDING") and not row["command"]:
                await self.repo.update_status(row["gid"], "PAUSED")
                await self.repo.video_update(row["id"], phase="恢复前请继续任务")
        try:
            while True:
                for row in await self.repo.video_jobs():
                    try:
                        await self._tick(row)
                    except Exception:
                        log.warning("video queue action failed for task %s", row["id"])
                await asyncio.sleep(1)
        finally:
            self.stopping = True
            for process in list(self.running.values()):
                await terminate(process)
            for task in list(self.tasks.values()):
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)

    async def _tick(self, row):
        gid, action = row["gid"], row["command"]
        process = self.running.get(gid)
        if action:
            await self.repo.video_update(row["id"], command=None)
        if action == "pause":
            if process:
                signal_group(process, signal.SIGSTOP)
            await self.repo.update_status(gid, "PAUSED")
        elif action == "resume":
            refusal = await quota.admission_error(
                self.repo,
                row["user_id"],
                None,
                pending_usage=self.task_manager.pending_usage(row["user_id"]),
                check_active=False,
            )
            if refusal:
                await self.repo.video_update(row["id"], phase=refusal)
            elif process:
                signal_group(process, signal.SIGCONT)
                await self.repo.update_status(gid, "ACTIVE")
            else:
                await self.repo.retry_task(row["id"], gid)
        elif action in ("cancel", "purge"):
            await self.repo.update_status(gid, "CANCELLED")
            if process:
                await terminate(process)
            task = self.tasks.get(gid)
            if task:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
            if action == "purge":
                target = safe_target(row["work_dir"], settings.download_dir)
                if target:
                    from bot.core.compress import remove_path

                    await asyncio.to_thread(remove_path, target)
        elif action == "retry":
            if row["status"] not in ("PENDING", "ACTIVE", "PAUSED"):
                refusal = await quota.admission_error(
                    self.repo, row["user_id"], None, pending_usage=self.task_manager.pending_usage(row["user_id"])
                )
                if not refusal:
                    await self.repo.retry_task(row["id"], gid)
        elif (
            not action
            and row["status"] == "PENDING"
            and gid not in self.tasks
            and len(self.tasks) < max(1, settings.video_concurrency)
        ):
            task = asyncio.create_task(self._execute(dict(row)))
            self.tasks[gid] = task
            task.add_done_callback(lambda done, key=gid: self.tasks.pop(key, None))
        latest = await self.repo.get_by_gid(gid)
        if latest and latest["status"] in ("PENDING", "PAUSED", "ACTIVE", "CANCELLED"):
            await self.report(latest)

    async def snapshot(self, row):
        job = await self.repo.video_job(row["id"])
        directory = Path(job["work_dir"] or settings.download_dir)
        output = job["output"]
        files = [File(1, Path(output), job["completed"], job["completed"], True)] if output else []
        return Download(
            row["gid"],
            {
                "PENDING": "waiting",
                "ACTIVE": "active",
                "PAUSED": "paused",
                "COMPLETED": "complete",
                "FAILED": "error",
                "CANCELLED": "removed",
            }.get(row["status"], "waiting"),
            job["total"],
            job["completed"],
            0,
            0,
            0,
            row["error"],
            directory,
            files,
        )

    async def report(self, row, force=False):
        if not force and getattr(self.task_manager, "is_task_menu_open", lambda gid: False)(row["gid"]):
            return
        now = time.monotonic()
        if not force and now - self._last_report.get(row["gid"], 0) < 5:
            return
        self._last_report[row["gid"]] = now
        from bot.video.ui import video_card, video_task_keyboard

        job = await self.repo.video_job(row["id"])
        if not row["reply_message_id"]:
            return
        try:
            await self.bot.edit_message_text(
                chat_id=row["chat_id"],
                message_id=row["reply_message_id"],
                text=video_card(row, job),
                reply_markup=video_task_keyboard(row["gid"], row["status"]),
                parse_mode="HTML",
            )
        except Exception:
            pass

    async def _execute(self, row):
        gid = row["gid"]
        directory = Path(settings.download_dir) / "video" / gid
        process = None
        try:
            directory.mkdir(parents=True, exist_ok=True)
            await self.repo.video_update(row["id"], work_dir=str(directory), phase="解析中")
            await self.repo.update_status(gid, "ACTIVE")
            if not storage.has_enough_space(settings.download_dir, row["file_size"] or 0):
                raise ValueError("磁盘空间不足")
            cookies = await account_cookies(self.repo, self.vault, row["user_id"], row["payload"], row["account_mode"])
            with self.vault.snapshot(cookies) as cookie_file:
                info = await self.resolver.probe(
                    row["payload"],
                    parser=row["parser"],
                    cookies=cookie_file,
                    proxy=settings.proxy_url,
                    quality=row["quality"],
                )
                if row["quality"] not in info.qualities:
                    raise ValueError("原画质已不可用，请重新发送链接选择画质")
                limits = await quota.limits_for(self.repo, row["user_id"])
                if limits.max_task_bytes and info.size and info.size > limits.max_task_bytes:
                    raise ValueError("视频预计大小超过单任务上限")
                refusal = await quota.admission_error(
                    self.repo,
                    row["user_id"],
                    info.size,
                    pending_usage=self.task_manager.pending_usage(row["user_id"]),
                    check_active=False,
                )
                if refusal:
                    await self.repo.update_status(gid, "PAUSED")
                    await self.repo.video_update(row["id"], phase=refusal)
                    return
                latest = await self.repo.get_by_gid(gid)
                if latest is None or latest["status"] in ("PAUSED", "CANCELLED"):
                    return
                await self.repo.update_file_name(gid, info.title)
                await self.repo.video_update(row["id"], phase="下载中", total=info.size or 0)
                process = await spawn(
                    download_args(
                        row["parser"], row["payload"], directory, row["quality"], cookie_file, settings.proxy_url
                    )
                )
                self.running[gid] = process
                output = None
                streams = json.loads(row.get("progress_data") or "{}")
                accounted = row["completed"]
                buffer = ""
                merging = False
                while True:
                    try:
                        chunk = await asyncio.wait_for(process.stdout.read(8192), 1)
                    except TimeoutError:
                        chunk = None
                    if chunk == b"":
                        break
                    if chunk:
                        buffer += chunk.decode("utf-8", errors="replace").replace("\r", "\n")
                        if len(buffer) > 262144:
                            raise ValueError("下载器输出异常")
                        lines = buffer.split("\n")
                        buffer = lines.pop()
                        for line in lines:
                            if "[Merger]" in line or "Merging" in line or "Merging video" in line:
                                merging = True
                                await self.repo.video_update(row["id"], phase="合并中")
                            if line.startswith("__VIDEO_OUTPUT__"):
                                output = line.removeprefix("__VIDEO_OUTPUT__").strip()
                            event = progress_event(line)
                            if event:
                                key = str(event.get("filename", "stream"))
                                streams[key] = max(streams.get(key, 0), int(event["downloaded"]))
                    if row["parser"] == "you-get" and not merging:
                        measured = sum(
                            p.stat().st_size for p in directory.rglob("*") if p.is_file() and not p.is_symlink()
                        )
                        streams["files"] = max(streams.get("files", 0), measured)
                    completed = sum(streams.values())
                    delta = max(0, completed - accounted)
                    if delta:
                        await self.repo.add_usage(row["user_id"], quota.today(), delta)
                        accounted = completed
                        await self.repo.video_update(row["id"], completed=completed, progress_data=json.dumps(streams))
                    latest = await self.repo.get_by_gid(gid)
                    if latest is None or latest["status"] == "CANCELLED":
                        await terminate(process)
                        return
                    if limits.max_task_bytes and completed > limits.max_task_bytes:
                        await terminate(process)
                        from bot.core.compress import remove_path

                        await asyncio.to_thread(remove_path, str(directory))
                        raise ValueError("视频超过单任务上限，已清理部分文件")
                    used = await self.task_manager.usage_today(row["user_id"])
                    if limits.daily_bytes and used >= limits.daily_bytes and latest["status"] == "ACTIVE":
                        signal_group(process, signal.SIGSTOP)
                        await self.repo.update_status(gid, "PAUSED")
                        await self.repo.video_update(row["id"], phase="每日额度已用完；次日可继续")
                    await self.report(latest)
                await process.wait()
                latest = await self.repo.get_by_gid(gid)
                if not latest or latest["status"] == "CANCELLED" or self.stopping:
                    return
                if process.returncode:
                    raise ValueError("下载或合并失败；可重试或重新扫码登录")
                if output:
                    candidate = Path(output)
                else:
                    candidates = [
                        p
                        for p in directory.glob("media.*")
                        if p.suffix.lower() in (".mp4", ".mkv", ".webm", ".flv", ".mov", ".m4a", ".mp3", ".ts")
                    ]
                    candidate = max(candidates, key=lambda p: p.stat().st_size) if candidates else None
                if (
                    candidate is None
                    or not candidate.is_file()
                    or not candidate.stat().st_size
                    or not safe_target(str(candidate), str(directory))
                ):
                    raise ValueError("没有找到合并完成的视频文件")
                if candidate.is_symlink():
                    raise ValueError("视频输出不能是符号链接")
                if limits.max_task_bytes and candidate.stat().st_size > limits.max_task_bytes:
                    from bot.core.compress import remove_path

                    await asyncio.to_thread(remove_path, str(directory))
                    raise ValueError("合并后的视频超过单任务上限，已清理文件")
                verification = json.loads(
                    await capture(
                        [
                            "ffprobe",
                            "-v",
                            "error",
                            "-select_streams",
                            "v:0",
                            "-show_entries",
                            "stream=codec_type",
                            "-of",
                            "json",
                            str(candidate),
                        ],
                        timeout=30,
                    )
                )
                if not verification.get("streams"):
                    raise ValueError("下载结果没有有效视频流")
                await self.repo.video_update(
                    row["id"], phase="已完成", output=str(candidate), total=candidate.stat().st_size
                )
                download = Download(
                    gid,
                    "complete",
                    candidate.stat().st_size,
                    accounted,
                    0,
                    0,
                    0,
                    None,
                    directory,
                    [File(1, candidate, candidate.stat().st_size, candidate.stat().st_size, True)],
                )
                await self.repo.video_set_size(row["id"], candidate.stat().st_size)
                await self.task_manager.complete_local_video(row, download)
        except asyncio.CancelledError:
            if process:
                await terminate(process)
            if self.stopping:
                await self.repo.update_status(gid, "PAUSED")
            raise
        except Exception as exc:
            message = str(exc) if isinstance(exc, ValueError) else "视频组件执行失败，请检查依赖和权限"
            await self.repo.update_status(gid, "FAILED", error=message)
            latest = await self.repo.get_by_gid(gid)
            if latest:
                await self.report(latest, force=True)
        finally:
            if process:
                await terminate(process)
            self.running.pop(gid, None)
