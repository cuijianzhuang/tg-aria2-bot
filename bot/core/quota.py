"""普通用户的额度：同时进行的任务数、单任务大小、每天下载总量。

规则
- 管理员永远不受限制
- 额度 = 该用户在 user_quotas 表里的覆盖值，没有覆盖就用 .env 里的默认值
- 0 表示不限制；覆盖值 NULL 表示"沿用默认"
- 每日用量按服务器时区的自然日统计，由 TaskManager 在轮询时累加（见
  task_manager._account_usage）
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime

from bot.config import settings

GIB = 1024**3
MIB = 1024**2


def today() -> str:
    return date.today().isoformat()


@dataclass(frozen=True)
class Limits:
    max_active: int = 0       # 同时进行的任务数，0 = 不限
    max_task_bytes: int = 0   # 单个任务大小上限（字节），0 = 不限
    daily_bytes: int = 0      # 每天下载总量（字节），0 = 不限

    @property
    def unlimited(self) -> bool:
        return not (self.max_active or self.max_task_bytes or self.daily_bytes)


def fmt_bytes(n: int | float) -> str:
    size = float(n)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if size < 1024:
            return f"{size:.0f} B" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} PiB"


def default_limits() -> Limits:
    return Limits(
        max_active=max(0, settings.user_max_active_tasks),
        max_task_bytes=max(0, int(settings.user_max_task_size_gb * GIB)),
        daily_bytes=max(0, int(settings.user_daily_quota_gb * GIB)),
    )


def merge_limits(row, base: Limits | None = None) -> Limits:
    """默认值 + 个别覆盖。row 是 user_quotas 的一行（或 None）；base 是 VIP 用户用的默认值。"""
    base = base or default_limits()
    if row is None:
        return base

    def pick(override, default):
        return default if override is None else max(0, int(override))

    return Limits(
        max_active=pick(row["max_active"], base.max_active),
        max_task_bytes=pick(row["max_task_bytes"], base.max_task_bytes),
        daily_bytes=pick(row["daily_bytes"], base.daily_bytes),
    )


async def limits_for(repo, user_id: int) -> Limits:
    if settings.is_admin(user_id):
        return Limits()
    from bot.core import vip  # 延迟导入：vip 反过来也用到这里的 Limits
    base = vip.vip_limits() if await vip.is_vip(repo, user_id) else default_limits()
    return merge_limits(await repo.get_user_quota(user_id), base)


async def admission_error(
    repo, user_id: int, size: int | None, *, pending_usage: int = 0, check_active: bool = True,
) -> str | None:
    """用户想开始一个新任务（或恢复一个暂停的任务）时检查额度。
    返回拒绝原因（给用户看的中文），None 表示放行。

    size 是已知的任务大小（转发的 Telegram 文件才知道；URL/磁力/种子在添加时
    不知道，传 None——它们的大小要等 aria2 连上后由 TaskManager 在下载中再拦）。
    pending_usage 是还没写进数据库的当日用量（TaskManager 的内存累加值）。
    check_active=False 用于"恢复暂停的任务"：它本来就占着名额，只需要看当日流量。"""
    limits = await limits_for(repo, user_id)
    if limits.unlimited:
        return None
    if check_active and limits.max_active:
        n = await repo.count_active_by_user(user_id)
        if n >= limits.max_active:
            return f"你最多同时进行 {limits.max_active} 个任务（现在有 {n} 个），请等有任务完成后再开始。"
    if size and limits.max_task_bytes and size > limits.max_task_bytes:
        return f"这个文件（{fmt_bytes(size)}）超过了你的单任务上限 {fmt_bytes(limits.max_task_bytes)}。"
    if limits.daily_bytes:
        used = await repo.get_usage(user_id, today()) + pending_usage
        if used >= limits.daily_bytes:
            return f"你今天的下载额度已用完（{fmt_bytes(used)} / {fmt_bytes(limits.daily_bytes)}），明天再来吧。"
        if size and used + size > limits.daily_bytes:
            return (
                f"这个文件（{fmt_bytes(size)}）会超出你今天剩余的额度"
                f"（还剩 {fmt_bytes(limits.daily_bytes - used)}）。"
            )
    return None


async def render_quota(repo, user_id: int, *, pending_usage: int = 0, title: str = "📊 <b>我的额度</b>") -> str:
    """/quota 的输出：每项的上限和当前用量。"""
    is_admin = settings.is_admin(user_id)
    limits = await limits_for(repo, user_id)
    if is_admin:
        return f"{title}\n\n管理员不受任何额度限制。"
    from bot.core import vip
    vip_now, vip_exp = await vip.get_status(repo, user_id)
    if vip_now:
        until = "永久" if vip_exp is None else f"到期 {datetime.fromtimestamp(vip_exp):%Y-%m-%d %H:%M}"
        title += f"\n👑 VIP（{until}）"
    if limits.unlimited:
        return f"{title}\n\n目前没有任何限制。"
    active = await repo.count_active_by_user(user_id)
    used = await repo.get_usage(user_id, today()) + pending_usage

    def line(label, current, cap, fmt):
        if not cap:
            return f"{label}：不限"
        return f"{label}：{fmt(current)} / {fmt(cap)}"

    lines = [
        title,
        "",
        line("进行中任务", active, limits.max_active, str),
        line("今日流量", used, limits.daily_bytes, fmt_bytes),
    ]
    if limits.daily_bytes:
        lines[-1] += f"（剩余 {fmt_bytes(max(0, limits.daily_bytes - used))}）"
    lines.append(f"单任务上限：{fmt_bytes(limits.max_task_bytes) if limits.max_task_bytes else '不限'}")
    lines += ["", "<i>今日流量按服务器时区的自然日统计，每天零点重置。</i>"]
    return "\n".join(lines)


# ---------------------------------------------------------------- /setquota 解析

_SIZE_UNITS = {"": GIB, "g": GIB, "gb": GIB, "gib": GIB, "m": MIB, "mb": MIB, "mib": MIB, "t": 1024 * GIB, "tb": 1024 * GIB}


def parse_size(text: str) -> int:
    """'2' / '2G' / '500M' / '1.5g' / '0' → 字节。不带单位按 GB 算。"""
    t = text.strip().lower()
    num = t.rstrip("abgimt")
    unit = t[len(num):]
    if unit not in _SIZE_UNITS:
        raise ValueError(f"看不懂的大小「{text}」，示例：2G、500M、0（不限）")
    try:
        value = float(num)
    except ValueError:
        raise ValueError(f"看不懂的大小「{text}」，示例：2G、500M、0（不限）") from None
    if value < 0:
        raise ValueError("大小不能是负数")
    return int(value * _SIZE_UNITS[unit])


SETQUOTA_USAGE = (
    "用法：<code>/setquota 用户ID active=3 size=2G daily=20G</code>\n"
    "• active：同时进行的任务数\n"
    "• size：单个任务大小上限（不带单位按 GB，也可以写 500M）\n"
    "• daily：每天下载总量\n"
    "• 值写 <code>0</code> = 不限制，写 <code>default</code> = 恢复默认\n"
    "• <code>/setquota 用户ID reset</code> 清掉这个人的所有个别设置"
)

_FIELD_ALIASES = {
    "active": "max_active", "size": "max_task_bytes", "daily": "daily_bytes",
}


def parse_setquota(args: str) -> tuple[int, dict[str, int | None] | None]:
    """解析 /setquota 的参数。返回 (用户ID, 要改的字段)；字段为 None 表示 reset。
    格式不对抛 ValueError（消息直接给管理员看）。"""
    parts = args.split()
    if len(parts) < 2:
        raise ValueError("参数不够")
    try:
        user_id = int(parts[0])
    except ValueError:
        raise ValueError("用户 ID 必须是数字") from None
    if len(parts) == 2 and parts[1].lower() == "reset":
        return user_id, None
    changes: dict[str, int | None] = {}
    for item in parts[1:]:
        key, sep, value = item.partition("=")
        key = key.lower()
        if not sep or key not in _FIELD_ALIASES:
            raise ValueError(f"看不懂「{item}」，可用的项：active / size / daily")
        field = _FIELD_ALIASES[key]
        if value.lower() == "default":
            changes[field] = None
        elif key == "active":
            if not value.isdigit():
                raise ValueError("active 必须是整数（0 = 不限）")
            changes[field] = int(value)
        else:
            changes[field] = parse_size(value)
    return user_id, changes
