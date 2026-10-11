"""软件授权（Pro）：用 Ed25519 签名的授权码，离线校验，不需要联网。

做法
- 发行方保管私钥（tools/license.py keygen 生成，**不要提交进仓库**），把对应的公钥填到
  下面的 PUBLIC_KEY_B64 再发布。授权码 = 签名过的 JSON：授权给谁、绑定哪个机器人、
  什么时候到期、最多多少个 VIP。
- 买家用 /license 看到自己的 Bot ID，发给发行方；发行方用 tools/license.py issue 签发；
  买家（管理员）发 /license <授权码> 激活。授权码绑定 Bot ID，不能拿去给别的机器人用。
- PUBLIC_KEY_B64 为空 = 不启用授权校验，所有功能直接可用（源码默认就是这样，fork 出去
  自己用不受影响）。

诚实的说明：这是开源项目，校验代码就在仓库里，有能力的人改掉它是几分钟的事。
授权校验只能挡住普通用户、让"正规渠道"有据可依，不能当作防破解手段。
"""
from __future__ import annotations

import base64
import binascii
import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

from bot.config import settings

log = logging.getLogger(__name__)

# 发行方的公钥（Ed25519，32 字节，base64）。留空 = 不启用授权校验。
PUBLIC_KEY_B64 = ""

PREFIX = "TGA2"
VERSION = 1


class LicenseError(Exception):
    """能直接给管理员看的原因。"""


@dataclass(frozen=True)
class LicenseInfo:
    name: str
    bot_id: str          # "*" = 不绑定
    expires_at: float    # 0 = 永久
    max_vip: int         # 0 = 不限
    issued_at: float

    @property
    def days_left(self) -> float | None:
        return None if not self.expires_at else (self.expires_at - time.time()) / 86400


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode().rstrip("=")


def enforced() -> bool:
    return bool(PUBLIC_KEY_B64)


def bot_id() -> str:
    """机器人 token 冒号前面的数字部分——不是秘密，授权码绑定它。"""
    return settings.bot_token.split(":", 1)[0]


def verify(token: str, *, now: float | None = None) -> LicenseInfo:
    """校验授权码，通过返回内容，否则抛 LicenseError。"""
    if not enforced():
        raise LicenseError("这个版本没有启用授权校验。")
    now = time.time() if now is None else now
    parts = (token or "").strip().split(".")
    if len(parts) != 3 or parts[0] != PREFIX:
        raise LicenseError("授权码格式不对。")
    try:
        payload_raw, sig = _b64d(parts[1]), _b64d(parts[2])
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        Ed25519PublicKey.from_public_bytes(_b64d(PUBLIC_KEY_B64)).verify(sig, payload_raw)
        data = json.loads(payload_raw)
    except (binascii.Error, ValueError):
        raise LicenseError("授权码格式不对。") from None
    except InvalidSignature:
        raise LicenseError("授权码签名无效（可能被改过，或不是这个发行方签发的）。") from None
    except ImportError:
        raise LicenseError("缺少 cryptography 库，无法校验授权码（pip install cryptography）。") from None
    if not isinstance(data, dict) or data.get("v") != VERSION:
        raise LicenseError("授权码版本不支持，请向发行方换一个新的。")
    try:
        info = LicenseInfo(
            name=str(data.get("name", ""))[:80], bot_id=str(data.get("bot_id", "")),
            expires_at=float(data.get("exp", 0)), max_vip=int(data.get("max_vip", 0)),
            issued_at=float(data.get("iat", 0)),
        )
    except (TypeError, ValueError):
        raise LicenseError("授权码内容不完整。") from None
    if info.bot_id not in ("*", bot_id()):
        raise LicenseError(f"这个授权码是签发给别的机器人的（Bot ID {info.bot_id}），当前机器人是 {bot_id()}。")
    if info.expires_at and info.expires_at <= now:
        raise LicenseError("授权码已过期。")
    return info


# ---------------------------------------------------------------- 存取

def key_path() -> Path:
    return Path(settings.db_path).parent / "license.key"


def load_token() -> str:
    """优先用 .env 的 LICENSE_KEY，其次 data/license.key。"""
    if settings.license_key.strip():
        return settings.license_key.strip()
    try:
        return key_path().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_token(token: str):
    path = key_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(token.strip(), encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def current() -> LicenseInfo | None:
    """当前有效的授权；没有、无效、过期都返回 None。（校验是纯计算，很便宜，不缓存，
    这样授权到期、替换授权码都能立刻生效。）"""
    if not enforced():
        return None
    token = load_token()
    if not token:
        return None
    try:
        return verify(token)
    except LicenseError:
        return None


def pro_active() -> bool:
    """Pro 功能（VIP 等级等）现在能不能用。"""
    return (not enforced()) or current() is not None


def status_text() -> str:
    if not enforced():
        return "ℹ️ 这个版本没有启用授权校验，所有功能可用。"
    token = load_token()
    if not token:
        return "🔒 <b>未授权</b>（免费版）：VIP 等级等 Pro 功能不可用。"
    try:
        info = verify(token)
    except LicenseError as e:
        return f"🔒 <b>授权无效</b>：{e}"
    left = "永久" if not info.expires_at else f"剩余 {max(0, int(info.days_left))} 天"
    vip = "不限" if not info.max_vip else f"最多 {info.max_vip} 个"
    return f"✅ <b>Pro 已授权</b>\n授权给：{info.name or '—'}\n有效期：{left}\nVIP 人数：{vip}"
