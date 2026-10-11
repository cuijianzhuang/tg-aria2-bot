"""⬆️ 在机器人里检查更新、一键升级（仅管理员）。

流程：设置页「⬆️ 检查更新」（或 /upgrade、或定期检查发来的通知）→ 看到将要更新的
提交列表 → 「⬆️ 立即升级」→ 确认页 →「✅ 确认升级」→ 升级任务在独立的 systemd
服务里跑（见 core/upgrade.py）→ 机器人重启后把结果编辑回这条消息上。
"""
import asyncio
import logging
import re
import time
from datetime import datetime
from html import escape

from aiogram import F, Router
from aiogram.filters import Command
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.core import upgrade
from bot.core.upgrade import UpdateInfo, UpgradeError
from bot.middlewares.auth import AdminMiddleware

log = logging.getLogger(__name__)
router = Router(name="upgrade")
router.message.middleware(AdminMiddleware())
router.callback_query.middleware(AdminMiddleware())

BACK = InlineKeyboardButton(text="⬅️ 返回设置", callback_data="nav:settings")
RECHECK = InlineKeyboardButton(text="🔄 重新检查", callback_data="admin:upgrade")
ACTION_RE = r"^upgrade:(ask|go):([0-9a-f]{7,40})(:reset)?$"
ROLLBACK_RE = r"^rollback:(ask|go):([0-9a-f]{7,40})$"
ROLLBACK = InlineKeyboardButton(text="⏪ 回退版本", callback_data="admin:rollback")

# 后台 watcher 任务要留强引用：事件循环只弱引用任务，没人引用会被回收
_tasks: set[asyncio.Task] = set()


def _ago(ts: int) -> str:
    seconds = max(0, int(time.time()) - ts)
    if seconds < 90:
        return "刚刚"
    if seconds < 3600:
        return f"{seconds // 60} 分钟前"
    if seconds < 86400:
        return f"{seconds // 3600} 小时前"
    return f"{seconds // 86400} 天前"


def _kb(*rows: list[InlineKeyboardButton]) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=list(rows))


def render_error(message: str) -> tuple[str, InlineKeyboardMarkup]:
    return f"⬆️ <b>检查更新</b>\n\n⚠️ {escape(message, quote=False)}", _kb([RECHECK, BACK])


def render_check(info: UpdateInfo, note: str = "") -> tuple[str, InlineKeyboardMarkup]:
    cur = info.current
    lines = ["⬆️ <b>检查更新</b>", ""]
    if note:
        lines += [f"ℹ️ {escape(note, quote=False)}", ""]
    lines.append(
        f"当前版本：<code>{cur.short}</code> · {datetime.fromtimestamp(cur.ts):%Y-%m-%d}\n"
        f"{escape(cur.subject, quote=False)}\n分支：<code>{escape(cur.branch, quote=False)}</code>"
    )
    lines.append("")

    if info.up_to_date:
        lines.append("✅ 已经是最新版本。")
        return "\n".join(lines), _kb([RECHECK, ROLLBACK], [BACK])

    if info.total:
        lines.append(f"🆕 <b>发现 {info.total} 个新提交</b>（最新 <code>{info.remote_short}</code>）：")
        for c in info.commits[:8]:
            lines.append(f"• <code>{c.short}</code> {escape(c.subject, quote=False)} <i>({_ago(c.ts)})</i>")
        if info.total > 8:
            lines.append(f"…还有 {info.total - 8} 个")
        if info.deps_changed:
            lines.append("\n📦 依赖有变化，升级时会自动重新安装。")
        if info.env_changed:
            lines.append("📝 <code>.env.example</code> 有新配置项；不添加也能运行（都有默认值）。")

    blocked = bool(info.dirty or info.diverged)
    if info.diverged:
        lines += ["", "⚠️ <b>本地分支和远端已经分叉</b>（本地有远端没有的提交），无法直接更新。"]
    if info.dirty:
        shown = ", ".join(f"<code>{escape(f, quote=False)}</code>" for f in info.dirty[:6])
        more = f" 等 {len(info.dirty)} 个" if len(info.dirty) > 6 else ""
        lines += ["", f"⚠️ <b>仓库里有本地改动</b>：{shown}{more}\n升级会因此中止。"]

    sha = info.remote_short
    if blocked:
        lines.append(
            "\n要继续的话只能<b>丢弃这些本地改动</b>后升级（本地的修改会永久丢失；"
            "<code>.env</code>、数据库、下载文件不受影响）。如果这些改动要保留，请到服务器上手动处理。"
        )
        buttons = [InlineKeyboardButton(text="⚠️ 丢弃本地改动并升级", callback_data=f"upgrade:ask:{sha}:reset")]
    else:
        buttons = [InlineKeyboardButton(text="⬆️ 立即升级", callback_data=f"upgrade:ask:{sha}")]
    return "\n".join(lines), _kb(buttons, [RECHECK, ROLLBACK], [BACK])


