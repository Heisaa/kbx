"""Host voice link: frame parsing, tools, and the recorder/player bridge with stand-in processes."""

from __future__ import annotations

import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest import mock

from kbx import voice
from kbx.docker import Docker

LINK = Path(__file__).resolve().parents[2] / "modules" / "voice" / "link.py"


def frame(kind: bytes, payload: bytes) -> bytes:
    return kind + len(payload).to_bytes(4, "big") + payload


def wait_for(check: object, timeout: float = 5) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if callable(check) and check():
            return True
        time.sleep(0.02)
    return False


class FramesTest(unittest.TestCase):
    def test_complete_frames_and_remainder(self) -> None:
        data = frame(b"M", b"1") + frame(b"A", b"\x01\x02\x03\x04") + frame(b"S", b"0")
        buffer = bytearray(data[:-2])
        self.assertEqual(voice.frames(buffer), [(b"M", b"1"), (b"A", b"\x01\x02\x03\x04")])
        buffer.extend(data[-2:])
        self.assertEqual(voice.frames(buffer), [(b"S", b"0")])
        self.assertEqual(buffer, bytearray())

    def test_oversized_frame(self) -> None:
        buffer = bytearray(b"A" + (voice.MAX_FRAME + 1).to_bytes(4, "big"))
        with self.assertRaises(ValueError):
            voice.frames(buffer)


class ToolsTest(unittest.TestCase):
    def test_prefers_pacat_then_alsa(self) -> None:
        found = voice.tools(lambda name: f"/usr/bin/{name}")
        assert found is not None
        self.assertEqual([found[0][:2], found[1][:2]], [["pacat", "--record"], ["pacat", "--playback"]])
        found = voice.tools(lambda name: None if name == "pacat" else f"/usr/bin/{name}")
        assert found is not None
        self.assertEqual([found[0][0], found[1][0]], ["arecord", "aplay"])
        self.assertIsNone(voice.tools(lambda name: "/usr/bin/arecord" if name == "arecord" else None))


class BridgeTest(unittest.TestCase):
    def setUp(self) -> None:
        self.dir = Path(tempfile.mkdtemp(prefix="kbx-voice-"))
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.dir)], check=False))
        self.sandbox_in = self.dir / "sandbox-in"
        self.played = self.dir / "played"
        # The link's stdin collects what the host recorder sends.
        self.link = subprocess.Popen(
            [
                sys.executable,
                "-c",
                f"import shutil, sys; shutil.copyfileobj(sys.stdin.buffer.raw, open({str(self.sandbox_in)!r}, 'wb', buffering=0), 1)",
            ],
            stdin=subprocess.PIPE,
        )
        recorder = [
            sys.executable,
            "-c",
            "import sys, time\nwhile True:\n    sys.stdout.buffer.write(b'mic!'); sys.stdout.flush(); time.sleep(0.01)",
        ]
        player = [
            sys.executable,
            "-c",
            f"import shutil, sys; shutil.copyfileobj(sys.stdin.buffer, open({str(self.played)!r}, 'wb'))",
        ]
        self.notes: list[str] = []
        self.bridge = voice.Bridge("kbx-p-1", self.link, recorder, player, lambda title, _: self.notes.append(title))

    def tearDown(self) -> None:
        self.bridge.close()
        self.link.wait(timeout=5)

    def test_records_only_while_the_sandbox_records(self) -> None:
        with mock.patch("sys.stderr"):
            self.bridge.handle(b"M", b"1")
            self.assertTrue(wait_for(lambda: self.sandbox_in.exists() and self.sandbox_in.stat().st_size >= 8))
            self.bridge.handle(b"M", b"0")
            self.assertIsNone(self.bridge.recorder)
            size = self.sandbox_in.stat().st_size
            time.sleep(0.1)
            self.assertEqual(self.sandbox_in.stat().st_size, size)
            # One notification per burst of recordings.
            self.bridge.handle(b"M", b"1")
            self.bridge.handle(b"M", b"0")
        self.assertEqual(self.notes, ["kbx: kbx-p-1 is using your microphone"])

    def test_plays_sandbox_audio(self) -> None:
        self.bridge.handle(b"A", b"\x00\x01")  # no player yet: dropped
        self.bridge.handle(b"S", b"1")
        self.bridge.handle(b"A", b"\x10\x11\x12\x13")
        self.bridge.handle(b"S", b"0")
        self.assertEqual(self.played.read_bytes(), b"\x10\x11\x12\x13")

    def test_rejects_unknown_frames(self) -> None:
        for kind, payload in ((b"M", b"2"), (b"X", b""), (b"A", b"\x00")):
            with self.subTest(kind=kind, payload=payload), self.assertRaises(ValueError):
                self.bridge.handle(kind, payload)


class ServeTest(unittest.TestCase):
    def test_stream_drives_bridge_and_stops(self) -> None:
        script = (
            "import sys\n"
            f"sys.stdout.buffer.write({frame(b'S', b'1') + frame(b'A', b'ab') + frame(b'S', b'0')!r})\n"
            "sys.stdout.flush()\n"
            "sys.stdin.buffer.read()\n"
        )
        process = subprocess.Popen([sys.executable, "-c", script], stdin=subprocess.PIPE, stdout=subprocess.PIPE)
        with tempfile.TemporaryDirectory() as temp:
            played = Path(temp) / "played"
            player = [
                sys.executable,
                "-c",
                f"import shutil, sys; shutil.copyfileobj(sys.stdin.buffer, open({str(played)!r}, 'wb'))",
            ]
            docker = Docker()
            stop = threading.Event()
            with mock.patch.object(docker, "exec_stream", return_value=process) as stream:
                thread = threading.Thread(target=voice.serve, args=(docker, "sandbox", (["true"], player), stop))
                thread.start()
                try:
                    self.assertTrue(wait_for(lambda: played.exists() and played.read_bytes() == b"ab"))
                finally:
                    stop.set()
                    thread.join(timeout=10)
            self.assertFalse(thread.is_alive())
            stream.assert_called_once_with("sandbox", voice.LINK)
            self.assertEqual(process.wait(timeout=5), 0)  # EOF on stdin stopped it


class LinkFramesTest(unittest.TestCase):
    def test_whole_frames_keeps_odd_bytes(self) -> None:
        namespace: dict[str, object] = {"__name__": "voice_link"}
        exec(compile(LINK.read_text(), str(LINK), "exec"), namespace)  # noqa: S102
        whole = namespace["whole_frames"]
        assert callable(whole)
        self.assertEqual(whole(b"", b"\x01\x02\x03"), (b"\x01\x02", b"\x03"))
        self.assertEqual(whole(b"\x03", b"\x04"), (b"\x03\x04", b""))


if __name__ == "__main__":
    unittest.main()
