from __future__ import annotations

import os
from datetime import datetime
from html import escape

from bot.config import settings
from bot.core.keyboards import STATUS_LABEL, text_progress_bar
from bot.core.storage import disk_usage_summary

DIVIDER = "──────────────"


def _fmt_size(value: int | None) -> str:
    if value is None:
        return "未知"
    size = float(value)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size:.0f} B"
        size /= 1024
    return f"{size:.1f} PiB"


def _safe_call(obj, name: str, default: str = "-") -> str:
    attr = getattr(obj, name, None)
    if callable(attr):
        try:
            return attr()
        except Exception:
            return default
    return default


def _eta(download) -> str:
    speed = getattr(download, "download_speed", 0) or 0
    total = getattr(download, "total_length", 0) or 0
    completed = getattr(download, "completed_length", 0) or 0
    remaining = max(0, total - completed)
    if not speed or not remaining:
        return "未知"
    seconds = int(remaining / speed)
    if seconds < 60:
        return f"约 {seconds}秒"
    minutes, sec = divmod(seconds, 60)
    if minutes < 60:
        return f"约 {minutes}分{sec:02d}秒"
    hours, minutes = divmod(minutes, 60)
    return f"约 {hours}小时{minutes:02d}分"


def _state_reason(download, status: str) -> str:
    if status == "PENDING":
        return "等待下载服务分配任务"
    if status == "PAUSED":
        return "用户已暂停"
    if status == "FAILED":
        return getattr(download, "error_message", None) or "下载服务返回错误"
    if status == "COMPLETED":
        return "下载完成"
    if status == "ACTIVE" and not (getattr(download, "download_speed", 0) or 0):
        return "暂无下载速度，可能正在连接节点或获取元数据"
    return "正在下载"


def _status_icon(status: str) -> str:
    return {
        "PENDING": "⏳",
        "ACTIVE": "⬇️",
        "PAUSED": "⏸",
        "COMPLETED": "✅",
        "FAILED": "⚠️",
        "CANCELLED": "🗑",
    }.get(status, "•")


def render_home(counts: dict[str, int] | None = None, stats=None) -> str:
    """Home doubles as the dashboard: task counts, live speed, disk space.
    stats is aria2's global stat; None degrades gracefully (no network line)."""
    c = counts or {}
    lines = [
        "🤖 <b>下载机器人</b>",
        DIVIDER,
        f"⬇️ 下载中 {c.get('ACTIVE', 0)} · ⏳ 等待 {c.get('PENDING', 0)} · ⏸ 暂停 {c.get('PAUSED', 0)}",
        f"✅ 已完成 {c.get('COMPLETED', 0)} · ⚠️ 失败 {c.get('FAILED', 0)}",
        "",
    ]
    if stats is not None:
        try:
            lines.append(f"⚡ ↓ {stats.download_speed_string()} · ↑ {stats.upload_speed_string()}")
        except Exception:
            pass
    try:
        disk = disk_usage_summary(settings.download_dir)
        lines.append(f"💾 剩余 {disk['free']}（已用 {disk['percent_used']}%）")
    except OSError:
        pass
    lines += [
        "",
        "💡 发送链接、磁力或种子文件即可开始下载",
        f"<i>更新于 {datetime.now().strftime('%H:%M:%S')}</i>",
    ]
    return "\n".join(lines)


def render_pending_card(kind: str, name: str, *, size: int | None = None, file_count: int | None = None) -> str:
    title = "📦 新下载任务" if kind != "magnet" else "🧲 磁力下载任务"
    safe_name = escape(name, quote=False)
    lines = [
        f"<b>{title}</b>",
        DIVIDER,
        f"📄 <code>{safe_name}</code>",
        f"📐 大小：{_fmt_size(size)}",
        f"🗂 文件数：{file_count if file_count is not None else '待解析'}",
        f"📂 <code>{escape(settings.download_dir, quote=False)}</code>",
        "",
        "确认无误后开始下载。",
    ]
    return "\n".join(lines)


def render_task_card(row, download=None, *, status: str | None = None) -> str:
    status = status or row["status"]
    name = row["file_name"] or getattr(download, "name", None) or row["source_ref"] or row["gid"]
    percent = getattr(download, "progress", 0.0) if download else 0.0
    completed = _safe_call(download, "completed_length_string", "0 B") if download else "0 B"
    total = _safe_call(download, "total_length_string", _fmt_size(row["file_size"])) if download else _fmt_size(row["file_size"])
    speed = _safe_call(download, "download_speed_string", "0 B/s") if download else "0 B/s"
    upload = _safe_call(download, "upload_speed_string", "0 B/s") if download else "0 B/s"
    connections = getattr(download, "connections", None) if download else None
    save_path = row["save_path"] or getattr(download, "dir", None) or settings.download_dir
    updated = datetime.now().strftime("%H:%M:%S")

    status_text = STATUS_LABEL.get(status, status)
    reason = _state_reason(download, status)
    safe_name = escape(str(name), quote=False)
    safe_reason = escape(str(reason), quote=False)
    safe_path = escape(str(save_path), quote=False)
    lines = [
        f"{_status_icon(status)} <b>{safe_name}</b>",
        DIVIDER,
        f"<code>{text_progress_bar(percent)}</code>  <b>{percent:.1f}%</b>",
        f"<code>{completed} / {total}</code>",
        "",
        f"{status_text} · {safe_reason}",
    ]
    if row["error"]:
        lines.append(f"❗ {escape(str(row['error']), quote=False)}")
    if status == "ACTIVE":
        lines.append(f"⚡ ↓ {speed} · ↑ {upload}")
        lines.append(f"⏱ 剩余 {_eta(download) if download else '未知'} · 🔗 {connections if connections is not None else '-'} 连接")
    lines.append(f"📂 <code>{safe_path}</code>")
    if row["gofile_link"]:
        lines.append(f"☁️ {escape(str(row['gofile_link']), quote=False)}")
    lines += ["", f"<code>#{row['id']}</code> · <i>{updated}</i>"]
    return "\n".join(lines)


def _fmt_limit(raw: str) -> str:
    """aria2's max-overall-download-limit: '0' means unlimited, otherwise bytes/s."""
    try:
        n = int(raw)
    except (TypeError, ValueError):
        return str(raw)
    return "不限速" if n == 0 else f"{_fmt_size(n)}/s"


def render_settings(limit_raw: str | None = None) -> str:
    limit = _fmt_limit(limit_raw) if limit_raw is not None else "未知"
    return (
        "⚙️ <b>设置</b>\n"
        f"{DIVIDER}\n"
        f"📂 默认目录：<code>{escape(settings.download_dir, quote=False)}</code>\n"
        f"🚀 全局限速：{limit}\n"
        f"🔢 最大同时下载：{settings.max_concurrent}\n"
        "🔔 完成通知：开启"
    )


def render_limit_chooser(limit_raw: str | None = None) -> str:
    current = _fmt_limit(limit_raw) if limit_raw is not None else "未知"
    return (
        "🚀 <b>全局下载限速</b>\n"
        f"{DIVIDER}\n"
        f"当前：{current}\n\n"
        "选择一个预设，立即生效："
    )


def html_escape(text: str) -> str:
    return escape(text, quote=False)
