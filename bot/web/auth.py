import hashlib
import hmac
import os
import secrets
import time

SESSION_COOKIE = "tg_aria2_admin_session"
SESSION_TTL_SECONDS = 7 * 24 * 3600


def load_or_create_secret(path: str) -> str:
    """Random per-deployment session-signing secret, persisted next to the DB.

    Deliberately NOT the admin password: tokens are `expiry.HMAC(secret, expiry)`,
    and signing with the password would let anyone holding a single cookie
    brute-force the password offline against the signature.
    """
    try:
        with open(path, encoding="utf-8") as f:
            secret = f.read().strip()
        if secret:
            return secret
    except FileNotFoundError:
        pass
    return rotate_secret(path)


def rotate_secret(path: str) -> str:
    """Mint a fresh secret (invalidating every outstanding session) and persist it."""
    secret = secrets.token_hex(32)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(secret)
    return secret


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
