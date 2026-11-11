"""Optional QR worker. Uses a durable queue, exposes no browser/debug ports."""

import asyncio
import logging
import time
from pathlib import Path
from urllib.parse import urlparse

import aiohttp

from bot.config import settings
from bot.db.repo import TaskRepo
from bot.video.accounts import PLATFORMS, QR_PLATFORMS, CredentialVault
from bot.video.qr_browser import QRLoginError, failure_reason, open_login, verification_required, visible_locator

log = logging.getLogger(__name__)
TERMINAL = {"success", "expired", "cancelled", "failed", "verification_required"}
BROWSER_SITES = {
    "douyin": {
        "url": "https://www.douyin.com/",
        "login": ['[data-e2e="top-login-button"]', 'button:text-is("登录")', 'text="登录"'],
        "qr": [
            'img[class*="qrcode" i]',
            'img[class*="qr-code" i]',
            '[class*="qrcode" i] img',
            '[class*="qrcode" i] canvas',
            '[class*="QRCode"] img',
        ],
        "profile": ['a[href*="/user/self"]', 'a[href*="/user/"]:has-text("我")'],
    },
    "xhs": {
        "url": "https://www.xiaohongshu.com/explore",
        "login": [".login-btn", 'button:text-is("登录")', 'text="登录"'],
        "qr": [
            'img[class*="qrcode" i]',
            'img[class*="qr-code" i]',
            ".login-container .qrcode-img",
            '.login-container img[class*="qrcode"]',
            '[class*="qrcode" i] canvas',
        ],
        "profile": ['a[href^="/user/profile/"]:has-text("我")', '.user.side-bar-component a[href*="/user/profile/"]'],
    },
}


