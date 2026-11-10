"""Platform-scoped encrypted credentials and short-lived downloader snapshots."""

import contextlib
import http.cookiejar
import json
import os
import tempfile
from pathlib import Path
from urllib.parse import urlparse

PLATFORMS = {
    "bilibili": ("bilibili.com", "b23.tv"),
    "douyin": ("douyin.com", "iesdouyin.com"),
    "xhs": ("xiaohongshu.com", "xhslink.com"),
}
VIDEO_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "vimeo.com",
    "twitch.tv",
    "twitter.com",
    "x.com",
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "dailymotion.com",
)


def matches_host(host, domain):
    return host == domain or host.endswith("." + domain)


def platform_for(url):
    host = (urlparse(url).hostname or "").lower()
    return next((p for p, domains in PLATFORMS.items() if any(matches_host(host, d) for d in domains)), None)


def is_video_url(url):
    host = (urlparse(url).hostname or "").lower()
    return bool(platform_for(url)) or any(matches_host(host, d) for d in VIDEO_DOMAINS)


def validate_url(url):
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError("视频地址必须是有效的 HTTP(S) 页面链接")
    return url


class CredentialVault:
    def __init__(self, data_dir):
        self.root = Path(data_dir) / "video-private"
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)

    def _cipher(self):
        from cryptography.fernet import Fernet

        path = self.root / "key"
        if not path.exists():
            fd, temporary = tempfile.mkstemp(dir=self.root)
            try:
                with os.fdopen(fd, "wb") as stream:
                    stream.write(Fernet.generate_key())
                try:
                    os.link(temporary, path)
                except FileExistsError:
                    pass
            finally:
                Path(temporary).unlink(missing_ok=True)
        return Fernet(path.read_bytes())

    def encrypt(self, cookies):
        return self._cipher().encrypt(json.dumps(cookies).encode())

    def decrypt(self, blob):
        return json.loads(self._cipher().decrypt(blob))

    @contextlib.contextmanager
    def snapshot(self, cookies):
        fd, path = tempfile.mkstemp(prefix="cookies-", suffix=".txt", dir=self.root)
        os.close(fd)
        try:
            jar = http.cookiejar.MozillaCookieJar(path)
            for item in cookies:
                domain = item["domain"]
                expiry = item.get("expires", 0)
                jar.set_cookie(
                    http.cookiejar.Cookie(
                        0,
                        item["name"],
                        item["value"],
                        None,
                        False,
                        domain,
                        domain.startswith("."),
                        domain.startswith("."),
                        item.get("path", "/"),
                        True,
                        item.get("secure", False),
                        int(expiry) if expiry and expiry > 0 else None,
                        not expiry or expiry <= 0,
                        None,
                        None,
                        {},
                        False,
                    )
                )
            jar.save(ignore_discard=True, ignore_expires=True)
            yield path
        finally:
            Path(path).unlink(missing_ok=True)


async def account_cookies(repo, vault, user_id, url, mode):
    platform = platform_for(url)
    if mode == "anonymous" or not platform:
        return []
    if mode not in ("personal", "shared"):
        raise ValueError("无效的账号选择")
    row = await repo.video_account(platform, user_id if mode == "personal" else 0)
    if row:
        if mode == "shared" and not row["enabled"]:
            raise ValueError("该平台的共享账号尚未开放")
        try:
            return vault.decrypt(row["cookies"])
        except Exception:
            raise ValueError("账号凭据无法解密，请恢复原密钥或重新扫码登录") from None
    if mode == "shared":
        from bot.config import settings

        configured = getattr(settings, "video_cookie_file_" + platform, "")
        if configured:
            if await repo.video_preference(0, platform) != "shared":
                raise ValueError("配置文件共享账号尚未开放，请管理员先开放共享")
            jar = http.cookiejar.MozillaCookieJar(configured)
            jar.load(ignore_discard=True, ignore_expires=False)
            return [
                dict(name=c.name, value=c.value, domain=c.domain, path=c.path, secure=c.secure, expires=c.expires or -1)
                for c in jar
                if any(matches_host(c.domain.lstrip("."), d) for d in PLATFORMS[platform])
            ]
    raise ValueError("未登录；请扫码登录，或明确切换为共享/未登录模式")
