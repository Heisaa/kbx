"""Clipboard bridge: serve host images pushed into ~/.cache/kbx-clipboard/ as the X11 CLIPBOARD.

One X11 clipboard on Xvfb :0 serves all three agents: Claude and pi run
`xclip`, Codex reads X11 directly through arboard. The bridge owns CLIPBOARD
while an image is present and releases it on a clear. It advertises exactly
TARGETS, TIMESTAMP and image/png and refuses every other target, so a client
probing for text never receives image bytes (earendil-works/pi#9786).

Ported from an earlier sbx kit: the X11 selection and INCR logic are kept;
the on-demand HTTP fetch is replaced by the host-pushed file source.
"""

from __future__ import annotations

import argparse
import ctypes
import fcntl
import json
import logging
import os
import select
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from Xlib import X, Xatom, display, error, protocol  # pyright: ignore[reportMissingTypeStubs]

CHUNK = 64 * 1024
MAX_IMAGE = 64 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PNG = "image/png"
POLL = 0.2
INCR_TIMEOUT = 10.0


class FileSource:
    """The latest image pushed by kbx-clip-put, or None after a clear."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self.stamp: tuple[int, int] | None = None
        self.seq = -1
        self.data: bytes | None = None

    def poll(self) -> bool:
        """Reload if meta.json changed. Returns True when the content changed."""
        meta = self.root / "meta.json"
        try:
            info = meta.stat()
        except OSError:
            if self.stamp is None:
                return False
            self.stamp, self.seq = None, -1
            changed, self.data = self.data is not None, None
            return changed
        stamp = (info.st_ino, info.st_mtime_ns)
        if stamp == self.stamp:
            return False
        self.stamp = stamp
        try:
            record = json.loads(meta.read_text())
        except (OSError, ValueError):
            return False
        seq = int(record.get("seq", 0))
        if seq == self.seq:
            return False
        self.seq = seq
        data: bytes | None = None
        if record.get("mime") == PNG:
            try:
                with (self.root / "data").open("rb") as handle:
                    data = handle.read(MAX_IMAGE + 1)
            except OSError:
                data = None
            if data is not None and (len(data) > MAX_IMAGE or not data.startswith(PNG_MAGIC)):
                logging.warning("ignoring invalid clipboard data (%d bytes)", len(data))
                data = None
        self.data = data
        return True


class Clipboard:
    def __init__(self, connection: Any, source: FileSource) -> None:
        self.d = connection
        self.source = source
        self.atoms = {
            name: self.d.intern_atom(name) for name in ("CLIPBOARD", "TARGETS", "TIMESTAMP", PNG, "INCR", "KBX_TIME")
        }
        self.window = self.d.screen().root.create_window(
            0, 0, 1, 1, 0, X.CopyFromParent, event_mask=X.PropertyChangeMask
        )
        self.transfers: dict[tuple[int, int], tuple[Any, int, bytes, int, float]] = {}
        self.owned = False
        self.acquired = 0
        self.data: bytes | None = None

    def server_time(self) -> int:
        """A real server timestamp (ICCCM discourages CurrentTime for ownership)."""
        self.window.change_property(self.atoms["KBX_TIME"], Xatom.STRING, 8, b"")
        self.d.flush()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            if not self.d.pending_events():
                select.select([self.d], [], [], 0.05)
                continue
            event = self.d.next_event()
            if (
                event.type == X.PropertyNotify
                and event.window.id == self.window.id
                and event.atom == self.atoms["KBX_TIME"]
            ):
                return int(event.time)
            self.dispatch(event)
        return X.CurrentTime

    def own(self) -> None:
        timestamp = self.server_time()
        self.window.set_selection_owner(self.atoms["CLIPBOARD"], timestamp)
        self.d.sync()
        owner = self.d.get_selection_owner(self.atoms["CLIPBOARD"])
        self.owned = bool(owner) and owner.id == self.window.id
        self.acquired = timestamp
        if not self.owned:
            logging.warning("could not take the CLIPBOARD selection")

    def release(self) -> None:
        owner = self.d.get_selection_owner(self.atoms["CLIPBOARD"])
        if owner and owner.id == self.window.id:
            protocol.request.SetSelectionOwner(
                display=self.d.display, window=X.NONE, selection=self.atoms["CLIPBOARD"], time=X.CurrentTime
            )
        self.d.sync()
        self.owned = False

    def refresh(self) -> None:
        if not self.source.poll():
            return
        self.data = self.source.data
        self.transfers.clear()
        if self.data is not None:
            self.own()
        else:
            self.release()

    def request(self, event: Any) -> None:
        prop = event.property or event.target
        result = X.NONE
        try:
            if event.selection != self.atoms["CLIPBOARD"] or self.data is None:
                pass
            elif event.target == self.atoms["TARGETS"]:
                event.requestor.change_property(
                    prop, Xatom.ATOM, 32, [self.atoms["TARGETS"], self.atoms["TIMESTAMP"], self.atoms[PNG]]
                )
                result = prop
            elif event.target == self.atoms["TIMESTAMP"]:
                event.requestor.change_property(prop, Xatom.INTEGER, 32, [self.acquired])
                result = prop
            elif event.target == self.atoms[PNG]:
                data = self.data
                if len(data) <= CHUNK:
                    event.requestor.change_property(prop, event.target, 8, data)
                else:
                    event.requestor.change_attributes(event_mask=X.PropertyChangeMask)
                    self.transfers[event.requestor.id, prop] = (
                        event.requestor, event.target, data, 0, time.monotonic(),
                    )  # fmt: skip
                    event.requestor.change_property(prop, self.atoms["INCR"], 32, [len(data)])
                result = prop
            # Any other target (text/plain, UTF8_STRING, image/*, MULTIPLE…) is refused.
        except (OSError, ValueError, error.XError):
            logging.exception("clipboard request failed")
            result = X.NONE
        event.requestor.send_event(
            protocol.event.SelectionNotify(
                time=event.time,
                requestor=event.requestor,
                selection=event.selection,
                target=event.target,
                property=result,
            )
        )
        self.d.flush()

    def property(self, event: Any) -> None:
        key = event.window.id, event.atom
        if event.state == X.PropertyDelete and key in self.transfers:
            window, target, data, offset, _ = self.transfers[key]
            chunk = data[offset : offset + CHUNK]
            window.change_property(event.atom, target, 8, chunk)
            if chunk:
                self.transfers[key] = window, target, data, offset + len(chunk), time.monotonic()
            else:
                del self.transfers[key]
            self.d.flush()

    def dispatch(self, event: Any) -> None:
        try:
            if event.type == X.SelectionRequest:
                self.request(event)
            elif event.type == X.PropertyNotify:
                self.property(event)
            elif event.type == X.SelectionClear:
                # Another client (e.g. an agent's own copy) took CLIPBOARD. Text
                # copies reach the host through the terminal (OSC 52), not here.
                self.owned = False
        except (OSError, ValueError, error.XError):
            logging.exception("clipboard event failed")

    def run(self, wake_fd: int | None = None) -> None:
        self.refresh()
        sources: list[Any] = [self.d] + ([wake_fd] if wake_fd is not None else [])
        while True:
            while self.d.pending_events():
                self.dispatch(self.d.next_event())
            now = time.monotonic()
            self.transfers = {k: v for k, v in self.transfers.items() if now - v[4] < INCR_TIMEOUT}
            self.refresh()
            readable, _, _ = select.select(sources, [], [], POLL)
            if wake_fd is not None and wake_fd in readable:
                try:
                    os.read(wake_fd, 4096)
                except BlockingIOError:
                    pass


def _parent_death_signal() -> None:
    """Make Xvfb exit if the bridge dies, so a restart can take :0 again."""
    try:
        libc = ctypes.CDLL(None, use_errno=True)
        libc.prctl(1, signal.SIGTERM)  # PR_SET_PDEATHSIG
    except (OSError, AttributeError):
        pass


def _display_in_use(number: str) -> bool:
    with socket.socket(socket.AF_UNIX) as probe:
        probe.settimeout(0.5)
        try:
            probe.connect(f"/tmp/.X11-unix/X{number}")
        except OSError:
            return False
    return True


def serve(name: str, root: Path) -> int:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    with (root / "lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("kbx bridge: another bridge is running", file=sys.stderr)
            return 1
        number = name.lstrip(":")
        server: subprocess.Popen[bytes] | None = None
        if _display_in_use(number):
            logging.warning("display %s already has an X server; using it", name)
        else:
            server = subprocess.Popen(
                ["Xvfb", name, "-screen", "0", "1x1x24", "-nolisten", "tcp", "-noreset"],
                stdin=subprocess.DEVNULL,
                preexec_fn=_parent_death_signal,
            )
        read_fd, write_fd = os.pipe()
        os.set_blocking(write_fd, False)
        os.set_blocking(read_fd, False)
        signal.set_wakeup_fd(write_fd)
        signal.signal(signal.SIGUSR1, lambda *_: None)
        signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))
        try:
            connection = None
            for _ in range(200):
                if server is not None and server.poll() is not None:
                    raise RuntimeError("Xvfb exited during startup")
                try:
                    connection = display.Display(name)
                    break
                except error.DisplayConnectionError:
                    time.sleep(0.05)
            if connection is None:
                raise RuntimeError("Xvfb did not become ready")
            (root / "bridge.pid").write_text(f"{os.getpid()}\n")
            print(f"kbx bridge: serving CLIPBOARD on {name} from {root}", flush=True)
            Clipboard(connection, FileSource(root)).run(read_fd)
        finally:
            (root / "bridge.pid").unlink(missing_ok=True)
            if server is not None:
                server.terminate()
                server.wait(timeout=5)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="kbx-clip-bridge", description=__doc__)
    parser.add_argument("--display", default=":0")
    parser.add_argument("--dir", type=Path, default=Path.home() / ".cache" / "kbx-clipboard")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s %(levelname)s %(message)s")
    return serve(args.display, args.dir)


if __name__ == "__main__":
    sys.exit(main())
