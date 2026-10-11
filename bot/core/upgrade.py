"""在机器人里升级：检查更新 → 管理员确认 → 起独立的升级任务 → 重启后汇报结果。

为什么升级要交给 systemd-run，而不是机器人自己 subprocess 一个脚本？
升级脚本最后会 `systemctl restart tg-aria2-bot`——把执行升级的机器人自己杀掉。
机器人的子进程和它在同一个 cgroup 里，会被一起杀掉，升级就断在半路。
`systemd-run` 起的临时服务（tg-aria2-upgrade）有自己的 cgroup，机器人重启杀不到它。

机器人被重启之后怎么把结果告诉管理员？升级前把"谁在哪条消息上发起的、升级前的
版本"写进 data/upgrade_job.json；新进程启动时看到这个文件，就等升级任务跑完（包括
升级脚本里 20 秒的健康检查——健康检查失败它会自动回滚，所以不能一启动就报成功），
再对比当前版本和升级前的版本，把结果编辑到那条消息上。升级失败、机器人没被重启的
情况走同一条路径（旧进程的 watcher 看到任务结束，对比版本发现没变 → 报失败 + 日志）。

安全：只有管理员能触发（handlers/upgrade.py 整个 router 挂 AdminMiddleware）；
命令行参数全是固定值，不拼接任何用户输入；升级前会列出将要更新的提交让管理员确认，
确认的是哪个版本就升到哪个版本（远端中途又有新提交会要求重新确认）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import time
from dataclasses import dataclass, field
from html import escape
from pathlib import Path

from bot.config import settings

log = logging.getLogger(__name__)

REPO_DIR = Path(__file__).resolve().parents[2]
SCRIPT_NAME = "tg-aria2.sh"
UNIT = "tg-aria2-upgrade"

# 等升级任务跑完的上限（pip 装依赖 + 20 秒健康检查 + 可能的回滚再重启）
MAX_WAIT_SECONDS = 15 * 60
# 任务文件超过这么久还在，说明是遗留物（机器人崩过、没人汇报），直接清掉
STALE_JOB_SECONDS = 60 * 60
LOG_MAX_BYTES = 1024 * 1024
ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")

# 升级任务的入口：重定向日志到文件（journal 不一定保留），再 exec 升级脚本。
# $1=日志 $2=脚本 $3=仓库目录，其余参数原样传给 update
RUNNER = (
    'exec >>"$1" 2>&1; echo "=== $(date "+%F %T") upgrade start ==="; '
    's="$2"; d="$3"; shift 3; exec bash "$s" update -y --dir "$d" "$@"'
)


class UpgradeError(Exception):
    """能直接给管理员看的错误信息。"""


@dataclass
class Version:
    sha: str
    short: str
    subject: str
    ts: int
    branch: str


@dataclass
class Commit:
    short: str
    subject: str
    ts: int


@dataclass
class UpdateInfo:
    current: Version
    remote_sha: str
    commits: list[Commit] = field(default_factory=list)
    total: int = 0                  # 新提交总数（commits 只列前几个）
    deps_changed: bool = False      # requirements.txt 有变化
    env_changed: bool = False       # .env.example 有新配置项
    diverged: bool = False          # 本地有远端没有的提交，无法快进
    dirty: list[str] = field(default_factory=list)   # 有本地改动的已跟踪文件

    @property
    def remote_short(self) -> str:
        return self.remote_sha[:7]

    @property
    def up_to_date(self) -> bool:
        return self.remote_sha == self.current.sha or (self.total == 0 and not self.diverged)


@dataclass
class Outcome:
    kind: str            # success | uptodate | failed | timeout
    old: str
    new: str
    log_tail: str = ""
    action: str = "upgrade"   # upgrade | rollback


# ---------------------------------------------------------------- 路径 / 状态文件

def _state_dir() -> Path:
    return Path(settings.db_path).parent


def job_path() -> Path:
    return _state_dir() / "upgrade_job.json"


def state_path() -> Path:
    return _state_dir() / "upgrade_state.json"


def log_path() -> Path:
    return _state_dir() / "upgrade.log"


def _read_json(path: Path) -> dict | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def _write_json(path: Path, data: dict):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)   # 原子替换：写到一半被杀不会留下半个文件


def read_job() -> dict | None:
    return _read_json(job_path())


def clear_job():
    try:
        job_path().unlink()
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------- 子进程

async def _run(*args: str, cwd: Path | None = None, timeout: float = 30.0) -> tuple[int, str]:
    """跑一个命令，返回 (退出码, 合并后的输出)。超时杀掉并返回 124。"""
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "LC_ALL": "C"}
    env.setdefault("GIT_SSH_COMMAND", "ssh -o BatchMode=yes")   # 要密码就直接失败，别卡住
    try:
        proc = await asyncio.create_subprocess_exec(
            *args, cwd=str(cwd or REPO_DIR), env=env,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
    except FileNotFoundError:
        return 127, f"找不到命令：{args[0]}"
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, f"命令超时（{timeout:.0f} 秒）：{' '.join(args[:3])}"
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


# ---------------------------------------------------------------- 能不能在机器人里升级

def support() -> tuple[bool, str]:
    """返回 (支持, 不支持的原因)。"""
    if not settings.upgrade_enabled:
        return False, "在线升级已被关闭（.env 里 UPGRADE_ENABLED=false）。"
    if not (REPO_DIR / ".git").exists():
        return False, "这不是 git 部署（docker 镜像里没有 git 仓库）。docker 模式请在服务器上运行：sudo tg-aria2 update"
    if not (REPO_DIR / SCRIPT_NAME).exists():
        return False, f"仓库里没有 {SCRIPT_NAME}。请先在服务器上手动升级一次：git pull && sudo ./{SCRIPT_NAME} install"
    if os.geteuid() != 0:
        return False, "机器人不是以 root 运行的，没有权限重启服务。"
    if not shutil.which("systemd-run") or not shutil.which("git"):
        return False, "服务器上缺少 systemd-run 或 git。"
    return True, ""


# ---------------------------------------------------------------- 版本 / 检查更新

async def _git(*args: str, timeout: float = 30.0) -> tuple[int, str]:
    return await _run("git", *args, timeout=timeout)


async def current_version() -> Version:
    rc, out = await _git("log", "-1", "--format=%H%x09%h%x09%ct%x09%s")
    if rc != 0 or out.count("\t") < 3:
        raise UpgradeError(f"读取当前版本失败：{out[-200:]}")
    sha, short, ts, subject = out.split("\t", 3)
    rc, branch = await _git("rev-parse", "--abbrev-ref", "HEAD")
    return Version(sha, short, subject, int(ts), branch.strip() if rc == 0 else "?")


async def recent_versions(limit: int = 10) -> list[Commit]:
    """当前版本往前数的最近 limit 个提交（第一个就是当前版本），供回退时选。"""
    ok, reason = support()
    if not ok:
        raise UpgradeError(reason)
    rc, out = await _git("log", f"-{limit}", "--first-parent", "--format=%H%x09%ct%x09%s")
    if rc != 0:
        raise UpgradeError(f"读取版本历史失败：{out[-200:]}")
    commits = []
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) == 3 and parts[1].isdigit():
            commits.append(Commit(parts[0][:7], parts[2], int(parts[1])))
    return commits


async def check() -> UpdateInfo:
    """git fetch 并比较本地和远端。出任何问题抛 UpgradeError（信息给管理员看）。"""
    ok, reason = support()
    if not ok:
        raise UpgradeError(reason)
    cur = await current_version()
    if cur.branch in ("HEAD", "?"):
        raise UpgradeError("仓库当前处于 detached HEAD，无法判断要跟踪哪个分支。")

    rc, out = await _git("fetch", "--quiet", "origin", cur.branch, timeout=90.0)
    if rc != 0:
        raise UpgradeError(f"git fetch 失败，检查服务器的网络和远端地址：\n{out[-300:]}")
    remote = f"origin/{cur.branch}"
    rc, remote_sha = await _git("rev-parse", remote)
    if rc != 0:
        raise UpgradeError(f"远端没有分支 {cur.branch}：{remote_sha[-200:]}")
    info = UpdateInfo(current=cur, remote_sha=remote_sha.strip())

    rc, count = await _git("rev-list", "--count", f"HEAD..{remote}")
    info.total = int(count) if rc == 0 and count.isdigit() else 0
    rc, _ = await _git("merge-base", "--is-ancestor", "HEAD", remote)
    info.diverged = rc != 0 and info.remote_sha != cur.sha
    if info.total:
        rc, log_out = await _git("log", "-10", "--format=%h%x09%ct%x09%s", f"HEAD..{remote}")
        for line in log_out.splitlines() if rc == 0 else []:
            parts = line.split("\t", 2)
            if len(parts) == 3 and parts[1].isdigit():
                info.commits.append(Commit(parts[0], parts[2], int(parts[1])))
        rc, _ = await _git("diff", "--quiet", "HEAD", remote, "--", "requirements.txt")
        info.deps_changed = rc == 1
        rc, _ = await _git("diff", "--quiet", "HEAD", remote, "--", ".env.example")
        info.env_changed = rc == 1
    # aria2-config/ 是运行时会被改写的模板，升级脚本会自己保存/恢复，不算"本地改动"
    rc, status = await _git("status", "--porcelain", "-uno", "--", ".", ":!aria2-config")
    info.dirty = [ln[3:] for ln in status.splitlines()] if rc == 0 else []
    return info


# ---------------------------------------------------------------- 起升级任务

async def unit_running() -> bool:
    rc, out = await _run("systemctl", "is-active", UNIT, timeout=10.0)
    return out.strip() in ("active", "activating", "reloading")


async def start_upgrade(*, chat_id: int, message_id: int | None, user_id: int,
                        from_sha: str, target_sha: str, reset: bool = False, rollback: bool = False) -> None:
    """rollback=True：回退到 target_sha（脚本的 --to，本身就是 reset 到该提交）。"""
    ok, reason = support()
    if not ok:
        raise UpgradeError(reason)
    if await unit_running() or read_job():
        raise UpgradeError("已经有一个升级在进行中，请等它结束。")
    log = log_path()
    try:
        if log.exists() and log.stat().st_size > LOG_MAX_BYTES:
            log.unlink()
    except OSError:
        pass
    write_job({
        "chat_id": chat_id, "message_id": message_id, "user_id": user_id,
        "from_sha": from_sha, "target_sha": target_sha, "reset": reset, "started_at": time.time(),
        "action": "rollback" if rollback else "upgrade",
    })
    cmd = [
        "systemd-run", f"--unit={UNIT}", "--collect", "--quiet", f"--working-directory={REPO_DIR}",
        "bash", "-c", RUNNER, "_", str(log), str(REPO_DIR / SCRIPT_NAME), str(REPO_DIR),
    ]
    if rollback:
        if not re.fullmatch(r"[0-9a-f]{7,40}", target_sha):
            clear_job()
            raise UpgradeError("版本号格式不对。")
        cmd += ["--to", target_sha]
    elif reset:
        cmd.append("--reset")
    rc, out = await _run(*cmd, timeout=30.0)
    if rc != 0:
        clear_job()
        raise UpgradeError(f"启动升级任务失败：{out[-300:]}")


def write_job(job: dict):
    _write_json(job_path(), job)


# ---------------------------------------------------------------- 结果判定与汇报

def log_tail(lines: int = 12) -> str:
    """最近一次升级的日志末尾（去掉颜色码）。日志是追加写的，只取最后一个
    "upgrade start" 标记之后的部分，免得把上一次升级的内容带进来。"""
    try:
        with open(log_path(), "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - 16 * 1024))
            text = f.read().decode("utf-8", "replace")
    except OSError:
        return ""
    marker = text.rfind("=== ")
    if marker != -1 and "upgrade start" in text[marker:marker + 60]:
        text = text[marker:]
    text = ANSI_RE.sub("", text)
    return "\n".join(text.strip().splitlines()[-lines:])


def classify(job: dict, current_sha: str, tail: str) -> Outcome:
    old = job.get("from_sha", "")
    action = job.get("action", "upgrade")
    if current_sha != old:
        return Outcome("success", old, current_sha, tail, action)
    if "已经是最新版本" in tail:
        return Outcome("uptodate", old, current_sha, tail, action)
    # 版本没变：要么升级前就失败了，要么健康检查没过、已经自动回滚
    return Outcome("failed", old, current_sha, tail, action)


def render_outcome(outcome: Outcome, new_subject: str = "") -> str:
    old, new = outcome.old[:7], outcome.new[:7]
    word = "回退" if outcome.action == "rollback" else "升级"
    if outcome.kind == "success":
        text = f"✅ <b>{word}完成</b>\n\n<code>{old}</code> → <code>{new}</code>"
        if new_subject:
            text += f"\n{escape(new_subject, quote=False)}"
        return text
    if outcome.kind == "uptodate":
        if outcome.action == "rollback":
            return f"✅ 当前已经是这个版本（<code>{new}</code>），无需回退。"
        return f"✅ 已经是最新版本（<code>{new}</code>），无需升级。"
    head = (
        f"⚠️ <b>{word}超时</b>\n\n{word}任务 15 分钟还没结束，请登录服务器查看。"
        if outcome.kind == "timeout"
        else f"⚠️ <b>{word}失败</b>\n\n版本没有变化（仍是 <code>{new}</code>）。如果{word}过程中服务异常，脚本已自动回滚到{word}前的版本。"
    )
    tail = f"\n\n<b>日志末尾：</b>\n<pre>{escape(outcome.log_tail, quote=False)}</pre>" if outcome.log_tail else ""
    return f"{head}{tail}\n\n完整日志：<code>{log_path()}</code>"


_watching = False


async def finish_pending_upgrade(bot, *, poll_seconds: float = 3.0, max_wait: float = MAX_WAIT_SECONDS) -> bool:
    """等升级任务跑完，把结果编辑到发起升级的那条消息上。

    机器人启动时调一次（升级重启后的新进程接着汇报），发起升级时也起一个（升级
    在重启之前就失败、机器人没被重启的情况）。两边共用任务文件，谁先汇报谁清掉它。
    返回是否汇报了。"""
    global _watching
    if _watching:
        return False
    job = read_job()
    if not job:
        return False
    if time.time() - float(job.get("started_at", 0)) > STALE_JOB_SECONDS:
        clear_job()
        return False
    _watching = True
    try:
        deadline = time.monotonic() + max_wait
        timed_out = False
        while await unit_running():
            if time.monotonic() > deadline:
                timed_out = True
                break
            await asyncio.sleep(poll_seconds)
        try:
            cur = await current_version()
            current_sha, subject = cur.sha, cur.subject
        except UpgradeError:
            current_sha, subject = job.get("from_sha", ""), ""
        tail = log_tail()
        outcome = classify(job, current_sha, tail)
        if timed_out and outcome.kind == "failed":
            outcome = Outcome("timeout", outcome.old, outcome.new, tail, outcome.action)
        await _report(bot, job, render_outcome(outcome, subject))
        clear_job()
        return True
    finally:
        _watching = False


async def _report(bot, job: dict, text: str):
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    markup = InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="⬅️ 返回设置", callback_data="nav:settings")]])
    chat_id, message_id = job.get("chat_id"), job.get("message_id")
    if chat_id and message_id:
        try:
            await bot.edit_message_text(
                chat_id=chat_id, message_id=message_id, text=text, parse_mode="HTML", reply_markup=markup,
            )
            return
        except Exception:
            log.info("could not edit the upgrade message, sending a new one")
    if chat_id:
        try:
            await bot.send_message(chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=markup)
        except Exception:
            log.warning("could not report the upgrade result to chat %s", chat_id)


# ---------------------------------------------------------------- 定期检查新版本

def _read_state() -> dict:
    return _read_json(state_path()) or {}


async def check_and_notify(bot) -> bool:
    """有新版本就私聊通知管理员，同一个新版本只通知一次。返回是否发了通知。"""
    if not settings.update_check_enabled or not support()[0]:
        return False
    if read_job() or await unit_running():
        return False   # 正在升级，别打扰
    try:
        info = await check()
    except UpgradeError as e:
        log.info("update check failed: %s", e)
        return False
    if info.up_to_date or info.diverged:
        return False
    state = _read_state()
    if state.get("notified_sha") == info.remote_sha:
        return False
    from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup
    text = (
        f"🆕 <b>发现新版本</b>（{info.total} 个新提交）\n"
        f"当前 <code>{info.current.short}</code> → 最新 <code>{info.remote_short}</code>\n\n"
        + "\n".join(f"• {escape(c.subject, quote=False)}" for c in info.commits[:5])
    )
    markup = InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="⬆️ 查看并升级", callback_data="admin:upgrade"),
    ]])
    sent = 0
    for admin_id in settings.admin_recipient_ids:
        try:
            await bot.send_message(chat_id=admin_id, text=text, parse_mode="HTML", reply_markup=markup)
            sent += 1
        except Exception:
            log.warning("could not send the update notice to admin %s", admin_id)
    if sent:
        state["notified_sha"] = info.remote_sha
        _write_json(state_path(), state)
    return bool(sent)


async def update_check_loop(bot, *, first_delay: float = 120.0):
    """启动后稍等一会儿再查（别每次重启都立刻去连 git 远端），之后每
    UPDATE_CHECK_HOURS 小时查一次。"""
    await asyncio.sleep(first_delay)
    while True:
        try:
            await check_and_notify(bot)
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("update check failed")
        await asyncio.sleep(max(1, settings.update_check_hours) * 3600)
