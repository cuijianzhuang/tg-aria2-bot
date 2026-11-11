"""Private Telegram notifications for QR state changes; never send cookies."""

import asyncio
import time
from pathlib import Path

from aiogram.types import FSInputFile

from bot.config import settings
from bot.video.qr_browser import REASONS
from bot.video.ui import STATES, login_keyboard


async def notify_loop(bot, repo):
    while True:
        for row in await repo.video_login_rows():
            if row["expires"] < time.time() and row["state"] in ("queued", "preparing", "waiting", "scanned"):
                await repo.video_login_update(row["id"], state="expired")
                row = await repo.video_login(row["id"])
            if not row["chat_id"] or row["notified_state"] == row["state"]:
                continue
            if row["state"] == "queued" or (row["state"] == "waiting" and not row["qr_path"]):
                continue
            qr_file = Path(settings.db_path).parent / "video-private" / "qr" / (row["id"] + ".png")
            try:
                text = f"🔑 {row['platform']} · {'共享账号' if row['owner_id'] == 0 else '个人账号'}\n{STATES.get(row['state'], '处理中')}"
                if row["state"] == "failed" and dict(row).get("failure_reason") in REASONS:
                    text += "\n⚠️ " + REASONS[row["failure_reason"]]
                if row["qr_path"] and qr_file.is_file() and row["state"] == "waiting":
                    message = await bot.send_photo(
                        row["chat_id"],
                        FSInputFile(qr_file),
                        caption=text,
                        reply_markup=login_keyboard(row["id"]),
                    )
                    await repo.video_login_update(row["id"], message_id=message.message_id)
                else:
                    await bot.send_message(
                        row["chat_id"],
                        text,
                        reply_markup=login_keyboard(row["id"]) if row["state"] != "success" else None,
                    )
                if (
                    row["state"] in ("success", "cancelled", "expired", "failed", "verification_required")
                    and row["message_id"]
                ):
                    try:
                        await bot.delete_message(row["chat_id"], row["message_id"])
                    except Exception:
                        pass
                await repo.video_login_update(row["id"], notified_state=row["state"])
            except Exception:
                pass
        await asyncio.sleep(2)
