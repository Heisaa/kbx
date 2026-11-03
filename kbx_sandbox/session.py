"""kbx-session: which dtach sessions have a live master.

kbx-session list          JSON list of live sessions
kbx-session alive KEY     exit 0 if session KEY is live
kbx-session status        JSON: each live session's state (see kbx-notify) and
                          whether a terminal is attached

A session's key is its agent (`claude`) in a project sandbox, and agent@project
(`claude@api-1a2b3c`) in a profile sandbox, where each project has its own.
"""

from __future__ import annotations

import json
import re
import socket
import stat
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

SESSIONS = Path("/run/kbx/sessions")
STATES = Path("/run/kbx/agents")
KEY = re.compile(r"^(claude|codex|pi)(@[a-z0-9][a-z0-9.-]{0,39})?$")


def valid(key: str) -> bool:
    return KEY.match(key) is not None


def alive(path: Path) -> bool:
    """A socket file alone may be stale; a live dtach master accepts connections."""
    if not path.is_socket():
        return False
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(1)
        try:
            sock.connect(str(path))
        except OSError:
            return False
    return True


def live_sessions(root: Path = SESSIONS) -> list[str]:
    if not root.is_dir():
        return []
    return sorted(path.stem for path in root.glob("*.sock") if valid(path.stem) and alive(path))


def attached(path: Path) -> bool:
    """dtach sets the socket's execute bit while a client is attached."""
    try:
        return bool(path.stat().st_mode & stat.S_IXUSR)
    except OSError:
        return False


def state(key: str, started: float, root: Path = STATES) -> dict[str, Any]:
    """The last state kbx-notify recorded for this session, if newer than the session."""
    try:
        data = json.loads((root / f"{key}.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict) or not isinstance(data.get("at"), int | float) or data["at"] < started:
        return {}  # from an earlier session of this key
    return {key: data[key] for key in ("state", "message", "at") if key in data}


def status(root: Path = SESSIONS, states: Path = STATES) -> dict[str, dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for key in live_sessions(root):
        path = root / f"{key}.sock"
        try:
            started = path.stat().st_mtime  # dtach's chmod on attach leaves the mtime alone
        except OSError:
            continue
        result[key] = {"attached": attached(path), **state(key, started, states)}
    return result


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["list"]:
        print(json.dumps(live_sessions()))
        return 0
    if args == ["status"]:
        print(json.dumps(status()))
        return 0
    if len(args) == 2 and args[0] == "alive":
        return 0 if valid(args[1]) and alive(SESSIONS / f"{args[1]}.sock") else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
