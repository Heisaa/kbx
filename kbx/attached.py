"""Host daemons that run per sandbox while any agent session is attached.

Each attach registers its own PID (it becomes the `docker exec` process after
`execvpe`), and the daemon exits once none of them is alive. The clipboard
watcher (`_clipd`) and the voice link (`_voiced`) work this way.
"""

from __future__ import annotations

import fcntl
import os
import subprocess
import sys
from pathlib import Path

from .paths import Paths


def starttime(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name may contain spaces or parentheses; fields follow the last ")".
    fields = stat[stat.rfind(")") + 2 :].split()
    return fields[19] if len(fields) > 19 else None


def files(paths: Paths, daemon: str, name: str) -> tuple[Path, Path, Path]:
    base = paths.runtime_dir
    return base / f"{daemon}-{name}.lock", base / f"{daemon}-{name}.pids", base / f"{daemon}-{name}.pid"


def _alive(pid: int, daemon: str) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return f"_{daemon}".encode() in cmdline


def register(paths: Paths, daemon: str, name: str, pid: int, entry: Path) -> None:
    """Register an attach PID and make sure `kbx _<daemon> <name>` runs."""
    paths.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path, pids_path, pid_path = files(paths, daemon, name)
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        start = starttime(pid)
        with open(pids_path, "a") as handle:
            handle.write(f"{pid} {start}\n")
        try:
            current = int(pid_path.read_text().strip())
        except (OSError, ValueError):
            current = 0
        if current and _alive(current, daemon):
            return
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        with open(paths.log_dir / f"{daemon}-{name}.log", "ab") as log:
            process = subprocess.Popen(
                [sys.executable, str(entry), f"_{daemon}", name],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )
        pid_path.write_text(f"{process.pid}\n")


def live(paths: Paths, daemon: str, name: str) -> int:
    """Number of registered attaches still alive; forgets the others."""
    lock_path, pids_path, _ = files(paths, daemon, name)
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            lines = pids_path.read_text().splitlines()
        except OSError:
            return 0
        alive: list[str] = []
        for line in lines:
            pid_text, _, start = line.partition(" ")
            if pid_text.isdigit() and starttime(int(pid_text)) == start:
                alive.append(line)
        pids_path.write_text("".join(f"{line}\n" for line in alive))
        return len(alive)


def release(paths: Paths, daemon: str, name: str) -> None:
    """Remove the daemon's PID file if it is still this process's."""
    pid_path = files(paths, daemon, name)[2]
    try:
        if pid_path.read_text().strip() == str(os.getpid()):
            pid_path.unlink()
    except OSError:
        pass
