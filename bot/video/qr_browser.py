"""QR discovery and safe failure reasons, independent of browser installation."""

import asyncio

REASONS = {
    "browser_missing": "未安装扫码浏览器，请管理员重新安装扫码组件",
    "browser_dependency": "扫码浏览器缺少系统依赖，请管理员检查扫码服务日志并修复安装",
    "browser_launch": "扫码浏览器无法启动，请管理员检查服务权限和运行环境",
    "network_timeout": "连接平台超时，请检查服务器网络后重试",
    "platform_blocked": "平台限制了当前服务器访问，请稍后重试或使用 Cookie 配置",
    "platform_unavailable": "平台登录页面暂不可用，请稍后重试",
    "qr_missing": "没有找到二维码，登录窗口可能未加载或平台页面已变化",
    "qr_capture": "二维码图片生成失败，请刷新重试",
    "unexpected": "扫码过程失败，请管理员查看扫码服务的错误类型",
}
# A visible preloaded captcha container is not evidence of a challenge. Require
# a visible instruction instead of inferring verification from CSS class names.
VERIFICATION_PROMPTS = (
    "text=请完成验证",
    "text=请完成下列验证",
    "text=请完成以下验证",
    "text=拖动滑块",
    "text=拖动下方滑块",
    "text=请依次点击",
    "text=请按顺序点击",
)


class QRLoginError(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(REASONS[reason])


def failure_reason(exc, stage):
    if isinstance(exc, QRLoginError):
        return exc.reason
    if isinstance(exc, (ImportError, ModuleNotFoundError)):
        return "browser_missing"
    # Inspect locally, but never persist or log raw browser exceptions: these
    # may contain signed URLs, proxy credentials, cookies and browser commands.
    text = str(exc).lower()
    if "executable doesn't exist" in text or "playwright install" in text:
        return "browser_missing"
    if any(value in text for value in ("error while loading shared libraries", "missing dependencies", "libatomic.so")):
        return "browser_dependency"
    if stage == "launch":
        return "browser_launch"
    if stage == "navigation":
        return (
            "network_timeout"
            if "timeout" in type(exc).__name__.lower() or "timed out" in text
            else "platform_unavailable"
        )
    return "qr_capture" if stage == "capture" else "unexpected"


async def visible_locator(page, selectors, minimum_size=0):
    for frame in page.frames:
        for selector in selectors:
            matches = frame.locator(selector)
            for index in range(min(await matches.count(), 5)):
                locator = matches.nth(index)
                if await locator.is_visible():
                    if minimum_size:
                        box = await locator.bounding_box()
                        if not box or min(box["width"], box["height"]) < minimum_size:
                            continue
                    return locator
    return None


async def open_login(page, selectors):
    locator = await visible_locator(page, selectors)
    if locator:
        try:
            await locator.click(timeout=1500)
        except Exception:
            pass  # A disappearing login button is normal during modal loading.


async def verification_required(page, *, confirmation_delay=2.0):
    if not await visible_locator(page, VERIFICATION_PROMPTS):
        return False
    # Some platform checks disappear after initialization without interaction.
    await asyncio.sleep(confirmation_delay)
    return bool(await visible_locator(page, VERIFICATION_PROMPTS))
