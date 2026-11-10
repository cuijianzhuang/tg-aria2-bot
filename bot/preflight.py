"""Image deployment startup checks; never print credentials or remote bodies."""
import asyncio
import os
import tempfile
from pathlib import Path

import aiohttp
from pydantic import ValidationError


async def check() -> None:
    # Import here so invalid settings produce a safe message, not secret values
    # from Pydantic's validation traceback.
    try:
        from bot.config import settings
    except ValidationError as exc:
        fields = sorted({str(error["loc"][0]) for error in exc.errors()})
        raise RuntimeError("配置缺失或无效：" + ", ".join(fields)) from None
    for directory in (Path(settings.db_path).parent, Path(settings.download_dir)):
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryFile(dir=directory):
                pass
        except OSError:
            raise RuntimeError(f"目录不可写：{directory}，请检查 UID/GID 1000 的挂载权限") from None
    if not os.access(".env", os.W_OK):
        raise RuntimeError(".env 不可写，请检查挂载文件及 UID/GID 1000 的权限")

    from bot.core.aria2_client import Aria2Client
    client = Aria2Client(settings.aria2_rpc, settings.aria2_secret)
    try:
        await client.version()
    except Exception:
        raise RuntimeError("aria2 RPC 不可用，请检查服务地址、密钥和网络") from None
    finally:
        await client.close()
    try:
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=15)) as session:
            url = f"{settings.bot_api_url.rstrip('/')}/bot{settings.bot_token}/getMe"
            async with session.get(url) as response:
                data = await response.json()
                if response.status != 200 or not data.get("ok"):
                    raise RuntimeError
    except Exception:
        raise RuntimeError("Telegram Bot API 不可用，请检查 Bot API 服务和 BOT_TOKEN") from None


def main() -> None:
    try:
        asyncio.run(check())
    except Exception as exc:
        print(f"启动自检失败：{exc}", flush=True)
        raise SystemExit(1) from None
    print("启动自检通过", flush=True)


if __name__ == "__main__":
    main()
