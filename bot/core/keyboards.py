from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

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
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🚀 调整限速", callback_data="settings:limit")],
            [InlineKeyboardButton(text="⬅️ 返回主菜单", callback_data="nav:start")],
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


def text_progress_bar(percent: float, width: int = 12) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return "█" * filled + "░" * (width - filled)