def render_confirm(info: UpdateInfo, *, reset: bool) -> tuple[str, InlineKeyboardMarkup]:
    lines = [
        "⚠️ <b>确认升级？</b>",
        "",
        f"<code>{info.current.short}</code> → <code>{info.remote_short}</code>（{info.total} 个新提交）",
        "",
        "• 先备份 <code>.env</code> 和数据库，再更新代码、重启机器人（约 30 秒）",
        "• 进行中的下载由 aria2 负责，<b>不受影响</b>，机器人起来后自动接上",
        "• 重启后会做 20 秒健康检查，没通过会<b>自动回滚</b>到现在的版本",
        "• 升级结果会回到这条消息上",
    ]
    if reset:
        files = ", ".join(f"<code>{escape(f, quote=False)}</code>" for f in info.dirty[:6]) or "（分叉的本地提交）"
        lines += ["", f"🗑 <b>将永久丢弃本地改动：</b>{files}"]
    sha = info.remote_short
    go = f"upgrade:go:{sha}" + (":reset" if reset else "")
    return "\n".join(lines), _kb(
        [InlineKeyboardButton(text="✅ 确认升级", callback_data=go)],
        [InlineKeyboardButton(text="取消", callback_data="admin:upgrade")],
    )


async def _show(target, text: str, markup: InlineKeyboardMarkup):
    """target 是 Message（新发一条）或 CallbackQuery（改当前这条）。"""
    if hasattr(target, "reply"):          # Message
        await target.reply(text, parse_mode="HTML", reply_markup=markup)
        return
    try:
        await target.message.edit_text(text, parse_mode="HTML", reply_markup=markup)
    except Exception:
        await target.message.answer(text, parse_mode="HTML", reply_markup=markup)
    await target.answer()


async def _check_and_show(target, note: str = ""):
    try:
        info = await upgrade.check()
    except UpgradeError as e:
        await _show(target, *render_error(str(e)))
        return None
    await _show(target, *render_check(info, note))
    return info


@router.message(Command("upgrade"))
async def cmd_upgrade(message: Message):
    await _check_and_show(message)


@router.callback_query(F.data == "admin:upgrade")
async def show_check(query: CallbackQuery):
    ok, reason = upgrade.support()
    if not ok:
        await _show(query, *render_error(reason))
        return
    await query.answer("正在检查更新…")
    await _check_and_show(query)


@router.callback_query(F.data.regexp(ACTION_RE))
async def upgrade_action(query: CallbackQuery, bot):
    m = re.match(ACTION_RE, query.data)
    action, sha, reset = m.group(1), m.group(2), bool(m.group(3))
    if query.message is None:
        await query.answer("消息已失效，请重新发起。", show_alert=True)
        return

    try:
        info = await upgrade.check()
    except UpgradeError as e:
        await _show(query, *render_error(str(e)))
        return

    # 确认的是哪个版本就升到哪个版本：远端在你看列表之后又有了新提交，要重新确认
    if not info.remote_sha.startswith(sha):
        await _show(query, *render_check(info, "远端在你确认之前又有了新提交，列表已刷新，请重新确认。"))
        return
    if info.up_to_date:
        await _show(query, *render_check(info))
        return
    # 有本地改动/分叉时，只有明确选了"丢弃并升级"才继续
    if (info.dirty or info.diverged) and not reset:
        await _show(query, *render_check(info))
        return

    if action == "ask":
        await _show(query, *render_confirm(info, reset=reset))
        return

    # action == "go"
    try:
        await upgrade.start_upgrade(
            chat_id=query.message.chat.id, message_id=query.message.message_id,
            user_id=query.from_user.id, from_sha=info.current.sha, target_sha=info.remote_sha, reset=reset,
        )
    except UpgradeError as e:
        await _show(query, *render_error(str(e)))
        return

    log.info("upgrade %s -> %s started by admin %s (reset=%s)",
             info.current.short, info.remote_short, query.from_user.id, reset)
    await query.answer("升级已开始")
    try:
        await query.message.edit_text(
            "⏳ <b>正在升级…</b>\n\n"
            f"<code>{info.current.short}</code> → <code>{info.remote_short}</code>\n"
            "备份 → 更新代码 → 重启 → 健康检查。机器人会短暂离线，完成后结果会显示在这条消息上。",
            parse_mode="HTML", reply_markup=None,
        )
    except Exception:
        pass
    # 升级在重启之前就失败时机器人不会被重启，由这个 watcher 汇报；
    # 机器人被重启的话，由新进程启动时的 finish_pending_upgrade 接着汇报
    task = asyncio.create_task(_watch_after_start(bot))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


