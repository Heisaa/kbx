"""Build the read-only stage mounted at /opt/kbx/stage in every sandbox.

The stage directory is bind-mounted into existing containers, and a bind mount
pins the directory inode it was created with. So the stage is synced in place,
file by file: changed files are written to a temp name and renamed over the old
one, removed files are deleted, and no directory that should exist is ever
replaced. Symlinks are dereferenced so the sandbox never sees a link into the
host filesystem.
"""

from __future__ import annotations

import fcntl
import json
import os
import stat
import tempfile
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config
from .errors import KbxError
from .modules import Resolved
from .paths import Paths

# Module parts that exist only at build time never reach the stage.
BUILD_ONLY = {"build.sh", "build"}
STAGE_VERSION = 1


@dataclass
class Tree:
    files: dict[str, Path | bytes] = field(default_factory=dict[str, "Path | bytes"])
    modes: dict[str, int] = field(default_factory=dict[str, int])
    dirs: set[str] = field(default_factory=set[str])

    def add_dir(self, rel: str) -> None:
        parts = Path(rel).parts
        for index in range(1, len(parts) + 1):
            self.dirs.add(str(Path(*parts[:index])))

    def add_file(self, rel: str, source: Path | bytes, mode: int = 0o644) -> None:
        parent = str(Path(rel).parent)
        if parent != ".":
            self.add_dir(parent)
        self.files[rel] = source
        self.modes[rel] = mode


def _walk(source: Path, warnings: list[str]) -> Iterator[tuple[str, Path]]:
    """Regular files under `source`, following symlinks but never looping."""
    seen: set[str] = set()
    for root, dirs, files in os.walk(source, followlinks=True):
        real = os.path.realpath(root)
        if real in seen:
            dirs[:] = []
            continue
        seen.add(real)
        dirs[:] = sorted(d for d in dirs if d not in ("__pycache__", ".git"))
        for name in sorted(files):
            path = Path(root) / name
            try:
                info = path.stat()
            except OSError:
                warnings.append(f"skipping broken link {path}")
                continue
            if stat.S_ISREG(info.st_mode):
                yield str(path.relative_to(source)), path


def _mode(path: Path) -> int:
    return 0o755 if os.stat(path).st_mode & 0o111 else 0o644


def config_json(config: Config, resolved: list[Resolved]) -> dict[str, Any]:
    env: dict[str, str] = {}
    for item in resolved:
        env.update(item.module.env)
    return {
        "version": STAGE_VERSION,
        "modules": [
            {
                "name": item.name,
                "options": dict(item.options),
                "option_env": item.option_env(),
                "env": dict(item.module.env),
                "home": item.module.has_home,
                "enforce": list(item.module.enforce),
                "copy": list(item.module.copy),
                "service": {"user": item.module.service_user} if item.module.has_service else None,
                "start": {
                    "user": item.module.start_user,
                    "before_launch": list(item.module.before_launch),
                }
                if item.module.has_start
                else None,
            }
            for item in resolved
        ],
        "env": env,
        "skills": config.launcher.shared_skills,
        "dns": list(config.launcher.dns),
        "docker": {"storage": config.runtime.docker_storage, "disk": config.launcher.docker_disk},
    }


def desired(config: Config, resolved: list[Resolved], warnings: list[str]) -> Tree:
    tree = Tree()
    tree.add_dir("modules")
    tree.add_dir("skills")
    for item in resolved:
        base = f"modules/{item.name}"
        tree.add_dir(base)
        for entry in sorted(item.module.path.iterdir()):
            if entry.name in BUILD_ONLY or entry.name.startswith("."):
                continue
            if entry.is_dir():
                tree.add_dir(f"{base}/{entry.name}")
                for rel, path in _walk(entry, warnings):
                    tree.add_file(f"{base}/{entry.name}/{rel}", path, _mode(path))
            elif entry.is_file():
                tree.add_file(f"{base}/{entry.name}", entry, _mode(entry))
    if config.launcher.shared_skills:
        owners: dict[str, Path] = {}
        for source in config.skills.sources:
            if not source.is_dir():
                continue
            for entry in sorted(source.iterdir()):
                if entry.name.startswith("."):
                    continue
                if entry.name in owners:
                    warnings.append(f"skill {entry.name!r} in {source} is shadowed by {owners[entry.name]}")
                    continue
                if entry.is_dir():
                    owners[entry.name] = source
                    tree.add_dir(f"skills/{entry.name}")
                    for rel, path in _walk(entry, warnings):
                        tree.add_file(f"skills/{entry.name}/{rel}", path, _mode(path))
                elif entry.is_file():
                    owners[entry.name] = source
                    tree.add_file(f"skills/{entry.name}", entry, _mode(entry))
    body = json.dumps(config_json(config, resolved), indent=2, sort_keys=True) + "\n"
    tree.add_file("config.json", body.encode())
    return tree


def _content(source: Path | bytes) -> bytes:
    return source if isinstance(source, bytes) else source.read_bytes()


def _write_atomic(path: Path, data: bytes, mode: int) -> None:
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".kbx-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def _remove(path: Path) -> None:
    if path.is_dir() and not path.is_symlink():
        for child in path.iterdir():
            _remove(child)
        path.rmdir()
    else:
        path.unlink()


def sync(root: Path, tree: Tree) -> int:
    """Make `root` match `tree` in place. Returns the number of changes."""
    changes = 0
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o755)
    for rel in sorted(tree.dirs, key=lambda r: len(Path(r).parts)):
        path = root / rel
        if path.is_symlink() or (path.exists() and not path.is_dir()):
            path.unlink()
        if not path.exists():
            path.mkdir(mode=0o755)
            changes += 1
        os.chmod(path, 0o755)
    for rel, source in sorted(tree.files.items()):
        path = root / rel
        data = _content(source)
        mode = tree.modes[rel]
        if path.is_dir() and not path.is_symlink():
            _remove(path)
        elif path.is_file() and not path.is_symlink():
            if stat.S_IMODE(path.stat().st_mode) == mode and path.read_bytes() == data:
                continue
        _write_atomic(path, data, mode)
        changes += 1
    # Remove what is no longer wanted, deepest first. Directories that are
    # still wanted are never touched, so their inodes stay the same.
    for current, dirs, files in os.walk(root, topdown=False):
        base = Path(current)
        for name in files + dirs:
            path = base / name
            rel = str(path.relative_to(root))
            if rel in tree.files or rel in tree.dirs:
                continue
            _remove(path)
            changes += 1
    return changes


def build(paths: Paths, config: Config, resolved: list[Resolved]) -> list[str]:
    """Restage under a lock. Returns warnings to show the user."""
    warnings: list[str] = []
    paths.data_dir.mkdir(parents=True, exist_ok=True)
    tree = desired(config, resolved, warnings)
    with open(paths.data_dir / "stage.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        try:
            sync(paths.stage, tree)
        except OSError as exc:
            raise KbxError(f"cannot write the stage at {paths.stage}: {exc}") from None
    return warnings
