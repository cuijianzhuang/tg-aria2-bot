from html import escape

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.core.keyboards import STATUS_LABEL, task_list_filter_keyboard, task_open_button

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


async def render_task_overview(repo) -> tuple[str, InlineKeyboardMarkup]:
    counts = await repo.count_by_status()
    recent = await repo.list_recent(5)
    lines = [
        "<b>📋 下载任务</b>",
        "",
        f"⬇️ 下载中 {counts.get('ACTIVE', 0)}   ⏳ 等待 {counts.get('PENDING', 0)}   ⏸ 暂停 {counts.get('PAUSED', 0)}",
        f"✅ 完成 {counts.get('COMPLETED', 0)}   ⚠️ 失败 {counts.get('FAILED', 0)}",
    ]
    if recent:
        lines += ["", "<b>最近任务</b>"]
        for row in recent:
            name = escape(row["file_name"] or row["source_ref"] or row["gid"] or "未命名任务")
            label = STATUS_LABEL.get(row["status"], row["status"])
            lines.append(f"<code>#{row['id']}</code> {name}\n{label}")
    else:
        lines += ["", "暂无任务记录。"]
    text = "\n".join(lines)
    return text, task_list_filter_keyboard(counts)


async def render_task_list(repo, aria2, status_key: str = "ALL", page: int = 0) -> tuple[str, InlineKeyboardMarkup | None]:
    status = LIST_STATUS_MAP.get(status_key, None)
    offset = max(0, page) * LIST_LIMIT
    rows = await repo.list_recent(LIST_LIMIT, offset=offset, status=status)
    if not rows:
        title = STATUS_LABEL.get(status_key, "全部任务") if status_key != "ALL" else "全部任务"
        return f"📋 {title}\n\n暂无任务。", _back_keyboard()

    active_gids = [r["gid"] for r in rows if r["status"] == "ACTIVE" and r["gid"]]
    progress = await aria2.get_progress_map() if active_gids else {}

    title = escape(_title_for(status_key))
    lines = [title, ""]
    keyboard_rows = []
    for index, row in enumerate(rows, start=offset + 1):
        name = row["file_name"] or row["source_ref"] or row["gid"] or "未命名任务"
        label = STATUS_LABEL.get(row["status"], row["status"])
        line = f"{index}. {escape(name)}\n   {label}"
        p = progress.get(row["gid"]) if row["gid"] and row["status"] == "ACTIVE" else None
        if p:
            line += f" · {p['percent']}% · {p['speed']}"
        elif row["status"] == "FAILED" and row["error"]:
            line += f" · {escape(row['error'])}"
        lines.append(line)
        if row["gid"]:
            keyboard_rows.append(task_open_button(index, row["gid"], name))

    controls = []
    if page > 0:
        controls.append(InlineKeyboardButton(text="⬅️ 上一页", callback_data=f"list:{status_key}:{page - 1}"))
    controls.append(InlineKeyboardButton(text=f"{page + 1}", callback_data="list:noop"))
    if len(rows) == LIST_LIMIT:
        controls.append(InlineKeyboardButton(text="下一页 ➡️", callback_data=f"list:{status_key}:{page + 1}"))
    keyboard_rows.append(controls)
    keyboard_rows.append([InlineKeyboardButton(text="⬅️ 返回", callback_data="list:overview")])

    if status_key == "ACTIVE":
        keyboard_rows.append(
            [
                InlineKeyboardButton(text="⏸ 全部暂停", callback_data="bulk:pause:ACTIVE"),
                InlineKeyboardButton(text="▶️ 全部继续", callback_data="bulk:resume:PAUSED"),
            ]
        )

    return "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard_rows)


def _title_for(status_key: str) -> str:
    if status_key == "ACTIVE":
        return "⬇️ 下载中的任务"
    if status_key == "PENDING":
        return "⏳ 等待中的任务"
    if status_key == "PAUSED":
        return "⏸ 已暂停的任务"
    if status_key == "COMPLETED":
        return "✅ 已完成的任务"
    if status_key == "FAILED":
        return "⚠️ 失败的任务"
    return "📋 全部任务"


def _back_keyboard() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[[InlineKeyboardButton(text="⬅️ 返回", callback_data="list:overview")]]
    )
