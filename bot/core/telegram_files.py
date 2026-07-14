import os

from bot.config import settings


def to_download_uri(file_path: str) -> str:
    """Build a source aria2 can fetch from, given a Bot API getFile() file_path.

    telegram-bot-api running with --local (which we always use, for the raised
    file-size limit) returns file_path as an *absolute path already on local disk*
    instead of a relative path meant for the HTTP /file/bot<token>/<path> endpoint —
    concatenating it onto that endpoint produces a malformed, 404ing URL. Detect
    which kind we got: an absolute path becomes a file:// URI (works whenever the
    caller has filesystem access to it — always true bare-metal, needs a shared
    volume across containers in docker mode); anything else is a normal relative
    path for the HTTP endpoint.
    """
    if os.path.isabs(file_path):
        return f"file://{file_path}"
    return f"{settings.bot_api_url}/file/bot{settings.bot_token}/{file_path}"


def to_local_path(file_path: str) -> str | None:
    """Same detection as to_download_uri, but for callers that need to read the
    file themselves (not hand a URI to aria2) — e.g. loading .torrent bytes.
    Returns the absolute path if already resolvable locally, None if it still needs
    to be downloaded over HTTP.
    """
    return file_path if os.path.isabs(file_path) else None