def render_rollback_list(commits: list) -> tuple[str, InlineKeyboardMarkup]:
    lines = ["⏪ <b>回退版本</b>", "", "选择要回退到的版本（最近的在前，第一项是当前版本）："]
    rows = []
    for i, c in enumerate(commits):
        mark = "（当前）" if i == 0 else ""
        lines.append(f"• <code>{c.short}</code> {escape(c.subject, quote=False)} <i>({_ago(c.ts)})</i>{mark}")
        if i > 0:
            rows.append([InlineKeyboardButton(text=f"{c.short} {c.subject}"[:40], callback_data=f"rollback:ask:{c.short}")])
    if len(commits) <= 1:
        lines.append("\n没有更早的版本可以回退。")
    lines.append("\n回退后如果又想升到最新，回到「检查更新」再升级即可。")
    return "\n".join(lines), _kb(*rows, [BACK])


def render_rollback_confirm(current, target) -> tuple[str, InlineKeyboardMarkup]:
    text = (
        "⚠️ <b>确认回退？</b>\n\n"
        f"<code>{current.short}</code> → <code>{target.short}</code>\n"
        f"{escape(target.subject, quote=False)}\n\n"
        "• 先备份 <code>.env</code> 和数据库，再把代码切回这个版本、重启机器人（约 30 秒）\n"
        "• 数据库升级只增不删，旧代码可以直接用\n"
        "• 仓库里的本地改动会被丢弃\n"
        "• 重启后做健康检查，没通过会自动恢复到现在的版本"
    )
    return text, _kb(
        [InlineKeyboardButton(text="✅ 确认回退", callback_data=f"rollback:go:{target.short}")],
        [InlineKeyboardButton(text="取消", callback_data="admin:rollback")],
    )


@router.callback_query(F.data == "admin:rollback")
async def show_rollback(query: CallbackQuery):
    try:
        commits = await upgrade.recent_versions()
    except UpgradeError as e:
        await _show(query, *render_error(str(e)))
        return
    await _show(query, *render_rollback_list(commits))


@router.callback_query(F.data.regexp(ROLLBACK_RE))
async def rollback_action(query: CallbackQuery, bot):
    m = re.match(ROLLBACK_RE, query.data)
    action, sha = m.group(1), m.group(2)
    if query.message is None:
        await query.answer("消息已失效，请重新发起。", show_alert=True)
        return
    try:
        commits = await upgrade.recent_versions()
        current = await upgrade.current_version()
    except UpgradeError as e:
        await _show(query, *render_error(str(e)))
        return
    # 只允许回退到列表里出现过的版本（不接受任意输入）
    target = next((c for c in commits[1:] if c.short.startswith(sha[:7])), None)
    if target is None:
        await _show(query, *render_rollback_list(commits))
        return
    if action == "ask":
        await _show(query, *render_rollback_confirm(current, target))
        return
    try:
        await upgrade.start_upgrade(
            chat_id=query.message.chat.id, message_id=query.message.message_id,
            user_id=query.from_user.id, from_sha=current.sha, target_sha=target.short, rollback=True,
        )
    except UpgradeError as e:
        await _show(query, *render_error(str(e)))
        return
    log.info("rollback %s -> %s started by admin %s", current.short, target.short, query.from_user.id)
    await query.answer("回退已开始")
    try:
        await query.message.edit_text(
            "⏳ <b>正在回退…</b>\n\n"
            f"<code>{current.short}</code> → <code>{target.short}</code>\n"
            "备份 → 切换代码 → 重启 → 健康检查。机器人会短暂离线，完成后结果会显示在这条消息上。",
            parse_mode="HTML", reply_markup=None,
        )
    except Exception:
        pass
    task = asyncio.create_task(_watch_after_start(bot))
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)


async def _watch_after_start(bot):
    await asyncio.sleep(2)   # 给升级任务一点启动时间，免得刚起就被当成"已结束"
    await upgrade.finish_pending_upgrade(bot)
