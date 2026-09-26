"""What the dashboard shows: one row per sandbox, details for one, and the
background refresh that keeps both current. No curses here (see kbx/dash.py).

The rule of kbx/git.py extends to the dashboard: the host never runs git in a
repository the agent can write. In mount mode every git question goes to the
sandbox (`docker exec git …` at the same path), so whatever the agent put in
`.git/config` or `.gitattributes` runs in its VM, not on the host, and a
stopped sandbox shows no git state. Everything read from a sandbox is
untrusted text; the dashboard cleans it before drawing.
"""

from __future__ import annotations

import json
import re
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from typing import TypeVar

from . import agents, git, guard, image
from . import sandbox as sandbox_mod
from .config import Config
from .docker import Docker
from .errors import KbxError
from .modules import Resolved
from .paths import Paths
from .sandbox import LABEL_WORKSPACE, Sandbox

LIST_INTERVAL = 2.0  # docker ps + inspect, the guard's files
SLOW_INTERVAL = 10.0  # sessions (one exec per sandbox) and docker stats
DETAIL_INTERVAL = 15.0  # health, git and agents of the selected sandbox
IMAGE_INTERVAL = 60.0  # image id and drift
RECENT_COMMITS = 5
NPM = "https://registry.npmjs.org"
# The native installers have no version endpoint; npm publishes the same versions.
NPM_PACKAGES = {"claude": "@anthropic-ai/claude-code", "codex": "@openai/codex", "pi": agents.PI_PACKAGE}
LIVE = ("running", "paused", "restarting")
T = TypeVar("T")

# Mount mode, inside the sandbox: commits since the start, recent subjects, dirty paths.
MOUNT_GIT = r"""
git rev-list --count --since="$1" HEAD || exit 1
echo --
git log -n "$2" --since="$1" --format='%h %s' HEAD
echo --
git --no-optional-locks status --porcelain | wc -l
"""


@dataclass(frozen=True)
class Row:
    name: str
    project_dir: Path
    state: str
    mode: str
    started_at: str = ""
    image_id: str = ""
    guard: str = "-"  # alert, unguarded, watching, off, -
    sessions: tuple[str, ...] | None = None  # None: not asked yet (or not running)
    activity: tuple[str, ...] = ()  # per session: working, waiting, done, or ""
    cpu: str = ""
    memory: str = ""
    orphan: bool = False  # the project directory is gone

    @property
    def sandbox(self) -> Sandbox:
        sb = sandbox_mod.for_project(self.project_dir, self.mode)
        return sb if sb.name == self.name else replace(sb, name=self.name)

    @property
    def running(self) -> bool:
        return self.state == "running"


@dataclass(frozen=True)
class Daemon:
    """`codex app-server daemon status`: its exit status and first line, as is."""

    ok: bool
    line: str
    version: str | None


@dataclass
class Details:
    at: float  # time.time() of the collection
    state: str
    health: list[str] | None = None  # problems; [] when all is well; None: no ready record
    git: list[str] = field(default_factory=list[str])
    versions: dict[str, str | None] = field(default_factory=dict[str, str | None])
    logins: dict[str, tuple[bool | None, str]] = field(default_factory=dict[str, tuple[bool | None, str]])
    daemon: Daemon | None = None
    errors: list[str] = field(default_factory=list[str])


@dataclass(frozen=True)
class Snapshot:
    rows: list[Row]
    details: dict[str, Details]
    reports: dict[str, list[str]]
    image_id: str
    drift: list[str]
    latest: dict[str, str | None] | None
    latest_error: str
    error: str
    listed_at: float
    busy: frozenset[str]  # sandboxes whose details are being collected


# --- rows ---


def guard_state(paths: Paths, config: Config, name: str, state: str, mode: str) -> str:
    if guard.Guard(paths, name).alert_file.exists():
        return "alert"  # also after the sandbox stopped: the seal found something
    if mode != "mount":
        return "-"
    if not config.workspace.guard:
        return "off"
    if state not in LIVE:
        return "-"
    return "watching" if guard.running(paths, name) else "unguarded"


