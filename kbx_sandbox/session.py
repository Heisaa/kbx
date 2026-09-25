"""kbx-session: which dtach sessions have a live master.

kbx-session list          JSON list of agents with a live session
kbx-session alive AGENT   exit 0 if AGENT's session is live
"""

from __future__ import annotations

import json
import socket
import sys
from collections.abc import Sequence
from pathlib import Path

SESSIONS = Path("/run/kbx/sessions")


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
    return sorted(path.stem for path in root.glob("*.sock") if alive(path))


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["list"]:
        print(json.dumps(live_sessions()))
        return 0
    if len(args) == 2 and args[0] == "alive":
        return 0 if alive(SESSIONS / f"{args[1]}.sock") else 1
    print(__doc__, file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
