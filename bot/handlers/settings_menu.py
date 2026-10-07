"""⚙️ 设置页：全局限速、同时下载数、单文件上限、下载目录、自动清理、通知
/自动发送开关。

这些全是作用于整个机器人的全局配置，单独成一个 router 并整体挂上
AdminMiddleware——以前它们和任务按钮混在 callbacks.py 的普通 router 里，
`/settings` 命令本身是管理员专属，但任何白名单用户点主菜单的「⚙️ 设置」
按钮照样能进来改全局限速、切下载目录、触发清理记录。
"""
import logging
import os

from aiogram import F, Router
from aiogram.types import CallbackQuery

from bot.config import settings
from bot.core.cards import (
    render_cleanup_chooser,
    render_concurrent_chooser,
    render_dir_chooser,
    render_limit_chooser,
    render_maxsize_chooser,
    render_settings,
)
from bot.core.conf_editor import write_kv
from bot.core.keyboards import (
    CLEANUP_PRESETS,
    CONCURRENT_PRESETS,
    LIMIT_PRESETS,
    MAXSIZE_PRESETS,
    cleanup_chooser_keyboard,
    concurrent_chooser_keyboard,
    dir_chooser_keyboard,
    limit_chooser_keyboard,
    maxsize_chooser_keyboard,
    settings_keyboard,
)
from bot.handlers.callbacks import _edit
from bot.middlewares.auth import AdminMiddleware

log = logging.getLogger(__name__)
router = Router(name="settings_menu")
router.callback_query.middleware(AdminMiddleware())


async def _settings_data(aria2) -> tuple[str | None, str | None]:
    """(max-overall-download-limit, max-concurrent-downloads) straight from
    aria2 — the live values, not whatever .env said at boot."""
    try:
        opts = await aria2.get_global_options()
    except Exception:
        opts = {}
    return opts.get("max-overall-download-limit"), opts.get("max-concurrent-downloads")


async def _show_settings(query: CallbackQuery, aria2):
    limit_raw, concurrent_raw = await _settings_data(aria2)
    await _edit(query, render_settings(limit_raw, concurrent_raw), reply_markup=settings_keyboard(), parse_mode="HTML")


def _persist_env(key: str, value: str):
    """Best-effort .env write-back so the choice survives a bot restart; a
    missing .env (e.g. env vars injected some other way) is not an error."""
    try:
        write_kv(".env", key, value)
    except OSError:
        log.warning("could not persist %s to .env", key)


@router.callback_query(F.data == "nav:settings")
async def nav_settings(query: CallbackQuery, aria2):
    await _show_settings(query, aria2)


@router.callback_query(F.data == "settings:limit")
async def settings_limit(query: CallbackQuery, aria2):
    try:
        limit_raw = await aria2.get_global_limit()
    except Exception:
        limit_raw = None
    await _edit(
        query, render_limit_chooser(limit_raw), reply_markup=limit_chooser_keyboard(limit_raw), parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("setlimit:"))
async def apply_limit(query: CallbackQuery, aria2):
    value = query.data.split(":", 1)[1]
    if value not in {v for _, v in LIMIT_PRESETS}:
        await query.answer("无效的限速值", show_alert=True)
        return
    try:
        await aria2.set_global_limit(value)
    except Exception:
        log.exception("failed to set global limit")
        await query.answer("设置失败，请稍后再试", show_alert=True)
        return
    await query.answer("✅ 已生效" if value != "0" else "✅ 已取消限速")
    await _show_settings(query, aria2)


@router.callback_query(F.data == "settings:concurrent")
async def settings_concurrent(query: CallbackQuery, aria2):
    _, concurrent_raw = await _settings_data(aria2)
    await _edit(
        query, render_concurrent_chooser(concurrent_raw),
        reply_markup=concurrent_chooser_keyboard(concurrent_raw), parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("setconcurrent:"))
async def apply_concurrent(query: CallbackQuery, aria2):
    value = query.data.split(":", 1)[1]
    if value not in CONCURRENT_PRESETS:
        await query.answer("无效的数量", show_alert=True)
        return
    try:
        await aria2.set_max_concurrent(int(value))
    except Exception:
        log.exception("failed to set max-concurrent-downloads")
        await query.answer("设置失败，请稍后再试", show_alert=True)
        return
    settings.max_concurrent = int(value)
    _persist_env("MAX_CONCURRENT", value)  # re-applied to aria2 on bot startup
    await query.answer("✅ 已生效")
    await _show_settings(query, aria2)


