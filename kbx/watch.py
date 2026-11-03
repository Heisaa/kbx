"""Per-sandbox host watcher: agent notifications and the idle stop.

`kbx _watch NAME` runs while the sandbox runs, started by the launcher (like
the guard). It follows `kbx-notify follow` in the sandbox over one long-lived
`docker exec`, so the sandbox never connects to the host, and:

- sends a desktop notification when an agent finishes a turn or waits for an
  approval ([launcher] notify: only while no terminal is attached to that
  session, always, or off);
- stops the sandbox as soon as its last agent session closes (once no
  interactive shell is left either), or after [launcher] idle_stop with no
  agent session and no shell when none was open, the same way `kbx stop`
  does (the guard seals as usual). idle_stop "off" turns both off.

Everything read from the sandbox is untrusted: fields are type-checked,
cleaned of control characters, cut short, and escaped for the notification
server's markup.
"""

from __future__ import annotations

import json
import os
import select
import shutil
import subprocess
import sys
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import IO, Any

from . import agents, guard
from . import config as config_mod
from . import sandbox as sandbox_mod
from .config import Config
from .docker import Docker
from .errors import KbxError
from .paths import Paths
from .sandbox import Sandbox
from .text import clean

CHECK_INTERVAL = 30.0  # container state, shells, idle
READ_TIMEOUT = 5.0
RESTART_DELAY = 5.0
QUICK_EXIT = 10.0  # a follow that ends this fast did not work
MAX_QUICK_EXITS = 3
POLL_INTERVAL = 60.0  # sessions, when the image has no kbx-notify
REPEAT_WINDOW = 60.0  # the same notification again only after this
MAX_PER_MINUTE = 6  # per sandbox: a chatty (or hostile) agent cannot flood the desktop
MAX_TITLE = 80
MAX_BODY = 200
QUIET_STOP = 30 * 60  # an idle stop sooner than this follows a close: no notification
SETTLE = 2.0  # after the last session closed: let its `docker exec` end, or a new launch show up
STATES = ("working", "waiting", "done")
LIVE = ("running", "paused", "restarting")


def log(message: str) -> None:
    print(f"{datetime.now().isoformat(timespec='seconds')} {message}", flush=True)


# --- lifecycle ---


def _pid_file(paths: Paths, name: str) -> Path:
    return paths.runtime_dir / f"watch-{name}.pid"


def running(paths: Paths, name: str) -> bool:
    try:
        pid = int(_pid_file(paths, name).read_text().strip())
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes().split(b"\0")
    except (OSError, ValueError):
        return False
    return b"_watch" in cmdline and name.encode() in cmdline


def wanted(config: Config) -> bool:
    return config.launcher.notify != "off" or config.launcher.idle_stop > 0


def ensure(paths: Paths, sandbox: Sandbox, config: Config, entry: Path) -> None:
    """The sandbox is running: make sure a watcher follows it."""
    if not wanted(config) or running(paths, sandbox.name):
        return
    paths.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    paths.log_dir.mkdir(parents=True, exist_ok=True)
    with open(paths.log_dir / f"watch-{sandbox.name}.log", "ab") as out:
        process = subprocess.Popen(
            [sys.executable, str(entry), "_watch", sandbox.name],
            stdin=subprocess.DEVNULL,
            stdout=out,
            stderr=out,
            start_new_session=True,
            close_fds=True,
        )
    _pid_file(paths, sandbox.name).write_text(f"{process.pid}\n")


# --- what the sandbox says ---


@dataclass(frozen=True)
class Event:
    agent: str
    state: str
    message: str
    attached: bool
    session: str = ""  # the session key; "" means the agent's own (a project sandbox)

    @property
    def key(self) -> str:
        return self.session or self.agent


def parse_event(data: Mapping[str, Any]) -> Event | None:
    agent, state = data.get("agent"), data.get("state")
    if agent not in agents.AGENTS or state not in STATES:
        return None
    key = data.get("session") or agent
    if not sandbox_mod.valid_session(key) or sandbox_mod.session_agent(str(key)) != agent:
        return None
    message = data.get("message")
    return Event(
        agent=str(agent),
        state=str(state),
        message=clean(message)[:MAX_BODY] if isinstance(message, str) else "",
        attached=data.get("attached") is True,
        session=str(key) if key != agent else "",
    )


def parse_sessions(data: Mapping[str, Any]) -> dict[str, dict[str, Any]]:
    sessions = data.get("sessions")
    if not isinstance(sessions, dict):
        return {}
    return {str(k): v for k, v in sessions.items() if sandbox_mod.valid_session(k) and isinstance(v, dict)}  # pyright: ignore[reportUnknownVariableType]


# --- notifications ---


