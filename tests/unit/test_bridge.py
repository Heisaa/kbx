"""Clipboard bridge on an isolated Xvfb display; never touches a real clipboard."""

from __future__ import annotations

import hashlib
import importlib.util
import json
import multiprocessing
import os
import select
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any

from kbx_sandbox import clip_put

# Xlib is imported inside the functions that need it, so this file loads
# (and skips) on machines without python3-xlib.
HAVE_XLIB = importlib.util.find_spec("Xlib") is not None

SMALL = b"\x89PNG\r\n\x1a\n" + b"small image"
LARGE = b"\x89PNG\r\n\x1a\n" + bytes(range(256)) * 8192  # > 64 KiB: INCR transfer


def bridge_process(name: str, root: str, ready: Any) -> None:
    from Xlib import display

    from kbx_sandbox import bridge

    clipboard = bridge.Clipboard(display.Display(name), bridge.FileSource(Path(root)))
    ready.set()
    clipboard.run()


def read_target(name: str, target: str) -> dict[str, Any]:
    """Convert CLIPBOARD to `target` like a native client (arboard, xclip) does."""
    from Xlib import X, display

    connection = display.Display(name)
    window = connection.screen().root.create_window(0, 0, 1, 1, 0, X.CopyFromParent, event_mask=X.PropertyChangeMask)
    atoms = {key: connection.intern_atom(key) for key in ("CLIPBOARD", target, "INCR", "KBX_TEST")}
    window.convert_selection(atoms["CLIPBOARD"], atoms[target], atoms["KBX_TEST"], X.CurrentTime)
    connection.flush()
    data = bytearray()
    incremental = False
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        if not connection.pending_events():
            select.select([connection], [], [], 0.05)
            continue
        event = connection.next_event()
        if event.type == X.SelectionNotify:
            if not event.property:
                return {"refused": True}
            value = window.get_full_property(atoms["KBX_TEST"], X.AnyPropertyType)
            if value.property_type == atoms["INCR"]:
                incremental = True
                window.delete_property(atoms["KBX_TEST"])
                connection.flush()
                continue
            if target == "TARGETS":
                return {"targets": sorted(connection.get_atom_name(a) for a in value.value)}
            data.extend(value.value)
            break
        if incremental and event.type == X.PropertyNotify and event.state == X.PropertyNewValue:
            value = window.get_full_property(atoms["KBX_TEST"], X.AnyPropertyType)
            if value is None or value.property_type == atoms["INCR"]:
                continue
            window.delete_property(atoms["KBX_TEST"])
            connection.flush()
            if not len(value.value):
                break
            data.extend(value.value)
    else:
        raise RuntimeError("clipboard transfer timed out")
    return {"sha256": hashlib.sha256(bytes(data)).hexdigest(), "size": len(data), "incremental": incremental}


def real_xclip() -> str | None:
    """The xclip binary, not a wrapper script some environments put first on PATH."""
    for candidate in ("/usr/bin/xclip", shutil.which("xclip")):
        if candidate and os.path.isfile(candidate):
            with open(candidate, "rb") as handle:
                if handle.read(4) == b"\x7fELF":
                    return candidate
    return None


def owner(name: str) -> bool:
    from Xlib import display

    connection = display.Display(name)
    return bool(connection.get_selection_owner(connection.intern_atom("CLIPBOARD")))


