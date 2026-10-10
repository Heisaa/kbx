"""Host copy forwarding; desktop writes are mocked to leave the real clipboard alone."""

from __future__ import annotations

import base64
import subprocess
import sys
import threading
import unittest
from unittest import mock

from kbx import clipd
from kbx.docker import Docker


class CopyTest(unittest.TestCase):
    def test_writes_wayland_and_x11_without_changing_text(self) -> None:
        data = "text\n世界\n".encode()
        for kind, binary in (("wayland", "wl-copy"), ("x11", "xclip")):
            with self.subTest(kind=kind), mock.patch.object(clipd.subprocess, "run") as run:
                run.return_value.returncode = 0
                self.assertTrue(clipd.Watcher(kind).copy(data))
                self.assertEqual(run.call_args.args[0][0], binary)
                self.assertEqual(run.call_args.kwargs["input"], data)
                self.assertEqual(run.call_args.kwargs["stdout"], subprocess.DEVNULL)

    def test_record_validation(self) -> None:
        watcher = clipd.Watcher("x11")
        with mock.patch.object(watcher, "copy", return_value=True) as copy, mock.patch("sys.stderr"):
            for record in (b"", b"invalid base64", base64.b64encode(b"\xff"), b"x" * (clipd.MAX_RECORD + 1)):
                clipd.copy_record(watcher, record)
            copy.assert_not_called()
            data = "quotes ' \"\n世界\n".encode()
            clipd.copy_record(watcher, base64.b64encode(data))
            copy.assert_called_once_with(data)

    def test_stream_fragments_and_stops_on_detach(self) -> None:
        data = b"first line\nsecond line\n"
        record = base64.b64encode(data)
        tail = record[5:] + b"\n"
        script = (
            "import os, sys, time\n"
            f"os.write(1, {record[:5]!r})\n"
            "time.sleep(0.1)\n"
            f"os.write(1, {tail!r})\n"
            "sys.stdin.buffer.read()\n"
        )
        process = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        stop = threading.Event()
        copied = threading.Event()
        watcher = clipd.Watcher("x11")
        docker = Docker()
        with (
            mock.patch.object(docker, "exec_stream", return_value=process) as stream,
            mock.patch.object(watcher, "copy", side_effect=lambda _: copied.set() or True) as copy,
        ):
            thread = threading.Thread(target=clipd.watch_copies, args=(docker, "sandbox", watcher, stop))
            thread.start()
            try:
                self.assertTrue(copied.wait(5))
                copy.assert_called_once_with(data)
                stream.assert_called_once_with("sandbox", ["kbx-clip-export"], env={"DISPLAY": ":0"})
            finally:
                stop.set()
                thread.join(10)
            self.assertFalse(thread.is_alive())
            self.assertEqual(process.returncode, 0)
