"""Mount mode's host-side guard: the agent may not plant code that host tools run.

In mount mode the checkout, `.git` included, is shared with the sandbox. Host
git then runs whatever the repository says: `core.fsmonitor` on every `git
status`, hooks on every commit, a `commondir` file that swaps in another
config. Kata shares a bind mount non-recursively, so a read-only sub-mount
cannot protect these paths from inside the VM. The guard watches them from the
host instead, in the checkout's `.git`, its submodules and worktrees, and in
repositories nested in the tree:

- config entries outside a known-safe list, new since the baseline,
- hooks, the in-tree `core.hooksPath` directory and the `protect` paths
  (hook-framework and editor files),
- `commondir` files that point anywhere but their own repository.

On a finding it neutralizes first (the agent's version goes to quarantine, the
trusted one comes back), then pauses the sandbox and tells the user. `kbx
resume` shows what happened; `kbx resume --accept` takes the change back.

The baseline is taken at every start, while the agent cannot run: whatever the
user changed while the sandbox was stopped is trusted. A guard process runs
while the sandbox does (`kbx _guard NAME`, spawned by the launcher); when the
sandbox stops it checks once more and seals the state. Without a seal (the
guard died, the host rebooted), the next start checks before it trusts.

A profile sandbox mounts several roots, each with a baseline of its own (under
`roots/<hash>/` of the sandbox's guard directory); one guard process watches
them all, and a finding in any pauses the whole sandbox. A root that is not a
repository itself holds projects: every repository found in it gets the checks
a checkout gets (its hooks path and `protect` paths, relative to it), and a
repository whose `.git` turns into a pointer elsewhere is a finding. A project
that disappears with its `.git` (the user removed it) is not. Repositories that
appear later are found by the walk: at once for a new directory directly in
the root, within WALK_INTERVAL deeper down.
"""

from __future__ import annotations

import contextlib
import ctypes
import difflib
import fcntl
import hashlib
import json
import os
import select
import shutil
import stat
import subprocess
import sys
import time
from collections.abc import Generator, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .docker import Docker
from .errors import KbxError
from .paths import Paths
from .sandbox import Sandbox, git_dirs, pause

STATE_VERSION = 1
MAX_FILES = 2000  # per protected directory tree
MAX_DIFF_LINES = 60
CHECK_INTERVAL = 1.0  # without inotify events
STATUS_INTERVAL = 2.0
WALK_INTERVAL = 60.0  # full-tree search for nested repositories

_CORE = frozenset(
    {
        "repositoryformatversion", "filemode", "logallrefupdates", "ignorecase", "precomposeunicode",
        "symlinks", "autocrlf", "eol", "safecrlf", "quotepath", "longpaths", "untrackedcache", "splitindex",
        "commitgraph", "multipackindex", "compression", "loosecompression", "bigfilethreshold", "abbrev",
        "sparsecheckout", "sparsecheckoutcone", "checkstat", "trustctime", "preloadindex", "protecthfs",
        "protectntfs", "warnambiguousrefs", "sharedrepository", "deltabasecachelimit", "packedgitlimit",
        "packedgitwindowsize", "fsync", "fsyncmethod",
    }
)  # fmt: skip
# Config keys the guard lets through: none of them runs a program, reads a
# file as config, or sends data somewhere new. Keys are as `git config --list`
# prints them (section and name lowercased). Everything else is flagged when
# it is new since the baseline, so the user's own settings stay untouched.
SAFE: dict[str, frozenset[str] | None] = {
    "core": _CORE,
    "user": frozenset({"name", "email", "signingkey"}),
    "init": frozenset({"defaultbranch"}),
    "pull": frozenset({"rebase", "ff"}),
    "push": frozenset({"default", "autosetupremote", "followtags"}),
    "fetch": frozenset({"prune", "prunetags", "writecommitgraph", "recursesubmodules", "showforcedupdates"}),
    "merge": frozenset({"ff", "conflictstyle", "renames", "renamelimit", "log"}),
    "diff": frozenset(
        {"algorithm", "renames", "colormoved", "mnemonicprefix", "noprefix", "context", "indentheuristic", "submodule"}
    ),
    "rebase": frozenset({"autosquash", "autostash", "updaterefs", "abbreviatecommands", "missingcommitscheck"}),
    "rerere": frozenset({"enabled", "autoupdate"}),
    "status": frozenset({"showuntrackedfiles", "submodulesummary", "short", "branch", "relativepaths", "showstash"}),
    "index": frozenset({"version", "skiphash", "sparse", "threads"}),
    "maintenance": frozenset({"auto", "strategy"}),
    "lfs": frozenset({"repositoryformatversion", "locksverify", "fetchinclude", "fetchexclude"}),
    "color": None,
    "advice": None,
    "gui": None,
    "log": None,
    "i18n": None,
    "gc": None,
    "pack": None,
    "feature": None,
}
# The same, for keys with a subsection (remote.<name>.fetch, …).
SAFE_SUB: dict[str, frozenset[str] | None] = {
    # url: a clone or `git submodule update` writes it. A push to a new URL
    # sends code (the internet is open anyway), and never the user's credentials
    # for other hosts. uploadpack, receivepack, vcs and proxy stay flagged.
    "remote": frozenset(
        {"url", "pushurl", "fetch", "push", "tagopt", "prune", "prunetags", "mirror", "skipdefaultupdate"}
    ),
    "branch": frozenset({"remote", "pushremote", "merge", "rebase", "description"}),
    "submodule": frozenset({"url", "active", "branch", "shallow", "fetchrecursesubmodules", "ignore"}),
    "lfs": frozenset({"access", "locksverify"}),
    "color": None,
    "gui": None,
    "gc": None,
}

