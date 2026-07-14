import hmac
import hashlib
import time

SESSION_COOKIE = "tg_aria2_admin_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600


def _sign(secret: str, payload: str) -> str:
    return hmac.new(secret.encode(), payload.encode(), hashlib.sha256).hexdigest()


def create_session_token(secret: str) -> str:
    expiry = str(int(time.time()) + SESSION_TTL_SECONDS)
    return f"{expiry}.{_sign(secret, expiry)}"


def verify_session_token(secret: str, token: str | None) -> bool:
    if not token or "." not in token:
        return False
    expiry_str, sig = token.split(".", 1)
    if not expiry_str.isdigit():
        return False
    if not hmac.compare_digest(sig, _sign(secret, expiry_str)):
        return False
    return int(expiry_str) > time.time()