def _markup(text: str) -> str:
    """Notification servers may read the body as markup."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def notification(event: Event, project: Path) -> tuple[str, str]:
    """`project`: the sandbox's; a profile session names its own project instead."""
    what = "needs you" if event.state == "waiting" else "is done"
    where = sandbox_mod.session_label(event.session).partition("@")[2] if event.session else project.name
    title = clean(f"{event.agent} {what} · {where}")[:MAX_TITLE]
    default = "waiting for your answer" if event.state == "waiting" else "finished its turn"
    return title, _markup(event.message or default)


def send(title: str, body: str, urgency: str = "normal") -> bool:
    if not shutil.which("notify-send"):
        return False
    # `--` so that text from the sandbox is never read as an option.
    result = subprocess.run(
        ["notify-send", "-a", "kbx", "-u", urgency, "--", title, body], capture_output=True, timeout=10, check=False
    )
    return result.returncode == 0


@dataclass
class Notifier:
    mode: str  # detached, always, off
    project: Path
    recent: dict[str, tuple[str, str, float]] = field(default_factory=dict[str, tuple[str, str, float]])
    sent_at: list[float] = field(default_factory=list[float])
    warned: bool = False

    def handle(self, event: Event, now: float) -> bool:
        """Send a notification if this event deserves one. True if sent."""
        if self.mode == "off" or event.state == "working":
            return False
        if self.mode == "detached" and event.attached:
            return False
        last = self.recent.get(event.key)
        if last and last[:2] == (event.state, event.message) and now - last[2] < REPEAT_WINDOW:
            return False
        self.sent_at = [at for at in self.sent_at if now - at < 60]
        if len(self.sent_at) >= MAX_PER_MINUTE:
            return False
        self.recent[event.key] = (event.state, event.message, now)
        self.sent_at.append(now)
        sent = send(*notification(event, self.project))
        if not sent and not self.warned:
            self.warned = True
            log("notify-send is missing or failed; no desktop notifications")
        return sent


# --- idleness ---


def interactive_execs(name: str, proc: Path = Path("/proc")) -> int:
    """Host `docker exec -t` processes into this container: attaches and shells."""
    count = 0
    for entry in proc.iterdir():
        if not entry.name.isdigit():
            continue
        try:
            argv = (entry / "cmdline").read_bytes().split(b"\0")
        except OSError:
            continue
        args = [arg.decode("utf-8", "replace") for arg in argv if arg]
        if len(args) < 3 or os.path.basename(args[0]) != "docker" or "exec" not in args or name not in args:
            continue
        before = args[: args.index(name)]
        if any(
            arg in ("-t", "--tty") or (arg.startswith("-") and not arg.startswith("--") and "t" in arg)
            for arg in before
        ):
            count += 1
    return count


@dataclass
class Idle:
    limit: float  # seconds; 0 = never stop
    since: float  # monotonic time of the last activity
    sessions: dict[str, dict[str, Any]] | None = None  # None: not known yet
    closed: bool = False  # the last session closed and none opened since: stop without waiting

    def active(self, now: float) -> None:
        self.since = now

    def update(self, sessions: dict[str, dict[str, Any]]) -> bool:
        """New session list. True if that closed the last session."""
        last_closed = bool(self.sessions) and not sessions
        self.sessions = sessions
        self.closed = last_closed or (self.closed and not sessions)
        return last_closed

    def due(self, now: float, shells: int) -> bool:
        if self.sessions is None or self.sessions or shells:
            self.since = now
        if self.limit <= 0:
            return False
        return (self.closed and not self.sessions and not shells) or now - self.since >= self.limit


# --- the loop ---


