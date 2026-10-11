"""过期文件自动清理：已完成任务的文件超过 FILE_EXPIRY_DAYS 天后从磁盘删除。

删除前 FILE_EXPIRY_WARN_DAYS 天私聊提醒文件所有人，消息上有「保留」按钮；点了就永远
不再自动删这个任务的文件。任务记录本身保留（历史里还能看到，只是文件没了）。

安全：只删本机节点下载目录里的东西——路径取自数据库，不能信任，所以删除前把路径
解析成真实路径，必须严格位于该节点下载目录的内部（不能是目录本身，也不能借符号
链接逃出去）；符号链接本身只删链接，不跟进去删目标。
"""
import asyncio
import logging
import os
import shutil
import time
from datetime import UTC, datetime, timedelta
from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import settings

log = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 3600


def safe_target(path: str | None, root: str) -> str | None:
    """path 是否严格位于 root 内部。是则返回要删的路径，否则 None。"""
    if not path or not root:
        return None
    real_root = os.path.realpath(root)
    # 符号链接：只删链接本身，所以对"链接所在目录"做真实路径检查
    parent = os.path.realpath(os.path.dirname(path))
    if os.path.islink(path):
        candidate = os.path.join(parent, os.path.basename(path))
    else:
        candidate = os.path.realpath(path)
    if candidate == real_root or os.path.commonpath([candidate, real_root]) != real_root:
        return None
    return candidate


def delete_path(target: str) -> bool:
    """删文件/目录/符号链接。已经不存在也算成功（目的就是让它不在了）。"""
    try:
        if os.path.islink(target) or os.path.isfile(target):
            os.unlink(target)
        elif os.path.isdir(target):
            shutil.rmtree(target)
        return True
    except FileNotFoundError:
        return True
    except OSError:
        log.warning("could not delete expired path %s", target, exc_info=True)
        return False


def _cutoff(days: float) -> str:
    return (datetime.now(UTC) - timedelta(days=days)).isoformat()


def keep_keyboard(task_id: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="🔒 保留这个文件", callback_data=f"expiry:keep:{task_id}"),
    ]])


def _name(row) -> str:
    return escape(row["file_name"] or os.path.basename(row["save_path"] or "") or f"任务 {row['id']}", quote=False)


async def run_expiry_once(bot, repo, nodes) -> tuple[int, int]:
    """返回 (提醒了几个, 删除了几个)。"""
    days = settings.file_expiry_days
    if days <= 0:
        return 0, 0
    warn_days = min(max(settings.file_expiry_warn_days, 0), max(days - 1, 0))

    warned = 0
    if warn_days > 0:
        for row in await repo.get_expiring(_cutoff(days - warn_days), unwarned=True):
            node = nodes.get_node(row["node"]) if nodes else None
            if node is None or not node.is_local or not safe_target(row["save_path"], node.download_dir):
                # 远程节点 / 已删除的节点 / 路径不在下载目录里：这个机器人管不了，标成"不管理"，
                # 否则它们每轮都被查出来，占满单次处理上限，挤掉后面真正过期的任务
                await repo.set_expiry_keep(row["id"], True)
                continue
            try:
                await bot.send_message(
                    chat_id=row["chat_id"],
                    text=f"🗑 <b>文件即将自动删除</b>\n\n<code>{_name(row)}</code>\n"
                         f"将在约 {warn_days} 天后从服务器删除（下载完成已超过 {days - warn_days} 天）。需要保留请点下面的按钮。",
                    parse_mode="HTML", reply_markup=keep_keyboard(row["id"]),
                )
            except Exception:
                log.warning("could not send expiry warning for task %s", row["id"])
                continue   # 没提醒到就不标记，下一轮再试；删除也要等提醒过之后
            await repo.mark_expiry_warned(row["id"])
            warned += 1

    deleted = 0
    per_chat: dict[int, list[str]] = {}
    # 开了提醒的话必须提醒满 warn_days 天才删：刚开启这个功能时存量的旧文件也是
    # 先提醒、给足时间，而不是一次性全删
    warned_before = time.time() - warn_days * 86400 if warn_days > 0 else None
    for row in await repo.get_expiring(_cutoff(days), warned_before=warned_before):
        node = nodes.get_node(row["node"]) if nodes else None
        if node is None or not node.is_local:
            await repo.set_expiry_keep(row["id"], True)
            continue
        target = safe_target(row["save_path"], node.download_dir)
        if target is None:
            log.warning("refusing to delete %r: outside download dir %r", row["save_path"], node.download_dir)
            await repo.mark_files_deleted(row["id"])   # 别每小时都重复尝试一个永远不会删的路径
            continue
        if await asyncio.to_thread(delete_path, target):
            await repo.mark_files_deleted(row["id"])
            deleted += 1
            per_chat.setdefault(row["chat_id"], []).append(_name(row))
    for chat_id, names in per_chat.items():
        shown = "\n".join(f"• <code>{n}</code>" for n in names[:10])
        more = f"\n…等共 {len(names)} 个" if len(names) > 10 else ""
        try:
            await bot.send_message(chat_id=chat_id, text=f"🧹 已自动删除 {len(names)} 个过期文件：\n{shown}{more}",
                                   parse_mode="HTML")
        except Exception:
            log.info("could not send expiry summary to chat %s", chat_id)
    if warned or deleted:
        log.info("file expiry: warned %d, deleted %d", warned, deleted)
    return warned, deleted


async def expiry_loop(bot, repo, nodes, *, first_delay: float = 300.0):
    await asyncio.sleep(first_delay)
    while True:
        try:
            await run_expiry_once(bot, repo, nodes)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("file expiry failed")
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
