"""给 Telegram 用户发消息——给没有 aiogram Bot 对象的进程用（Web 后台）。

Web 后台和机器人是两个进程，Web 里没有 Bot 实例，但有 BOT_TOKEN 和 Bot API 地址，
直接调 sendMessage 就行。只做尽力而为：发不出去（用户没和机器人说过话、拉黑了、Bot API 没起）
返回 False，不抛异常。token 绝不写进日志。
"""
import logging

import aiohttp

from bot.config import settings

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 10


async def post_json(url: str, payload: dict) -> int:
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_SECONDS)
    async with aiohttp.ClientSession(timeout=timeout) as session, session.post(url, json=payload) as resp:
        return resp.status


async def send_message(chat_id: int, text: str) -> bool:
    url = f"{settings.bot_api_url}/bot{settings.bot_token}/sendMessage"
    try:
        status = await post_json(url, {"chat_id": chat_id, "text": text})
    except (aiohttp.ClientError, TimeoutError) as e:
        # 只记异常类型：异常信息里可能带着含 token 的 URL
        log.info("could not notify %s: %s", chat_id, type(e).__name__)
        return False
    if status != 200:
        log.info("could not notify %s: Bot API returned HTTP %s", chat_id, status)
    return status == 200
