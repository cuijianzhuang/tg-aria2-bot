"""One metadata concurrency budget for message previews and queued downloads."""

import asyncio

from bot.config import settings
from bot.video.backends import VideoResolver

_resolver = None
_loop = None


def get_resolver():
    global _resolver, _loop
    loop = asyncio.get_running_loop()
    if _resolver is None or loop is not _loop:
        _loop = loop
        _resolver = VideoResolver(settings.video_probe_concurrency, settings.video_probe_timeout)
    return _resolver