_INOTIFY_MASK = 0x2 | 0x4 | 0x8 | 0x40 | 0x80 | 0x100 | 0x200 | 0x400 | 0x800

# Dependency and cache directories the walk for nested repositories skips: huge,
# rebuilt by tools, and not where anyone runs git. Each applies only beside the
# file that marks its tool, where there is one.
SKIP_DIRS: dict[str, tuple[str, ...]] = {
    "node_modules": (),
    "__pycache__": (),
    ".tox": (),
    ".nox": (),
    ".mypy_cache": (),
    ".pytest_cache": (),
    ".ruff_cache": (),
    "target": ("Cargo.toml",),
}


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


_verbose = False  # the guard process logs; the launcher reports through alerts


def log(message: str) -> None:
    if _verbose:
        print(f"{_now()} {message}", file=sys.stderr, flush=True)


def _git_env() -> dict[str, str]:
    """For reading a single config file: no system or global config, no repository."""
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "LANG": "C",
        "HOME": "/nonexistent",
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CEILING_DIRECTORIES": "/",
    }


def _inside(root: Path, path: Path) -> bool:
    return path == root or root in path.parents


def _label(root: Path, path: Path) -> str:
    """How findings and the baseline name a path: relative inside the project,
    absolute outside (a linked worktree's repository directory). `root / label`
    gives the path back either way."""
    return str(path.relative_to(root)) if _inside(root, path) else str(path)


def _stored(base: Path, label: str) -> Path:
    """Where a copy of `label` lives under `base` (trusted copies, quarantine)."""
    return base / "host" / label.lstrip("/") if label.startswith("/") else base / "tree" / label


def is_safe(key: str, value: str | None, gitdir: Path, root: Path) -> bool:
    section, _, rest = key.partition(".")
    subsection, _, name = rest.rpartition(".")
    if not name:
        return False
    table = SAFE_SUB if subsection else SAFE
    if section not in table:
        allowed: frozenset[str] | None = frozenset()
    else:
        allowed = table[section]
    if allowed is None or name in allowed:
        return True
    if subsection:
        return False
    if (section, name) == ("core", "fsmonitor"):
        return (value or "").lower() in ("true", "false")  # the built-in daemon, not a command
    if (section, name) == ("core", "bare"):
        return (value or "").lower() == "false"
    if (section, name) == ("core", "worktree") and value:
        # Submodules point at their checkout; anything outside the project is not theirs.
        target = Path(os.path.normpath(gitdir / value))
        return _inside(root, target) and ".git" not in target.relative_to(root).parts
    return False


class ConfigError(Exception):
    pass


