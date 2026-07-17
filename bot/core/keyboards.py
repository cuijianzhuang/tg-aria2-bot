from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.config import settings

STATUS_EMOJI = {
    "PENDING": "⏳",
    "ACTIVE": "⬇️",
    "PAUSED": "⏸",
    "COMPLETED": "✅",
    "FAILED": "⚠️",
    "CANCELLED": "🗑",
}

STATUS_LABEL = {
    "PENDING": "排队中",
    "ACTIVE": "下载中",
    "PAUSED": "已暂停",
    "COMPLETED": "已完成",
    "FAILED": "失败",
    "CANCELLED": "已取消",
}


def main_inline_keyboard(counts: dict[str, int] | None = None) -> InlineKeyboardMarkup:
    active = (counts or {}).get("ACTIVE", 0)
    active_label = f"⬇️ 下载中 {active}" if active else "⬇️ 下载中"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=active_label, callback_data="list:ACTIVE:0"),
                InlineKeyboardButton(text="📋 任务列表", callback_data="list:ALL:0"),
            ],
            [
                InlineKeyboardButton(text="⚙️ 设置", callback_data="nav:settings"),
                InlineKeyboardButton(text="🔄 刷新", callback_data="nav:start"),
            ],
        ]
    )


def pending_task_keyboard(token: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="▶️ 开始下载", callback_data=f"pending:start:{token}"),
                InlineKeyboardButton(text="❌ 取消", callback_data=f"pending:cancel:{token}"),
            ],
        ]
    )


def batch_pending_keyboard(batch_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="▶️ 全部开始", callback_data=f"pending:startall:{batch_id}"),
                InlineKeyboardButton(text="❌ 全部取消", callback_data=f"pending:cancelall:{batch_id}"),
            ],
        ]
    )


def _action_buttons(gid: str, status: str, prefix: str, label_prefix: str = "") -> list[list[InlineKeyboardButton]]:
    """Action buttons for one task, using the given callback_data prefix so the
    handler can tell single-task messages ("task:") apart from a /list row
    ("ltask:") — the latter needs to re-render the whole list on tap, not just
    edit its own row, since one Telegram message has one shared keyboard.
    label_prefix (e.g. "#3 ") disambiguates rows when several sit in one keyboard.
    """
    if status == "PENDING":
        rows = [
            [("ℹ️ 详情", "detail")],
            [("🗑 取消任务", "cancel")],
        ]
    elif status == "ACTIVE":
        rows = [
            [("⏸ 暂停", "pause"), ("ℹ️ 详情", "detail")],
            [("📂 位置", "files"), ("🚀 限速", "limit")],
            [("🗑 取消任务", "cancel")],
        ]
    elif status == "PAUSED":
        rows = [
            [("▶️ 继续", "resume"), ("ℹ️ 详情", "detail")],
            [("📂 位置", "files"), ("🗑 取消任务", "cancel")],
        ]
    elif status == "COMPLETED":
        rows = [
            [("📂 保存位置", "files"), ("🔗 获取链接", "link")],
            [("📤 发送到 TG", "sendtg")],
            [("🗑 删除记录", "delete")],
        ]
    elif status == "FAILED":
        rows = [
            [("🔄 重试", "retry"), ("ℹ️ 查看原因", "detail")],
            [("🗑 删除记录", "delete")],
        ]
    elif status == "CANCELLED":
        rows = [[("🗑 删除记录", "delete")]]
    else:
        return []
    return [
        [
            InlineKeyboardButton(text=f"{label_prefix}{label}", callback_data=f"{prefix}:{action}:{gid}")
            for label, action in row
        ]
        for row in rows
    ]


def task_keyboard(gid: str, status: str, *, with_back: bool = False) -> InlineKeyboardMarkup | None:
    """Buttons for a single task's own progress message; None if the status is
    unrecognized (there's always at least a delete option once terminal).
    with_back appends a 返回列表 row for cards opened from the task list."""
    rows = _action_buttons(gid, status, "task")
    if with_back:
        rows.append([InlineKeyboardButton(text="⬅️ 返回列表", callback_data="list:ALL:0")])
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def task_open_button(index: int, gid: str, name: str) -> list[InlineKeyboardButton]:
    text = f"{index}. {name[:30]}"
    return [InlineKeyboardButton(text=text, callback_data=f"task:open:{gid}")]


