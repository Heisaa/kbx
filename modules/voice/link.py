"""Connect the sandbox's PulseAudio microphone and speaker to the host.

The host runs this over `docker exec -i` while a session is attached; the
sandbox never connects to the host. It tells the host when a program starts
or stops recording from the microphone or playing to the speaker, so the host
opens its own microphone only while something here records.

stdin: host microphone audio (raw s16le, 48 kHz, mono) for the mic FIFO.
stdout: frames of one type byte, a 4-byte big-endian length and the payload:
  b"M" b"1" or b"0"  a program records from the microphone, or none does
  b"S" b"1" or b"0"  a program plays to the speaker, or none does
  b"A" <pcm>         speaker audio (raw s16le, 48 kHz, mono), only while S is on
EOF on stdin stops it.
"""

from __future__ import annotations

import os
import select
import struct
import subprocess
import sys
import time
from typing import IO

RUN = "/run/kbx-voice"
SOURCE = "kbx_mic"
SINK = "kbx_speaker"
FRAME = 2  # bytes per sample: s16le mono
CHUNK = 4096  # at most PIPE_BUF, so a FIFO write is all or nothing
RECHECK = 2.0  # seconds between stream counts without events


def pactl(*args: str) -> list[list[str]]:
    result = subprocess.run(["pactl", *args], capture_output=True, text=True, timeout=10, check=True)
    return [line.split("\t") for line in result.stdout.splitlines() if line.strip()]


def index(kind: str, name: str) -> str:
    for row in pactl("list", "short", kind):
        if len(row) > 1 and row[1] == name:
            return row[0]
    raise RuntimeError(f"PulseAudio has no {name}")


def streams(kind: str, target: str) -> int:
    """Recording streams on a source (source-outputs) or playing ones on a sink (sink-inputs)."""
    return sum(1 for row in pactl("list", "short", kind) if len(row) > 1 and row[1] == target)


def whole_frames(pending: bytes, data: bytes) -> tuple[bytes, bytes]:
    """Split into whole samples and the remainder for next time."""
    data = pending + data
    cut = len(data) - len(data) % FRAME
    return data[:cut], data[cut:]


class Link:
    def __init__(self, out: IO[bytes]) -> None:
        self.out = out
        self.source = index("sources", SOURCE)
        self.sink = index("sinks", SINK)
        # PulseAudio holds the other end of both FIFOs open, so neither open blocks.
        self.mic_fd = os.open(f"{RUN}/mic", os.O_WRONLY | os.O_NONBLOCK)
        self.speaker_fd = os.open(f"{RUN}/speaker", os.O_RDONLY | os.O_NONBLOCK)
        self.mic = False
        self.speaker = False
        self.from_host = b""
        self.to_host = b""
        self.checked = 0.0

    def send(self, kind: bytes, payload: bytes) -> None:
        self.out.write(kind + struct.pack(">I", len(payload)) + payload)
        self.out.flush()

    def refresh(self) -> None:
        self.checked = time.monotonic()
        mic = streams("source-outputs", self.source) > 0
        speaker = streams("sink-inputs", self.sink) > 0
        if mic != self.mic:
            self.mic = mic
            self.send(b"M", b"1" if mic else b"0")
        if speaker != self.speaker:
            self.speaker = speaker
            self.to_host = b""
            self.send(b"S", b"1" if speaker else b"0")

    def microphone(self, data: bytes) -> None:
        audio, self.from_host = whole_frames(self.from_host, data)
        if not self.mic:
            return
        for start in range(0, len(audio), CHUNK):
            try:
                os.write(self.mic_fd, audio[start : start + CHUNK])
            except BlockingIOError:
                pass  # PulseAudio is behind: drop audio rather than add latency

    def speakers(self) -> None:
        try:
            data = os.read(self.speaker_fd, 65536)
        except BlockingIOError:
            return
        # Read even when idle: PulseAudio renders silence into the FIFO all the time.
        if not self.speaker:
            return
        audio, self.to_host = whole_frames(self.to_host, data)
        if audio:
            self.send(b"A", audio)

    def run(self, host: int, events: int) -> None:
        self.refresh()
        lines = b""
        while True:
            readable, _, _ = select.select([host, events, self.speaker_fd], [], [], RECHECK)
            # Also covers events from before `pactl subscribe` was listening.
            if time.monotonic() - self.checked > RECHECK:
                self.refresh()
            if events in readable:
                chunk = os.read(events, 65536)
                if not chunk:
                    raise RuntimeError("pactl subscribe stopped")
                lines += chunk
                *complete, lines = lines.split(b"\n")
                if any(b"source-output" in line or b"sink-input" in line for line in complete):
                    self.refresh()
            if self.speaker_fd in readable:
                self.speakers()
            if host in readable:
                data = os.read(host, 65536)
                if not data:
                    return
                self.microphone(data)


def main() -> int:
    events = subprocess.Popen(
        ["pactl", "subscribe"], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL
    )
    assert events.stdout is not None
    try:
        Link(sys.stdout.buffer).run(sys.stdin.buffer.fileno(), events.stdout.fileno())
    except (OSError, RuntimeError, subprocess.SubprocessError) as exc:
        print(f"kbx voice link: {exc}", file=sys.stderr)
        return 1
    finally:
        events.terminate()
        events.wait(timeout=5)
    return 0


if __name__ == "__main__":
    sys.exit(main())
