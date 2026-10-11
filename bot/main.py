import asyncio
import logging
import os

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.client.telegram import TelegramAPIServer
from aiogram.types import BotCommand

from bot.config import settings
from bot.core import expiry, gofile, health, rss, schedule, upgrade
from bot.core.node_pool import NodePool
from bot.core.task_manager import TaskManager
from bot.db.repo import TaskRepo
from bot.handlers import access, admin, callbacks, commands, files, links, media, ops, settings_menu
from bot.handlers import rss as rss_handler
from bot.handlers import upgrade as upgrade_handler
from bot.middlewares.auth import AuthMiddleware

BOT_COMMANDS = [
    BotCommand(command="start", description="主菜单（状态总览）"),
    BotCommand(command="list", description="任务列表"),
    BotCommand(command="find", description="按文件名搜索任务"),
    BotCommand(command="stats", description="下载统计"),
    BotCommand(command="rss", description="RSS 订阅自动下载"),
    BotCommand(command="quota", description="我的额度和今日用量"),
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
    dp.include_router(access.router)
    dp.include_router(admin.router)
    dp.include_router(settings_menu.router)
    dp.include_router(upgrade_handler.router)
    dp.include_router(ops.router)
    dp.include_router(rss_handler.router)
    dp.include_router(files.router)
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

    # 在机器人里升级：升级重启后的新进程在这里接着把结果汇报给发起升级的管理员；
    # 另有一个定期检查新版本的后台任务（只通知，不会自动升级）
    background = [
        asyncio.create_task(upgrade.finish_pending_upgrade(bot)),
        asyncio.create_task(upgrade.update_check_loop(bot)),
        asyncio.create_task(schedule.schedule_loop(aria2)),
        asyncio.create_task(expiry.expiry_loop(bot, repo, nodes)),
        asyncio.create_task(rss.rss_loop(bot, repo, nodes, task_manager)),
    ]
    if health.record_start():
        background.append(asyncio.create_task(health.alert_restart_loop(bot)))

    try:
        await dp.start_polling(bot)
    finally:
        for task in background:
            task.cancel()
        task_manager.stop()
        await nodes.close()
        await repo.close()
        await bot.session.close()
        await gofile.close_session()


if __name__ == "__main__":
    asyncio.run(main())