def file_selection_keyboard(gid: str, download) -> InlineKeyboardMarkup:
    """One row per real file (metadata entries filtered out), checkbox-style
    toggle button. Each tap applies immediately (pause/changeOption/resume
    happens synchronously in aria2_client), so there's no separate 应用 step —
    just a way back to the task card. Index is aria2's own 1-based file index."""
    rows = []
    for f in download.files:
        if f.is_metadata:
            continue
        box = "☑️" if f.selected else "⬜"
        name = f.path.name or str(f.path)
        label = f"{box} {name[:35]} ({f.length_string()})"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"filesel:{gid}:{f.index}")])
    rows.append([InlineKeyboardButton(text="✅ 完成", callback_data=f"task:detail:{gid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def redownload_keyboard(gid: str | None) -> InlineKeyboardMarkup | None:
    """Offered on the '已下载过' dedup reply so it isn't a dead end."""
    if not gid:
        return None
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="🔄 重新下载", callback_data=f"task:retry:{gid}")]]
    )


def task_cancel_confirm_keyboard(gid: str, *, destructive: bool = False) -> InlineKeyboardMarkup:
    if destructive:
        rows = [
            [InlineKeyboardButton(text="⚠️ 确认永久删除", callback_data=f"task:delete_files:{gid}")],
            [InlineKeyboardButton(text="⬅️ 返回任务", callback_data=f"task:detail:{gid}")],
        ]
    else:
        rows = [
            [InlineKeyboardButton(text="仅取消任务", callback_data=f"task:cancel_only:{gid}")],
            [InlineKeyboardButton(text="取消并删除文件", callback_data=f"task:confirm_delete_files:{gid}")],
            [InlineKeyboardButton(text="⬅️ 返回任务", callback_data=f"task:detail:{gid}")],
        ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


TAB_ORDER = ("ALL", "ACTIVE", "PENDING", "PAUSED", "COMPLETED", "FAILED")
TAB_ICON = {"ALL": "📚", "ACTIVE": "⬇️", "PENDING": "⏳", "PAUSED": "⏸", "COMPLETED": "✅", "FAILED": "⚠️"}


def list_tab_row(selected: str, counts: dict[str, int]) -> list[InlineKeyboardButton]:
    """Segmented-control style filter tabs shown atop the task list; the active
    tab is bracketed since Telegram buttons can't be styled."""
    row = []
    for key in TAB_ORDER:
        n = sum(counts.values()) if key == "ALL" else counts.get(key, 0)
        label = f"{TAB_ICON[key]}{n}"
        if key == selected:
            label = f"·{label}·"
        row.append(InlineKeyboardButton(text=label, callback_data=f"list:{key}:0"))
    return row


def cleanup_confirm_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⚠️ 确认清理", callback_data="list:cleanup_yes:0")],
            [InlineKeyboardButton(text="↩️ 返回列表", callback_data="list:ALL:0")],
        ]
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    """Single hub: download tuning on top, admin features (formerly /admin) below."""
    notify_label = f"🔔 完成通知: {'✅' if settings.notify_on_complete else '❌'}"
    send_tg_label = f"📤 自动发送: {'✅' if settings.auto_send_to_tg else '❌'}"
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="🚀 调整限速", callback_data="settings:limit"),
                InlineKeyboardButton(text="🔢 同时下载数", callback_data="settings:concurrent"),
            ],
            [
                InlineKeyboardButton(text="📏 单文件上限", callback_data="settings:maxsize"),
                InlineKeyboardButton(text="📂 下载目录", callback_data="settings:dir"),
            ],
            [
                InlineKeyboardButton(text=notify_label, callback_data="settings:notify"),
                InlineKeyboardButton(text="🧹 自动清理", callback_data="settings:cleanup"),
            ],
            [InlineKeyboardButton(text=send_tg_label, callback_data="settings:sendtg")],
            [
                InlineKeyboardButton(text="👥 白名单", callback_data="admin:users"),
                InlineKeyboardButton(text="☁️ GoFile", callback_data="admin:gofile"),
            ],
            [
                InlineKeyboardButton(text="📁 rclone", callback_data="admin:rclone"),
                InlineKeyboardButton(text="🔄 重启服务", callback_data="admin:restart"),
            ],
            [InlineKeyboardButton(text="🖥 服务器状态", callback_data="admin:sysinfo")],
            [InlineKeyboardButton(text="⬅️ 返回主菜单", callback_data="nav:start")],
        ]
    )


