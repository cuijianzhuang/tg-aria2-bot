import asyncio
import logging

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import settings
from bot.core.cards import render_server_status, render_settings
from bot.core.conf_editor import aria2_conf_path, read_kv, script_conf_path, write_kv
from bot.core.keyboards import server_status_keyboard, settings_keyboard
from bot.core.sysinfo import collect_system_status
from bot.middlewares.auth import AdminMiddleware

log = logging.getLogger(__name__)
router = Router(name="admin")

# Everything in this router is admin-level (whitelist management, service
# restarts, gofile/rclone config) — gate the whole router, not per-handler.
router.message.middleware(AdminMiddleware())
router.callback_query.middleware(AdminMiddleware())

BACK_TO_SETTINGS = InlineKeyboardButton(text="⬅️ 返回设置", callback_data="nav:settings")


async def _settings_view(aria2) -> tuple[str, InlineKeyboardMarkup]:
    try:
        opts = await aria2.get_global_options()
    except Exception:
        opts = {}
    return (
        render_settings(opts.get("max-overall-download-limit"), opts.get("max-concurrent-downloads")),
        settings_keyboard(),
    )


@router.message(Command("settings"))
@router.message(Command("admin"))  # legacy shortcut — admin menu merged into ⚙️ 设置
async def cmd_admin(message: Message, aria2):
    text, kb = await _settings_view(aria2)
    await message.reply(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data == "admin:menu")
async def show_menu(query: CallbackQuery, aria2):
    """Legacy alias for buttons on old messages — routes to the merged settings hub."""
    text, kb = await _settings_view(aria2)
    await query.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await query.answer()


# ---------- 白名单管理 ----------

async def _render_users(repo) -> tuple[str, InlineKeyboardMarkup]:
    env_ids = sorted(settings.allowed_ids)
    db_rows = await repo.list_allowed_users()

    lines = ["👥 <b>白名单用户</b>", "", "<i>来自 .env（不可在此删除）</i>"]
    lines += [f"  {uid}" for uid in env_ids] or ["  (无)"]
    lines.append("")
    lines.append("<i>可管理</i>")

    kb = []
    if db_rows:
        for row in db_rows:
            note = f"（{row['note']}）" if row["note"] else ""
            lines.append(f"  {row['user_id']}{note}")
            kb.append([InlineKeyboardButton(
                text=f"🗑 删除 {row['user_id']}", callback_data=f"admin:deluser:{row['user_id']}",
            )])
    else:
        lines.append("  (无)")
    lines.append("")
    lines.append("用 <code>/adduser 数字ID 备注（可选）</code> 添加")

    kb.append([BACK_TO_SETTINGS])
    return "\n".join(lines), InlineKeyboardMarkup(inline_keyboard=kb)


@router.callback_query(F.data == "admin:users")
async def show_users(query: CallbackQuery, repo):
    text, markup = await _render_users(repo)
    await query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    await query.answer()


@router.callback_query(F.data.startswith("admin:deluser:"))
async def del_user(query: CallbackQuery, repo):
    # callback_data is client-supplied and forgeable — never trust it to parse
    try:
        user_id = int(query.data.split(":", 2)[2])
    except ValueError:
        await query.answer("无效的用户 ID", show_alert=True)
        return
    await repo.remove_allowed_user(user_id)
    await query.answer("已删除")
    text, markup = await _render_users(repo)
    await query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")


@router.message(Command("adduser"))
async def cmd_adduser(message: Message, command: CommandObject, repo):
    if not command.args:
        await message.reply("用法: /adduser 数字ID 备注（可选）")
        return
    parts = command.args.split(maxsplit=1)
    try:
        user_id = int(parts[0])
    except ValueError:
        await message.reply("ID 必须是数字")
        return
    note = parts[1] if len(parts) > 1 else None
    await repo.add_allowed_user(user_id, note)
    await message.reply(f"✅ 已添加 {user_id}")


@router.message(Command("removeuser"))
async def cmd_removeuser(message: Message, command: CommandObject, repo):
    if not command.args:
        await message.reply("用法: /removeuser 数字ID")
        return
    try:
        user_id = int(command.args.strip())
    except ValueError:
        await message.reply("ID 必须是数字")
        return
    if user_id in settings.allowed_ids:
        await message.reply("该用户来自 .env，无法在此删除")
        return
    await repo.remove_allowed_user(user_id)
    await message.reply(f"✅ 已删除 {user_id}")


# ---------- GoFile 设置（同一进程内直接生效，不用重启） ----------

def _gofile_text() -> str:
    on, off = "✅", "❌"
    return (
        "☁️ <b>GoFile 设置</b>\n\n"
        f"启用: {on if settings.gofile_enabled else off}\n"
        f"上传前压缩: {on if settings.gofile_compress else off}\n"
        f"上传后删除本地: {on if settings.gofile_delete_local else off}\n\n"
        "<i>token 涉及敏感信息，去 web 管理后台配置。</i>"
    )


def _gofile_menu() -> InlineKeyboardMarkup:
    def label(text, flag):
        return f"{text}: {'✅' if flag else '❌'}"

    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=label("启用", settings.gofile_enabled), callback_data="admin:gofile:t:enabled")],
        [InlineKeyboardButton(text=label("压缩", settings.gofile_compress), callback_data="admin:gofile:t:compress")],
        [InlineKeyboardButton(text=label("删本地", settings.gofile_delete_local), callback_data="admin:gofile:t:delete_local")],
        [BACK_TO_SETTINGS],
    ])


