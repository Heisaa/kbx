"""kbx-clip-put: receive a clipboard image (or a clear) pushed by the host.

  kbx-clip-put image/png < image.png
  kbx-clip-put --clear

Writes the image, its MIME type and a sequence number atomically to
~/.cache/kbx-clipboard/, then wakes the bridge (SIGUSR1). The bridge polls
the directory as well, so a missed signal only delays the update.
"""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path

MAX_IMAGE = 64 * 1024 * 1024
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
MIME_TYPES = ("image/png",)


def state_dir() -> Path:
    return Path.home() / ".cache" / "kbx-clipboard"


def _atomic(path: Path, data: bytes) -> None:
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def read_meta(root: Path) -> dict[str, object]:
    try:
        data = json.loads((root / "meta.json").read_text())
    except (OSError, ValueError):
        return {"seq": 0, "mime": None}
    return data if isinstance(data, dict) else {"seq": 0, "mime": None}  # pyright: ignore[reportUnknownVariableType]


def put(root: Path, mime: str | None, data: bytes) -> int:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    seq_value = read_meta(root).get("seq", 0)
    seq = (seq_value if isinstance(seq_value, int) else 0) + 1
    if mime is not None:
        _atomic(root / "data", data)
    else:
        (root / "data").unlink(missing_ok=True)
    # meta.json is written last: the bridge treats it as the commit point.
    _atomic(root / "meta.json", json.dumps({"seq": seq, "mime": mime, "size": len(data)}).encode())
    return seq


def notify(root: Path) -> None:
    try:
        pid = int((root / "bridge.pid").read_text().strip())
        os.kill(pid, signal.SIGUSR1)
    except (OSError, ValueError):
        pass


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = state_dir()
    if args == ["--clear"]:
        put(root, None, b"")
        notify(root)
        return 0
    if len(args) != 1 or args[0] not in MIME_TYPES:
        print(f"usage: kbx-clip-put {{{'|'.join(MIME_TYPES)}}} < data | kbx-clip-put --clear", file=sys.stderr)
        return 2
    data = sys.stdin.buffer.read(MAX_IMAGE + 1)
    if len(data) > MAX_IMAGE:
        print("kbx-clip-put: image exceeds the 64 MiB limit", file=sys.stderr)
        return 1
    if not data.startswith(PNG_MAGIC):
        print("kbx-clip-put: data is not a PNG image", file=sys.stderr)
        return 1
    put(root, args[0], data)
    notify(root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
