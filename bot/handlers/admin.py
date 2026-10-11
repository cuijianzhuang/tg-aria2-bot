import asyncio
import logging
from html import escape

from aiogram import F, Router
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandObject
from aiogram.types import CallbackQuery, InlineKeyboardButton, InlineKeyboardMarkup, Message

from bot.config import settings
from bot.core import quota
from bot.core.aria2_client import Aria2Client
from bot.core.cards import render_node_manage, render_server_status, render_settings
from bot.core.conf_editor import aria2_conf_path, read_kv, script_conf_path, write_kv
from bot.core.keyboards import node_manage_keyboard, server_status_keyboard, settings_keyboard
from bot.core.node_pool import DEFAULT_NODE, MAX_NODE_NAME_BYTES
from bot.core.sysinfo import collect_system_status
from bot.handlers.access import who_html
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
    pending = await repo.list_pending_requests()

    lines = ["👥 <b>白名单用户</b>", ""]
    kb = []
    if pending:
        # 没来得及点通知消息的申请，在这里也能处理
        lines.append(f"🙋 <b>待处理的申请（{len(pending)}）</b>")
        for req in pending:
            lines.append(f"  <code>{req['user_id']}</code> · {who_html(req['username'], req['full_name'])}")
            kb.append([
                InlineKeyboardButton(text=f"✅ 通过 {req['user_id']}", callback_data=f"access:pok:{req['user_id']}"),
                InlineKeyboardButton(text=f"❌ 拒绝 {req['user_id']}", callback_data=f"access:pno:{req['user_id']}"),
            ])
        lines.append("")
    lines.append("<i>来自 .env（不可在此删除）</i>")
    lines += [f"  <code>{uid}</code>" for uid in env_ids] or ["  (无)"]
    lines.append("")
    lines.append("<i>可管理</i>")

    if db_rows:
        for row in db_rows:
            name = who_html(row["username"], row["full_name"]) if (row["username"] or row["full_name"]) else ""
            note = f"（{escape(row['note'], quote=False)}）" if row["note"] else ""
            lines.append(f"  <code>{row['user_id']}</code>" + (f" · {name}" if name else "") + note)
            kb.append([InlineKeyboardButton(
                text=f"🗑 删除 {row['user_id']}", callback_data=f"admin:deluser:{row['user_id']}",
            )])
    else:
        lines.append("  (无)")
    lines.append("")
    lines.append("用 <code>/adduser 数字ID 备注（可选）</code> 添加；没权限的人点「申请使用」后会在这里和通知消息里等你处理")

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


# ---------- 使用申请：通过 / 拒绝 ----------

def _admin_name(user) -> str:
    return f"@{user.username}" if user.username else user.full_name


@router.callback_query(F.data.regexp(r"^access:(ok|no|pok|pno):\d+$"))
async def decide_access(query: CallbackQuery, repo, bot):
    _, action, uid_raw = query.data.split(":", 2)
    user_id = int(uid_raw)
    from_page = action in ("pok", "pno")
    approve = action in ("ok", "pok")

    req = await repo.get_access_request(user_id)
    if req is None:
        await query.answer("这条申请已经不存在了。", show_alert=True)
        return

    changed = await repo.decide_access_request(
        user_id, "approved" if approve else "rejected", query.from_user.id,
    )
    if not changed:
        # 另一个管理员先处理了（或者申请人已经被别处加进了白名单）
        done = {"approved": "✅ 已通过", "rejected": "❌ 已拒绝"}.get(req["status"], "已处理")
        await query.answer(f"这条申请已经处理过了：{done}", show_alert=True)
        if from_page:
            text, markup = await _render_users(repo)
            await query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
        elif query.message:
            await _finish_notice(query, f"{done}（已被其他管理员处理）")
        return

    if approve:
        await repo.add_allowed_user(user_id, "申请通过", username=req["username"], full_name=req["full_name"])
        applicant_text = "✅ 你的使用申请已通过！发送 /start 开始使用。"
        verdict = f"✅ 已通过（{escape(_admin_name(query.from_user), quote=False)}）"
    else:
        hours = settings.access_request_cooldown_hours
        applicant_text = f"❌ 你的使用申请没有通过。{hours} 小时后可以再次申请。"
        verdict = f"❌ 已拒绝（{escape(_admin_name(query.from_user), quote=False)}）"

    try:  # 申请人可能屏蔽了机器人，发不出去不影响结果
        await bot.send_message(chat_id=user_id, text=applicant_text)
    except Exception:
        log.warning("could not tell applicant %s about the decision", user_id)

    await query.answer("已通过" if approve else "已拒绝")
    if from_page:
        text, markup = await _render_users(repo)
        await query.message.edit_text(text, reply_markup=markup, parse_mode="HTML")
    elif query.message:
        await _finish_notice(query, verdict)


async def _finish_notice(query: CallbackQuery, verdict: str):
    """把申请通知消息改成最终结果，并去掉按钮。"""
    try:
        await query.message.edit_text(
            f"{query.message.html_text}\n\n{verdict}", parse_mode="HTML", reply_markup=None,
        )
    except Exception:
        log.debug("could not edit access request notice")


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
    # 这个人如果正好有待处理的申请，顺手标记为已通过，免得它还留在待处理里
    await repo.decide_access_request(user_id, "approved", message.from_user.id if message.from_user else None)
    await message.reply(f"✅ 已添加 {user_id}")


