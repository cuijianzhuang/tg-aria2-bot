"""Video-specific presentation and capability-aware keyboards."""

import json
from html import escape

from aiogram.types import InlineKeyboardButton as Button
from aiogram.types import InlineKeyboardMarkup as Markup

from bot.core.keyboards import BACK_TO_LIST_TEXT, task_keyboard
from bot.core.quota import fmt_bytes

MODES = {"personal": "个人账号", "shared": "共享账号", "anonymous": "未登录"}
STATES = {
    "queued": "准备登录",
    "preparing": "正在准备登录页面",
    "waiting": "等待扫码",
    "scanned": "已扫码，请在手机确认",
    "success": "登录成功",
    "expired": "二维码已过期",
    "cancelled": "已取消",
    "verification_required": "服务器上的平台登录页要求额外验证，暂时无法继续扫码。请稍后重试，或使用已有 Cookie 配置",
    "failed": "登录失败，请刷新重试",
}


def preview_card(preview):
    info = json.loads(preview["info"])
    return (
        f"🎬 <b>{escape(info['title'])}</b>\n"
        f"🧩 {escape(preview['parser'])} · 🎞 {escape(preview['quality'])}\n"
        f"👤 {MODES[preview['account_mode']]} · 📍 本机\n"
        f"📦 {fmt_bytes(info['size']) if info.get('size') else '大小未知'}\n"
        "确认后下载并自动合并；暂停任务保留执行槽位。"
    )


def preview_keyboard(preview):
    token = preview["token"]
    info = json.loads(preview["info"])
    buttons = [
        Button(
            text=("✅ " if q == preview["quality"] else "") + ("最佳" if q == "best" else q),
            callback_data=f"vp:q:{token}:{i}",
        )
        for i, q in enumerate(info["qualities"])
    ]
    rows = [buttons[i : i + 3] for i in range(0, len(buttons), 3)]
    rows += [
        [Button(text=p, callback_data=f"vp:p:{token}:{p}") for p in ("yt-dlp", "you-get")],
        [Button(text=label, callback_data=f"vp:a:{token}:{mode}") for mode, label in MODES.items()],
        [
            Button(text="▶️ 确认下载", callback_data=f"vp:start:{token}"),
            Button(text="❌ 取消", callback_data=f"vp:cancel:{token}"),
        ],
        [Button(text="🔑 平台账号", callback_data=f"vacc:menu:{token}")],
    ]
    return Markup(inline_keyboard=rows)


def video_task_keyboard(gid, status, back=None, link=None):
    keyboard = task_keyboard(gid, status, multi_file=False, link=link)
    if keyboard:
        for row in keyboard.inline_keyboard:
            row[:] = [
                b
                for b in row
                if not b.callback_data
                or not any(b.callback_data.startswith("task:" + action + ":") for action in ("limit", "files"))
                or status == "COMPLETED"
            ]
        keyboard.inline_keyboard[:] = [row for row in keyboard.inline_keyboard if row]
        if back:
            keyboard.inline_keyboard.append([Button(text=BACK_TO_LIST_TEXT, callback_data=back)])
    return keyboard


def video_card(row, job):
    total = job["total"]
    progress = f"{min(100, job['completed'] / total * 100):.1f}%" if total else "进度待定"
    lines = [
        f"🎬 <b>{escape(row['file_name'] or '视频')}</b>",
        f"{escape(row['status'])} · {escape(job['phase'])}",
        f"📦 {fmt_bytes(job['completed'])} · {progress}",
        f"🧩 {escape(job['parser'])} · 🎞 {escape(job['quality'])} · 👤 {MODES[job['account_mode']]}",
    ]
    if row["save_path"] and row["status"] == "COMPLETED":
        lines.append("📂 <code>" + escape(row["save_path"]) + "</code>")
    if row["error"]:
        lines.append("⚠️ " + escape(row["error"]))
    lines.append(f"📍 本机 · #{row['id']}")
    return "\n".join(lines)


def login_keyboard(login_id):
    return Markup(
        inline_keyboard=[
            [
                Button(text="🔄 刷新二维码", callback_data=f"vlogin:refresh:{login_id}"),
                Button(text="❌ 取消", callback_data=f"vlogin:cancel:{login_id}"),
            ]
        ]
    )


def with_list_back(markup, back):
    if not back:
        return markup
    return Markup(inline_keyboard=[*markup.inline_keyboard, [Button(text=BACK_TO_LIST_TEXT, callback_data=back)]])
