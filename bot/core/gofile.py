import logging
import os

import aiohttp

API_HOST = "https://api.gofile.io"
log = logging.getLogger(__name__)

# gofile no longer accepts truly anonymous uploads (verified live: uploadfile
# without a token now 401s) — a guest account token is required. Cache one in
# memory per process so we don't mint a fresh throwaway account on every upload.
_guest_token_cache: str | None = None


async def _pick_server() -> str:
    async with aiohttp.ClientSession() as session:
        async with session.get(f"{API_HOST}/servers", timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
    if data.get("status") != "ok" or not data.get("data", {}).get("servers"):
        raise RuntimeError(f"gofile /servers returned unexpected payload: {data}")
    return data["data"]["servers"][0]["name"]


async def _get_or_create_guest_token() -> str:
    global _guest_token_cache
    if _guest_token_cache:
        return _guest_token_cache

    async with aiohttp.ClientSession() as session:
        async with session.post(f"{API_HOST}/accounts", timeout=aiohttp.ClientTimeout(total=10)) as resp:
            data = await resp.json()
    if data.get("status") != "ok" or not data.get("data", {}).get("token"):
        raise RuntimeError(f"gofile guest account creation failed: {data}")

    _guest_token_cache = data["data"]["token"]
    log.info("created gofile guest account (tier=%s)", data["data"].get("tier"))
    return _guest_token_cache


async def upload_file(path: str, token: str | None = None) -> dict:
    """Uploads a file to gofile.io, returns the response `data` dict (includes
    downloadPage, fileId, etc.). Without a configured token, a throwaway guest
    account is created automatically (required — gofile 401s on a token-less
    upload); pass a real account token (from the user's gofile.io profile
    settings) instead for uploads to persist under their own account.
    """
    server = await _pick_server()
    if not token:
        token = await _get_or_create_guest_token()

    url = f"https://{server}.gofile.io/uploadfile"

    with open(path, "rb") as f:
        form = aiohttp.FormData()
        form.add_field("token", token)
        form.add_field("file", f, filename=os.path.basename(path))

        async with aiohttp.ClientSession() as session:
            async with session.post(url, data=form, timeout=aiohttp.ClientTimeout(total=None)) as resp:
                data = await resp.json()

    if data.get("status") != "ok":
        raise RuntimeError(f"gofile upload failed: {data}")
    return data["data"]
