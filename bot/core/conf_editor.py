import os
import re

from bot.config import settings


def aria2_conf_path() -> str:
    return os.path.join(settings.aria2_config_dir, "aria2.conf")


def script_conf_path() -> str:
    return os.path.join(settings.aria2_config_dir, "script.conf")


def rclone_conf_path() -> str:
    return os.path.join(settings.aria2_config_dir, "rclone.conf")


# Only used for values coming from the web admin into shell scripts (script.conf is
# sourced by aria2's hook scripts via `grep | cut`, then interpolated inside double
# quotes in shell — e.g. "${DRIVE_NAME}:${DRIVE_DIR}"). Double quotes block most
# injection but not $(...) / `...` command substitution, so reject anything but a
# conservative charset rather than trust free-form admin input verbatim.
SAFE_VALUE_RE = re.compile(r"^[A-Za-z0-9._/\- ]*$")


def is_safe_value(value: str) -> bool:
    return bool(SAFE_VALUE_RE.match(value))


def read_kv(path: str, key: str) -> str | None:
    """Read the first uncommented `key=value` line from a flat aria2-style config file."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                stripped = line.strip()
                if stripped.startswith(f"{key}="):
                    return stripped[len(key) + 1:]
    except FileNotFoundError:
        return None
    return None


def write_kv(path: str, key: str, value: str | None):
    """Set `key=value` in place, uncommenting existing lines as needed.

    value=None comments the key out instead of deleting it, preserving the
    upstream P3TERX file's documentation comments around it.
    """
    with open(path, "r", encoding="utf-8") as f:
        lines = f.readlines()

    pattern = re.compile(rf"^\s*#?\s*{re.escape(key)}=")
    found = False
    new_lines = []
    for line in lines:
        if pattern.match(line):
            found = True
            new_lines.append(f"#{key}=\n" if value is None else f"{key}={value}\n")
        else:
            new_lines.append(line)

    if not found and value is not None:
        new_lines.append(f"{key}={value}\n")

    with open(path, "w", encoding="utf-8") as f:
        f.writelines(new_lines)


REMOTE_SECTION_RE = re.compile(r"^\[([^\]]+)\]$")


def list_rclone_remotes(rclone_conf_path: str) -> list[str]:
    """Parse remote names straight out of rclone.conf's `[name]` section headers.

    Avoids needing the rclone binary in this container just to list configured
    remotes — rclone.conf is a plain INI file, one `[name]` header per remote.
    """
    try:
        with open(rclone_conf_path, "r", encoding="utf-8") as f:
            return [m.group(1) for line in f if (m := REMOTE_SECTION_RE.match(line.strip()))]
    except FileNotFoundError:
        return []
