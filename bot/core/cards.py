from __future__ import annotations

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


def render_pending_card(
    kind: str, name: str, *,
    size: int | None = None, file_count: int | None = None,
    node_label: str | None = None, download_dir: str | None = None,
) -> str:
    """node_label 仅在多节点部署时传入（单节点不显示，界面保持简洁）；
    download_dir 是目标节点的目录（远程节点与本机不同），不传退回本机配置。"""
    title = "📦 新下载任务" if kind != "magnet" else "🧲 磁力下载任务"
    safe_name = escape(name, quote=False)
    lines = [
        f"<b>{title}</b>",
        DIVIDER,
        f"📄 <code>{safe_name}</code>",
        f"📐 大小：{_fmt_size(size)}",
        f"🗂 文件数：{file_count if file_count is not None else '待解析'}",
        f"📂 <code>{escape(download_dir or settings.download_dir, quote=False)}</code>",
    ]
    if node_label:
        lines.append(f"📍 节点：{escape(node_label, quote=False)}")
    lines += ["", "确认无误后开始下载。"]
    return "\n".join(lines)


def render_task_card(row, download=None, *, status: str | None = None, node_label: str | None = None) -> str:
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
    if node_label:
        lines.append(f"📍 节点：{escape(node_label, quote=False)}")
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


def _fmt_max_file_size(n: int) -> str:
    # 0 约定为不限制单文件大小
    return "不限" if not n else _fmt_size(n)


def render_settings(limit_raw: str | None = None, concurrent_raw: str | None = None) -> str:
    limit = _fmt_limit(limit_raw) if limit_raw is not None else "未知"
    concurrent = concurrent_raw if concurrent_raw is not None else str(settings.max_concurrent)
    notify = "开启" if settings.notify_on_complete else "关闭"
    cleanup = f"保留 {settings.auto_cleanup_days} 天" if settings.auto_cleanup_days > 0 else "关闭"
    send_tg = "开启" if settings.auto_send_to_tg else "关闭"
    return (
        "⚙️ <b>设置</b>\n"
        f"{DIVIDER}\n"
        f"📂 默认目录：<code>{escape(settings.download_dir, quote=False)}</code>\n"
        f"🚀 全局限速：{limit}\n"
        f"🔢 最大同时下载：{concurrent}\n"
        f"📏 单文件上限：{_fmt_max_file_size(settings.max_file_size)}\n"
        f"🔔 完成通知：{notify}\n"
        f"🧹 自动清理已完成：{cleanup}\n"
        f"📤 自动发送到 TG：{send_tg}"
    )


def render_file_selection(download) -> str:
    files = [f for f in download.files if not f.is_metadata]
    selected_n = sum(1 for f in files if f.selected)
    lines = [
        "🗂 <b>选择要下载的文件</b>",
        DIVIDER,
        f"已选 {selected_n}/{len(files)} 个文件",
        "",
        "点击文件切换选中/取消，即点即生效（会短暂暂停任务）。",
    ]
    return "\n".join(lines)


def render_concurrent_chooser(current: str | None = None) -> str:
    return (
        "🔢 <b>最大同时下载数</b>\n"
        f"{DIVIDER}\n"
        f"当前：{current if current is not None else '未知'}\n\n"
        "选择一个数量，立即生效："
    )


def render_maxsize_chooser(current: int) -> str:
    return (
        "📏 <b>单文件大小上限</b>\n"
        f"{DIVIDER}\n"
        f"当前：{_fmt_max_file_size(current)}\n\n"
        "超过上限的文件会被直接拒绝，不会开始下载。选择一个上限，立即生效："
    )


def render_cleanup_chooser() -> str:
    current = f"保留 {settings.auto_cleanup_days} 天" if settings.auto_cleanup_days > 0 else "关闭"
    return (
        "🧹 <b>自动清理已完成任务</b>\n"
        f"{DIVIDER}\n"
        f"当前：{current}\n\n"
        "到期后自动删除机器人里的任务记录（只删记录，不删磁盘文件）。选择保留天数："
    )


def render_dir_chooser(options: list[str]) -> str:
    current = settings.download_dir
    lines = [
        "📂 <b>下载目录</b>",
        DIVIDER,
        f"当前：<code>{escape(current, quote=False)}</code>",
        "",
        "选择要切换到的目录（不影响已下载的文件位置）：",
    ]
    if len(options) <= 1:
        lines += ["", "<i>只有一个目录可选，在 .env 里配置 DOWNLOAD_DIR_PRESETS 增加更多候选目录。</i>"]
    return "\n".join(lines)


def render_limit_chooser(limit_raw: str | None = None) -> str:
    current = _fmt_limit(limit_raw) if limit_raw is not None else "未知"
    return (
        "🚀 <b>全局下载限速</b>\n"
        f"{DIVIDER}\n"
        f"当前：{current}\n\n"
        "选择一个预设，立即生效："
    )


def render_task_limit_chooser(name: str, limit_raw: str | None = None) -> str:
    current = _fmt_limit(limit_raw) if limit_raw is not None else "未知"
    return (
        "🚀 <b>单任务限速</b>\n"
        f"{DIVIDER}\n"
        f"任务：{escape(str(name), quote=False)}\n"
        f"当前：{current}\n\n"
        "只影响这一个任务，不影响全局限速。选择一个预设，立即生效："
    )


