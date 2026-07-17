import asyncio
import hmac
import os
import time
from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from bot.config import settings
from bot.core.aria2_client import Aria2Client
from bot.core.conf_editor import is_safe_value, list_rclone_remotes, read_kv, write_kv
from bot.core.storage import disk_usage_summary
from bot.db.repo import TaskRepo
from bot.web.auth import (
    SESSION_COOKIE,
    create_session_token,
    load_or_create_secret,
    rotate_secret,
    verify_session_token,
)

ENV_PATH = ".env"

STATIC_DIR = os.path.join(os.path.dirname(__file__), "static")

state: dict = {}

# In-memory login throttle (per-process, resets on restart — fine for a single
# admin instance). Needed once this port is exposed publicly: without it,
# ADMIN_PASSWORD is the only thing standing between the internet and the admin
# API, with nothing else rate-limiting login attempts.
_LOGIN_MAX_ATTEMPTS = 5
_LOGIN_WINDOW_SECONDS = 300
_LOGIN_LOCKOUT_SECONDS = 300
_login_attempts: dict[str, list[float]] = {}
_login_locked_until: dict[str, float] = {}


def _client_ip(request: Request) -> str:
    # X-Forwarded-For is attacker-controlled unless a trusted reverse proxy sets
    # it; honoring it while directly exposed lets one client rotate fake IPs and
    # bypass the login rate limit entirely.
    if settings.trust_proxy_headers:
        forwarded = request.headers.get("x-forwarded-for")
        if forwarded:
            return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


def _check_password(supplied: str) -> bool:
    return hmac.compare_digest(supplied.encode(), settings.admin_password.encode())


def _check_login_rate_limit(ip: str):
    now = time.monotonic()
    locked_until = _login_locked_until.get(ip)
    if locked_until and now < locked_until:
        retry_after = int(locked_until - now)
        raise HTTPException(429, f"尝试次数过多，请 {retry_after} 秒后再试")


def _record_login_failure(ip: str):
    now = time.monotonic()
    attempts = [t for t in _login_attempts.get(ip, []) if now - t < _LOGIN_WINDOW_SECONDS]
    attempts.append(now)
    if len(attempts) >= _LOGIN_MAX_ATTEMPTS:
        _login_locked_until[ip] = now + _LOGIN_LOCKOUT_SECONDS
        attempts = []
    _login_attempts[ip] = attempts


def _clear_login_failures(ip: str):
    _login_attempts.pop(ip, None)
    _login_locked_until.pop(ip, None)


def _secret_path() -> str:
    return os.path.join(os.path.dirname(settings.db_path), "web_session_secret")


@asynccontextmanager
async def lifespan(app: FastAPI):
    repo = TaskRepo(settings.db_path)
    await repo.connect()
    state["repo"] = repo
    state["aria2"] = Aria2Client(settings.aria2_rpc, settings.aria2_secret)
    state["session_secret"] = load_or_create_secret(_secret_path())
    yield
    await repo.close()


app = FastAPI(title="tg-aria2-bot admin", lifespan=lifespan)


def require_login(request: Request):
    if not settings.admin_password:
        raise HTTPException(503, "ADMIN_PASSWORD 未配置，管理后台已禁用")
    token = request.cookies.get(SESSION_COOKIE)
    if not verify_session_token(state["session_secret"], token):
        raise HTTPException(401, "未登录")


class LoginRequest(BaseModel):
    password: str


class AddUserRequest(BaseModel):
    user_id: int
    note: str | None = None


class LimitRequest(BaseModel):
    speed: str


class RcloneSettingsRequest(BaseModel):
    enabled: bool
    drive_name: str = ""
    drive_dir: str = ""


class ChangePasswordRequest(BaseModel):
    current_password: str
    new_password: str


class RestartRequest(BaseModel):
    service: str  # only ever "bot" or "aria2" — never a raw systemd unit name


class GofileSettingsRequest(BaseModel):
    enabled: bool
    token: str = ""
    compress: bool = True
    delete_local: bool = False


@app.post("/api/login")
async def login(body: LoginRequest, request: Request, response: Response):
    if not settings.admin_password:
        raise HTTPException(503, "ADMIN_PASSWORD 未配置，管理后台已禁用")
    ip = _client_ip(request)
    _check_login_rate_limit(ip)
    if not _check_password(body.password):
        _record_login_failure(ip)
        raise HTTPException(401, "密码错误")
    _clear_login_failures(ip)
    token = create_session_token(state["session_secret"])
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=7 * 24 * 3600)
    return {"ok": True}