@unittest.skipUnless(HAVE_XLIB and shutil.which("Xvfb"), "needs python3-xlib and Xvfb")
class BridgeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        read_fd, write_fd = os.pipe()
        cls.xvfb = subprocess.Popen(
            ["Xvfb", "-displayfd", str(write_fd), "-screen", "0", "1x1x24", "-nolisten", "tcp", "-noreset"],
            pass_fds=(write_fd,),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        os.close(write_fd)
        with os.fdopen(read_fd) as pipe:
            cls.name = ":" + pipe.readline().strip()
        cls.root = Path(tempfile.mkdtemp(prefix="kbx-clip-"))
        context = multiprocessing.get_context("spawn")
        ready = context.Event()
        cls.process = context.Process(target=bridge_process, args=(cls.name, str(cls.root), ready))
        cls.process.start()
        assert ready.wait(10), "bridge did not start"

    @classmethod
    def tearDownClass(cls) -> None:
        cls.process.terminate()
        cls.process.join(5)
        cls.xvfb.terminate()
        cls.xvfb.wait(timeout=5)
        shutil.rmtree(cls.root, ignore_errors=True)

    def push(self, data: bytes | None) -> None:
        clip_put.put(self.root, "image/png" if data is not None else None, data or b"")
        time.sleep(0.6)  # the bridge polls every 0.2 s

    def call(self, target: str) -> dict[str, Any]:
        result = subprocess.run(
            [sys.executable, __file__, "--client", self.name, target],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
            env=dict(os.environ, PYTHONPATH=str(Path(__file__).resolve().parents[2])),
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_small_image_round_trips(self) -> None:
        self.push(SMALL)
        value = self.call("image/png")
        self.assertEqual(value["sha256"], hashlib.sha256(SMALL).hexdigest())
        self.assertFalse(value["incremental"])

    def test_large_image_uses_incr(self) -> None:
        self.push(LARGE)
        value = self.call("image/png")
        self.assertEqual(value["sha256"], hashlib.sha256(LARGE).hexdigest())
        self.assertTrue(value["incremental"])

    def test_exact_targets_and_refusals(self) -> None:
        self.push(SMALL)
        self.assertEqual(self.call("TARGETS")["targets"], ["TARGETS", "TIMESTAMP", "image/png"])
        for target in ("text/plain", "UTF8_STRING", "STRING", "image/jpeg"):
            with self.subTest(target=target):
                self.assertEqual(self.call(target), {"refused": True})

    def test_clear_releases_ownership(self) -> None:
        self.push(SMALL)
        self.assertTrue(owner(self.name))
        self.push(None)
        self.assertFalse(owner(self.name))
        self.assertEqual(self.call("image/png"), {"refused": True})

    def test_new_image_replaces_old(self) -> None:
        self.push(SMALL)
        self.push(LARGE)
        self.assertEqual(self.call("image/png")["size"], len(LARGE))

    @unittest.skipUnless(real_xclip(), "needs xclip")
    def test_xclip_like_claude_and_pi(self) -> None:
        xclip = real_xclip() or "xclip"
        self.push(SMALL)
        env = dict(os.environ, DISPLAY=self.name)
        targets = subprocess.run(
            [xclip, "-selection", "clipboard", "-t", "TARGETS", "-o"],
            capture_output=True,
            env=env,
            timeout=10,
            check=False,
        )
        self.assertIn(b"image/png", targets.stdout)
        png = subprocess.run(
            [xclip, "-selection", "clipboard", "-t", "image/png", "-o"],
            capture_output=True,
            env=env,
            timeout=10,
            check=False,
        )
        self.assertEqual(png.stdout, SMALL)
        text = subprocess.run(
            [xclip, "-selection", "clipboard", "-t", "text/plain", "-o"],
            capture_output=True,
            env=env,
            timeout=10,
            check=False,
        )
        self.assertNotEqual(text.returncode, 0)
        self.assertEqual(text.stdout, b"")


class ClipPutTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(tempfile.mkdtemp(prefix="kbx-put-"))
        self.addCleanup(shutil.rmtree, self.root, True)

    def test_put_and_clear(self) -> None:
        self.assertEqual(clip_put.put(self.root, "image/png", SMALL), 1)
        self.assertEqual((self.root / "data").read_bytes(), SMALL)
        self.assertEqual(clip_put.read_meta(self.root)["mime"], "image/png")
        self.assertEqual(clip_put.put(self.root, None, b""), 2)
        self.assertFalse((self.root / "data").exists())
        self.assertIsNone(clip_put.read_meta(self.root)["mime"])

    def test_cli_rejects_non_png(self) -> None:
        env = dict(os.environ, HOME=str(self.root))
        script = "import sys; from kbx_sandbox import clip_put; sys.exit(clip_put.main())"
        root = str(Path(__file__).resolve().parents[2])
        bad = subprocess.run(
            [sys.executable, "-c", script, "image/png"],
            input=b"GIF89a",
            env=env,
            cwd=root,
            capture_output=True,
            check=False,
        )
        self.assertEqual(bad.returncode, 1)
        wrong = subprocess.run(
            [sys.executable, "-c", script, "text/plain"],
            input=b"x",
            env=env,
            cwd=root,
            capture_output=True,
            check=False,
        )
        self.assertEqual(wrong.returncode, 2)
        good = subprocess.run(
            [sys.executable, "-c", script, "image/png"],
            input=SMALL,
            env=env,
            cwd=root,
            capture_output=True,
            check=False,
        )
        self.assertEqual(good.returncode, 0, good.stderr)
        self.assertEqual((self.root / ".cache/kbx-clipboard/data").read_bytes(), SMALL)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--client":
        print(json.dumps(read_target(sys.argv[2], sys.argv[3])))
    else:
        unittest.main()
