"""Host clipboard sync: push images in and pull new sandbox text copies out.

The sandbox may not connect to the host, so the host pushes and the sandbox
never calls out. One clipd runs per sandbox while any agent session is
attached. Each attach registers its own PID (it becomes the `docker exec`
process after `execvpe`), and clipd exits once none of them is alive.
"""

from __future__ import annotations

import base64
import fcntl
import hashlib
import os
import queue
import select
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Mapping
from pathlib import Path

from .docker import Docker
from .errors import KbxError
from .paths import Paths

MAX_IMAGE = 64 * 1024 * 1024
PNG = "image/png"
MAX_TEXT = 1024 * 1024
MAX_RECORD = 4 * ((MAX_TEXT + 2) // 3)


def _starttime(pid: int) -> str | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # The command name may contain spaces or parentheses; fields follow the last ")".
    fields = stat[stat.rfind(")") + 2 :].split()
    return fields[19] if len(fields) > 19 else None


def _files(paths: Paths, name: str) -> tuple[Path, Path, Path]:
    base = paths.runtime_dir
    return base / f"clipd-{name}.lock", base / f"clipd-{name}.pids", base / f"clipd-{name}.pid"


def backend(env: Mapping[str, str]) -> str | None:
    """ "wayland", "x11" or None when the needed tools are missing."""
    if env.get("WAYLAND_DISPLAY") and shutil.which("wl-paste") and shutil.which("wl-copy"):
        return "wayland"
    if env.get("DISPLAY") and shutil.which("xclip") and shutil.which("clipnotify"):
        return "x11"
    return None


def missing_tools_hint(env: Mapping[str, str]) -> str:
    if env.get("WAYLAND_DISPLAY"):
        return "install wl-clipboard (wl-paste and wl-copy)"
    if env.get("DISPLAY"):
        return "install xclip and clipnotify"
    return "no Wayland or X11 session found"


def _alive(pid: int) -> bool:
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    return b"_clipd" in cmdline


def register(paths: Paths, name: str, pid: int, entry: Path) -> None:
    """Register an attach PID and make sure a clipd runs for this sandbox."""
    paths.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path, pids_path, pid_path = _files(paths, name)
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        start = _starttime(pid)
        with open(pids_path, "a") as handle:
            handle.write(f"{pid} {start}\n")
        try:
            current = int(pid_path.read_text().strip())
        except (OSError, ValueError):
            current = 0
        if current and _alive(current):
            return
        paths.log_dir.mkdir(parents=True, exist_ok=True)
        with open(paths.log_dir / f"clipd-{name}.log", "ab") as log:
            process = subprocess.Popen(
                [sys.executable, str(entry), "_clipd", name],
                stdin=subprocess.DEVNULL,
                stdout=log,
                stderr=log,
                start_new_session=True,
                close_fds=True,
            )
        pid_path.write_text(f"{process.pid}\n")


def _live_attaches(pids_path: Path, lock_path: Path) -> int:
    with open(lock_path, "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            lines = pids_path.read_text().splitlines()
        except OSError:
            return 0
        alive: list[str] = []
        for line in lines:
            pid_text, _, start = line.partition(" ")
            if pid_text.isdigit() and _starttime(int(pid_text)) == start:
                alive.append(line)
        pids_path.write_text("".join(f"{line}\n" for line in alive))
        return len(alive)


class Watcher:
    """Reads the host clipboard with wl-paste or xclip."""

    def __init__(self, kind: str) -> None:
        self.kind = kind

    def types(self) -> list[str]:
        if self.kind == "wayland":
            argv = ["wl-paste", "--list-types"]
        else:
            argv = ["xclip", "-selection", "clipboard", "-t", "TARGETS", "-o"]
        result = subprocess.run(argv, capture_output=True, timeout=5, check=False)
        if result.returncode != 0:
            return []
        return [line.strip() for line in result.stdout.decode("utf-8", "replace").splitlines() if line.strip()]

    def image(self) -> bytes | None:
        if self.kind == "wayland":
            argv = ["wl-paste", "--no-newline", "--type", PNG]
        else:
            argv = ["xclip", "-selection", "clipboard", "-t", PNG, "-o"]
        result = subprocess.run(argv, capture_output=True, timeout=10, check=False)
        data = result.stdout
        if result.returncode != 0 or not data.startswith(b"\x89PNG\r\n\x1a\n") or len(data) > MAX_IMAGE:
            return None
        return data

    def copy(self, data: bytes) -> bool:
        """Write sandbox text to the desktop clipboard without logging it."""
        if self.kind == "wayland":
            argv = ["wl-copy", "--type", "text/plain;charset=utf-8"]
        else:
            argv = ["xclip", "-selection", "clipboard", "-target", "UTF8_STRING", "-in"]
        result = subprocess.run(
            argv, input=data, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=5, check=False
        )
        return result.returncode == 0

    def watch(self, notify: Callable[[], None], stop: threading.Event) -> None:
        """Call notify() on every clipboard change until stop is set."""
        if self.kind == "wayland":
            process = subprocess.Popen(
                ["wl-paste", "--watch", "sh", "-c", "cat >/dev/null; echo"],
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
            )
            assert process.stdout is not None
            for _ in process.stdout:
                if stop.is_set():
                    break
                notify()
            process.terminate()
            _, err = process.communicate()
            if stop.is_set():
                return
            # Compositors without the data-control protocol (e.g. GNOME) cannot
            # be watched; use the X11 clipboard through XWayland instead.
            print(f"wl-paste --watch stopped: {err.decode(errors='replace').strip()}", file=sys.stderr)
            if not (os.environ.get("DISPLAY") and shutil.which("xclip") and shutil.which("clipnotify")):
                print("no X11 fallback (needs DISPLAY, xclip and clipnotify); image paste is off", file=sys.stderr)
                return
            self.kind = "x11"
            notify()
        argv = ["clipnotify", "-s", "clipboard"]
        while not stop.is_set():
            started = time.monotonic()
            result = subprocess.run(argv, capture_output=True, check=False)
            if result.returncode != 0 and time.monotonic() - started < 0.5:
                if argv[1:]:
                    argv = ["clipnotify"]  # older clipnotify without -s
                    continue
                time.sleep(1)
                continue
            notify()


def push(docker: Docker, name: str, data: bytes | None) -> bool:
    argv = ["kbx-clip-put", PNG] if data is not None else ["kbx-clip-put", "--clear"]
    result = docker.exec(name, argv, input=data if data is not None else b"", check=False, timeout=30)
    return result.returncode == 0


def copy_record(watcher: Watcher, record: bytes) -> None:
    if not record or len(record) > MAX_RECORD:
        return
    try:
        data = base64.b64decode(record, validate=True)
        if len(data) > MAX_TEXT:
            return
        data.decode("utf-8")
        if not watcher.copy(data):
            print("could not copy sandbox text to the host clipboard", file=sys.stderr)
    except (ValueError, OSError, subprocess.TimeoutExpired):
        print("could not copy sandbox text to the host clipboard", file=sys.stderr)


def watch_copies(docker: Docker, name: str, watcher: Watcher, stop: threading.Event) -> None:
    """Host-initiated stream; one listener per attached sandbox, no guest networking."""
    while not stop.is_set():
        try:
            process = docker.exec_stream(name, ["kbx-clip-export"], env={"DISPLAY": ":0"})
        except (OSError, KbxError) as exc:
            print(f"cannot listen for sandbox copies: {exc}", file=sys.stderr)
            if stop.wait(5):
                return
            continue
        assert process.stdout is not None and process.stdin is not None
        try:
            buffer = bytearray()
            while not stop.is_set():
                if not select.select([process.stdout], [], [], 0.2)[0]:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                buffer.extend(chunk)
                while b"\n" in buffer:
                    record, _, rest = buffer.partition(b"\n")
                    buffer = bytearray(rest)
                    copy_record(watcher, bytes(record))
                if len(buffer) > MAX_RECORD:
                    print("sandbox clipboard record exceeds the text limit", file=sys.stderr)
                    break
        except OSError as exc:
            print(f"sandbox copy stream failed: {exc}", file=sys.stderr)
        finally:
            # EOF also stops the remote listener, even while it is idle.
            process.stdin.close()
            try:
                process.wait(timeout=7)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()
        if stop.wait(5):
            return


def run(paths: Paths, name: str) -> int:
    kind = backend(os.environ)
    lock_path, pids_path, pid_path = _files(paths, name)
    if kind is None:
        return 0
    docker = Docker()
    watcher = Watcher(kind)
    events: queue.Queue[None] = queue.Queue()
    stop = threading.Event()
    thread = threading.Thread(target=watcher.watch, args=(lambda: events.put(None), stop), daemon=True)
    thread.start()
    copies = threading.Thread(target=watch_copies, args=(docker, name, watcher, stop), daemon=True)
    copies.start()
    last: str | None = None  # sha256 of the last pushed image, "" for clear
    grace = time.monotonic() + 10
    pending = True  # push the current clipboard once at startup
    try:
        while True:
            if pending:
                pending = False
                data = watcher.image() if PNG in watcher.types() else None
                state = hashlib.sha256(data).hexdigest() if data is not None else ""
                if state != last and push(docker, name, data):
                    last = state
            try:
                events.get(timeout=1)
                pending = True
                while not events.empty():
                    events.get_nowait()
            except queue.Empty:
                pass
            if time.monotonic() > grace and _live_attaches(pids_path, lock_path) == 0:
                return 0
    finally:
        stop.set()
        copies.join(timeout=15)
        try:
            if pid_path.read_text().strip() == str(os.getpid()):
                pid_path.unlink()
        except OSError:
            pass
