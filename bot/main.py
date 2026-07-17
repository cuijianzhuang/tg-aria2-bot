import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import BotCommand

from bot.config import settings
from bot.core.aria2_client import Aria2Client
from bot.core.task_manager import TaskManager
from bot.db.repo import TaskRepo
from bot.middlewares.auth import AuthMiddleware
from bot.handlers import admin, callbacks, commands, links, media

BOT_COMMANDS = [
    BotCommand(command="start", description="主菜单（状态总览）"),
    BotCommand(command="list", description="任务列表"),
    BotCommand(command="settings", description="设置与管理"),
]

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
log = logging.getLogger(__name__)


async def main():
    os.makedirs(os.path.dirname(settings.db_path), exist_ok=True)
    os.makedirs(settings.download_dir, exist_ok=True)

    # Point aiogram at the self-hosted Bot API server so the 20MB download cap is lifted.
    local_server = TelegramAPIServer.from_base(settings.bot_api_url)
    session = AiohttpSession(api=local_server)
    bot = Bot(token=settings.bot_token, session=session)

    repo = TaskRepo(settings.db_path)
    await repo.connect()

    aria2 = Aria2Client(settings.aria2_rpc, settings.aria2_secret)
    # MAX_CONCURRENT chosen in the settings menu is persisted to .env; re-apply
    # it to aria2 here since aria2 forgets runtime option changes on restart.
    try:
        await aria2.set_max_concurrent(settings.max_concurrent)
    except Exception:
        log.warning("could not apply max_concurrent=%s to aria2 (is it up yet?)", settings.max_concurrent)
    task_manager = TaskManager(bot, aria2, repo)

    dp = Dispatcher()
    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())
    dp.include_router(commands.router)
    dp.include_router(admin.router)
    dp.include_router(callbacks.router)
    dp.include_router(links.router)
    dp.include_router(media.router)

    dp["aria2"] = aria2
    dp["repo"] = repo

    await bot.set_my_commands(BOT_COMMANDS)
    await task_manager.reconcile_on_startup()
    await task_manager.start()

    try:
        await dp.start_polling(bot)
    finally:
        task_manager.stop()
        await repo.close()
        await bot.session.close()


if __name__ == "__main__":
    asyncio.run(main())
