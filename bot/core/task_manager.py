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
from bot.core.cards import render_task_card
from bot.core.compress import compress_path, remove_path
from bot.core.keyboards import task_keyboard
from bot.core.node_pool import NodePool
from bot.db.repo import TaskRepo

log = logging.getLogger(__name__)

# 轮询间隔。平时 5 秒（完成/出错主要靠 WebSocket 推送，轮询只是兜底）；
# 有用户正在看的卡片时降到 2 秒，让进度刷新跟得上
POLL_INTERVAL_SECONDS = 5
POLL_FAST_INTERVAL_SECONDS = 2
# 自动清理检查间隔：不需要很频繁，一天查一次即可（用户改天数后手动触发的
# run_cleanup_once 会立即生效，不必等这个周期）
CLEANUP_CHECK_INTERVAL_SECONDS = 24 * 3600
# 进度卡片刷新节流（配置见 config.progress_*）。
# 按聊天分配编辑预算：同一聊天里每多一张卡片，每张的间隔多 CHAT_EDIT_SPACING
# 秒，整个聊天始终保持约每 2 秒一次编辑，不会撞上 Telegram 的频率限制
# （真撞上了还有 429 退避兜底）
CHAT_EDIT_SPACING = 2.0
# 判断"这个聊天里有几张活跃卡片"的时间窗口：最近这么多秒内考虑过刷新的才算
ACTIVE_CARD_WINDOW = 15.0
# 下载卡住（已下载字节数没变）时不用每次都编辑，但也要隔一阵刷新一下，
# 让用户看到连接数的变化、知道机器人还活着
STALLED_REFRESH_INTERVAL = 30.0
# 用户打开卡片上的子菜单（限速/取消确认/选择文件…）后，暂停这张卡片的自动
# 刷新这么久——否则自动刷新会把正在操作的菜单冲掉
MENU_HOLD_SECONDS = 120.0

TERMINAL_STATUSES = {"COMPLETED", "FAILED", "CANCELLED"}

# 自建 telegram-bot-api（--local 模式）发送文件的上限，比公有 Bot API 的 50MB
# 宽松得多。这是 Telegram 本地服务器自身的硬限制，跟 settings.max_file_size
# （控制接收文件时拒绝的上限）是两回事，不要混用。
TG_MAX_SEND_BYTES = 2 * 1000 * 1024 * 1024

# 磁盘告警冷却时间：跌破阈值后先提醒一次，之后在冷却期内即使仍然低于阈值也不
# 重复刷屏；只有回升到阈值以上再次跌破时才会重新计时。
DISK_ALERT_COOLDOWN_SECONDS = 6 * 3600

# 节点连续不可达超过这个时长才告警一次（见 docs/MULTI_NODE_DESIGN.md §七）；
# 5 秒轮询本身就会偶尔因为网络抖动/aria2 重启瞬间连不上，10 分钟的门槛过滤掉
# 这类瞬时抖动，只对真正掉线的节点报警。
NODE_OFFLINE_ALERT_SECONDS = 10 * 60


def _multi_file(download) -> bool | None:
    """跟 callbacks._multi_file 同义：只有确定是单文件时才隐藏「选择文件」按钮。"""
    real = [f for f in download.files if not f.is_metadata]
    return len(real) > 1 if real else None