def config_entries(path: Path) -> list[str]:
    """`key=value` lines of one config file (no includes, nothing executed)."""
    try:
        result = subprocess.run(
            ["git", "config", "--file", str(path), "--list", "-z"],
            cwd="/",
            env=_git_env(),
            capture_output=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ConfigError(str(exc)) from None
    if result.returncode != 0:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        raise ConfigError(detail[-1] if detail else f"git config exited {result.returncode}")
    entries: list[str] = []
    for item in result.stdout.decode("utf-8", "replace").split("\0"):
        if not item:
            continue
        key, newline, value = item.partition("\n")
        entries.append(f"{key}={value}" if newline else key)
    return entries


def _split(entry: str) -> tuple[str, str | None]:
    key, equals, value = entry.partition("=")
    return key, value if equals else None


def flagged_entries(path: Path, gitdir: Path, root: Path) -> list[str]:
    return sorted(e for e in config_entries(path) if not is_safe(*_split(e), gitdir, root))


def global_hooks_path() -> str | None:
    try:
        result = subprocess.run(
            ["git", "config", "--global", "--get", "core.hooksPath"],
            cwd="/",
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def _digest(path: Path) -> str:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        return "link:" + os.readlink(path)
    if not stat.S_ISREG(info.st_mode):
        return f"special:{stat.S_IFMT(info.st_mode):o}"
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            hasher.update(chunk)
    mode = "x" if info.st_mode & 0o111 else "-"
    return f"{mode}{hasher.hexdigest()}"


def _read_gitfile(path: Path) -> Path | None:
    try:
        text = path.read_text(errors="replace")
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    target = Path(text[len("gitdir:") :].strip())
    return Path(os.path.normpath(path.parent / target))


@dataclass(frozen=True)
class GitDir:
    path: Path
    common: Path | None = None  # set for a linked worktree's private directory


@dataclass(frozen=True)
class Finding:
    path: str  # a label: relative to the project, or absolute outside it
    kind: str  # config, file, commondir, gitfile, symlink, unreadable, gitdir, limit
    detail: str
    entries: tuple[str, ...] = ()  # config: the flagged `key=value` entries


@dataclass
class Snapshot:
    flagged: dict[str, list[str]] = field(default_factory=dict[str, list[str]])
    files: dict[str, str] = field(default_factory=dict[str, str])
    configs: list[str] = field(default_factory=list[str])  # kept as trusted copies, not hashed
    problems: list[Finding] = field(default_factory=list[Finding])
    watch: set[str] = field(default_factory=set[str])
    gone: list[str] = field(default_factory=list[str])  # labels of projects removed with their .git


def _expand(gitdir: Path) -> Iterator[GitDir]:
    """A repository's own directory, its submodules' and its worktrees'."""
    yield GitDir(gitdir)
    worktrees = gitdir / "worktrees"
    if worktrees.is_dir() and not worktrees.is_symlink():
        for entry in sorted(worktrees.iterdir()):
            if entry.is_dir() and not entry.is_symlink():
                yield GitDir(entry, common=gitdir)
    modules = gitdir / "modules"
    if modules.is_dir() and not modules.is_symlink():
        # Submodule names may contain slashes: any directory with HEAD is one.
        pending = [modules]
        while pending:
            current = pending.pop()
            for entry in sorted(current.iterdir()):
                if not entry.is_dir() or entry.is_symlink():
                    continue
                if (entry / "HEAD").exists():
                    yield from _expand(entry)
                else:
                    pending.append(entry)


def _skipped(base: Path, name: str, files: Sequence[str]) -> bool:
    if name == ".git":
        return True
    if name in (".venv", "venv"):
        return (base / name / "pyvenv.cfg").is_file()
    markers = SKIP_DIRS.get(name)
    return markers is not None and (not markers or any(marker in files for marker in markers))


def find_repos(root: Path) -> list[tuple[Path, Path]]:
    """Every repository in the tree: (its working directory, its git directory),
    the root's own included. A `.git` that leads outside the tree is left out."""
    found: list[tuple[Path, Path]] = []
    for current, dirs, files in os.walk(root):
        base = Path(current)
        if ".git" in dirs or ".git" in files:
            entry = base / ".git"
            target = entry
            if entry.is_symlink():
                target = Path(os.path.realpath(entry))
            elif entry.is_file():
                target = _read_gitfile(entry) or entry
            if target.is_dir() and _inside(root, target):
                found.append((base, target))
        dirs[:] = sorted(d for d in dirs if not _skipped(base, d, files))
    return found


def nested_gitdirs(root: Path) -> list[Path]:
    """Repositories inside the tree (not the checkout's own): editors run git in them too."""
    return [gitdir for base, gitdir in find_repos(root) if base != root]


def _top_level(repos: Sequence[tuple[Path, Path]]) -> list[tuple[Path, Path]]:
    """The projects of a root that is not a repository: repositories not inside another's tree."""
    bases = {base for base, _ in repos}
    return [(base, gitdir) for base, gitdir in repos if not any(p in bases for p in base.parents)]


def _common(gitdir: Path) -> Path:
    """A git directory's common directory (itself, unless it is a linked worktree's)."""
    commondir = gitdir / "commondir"
    if commondir.is_file() and not commondir.is_symlink():
        text = commondir.read_text(errors="replace").strip()
        if text:
            return Path(os.path.normpath(gitdir / text))
    return gitdir


def _repo_layout(base: Path) -> dict[str, str] | None:
    """How a project's `.git` looks: a directory, or a file pointing to one."""
    entry = base / ".git"
    if entry.is_symlink():
        return None
    if entry.is_dir():
        return {"kind": "dir"}
    target = _read_gitfile(entry) if entry.is_file() else None
    return {"kind": "file", "target": os.path.realpath(target)} if target else None


def _root_key(root: Path) -> str:
    return hashlib.sha256(str(root).encode()).hexdigest()[:12]


class Guard:
    """State, checks and actions for one sandbox's checkout, or one root of a
    profile sandbox (`root`). Methods ending in `_locked` expect the caller to
    hold `lock()`."""

    def __init__(self, paths: Paths, name: str, root: Path | None = None) -> None:
        self.paths = paths
        self.name = name
        self.dir = paths.guard_dir / name if root is None else paths.guard_dir / name / "roots" / _root_key(root)
        self.nested: list[Path] = []
        self.projects: list[tuple[Path, Path]] = []  # a root that is not a repository: its repositories
        self._heads: set[str] = set()  # those directly in the root, to notice a new one at once
        self.watch: set[str] = set()  # directories the last snapshot looked at
        self._walked = False
        self._global_hooks: str | None = None
        self._configs: dict[str, tuple[tuple[int, int, int, int], list[str]]] = {}
        self._kept: dict[str, tuple[int, int, int, int]] = {}

    # --- files ---

    @property
    def state_file(self) -> Path:
        return self.dir / "state.json"

    @property
    def alert_file(self) -> Path:
        return self.dir / "alert.json"

    @property
    def trusted(self) -> Path:
        return self.dir / "trusted"

    @contextlib.contextmanager
    def lock(self, create: bool = True) -> Generator[None]:
        """Without `create`, a removed state directory (`kbx rm`) stays removed:
        FileNotFoundError instead."""
        if create:
            self.dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        with open(self.dir / "lock", "w") as handle:
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def load_state(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.state_file.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) and data.get("version") == STATE_VERSION else None

    def save_state(self, state: dict[str, Any]) -> None:
        _write_json(self.state_file, state)

    def load_alert(self) -> dict[str, Any] | None:
        try:
            data = json.loads(self.alert_file.read_text())
        except (OSError, ValueError):
            return None
        return data if isinstance(data, dict) else None

    # --- scanning ---

    def snapshot(
        self,
        root: Path,
        protect: Sequence[str],
        walk: bool,
        layout: tuple[Path, Path] | None,
        repos: dict[str, dict[str, str]] | None = None,
    ) -> Snapshot:
        """`layout`: the checkout's git directory and the repository's common
        directory, as trusted at the baseline (see sandbox.git_dirs); None for a
        profile root that is not a repository, whose projects' `.git` layouts at
        the baseline are `repos`."""
        snap = Snapshot()
        snap.watch.add(str(root))
        if layout is None:
            self._check_repos(root, repos or {}, snap)
            heads = (
                {e.name for e in os.scandir(root) if os.path.lexists(Path(e.path) / ".git")} if root.is_dir() else set()
            )
            walk = walk or heads != self._heads
            self._heads = heads
        else:
            gitdir, common = layout
            entry = root / ".git"
            if gitdir == entry:
                if entry.is_symlink() or not entry.is_dir():
                    snap.problems.append(Finding(".git", "gitdir", "is no longer the repository directory"))
                    return snap
            else:
                # A linked worktree: `.git` is a file naming its git directory.
                target = _read_gitfile(entry) if entry.is_file() and not entry.is_symlink() else None
                if target is None or os.path.realpath(target) != os.path.realpath(gitdir):
                    snap.problems.append(Finding(".git", "gitfile", "points git to another directory"))
                else:
                    snap.configs.append(".git")
                if not common.is_dir():
                    snap.problems.append(
                        Finding(_label(root, common), "gitdir", "is no longer the repository directory")
                    )
                    return snap
        if walk or not self._walked:
            found = find_repos(root) if root.is_dir() else []
            self.nested = [gitdir for base, gitdir in found if base != root]
            self.projects = _top_level(found) if layout is None else []
            self._global_hooks = global_hooks_path()
            self._walked = True
        seen: set[str] = set()
        for top in self.nested if layout is None else [layout[1], *self.nested]:
            if not top.is_dir():
                continue
            for repo in _expand(top):
                real = os.path.realpath(repo.path)
                if real in seen:
                    continue
                seen.add(real)
                self._scan_gitdir(root, repo, snap)
        checkouts = [(root, layout[1])] if layout is not None else self.projects
        for checkout, gitdir in checkouts:
            common = layout[1] if layout is not None else _common(gitdir)
            hooks_path = self._hooks_path(checkout, _label(root, common / "config"), snap)
            if hooks_path is not None:
                self._scan_tree(root, hooks_path, snap)
            for rel in protect:
                self._scan_tree(root, checkout / rel, snap)
        self.watch = snap.watch
        return snap

    def _check_repos(self, root: Path, repos: dict[str, dict[str, str]], snap: Snapshot) -> None:
        """A profile root: each repository's `.git` as at the baseline. Gone with
        it (the project was removed) is fine; pointing elsewhere is not."""
        for label, before in sorted(repos.items()):
            base = root / label if label != "." else root
            entry = base / ".git"
            if not os.path.lexists(entry):
                snap.gone.append(label)
                continue
            if _repo_layout(base) != before:
                rel = ".git" if label == "." else f"{label}/.git"
                snap.problems.append(Finding(rel, "gitfile", "now points git to another directory"))

    def _hooks_path(self, checkout: Path, config: str, snap: Snapshot) -> Path | None:
        """The in-tree hooks directory, if core.hooksPath points into the checkout.
        A relative path is relative to the checkout git runs in."""
        value = self._global_hooks
        for entry in snap.flagged.get(config, []):  # core.hookspath is never a safe key
            key, text = _split(entry)
            if key == "core.hookspath":
                value = text
        if not value:
            return None
        path = Path(os.path.expanduser(value))
        path = Path(os.path.normpath(path if path.is_absolute() else checkout / path))
        return path if _inside(checkout, path) and path != checkout else None

    def _scan_gitdir(self, root: Path, gitdir: GitDir, snap: Snapshot) -> None:
        path = gitdir.path
        snap.watch.add(str(path))
        for sub in ("worktrees", "modules"):
            if (path / sub).is_dir():
                snap.watch.add(str(path / sub))
        rel = _label(root, path)
        commondir = path / "commondir"
        if commondir.is_symlink() or commondir.exists():
            wanted = gitdir.common or path
            target = None
            if commondir.is_file() and not commondir.is_symlink():
                text = commondir.read_text(errors="replace").strip()
                target = Path(os.path.normpath(path / text)) if text else None
            if target is None or os.path.realpath(target) != os.path.realpath(wanted):
                snap.problems.append(Finding(f"{rel}/commondir", "commondir", "redirects git to another directory"))
        names = ["config.worktree"] if gitdir.common else ["config", "config.worktree"]
        for name in names:
            config = path / name
            config_rel = f"{rel}/{name}"
            if config.is_symlink():
                snap.problems.append(Finding(config_rel, "symlink", "is a symlink"))
                continue
            if not config.exists():
                continue
            try:
                snap.flagged[config_rel] = self._flagged(config, path, root)
                snap.configs.append(config_rel)
            except ConfigError as exc:
                snap.problems.append(Finding(config_rel, "unreadable", f"cannot be parsed ({exc})"))
        if gitdir.common:
            return
        hooks = path / "hooks"
        if hooks.is_symlink():
            snap.problems.append(Finding(f"{rel}/hooks", "symlink", "is a symlink"))
        elif hooks.is_dir():
            self._scan_tree(root, hooks, snap, skip_samples=True)

    def _flagged(self, config: Path, gitdir: Path, root: Path) -> list[str]:
        info = config.stat()
        key = (info.st_ino, info.st_mtime_ns, info.st_size, info.st_ctime_ns)
        cached = self._configs.get(str(config))
        if cached is not None and cached[0] == key:
            return cached[1]
        entries = flagged_entries(config, gitdir, root)
        self._configs[str(config)] = (key, entries)
        return entries

    def _scan_tree(self, root: Path, top: Path, snap: Snapshot, skip_samples: bool = False) -> None:
        if not top.is_symlink() and not top.exists():
            if top.parent.is_dir():
                snap.watch.add(str(top.parent))  # to see it appear
            return
        rel_top = _label(root, top)
        if top.is_symlink() or not top.is_dir():
            snap.watch.add(str(top.parent))
            snap.files[rel_top] = _digest(top)
            return
        count = 0
        for current, dirs, files in os.walk(top):
            snap.watch.add(current)
            dirs.sort()
            for name in sorted(files) + [d for d in dirs if (Path(current) / d).is_symlink()]:
                if skip_samples and name.endswith(".sample"):
                    continue
                path = Path(current) / name
                count += 1
                if count > MAX_FILES:
                    snap.problems.append(Finding(rel_top, "limit", f"has more than {MAX_FILES} files"))
                    return
                try:
                    snap.files[_label(root, path)] = _digest(path)
                except OSError:
                    continue

    # --- comparing and acting ---

    def compare(self, state: dict[str, Any], snap: Snapshot) -> list[Finding]:
        gone = [p for p in snap.problems if p.kind == "gitdir"]
        if gone:
            return gone  # nothing else can be compared or repaired
        findings = list(snap.problems)
        base_flagged: dict[str, list[str]] = state.get("flagged", {})
        for rel, entries in sorted(snap.flagged.items()):
            added = sorted(set(entries) - set(base_flagged.get(rel, [])))
            if added:
                findings.append(Finding(rel, "config", "adds " + "; ".join(added), tuple(added)))
        base_files: dict[str, str] = state.get("files", {})
        for rel in sorted(set(base_files) | set(snap.files)):
            before, after = base_files.get(rel), snap.files.get(rel)
            if before == after:
                continue
            if after is None and any(rel.startswith(f"{label}/") for label in snap.gone if label != "."):
                continue  # its project was removed
            what = "added" if before is None else "removed" if after is None else "changed"
            findings.append(Finding(rel, "file", what))
        return findings

    def trust_locked(self, root: Path, protect: Sequence[str], *, sealed: bool = False) -> Snapshot:
        """Take the current state as the baseline, with copies to restore from.
        A profile root need not be a repository (then `repo` is false)."""
        repo = os.path.lexists(root / ".git")
        layout = git_dirs(root) if repo else None
        snap = self.snapshot(root, protect, walk=True, layout=layout)
        gitdir, common = layout or (None, None)
        repos: dict[str, dict[str, str]] = {}
        if not repo:
            for base, _ in find_repos(root) if root.is_dir() else []:
                found = _repo_layout(base)
                if found is not None:
                    repos[_label(root, base)] = found
        shutil.rmtree(self.trusted, ignore_errors=True)
        self.trusted.mkdir(parents=True, mode=0o700)
        self._kept.clear()
        for rel in [*snap.files, *snap.configs]:
            self._keep(root, rel)
        self.save_state(
            {
                "version": STATE_VERSION,
                "root": str(root),
                "repo": repo,
                "gitdir": str(gitdir) if gitdir else "",
                "common": str(common) if common else "",
                "repos": repos,
                "protect": list(protect),
                "flagged": snap.flagged,
                "files": {rel: digest for rel, digest in snap.files.items()},
                "sealed": sealed,
                "time": _now(),
            }
        )
        return snap

    def _keep(self, root: Path, rel: str) -> None:
        source = root / rel
        if source.is_symlink() or not source.is_file():
            return
        info = source.stat()
        key = (info.st_ino, info.st_mtime_ns, info.st_size, info.st_ctime_ns)
        dest = _stored(self.trusted, rel)
        if self._kept.get(rel) == key and dest.is_file():
            return
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
        self._kept[rel] = key

    def check_locked(self, state: dict[str, Any], walk: bool) -> list[dict[str, str]]:
        """Compare with the baseline; neutralize and record what differs."""
        root = Path(state["root"])
        if not root.is_dir():
            return []
        snap = self.snapshot(root, state.get("protect", []), walk, _layout(state), state.get("repos"))
        findings = self.compare(state, snap)
        if not findings:
            for rel in snap.configs:  # the user's own safe edits become the version to restore
                self._keep(root, rel)
            return []
        quarantine = self.dir / "quarantine" / f"{time.strftime('%Y%m%d-%H%M%S')}-{time.time_ns() % 1000000:06d}"
        actions = [self._neutralize(root, state, finding, quarantine) for finding in findings]
        again = self.snapshot(root, state.get("protect", []), False, _layout(state), state.get("repos"))
        left = {f.path for f in self.compare(state, again)}
        for action in actions:
            if action["path"] in left and "by hand" not in action["action"]:
                action["action"] += "; still there, fix it by hand"
        self._record(actions)
        return actions

    def _neutralize(self, root: Path, state: dict[str, Any], finding: Finding, quarantine: Path) -> dict[str, str]:
        path = root / finding.path
        action: dict[str, str] = {"path": finding.path, "kind": finding.kind, "detail": finding.detail}
        saved = _stored(quarantine, finding.path)
        try:
            if finding.kind == "config":
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(path, saved)
                self._reset_keys(path, finding, state.get("flagged", {}).get(finding.path, []))
                action.update(action="removed the new settings", quarantine=str(saved))
            elif finding.kind == "gitfile" and finding.path != ".git":
                # A project's .git now leads elsewhere; its own directory is gone.
                saved.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(saved))
                action.update(action="moved it away, so git no longer runs there", quarantine=str(saved))
            elif finding.kind in ("file", "symlink", "unreadable", "commondir", "gitfile"):
                if path.is_symlink() or path.exists():
                    saved.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(path), str(saved))
                    action["quarantine"] = str(saved)
                restored = self._restore(root, finding.path, state)
                if finding.kind == "commondir" and finding.path.split("/")[-3:-2] == ["worktrees"]:
                    # A worktree needs its commondir: point it back at its repository.
                    (root / finding.path).write_text("../..\n")
                    restored = True
                action["action"] = "restored the trusted version" if restored else "removed it"
            else:
                action["action"] = "cannot be repaired automatically; inspect it by hand"
        except OSError as exc:
            action["action"] = f"could not repair it ({exc})"
        log(f"guard {self.name}: {finding.path}: {finding.detail} → {action['action']}")
        return action

    def _reset_keys(self, path: Path, finding: Finding, baseline: list[str]) -> None:
        for key in sorted({_split(entry)[0] for entry in finding.entries}):
            subprocess.run(
                ["git", "config", "--file", str(path), "--unset-all", key],
                cwd="/",
                env=_git_env(),
                capture_output=True,
                timeout=20,
                check=False,
            )
            for entry in baseline:
                base_key, value = _split(entry)
                if base_key == key:
                    subprocess.run(
                        ["git", "config", "--file", str(path), "--add", key, "true" if value is None else value],
                        cwd="/",
                        env=_git_env(),
                        capture_output=True,
                        timeout=20,
                        check=False,
                    )
        self._configs.pop(str(path), None)

    def _restore(self, root: Path, rel: str, state: dict[str, Any]) -> bool:
        """Put the baseline version of `rel` (or of everything under it) back."""
        restored = False
        files: dict[str, str] = state.get("files", {})
        wanted = {r: d for r, d in files.items() if r == rel or r.startswith(rel + "/")}
        if rel not in wanted and _stored(self.trusted, rel).is_file():
            wanted[rel] = "copy"  # a config or .git file: kept, but not hashed
        for item, digest in sorted(wanted.items()):
            dest = root / item
            if dest.is_symlink() or dest.exists():
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            if digest.startswith("link:"):
                dest.symlink_to(digest[len("link:") :])
                restored = True
            elif _stored(self.trusted, item).is_file():
                shutil.copy2(_stored(self.trusted, item), dest)
                restored = True
        return restored

    def _record(self, actions: list[dict[str, str]]) -> None:
        alert = self.load_alert() or {"time": _now(), "findings": []}
        for action in actions:
            action["time"] = _now()
        alert["findings"].extend(actions)
        _write_json(self.alert_file, alert)

    # --- lifecycle ---

    def seal(self) -> list[dict[str, str]]:
        """After the sandbox stopped: a last check, then mark the state clean."""
        try:
            with self.lock(create=False):
                state = self.load_state()
                if state is None:
                    return []
                actions = self.check_locked(state, walk=True)
                if not actions:
                    state["sealed"] = True
                    self.save_state(state)
                return actions
        except FileNotFoundError:
            return []  # removed with the sandbox (`kbx rm`)


