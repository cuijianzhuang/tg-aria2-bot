"""VIP 等级：更高的额度、只对 VIP 开放的功能、任务排队优先。

- VIP 用户 = vip_users 表里有一行且没过期（expires_at 为空 = 永久）。管理员不需要 VIP。
- VIP 系统是 Pro 功能：授权校验启用而又没有有效授权时（license.pro_active() 为假），整个
  VIP 系统关闭——没有人是 VIP、没有功能被限制、不排队优先，不会因为授权问题把普通用户锁在外面。
- 兑换码一次性，格式 VIP-XXXX-XXXX-XXXX（去掉容易混淆的字符），兑换失败有频率限制。
"""
from __future__ import annotations

import logging
import re
import secrets
import time

from bot.config import settings
from bot.core import license as lic

log = logging.getLogger(__name__)

FEATURES = {
    "rss": "RSS 订阅",
    "node": "切换下载节点",
    "gofile": "GoFile 自动上传",
    "autosend": "下载完成自动发到 Telegram",
}
DAY = 86400
# 去掉 0/O、1/I/L 这类容易看错的字符
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_GROUPS, CODE_GROUP_LEN = 3, 4
MAX_CODES_PER_BATCH = 20
REDEEM_MAX_FAILS = 5
REDEEM_LOCK_SECONDS = 3600

_redeem_fails: dict[int, list[float]] = {}


class VipError(Exception):
    """能直接给用户/管理员看的原因。"""


def system_active() -> bool:
    return lic.pro_active()


def gated_features() -> set[str]:
    return {f.strip().lower() for f in settings.vip_only_features.split(",")} & FEATURES.keys()


def _active(row, now: float) -> bool:
    return row is not None and (row["expires_at"] is None or row["expires_at"] > now)


async def get_status(repo, user_id: int):
    """(是否 VIP, 到期时间或 None=永久)。"""
    if not system_active():
        return False, None
    row = await repo.get_vip(user_id)
    now = time.time()
    if not _active(row, now):
        return False, None
    return True, row["expires_at"]


async def is_vip(repo, user_id: int) -> bool:
    return (await get_status(repo, user_id))[0]


async def feature_allowed(repo, user_id: int | None, feature: str) -> bool:
    """这个功能这个用户能不能用。没被设为"仅 VIP"的功能所有人可用；管理员始终可用。"""
    if feature not in gated_features() or not system_active():
        return True
    if user_id is None:
        return False
    return settings.is_admin(user_id) or await is_vip(repo, user_id)


def denied_text(feature: str) -> str:
    return f"🔒 「{FEATURES.get(feature, feature)}」仅限 VIP。发 /vip 看怎么开通。"


def vip_limits():
    from bot.core.quota import GIB, Limits
    return Limits(
        max_active=max(0, settings.vip_max_active_tasks),
        max_task_bytes=max(0, int(settings.vip_max_task_size_gb * GIB)),
        daily_bytes=max(0, int(settings.vip_daily_quota_gb * GIB)),
    )


def max_feeds(vip: bool) -> int:
    return settings.rss_max_feeds_vip if vip else settings.rss_max_feeds_per_user


async def prioritize(repo, nodes, node_name: str, gid: str, user_id: int):
    """VIP 的新任务插到等待队列最前面。失败（任务已经在下载、节点不支持）就算了。"""
    if not settings.vip_priority or not await is_vip(repo, user_id):
        return
    try:
        await nodes.get(node_name).change_position(gid, 0, "POS_SET")
    except Exception:
        log.debug("could not move task %s to the front of the queue", gid, exc_info=True)


# ---------------------------------------------------------------- 授予 / 撤销

def _check_capacity(active_count: int, already_vip: bool):
    info = lic.current()
    if info and info.max_vip and not already_vip and active_count >= info.max_vip:
        raise VipError(f"已达到授权允许的 VIP 人数上限（{info.max_vip}）。可以撤销不用的 VIP，或联系发行方升级授权。")


async def extend(repo, user_id: int, days: int, *, granted_by: int | None, note: str | None = None) -> float | None:
    """给用户加 days 天 VIP（0 = 永久），已是 VIP 则在原到期时间上顺延。返回新的到期时间（None = 永久）。"""
    if not system_active():
        raise VipError("需要 Pro 授权才能使用 VIP 功能。管理员发 /license 查看。")
    if days < 0:
        raise VipError("天数不能是负数。")
    now = time.time()
    row = await repo.get_vip(user_id)
    active = _active(row, now)
    _check_capacity(await repo.count_active_vips(now), active)
    if days == 0 or (active and row["expires_at"] is None):
        new_exp = None          # 永久；已经是永久的不会被短期码"降级"
    else:
        base = row["expires_at"] if active else now
        new_exp = base + days * DAY
    await repo.set_vip(user_id, new_exp, granted_by=granted_by, note=note)
    return new_exp


# ---------------------------------------------------------------- 兑换码

def normalize_code(text: str) -> str:
    """用户可能带空格、小写、少打连字符：统一成 VIP-XXXX-XXXX-XXXX。格式不对返回空串。"""
    raw = re.sub(r"[^A-Za-z0-9]", "", text or "").upper()
    if raw.startswith("VIP"):
        raw = raw[3:]
    if len(raw) != CODE_GROUPS * CODE_GROUP_LEN or any(c not in CODE_ALPHABET for c in raw):
        return ""
    groups = [raw[i:i + CODE_GROUP_LEN] for i in range(0, len(raw), CODE_GROUP_LEN)]
    return "VIP-" + "-".join(groups)