def server_status_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔄 刷新", callback_data="admin:sysinfo")],
            [InlineKeyboardButton(text="⬅️ 返回设置", callback_data="nav:settings")],
        ]
    )


LIMIT_PRESETS = (
    ("🚫 不限速", "0"),
    ("1 MiB/s", "1M"),
    ("2 MiB/s", "2M"),
    ("5 MiB/s", "5M"),
    ("10 MiB/s", "10M"),
)


def limit_chooser_keyboard() -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"setlimit:{value}")]
        for label, value in LIMIT_PRESETS
    ]
    rows.append([InlineKeyboardButton(text="⬅️ 返回设置", callback_data="nav:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


CONCURRENT_PRESETS = ("1", "2", "3", "5", "8", "10")


def concurrent_chooser_keyboard(current: str | None = None) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(
            text=f"·{n}·" if n == current else n,
            callback_data=f"setconcurrent:{n}",
        )
        for n in CONCURRENT_PRESETS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(text="⬅️ 返回设置", callback_data="nav:settings")],
    ])


# MB 转字节时用 1024*1024（跟 _fmt_size 的 MiB 单位保持一致），"0" 约定为不限
MAXSIZE_PRESETS = (
    ("不限", "0"),
    ("512 MB", "512"),
    ("1 GB", "1024"),
    ("2 GB", "2048"),
    ("5 GB", "5120"),
    ("10 GB", "10240"),
)


def maxsize_chooser_keyboard(current_mb: str | None = None) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(
            text=f"·{label}·" if value == current_mb else label,
            callback_data=f"setmaxsize:{value}",
        )]
        for label, value in MAXSIZE_PRESETS
    ]
    rows.append([InlineKeyboardButton(text="⬅️ 返回设置", callback_data="nav:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


CLEANUP_PRESETS = (
    ("关闭", "0"),
    ("3 天", "3"),
    ("7 天", "7"),
    ("14 天", "14"),
    ("30 天", "30"),
)


def cleanup_chooser_keyboard(current_days: int) -> InlineKeyboardMarkup:
    current = str(current_days)
    row = [
        InlineKeyboardButton(
            text=f"·{label}·" if value == current else label,
            callback_data=f"setcleanup:{value}",
        )
        for label, value in CLEANUP_PRESETS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(text="⬅️ 返回设置", callback_data="nav:settings")],
    ])


def task_limit_chooser_keyboard(gid: str, current: str | None = None) -> InlineKeyboardMarkup:
    """跟全局限速用同一套预设，callback_data 里带 gid 区分是哪个任务。"""
    rows = [
        [InlineKeyboardButton(
            text=f"·{label}·" if value == current else label,
            callback_data=f"tasklimit:{gid}:{value}",
        )]
        for label, value in LIMIT_PRESETS
    ]
    rows.append([InlineKeyboardButton(text="⬅️ 返回任务", callback_data=f"task:detail:{gid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


PERIOD_PRESETS = (
    ("24 小时", "1"),
    ("7 天", "7"),
    ("30 天", "30"),
    ("全部", "0"),
)


def stats_period_keyboard(current_days: str) -> InlineKeyboardMarkup:
    row = [
        InlineKeyboardButton(
            text=f"·{label}·" if value == current_days else label,
            callback_data=f"stats:{value}",
        )
        for label, value in PERIOD_PRESETS
    ]
    return InlineKeyboardMarkup(inline_keyboard=[
        row,
        [InlineKeyboardButton(text="⬅️ 主菜单", callback_data="nav:start")],
    ])


def dir_chooser_keyboard(options: list[str]) -> InlineKeyboardMarkup:
    """按 index 而不是路径本身做 callback_data —— 路径可能带非法字符或超长，
    索引更安全也更短。"""
    current = settings.download_dir
    rows = []
    for i, path in enumerate(options):
        label = f"✅ {path}" if path == current else path
        rows.append([InlineKeyboardButton(text=label[:60], callback_data=f"setdir:{i}")])
    rows.append([InlineKeyboardButton(text="⬅️ 返回设置", callback_data="nav:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def text_progress_bar(percent: float, width: int = 12) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return "█" * filled + "░" * (width - filled)
