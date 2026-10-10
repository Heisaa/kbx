"""Stream new sandbox X11 text copies to the host over docker exec stdout.

XFIXES reports each copy, including when arboard reuses its owner window.
Existing clipboard content is never replayed on attach. Images are refused by
the UTF8_STRING request, so host image pushes cannot loop back as text.
Closing stdin stops the listener when the last host session detaches.
"""

from __future__ import annotations

import base64
import os
import select
import subprocess
import sys
import time
from typing import Any

from Xlib import X, display
from Xlib.ext import xfixes

MAX_TEXT = 1024 * 1024
READ_TIMEOUT = 5.0


def read_text(name: str) -> bytes | None:
    """Use xclip's selection/INCR handling, with bounded output and a timeout."""
    with subprocess.Popen(
        ["xclip", "-display", name, "-selection", "clipboard", "-target", "UTF8_STRING", "-out"],
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
    ) as process:
        assert process.stdout is not None
        data = bytearray()
        deadline = time.monotonic() + READ_TIMEOUT
        try:
            while time.monotonic() < deadline:
                if not select.select([process.stdout], [], [], 0.1)[0]:
                    continue
                chunk = os.read(process.stdout.fileno(), min(65536, MAX_TEXT + 1 - len(data)))
                if not chunk:
                    if process.wait(timeout=1) == 0:
                        try:
                            data.decode("utf-8")
                        except UnicodeDecodeError:
                            return None
                        return bytes(data)
                    return None
                data.extend(chunk)
                if len(data) > MAX_TEXT:
                    return None
            return None
        finally:
            if process.poll() is None:
                process.kill()


class Listener:
    def __init__(self, connection: Any) -> None:
        self.d = connection
        self.selection = connection.intern_atom("CLIPBOARD")
        self.notify_type = connection.query_extension("XFIXES").first_event + xfixes.XFixesSelectionNotify
        self.window = connection.screen().root.create_window(0, 0, 1, 1, 0, X.CopyFromParent)
        connection.xfixes_query_version()
        connection.xfixes_select_selection_input(self.window, self.selection, xfixes.XFixesSetSelectionOwnerNotifyMask)
        connection.sync()

    def run(self, name: str, input_fd: int) -> None:
        # A blank ready record lets callers know the subscription is active.
        print(flush=True)
        while True:
            while self.d.pending_events():
                event = self.d.next_event()
                if (
                    event.type != self.notify_type
                    or event.sub_code != xfixes.XFixesSetSelectionOwnerNotify
                    or not event.owner
                ):
                    continue
                data = read_text(name)
                owner = self.d.get_selection_owner(self.selection)
                if data is not None and owner and owner.id == event.owner.id:
                    print(base64.b64encode(data).decode("ascii"), flush=True)
            readable, _, _ = select.select([self.d, input_fd], [], [])
            if input_fd in readable and not os.read(input_fd, 4096):
                return


def main() -> int:
    name = os.environ.get("DISPLAY", ":0")
    connection = display.Display(name)
    try:
        Listener(connection).run(name, sys.stdin.fileno())
    finally:
        connection.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