def _layout(state: dict[str, Any]) -> tuple[Path, Path] | None:
    if state.get("repo") is False:
        return None
    root = Path(state["root"])
    return Path(state.get("gitdir") or root / ".git"), Path(state.get("common") or root / ".git")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + ".tmp")
    temp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(temp, path)


def summary(actions: Sequence[dict[str, str]]) -> str:
    first = actions[0]
    more = f" (+{len(actions) - 1} more)" if len(actions) > 1 else ""
    return f"{first['path']} {first['detail']}{more}"


# --- launcher entry points ---


def _alert_error(root: Path, actions: Sequence[dict[str, str]]) -> KbxError:
    return KbxError(
        f"the kbx guard found changes host tools would run in {root}: {summary(actions)}. "
        "They are neutralized; `kbx resume` shows the details"
    )


def guards(paths: Paths, sandbox: Sandbox) -> list[tuple[Path, Guard]]:
    """Each mounted root and its guard: the checkout of a project sandbox, every
    root of a profile sandbox (`sandbox.roots` must be what the container mounts)."""
    if not sandbox.profile:
        return [(sandbox.project_dir, Guard(paths, sandbox.name))]
    return [(root, Guard(paths, sandbox.name, root)) for root in sandbox.roots]


def recorded(paths: Paths, name: str) -> list[Guard]:
    """Every guard with state on disk for this sandbox (for the guard process and
    the watcher, which know only its name)."""
    found = [Guard(paths, name)]
    roots = paths.guard_dir / name / "roots"
    if roots.is_dir():
        for entry in sorted(roots.iterdir()):
            guard = Guard(paths, name)
            guard.dir = entry
            found.append(guard)
    return [guard for guard in found if guard.state_file.exists() or guard.alert_file.exists()]


