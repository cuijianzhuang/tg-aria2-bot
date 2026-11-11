"""Scoped personal QR pages and administrator shared-account endpoints."""

import time
from pathlib import Path
from typing import Literal

from fastapi import Depends, HTTPException, Request, Response
from fastapi.responses import FileResponse, HTMLResponse
from pydantic import BaseModel

from bot.config import settings
from bot.video.account_views import shared_summary
from bot.video.qr_browser import REASONS
from bot.video.ui import STATES

COOKIE = "video_login_session"


class Exchange(BaseModel):
    token: str


class PlatformRequest(BaseModel):
    platform: Literal["bilibili", "douyin", "xhs"]


class SharingRequest(PlatformRequest):
    enabled: bool


def public_status(row):
    expired = row["expires"] <= time.time() and row["state"] not in (
        "success",
        "cancelled",
        "failed",
        "verification_required",
    )
    state = "expired" if expired else row["state"]
    return {
        "state": state,
        "message": REASONS.get(dict(row).get("failure_reason"), STATES.get(state, "处理中"))
        if state == "failed"
        else STATES.get(state, "处理中"),
        "platform": row["platform"],
        "qr_ready": bool(row["qr_path"]) and state in ("waiting", "scanned"),
        "identity": row["identity"] if state == "success" else None,
    }


def register(app, state, require_login):
    def login_enabled():
        if not settings.video_login_enabled:
            raise HTTPException(503, "扫码服务未启用")

    async def personal(request: Request):
        login_enabled()
        row = await state["repo"].video_web_login(request.cookies.get(COOKIE, ""))
        if not row:
            raise HTTPException(401, "链接已失效，请从机器人私聊重新打开")
        return row

    def no_cross_origin(request: Request):
        origin = request.headers.get("origin")
        allowed = {str(request.base_url).rstrip("/")}
        if settings.video_public_url:
            allowed.add(settings.video_public_url.rstrip("/"))
        if origin and origin.rstrip("/") not in allowed:
            raise HTTPException(403, "跨站请求已拒绝")

    personal_dependency = Depends(personal)

    @app.get("/video-login", response_class=HTMLResponse)
    async def login_page():
        return HTMLResponse(
            (Path(__file__).parent / "static" / "video-login.html").read_text(),
            headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"},
        )

    @app.post("/api/video-login/exchange", dependencies=[Depends(no_cross_origin)])
    async def exchange(body: Exchange, request: Request, response: Response):
        login_enabled()
        session = await state["repo"].video_exchange(body.token)
        if not session:
            raise HTTPException(401, "链接已使用或过期，请从私聊重新生成")
        secure = request.url.scheme == "https" or (
            settings.trust_proxy_headers and request.headers.get("x-forwarded-proto") == "https"
        )
        response.set_cookie(
            COOKIE, session, httponly=True, samesite="strict", secure=secure, max_age=600, path="/api/video-login"
        )
        response.headers["Cache-Control"] = "no-store"
        return {"ok": True}

    @app.get("/api/video-login/status")
    async def status(response: Response, row=personal_dependency):
        response.headers["Cache-Control"] = "no-store"
        return public_status(row)

    @app.get("/api/video-login/qr")
    async def qr(row=personal_dependency):
        if not public_status(row)["qr_ready"]:
            raise HTTPException(404, "二维码尚未生成或已失效")
        root = Path(settings.db_path).parent / "video-private" / "qr"
        path = root / (row["id"] + ".png")
        if not path.is_file():
            raise HTTPException(404, "二维码已失效")
        return FileResponse(
            path, media_type="image/png", headers={"Cache-Control": "no-store", "Referrer-Policy": "no-referrer"}
        )

    @app.post("/api/video-login/cancel", dependencies=[Depends(no_cross_origin)])
    async def cancel(row=personal_dependency):
        if row["state"] != "success":
            await state["repo"].video_login_update(row["id"], state="cancelled")
        return {"ok": True}

    @app.post("/api/video-login/refresh", dependencies=[Depends(no_cross_origin)])
    async def refresh(request: Request, row=personal_dependency):
        repo = state["repo"]
        await repo.video_login_update(row["id"], state="cancelled")
        login_id = await repo.video_new_login(row["platform"], row["owner_id"], row["requester_id"], row["chat_id"])
        await repo.video_rebind_session(request.cookies.get(COOKIE, ""), login_id)
        return {"ok": True}

    admin_dependencies = [Depends(require_login), Depends(no_cross_origin), Depends(login_enabled)]

    @app.get("/api/video/accounts", dependencies=[Depends(require_login)])
    async def accounts():
        return [await shared_summary(state["repo"], platform) for platform in ("bilibili", "douyin", "xhs")]

    @app.post("/api/video/accounts/login", dependencies=admin_dependencies)
    async def admin_login(body: PlatformRequest):
        ids = settings.admin_ids or settings.allowed_ids
        if not ids:
            raise HTTPException(409, "请先配置管理员 Telegram ID")
        repo = state["repo"]
        login_id = await repo.video_new_login(body.platform, 0, next(iter(ids)))
        token = await repo.video_grant(login_id)
        return {"url": "/video-login#" + token}

    @app.post("/api/video/accounts/sharing", dependencies=admin_dependencies)
    async def sharing(body: SharingRequest):
        await state["repo"].video_share(body.platform, body.enabled)
        return {"ok": True}

    @app.post("/api/video/accounts/remove", dependencies=admin_dependencies)
    async def remove(body: PlatformRequest):
        if not await state["repo"].video_account(body.platform, 0):
            raise HTTPException(409, "没有可移除的扫码账号；配置文件来源可关闭共享或修改服务器配置")
        await state["repo"].video_remove_account(body.platform, 0)
        return {"ok": True}