@router.callback_query(F.data == "admin:gofile")
async def show_gofile(query: CallbackQuery):
    await query.message.edit_text(_gofile_text(), reply_markup=_gofile_menu(), parse_mode="HTML")
    await query.answer()


GOFILE_TOGGLE_FIELDS = {"enabled", "compress", "delete_local"}


@router.callback_query(F.data.startswith("admin:gofile:t:"))
async def toggle_gofile(query: CallbackQuery):
    field = query.data.split(":")[3]
    # forged callback_data could otherwise setattr() arbitrary gofile_* fields
    # (e.g. clobber gofile_token with a bool and persist it to .env)
    if field not in GOFILE_TOGGLE_FIELDS:
        await query.answer("未知设置项", show_alert=True)
        return
    attr = f"gofile_{field}"
    new_value = not getattr(settings, attr)
    setattr(settings, attr, new_value)  # same process as task_manager.py — takes effect immediately
    write_kv(".env", attr.upper(), "true" if new_value else "false")

    await query.answer("已更新，立即生效")
    await query.message.edit_text(_gofile_text(), reply_markup=_gofile_menu(), parse_mode="HTML")


# ---------- rclone 设置（钩子脚本，需要重启 aria2 才生效） ----------

def _rclone_enabled() -> bool:
    on_complete = read_kv(aria2_conf_path(), "on-download-complete") or ""
    return on_complete.strip().endswith("upload.sh")


def _rclone_text() -> str:
    drive_name = read_kv(script_conf_path(), "drive-name") or "(未设置)"
    return (
        "📁 <b>rclone 设置</b>\n\n"
        f"启用: {'✅' if _rclone_enabled() else '❌'}\n"
        f"网盘: {drive_name}\n\n"
        "<i>网盘名称/目录去 web 管理后台配置。</i>"
    )


def _rclone_menu() -> InlineKeyboardMarkup:
    enabled = _rclone_enabled()
    return InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="关闭自动上传" if enabled else "启用自动上传", callback_data="admin:rclone:toggle")],
        [BACK_TO_SETTINGS],
    ])


@router.callback_query(F.data == "admin:rclone")
async def show_rclone(query: CallbackQuery):
    await query.message.edit_text(_rclone_text(), reply_markup=_rclone_menu(), parse_mode="HTML")
    await query.answer()


@router.callback_query(F.data == "admin:rclone:toggle")
async def toggle_rclone(query: CallbackQuery):
    hook = settings.aria2_clean_hook if _rclone_enabled() else settings.aria2_upload_hook
    write_kv(aria2_conf_path(), "on-download-complete", hook)
    await query.answer("已切换，需要重启 aria2 才生效")
    await query.message.edit_text(_rclone_text(), reply_markup=_rclone_menu(), parse_mode="HTML")


# ---------- 服务器状态 ----------

async def _server_status_view(aria2) -> str:
    # sampling sleeps ~0.3s inside — keep it off the event loop
    info = await asyncio.to_thread(collect_system_status, settings.download_dir)
    try:
        stats = await aria2.global_stat()
    except Exception:
        stats = None
    return render_server_status(info, stats)


@router.callback_query(F.data == "admin:sysinfo")
async def show_sysinfo(query: CallbackQuery, aria2):
    await query.answer("正在采集…")  # sampling takes a moment; ack the tap first
    text = await _server_status_view(aria2)
    try:
        await query.message.edit_text(text, reply_markup=server_status_keyboard(), parse_mode="HTML")
    except TelegramBadRequest as e:
        # double-tapped 刷新 within the same second: card text (incl. timestamp)
        # is identical, Telegram rejects the no-op edit — nothing to do
        if "message is not modified" not in str(e):
            raise


@router.message(Command("server"))
async def cmd_server(message: Message, aria2):
    text = await _server_status_view(aria2)
    await message.reply(text, reply_markup=server_status_keyboard(), parse_mode="HTML")


# ---------- 重启服务 ----------

@router.callback_query(F.data == "admin:restart")
async def show_restart(query: CallbackQuery):
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🔄 重启 aria2", callback_data="admin:restart:aria2")],
        [InlineKeyboardButton(text="🔄 重启机器人", callback_data="admin:restart:bot")],
        [BACK_TO_SETTINGS],
    ])
    await query.message.edit_text("🔄 <b>重启服务</b>", reply_markup=kb, parse_mode="HTML")
    await query.answer()


async def _fire_restart(unit: str):
    try:
        await asyncio.create_subprocess_exec("systemctl", "restart", unit)
    except FileNotFoundError:
        pass  # docker mode: no systemctl in this container; nothing more we can do


@router.callback_query(F.data.startswith("admin:restart:"))
async def do_restart(query: CallbackQuery):
    which = query.data.split(":")[2]
    unit = settings.aria2_service_name if which == "aria2" else settings.bot_service_name
    await query.answer("正在重启…")

    if which == "bot":
        # restarting ourselves — reply first, then fire the restart without
        # awaiting it (this process gets SIGTERM shortly after either way)
        await query.message.edit_text("🔄 正在重启机器人，几秒后会自动重连…")
        asyncio.create_task(_fire_restart(unit))
        return

    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "restart", unit,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
    except FileNotFoundError:
        await query.message.edit_text(
            "此环境没有 systemctl（大概率是 docker 部署），请手动执行：\n"
            f"<code>docker compose restart {which}</code>",
            parse_mode="HTML",
        )
        return

    if proc.returncode != 0:
        await query.message.edit_text(f"❌ 重启失败: {stderr.decode(errors='replace')[:300]}")
        return
    await query.message.edit_text(f"✅ {which} 已重启")