class TaskManager:
    """Polls every enabled aria2 node for in-flight tasks and throttles Telegram progress edits."""

    def __init__(self, bot: Bot, nodes: NodePool | None, repo: TaskRepo):
        self._bot = bot
        self._nodes = nodes
        self._repo = repo
        self._last_edit: dict[str, tuple[float, int]] = {}  # gid -> (timestamp, completed bytes)
        # chat_id -> {gid: 最近一次考虑刷新它的时间}，用来算这个聊天有几张活跃卡片
        self._chat_cards: dict[int, dict[str, float]] = {}
        # gid -> 到期时间（monotonic）。watched：用户正在看，快速刷新；
        # held：用户正开着子菜单，暂停自动刷新
        self._watched: dict[str, float] = {}
        self._held: dict[str, float] = {}
        self._chat_backoff: dict[int, float] = {}  # chat_id -> monotonic deadline after a 429
        self._poll_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        # monotonic 时间戳；None 表示当前不处于告警状态。不能用 0.0 当哨兵值——
        # time.monotonic() 的起点是系统/容器启动时刻，刚启动时它本身就可能小于
        # 冷却时长，会导致 `now - 0.0 < COOLDOWN` 恒为真，把第一次告警也吞掉。
        self._last_disk_alert: float | None = None
        # 节点离线告警：node name -> 首次探测到不可达的 monotonic 时间戳；
        # 已经告警过的节点进这个集合，恢复后清掉，跟磁盘告警同一套"跌破一次
        # 提醒、冷却期内不重复、恢复后重置"的语义
        self._node_unhealthy_since: dict[str, float] = {}
        self._node_alerted: set[str] = set()
        # Strong refs to fire-and-forget pipeline tasks: the event loop only
        # keeps weak references, so an unreferenced task can be GC'd mid-flight.
        self._bg_tasks: set[asyncio.Task] = set()
        # node name -> 该节点常驻 WebSocket 事件监听协程；节点是运行时动态
        # 加/删的（/addnode、节点管理页），所以这个集合每轮轮询都会对齐一次
        # （_reconcile_ws_listeners），不是启动时建好就不变了。
        self._ws_tasks: dict[str, asyncio.Task] = {}
        # 接了 WS 推送之后，同一个 gid 的完成/出错事件可能同时被轮询循环和
        # WS 回调两条路径拿到（WS 先一步 commit 更新，轮询循环手里那份
        # rows 快照还是旧状态，等轮到这个 gid 时又处理一遍）——不加锁的话
        # gofile 压缩上传/自动发送 TG 会被并发触发两次。这个集合只是"正在
        # 处理这个 gid 的终止转换"的标记，check-and-add 之间没有 await，
        # asyncio 单线程协作式调度保证这两步不会被其它协程插入。
        self._terminal_in_flight: set[str] = set()
        self._running = False

    async def start(self):
        self._running = True
        self._poll_task = asyncio.create_task(self._poll_loop())
        self._cleanup_task = asyncio.create_task(self._cleanup_loop())
        self._reconcile_ws_listeners()  # 不等第一轮轮询，启动就把 WS 连上

    def stop(self):
        self._running = False
        if self._poll_task:
            self._poll_task.cancel()
        if self._cleanup_task:
            self._cleanup_task.cancel()
        for task in self._ws_tasks.values():
            task.cancel()
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
                self._reconcile_ws_listeners()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("poll loop iteration failed")
            await asyncio.sleep(POLL_FAST_INTERVAL_SECONDS if self._any_watched() else POLL_INTERVAL_SECONDS)

    def _reconcile_ws_listeners(self):
        """WS 监听任务跟着轮询顺带对齐：新启用的节点补一条监听，被删/停用的
        节点撤掉对应监听。WS 只是让"通常情况"更快知道下载完成/出错，不是
        轮询的替代品——断线、节点没实现 WS（老版本 aria2）都不影响正确性，
        5 秒轮询这条兜底路径始终在跑。"""
        if self._nodes is None:
            return
        wanted = {n.name for n in self._nodes.enabled_nodes()}
        for name in [n for n in self._ws_tasks if n not in wanted]:
            self._ws_tasks.pop(name).cancel()
        for name in wanted:
            task = self._ws_tasks.get(name)
            if task is None or task.done():
                self._ws_tasks[name] = asyncio.create_task(self._ws_listen(name))

    async def _ws_listen(self, node_name: str):
        """常驻某节点的 WebSocket 事件流。一次 listen_events() 调用只跑完
        一条连接的生命周期（断开就返回/抛异常），断线退避 5 秒后重连；
        节点被删除/停用则直接退出，不再重连（下一轮 reconcile 也不会再补）。"""
        while self._running:
            node = self._nodes.get_node(node_name)
            if node is None or not node.enabled:
                return
            try:
                client = self._nodes.get(node_name)
                async for gid, _event in client.listen_events():
                    self._spawn(self._handle_ws_event(node_name, gid))
            except asyncio.CancelledError:
                raise
            except Exception:
                log.debug("node %s websocket disconnected, retrying in 5s", node_name)
            await asyncio.sleep(5)

    async def _handle_ws_event(self, node_name: str, gid: str):
        """WS 推送到一个 gid 的完成/出错事件后，立即单独查一次这个任务的
        状态并处理——不用等下一轮 5 秒轮询才发现。"""
        row = await self._repo.get_by_gid(gid)
        if row is None or row["node"] != node_name or row["status"] not in ("PENDING", "ACTIVE", "PAUSED"):
            return  # 跟这个 bot 无关，或者轮询已经先一步处理过了
        node = self._nodes.get_node(node_name)
        try:
            download = await self._nodes.get(node_name).get_status(gid)
        except Exception:
            return  # 拿不到就算了，兜底的轮询循环会补上
        await self._handle_download_state(row, download, node_is_local=node.is_local if node else True)

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
        # 逐节点轮询并发展开：每次 RPC 调用都有 10s 超时兜底（aria2_rpc.py），
        # 但串行 for 循环仍然会让排在后面的健康节点等前一个卡住的节点等满这
        # 10 秒——节点一多，一个离线节点就拖累了整轮轮询的延迟。改成 gather
        # 后每个节点互不阻塞；return_exceptions=True 确保一个节点内部的
        # 非预期异常（不是 RPC 层已经处理的连接失败）也不会打断其它节点。
        nodes = self._nodes.enabled_nodes()
        results = await asyncio.gather(*(self._poll_node(node) for node in nodes), return_exceptions=True)
        for node, result in zip(nodes, results, strict=True):
            if isinstance(result, Exception):
                log.exception("poll failed for node %s", node.name, exc_info=result)

    async def _poll_node(self, node):
        # 一个节点断线只跳过它自己，不影响其它节点，更不能把它的任务标
        # FAILED（节点不可达 ≠ 任务丢失）
        try:
            downloads = {d.gid: d for d in await self._nodes.get(node.name).get_all_downloads()}
        except Exception:
            await self._handle_node_health(node, False)
            return
        await self._handle_node_health(node, True)

        rows = await self._repo.get_unfinished(node=node.name)
        for row in rows:
            gid = row["gid"]
            if not gid:
                continue
            download = downloads.get(gid)
            if download is None:
                await self._mark_lost(row, gid, is_local=node.is_local)
                continue
            await self._handle_download_state(row, download, node_is_local=node.is_local)

    async def _handle_node_health(self, node, ok: bool):
        """更新健康缓存 + 离线超过 NODE_OFFLINE_ALERT_SECONDS 才告警一次。"""
        was_healthy = self._nodes.is_healthy(node.name)
        self._nodes.mark_health(node.name, ok)
        if ok:
            self._node_unhealthy_since.pop(node.name, None)
            self._node_alerted.discard(node.name)
            return
        if was_healthy:
            # 只在 在线→离线 的边沿记一条日志，避免每 5 秒刷一次
            log.warning("node %s unreachable, skipping this poll round", node.name)
        # dict.setdefault 的默认值参数无论 key 是否已存在都会先求值，不能直接
        # 塞 time.monotonic() 进去（那样每次调用都会多耗一次时间戳，且第二次
        # 调用起 since 会被错误地重新赋成"当前时间"）——先取一次时间戳存局部变量复用
        now = time.monotonic()
        since = self._node_unhealthy_since.setdefault(node.name, now)
        if node.name in self._node_alerted:
            return  # 已经告警过，冷却到恢复为止，不重复刷屏
        if now - since >= NODE_OFFLINE_ALERT_SECONDS:
            self._node_alerted.add(node.name)
            await self._notify_admins(
                f"🔴 <b>节点离线告警</b>\n节点「{node.display_name}」已连续 "
                f"{NODE_OFFLINE_ALERT_SECONDS // 60} 分钟无法访问，请检查该节点的 aria2 服务。"
            )

    async def _mark_lost(self, row, gid: str, *, is_local: bool = True):
        """aria2 no longer knows this gid. Usually a real loss (restart without a
        session file), but a completed task purged by the cleanup hook between
        polls looks identical — disambiguate cheaply by checking the disk.
        远程节点摸不到它的文件系统，跳过探测、一律按 FAILED 处理。"""
        target = row["save_path"] or (
            os.path.join(settings.download_dir, row["file_name"]) if row["file_name"] else None
        )
        if is_local and target and os.path.exists(target):
            log.info("gid %s gone from aria2 but file exists on disk, marking COMPLETED", gid)
            await self._repo.update_status(gid, "COMPLETED", save_path=target)
            await self._notify(row, self._render_card(row, status="COMPLETED"),
                               gid=gid, status="COMPLETED", parse_mode="HTML")
        else:
            log.warning("gid %s not found in aria2, marking FAILED", gid)
            await self._repo.update_status(
                gid, "FAILED", error="任务在 aria2 中丢失（服务重启或已被清理）"
            )
        self._forget_progress(gid)

    def _render_card(self, row, download=None, *, status: str) -> str:
        # 多节点部署时卡片带节点标注；单节点 label() 返回 None，界面不变
        return render_task_card(row, download, status=status, node_label=self._node_label(row))

    def _node_label(self, row) -> str | None:
        if self._nodes is None:
            return None  # 只测清理/发送等旁路功能的用例不装配节点池
        try:
            return self._nodes.label(row["node"])
        except (KeyError, IndexError):
            return None  # 旧测试的精简 fake row 可能没有 node 列

    async def _handle_download_state(self, row, download, *, node_is_local: bool = True):
        gid = row["gid"]
        status = download.status.upper()

        if status in ("COMPLETE", "ERROR"):
            if gid in self._terminal_in_flight:
                return  # 另一条路径（轮询/WS）已经在处理这个 gid 的终止转换了
            self._terminal_in_flight.add(gid)
            try:
                # 进正式处理前重新确认一次 DB 里的当前状态——轮询循环手里的
                # row 是本轮开始时的快照，如果 WS 路径已经抢先处理完（改成
                # COMPLETED/FAILED 了），这里就不用再重复触发一次 gofile
                # 上传/自动发送
                current = await self._repo.get_by_gid(gid)
                if current is None or current["status"] not in ("PENDING", "ACTIVE", "PAUSED"):
                    return
                if status == "COMPLETE":
                    if download.followed_by:
                        await self._handle_metadata_resolved(row, download)
                    else:
                        await self._handle_complete(row, download, node_is_local=node_is_local)
                else:
                    await self._handle_error(row, download)
            finally:
                self._terminal_in_flight.discard(gid)
            return

        if status in ("ACTIVE", "PAUSED", "WAITING"):
            mapped = "ACTIVE" if status == "ACTIVE" else ("PAUSED" if status == "PAUSED" else "PENDING")
            if mapped != row["status"]:
                await self._repo.update_status(gid, mapped)
                if mapped != "ACTIVE":  # ACTIVE keyboard refresh piggybacks on the progress edit below
                    await self._update_keyboard(row, gid, mapped)
            if status == "ACTIVE":
                await self._sync_real_name(row, gid, download)
                await self._maybe_report_progress(row, download)

    async def _sync_real_name(self, row, gid: str, download):
        """种子/磁力任务入库时的名字是 .torrent 文件名或磁力链接；拿到元数据后
        把真正的内容名回写，任务列表、搜索、完成通知就都显示好看的名字了。"""
        if row["source_type"] not in ("torrent", "magnet"):
            return
        real = download.name
        if not real or real.startswith("[METADATA]") or real == row["file_name"]:
            return
        try:
            await self._repo.update_file_name(gid, real)
        except Exception:
            log.debug("could not persist real name for gid %s", gid)

    async def _handle_metadata_resolved(self, row, download):
        """磁力/裸 infohash 任务的"元数据下载"阶段结束——这个 gid 抓到的只是
        种子信息本身（几十 KB），不是真正要下载的内容。aria2 已经自动另起
        了 download.followed_by[0] 这个新 gid 去下载真正的文件，这里把任务
        接到新 gid 上、状态打回 PENDING，交给下一轮轮询/WS 事件接着追踪；
        不触发 gofile/发送 TG 那一套（那是留给真正内容下载完成时的）。"""
        gid = row["gid"]
        new_gid = download.followed_by[0]
        log.info("gid %s finished metadata download, following to %s", gid, new_gid)
        await self._repo.retry_task(row["id"], new_gid)
        self._forget_progress(gid)

    async def _handle_complete(self, row, download, *, node_is_local: bool):
        gid = row["gid"]
        self._forget_progress(gid)
        save_path = str(download.files[0].path) if download.files else None
        # download.dir + download.name covers multi-file torrents too (the
        # first file alone would just be one piece of the whole download)
        target_path = os.path.join(download.dir, download.name) if download.name else save_path
        await self._repo.update_status(gid, "COMPLETED", save_path=target_path or save_path)

        # gofile 压缩上传 / 自动发送 TG 都要读本机磁盘上的产物，远程节点的
        # 文件在远端机器上，这两条流水线只对本机节点触发
        if node_is_local and settings.gofile_enabled and target_path and os.path.exists(target_path):
            # background task: a multi-GB compress+upload must not stall the
            # poll loop (it would freeze progress edits for every other task)
            self._spawn(self._run_gofile_pipeline(row, gid, target_path))
        else:
            await self._notify(
                row, self._render_card(row, download, status="COMPLETED"),
                gid=gid, status="COMPLETED", parse_mode="HTML",
                local=node_is_local,
            )
        # 跟 gofile 流水线是否启用无关，独立触发；目录任务和超限文件在
        # send_file_to_tg 内部直接跳过，这里不用重复判断
        if node_is_local and settings.auto_send_to_tg and target_path:
            self._spawn(self._auto_send_to_tg(row, gid, target_path))

    async def _handle_error(self, row, download):
        gid = row["gid"]
        self._forget_progress(gid)
        await self._repo.update_status(gid, "FAILED", error=download.error_message)
        await self._notify(
            row, self._render_card(row, download, status="FAILED"),
            gid=gid, status="FAILED", parse_mode="HTML",
        )

    async def _run_gofile_pipeline(self, row, gid, path: str):
        """compress (required for multi-file torrent directories, optional
        otherwise) -> upload to gofile.io -> delete the local copy if configured.
        Deletion only happens after a confirmed successful upload."""
        link = None  # 上传前就失败时，下面的完成通知也要能正常发出
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

        await self._notify(row, text, gid=gid, status="COMPLETED", link=link or None)

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
        now = time.monotonic()

        if now < self._chat_backoff.get(chat_id, 0.0):
            return  # still inside a Telegram flood-control window for this chat
        if self._is_held(gid, now):
            return  # 用户正开着这张卡片的子菜单，别把它冲掉

        interval = self._progress_interval(chat_id, gid, now)
        completed = download.completed_length
        last_time, last_completed = self._last_edit.get(gid, (0.0, -1))
        elapsed = now - last_time
        if elapsed < interval:
            return
        if completed == last_completed and elapsed < STALLED_REFRESH_INTERVAL:
            return  # 一个字节都没动：卡片内容基本不变，没必要每次都编辑

        self._last_edit[gid] = (now, completed)
        text = self._render_card(row, download, status="ACTIVE")
        if row["reply_message_id"]:
            try:
                await self._bot.edit_message_text(
                    chat_id=chat_id, message_id=row["reply_message_id"], text=text,
                    reply_markup=task_keyboard(gid, "ACTIVE", multi_file=_multi_file(download)),
                    parse_mode="HTML",
                )
            except TelegramRetryAfter as e:
                # honor flood control instead of hammering through it
                self._chat_backoff[chat_id] = now + e.retry_after
                log.info("telegram 429 for chat %s, backing off %ss", chat_id, e.retry_after)
            except Exception:
                pass  # message unchanged or transient error; safe to skip this tick

    # ---- 谁在看：快速刷新 / 暂停刷新 ----

    def watch(self, gid: str):
        """用户刚和这个任务互动过（添加、点了卡片上的按钮）：接下来
        progress_watch_seconds 秒内快速刷新它的卡片，并结束子菜单的暂停。"""
        self._watched[gid] = time.monotonic() + settings.progress_watch_seconds
        self._held.pop(gid, None)

    def hold(self, gid: str):
        """用户打开了卡片上的子菜单：暂停自动刷新，免得把菜单冲掉。"""
        now = time.monotonic()
        self._held[gid] = now + MENU_HOLD_SECONDS
        self._watched[gid] = now + settings.progress_watch_seconds

    def _is_watched(self, gid: str, now: float) -> bool:
        return self._watched.get(gid, 0.0) > now

    def _is_held(self, gid: str, now: float | None = None) -> bool:
        return self._held.get(gid, 0.0) > (time.monotonic() if now is None else now)

    def _any_watched(self) -> bool:
        now = time.monotonic()
        for gid in [g for g, t in self._watched.items() if t <= now]:
            del self._watched[gid]
        return bool(self._watched)

    def _forget_progress(self, gid: str):
        """任务进入终态/换 gid 时清掉进度节流的记录，不再占用聊天的刷新预算。"""
        self._last_edit.pop(gid, None)
        self._watched.pop(gid, None)
        self._held.pop(gid, None)
        for cards in self._chat_cards.values():
            cards.pop(gid, None)

    def _progress_interval(self, chat_id: int, gid: str, now: float) -> float:
        """这张卡片的最小刷新间隔。正在看的卡片按 progress_interval，后台的按
        progress_idle_interval；再和"聊天预算"取大——同一聊天里每多一张
        同档位的卡片，每张多等 CHAT_EDIT_SPACING 秒。顺带登记这张卡片、清掉
        过期的登记。"""
        cards = self._chat_cards.setdefault(chat_id, {})
        cards[gid] = now
        for g in [g for g, t in cards.items() if now - t > ACTIVE_CARD_WINDOW]:
            del cards[g]
        watched = self._is_watched(gid, now)
        peers = sum(1 for g in cards if self._is_watched(g, now) == watched)
        base = settings.progress_interval if watched else settings.progress_idle_interval
        return max(2.0, float(base), CHAT_EDIT_SPACING * peers)

    async def _update_keyboard(self, row, gid: str, status: str):
        if not row["reply_message_id"] or self._is_held(gid):
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
        local: bool = True,
        link: str | None = None,
    ):
        markup = task_keyboard(gid, status, local=local, link=link) if gid and status else None
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
        for node in self._nodes.enabled_nodes():
            rows = await self._repo.get_unfinished(node=node.name)
            if not rows:
                continue
            try:
                remote = {d.gid: d for d in await self._nodes.get(node.name).get_all_downloads()}
            except Exception:
                # 启动时节点连不上：不动它的任务（可能只是还没起来），交给
                # 轮询循环后续处理
                log.warning("node %s unreachable during startup reconcile, leaving its tasks as-is", node.name)
                await self._handle_node_health(node, False)
                continue
            for row in rows:
                gid = row["gid"]
                if gid and gid not in remote:
                    await self._mark_lost(row, gid, is_local=node.is_local)