def alerted(paths: Paths, name: str) -> bool:
    return any(guard.alert_file.exists() for guard in recorded(paths, name))


def seal(paths: Paths, name: str) -> list[dict[str, str]]:
    """After the sandbox stopped: a last check of every root, then seal each."""
    return [action for guard in recorded(paths, name) for action in guard.seal()]


def _forget_other_roots(paths: Paths, sandbox: Sandbox) -> None:
    """A profile no longer mounts these roots: drop their baselines (alerts stay)."""
    roots = paths.guard_dir / sandbox.name / "roots"
    if not sandbox.profile or not roots.is_dir():
        return
    wanted = {_root_key(root) for root in sandbox.roots}
    for entry in roots.iterdir():
        if entry.name not in wanted and not (entry / "alert.json").exists():
            shutil.rmtree(entry, ignore_errors=True)


def before_start(paths: Paths, sandbox: Sandbox, protect: Sequence[str]) -> None:
    """The sandbox is not running: check an unsealed state, then take the baseline."""
    for guard in recorded(paths, sandbox.name):
        alert = guard.load_alert()
        if alert:
            state = guard.load_state() or {}
            raise _alert_error(Path(state.get("root") or sandbox.project_dir), alert["findings"])
    _forget_other_roots(paths, sandbox)
    for root, guard in guards(paths, sandbox):
        with guard.lock():
            state = guard.load_state()
            if state is not None and not state.get("sealed") and Path(state["root"]) == root:
                actions = guard.check_locked(state, walk=True)
                if actions:
                    raise _alert_error(root, actions)
            guard.trust_locked(root, protect)


