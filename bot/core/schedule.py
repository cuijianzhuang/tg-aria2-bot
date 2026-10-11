"""限速时段：夜里放开、白天压低，不用每天手动改全局限速。

规则写在 .env 的 SPEED_SCHEDULE，格式 `时段=限速`，多条用分号隔开：
    23:00-07:00=0;07:00-23:00=2M
限速用 aria2 的写法（0 = 不限，500K，2M）。时段可以跨午夜；不在任何时段内时恢复
不限速。时间用服务器本地时区。只作用于本机 default 节点（跟"全局限速"设置一致）。
"""
import asyncio
import logging
import re
from dataclasses import dataclass
from datetime import datetime, time

from bot.config import settings

log = logging.getLogger(__name__)

_RULE_RE = re.compile(r"^(\d{1,2}):(\d{2})-(\d{1,2}):(\d{2})=(\d+(?:\.\d+)?[KkMm]?)$")
MAX_RULES = 8
CHECK_INTERVAL_SECONDS = 30


@dataclass(frozen=True)
class Rule:
    start: time
    end: time
    limit: str

    def covers(self, now: time) -> bool:
        if self.start == self.end:
            return True                       # 00:00-00:00 之类：全天
        if self.start < self.end:
            return self.start <= now < self.end
        return now >= self.start or now < self.end   # 跨午夜

    def __str__(self) -> str:
        return f"{self.start:%H:%M}-{self.end:%H:%M}={self.limit}"


def _to_time(h: str, m: str) -> time:
    hh, mm = int(h), int(m)
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        raise ValueError(f"时间不合法：{h}:{m}")
    return time(hh, mm)


def parse(text: str) -> list[Rule]:
    """解析规则；空串 = 没有规则。格式错误抛 ValueError（信息给管理员看）。"""
    rules = []
    for part in re.split(r"[;；\n]", text or ""):
        part = part.strip()
        if not part:
            continue
        m = _RULE_RE.match(part.replace(" ", ""))
        if not m:
            raise ValueError(f"看不懂「{part}」，格式应为 23:00-07:00=0 或 07:00-23:00=2M")
        rules.append(Rule(_to_time(m[1], m[2]), _to_time(m[3], m[4]), m[5].upper()))
    if len(rules) > MAX_RULES:
        raise ValueError(f"最多 {MAX_RULES} 条规则")
    return rules


def limit_at(rules: list[Rule], now: time) -> str:
    """此刻应该生效的限速；多条规则重叠时靠前的优先；都不匹配则不限速。"""
    for rule in rules:
        if rule.covers(now):
            return rule.limit
    return "0"


def describe(rules: list[Rule], now: time | None = None) -> str:
    if not rules:
        return "未设置（全天按设置页里的全局限速）"
    lines = [f"• {r.start:%H:%M}–{r.end:%H:%M}　{'不限速' if r.limit == '0' else r.limit + '/s'}" for r in rules]
    lines.append("• 其余时间　不限速")
    if now is not None:
        cur = limit_at(rules, now)
        lines.append(f"\n此刻生效：{'不限速' if cur == '0' else cur + '/s'}")
    return "\n".join(lines)


async def schedule_loop(aria2):
    """每 30 秒看一眼：期望的限速变了才真的去改 aria2（所以管理员在时段内手动改
    的限速会保持到下一次切换时段，不会被每半分钟覆盖一次）。"""
    last_applied: str | None = None
    last_rules: str | None = None
    complained: str | None = None
    while True:
        try:
            raw = settings.speed_schedule
            if raw != last_rules:             # 规则改了：重新应用
                last_rules, last_applied = raw, None
            rules = parse(raw)
            if rules:
                want = limit_at(rules, datetime.now().time())
                if want != last_applied:
                    await aria2.set_global_limit(want)
                    last_applied = want
                    log.info("speed schedule: global limit -> %s", want)
        except asyncio.CancelledError:
            raise
        except ValueError:
            if complained != settings.speed_schedule:     # 同一个坏值只记一次，别每 30 秒刷日志
                complained = settings.speed_schedule
                log.warning("invalid SPEED_SCHEDULE, ignored: %r", complained)
        except Exception:
            log.exception("speed schedule iteration failed")   # aria2 暂时连不上：下一轮重试
        await asyncio.sleep(CHECK_INTERVAL_SECONDS)
