import hashlib
import os
import shutil

CATEGORY_BY_EXT = {
    ".mp4": "video", ".mkv": "video", ".mov": "video", ".avi": "video",
    ".mp3": "audio", ".flac": "audio", ".m4a": "audio", ".wav": "audio",
    ".jpg": "photo", ".jpeg": "photo", ".png": "photo", ".webp": "photo",
    ".zip": "archive", ".rar": "archive", ".7z": "archive",
}


def category_for(file_name: str | None) -> str:
    if not file_name:
        return "other"
    ext = os.path.splitext(file_name)[1].lower()
    return CATEGORY_BY_EXT.get(ext, "other")


def build_subdir(download_dir: str, file_name: str | None) -> str:
    category = category_for(file_name)
    path = os.path.join(download_dir, category)
    os.makedirs(path, exist_ok=True)
    return path


def url_hash(url: str) -> str:
    return hashlib.sha256(url.strip().lower().encode()).hexdigest()[:16]


# torrent/magnet sizes are unknown before download starts, so the size-based
# check alone lets them through on a nearly-full disk — keep a hard floor too
MIN_FREE_BYTES = 1 * 1024**3


def has_enough_space(download_dir: str, required_bytes: int, safety_factor: float = 1.2) -> bool:
    usage = shutil.disk_usage(download_dir)
    return usage.free > max(required_bytes * safety_factor, MIN_FREE_BYTES)


def _human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024:
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}PB"


def disk_usage_summary(path: str) -> dict:
    usage = shutil.disk_usage(path)
    return {
        "total": _human_size(usage.total),
        "used": _human_size(usage.used),
        "free": _human_size(usage.free),
        "percent_used": round(usage.used / usage.total * 100, 1) if usage.total else 0,
    }
