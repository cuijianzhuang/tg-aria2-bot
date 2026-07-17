from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.core.keyboards import STATUS_EMOJI, STATUS_LABEL, list_tab_row, task_open_button

LIST_LIMIT = 8
LIST_STATUS_MAP = {
    "ALL": None,
    "ACTIVE": "ACTIVE",
    "PENDING": "PENDING",
    "PAUSED": "PAUSED",
    "COMPLETED": "COMPLETED",
    "FAILED": "FAILED",
    "CANCELLED": "CANCELLED",
}

TITLES = {
    "ALL": "📚 全部任务",
    "ACTIVE": "⬇️ 下载中",
    "PENDING": "⏳ 等待中",
    "PAUSED": "⏸ 已暂停",
    "COMPLETED": "✅ 已完成",
    "FAILED": "⚠️ 失败",
    "CANCELLED": "🗑 已取消",
}


async def render_task_list(repo, aria2, status_key: str = "ALL", page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
    """One-page task browser: filter tabs on top (segmented-control style),
    task rows as buttons, pagination + bulk actions + footer below."""
    counts = await repo.count_by_status()
    status = LIST_STATUS_MAP.get(status_key)
    page = max(0, page)
    offset = page * LIST_LIMIT
    # 多取 1 条来判断"是否还有下一页"，避免任务数恰好是 LIST_LIMIT 整数倍时
    # 显示一个空白的下一页（旧逻辑用 len(rows)==LIST_LIMIT 判断，这种情况会误判）
    fetched = await repo.list_recent(LIST_LIMIT + 1, offset=offset, status=status)
    has_next_page = len(fetched) > LIST_LIMIT
    rows = fetched[:LIST_LIMIT]

    keyboard_rows = [list_tab_row(status_key, counts)]
    title = TITLES.get(status_key, TITLES["ALL"])
    lines = [f"<b>{title}</b>"]

    if not rows:
        lines += ["暂无任务。"]
    else:
        active_gids = [r["gid"] for r in rows if r["status"] == "ACTIVE" and r["gid"]]
        progress = await aria2.get_progress_map() if active_gids else {}

        for index, row in enumerate(rows, start=offset + 1):
            name = row["file_name"] or row["source_ref"] or row["gid"] or "未命名任务"
            icon = STATUS_EMOJI.get(row["status"], "•")
            label = STATUS_LABEL.get(row["status"], row["status"])
            sub = f"{icon} {label}"
            p = progress.get(row["gid"]) if row["gid"] and row["status"] == "ACTIVE" else None
            if p:
                sub += f" · {p['percent']}% · {p['speed']}"
            elif row["status"] == "FAILED" and row["error"]:
                sub += f" · {escape(row['error'])}"
            lines.append(f"<b>{index}.</b> {escape(name)}\n{sub}")
            if row["gid"]:
                keyboard_rows.append(task_open_button(index, row["gid"], name))

        if page > 0 or has_next_page:
            controls = []
            if page > 0:
                controls.append(InlineKeyboardButton(text="⬅️", callback_data=f"list:{status_key}:{page - 1}"))
            controls.append(InlineKeyboardButton(text=f"第 {page + 1} 页", callback_data="list:noop"))
            if has_next_page:
                controls.append(InlineKeyboardButton(text="➡️", callback_data=f"list:{status_key}:{page + 1}"))
            keyboard_rows.append(controls)

        if status_key == "ACTIVE":
            keyboard_rows.append(
                [
                    InlineKeyboardButton(text="⏸ 全部暂停", callback_data="bulk:pause:ACTIVE"),
                    InlineKeyboardButton(text="▶️ 全部继续", callback_data="bulk:resume:PAUSED"),
                ]
            )

    footer = [InlineKeyboardButton(text="⬅️ 主菜单", callback_data="nav:start")]
    if counts.get("COMPLETED", 0):
        footer.insert(0, InlineKeyboardButton(text="🧹 清理已完成", callback_data="list:cleanup:0"))
    keyboard_rows.append(footer)

    return "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