@app.post("/api/logout")
async def logout(response: Response):
    response.delete_cookie(SESSION_COOKIE)
    return {"ok": True}


@app.post("/api/settings/password", dependencies=[Depends(require_login)])
async def change_password(body: ChangePasswordRequest, response: Response):
    if not _check_password(body.current_password):
        raise HTTPException(401, "当前密码错误")
    if len(body.new_password) < 8:
        raise HTTPException(400, "新密码至少 8 位")

    write_kv(ENV_PATH, "ADMIN_PASSWORD", body.new_password)
    settings.admin_password = body.new_password  # take effect immediately, no restart needed

    # rotate the signing secret so every other session is invalidated on a
    # password change, then reissue a fresh token for this session.
    state["session_secret"] = rotate_secret(_secret_path())
    token = create_session_token(state["session_secret"])
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", max_age=7 * 24 * 3600)
    return {"ok": True}


@app.get("/api/tasks", dependencies=[Depends(require_login)])
async def list_tasks(limit: int = 20, offset: int = 0, status: str | None = None):
    rows = await state["repo"].list_recent(limit, offset, status)
    total = await state["repo"].count_tasks(status)
    items = [dict(row) for row in rows]

    active_gids = [t["gid"] for t in items if t["status"] == "ACTIVE" and t["gid"]]
    if active_gids:
        progress = await state["aria2"].get_progress_map()
        for t in items:
            t["progress"] = progress.get(t["gid"])

    return {"items": items, "total": total}


@app.post("/api/tasks/{task_id}/pause", dependencies=[Depends(require_login)])
async def pause_task(task_id: int):
    gid = await _gid_for_task(task_id)
    await state["aria2"].pause(gid)
    return {"ok": True}


@app.post("/api/tasks/{task_id}/resume", dependencies=[Depends(require_login)])
async def resume_task(task_id: int):
    gid = await _gid_for_task(task_id)
    await state["aria2"].resume(gid)
    return {"ok": True}


@app.post("/api/tasks/{task_id}/cancel", dependencies=[Depends(require_login)])
async def cancel_task(task_id: int):
    gid = await _gid_for_task(task_id)
    await state["aria2"].remove(gid)
    await state["repo"].update_status(gid, "CANCELLED")
    return {"ok": True}


async def _gid_for_task(task_id: int) -> str:
    row = await state["repo"].get_by_id(task_id)
    if row is None:
        raise HTTPException(404, "任务不存在")
    if not row["gid"]:
        raise HTTPException(404, "任务没有关联的 aria2 gid")
    return row["gid"]


@app.get("/api/stats", dependencies=[Depends(require_login)])
async def stats():
    aria2_stats = await state["aria2"].global_stat()
    by_status = await state["repo"].count_by_status()
    try:
        disk = disk_usage_summary(settings.download_dir)
    except OSError:
        disk = None
    return {
        "active": aria2_stats.num_active,
        "waiting": aria2_stats.num_waiting,
        "stopped": aria2_stats.num_stopped,
        "download_speed": aria2_stats.download_speed_string(),
        "upload_speed": aria2_stats.upload_speed_string(),
        "by_status": by_status,
        "disk": disk,
    }


@app.post("/api/limit", dependencies=[Depends(require_login)])
async def set_limit(body: LimitRequest):
    await state["aria2"].set_global_limit(body.speed)
    return {"ok": True}


@app.get("/api/users", dependencies=[Depends(require_login)])
async def list_users():
    db_users = await state["repo"].list_allowed_users()
    result = [{"user_id": uid, "note": "来自 .env (ALLOWED_USER_IDS)", "source": "env", "removable": False}
              for uid in sorted(settings.allowed_ids)]
    result += [
        {"user_id": row["user_id"], "note": row["note"], "source": "db", "removable": True}
        for row in db_users
    ]
    return result


@app.post("/api/users", dependencies=[Depends(require_login)])
async def add_user(body: AddUserRequest):
    await state["repo"].add_allowed_user(body.user_id, body.note)
    return {"ok": True}


