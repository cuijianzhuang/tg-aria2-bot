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


# 从任务列表打开任务卡片时附在卡片底部的返回按钮文案。callbacks.py 靠这段
# 文案 + "list:" 前缀从当前消息里认出它，在任务卡片的各级子菜单（限速、取消
# 确认、删除确认……）之间一路带着走，返回时回到原来的 tab 和页码
BACK_TO_LIST_TEXT = "⬅️ 返回列表"


def _chunk(buttons: list[InlineKeyboardButton], size: int) -> list[list[InlineKeyboardButton]]:
    return [buttons[i:i + size] for i in range(0, len(buttons), size)]


def _mark(label: str, selected: bool) -> str:
    """选择器里标记当前值（Telegram 按钮没法加样式，只能改文字）。"""
    return f"·{label}·" if selected else label


def main_inline_keyboard(
    counts: dict[str, int] | None = None, *, node_label: str | None = None, is_admin: bool = True,
) -> InlineKeyboardMarkup:
    """node_label 仅多节点部署时传入（当前节点显示名），单节点不显示该行。
    is_admin=False 时不显示「设置」——那里全是全局配置，非管理员点了也只会被拒。"""
    c = counts or {}
    active = c.get("ACTIVE", 0)
    first = [
        InlineKeyboardButton(text=f"⬇️ 下载中 {active}" if active else "⬇️ 下载中", callback_data="list:ACTIVE:0"),
        InlineKeyboardButton(text="📋 任务列表", callback_data="list:ALL:0"),
    ]
    # 有失败任务时给个直达入口，不用进列表再切 tab
    if c.get("FAILED", 0):
        first.append(InlineKeyboardButton(text=f"⚠️ 失败 {c['FAILED']}", callback_data="list:FAILED:0"))
    rows = [first]
    if node_label:
        rows.append([InlineKeyboardButton(text=f"🖥 节点: {node_label} ▾", callback_data="node:pick")])
    bottom = [InlineKeyboardButton(text="📊 统计", callback_data="stats:7")]
    if is_admin:
        bottom.append(InlineKeyboardButton(text="⚙️ 设置", callback_data="nav:settings"))
    bottom.append(InlineKeyboardButton(text="🔄 刷新", callback_data="nav:start"))
    rows.append(bottom)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def node_chooser_keyboard(current: str, nodes: list, healthy: dict[str, bool]) -> InlineKeyboardMarkup:
    """全局切换当前节点。callback 用节点名（/addnode 已限制名字字节长度，
    拼进 64 字节的 callback_data 不会超）。"""
    rows = []
    for node in nodes:
        dot = "🟢" if healthy.get(node.name, True) else "🔴"
        label = f"·{dot} {node.display_name}·" if node.name == current else f"{dot} {node.display_name}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"node:use:{node.name}")])
    rows.append([InlineKeyboardButton(text="⬅️ 主菜单", callback_data="nav:start")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pending_node_chooser_keyboard(token: str, current: str, nodes: list, healthy: dict[str, bool]) -> InlineKeyboardMarkup:
    """确认卡片上的临时切换：只改这一条待确认任务的目标节点，不动用户全局偏好。"""
    rows = []
    for node in nodes:
        dot = "🟢" if healthy.get(node.name, True) else "🔴"
        label = f"·{dot} {node.display_name}·" if node.name == current else f"{dot} {node.display_name}"
        rows.append([InlineKeyboardButton(text=label, callback_data=f"pnode:{token}:{node.name}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def node_manage_keyboard(nodes: list) -> InlineKeyboardMarkup:
    """管理员的节点管理页：default 不给停用/删除按钮（它来自 .env）。"""
    rows = []
    for node in nodes:
        if node.name == "default":
            continue
        toggle = "▶️ 启用" if not node.enabled else "⏸ 停用"
        rows.append([
            InlineKeyboardButton(text=f"{toggle} {node.display_name}", callback_data=f"admin:node:t:{node.name}"),
            InlineKeyboardButton(text=f"🗑 删除 {node.display_name}", callback_data=f"admin:node:d:{node.name}"),
        ])
    rows.append([InlineKeyboardButton(text="⬅️ 返回设置", callback_data="nav:settings")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def pending_task_keyboard(token: str, *, show_node_switch: bool = False) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="▶️ 开始下载", callback_data=f"pending:start:{token}"),
            InlineKeyboardButton(text="❌ 取消", callback_data=f"pending:cancel:{token}"),
        ],
    ]
    # 多节点部署才显示：发之前临时改目标节点，不用先回主菜单切全局偏好
    if show_node_switch:
        rows.append([InlineKeyboardButton(text="🖥 切换节点", callback_data=f"pending:nodes:{token}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def batch_pending_keyboard(batch_id: str) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="▶️ 全部开始", callback_data=f"pending:startall:{batch_id}"),
                InlineKeyboardButton(text="❌ 全部取消", callback_data=f"pending:cancelall:{batch_id}"),
            ],
        ]
    )


def _action_buttons(
    gid: str, status: str, *, local: bool = True, multi_file: bool | None = None,
) -> list[list[InlineKeyboardButton]]:
    """单个任务卡片上的操作按钮，按状态给不同组合。

    - 卡片本身就是详情页，原来的「ℹ️ 详情」实际作用是刷新，直接叫「🔄 刷新」
    - 「🗂 选择文件」只对多文件任务有意义；multi_file=False（明确知道是单文件）
      时不显示，None（不知道，比如磁力还在抓元数据）时照常显示
    - 远程节点（local=False）不显示依赖本机文件系统的「发送到 TG」
    """
    refresh = ("🔄 刷新", "detail")
    pick_files = ("🗂 选择文件", "files") if multi_file is not False else None
    if status == "PENDING":
        rows = [[refresh, ("🗑 取消任务", "cancel")]]
    elif status == "ACTIVE":
        rows = [
            [("⏸ 暂停", "pause"), refresh],
            [b for b in (pick_files, ("🚀 限速", "limit")) if b],
            [("🗑 取消任务", "cancel")],
        ]
    elif status == "PAUSED":
        rows = [
            [("▶️ 继续", "resume"), refresh],
            [b for b in (pick_files, ("🗑 取消任务", "cancel")) if b],
        ]
    elif status == "COMPLETED":
        rows = [
            [b for b in (("📂 保存位置", "files"), ("📤 发送到 TG", "sendtg") if local else None) if b],
            [("🗑 删除", "delete")],
        ]
    elif status == "FAILED":
        # 失败原因已经写在卡片正文里了，不再单独占一个按钮
        rows = [[("🔄 重试", "retry"), ("🗑 删除", "delete")]]
    elif status == "CANCELLED":
        rows = [[("🔄 重新下载", "retry"), ("🗑 删除记录", "delete")]]
    else:
        return []
    return [
        [InlineKeyboardButton(text=label, callback_data=f"task:{action}:{gid}") for label, action in row]
        for row in rows if row
    ]


def task_keyboard(
    gid: str, status: str, *,
    with_back: bool = False, back: str | None = None,
    local: bool = True, multi_file: bool | None = None, link: str | None = None,
) -> InlineKeyboardMarkup | None:
    """单个任务卡片的键盘；状态不认识时返回 None。

    back：返回按钮的 callback_data（从列表打开时是 "list:<tab>:<页>"，回到
    原来的位置）；with_back=True 是旧写法，等价于 back="list:ALL:0"。
    link：已完成任务的 GoFile 链接，有就放一个直接打开的 URL 按钮，不用再
    点开弹窗复制。"""
    rows = _action_buttons(gid, status, local=local, multi_file=multi_file)
    if not rows:
        return None
    if link and status == "COMPLETED" and link.startswith(("http://", "https://")):
        rows.insert(0, [InlineKeyboardButton(text="☁️ 打开 GoFile 链接", url=link)])
    if back is None and with_back:
        back = "list:ALL:0"
    if back:
        rows.append([InlineKeyboardButton(text=BACK_TO_LIST_TEXT, callback_data=back)])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def task_open_button(index: int, gid: str, name: str, *, status: str | None = None,
                     back: str | None = None) -> list[InlineKeyboardButton]:
    """列表里的一行任务。带状态图标，扫一眼就知道哪些在下载/失败；
    back 是 "<tab>:<页>"，让打开的卡片能返回到原来的列表位置。"""
    icon = STATUS_EMOJI.get(status, "") if status else ""
    text = f"{icon} {index}. {name[:30]}".strip()
    data = f"topen:{back}:{gid}" if back else f"task:open:{gid}"
    return [InlineKeyboardButton(text=text, callback_data=data)]


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


def task_delete_confirm_keyboard(gid: str, *, can_delete_files: bool = False,
                                 destructive: bool = False) -> InlineKeyboardMarkup:
    """「🗑 删除」的确认：仅删记录 / 连同磁盘文件一起删（本机节点且有保存
    路径时才给这个选项），删文件还要再确认一次——跟取消任务同一套两步确认。"""
    if destructive:
        rows = [
            [InlineKeyboardButton(text="⚠️ 确认永久删除文件", callback_data=f"task:purge:{gid}")],
            [InlineKeyboardButton(text="⬅️ 返回任务", callback_data=f"task:detail:{gid}")],
        ]
    else:
        rows = [[InlineKeyboardButton(text="🗑 仅删除记录", callback_data=f"task:delete_record:{gid}")]]
        if can_delete_files:
            rows.append([InlineKeyboardButton(text="🗑 删除记录和文件", callback_data=f"task:confirm_purge:{gid}")])
        rows.append([InlineKeyboardButton(text="⬅️ 返回任务", callback_data=f"task:detail:{gid}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


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
            [
                InlineKeyboardButton(text="🖥 服务器状态", callback_data="admin:sysinfo"),
                InlineKeyboardButton(text="🌐 节点管理", callback_data="admin:nodes"),
            ],
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


def _limit_value_key(raw: str | None) -> str | None:
    """aria2 返回的限速是字节数（"2097152"），预设是 "2M" 这种写法——换算
    成同一种形式才能标出当前选中的是哪个预设。"""
    if raw is None:
        return None
    if raw in {v for _, v in LIMIT_PRESETS}:
        return raw
    try:
        n = int(raw)
    except ValueError:
        return None
    if n == 0:
        return "0"
    if n % (1024 * 1024) == 0:
        return f"{n // (1024 * 1024)}M"
    return None


def limit_chooser_keyboard(current: str | None = None) -> InlineKeyboardMarkup:
    cur = _limit_value_key(current)
    buttons = [
        InlineKeyboardButton(text=_mark(label, value == cur), callback_data=f"setlimit:{value}")
        for label, value in LIMIT_PRESETS
    ]
    rows = [[buttons[0]], *_chunk(buttons[1:], 2)]
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
    rows = _chunk([
        InlineKeyboardButton(text=_mark(label, value == current_mb), callback_data=f"setmaxsize:{value}")
        for label, value in MAXSIZE_PRESETS
    ], 3)
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
    cur = _limit_value_key(current)
    buttons = [
        InlineKeyboardButton(text=_mark(label, value == cur), callback_data=f"tasklimit:{gid}:{value}")
        for label, value in LIMIT_PRESETS
    ]
    rows = [[buttons[0]], *_chunk(buttons[1:], 2)]
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


BAR_FILLED = "▰"
BAR_EMPTY = "▱"


def text_progress_bar(percent: float, width: int = 14) -> str:
    """▰▰▰▱▱▱ 风格的进度条。两个字符同属 Unicode 几何图形区，各平台字体里
    宽度一致，不需要放在等宽 <code> 里（以前的 █/░ 在手机上 ░ 显示成灰色
    网纹，很难看）。

    两端特殊处理：刚开始下载（>0 但不到一格）也点亮第一格，不然大文件开头
    很长一段时间看起来像"没动"；不到 100% 时最后一格不点亮，不然 99.6% 看起来
    已经下完了。"""
    p = max(0.0, min(100.0, percent))
    filled = round(p / 100 * width)
    if p > 0 and filled == 0:
        filled = 1
    if p < 100 and filled == width:
        filled = width - 1
    return BAR_FILLED * filled + BAR_EMPTY * (width - filled)