class Watcher:
    def __init__(self, paths: Paths, name: str, docker: Docker, config: Config) -> None:
        self.paths = paths
        self.name = name
        self.docker = docker
        self.config = config
        self.sandbox: Sandbox = sandbox_mod.from_labels(name, sandbox_mod.labels(docker, name))
        if self.sandbox.profile in config.profiles:
            self.config = config = config.for_profile(self.sandbox.profile)  # its own idle_stop
        self.project = self.sandbox.project_dir
        self.mode = self.sandbox.mode
        self.notifier = Notifier(config.launcher.notify, self.project)
        self.idle = Idle(float(config.launcher.idle_stop), time.monotonic())
        self.next_check = time.monotonic() + CHECK_INTERVAL

    @property
    def title(self) -> str:
        return f"profile {self.sandbox.profile}" if self.sandbox.profile else self.project.name

    def state(self) -> str:
        data = self.docker.inspect("container", self.name)
        return str(((data or {}).get("State") or {}).get("Status", "gone"))

    def line(self, raw: bytes) -> None:
        try:
            data = json.loads(raw)
        except ValueError:
            return
        if not isinstance(data, dict):
            return
        now = time.monotonic()
        if data.get("type") == "sessions":
            if self.idle.update(parse_sessions(data)) and self.idle.limit > 0:  # pyright: ignore[reportUnknownArgumentType]
                self.next_check = min(self.next_check, now + SETTLE)
        elif data.get("type") == "event":
            event = parse_event(data)  # pyright: ignore[reportUnknownArgumentType]
            if event is not None:
                self.idle.active(now)
                if self.notifier.handle(event, time.time()):
                    log(f"notified: {event.key} {event.state}")

    def check(self) -> bool:
        """Every CHECK_INTERVAL. False when the watcher should end."""
        status = self.state()
        now = time.monotonic()
        if status not in LIVE:
            log(f"sandbox is {status}; exiting")
            return False
        if status != "running":
            self.idle.active(now)  # paused by the guard: someone has to look first
            return True
        if not self.idle.due(now, interactive_execs(self.name)):
            return True
        return not self.stop_idle()

    def stop_idle(self) -> bool:
        """True if the sandbox was stopped."""
        if guard.alerted(self.paths, self.name):
            return False
        closed = self.idle.closed
        if closed:
            live = sandbox_mod.sessions(self.docker, self.name)
            if live:  # a launch opened one since the last report: not idle
                self.idle.update({key: {} for key in live})
                self.idle.active(time.monotonic())
                return False
        minutes = round(self.idle.limit / 60)
        log("last session closed; stopping" if closed else f"idle for {minutes} min (no agent session, no shell); stopping")
        try:
            sandbox_mod.stop(self.docker, self.sandbox)
        except KbxError as exc:
            log(f"could not stop: {exc}")
            self.idle.active(time.monotonic())
            return False
        if self.mode == "mount" and self.config.workspace.guard:
            actions = guard.seal(self.paths, self.name)
            if actions:
                log(f"guard: {guard.summary(actions)}")
        if not closed and self.config.launcher.notify != "off" and self.idle.limit >= QUIET_STOP:
            send(
                clean(f"kbx stopped {self.title}")[:MAX_TITLE],
                f"Idle for {minutes} min. It starts again at the next kbx command.",
            )
        return True

    def follow(self) -> bool:
        """Read one `kbx-notify follow` until it ends. False when the watcher should end."""
        args = self.docker.exec_args(self.name, ["kbx-notify", "follow"])
        process = subprocess.Popen(
            [self.docker.binary_path(), *args],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
        )
        assert process.stdout is not None
        try:
            return self._read(process.stdout)
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()

    def _read(self, stream: IO[bytes]) -> bool:
        buffer = b""
        self.next_check = time.monotonic() + CHECK_INTERVAL
        fd = stream.fileno()
        while True:
            timeout = min(READ_TIMEOUT, max(0.0, self.next_check - time.monotonic()))
            ready, _, _ = select.select([fd], [], [], timeout)
            if ready:
                chunk = os.read(fd, 65536)
                if not chunk:
                    return True  # ended; the caller decides
                buffer += chunk
                *lines, buffer = buffer.split(b"\n")
                for raw in lines:
                    self.line(raw)
                buffer = buffer[-65536:]  # a line longer than this is not an event
            if time.monotonic() >= self.next_check:
                self.next_check = time.monotonic() + CHECK_INTERVAL
                if not self.check():
                    return False

    def poll(self) -> None:
        """No kbx-notify in the image: sessions every POLL_INTERVAL, for the idle stop only."""
        while True:
            self.idle.update({name: {} for name in sandbox_mod.sessions(self.docker, self.name)})
            if not self.check():
                return
            time.sleep(POLL_INTERVAL)

    def run(self) -> int:
        quick = 0
        while True:
            started = time.monotonic()
            if not self.follow():
                return 0
            if self.state() not in LIVE:
                log("sandbox stopped; exiting")
                return 0
            quick = quick + 1 if time.monotonic() - started < QUICK_EXIT else 0
            if quick >= MAX_QUICK_EXITS:
                log("kbx-notify follow keeps failing (an image from before it? `kbx build`); idle stop only")
                self.poll()
                return 0
            time.sleep(RESTART_DELAY)


def run(paths: Paths, name: str) -> int:
    """`kbx _watch NAME`."""
    watcher = Watcher(paths, name, Docker(), config_mod.load(paths, os.environ))
    config = watcher.config
    limit = f"{config.launcher.idle_stop // 60} min" if config.launcher.idle_stop else "off"
    log(f"watching {name}: notify {config.launcher.notify}, idle stop {limit}")
    try:
        return watcher.run()
    finally:
        try:
            if _pid_file(paths, name).read_text().strip() == str(os.getpid()):
                _pid_file(paths, name).unlink()
        except OSError:
            pass