def list_rows(docker: Docker, paths: Paths, config: Config) -> list[Row]:
    listed = sandbox_mod.list_sandboxes(docker)
    info: dict[str, dict[str, object]] = {}
    for item in docker.inspect_many("container", [entry["name"] for entry in listed]):
        info[str(item.get("Name") or "").lstrip("/")] = item
    rows: list[Row] = []
    for entry in listed:
        data = info.get(entry["name"], {})
        config_part = data.get("Config")
        labels = config_part.get("Labels") if isinstance(config_part, dict) else None
        labels = labels if isinstance(labels, dict) else {}  # pyright: ignore[reportUnknownVariableType]
        state_part = data.get("State")
        started = state_part.get("StartedAt") if isinstance(state_part, dict) else None
        mode = str(labels.get(LABEL_WORKSPACE) or "clone")  # pyright: ignore[reportUnknownMemberType]
        project = entry["project"]
        rows.append(
            Row(
                name=entry["name"],
                project_dir=Path(project) if project else Path("/"),
                state=entry["state"],
                mode=mode,
                started_at=str(started or ""),  # pyright: ignore[reportUnknownArgumentType]
                image_id=str(data.get("Image") or ""),
                guard=guard_state(paths, config, entry["name"], entry["state"], mode),
                orphan=not project or not Path(project).is_dir(),
            )
        )
    return sorted(rows, key=lambda row: (str(row.project_dir), row.name))


def stats(docker: Docker, names: Sequence[str]) -> dict[str, tuple[str, str]]:
    """CPU and memory in use, by container name. Empty if the runtime reports none."""
    if not names:
        return {}
    result = docker.run(
        ["stats", "--no-stream", "--format", "{{.Name}}\t{{.CPUPerc}}\t{{.MemUsage}}", *names],
        check=False,
        timeout=30,
    )
    found: dict[str, tuple[str, str]] = {}
    for line in result.stdout.decode("utf-8", "replace").splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            found[parts[0]] = (parts[1].strip(), parts[2].split("/")[0].strip())
    return found


def stale(row: Row, config: Config, image_id: str) -> list[str]:
    """Why this sandbox differs from what a fresh one would be."""
    notes: list[str] = []
    if row.orphan:
        notes.append(f"its project directory {row.project_dir} is gone")
    if row.mode != config.workspace.mode:
        notes.append(f"created in {row.mode} mode, the config says {config.workspace.mode}; `kbx recreate` switches")
    if image_id and row.image_id and row.image_id != image_id:
        notes.append("created from an older image than the current one; `kbx recreate` uses the new one")
    return notes


def image_state(docker: Docker, paths: Paths, config: Config, resolved: list[Resolved]) -> tuple[str, list[str]]:
    data = docker.inspect("image", config.image.name)
    if data is None:
        return "", [f"image {config.image.name!r} not found; run `kbx build`"]
    return str(data.get("Id") or ""), image.drift(docker, paths, config, resolved)


# --- details of one sandbox ---


def git_since(started_at: str) -> str | None:
    """Docker's StartedAt (nanoseconds, Z) as a date git parses, or None if never started."""
    match = re.match(r"(\d{4}-\d\d-\d\d)T(\d\d:\d\d:\d\d)", started_at)
    if not match or match.group(1).startswith("0001"):
        return None
    try:
        moment = datetime.strptime(f"{match.group(1)} {match.group(2)}", "%Y-%m-%d %H:%M:%S").replace(tzinfo=UTC)
    except ValueError:
        return None
    return moment.strftime("%Y-%m-%d %H:%M:%S +0000")


def mount_git(docker: Docker, sb: Sandbox, started_at: str) -> list[str]:
    since = git_since(started_at)
    if since is None:
        return ["no start time; git state unknown"]
    result = docker.exec(
        sb.name,
        ["sh", "-c", MOUNT_GIT, "sh", since, str(RECENT_COMMITS)],
        workdir=str(sb.project_dir),
        check=False,
        timeout=30,
    )
    output = result.stdout.decode("utf-8", "replace").splitlines()
    # Log lines start with a hash, so a bare "--" is always a separator.
    marks = [index for index, line in enumerate(output) if line == "--"]
    if result.returncode != 0 or len(marks) != 2:
        detail = result.stderr.decode("utf-8", "replace").strip().splitlines()
        return [f"git in the sandbox failed: {detail[-1] if detail else 'no output'}"]
    count = "".join(output[: marks[0]]).strip()
    log = output[marks[0] + 1 : marks[1]]
    dirty = "".join(output[marks[1] + 1 :]).strip()
    commits = int(count) if count.isdigit() else 0
    lines = [f"{commits} commit(s) on HEAD since the sandbox started"]
    lines += [f"  {line}" for line in log[:RECENT_COMMITS]]
    if commits > RECENT_COMMITS:
        lines.append(f"  … {commits - RECENT_COMMITS} more (`git log` on the host after review)")
    paths_dirty = int(dirty) if dirty.isdigit() else 0
    lines.append(f"{paths_dirty} uncommitted path(s) in the checkout" if paths_dirty else "no uncommitted changes")
    return lines