@app.delete("/api/users/{user_id}", dependencies=[Depends(require_login)])
async def remove_user(user_id: int):
    if user_id in settings.allowed_ids:
        raise HTTPException(400, "该用户来自 .env 的 ALLOWED_USER_IDS，无法在此删除，请直接编辑 .env")
    await state["repo"].remove_allowed_user(user_id)
    return {"ok": True}


def _aria2_conf_path() -> str:
    return os.path.join(settings.aria2_config_dir, "aria2.conf")


def _script_conf_path() -> str:
    return os.path.join(settings.aria2_config_dir, "script.conf")


def _rclone_conf_path() -> str:
    # Matches p3terx/aria2-pro's own RCLONE_CONFIG=/config/rclone.conf default;
    # /aria2-config here and /config in the aria2 container are the same host dir.
    return os.path.join(settings.aria2_config_dir, "rclone.conf")


@app.get("/api/settings/rclone", dependencies=[Depends(require_login)])
async def get_rclone_settings():
    on_complete = read_kv(_aria2_conf_path(), "on-download-complete") or ""
    return {
        "enabled": on_complete.strip().endswith("upload.sh"),
        "drive_name": read_kv(_script_conf_path(), "drive-name") or "",
        "drive_dir": read_kv(_script_conf_path(), "drive-dir") or "",
        "remotes": list_rclone_remotes(_rclone_conf_path()),
    }


@app.post("/api/settings/rclone", dependencies=[Depends(require_login)])
async def set_rclone_settings(body: RcloneSettingsRequest):
    for value in (body.drive_name, body.drive_dir):
        if not is_safe_value(value):
            raise HTTPException(400, "只允许字母、数字、空格、. _ - / 这些字符")

    write_kv(_script_conf_path(), "drive-name", body.drive_name or None)
    write_kv(_script_conf_path(), "drive-dir", body.drive_dir or None)

    hook_target = settings.aria2_upload_hook if body.enabled else settings.aria2_clean_hook
    write_kv(_aria2_conf_path(), "on-download-complete", hook_target)

    return {"ok": True, "restart_required": True}


@app.get("/api/settings/gofile", dependencies=[Depends(require_login)])
async def get_gofile_settings():
    return {
        "enabled": settings.gofile_enabled,
        "token": settings.gofile_token,
        "compress": settings.gofile_compress,
        "delete_local": settings.gofile_delete_local,
    }


@app.post("/api/settings/gofile", dependencies=[Depends(require_login)])
async def set_gofile_settings(body: GofileSettingsRequest):
    if "\n" in body.token or "\r" in body.token:
        raise HTTPException(400, "token 不能包含换行符")

    write_kv(ENV_PATH, "GOFILE_ENABLED", "true" if body.enabled else "false")
    write_kv(ENV_PATH, "GOFILE_TOKEN", body.token)
    write_kv(ENV_PATH, "GOFILE_COMPRESS", "true" if body.compress else "false")
    write_kv(ENV_PATH, "GOFILE_DELETE_LOCAL", "true" if body.delete_local else "false")

    # unlike ADMIN_PASSWORD, this doesn't take effect in this process — it's read
    # by the separate bot process (task_manager.py), which only reloads .env at
    # its own startup.
    return {"ok": True, "restart_required": True}


@app.post("/api/settings/restart", dependencies=[Depends(require_login)])
async def restart_service(body: RestartRequest):
    unit = {"bot": settings.bot_service_name, "aria2": settings.aria2_service_name}.get(body.service)
    if not unit:
        raise HTTPException(400, "service 必须是 bot 或 aria2")

    try:
        proc = await asyncio.create_subprocess_exec(
            "systemctl", "restart", unit,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
    except FileNotFoundError as e:
        # docker mode: this container has no systemctl at all (and even if it
        # did, "restart" would need to mean the *other* container, not this
        # process) — nothing to do here except tell the user to do it themselves.
        raise HTTPException(
            501,
            "此环境没有 systemctl（大概率是 docker 部署），请手动执行："
            f" docker compose restart {'bot' if body.service == 'bot' else 'aria2'}",
        ) from e

    if proc.returncode != 0:
        raise HTTPException(500, f"重启失败: {stderr.decode(errors='replace')[:300]}")
    return {"ok": True}


@app.get("/api/me", dependencies=[Depends(require_login)])
async def me():
    return {"ok": True}


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
