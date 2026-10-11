"""机器人反复崩溃重启的告警。

systemd 会把崩溃的服务一次次拉起来，管理员平时根本不知道它在循环崩溃。每次启动记一条
时间戳到 data/starts.json；10 分钟内启动 3 次以上，就在这次成功起来之后私聊管理员，
同一轮崩溃循环 30 分钟内只提醒一次。
"""
import json
import logging
import os
import time
from pathlib import Path

from bot.config import settings

log = logging.getLogger(__name__)

WINDOW_SECONDS = 600
THRESHOLD = 3
ALERT_COOLDOWN_SECONDS = 1800


def _path() -> Path:
    return Path(settings.db_path).parent / "starts.json"


def record_start(now: float | None = None) -> bool:
    """记录一次启动；返回这次是否应该发"反复重启"告警。"""
    now = time.time() if now is None else now
    path = _path()
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        starts = [float(t) for t in state.get("starts", []) if now - float(t) < WINDOW_SECONDS]
        last_alert = float(state["last_alert"]) if state.get("last_alert") is not None else float("-inf")
    except (OSError, ValueError, AttributeError, TypeError):
        starts, last_alert = [], float("-inf")
    starts.append(now)
    alert = len(starts) >= THRESHOLD and now - last_alert > ALERT_COOLDOWN_SECONDS
    if alert:
        last_alert = now
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps({"starts": starts, "last_alert": last_alert if last_alert != float("-inf") else None}), encoding="utf-8")
        os.replace(tmp, path)
    except OSError:
        log.warning("could not record the start time")
    return alert and settings.restart_alert_enabled


async def alert_restart_loop(bot, count_hint: int = THRESHOLD):
    text = (
        "🔁 <b>机器人在反复重启</b>\n\n"
        f"最近 {WINDOW_SECONDS // 60} 分钟内已经启动了至少 {count_hint} 次，可能刚刚崩溃过。"
        "请在服务器上查看日志：<code>tg-aria2 logs bot</code>"
    )
    for uid in settings.admin_recipient_ids:
        try:
            await bot.send_message(chat_id=uid, text=text, parse_mode="HTML")
        except Exception:
            log.warning("could not send the restart alert to admin %s", uid)