def clone_git(docker: Docker, sb: Sandbox) -> list[str]:
    if not git.is_seeded(docker, sb):
        return ["the sandbox clone is not seeded yet (`kbx start` seeds it)"]
    pending = git.unfetched(docker, sb)
    lines = [f"unfetched: {item.branch} ({item.commits} commit(s)); f fetches" for item in pending]
    if not pending:
        lines.append("every sandbox branch is on the host")
    lines += git.local_changes(docker, sb)
    return lines


def health(docker: Docker, sb: Sandbox) -> list[str] | None:
    ready = sandbox_mod.ready_status(docker, sb)
    if ready is None:
        return None
    problems: list[str] = []
    for name, result in sorted((ready.get("seed") or {}).items()):
        if not result.get("ok", False):
            problems.append(f"seed failed for module {name}: {result.get('error')}")
    for name, result in sorted((ready.get("start") or {}).items()):
        if not result.get("ok", False):
            problems.append(f"start.sh failed for module {name} (exit {result.get('status')})")
    for name, result in sorted((ready.get("services") or {}).items()):
        if result.get("state") == "failed":
            problems.append(f"service {name} failed (last exit {result.get('last_exit')})")
    return problems


def codex_daemon(docker: Docker, sb: Sandbox) -> Daemon:
    result = docker.exec(sb.name, ["codex", "app-server", "daemon", "status"], check=False, timeout=30)
    text = (result.stdout + result.stderr).decode("utf-8", "replace")
    first = next((line.strip() for line in text.splitlines() if line.strip()), "no output")
    match = agents.VERSION.search(text)
    return Daemon(result.returncode == 0, first, match.group(0) if match else None)


def collect_details(docker: Docker, row: Row) -> Details:
    details = Details(at=time.time(), state=row.state)
    if not row.running:
        return details
    sb = row.sandbox

    def attempt(label: str, step: Callable[[], T]) -> T | None:
        try:
            return step()
        except (KbxError, OSError, ValueError) as exc:
            details.errors.append(f"{label}: {exc}")
            return None

    details.health = attempt("health", lambda: health(docker, sb))
    if sb.mode == "mount":
        details.git = attempt("git", lambda: mount_git(docker, sb, row.started_at)) or []
    else:
        details.git = attempt("git", lambda: clone_git(docker, sb)) or []
    for agent in agents.AGENTS.values():
        details.versions[agent.name] = attempt(agent.name, lambda agent=agent: agents.version(docker, sb, agent))
    details.logins = attempt("logins", lambda: agents.login_status(docker, sb)) or {}
    if details.versions.get("codex"):
        details.daemon = attempt("codex daemon", lambda: codex_daemon(docker, sb))
    return details


def latest_versions(timeout: float = 10.0) -> dict[str, str | None]:
    """The newest published versions, from the npm registry (network, host side)."""
    found: dict[str, str | None] = {}
    for name, package in NPM_PACKAGES.items():
        url = f"{NPM}/{urllib.parse.quote(package, safe='@')}/latest"
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response:  # noqa: S310 (fixed https URL)
                data = json.loads(response.read(1 << 20))
        except (OSError, ValueError):
            found[name] = None
            continue
        version = str(data.get("version", "")) if isinstance(data, dict) else ""
        match = agents.VERSION.fullmatch(version)
        found[name] = match.group(0) if match else None
    return found


# --- background refresh ---


