"""Credential-free account metadata and private Telegram account views."""

from datetime import UTC, datetime
from html import escape

from aiogram.types import InlineKeyboardButton as Button
from aiogram.types import InlineKeyboardMarkup as Markup

from bot.config import settings
from bot.video.ui import MODES

PLATFORM_LABELS = {"bilibili": "B站", "douyin": "抖音", "xhs": "小红书"}


def metadata(row):
    if row is None:
        return None
    return {"identity": row["identity"], "updated": row["updated"]}


def display_time(updated):
    return datetime.fromtimestamp(updated, UTC).strftime("%Y-%m-%d %H:%M UTC") if updated else "未知"


async def shared_summary(repo, platform):
    row = await repo.video_account(platform, 0)
    configured = bool(getattr(settings, "video_cookie_file_" + platform, ""))
    enabled = bool(row["enabled"]) if row else await repo.video_preference(0, platform) == "shared"
    saved = bool(row) or configured
    return {
        "platform": platform,
        "label": PLATFORM_LABELS[platform],
        "identity": row["identity"] if row else None,
        "updated": row["updated"] if row else None,
        "shared": enabled and saved,
        "saved": saved,
        "source": "scan" if row else "file" if configured else None,
    }


async def overview(repo, user_id, admin):
    lines = ["🔑 <b>已保存的平台账号</b>", "登录态是否有效会在解析/下载时校验。", ""]
    rows = []
    for platform, label in PLATFORM_LABELS.items():
        personal = metadata(await repo.video_account(platform, user_id))
        shared = await shared_summary(repo, platform)
        mode = await repo.video_preference(user_id, platform)
        lines += [f"<b>{label}</b> · 默认：{MODES[mode]}"]
        lines.append("👤 个人：" + (escape(personal["identity"]) if personal else "未登录"))
        if personal:
            lines.append("🕒 " + display_time(personal["updated"]))
        shared_state = "未登录"
        if shared["saved"]:
            shared_state = "已开放" if shared["shared"] else "已保存，未开放"
            if shared["identity"] and (admin or shared["shared"]):
                shared_state += " · " + escape(shared["identity"])
            elif shared["source"] == "file":
                shared_state += " · Cookie 文件"
        lines += ["👥 共享：" + shared_state, ""]
        count = int(personal is not None) + int(shared["saved"])
        rows.append([Button(text=f"🔑 {label} · 已保存 {count}", callback_data=f"vacc:platform:{platform}")])
    rows.append([Button(text="🔄 刷新账号列表", callback_data="vacc:menu")])
    return "\n".join(lines), Markup(inline_keyboard=rows)


async def platform_view(repo, user_id, platform, admin):
    personal = metadata(await repo.video_account(platform, user_id))
    shared = await shared_summary(repo, platform)
    mode = await repo.video_preference(user_id, platform)
    lines = [f"🔑 <b>{PLATFORM_LABELS[platform]}账号管理</b>", f"默认使用：{MODES[mode]}", "", "👤 <b>个人账号</b>"]
    lines += (
        [escape(personal["identity"]), "最近登录：" + display_time(personal["updated"])] if personal else ["未登录"]
    )
    lines += ["", "👥 <b>共享账号</b>"]
    if shared["saved"]:
        lines.append("已开放" if shared["shared"] else "已保存，未开放")
        if shared["identity"] and (admin or shared["shared"]):
            lines.append(escape(shared["identity"]))
            lines.append("最近登录：" + display_time(shared["updated"]))
        elif shared["source"] == "file":
            lines.append("来源：管理员配置的 Cookie 文件")
    else:
        lines.append("未登录")
    lines += ["", "已保存不代表当前仍有效；解析失败时可重新扫码。"]
    rows = [
        [
            Button(
                text="📱 重新登录个人账号" if personal else "📱 个人扫码登录", callback_data=f"vacc:personal:{platform}"
            )
        ]
    ]
    if personal:
        rows.append([Button(text="🗑 移除个人登录", callback_data=f"vacc:remove:{platform}")])
    if admin:
        rows.append(
            [
                Button(
                    text="👥 重新登录共享账号" if shared["saved"] else "👥 共享账号扫码",
                    callback_data=f"vacc:shared:{platform}",
                )
            ]
        )
        if shared["saved"]:
            rows.append(
                [
                    Button(
                        text="⛔ 关闭共享" if shared["shared"] else "✅ 开放共享",
                        callback_data=f"vacc:{'unshare' if shared['shared'] else 'share'}:{platform}",
                    )
                ]
            )
        if shared["identity"]:
            rows.append([Button(text="🗑 移除共享登录", callback_data=f"vacc:remove-shared:{platform}")])
    modes = [
        ("personal", "👤 使用个人", bool(personal)),
        ("shared", "👥 使用共享", shared["shared"]),
        ("anonymous", "🌐 未登录下载", True),
    ]
    rows.append(
        [
            Button(text=("✅ " if mode == name else "") + label, callback_data=f"vacc:use-{name}:{platform}")
            for name, label, available in modes
            if available
        ]
    )
    rows.append([Button(text="⬅️ 全部已保存账号", callback_data="vacc:menu")])
    return "\n".join(lines), Markup(inline_keyboard=rows)
