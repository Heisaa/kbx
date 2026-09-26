"""kbx-login-status: is each agent logged in? JSON for the host, no secrets.

  kbx-login-status [AGENT…]   {"claude": {"ok": true, "detail": "claude.ai (max)"}, …}

`ok` is null when it cannot be told (the agent is missing or did not answer).
"""

from __future__ import annotations

import json
import subprocess
import sys
from collections.abc import Callable, Sequence
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

AGENTS = ("claude", "codex", "pi")
TIMEOUT = 30

Status = dict[str, Any]


def _run(argv: Sequence[str]) -> subprocess.CompletedProcess[str] | None:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=TIMEOUT, check=False)
    except (OSError, subprocess.TimeoutExpired):
        return None


def claude() -> Status:
    result = _run(["claude", "auth", "status"])
    try:
        data = json.loads(result.stdout) if result else None
    except ValueError:
        data = None
    if not isinstance(data, dict):
        return {"ok": None, "detail": "claude auth status gave no answer"}
    if not data.get("loggedIn"):
        return {"ok": False, "detail": "not logged in; /login in Claude"}
    method = str(data.get("authMethod") or "logged in")
    plan = data.get("subscriptionType")
    return {"ok": True, "detail": f"{method} ({plan})" if plan else method}


def codex() -> Status:
    result = _run(["codex", "login", "status"])
    if result is None:
        return {"ok": None, "detail": "codex login status gave no answer"}
    lines = [line.strip() for line in (result.stdout + result.stderr).splitlines() if line.strip()]
    lines = [line for line in lines if not line.startswith("WARNING")]
    if result.returncode != 0:
        return {"ok": False, "detail": "not logged in; sign in when Codex starts"}
    return {"ok": True, "detail": lines[-1] if lines else "logged in"}


def pi(home: Path | None = None) -> Status:
    path = (home or Path.home()) / ".pi" / "agent" / "auth.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        data = {}
    except (OSError, ValueError):
        return {"ok": None, "detail": f"cannot read {path}"}
    providers = sorted(str(key) for key in data) if isinstance(data, dict) else []
    if not providers:
        return {"ok": False, "detail": "no stored login; /login in pi (or provider keys in the environment)"}
    return {"ok": True, "detail": ", ".join(providers)}


CHECKS: dict[str, Callable[[], Status]] = {"claude": claude, "codex": codex, "pi": pi}


def main(argv: Sequence[str] | None = None) -> int:
    names = list(sys.argv[1:] if argv is None else argv) or list(AGENTS)
    if any(name not in CHECKS for name in names):
        print(__doc__, file=sys.stderr)
        return 2
    with ThreadPoolExecutor(len(names)) as pool:
        results = dict(zip(names, pool.map(lambda name: CHECKS[name](), names), strict=True))
    print(json.dumps(results))
    return 0


if __name__ == "__main__":
    sys.exit(main())
