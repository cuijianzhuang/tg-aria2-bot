from datetime import UTC, datetime, timedelta

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from bot.core.cards import render_stats
from bot.core.keyboards import PERIOD_PRESETS, stats_period_keyboard

DEFAULT_PERIOD = "7"  # /stats 不带参数时默认显示最近 7 天
# PERIOD_PRESETS 里每项是 (label, value)，这里要的是 value -> label 的反向映射
PERIOD_LABELS = {value: label for label, value in PERIOD_PRESETS}


def _period_since(days: str) -> str | None:
    """days 是字符串形式的天数，"0" 表示不限时间范围。返回值要跟 SQLite
    CURRENT_TIMESTAMP 写入 created_at 时同样的格式（'YYYY-MM-DD HH:MM:SS'，
    UTC，空格分隔无时区后缀），repo.get_period_stats 靠字符串比较筛选时间范围，
    格式对不上比较结果就是错的。"""
    n = int(days)
    if n <= 0:
        return None
    return (datetime.now(UTC) - timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")


DAILY_DAYS = 14
BAR_WIDTH = 12


async def render_daily_view(repo, *, user_id: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    """最近 14 天每天的实际下载量（来自额度统计的 usage_daily），管理员额外看到
    谁下得最多。user_id=None 是管理员视角（所有人合计）。"""
    from datetime import date

    from bot.core.quota import fmt_bytes

    today = date.today()
    days = [(today - timedelta(days=i)).isoformat() for i in range(DAILY_DAYS - 1, -1, -1)]
    data = dict(await repo.get_usage_since(days[0], user_id=user_id))
    peak = max(data.values(), default=0)
    lines = [f"📅 <b>最近 {DAILY_DAYS} 天下载量</b>" + ("" if user_id is None else "（我的）"), ""]
    for day in days:
        n = data.get(day, 0)
        filled = max(1, round(n / peak * BAR_WIDTH)) if n and peak else 0
        lines.append(f"<code>{day[5:]}</code> {'▰' * filled}{'▱' * (BAR_WIDTH - filled)} {fmt_bytes(n) if n else '-'}")
    total = sum(data.values())
    lines += ["", f"合计 {fmt_bytes(total)}，日均 {fmt_bytes(total / DAILY_DAYS)}"]
    if user_id is None:
        top = await repo.get_top_users_since(days[0])
        if top:
            lines += ["", "<b>下载最多的用户</b>"]
            lines += [f"• <code>{uid}</code>　{fmt_bytes(b)}" for uid, b in top]
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📊 返回统计", callback_data=f"stats:{DEFAULT_PERIOD}"),
        InlineKeyboardButton(text="⬅️ 主菜单", callback_data="nav:start"),
    ]])
    return "\n".join(lines), markup


async def render_stats_view(repo, days: str, *, user_id: int | None = None) -> tuple[str, InlineKeyboardMarkup]:
    stats = await repo.get_period_stats(_period_since(days), user_id=user_id)
    label = PERIOD_LABELS.get(days, "全部")
    return render_stats(label, stats), stats_period_keyboard(days)
