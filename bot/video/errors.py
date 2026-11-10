"""Classify downloader failures without exposing signed URLs or credentials."""


def download_error_hint(output: str) -> str | None:
    text = output.lower()
    if "http error 403" in text or "403 forbidden" in text:
        return "视频源拒绝访问（HTTP 403），请检查账号权限、登录状态或平台防盗链限制"
    if "http error 412" in text or "412 precondition" in text:
        return "平台触发访问限制（HTTP 412），请稍后重试并检查账号登录状态"
    if "http error 429" in text or "too many requests" in text:
        return "平台请求过于频繁（HTTP 429），请稍后再试"
    if "timed out" in text or "timeout" in text:
        return "视频源连接超时，请检查服务器到视频站点的网络"
    if "requested format" in text and "not available" in text:
        return "所选画质已不可用，请重新解析或切换画质"
    if any(phrase in text for phrase in ("sign in", "login required", "cookies are no longer valid", "not logged in")):
        return "平台要求有效登录，请重新扫码后重试"
    return None