class Monitor:
    """Keeps a Snapshot current from three threads, so the UI never waits for docker.

    - list: every LIST_INTERVAL, rows from docker ps/inspect and the guard's files;
    - slow: every SLOW_INTERVAL, sessions and stats of running sandboxes;
    - detail: the selected sandbox, when selected and every DETAIL_INTERVAL after.
    """

    def __init__(self, docker: Docker, paths: Paths, config: Config, resolved: list[Resolved]) -> None:
        self.docker = docker
        self.paths = paths
        self.config = config
        self.resolved = resolved
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._active = threading.Event()
        self._active.set()
        self._wakes = {name: threading.Event() for name in ("list", "slow", "detail")}
        self._rows: list[Row] = []
        self._sessions: dict[str, dict[str, str]] = {}
        self._stats: dict[str, tuple[str, str]] = {}
        self._details: dict[str, Details] = {}
        self._reports: dict[str, list[str]] = {}
        self._image_id = ""
        self._drift: list[str] = []
        self._image_at = 0.0
        self._latest: dict[str, str | None] | None = None
        self._latest_error = ""
        self._want_latest = False
        self._error = ""
        self._listed_at = 0.0
        self._selected: str | None = None
        self._stale: set[str] = set()
        self._busy: set[str] = set()
        self._threads: list[threading.Thread] = []

    # --- control, from the UI thread ---

    def start(self) -> None:
        for target in (self._list_loop, self._slow_loop, self._detail_loop):
            thread = threading.Thread(target=target, daemon=True)
            thread.start()
            self._threads.append(thread)

    def stop(self) -> None:
        self._stop.set()
        self._active.set()
        for event in self._wakes.values():
            event.set()

    def pause(self) -> None:
        """While a command has the terminal: no polling."""
        self._active.clear()

    def resume(self, changed: str | None = None) -> None:
        self.refresh(changed)
        self._active.set()

    def refresh(self, name: str | None = None) -> None:
        with self._lock:
            if name is not None:
                self._stale.add(name)
            elif self._selected is not None:
                self._stale.add(self._selected)
            self._image_at = 0.0
        for event in self._wakes.values():
            event.set()

    def select(self, name: str | None) -> None:
        with self._lock:
            changed = name != self._selected
            self._selected = name
        if changed:
            self._wakes["detail"].set()

    def check_latest(self) -> None:
        with self._lock:
            self._want_latest = True
            self._latest_error = "checking…"
        self._wakes["detail"].set()

    def snapshot(self) -> Snapshot:
        with self._lock:
            rows: list[Row] = []
            for row in self._rows:
                live = self._sessions.get(row.name) if row.running else None
                rows.append(
                    replace(
                        row,
                        sessions=tuple(live) if live is not None else None,
                        activity=tuple(live.values()) if live else (),
                        cpu=self._stats.get(row.name, ("", ""))[0] if row.running else "",
                        memory=self._stats.get(row.name, ("", ""))[1] if row.running else "",
                    )
                )
            return Snapshot(
                rows=rows,
                details=dict(self._details),
                reports=dict(self._reports),
                image_id=self._image_id,
                drift=list(self._drift),
                latest=dict(self._latest) if self._latest is not None else None,
                latest_error=self._latest_error,
                error=self._error,
                listed_at=self._listed_at,
                busy=frozenset(self._busy),
            )

    # --- threads ---

    def _wait(self, name: str, timeout: float) -> bool:
        """Sleep until the interval passes or a refresh; False once stopped."""
        event = self._wakes[name]
        event.wait(timeout)
        event.clear()
        while not self._active.is_set() and not self._stop.is_set():
            self._active.wait(1.0)
        return not self._stop.is_set()

    def _list_loop(self) -> None:
        while not self._stop.is_set():
            self._list_once()
            if not self._wait("list", LIST_INTERVAL):
                return

    def _list_once(self) -> None:
        try:
            if time.monotonic() - self._image_at > IMAGE_INTERVAL:
                image_id, drift = image_state(self.docker, self.paths, self.config, self.resolved)
                with self._lock:
                    self._image_id, self._drift, self._image_at = image_id, drift, time.monotonic()
            rows = list_rows(self.docker, self.paths, self.config)
            reports: dict[str, list[str]] = {}
            for row in rows:
                if row.guard == "alert":
                    reports[row.name] = guard.report(self.paths, row.sandbox)
            with self._lock:
                self._rows, self._reports, self._error = rows, reports, ""
                self._listed_at = time.time()
        except (KbxError, OSError, ValueError) as exc:
            with self._lock:
                self._error = str(exc)

    def _slow_loop(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                running = [row.name for row in self._rows if row.running]
            sessions: dict[str, dict[str, str]] = {}
            try:
                for name in running:
                    sessions[name] = sandbox_mod.session_states(self.docker, name)
                found = stats(self.docker, running)
            except (KbxError, OSError, ValueError):
                found = {}
            with self._lock:
                self._sessions, self._stats = sessions, found
            # The first pass may have run before the first listing.
            if not self._wait("slow", SLOW_INTERVAL if running or self._listed_at else 0.5):
                return

    def _detail_loop(self) -> None:
        while not self._stop.is_set():
            self._detail_once()
            if not self._wait("detail", 1.0):
                return

    def _detail_once(self) -> None:
        with self._lock:
            name = self._selected
            row = next((r for r in self._rows if r.name == name), None)
            current = self._details.get(name) if name else None
            forced = name in self._stale
            want_latest = self._want_latest
            self._want_latest = False
        if want_latest:
            latest = latest_versions()
            with self._lock:
                self._latest = latest
                self._latest_error = "" if any(latest.values()) else "could not reach the npm registry"
        if row is None:
            return
        due = (
            forced
            or current is None
            or current.state != row.state
            or (row.running and time.time() - current.at > DETAIL_INTERVAL)
        )
        if not due:
            return
        with self._lock:
            self._stale.discard(row.name)
            self._busy.add(row.name)
        try:
            details = collect_details(self.docker, row)
        finally:
            with self._lock:
                self._busy.discard(row.name)
        with self._lock:
            self._details[row.name] = details