class LoginWorker:
    def __init__(self, repo, vault=None):
        self.repo = repo
        self.vault = vault or CredentialVault(Path(settings.db_path).parent)
        self.tasks = {}
        self.stages = {}

    def qr_path(self, login_id):
        directory = self.vault.root / "qr"
        directory.mkdir(exist_ok=True, mode=0o700)
        return directory / (login_id + ".png")

    async def active(self, login_id):
        row = await self.repo.video_login(login_id)
        return row and row["state"] not in TERMINAL and row["expires"] > time.time()

    async def success(self, row, cookies, identity):
        # A cancelled or replaced session must never overwrite a newer account.
        current = await self.repo.video_login(row["id"])
        if not current or current["state"] in TERMINAL or current["expires"] <= time.time():
            return
        filtered = [
            c
            for c in cookies
            if any(
                c["domain"].lstrip(".") == d or c["domain"].lstrip(".").endswith("." + d)
                for d in PLATFORMS[row["platform"]]
            )
        ]
        if not filtered:
            raise ValueError("missing cookies")
        await self.repo.video_complete_login(row["id"], str(identity)[:100], self.vault.encrypt(filtered))

    async def bilibili(self, row):
        import qrcode

        timeout = aiohttp.ClientTimeout(total=20)
        async with aiohttp.ClientSession(
            timeout=timeout,
            trust_env=True,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://www.bilibili.com/"},
        ) as session:
            proxy = settings.proxy_url or None
            async with session.get(
                "https://passport.bilibili.com/x/passport-login/web/qrcode/generate", proxy=proxy
            ) as response:
                result = await response.json()
            if result.get("code") != 0:
                raise ValueError("QR generate failed")
            data = result["data"]
            path = self.qr_path(row["id"])
            qrcode.make(data["url"]).save(path)
            path.chmod(0o600)
            await self.repo.video_login_update(row["id"], state="waiting", qr_path=str(path))
            while await self.active(row["id"]):
                await asyncio.sleep(2)
                async with session.get(
                    "https://passport.bilibili.com/x/passport-login/web/qrcode/poll",
                    params={"qrcode_key": data["qrcode_key"]},
                    proxy=proxy,
                ) as response:
                    result = await response.json()
                status = result.get("data", {}).get("code")
                if status == 86038:
                    await self.repo.video_login_update(row["id"], state="expired")
                    return
                if status == 86090:
                    await self.repo.video_login_update(row["id"], state="scanned")
                if status == 0:
                    async with session.get("https://api.bilibili.com/x/web-interface/nav", proxy=proxy) as response:
                        nav = await response.json()
                    profile = nav.get("data", {})
                    if not profile.get("isLogin") or not profile.get("mid"):
                        raise ValueError("identity not verified")
                    cookies = [
                        dict(
                            name=c.key,
                            value=c.value,
                            domain=c["domain"] or ".bilibili.com",
                            path=c["path"] or "/",
                            secure=bool(c["secure"]),
                            expires=-1,
                        )
                        for c in session.cookie_jar
                    ]
                    await self.success(row, cookies, str(profile["mid"]))
                    return

    async def browser_login(self, row):
        self.stages[row["id"]] = "import"
        from playwright.async_api import async_playwright

        site = BROWSER_SITES[row["platform"]]
        async with async_playwright() as playwright:
            self.stages[row["id"]] = "launch"
            browser = await playwright.chromium.launch(
                headless=True, **({"proxy": {"server": settings.proxy_url}} if settings.proxy_url else {})
            )
            try:
                context = await browser.new_context(viewport={"width": 1280, "height": 900}, locale="zh-CN")
                page = await context.new_page()
                self.stages[row["id"]] = "navigation"
                response = await page.goto(site["url"], wait_until="domcontentloaded", timeout=45000)
                if response and response.status >= 400:
                    raise QRLoginError(
                        "platform_blocked" if response.status in (403, 412, 429) else "platform_unavailable"
                    )
                self.stages[row["id"]] = "discovery"
                qr = None
                deadline = time.monotonic() + 45
                while time.monotonic() < deadline and await self.active(row["id"]):
                    if await verification_required(page):
                        if await self.active(row["id"]):
                            log.info("QR verification required for platform %s before QR discovery", row["platform"])
                            await self.repo.video_login_update(row["id"], state="verification_required")
                        return
                    qr = await visible_locator(page, site["qr"], minimum_size=60)
                    if qr:
                        break
                    await open_login(page, site["login"])
                    await open_login(page, ['text="扫码登录"', 'text="二维码登录"'])
                    await asyncio.sleep(1)
                if not await self.active(row["id"]):
                    return
                if qr is None:
                    raise QRLoginError("qr_missing")
                self.stages[row["id"]] = "capture"
                path = self.qr_path(row["id"])
                await qr.screenshot(path=str(path))
                path.chmod(0o600)
                await self.repo.video_login_update(row["id"], state="waiting", qr_path=str(path))
                self.stages[row["id"]] = "waiting"
                while await self.active(row["id"]):
                    if await verification_required(page):
                        if await self.active(row["id"]):
                            log.info("QR verification required for platform %s after QR discovery", row["platform"])
                            await self.repo.video_login_update(row["id"], state="verification_required")
                        return
                    for selector in site["profile"]:
                        profile = page.locator(selector).first
                        if await profile.is_visible():
                            identity = await profile.get_attribute("href")
                            if identity and "/user/" in identity:
                                if row["platform"] == "douyin":
                                    try:
                                        response = await context.request.get(
                                            "https://www.douyin.com/aweme/v1/web/user/profile/self/?aid=6383&device_platform=webapp",
                                            timeout=10000,
                                        )
                                        account = (await response.json()).get("user", {})
                                        verified = account.get("uid") or account.get("sec_uid")
                                    except Exception:
                                        verified = None
                                    if not verified:
                                        continue
                                    identity = str(verified)
                                else:
                                    identity = urlparse(identity).path
                                await self.success(row, await context.cookies(), identity)
                                return
                    await asyncio.sleep(2)
            finally:
                await browser.close()

    async def execute(self, row):
        try:
            if row["platform"] not in QR_PLATFORMS or (
                row["owner_id"] == 0 and not settings.is_admin(row["requester_id"])
            ):
                raise ValueError("invalid login request")
            await self.repo.video_login_update(row["id"], state="preparing", failure_reason=None)
            await asyncio.wait_for(
                self.bilibili(row) if row["platform"] == "bilibili" else self.browser_login(row),
                max(1, row["expires"] - time.time()),
            )
            current = await self.repo.video_login(row["id"])
            if current and current["state"] not in TERMINAL:
                await self.repo.video_login_update(row["id"], state="expired")
        except TimeoutError:
            current = await self.repo.video_login(row["id"])
            if current and current["state"] not in TERMINAL:
                await self.repo.video_login_update(row["id"], state="expired")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            # Browser/network errors can embed credential URLs; do not log them.
            reason = failure_reason(exc, self.stages.get(row["id"]))
            current = await self.repo.video_login(row["id"])
            if current and current["state"] not in TERMINAL:
                await self.repo.video_login_update(row["id"], state="failed", failure_reason=reason)
            log.warning("QR session failed for platform %s: %s (%s)", row["platform"], reason, type(exc).__name__)
        finally:
            self.stages.pop(row["id"], None)
            self.qr_path(row["id"]).unlink(missing_ok=True)
            await self.repo.video_login_update(row["id"], qr_path=None)

    async def run(self):
        for path in (self.vault.root / "qr").glob("*.png"):
            path.unlink(missing_ok=True)
        # This service runs as a single instance. Sessions left by a previous
        # worker cannot be resumed safely; expose refresh rather than fake success.
        for row in await self.repo.video_login_rows():
            if row["state"] in ("preparing", "waiting", "scanned"):
                await self.repo.video_login_update(row["id"], state="expired", qr_path=None)
        try:
            while True:
                for row in await self.repo.video_login_rows():
                    if row["expires"] <= time.time() and row["state"] not in TERMINAL:
                        await self.repo.video_login_update(row["id"], state="expired")
                    elif row["state"] == "queued" and len(self.tasks) < 2 and row["id"] not in self.tasks:
                        task = asyncio.create_task(self.execute(row))
                        self.tasks[row["id"]] = task
                        task.add_done_callback(lambda done, key=row["id"]: self.tasks.pop(key, None))
                    elif row["state"] in TERMINAL and row["id"] in self.tasks:
                        self.tasks[row["id"]].cancel()
                await self.repo.video_prune()
                await asyncio.sleep(1)
        finally:
            for task in self.tasks.values():
                task.cancel()
            await asyncio.gather(*self.tasks.values(), return_exceptions=True)


async def main():
    if not settings.video_login_enabled:
        raise SystemExit("请先设置 VIDEO_LOGIN_ENABLED=true")
    repo = TaskRepo(settings.db_path)
    await repo.connect()
    try:
        await LoginWorker(repo).run()
    finally:
        await repo.close()


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    asyncio.run(main())