@router.callback_query(F.data == "settings:notify")
async def toggle_notify(query: CallbackQuery, aria2):
    settings.notify_on_complete = not settings.notify_on_complete
    _persist_env("NOTIFY_ON_COMPLETE", "true" if settings.notify_on_complete else "false")
    await query.answer("🔔 完成通知已开启" if settings.notify_on_complete else "🔕 完成通知已关闭")
    await _show_settings(query, aria2)


@router.callback_query(F.data == "settings:maxsize")
async def settings_maxsize(query: CallbackQuery):
    current_mb = str(settings.max_file_size // (1024 * 1024))
    await _edit(
        query, render_maxsize_chooser(settings.max_file_size),
        reply_markup=maxsize_chooser_keyboard(current_mb), parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("setmaxsize:"))
async def apply_maxsize(query: CallbackQuery, aria2):
    value = query.data.split(":", 1)[1]
    presets = {v for _, v in MAXSIZE_PRESETS}
    if value not in presets:
        await query.answer("无效的大小", show_alert=True)
        return
    # "0" 约定为不限；其余预设单位是 MB，换算成字节存进 settings
    settings.max_file_size = int(value) * 1024 * 1024 if value != "0" else 0
    _persist_env("MAX_FILE_SIZE", str(settings.max_file_size))
    await query.answer("✅ 已生效")
    await _show_settings(query, aria2)


@router.callback_query(F.data == "settings:cleanup")
async def settings_cleanup(query: CallbackQuery):
    await _edit(
        query, render_cleanup_chooser(),
        reply_markup=cleanup_chooser_keyboard(settings.auto_cleanup_days), parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("setcleanup:"))
async def apply_cleanup(query: CallbackQuery, aria2, task_manager):
    value = query.data.split(":", 1)[1]
    presets = {v for _, v in CLEANUP_PRESETS}
    if value not in presets:
        await query.answer("无效的天数", show_alert=True)
        return
    settings.auto_cleanup_days = int(value)
    _persist_env("AUTO_CLEANUP_DAYS", value)
    # 立即按新设置跑一次，不用等下一个 24 小时周期才看到效果
    deleted = await task_manager.run_cleanup_once()
    toast = "✅ 已关闭自动清理" if settings.auto_cleanup_days == 0 else f"✅ 已生效，本次清理了 {deleted} 条记录"
    await query.answer(toast)
    await _show_settings(query, aria2)


@router.callback_query(F.data == "settings:dir")
async def settings_dir(query: CallbackQuery):
    options = settings.download_dir_options
    await _edit(
        query, render_dir_chooser(options),
        reply_markup=dir_chooser_keyboard(options), parse_mode="HTML",
    )


@router.callback_query(F.data.startswith("setdir:"))
async def apply_dir(query: CallbackQuery, aria2):
    try:
        index = int(query.data.split(":", 1)[1])
        chosen = settings.download_dir_options[index]
    except (ValueError, IndexError):
        await query.answer("无效的目录", show_alert=True)
        return
    # 新目录此刻可能还不存在（比如刚在 .env 里配的预设），提前建好，
    # 避免用户切完目录第一次下载才发现目录不存在
    os.makedirs(chosen, exist_ok=True)
    settings.download_dir = chosen
    _persist_env("DOWNLOAD_DIR", chosen)
    await query.answer("✅ 已切换（不影响已下载文件的位置）")
    await _show_settings(query, aria2)


@router.callback_query(F.data == "settings:sendtg")
async def toggle_send_tg(query: CallbackQuery, aria2):
    settings.auto_send_to_tg = not settings.auto_send_to_tg
    _persist_env("AUTO_SEND_TO_TG", "true" if settings.auto_send_to_tg else "false")
    await query.answer("📤 自动发送已开启" if settings.auto_send_to_tg else "📤 自动发送已关闭")
    await _show_settings(query, aria2)


@router.callback_query(F.data.startswith("settings:"))
async def settings_fallback(query: CallbackQuery):
    await query.answer("该设置暂未接入。", show_alert=True)