def new_code() -> str:
    raw = "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_GROUPS * CODE_GROUP_LEN))
    return normalize_code(raw)


async def create_codes(repo, days: int, count: int, *, created_by: int, valid_days: int = 0) -> list[str]:
    if not system_active():
        raise VipError("需要 Pro 授权才能使用 VIP 功能。管理员发 /license 查看。")
    if not 1 <= count <= MAX_CODES_PER_BATCH:
        raise VipError(f"一次生成 1–{MAX_CODES_PER_BATCH} 个。")
    if days < 0:
        raise VipError("天数不能是负数。")
    codes = [new_code() for _ in range(count)]
    expires = time.time() + valid_days * DAY if valid_days else None
    await repo.create_vip_codes(codes, days=days, created_by=created_by, expires_at=expires)
    return codes


def _locked(user_id: int, now: float) -> bool:
    recent = [t for t in _redeem_fails.get(user_id, []) if now - t < REDEEM_LOCK_SECONDS]
    _redeem_fails[user_id] = recent
    return len(recent) >= REDEEM_MAX_FAILS


async def redeem(repo, user_id: int, text: str) -> float | None:
    """兑换。成功返回新的到期时间（None = 永久），失败抛 VipError。"""
    if not system_active():
        raise VipError("当前不能兑换：机器人没有 Pro 授权，请联系管理员。")
    now = time.time()
    if _locked(user_id, now):
        raise VipError("输错次数太多了，请 1 小时后再试。")
    code = normalize_code(text)
    row = await repo.get_vip_code(code) if code else None
    if row is None or row["redeemed_by"] is not None or (row["expires_at"] and row["expires_at"] <= now):
        _redeem_fails.setdefault(user_id, []).append(now)
        raise VipError("兑换码无效、已被使用或已过期。")
    # 名额满了就别把码烧掉
    existing = await repo.get_vip(user_id)
    _check_capacity(await repo.count_active_vips(now), _active(existing, now))
    if not await repo.claim_vip_code(code, user_id, now):
        _redeem_fails.setdefault(user_id, []).append(now)
        raise VipError("兑换码无效、已被使用或已过期。")
    try:
        return await extend(repo, user_id, row["days"], granted_by=user_id, note=f"兑换码 {code}")
    except Exception:
        await repo.unclaim_vip_code(code)       # 失败就把码还回去，不让用户白白损失
        raise


def reset_rate_limits():
    _redeem_fails.clear()


# ---------------------------------------------------------------- 到期提醒

NOTICE_INTERVAL_SECONDS = 3600


async def send_notices(bot, repo, task_manager=None) -> tuple[int, int]:
    """VIP 快到期 / 已到期提醒（各只发一次，续期后重置）；授权快到期提醒管理员。
    返回 (快到期通知数, 已到期通知数)。"""
    if not system_active():
        return 0, 0
    now = time.time()
    soon, expired = await repo.vips_needing_notice(now, settings.vip_warn_days * DAY)
    for row in soon:
        days = max(1, round((row["expires_at"] - now) / DAY))
        if await _try_send(bot, row["user_id"], f"⏰ 你的 VIP 将在约 {days} 天后到期。需要续期请联系管理员或 /redeem 兑换码。"):
            await repo.mark_vip_notified(row["user_id"], expired=False)
    for row in expired:
        if await _try_send(bot, row["user_id"], "👑 你的 VIP 已到期，额度和功能已恢复为普通用户。续期请联系管理员或 /redeem 兑换码。"):
            await repo.mark_vip_notified(row["user_id"], expired=True)
    if expired and task_manager is not None:
        task_manager.invalidate_limits()
    return len(soon), len(expired)


async def _try_send(bot, user_id: int, text: str) -> bool:
    """发通知。返回值表示"这条通知算处理完了"——发不出去（用户拉黑了机器人）也算，免得每小时重试。"""
    try:
        await bot.send_message(chat_id=user_id, text=text)
        return True
    except Exception:
        log.info("could not send VIP notice to %s", user_id)
        return True    # 用户拉黑了机器人之类的：不要每小时重试，当作已通知


async def notice_loop(bot, repo, task_manager=None, *, first_delay: float = 90.0):
    import asyncio
    await asyncio.sleep(first_delay)
    while True:
        try:
            await send_notices(bot, repo, task_manager)
            await _license_notice(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("vip notice loop failed")
        await asyncio.sleep(NOTICE_INTERVAL_SECONDS)


async def _license_notice(bot):
    """授权剩 7 天、以及到期当天各提醒管理员一次（按授权到期时间去重，换了新授权后重新计）。"""
    import json
    from pathlib import Path
    if not lic.enforced():
        return
    path = Path(settings.db_path).parent / "license_notice.json"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        state = {}
    token = lic.load_token()
    if not token:
        return
    try:
        info = lic.verify(token)
    except lic.LicenseError:
        info = None
    if info is not None:
        if not info.expires_at or info.days_left is None or info.days_left > 7 or state.get("warned") == info.expires_at:
            return
        text = f"⏰ Pro 授权将在约 {max(1, int(info.days_left))} 天后到期，到期后 VIP 功能会关闭。请联系发行方续期。"
        state["warned"] = info.expires_at
    else:
        key = token[-16:]
        if state.get("expired") == key:
            return
        text = "🔒 Pro 授权已失效或过期，VIP 功能已关闭（普通功能不受影响）。续期后发 /license 授权码 激活。"
        state["expired"] = key
    for admin_id in settings.admin_recipient_ids:
        await _try_send(bot, admin_id, text)
    try:
        path.write_text(json.dumps(state), encoding="utf-8")
    except OSError:
        pass
