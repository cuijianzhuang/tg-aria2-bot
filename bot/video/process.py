"""Bounded subprocess output and Linux process-group lifecycle."""

import asyncio
import contextlib
import os
import signal


async def spawn(args):
    return await asyncio.create_subprocess_exec(
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
        limit=2 * 1024 * 1024,
    )


def signal_group(process, sig):
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, sig)


async def terminate(process):
    signal_group(process, signal.SIGCONT)
    signal_group(process, signal.SIGTERM)
    try:
        await asyncio.wait_for(process.wait(), 5)
    except TimeoutError:
        signal_group(process, signal.SIGKILL)
        await process.wait()


async def capture(args, timeout=45, maximum=4 * 1024 * 1024):
    process = await spawn(args)

    async def read():
        result = bytearray()
        while chunk := await process.stdout.read(65536):
            result.extend(chunk)
            if len(result) > maximum:
                raise ValueError("解析输出过大")
        await process.wait()
        if process.returncode:
            # Output can contain signed URLs, account cookies and tokens.
            raise ValueError("解析失败：站点不支持、需登录或服务暂不可用")
        return result.decode("utf-8", errors="replace")

    try:
        return await asyncio.wait_for(read(), timeout)
    except TimeoutError:
        raise ValueError("视频解析超时，请稍后重试") from None
    finally:
        await terminate(process)
