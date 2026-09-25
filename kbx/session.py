"""dtach command lines and the final `execvpe` attach.

dtach keeps each agent running on a socket in /run/kbx/sessions and does
nothing else: no prefix key, no panes, no scrollback. The only key it
intercepts is the detach key, which can be disabled.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Mapping, Sequence

from .docker import Docker
from .errors import KbxError
from .sandbox import Sandbox

SESSIONS = "/run/kbx/sessions"
DISPLAY = ":0"


def socket_path(agent: str) -> str:
    return f"{SESSIONS}/{agent}.sock"


def _detach_flags(detach_key: str) -> list[str]:
    if detach_key.lower() in ("", "none"):
        return ["-E"]
    return ["-e", detach_key]


def dtach_start(agent: str, detach_key: str, argv: Sequence[str]) -> list[str]:
    """Start-or-attach: `-A` creates the session if the socket has no master."""
    return ["dtach", "-A", socket_path(agent), *_detach_flags(detach_key), "-r", "winch", *argv]


def dtach_attach(agent: str, detach_key: str) -> list[str]:
    return ["dtach", "-a", socket_path(agent), *_detach_flags(detach_key), "-r", "winch"]


def alive(docker: Docker, sandbox: Sandbox, agent: str) -> bool:
    return docker.exec(sandbox.name, ["kbx-session", "alive", agent], check=False, timeout=15).returncode == 0


def session_env(extra: Mapping[str, str]) -> dict[str, str]:
    """Environment inside the sandbox for an attached agent."""
    env = {"DISPLAY": DISPLAY}
    for key in ("TERM", "COLORTERM", "TERM_PROGRAM", "LANG"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    env.update(extra)
    return env


def exec_attach(
    docker: Docker,
    sandbox: Sandbox,
    inner: Sequence[str],
    *,
    workdir: str,
    env: Mapping[str, str],
    host_env: Mapping[str, str],
) -> None:
    """Replace this process with `docker exec -it … <inner>`. Never returns."""
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise KbxError("attaching needs a terminal (stdin and stdout must be a TTY)")
    args = docker.exec_args(sandbox.name, inner, workdir=workdir, env=env, interactive=True, tty=True)
    binary = docker.binary_path()
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvpe(binary, ["docker", *args], dict(host_env))