def _pid_file(paths: Paths, name: str) -> Path:
    return paths.runtime_dir / f"guard-{name}.pid"


def running(paths: Paths, name: str) -> bool:
    try:
        pid = int(_pid_file(paths, name).read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return False
    return b"_guard" in cmdline and name.encode() in cmdline


@contextlib.contextmanager
def _spawn_lock(paths: Paths, name: str) -> Generator[None]:
    """One launcher at a time decides whether this sandbox needs a guard process."""
    directory = paths.guard_dir / name
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    with open(directory / "spawn.lock", "w") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        yield


def ensure(paths: Paths, sandbox: Sandbox, docker: Docker, entry: Path, protect: Sequence[str]) -> None:
    """The sandbox is running: make sure a guard watches it (checking first if none did)."""
    with _spawn_lock(paths, sandbox.name):
        _ensure_locked(paths, sandbox, docker, entry, protect)
    for root, guard in guards(paths, sandbox):
        alert = guard.load_alert()
        if alert:
            raise _alert_error(root, alert["findings"])


def _ensure_locked(paths: Paths, sandbox: Sandbox, docker: Docker, entry: Path, protect: Sequence[str]) -> None:
    if not running(paths, sandbox.name):
        for root, guard in guards(paths, sandbox):
            with guard.lock():
                state = guard.load_state()
                if state is None or Path(state["root"]) != root:
                    print(
                        f"⚠ the kbx guard has no baseline for {root} in this running sandbox; taking the current state",
                        file=sys.stderr,
                    )
                    guard.trust_locked(root, protect)
                else:
                    actions = guard.check_locked(state, walk=True)
                    if actions:
                        pause(docker, sandbox)
                    state["sealed"] = False
                    guard.save_state(state)
        spawn(paths, sandbox.name, entry)


def spawn(paths: Paths, name: str, entry: Path) -> None:
    paths.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    with open(paths.log_dir / f"guard-{name}.log", "ab") as log_file:
        process = subprocess.Popen(
            [sys.executable, str(entry), "_guard", name],
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=log_file,
            start_new_session=True,
            close_fds=True,
        )
    _pid_file(paths, name).write_text(f"{process.pid}\n")


def register_tty(paths: Paths, name: str) -> None:
    """Remember the attaching terminal, so the guard can explain a frozen agent there."""
    try:
        tty = os.ttyname(sys.stdout.fileno())
    except OSError:
        return
    ttys = paths.guard_dir / name / "ttys"
    try:
        ttys.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        known = ttys.read_text().split() if ttys.exists() else []
        if tty not in known:
            ttys.write_text("".join(f"{t}\n" for t in [*known[-7:], tty]))
    except OSError:
        pass


def _tell(guard: Guard, sandbox_root: str, actions: Sequence[dict[str, str]]) -> None:
    text = (
        f"\r\n\x1b[1;31m⚠ kbx guard paused this sandbox:\x1b[0m {summary(actions)} (neutralized).\r\n"
        f"  The agent is frozen. Detach with Ctrl-P Ctrl-Q, then run `kbx resume` in {sandbox_root}.\r\n"
    )
    try:
        ttys = (guard.paths.guard_dir / guard.name / "ttys").read_text().split()
    except OSError:
        ttys = []
    for tty in ttys:
        try:
            info = os.stat(tty)
            if not stat.S_ISCHR(info.st_mode) or info.st_uid != os.getuid():
                continue
            fd = os.open(tty, os.O_WRONLY | os.O_NOCTTY | os.O_NONBLOCK)
            try:
                os.write(fd, text.encode())
            finally:
                os.close(fd)
        except OSError:
            continue
    if shutil.which("notify-send"):
        subprocess.run(
            ["notify-send", "-u", "critical", f"kbx paused {guard.name}", f"{summary(actions)}. Run `kbx resume`."],
            capture_output=True,
            timeout=10,
            check=False,
        )


class Inotify:
    """Wakes the guard on changes in the watched directories (libc via ctypes)."""

    def __init__(self) -> None:
        self.libc = ctypes.CDLL(None, use_errno=True)
        self.fd = -1
        self.watched: set[str] = set()

    def update(self, dirs: set[str]) -> None:
        if dirs == self.watched and self.fd >= 0:
            return
        if self.fd >= 0:
            os.close(self.fd)
        self.fd = self.libc.inotify_init1(os.O_NONBLOCK | os.O_CLOEXEC)
        if self.fd < 0:
            raise OSError(ctypes.get_errno(), "inotify_init1 failed")
        for directory in sorted(dirs):
            self.libc.inotify_add_watch(self.fd, os.fsencode(directory), _INOTIFY_MASK)
        self.watched = set(dirs)

    def wait(self, timeout: float) -> None:
        ready, _, _ = select.select([self.fd], [], [], timeout)
        if not ready:
            return
        with contextlib.suppress(BlockingIOError):
            while os.read(self.fd, 65536):
                pass


def run(paths: Paths, name: str) -> int:
    """`kbx _guard NAME`: watch every root until the sandbox stops, then seal."""
    global _verbose
    _verbose = True
    watched = [(guard, state) for guard in recorded(paths, name) if (state := guard.load_state()) is not None]
    if not watched:
        log(f"guard {name}: no baseline; exiting")
        return 1
    docker = Docker()
    sandbox = Sandbox(name=name, project=name, project_dir=Path(watched[0][1]["root"]), mode="mount")
    try:
        watcher: Inotify | None = Inotify()
    except (OSError, AttributeError) as exc:
        log(f"guard {name}: no inotify ({exc}); checking every {CHECK_INTERVAL:.0f}s")
        watcher = None
    log(f"guard {name}: watching {', '.join(str(state['root']) for _, state in watched)}")
    status = "running"
    next_status = 0.0
    next_walk = time.monotonic() + WALK_INTERVAL
    while True:
        now = time.monotonic()
        if now >= next_status:
            data = docker.inspect("container", name)
            status = str(((data or {}).get("State") or {}).get("Status", "gone"))
            next_status = now + STATUS_INTERVAL
            if status not in ("running", "paused", "restarting"):
                actions = [action for guard, _ in watched for action in guard.seal()]
                log(f"guard {name}: sandbox is {status}; " + ("sealed" if not actions else summary(actions)))
                return 0
        if not (paths.guard_dir / name).is_dir():
            log(f"guard {name}: state removed; exiting")
            return 0
        if status == "paused":
            time.sleep(STATUS_INTERVAL)
            continue
        walk = now >= next_walk
        if walk:
            next_walk = now + WALK_INTERVAL
        found: tuple[Guard, dict[str, Any], list[dict[str, str]]] | None = None
        try:
            for index, (guard, last) in enumerate(watched):
                with guard.lock(create=False):
                    state = guard.load_state() or last
                    watched[index] = (guard, state)
                    actions = guard.check_locked(state, walk)
                if actions:
                    found = (guard, state, actions)
                    break
        except FileNotFoundError:
            log(f"guard {name}: state removed; exiting")
            return 0
        if found is not None:
            guard, state, actions = found
            paused = pause(docker, sandbox)
            log(f"guard {name}: {'paused' if paused else 'could not pause'} the sandbox")
            _tell(guard, state["root"], actions)
            status = "paused"
            continue
        if watcher is None:
            time.sleep(CHECK_INTERVAL)
            continue
        try:
            watcher.update({d for guard, _ in watched for d in guard.watch if os.path.isdir(d)})
            watcher.wait(CHECK_INTERVAL)
        except OSError as exc:
            log(f"guard {name}: inotify failed ({exc}); polling")
            watcher = None


# --- kbx resume ---


def _diff(before: Path | None, after: Path | None, label: str) -> list[str]:
    def lines(path: Path | None) -> list[str]:
        if path is None or not path.is_file() or path.is_symlink():
            return []
        data = path.read_bytes()
        if b"\0" in data[:8192]:
            return ["(binary)\n"]
        return data.decode("utf-8", "replace").splitlines(keepends=True)

    diff = list(difflib.unified_diff(lines(before), lines(after), f"trusted/{label}", f"agent/{label}"))
    if len(diff) > MAX_DIFF_LINES:
        diff = [*diff[:MAX_DIFF_LINES], f"… {len(diff) - MAX_DIFF_LINES} more lines\n"]
    return diff


def _alerts(paths: Paths, sandbox: Sandbox) -> list[tuple[Path, Guard]]:
    """Each root (with its guard) whose guard holds an alert, a root no longer
    mounted included."""
    found: list[tuple[Path, Guard]] = []
    for guard in recorded(paths, sandbox.name):
        if guard.alert_file.exists():
            state = guard.load_state() or {}
            found.append((Path(state.get("root") or sandbox.project_dir), guard))
    return found


def report(paths: Paths, sandbox: Sandbox) -> list[str]:
    """Lines describing the pending alerts (empty if there is none)."""
    lines: list[str] = []
    for root, guard in _alerts(paths, sandbox):
        alert = guard.load_alert()
        if not alert:
            continue
        lines.append(f"The kbx guard stopped these changes in {root}:")
        for action in alert["findings"]:
            lines.append(f"  {action['time']}  {action['path']}: {action['detail']}")
            lines.append(f"      → {action.get('action', '?')}")
            saved = action.get("quarantine")
            if saved:
                lines.append(f"      agent's version: {saved}")
                current = root / action["path"]
                for line in _diff(current if current.exists() else None, Path(saved), action["path"]):
                    lines.append("      " + line.rstrip("\n"))
    return lines


def resolve(paths: Paths, sandbox: Sandbox, protect: Sequence[str], accept: bool) -> list[str]:
    """Clear the alerts. With accept, put the quarantined versions back first.
    Either way the result becomes the new baseline, of every mounted root."""
    notes: list[str] = []
    for root, guard in _alerts(paths, sandbox):
        notes += _resolve_root(guard, root, accept)
    for root, guard in guards(paths, sandbox):
        if root.is_dir():
            with guard.lock():
                guard.trust_locked(root, protect)
    _forget_other_roots(paths, sandbox)
    return notes


def _resolve_root(guard: Guard, root: Path, accept: bool) -> list[str]:
    notes: list[str] = []
    with guard.lock():
        alert = guard.load_alert() or {"findings": []}
        if accept:
            for action in alert["findings"]:
                saved = action.get("quarantine")
                if not saved or not (Path(saved).exists() or Path(saved).is_symlink()):
                    continue
                dest = root / action["path"]
                if dest.is_dir() and not dest.is_symlink():
                    shutil.rmtree(dest)
                elif dest.is_symlink() or dest.exists():
                    dest.unlink()
                dest.parent.mkdir(parents=True, exist_ok=True)
                if Path(saved).is_dir() and not Path(saved).is_symlink():
                    shutil.copytree(saved, dest, symlinks=True)
                else:
                    shutil.copy2(saved, dest, follow_symlinks=False)
                notes.append(f"restored {action['path']} (the agent's version)")
        guard.alert_file.unlink(missing_ok=True)  # resolve() takes the new baselines
    return notes


def remove(paths: Paths, name: str) -> None:
    shutil.rmtree(paths.guard_dir / name, ignore_errors=True)
    _pid_file(paths, name).unlink(missing_ok=True)
