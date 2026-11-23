#!/usr/bin/env python3
"""Check that requirements.lock has not drifted from the direct requirements.

requirements.lock is what the Docker image actually installs, while
requirements.txt / requirements-video.txt are what bare installs and the
tests use. The two drifted apart once already (fastapi stayed at the old
pin in the lock, and curl-cffi was missing from the lock entirely, silently
dropping a feature from the Docker deployment). This check fails if any
direct requirement is missing from the lock or pinned to a different
version there.

Scope: requirements.txt + requirements-video.txt — exactly the set the
lock is generated from. requirements-login.txt is intentionally not part
of the lock (the login stage installs it separately).

Usage: python scripts/check_requirements_lock.py
Exit code 0 = in sync, 1 = drift (details on stdout).
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DIRECT_FILES = ["requirements.txt", "requirements-video.txt"]
LOCK_FILE = "requirements.lock"

_PIN_RE = re.compile(r"^([A-Za-z0-9._-]+)(?:\[[^\]]*\])?\s*==\s*([^\s;#]+)")


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def parse_pins(path: Path) -> dict[str, str]:
    pins: dict[str, str] = {}
    for raw in path.read_text().splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        match = _PIN_RE.match(line)
        if not match:
            raise SystemExit(f"{path.name}: unpinned or unparsable requirement line: {raw!r}")
        pins[_normalize(match.group(1))] = match.group(2)
    return pins


def main() -> int:
    lock = parse_pins(ROOT / LOCK_FILE)
    problems: list[str] = []
    for filename in DIRECT_FILES:
        for name, version in parse_pins(ROOT / filename).items():
            locked = lock.get(name)
            if locked is None:
                problems.append(f"{name}=={version} ({filename}) is missing from {LOCK_FILE}")
            elif locked != version:
                problems.append(
                    f"{name}: {filename} pins {version} but {LOCK_FILE} has {locked}"
                )
    if problems:
        print("requirements.lock is out of sync with the direct requirements:")
        for problem in problems:
            print(f"  - {problem}")
        print("Regenerate the lock (pip-compile) and commit the result.")
        return 1
    print(f"{LOCK_FILE} is in sync with {' + '.join(DIRECT_FILES)}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
