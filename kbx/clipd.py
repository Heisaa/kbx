"""Host clipboard sync: push images in and pull new sandbox text copies out.

The sandbox may not connect to the host, so the host pushes and the sandbox
never calls out. One clipd runs per sandbox while any agent session is
attached (see attached.py).
"""

from __future__ import annotations

import base64
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

from . import attached
from .docker import Docker
from .errors import KbxError
from .paths import Paths

MAX_IMAGE = 64 * 1024 * 1024
PNG = "image/png"
MAX_TEXT = 1024 * 1024
MAX_RECORD = 4 * ((MAX_TEXT + 2) // 3)
DAEMON = "clipd"


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


def register(paths: Paths, name: str, pid: int, entry: Path) -> None:
    attached.register(paths, DAEMON, name, pid, entry)


def run(paths: Paths, name: str) -> int:
    kind = backend(os.environ)
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
            if time.monotonic() > grace and attached.live(paths, DAEMON, name) == 0:
                return 0
    finally:
        stop.set()
        copies.join(timeout=15)
        attached.release(paths, DAEMON, name)
