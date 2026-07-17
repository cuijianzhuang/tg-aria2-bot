import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import BotCommand

from bot.config import settings
from bot.core import gofile
from bot.core.node_pool import NodePool
from bot.core.task_manager import TaskManager
from bot.db.repo import TaskRepo
from bot.handlers import admin, callbacks, commands, links, media
from bot.middlewares.auth import AuthMiddleware

BOT_COMMANDS = [
    BotCommand(command="start", description="主菜单（状态总览）"),
    BotCommand(command="list", description="任务列表"),
    BotCommand(command="find", description="按文件名搜索任务"),
    BotCommand(command="stats", description="下载统计"),
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

    # 节点池：default 节点来自 .env，额外节点来自 DB（/addnode 添加）
    nodes = NodePool(repo)
    await nodes.load()
    aria2 = nodes.get("default")
    # MAX_CONCURRENT chosen in the settings menu is persisted to .env; re-apply
    # it to aria2 here since aria2 forgets runtime option changes on restart.
    # 只作用于 default 节点 —— 远程节点的全局并发由它们自己的配置管理。
    try:
        await aria2.set_max_concurrent(settings.max_concurrent)
    except Exception:
        log.warning("could not apply max_concurrent=%s to aria2 (is it up yet?)", settings.max_concurrent)
    task_manager = TaskManager(bot, nodes, repo)

    dp = Dispatcher()
    dp.message.middleware(AuthMiddleware())
    dp.callback_query.middleware(AuthMiddleware())
    dp.include_router(commands.router)
    dp.include_router(admin.router)
    dp.include_router(callbacks.router)
    dp.include_router(links.router)
    dp.include_router(media.router)

    # aria2 = default 节点客户端，供设置菜单（全局限速/并发）等只作用于本机的
    # handler 使用；任务级操作一律通过 nodes 按任务归属路由
    dp["aria2"] = aria2
    dp["nodes"] = nodes
    dp["repo"] = repo
    # 设置菜单里"立即清理一次"需要直接调用 task_manager.run_cleanup_once()
    dp["task_manager"] = task_manager

    await bot.set_my_commands(BOT_COMMANDS)
    await task_manager.reconcile_on_startup()
    await task_manager.start()

    try:
        await dp.start_polling(bot)
    finally:
        task_manager.stop()
        await repo.close()
        await bot.session.close()
        await gofile.close_session()


if __name__ == "__main__":
    asyncio.run(main())
