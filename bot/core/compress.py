import os
import shutil


def compress_path(path: str) -> str:
    """Zip a completed download (single file or a multi-file torrent's directory)
    in place; returns the path to the resulting .zip, left next to the original.
    """
    base = path.rstrip("/\\")
    archive_path = shutil.make_archive(
        base, "zip", root_dir=os.path.dirname(base), base_dir=os.path.basename(base)
    )
    return archive_path


def remove_path(path: str):
    if os.path.isdir(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.exists(path):
        os.remove(path)
