from datetime import datetime, timedelta, timezone

from aiogram.types import InlineKeyboardMarkup

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
    return (datetime.now(timezone.utc) - timedelta(days=n)).strftime("%Y-%m-%d %H:%M:%S")


async def render_stats_view(repo, days: str) -> tuple[str, InlineKeyboardMarkup]:
    stats = await repo.get_period_stats(_period_since(days))
    label = PERIOD_LABELS.get(days, "全部")
    return render_stats(label, stats), stats_period_keyboard(days)
