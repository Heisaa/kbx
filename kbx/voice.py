"""Host side of voice input: the host microphone and speakers for one sandbox.

The sandbox may not connect to the host, so while any session is attached the
host keeps one `docker exec` stream to the voice module's link.py (see
attached.py). The host records only while a program in the sandbox records
from its microphone, and plays only while one plays to its speaker. What the
sandbox sends is data: frames are checked, and audio goes only to the host's
player as raw PCM.
"""

from __future__ import annotations

import os
import select
import shutil
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from pathlib import Path

from . import attached, watch
from .docker import Docker
from .errors import KbxError
from .paths import Paths

DAEMON = "voiced"
LINK = ["python3", "/opt/kbx/stage/modules/voice/link.py"]
RATE = 48000  # s16le mono, as in the module's voice.pa
MAX_FRAME = 1024 * 1024
QUIET = 600.0  # seconds without recording after which the next recording notifies again


def tools(which: Callable[[str], str | None] = shutil.which) -> tuple[list[str], list[str]] | None:
    """Recorder and player that speak raw PCM on stdout/stdin, or None."""
    if which("pacat"):
        spec = ["--raw", "--format=s16le", f"--rate={RATE}", "--channels=1", "--client-name=kbx"]
        return ["pacat", "--record", "--latency-msec=20", *spec], ["pacat", "--playback", "--latency-msec=60", *spec]
    if which("arecord") and which("aplay"):
        spec = ["-q", "-t", "raw", "-f", "S16_LE", "-r", str(RATE), "-c", "1"]
        return ["arecord", *spec], ["aplay", *spec]
    return None


MISSING_TOOLS = "install pacat (pulseaudio-utils; libpulse on Arch) or alsa-utils"


def register(paths: Paths, name: str, pid: int, entry: Path) -> None:
    attached.register(paths, DAEMON, name, pid, entry)


def log(message: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}", file=sys.stderr, flush=True)


def _stop(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is None:
        process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()


class Bridge:
    """One link stream: starts and stops the host recorder and player as the sandbox asks."""

    def __init__(
        self,
        name: str,
        link: subprocess.Popen[bytes],
        recorder: list[str],
        player: list[str],
        notify: Callable[[str, str], object] = watch.send,
    ) -> None:
        assert link.stdin is not None
        self.name = name
        self.link_in = link.stdin
        self.recorder_argv = recorder
        self.player_argv = player
        self.notify = notify
        self.recorder: subprocess.Popen[bytes] | None = None
        self.player: subprocess.Popen[bytes] | None = None
        self.lock = threading.Lock()
        self.closed = False
        self.last_recording = float("-inf")

    def handle(self, kind: bytes, payload: bytes) -> None:
        if kind in (b"M", b"S") and payload in (b"0", b"1"):
            (self.microphone if kind == b"M" else self.speakers)(payload == b"1")
        elif kind == b"A" and len(payload) % 2 == 0:
            self.play(payload)
        else:
            raise ValueError("unexpected frame from the sandbox voice link")

    def microphone(self, on: bool) -> None:
        if on and self.recorder is None:
            log("microphone on: a program in the sandbox is recording")
            if time.monotonic() - self.last_recording > QUIET:
                self.notify(
                    f"kbx: {self.name} is using your microphone",
                    "A program in the sandbox started recording. kbx stops when it stops or when you detach.",
                )
            try:
                self.recorder = subprocess.Popen(self.recorder_argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE)
            except OSError as exc:
                log(f"cannot start {self.recorder_argv[0]}: {exc}")
                return
            threading.Thread(target=self.pump, args=(self.recorder,), daemon=True).start()
        elif not on and self.recorder is not None:
            _stop(self.recorder)
            self.recorder = None
            self.last_recording = time.monotonic()
            log("microphone off")

    def pump(self, recorder: subprocess.Popen[bytes]) -> None:
        """Host microphone → sandbox, until the recorder stops."""
        assert recorder.stdout is not None
        fd = recorder.stdout.fileno()
        try:
            while chunk := os.read(fd, 65536):
                with self.lock:
                    if self.closed:
                        return
                    self.link_in.write(chunk)
                    self.link_in.flush()
        except OSError:
            pass
        finally:
            recorder.stdout.close()

    def speakers(self, on: bool) -> None:
        if on and self.player is None:
            try:
                self.player = subprocess.Popen(self.player_argv, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL)
            except OSError as exc:
                log(f"cannot start {self.player_argv[0]}: {exc}")
        elif not on and self.player is not None:
            self._close_player()

    def play(self, audio: bytes) -> None:
        if self.player is None or self.player.stdin is None:
            return
        try:
            self.player.stdin.write(audio)
            self.player.stdin.flush()
        except OSError:
            log(f"{self.player_argv[0]} stopped; sandbox sound is off until it plays again")
            self._close_player()

    def _close_player(self) -> None:
        player, self.player = self.player, None
        if player is None:
            return
        try:
            if player.stdin is not None:
                player.stdin.close()  # let it play out what it has
            player.wait(timeout=2)
        except (OSError, subprocess.TimeoutExpired):
            pass
        _stop(player)

    def close(self) -> None:
        self.microphone(False)
        self._close_player()
        with self.lock:
            self.closed = True
            try:
                self.link_in.close()  # EOF stops the link
            except OSError:
                pass


def frames(buffer: bytearray) -> list[tuple[bytes, bytes]]:
    """Take the complete frames off the front of buffer."""
    found: list[tuple[bytes, bytes]] = []
    while len(buffer) >= 5:
        size = int.from_bytes(buffer[1:5], "big")
        if size > MAX_FRAME:
            raise ValueError("oversized frame from the sandbox voice link")
        if len(buffer) < 5 + size:
            break
        found.append((bytes(buffer[:1]), bytes(buffer[5 : 5 + size])))
        del buffer[: 5 + size]
    return found


def serve(docker: Docker, name: str, found: tuple[list[str], list[str]], stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            process = docker.exec_stream(name, LINK)
        except (OSError, KbxError) as exc:
            log(f"cannot start the sandbox voice link: {exc}")
            if stop.wait(5):
                return
            continue
        assert process.stdout is not None
        bridge = Bridge(name, process, *found)
        try:
            buffer = bytearray()
            while not stop.is_set():
                if not select.select([process.stdout], [], [], 0.2)[0]:
                    continue
                chunk = os.read(process.stdout.fileno(), 65536)
                if not chunk:
                    break
                buffer.extend(chunk)
                for kind, payload in frames(buffer):
                    bridge.handle(kind, payload)
        except (OSError, ValueError) as exc:
            log(f"sandbox voice link failed: {exc}")
        finally:
            bridge.close()
            try:
                process.wait(timeout=7)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()
            process.stdout.close()
        if stop.wait(5):
            return


def run(paths: Paths, name: str) -> int:
    found = tools()
    if found is None:
        return 0
    stop = threading.Event()
    thread = threading.Thread(target=serve, args=(Docker(), name, found, stop), daemon=True)
    thread.start()
    grace = time.monotonic() + 10
    try:
        while not stop.wait(1):
            if time.monotonic() > grace and attached.live(paths, DAEMON, name) == 0:
                return 0
    finally:
        stop.set()
        thread.join(timeout=15)
        attached.release(paths, DAEMON, name)
    return 0
