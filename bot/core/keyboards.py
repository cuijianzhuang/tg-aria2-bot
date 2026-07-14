from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

STATUS_EMOJI = {
    "PENDING": "⏳",
    "ACTIVE": "⬇️",
    "PAUSED": "⏸",
    "COMPLETED": "✅",
    "FAILED": "❌",
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
                InlineKeyboardButton(text="📋 任务列表", callback_data="list:overview"),
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


def task_keyboard(gid: str, status: str) -> InlineKeyboardMarkup | None:
    """Buttons for a single task's own progress message; None if the status is
    unrecognized (there's always at least a delete option once terminal)."""
    rows = _action_buttons(gid, status, "task")
    return InlineKeyboardMarkup(inline_keyboard=rows) if rows else None


def task_open_button(index: int, gid: str, name: str) -> list[InlineKeyboardButton]:
    text = f"{index}. {name[:30]}"
    return [InlineKeyboardButton(text=text, callback_data=f"task:detail:{gid}")]


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


def task_list_filter_keyboard(counts: dict[str, int]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text=f"⬇️ 下载中 {counts.get('ACTIVE', 0)}", callback_data="list:ACTIVE:0"),
                InlineKeyboardButton(text=f"⏳ 等待中 {counts.get('PENDING', 0)}", callback_data="list:PENDING:0"),
            ],
            [
                InlineKeyboardButton(text=f"✅ 已完成 {counts.get('COMPLETED', 0)}", callback_data="list:COMPLETED:0"),
                InlineKeyboardButton(text=f"⚠️ 失败 {counts.get('FAILED', 0)}", callback_data="list:FAILED:0"),
            ],
            [
                InlineKeyboardButton(text=f"⏸ 已暂停 {counts.get('PAUSED', 0)}", callback_data="list:PAUSED:0"),
                InlineKeyboardButton(text="📚 全部任务", callback_data="list:ALL:0"),
            ],
            [InlineKeyboardButton(text="🧹 清理已完成", callback_data="list:cleanup")],
            [InlineKeyboardButton(text="⬅️ 返回主菜单", callback_data="nav:start")],
        ]
    )


def settings_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="⬇️ 下载限速", callback_data="settings:download_limit"),
                InlineKeyboardButton(text="📂 默认目录", callback_data="settings:dir"),
            ],
            [InlineKeyboardButton(text="⬅️ 返回主菜单", callback_data="nav:start")],
        ]
    )


def text_progress_bar(percent: float, width: int = 12) -> str:
    filled = max(0, min(width, round(percent / 100 * width)))
    return "█" * filled + "░" * (width - filled)