@router.message(Command("setquota"))
async def cmd_setquota(message: Message, command: CommandObject, repo, task_manager=None):
    if not command.args:
        await message.reply(quota.SETQUOTA_USAGE, parse_mode="HTML")
        return
    try:
        user_id, changes = quota.parse_setquota(command.args)
    except ValueError as e:
        await message.reply(f"⚠️ {escape(str(e), quote=False)}\n\n{quota.SETQUOTA_USAGE}", parse_mode="HTML")
        return
    if settings.is_admin(user_id):
        await message.reply("管理员不受额度限制，不需要设置。")
        return
    if changes is None:
        await repo.clear_user_quota(user_id)
    else:
        await repo.set_user_quota(user_id, changes)
    if task_manager is not None:
        task_manager.invalidate_limits()
    pending = task_manager.pending_usage(user_id) if task_manager is not None else 0
    text = await quota.render_quota(
        repo, user_id, pending_usage=pending, title=f"✅ 已更新 <code>{user_id}</code> 的额度",
    )
    await message.reply(text, parse_mode="HTML")


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


# ---------- 节点管理 ----------

async def _node_manage_view(nodes) -> tuple[str, InlineKeyboardMarkup]:
    all_nodes = nodes.all_nodes()
    healthy = {n.name: nodes.is_healthy(n.name) for n in all_nodes}
    return render_node_manage(all_nodes, healthy), node_manage_keyboard(all_nodes)


@router.callback_query(F.data == "admin:nodes")
async def show_nodes(query: CallbackQuery, nodes):
    text, kb = await _node_manage_view(nodes)
    await query.message.edit_text(text, reply_markup=kb, parse_mode="HTML")
    await query.answer()


@router.callback_query(F.data.startswith("admin:node:t:"))
async def toggle_node(query: CallbackQuery, nodes):
    name = query.data.split(":", 3)[3]
    node = nodes.get_node(name)
    if node is None or name == DEFAULT_NODE:
        await query.answer("无效的节点", show_alert=True)
        return
    await nodes.set_enabled(name, not node.enabled)
    await query.answer(f"已{'启用' if node.enabled else '停用'} {node.display_name}")
    text, kb = await _node_manage_view(nodes)
    await query.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.callback_query(F.data.startswith("admin:node:d:"))
async def delete_node_confirm(query: CallbackQuery, nodes):
    name = query.data.split(":", 3)[3]
    node = nodes.get_node(name)
    if node is None or name == DEFAULT_NODE:
        await query.answer("无效的节点", show_alert=True)
        return
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text=f"⚠️ 确认删除 {node.display_name}", callback_data=f"admin:node:dy:{name}")],
        [InlineKeyboardButton(text="⬅️ 返回节点管理", callback_data="admin:nodes")],
    ])
    await query.message.edit_text(
        f"⚠️ 确认删除节点 <b>{node.display_name}</b>？\n\n"
        "只移除节点注册信息，不影响该节点上正在跑的 aria2 和已下载的文件；"
        "它的历史任务记录会保留但无法再操作。",
        reply_markup=kb, parse_mode="HTML",
    )
    await query.answer()


@router.callback_query(F.data.startswith("admin:node:dy:"))
async def delete_node(query: CallbackQuery, nodes):
    name = query.data.split(":", 3)[3]
    if nodes.get_node(name) is None or name == DEFAULT_NODE:
        await query.answer("无效的节点", show_alert=True)
        return
    await nodes.remove(name)
    await query.answer("已删除")
    text, kb = await _node_manage_view(nodes)
    await query.message.edit_text(text, reply_markup=kb, parse_mode="HTML")


@router.message(Command("addnode"))
async def cmd_addnode(message: Message, command: CommandObject, nodes):
    usage = "用法: /addnode 名称 rpc地址 密钥 [下载目录]\n例: /addnode 群晖 http://192.168.1.5:6800/jsonrpc s3cret /volume1/downloads"
    parts = (command.args or "").split()
    if len(parts) not in (3, 4):
        await message.reply(usage)
        return
    name, rpc_url, secret = parts[0], parts[1], parts[2]
    download_dir = parts[3] if len(parts) == 4 else "/downloads"

    if name == DEFAULT_NODE or nodes.get_node(name) is not None:
        await message.reply("⛔ 节点名已存在（default 是内置本机节点）。")
        return
    # 名字要拼进 callback_data（64 字节上限），限制字节长度
    if len(name.encode()) > MAX_NODE_NAME_BYTES:
        await message.reply(f"⛔ 节点名太长（最多 {MAX_NODE_NAME_BYTES} 字节，约 10 个汉字）。")
        return
    if not rpc_url.startswith(("http://", "https://")):
        await message.reply("⛔ rpc地址必须是 http(s)://host:port/jsonrpc 形式。")
        return

    # 消息里带着 RPC 密钥，处理完立刻删掉原消息，避免密钥留在聊天记录里
    try:
        await message.delete()
        deleted_hint = ""
    except Exception:
        deleted_hint = "\n⚠️ 无法删除你发的那条命令消息，请手动删除（里面有密钥）。"

    # 入库前先探活，连不上直接拒绝，避免存入废节点
    probe = Aria2Client(rpc_url, secret)
    ok, info = await nodes.check(probe)
    if not ok:
        await message.answer(f"⛔ 连接失败，节点未添加：{info[:200]}{deleted_hint}")
        return

    await nodes.add(name=name, rpc_url=rpc_url, secret=secret, download_dir=download_dir)
    await message.answer(
        f"✅ 节点 <b>{name}</b> 已添加（aria2 {info}）\n"
        f"下载目录：<code>{download_dir}</code>\n"
        "在主菜单「🖥 节点」里切换后即可往它下载。"
        f"{deleted_hint}",
        parse_mode="HTML",
    )


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
