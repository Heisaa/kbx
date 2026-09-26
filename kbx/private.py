"""Mount mode: build directories the sandbox keeps to itself.

The checkout is shared, but some directories inside it hold what one side
built for itself: Cargo's `target/`, a Python `.venv` (which points at one
side's interpreter), `node_modules` with native add-ons. When both sides use
the same one, each build invalidates the other's.

For each `[workspace] private` entry, kbx bind-mounts a directory from the
sandbox's home volume over it, inside the VM only. The host keeps its own
directory and never sees the sandbox's; in the sandbox the path is unchanged,
so `./target/debug/app` and `.venv/bin/python` work as usual. It happens at
each launch (idempotent; mounts end with the container).

An entry applies when the directory exists in the checkout, or when a file
that marks its tool sits beside it (Cargo.toml for `target`), so other
projects get no empty directories. A symlink is left alone.

Side effect: a mount point cannot be removed, so `cargo clean` or `rm -rf
target` in the sandbox empty it but then report "Device or resource busy".
"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path

from .docker import ROOT, Docker
from .sandbox import HOME, Sandbox

STORE = f"{HOME}/.cache/kbx-private"
PYTHON = ("pyproject.toml", "requirements.txt", "setup.py", "setup.cfg", "uv.lock", "Pipfile", ".python-version")
MARKERS: dict[str, tuple[str, ...]] = {
    "target": ("Cargo.toml",),
    ".venv": PYTHON,
    "venv": PYTHON,
    "node_modules": ("package.json",),
}

# As root in the sandbox: $1 the project, then the entries to make private.
SCRIPT = r"""
project=$1
shift
status=0
for rel in "$@"; do
  target="$project/$rel" store="__STORE__/$rel"
  if [ -L "$target" ]; then echo "$rel is a symlink" >&2; status=1; continue; fi
  if mountpoint -q "$target" 2>/dev/null; then continue; fi
  runuser -u agent -- mkdir -p "$store" "$target" && mount --bind "$store" "$target" || status=1
done
exit $status
""".replace("__STORE__", STORE)


def applies(project: Path, rel: str) -> bool:
    path = project / rel
    if path.is_symlink():
        return False
    if path.is_dir():
        return True
    return any((path.parent / marker).is_file() for marker in MARKERS.get(path.name, ()))


def wanted(project: Path, entries: Sequence[str]) -> list[str]:
    return [rel for rel in entries if applies(project, rel)]


def mount(docker: Docker, sandbox: Sandbox, entries: Sequence[str]) -> list[str]:
    """Make the entries that apply private; returns warnings."""
    rels = wanted(sandbox.project_dir, entries)
    notes = [
        f"{rel} is a symlink in the checkout; it stays shared"
        for rel in entries
        if (sandbox.project_dir / rel).is_symlink()
    ]
    if not rels:
        return notes
    result = docker.exec(
        sandbox.name, ["sh", "-c", SCRIPT, "sh", str(sandbox.project_dir), *rels], user=ROOT, check=False, timeout=60
    )
    if result.returncode != 0:
        detail = (result.stderr.decode("utf-8", "replace").strip().splitlines() or ["no output"])[-1]
        notes.append(f"could not give the sandbox its own {', '.join(rels)} ({detail}); they stay shared")
    return notes
