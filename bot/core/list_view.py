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


async def _progress_for(nodes, rows) -> dict:
    """页面上 ACTIVE 行的实时进度。多节点时按行归属分别去各节点取，某个节点
    连不上就只缺它那部分进度（显示为无进度），不拖垮整页渲染。"""
    node_names = {r["node"] for r in rows if r["status"] == "ACTIVE" and r["gid"]}
    progress: dict = {}
    for name in node_names:
        try:
            progress.update(await nodes.get(name).get_progress_map())
        except Exception:
            pass
    return progress


def _row_node_suffix(nodes, row) -> str:
    label = nodes.label(row["node"])
    return f" · 📍{escape(label)}" if label else ""


async def render_task_list(repo, nodes, status_key: str = "ALL", page: int = 0) -> tuple[str, InlineKeyboardMarkup]:
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
        progress = await _progress_for(nodes, rows)

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
            sub += _row_node_suffix(nodes, row)
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


SEARCH_LIMIT = 15


async def render_search_results(repo, nodes, keyword: str) -> tuple[str, InlineKeyboardMarkup]:
    """/find 关键词的结果页。不做分页 —— 结果数上限较高（15 条），关键词不够
    精确时提示缩小范围，比再实现一套搜索分页要划算。搜索用的 callback_data
    没法直接塞中文关键词进去（64 字节很快超），所以干脆不留"下一页"入口。"""
    fetched = await repo.search_tasks(keyword, limit=SEARCH_LIMIT)
    truncated = len(fetched) > SEARCH_LIMIT
    rows = fetched[:SEARCH_LIMIT]

    safe_keyword = escape(keyword)
    lines = [f"🔍 <b>搜索「{safe_keyword}」</b>"]
    keyboard_rows: list[list[InlineKeyboardButton]] = []

    if not rows:
        lines.append("没有找到匹配的任务。")
    else:
        progress = await _progress_for(nodes, rows)

        for index, row in enumerate(rows, start=1):
            name = row["file_name"] or row["source_ref"] or row["gid"] or "未命名任务"
            icon = STATUS_EMOJI.get(row["status"], "•")
            label = STATUS_LABEL.get(row["status"], row["status"])
            sub = f"{icon} {label}"
            p = progress.get(row["gid"]) if row["gid"] and row["status"] == "ACTIVE" else None
            if p:
                sub += f" · {p['percent']}% · {p['speed']}"
            sub += _row_node_suffix(nodes, row)
            lines.append(f"<b>{index}.</b> {escape(name)}\n{sub}")
            if row["gid"]:
                keyboard_rows.append(task_open_button(index, row["gid"], name))

        if truncated:
            lines.append(f"\n<i>结果超过 {SEARCH_LIMIT} 条，只显示最新的部分，请用更精确的关键词缩小范围。</i>")

    keyboard_rows.append([InlineKeyboardButton(text="⬅️ 主菜单", callback_data="nav:start")])
    return "\n\n".join(lines), InlineKeyboardMarkup(inline_keyboard=keyboard_rows)