def render_batch_pending(names: list[str], duplicate_count: int, overflow_count: int) -> str:
    """一条消息里贴了多条链接/磁力时的汇总确认卡片。"""
    lines = [f"📦 <b>批量任务</b>（{len(names)} 个待确认）", DIVIDER]
    for i, name in enumerate(names, start=1):
        lines.append(f"{i}. {escape(str(name), quote=False)}")
    if duplicate_count:
        lines.append(f"\n<i>已跳过 {duplicate_count} 个之前下载过的链接。</i>")
    if overflow_count:
        lines.append(f"<i>超出单次批量上限，还有 {overflow_count} 条未处理，请分批发送。</i>")
    lines.append("\n确认无误后一键开始，或取消整批。")
    return "\n".join(lines)


def _fmt_speed(bytes_per_sec: float) -> str:
    return f"{_fmt_size(int(bytes_per_sec))}/s"


def render_server_status(info: dict, stats=None) -> str:
    """服务器状态 page: host metrics from sysinfo.collect_system_status plus
    aria2's global stat (None degrades to omitting the aria2 line)."""
    from bot.core.sysinfo import format_uptime  # local import to avoid a cycle

    load1, load5, load15 = info["load_avg"]
    lines = [
        "🖥 <b>服务器状态</b>",
        DIVIDER,
        f"⏱ 已运行 {format_uptime(info['uptime_seconds'])}",
        f"🧮 CPU {info['cpu_percent']}%（{info['cpu_count']} 核）",
        f"📊 负载 {load1:.2f} / {load5:.2f} / {load15:.2f}",
        f"🧠 内存 {_fmt_size(info['mem_used'])} / {_fmt_size(info['mem_total'])}（{info['mem_percent']}%）",
    ]
    if info.get("swap_total"):
        lines.append(f"💱 交换 {_fmt_size(info['swap_used'])} / {_fmt_size(info['swap_total'])}")

    disk = info.get("disk")
    if disk:
        lines.append(f"💾 磁盘 已用 {disk['used']} / {disk['total']}（{disk['percent_used']}%）· 剩余 {disk['free']}")

    lines.append(
        f"📶 网络 ↓ {_fmt_speed(info['net_rx_speed'])} · ↑ {_fmt_speed(info['net_tx_speed'])}"
        f"\n　　 累计 ↓ {_fmt_size(info['net_rx_total'])} · ↑ {_fmt_size(info['net_tx_total'])}"
    )
    if info.get("bot_rss"):
        lines.append(f"🤖 机器人内存 {_fmt_size(info['bot_rss'])}")

    if stats is not None:
        try:
            lines.append(
                f"⚡ aria2 ↓ {stats.download_speed_string()} · ↑ {stats.upload_speed_string()}"
                f" · 活动 {stats.num_active} · 等待 {stats.num_waiting}"
            )
        except Exception:
            pass

    lines += ["", f"<i>更新于 {datetime.now().strftime('%H:%M:%S')}</i>"]
    return "\n".join(lines)


def render_node_chooser(current: str, nodes: list, healthy: dict[str, bool]) -> str:
    """nodes 是 node_pool.Node 列表；healthy 是名字->健康状态的缓存快照。"""
    lines = ["🖥 <b>选择下载节点</b>", DIVIDER, "新任务会下载到你选中的节点。", ""]
    for node in nodes:
        dot = "🟢" if healthy.get(node.name, True) else "🔴"
        marker = " ←当前" if node.name == current else ""
        lines.append(f"{dot} {escape(node.display_name, quote=False)}{marker}")
    return "\n".join(lines)


def render_node_manage(nodes: list, healthy: dict[str, bool]) -> str:
    lines = ["🖥 <b>节点管理</b>", DIVIDER]
    for node in nodes:
        dot = "🟢" if healthy.get(node.name, True) else "🔴"
        state = "" if node.enabled else "（已停用）"
        kind = "本机" if node.is_local else "远程"
        lines.append(f"{dot} <b>{escape(node.display_name, quote=False)}</b>{state} · {kind}")
        # rpc_url 里可能带内网地址，管理页只对管理员可见，直接展示便于排错
        lines.append(f"　<code>{escape(node.rpc_url, quote=False)}</code> → <code>{escape(node.download_dir, quote=False)}</code>")
    lines += [
        "",
        "添加节点：<code>/addnode 名称 rpc地址 密钥 [下载目录]</code>",
        "<i>例：/addnode 群晖 http://192.168.1.5:6800/jsonrpc s3cret /volume1/downloads</i>",
    ]
    return "\n".join(lines)


def render_stats(period_label: str, stats: dict) -> str:
    total = stats["total"]
    completed = stats["completed"]
    failed = stats["failed"]
    cancelled = stats["cancelled"]
    finished = completed + failed + cancelled  # 已有结果的任务数（不含还在进行中的）
    success_rate = f"{completed / finished * 100:.1f}%" if finished else "暂无数据"
    lines = [
        f"📊 <b>下载统计</b>　{period_label}",
        DIVIDER,
        f"📥 新增任务：{total}",
        f"✅ 完成 {completed} · ⚠️ 失败 {failed} · 🗑 取消 {cancelled}",
        f"🎯 成功率：{success_rate}",
        f"💾 已下载总量：{_fmt_size(stats['total_bytes'])}",
    ]
    if stats["total_bytes"] == 0 and completed:
        lines.append("<i>部分任务（如种子/磁力）未记录原始大小，总量可能偏低。</i>")
    return "\n".join(lines)


def html_escape(text: str) -> str:
    return escape(text, quote=False)
