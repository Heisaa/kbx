"""kbx-init: runs as root under tini on every container start (every Kata VM boot).

1. Copy the image's home template into a new, empty home volume. In mount
   mode, give `agent` the host user's uid/gid (files in the shared checkout
   keep their owner).
2. Point DNS at the configured resolvers if Docker's embedded one is unreachable.
3. Mount inner Docker's storage (ext4 image file, loop-mounted).
4. Link skills, run kbx-seed as agent, run each module's start.sh.
5. Start and supervise dockerd and module services.
6. Write /run/kbx/ready, bound to this boot, then keep supervising.

`kbx-init status` prints the ready record (exit 1 if not ready for this boot);
`kbx-init run-start MODULE` reruns one module's start.sh as the calling user.
"""

from __future__ import annotations

import collections
import grp
import json
import os
import pwd
import select
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import IO, Any

STAGE = Path("/opt/kbx/stage")
HOME = Path("/home/agent")
TEMPLATE = Path("/opt/kbx/home-template")
TEMPLATE_MARKER = ".local/state/kbx/home-template"
RUN = Path("/run/kbx")
READY = RUN / "ready"
SERVICES = RUN / "services.json"
SESSIONS = RUN / "sessions"
LOG = Path("/var/log/kbx-startup.log")
SERVICE_LOGS = Path("/var/log/kbx")
DOCKER_STORE = Path("/var/lib/kbx-docker")
DOCKER_ROOT = Path("/var/lib/docker")
SKILL_LINKS = (".agents/skills", ".claude/skills")
PATH = "/home/agent/.local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
START_TIMEOUT = 300
MAX_RESTARTS = 5  # per minute, then the service is marked failed

_log_handle: IO[str] | None = None


def log(message: str) -> None:
    line = f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n"
    sys.stderr.write(line)
    sys.stderr.flush()
    if _log_handle is not None:
        _log_handle.write(line)
        _log_handle.flush()


def boot_id() -> str:
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return "unknown"


def write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".tmp-")
    with os.fdopen(fd, "w") as handle:
        json.dump(data, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.chmod(temp, 0o644)
    os.replace(temp, path)


# --- users and environment ---


@dataclass(frozen=True)
class User:
    name: str
    uid: int
    gid: int
    groups: tuple[int, ...]
    home: str

    @classmethod
    def lookup(cls, name: str) -> User:
        entry = pwd.getpwnam(name)
        groups = tuple(os.getgrouplist(name, entry.pw_gid))
        return cls(name, entry.pw_uid, entry.pw_gid, groups, entry.pw_dir)


def base_env(user: User, config: Mapping[str, Any], extra: Mapping[str, str] | None = None) -> dict[str, str]:
    env = {
        "HOME": user.home,
        "USER": user.name,
        "LOGNAME": user.name,
        "PATH": PATH,
        "LANG": os.environ.get("LANG", "C.UTF-8"),
        "DISPLAY": ":0",
    }
    if "SANDBOX_NAME" in os.environ:
        env["SANDBOX_NAME"] = os.environ["SANDBOX_NAME"]
    env.update(config.get("env") or {})
    env.update(extra or {})
    return env


def run_as(
    user: User, argv: Sequence[str], env: Mapping[str, str], **kwargs: Any
) -> subprocess.CompletedProcess[bytes]:
    if os.getuid() == 0 and user.uid != 0:
        kwargs.update(user=user.uid, group=user.gid, extra_groups=list(user.groups))
    return subprocess.run(list(argv), env=dict(env), check=False, **kwargs)


def chown_agent(path: Path, agent: User) -> None:
    os.lchown(path, agent.uid, agent.gid)


def makedirs_agent(path: Path, agent: User) -> None:
    missing: list[Path] = []
    current = path
    while not current.exists() and current != current.parent:
        missing.append(current)
        current = current.parent
    for directory in reversed(missing):
        directory.mkdir()
        chown_agent(directory, agent)


# --- boot steps ---


def remap_agent(env: Mapping[str, str]) -> None:
    """Give `agent` the host user's ids (KBX_HOST_UID/GID, set in mount mode)."""
    try:
        uid, gid = int(env["KBX_HOST_UID"]), int(env["KBX_HOST_GID"])
    except (KeyError, ValueError):
        return
    agent = pwd.getpwnam("agent")
    old_uid, old_gid = agent.pw_uid, agent.pw_gid
    if (uid, gid) == (old_uid, old_gid):
        return
    if gid != old_gid:
        try:
            owner = grp.getgrgid(gid).gr_name
        except KeyError:
            owner = None
        if owner is not None:
            log(f"ids: gid {gid} belongs to group {owner} in the image; keeping agent's gid {old_gid}")
            gid = old_gid
        else:
            subprocess.run(["groupmod", "-g", str(gid), "agent"], check=True, capture_output=True)
    if uid != old_uid:
        try:
            owner = pwd.getpwuid(uid).pw_name
        except KeyError:
            owner = None
        if owner is not None:
            log(f"ids: uid {uid} belongs to {owner} in the image; keeping agent's uid {old_uid}")
            uid = old_uid
        else:
            subprocess.run(["usermod", "-u", str(uid), "-g", str(gid), "agent"], check=True, capture_output=True)
    if (uid, gid) == (old_uid, old_gid):
        return
    # usermod only fixes the home directory's top; the volume keeps the old ids.
    subprocess.run(["chown", "-R", "-h", f"--from={old_uid}:{old_gid}", f"{uid}:{gid}", str(HOME)], check=False)
    subprocess.run(["chown", "-R", "-h", f"--from={old_uid}", str(uid), str(HOME)], check=False)
    subprocess.run(["chown", "-R", "-h", f"--from=:{old_gid}", f":{gid}", str(HOME)], check=False)
    log(f"ids: agent is now {uid}:{gid} (was {old_uid}:{old_gid}), like the host user")


def copy_home_template(agent: User) -> None:
    """Seed an empty home volume from the image (independent of Docker copy-up)."""
    if (HOME / TEMPLATE_MARKER).exists() or not TEMPLATE.is_dir():
        return
    log(f"home: copying the image's home template into {HOME}")
    result = subprocess.run(
        ["cp", "-a", "--update=none", f"{TEMPLATE}/.", f"{HOME}/"], capture_output=True, text=True, check=False
    )
    if result.returncode != 0:
        log(f"home: copy failed: {result.stderr.strip()}")
        return
    os.chown(HOME, agent.uid, agent.gid)


def fix_dns(servers: Sequence[str]) -> None:
    """Use the configured resolvers instead of Docker's embedded one (127.0.0.11).

    Under Kata it is unreachable from the guest (risk R3). Under runc it lives
    in the host's Docker daemon, and the inner dockerd's NAT rules break it.
    """
    path = Path("/etc/resolv.conf")
    try:
        text = path.read_text()
    except OSError:
        return
    if "127.0.0.11" not in text or not servers:
        return
    kept = [line for line in text.splitlines() if line.startswith(("search ", "options ")) and "ndots:0" not in line]
    body = "".join(f"nameserver {server}\n" for server in servers) + "".join(f"{line}\n" for line in kept)
    log(f"dns: using {', '.join(servers)}")
    try:
        with path.open("w") as handle:  # in place: resolv.conf is itself a bind mount
            handle.write(body)
    except OSError:
        replacement = RUN / "resolv.conf"
        replacement.write_text(body)
        subprocess.run(["mount", "--bind", str(replacement), str(path)], check=False)


def _mounted(target: Path) -> bool:
    return _mount_options(target) is not None


def _mount_options(target: Path) -> list[str] | None:
    try:
        lines = Path("/proc/self/mountinfo").read_text().splitlines()
    except OSError:
        return None
    found: list[str] | None = None
    for line in lines:  # the last mount on a path is the visible one
        fields = line.split()
        if len(fields) > 5 and fields[4] == str(target):
            found = fields[5].split(",")
    return found


def prepare_cgroups() -> None:
    """Let the inner dockerd create cgroups for its containers.

    Without --privileged, Docker mounts /sys/fs/cgroup read-only. The container
    has its own cgroup namespace, so a fresh cgroup2 mount exposes only its own
    subtree, read-write. Then, as Docker's dind script does for cgroup v2: move
    every process out of the root group (a group with processes cannot delegate
    controllers) and enable all controllers for child groups.
    """
    cgroup = Path("/sys/fs/cgroup")
    options = _mount_options(cgroup)
    if options is not None and "ro" in options and (cgroup / "cgroup.controllers").exists():
        subprocess.run(["umount", str(cgroup)], check=False, capture_output=True)
        result = subprocess.run(
            ["mount", "-t", "cgroup2", "-o", "nosuid,nodev,noexec", "cgroup2", str(cgroup)],
            check=False,
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            log(f"cgroups: cannot remount /sys/fs/cgroup read-write: {result.stderr.strip()}")
            return
        log("cgroups: remounted /sys/fs/cgroup read-write (own namespace)")
    controllers = cgroup / "cgroup.controllers"
    if not controllers.exists():
        return  # cgroup v1: nothing to delegate
    init_group = cgroup / "init"
    try:
        init_group.mkdir(exist_ok=True)
        for pid in (cgroup / "cgroup.procs").read_text().split():
            try:
                (init_group / "cgroup.procs").write_text(pid)
            except OSError:
                pass  # kernel threads and exited processes cannot move
        for name in controllers.read_text().split():
            try:
                (cgroup / "cgroup.subtree_control").write_text(f"+{name}")
            except OSError as exc:
                log(f"cgroups: cannot enable controller {name}: {exc.strerror}")
    except OSError as exc:
        log(f"cgroups: nesting setup failed: {exc}")
    subprocess.run(["mount", "--make-rshared", "/"], check=False, capture_output=True)


def _size_bytes(text: str) -> int:
    units = {"k": 1 << 10, "m": 1 << 20, "g": 1 << 30, "t": 1 << 40}
    text = text.strip().lower()
    return int(text[:-1]) * units[text[-1]] if text[-1] in units else int(text)


def ensure_loop_devices(count: int = 16) -> None:
    """Create loop device nodes if /dev lacks them (the container's /dev is a
    tmpfs without them unless privileged). The device cgroup rules kbx sets allow
    exactly these; the guest kernel provides the driver."""
    nodes = [(Path("/dev/loop-control"), stat.S_IFCHR, 10, 237)]
    nodes += [(Path(f"/dev/loop{index}"), stat.S_IFBLK, 7, index) for index in range(count)]
    for path, kind, major, minor in nodes:
        if path.exists():
            continue
        try:
            os.mknod(path, kind | 0o660, os.makedev(major, minor))
        except OSError as exc:
            log(f"docker: cannot create {path}: {exc.strerror}")
            return


def setup_docker_storage(config: Mapping[str, Any]) -> tuple[str, list[str]]:
    """Returns (storage description, extra dockerd args)."""
    docker = config.get("docker") or {}
    DOCKER_STORE.mkdir(parents=True, exist_ok=True)
    volume_args = ["--data-root", str(DOCKER_STORE / "data")]
    if docker.get("storage", "loop") != "loop":
        return "volume", volume_args
    image = DOCKER_STORE / "docker.img"
    size = _size_bytes(str(docker.get("disk", "64g")))
    try:
        if _mounted(DOCKER_ROOT):
            return "loop", []
        if not image.exists():
            log(f"docker: creating a sparse {docker.get('disk', '64g')} ext4 image for inner Docker")
            with image.open("wb") as handle:
                handle.truncate(size)
            subprocess.run(["mkfs.ext4", "-q", "-F", "-m", "0", str(image)], check=True, capture_output=True)
        elif image.stat().st_size < size:
            log(f"docker: growing the inner Docker image to {docker.get('disk')}")
            with image.open("r+b") as handle:
                handle.truncate(size)
            subprocess.run(["e2fsck", "-fy", str(image)], check=False, capture_output=True)
            subprocess.run(["resize2fs", str(image)], check=True, capture_output=True)
        DOCKER_ROOT.mkdir(parents=True, exist_ok=True)
        ensure_loop_devices()
        subprocess.run(["mount", "-o", "loop", str(image), str(DOCKER_ROOT)], check=True, capture_output=True)
        return "loop", []
    except (OSError, subprocess.CalledProcessError) as exc:
        detail = (
            exc.stderr.decode(errors="replace").strip()
            if isinstance(exc, subprocess.CalledProcessError) and exc.stderr
            else str(exc)
        )
        log(f"docker: loop mount failed ({detail}); using the volume directly (overlay2 may not work)")
        return "volume (loop failed)", volume_args


def link_skills(config: Mapping[str, Any], agent: User) -> None:
    target = STAGE / "skills"
    enabled = bool(config.get("skills")) and target.is_dir()
    for rel in SKILL_LINKS:
        path = HOME / rel
        ours = path.is_symlink() and os.readlink(path) == str(target)
        if not enabled:
            if ours:
                path.unlink()
            continue
        if ours:
            continue
        if path.is_symlink() or path.exists():
            log(f"skills: {path} already exists and is not kbx's link; leaving it")
            continue
        makedirs_agent(path.parent, agent)
        path.symlink_to(target)
        chown_agent(path, agent)


def run_seed(agent: User, config: Mapping[str, Any]) -> dict[str, Any]:
    report = RUN / "seed.json"
    report.unlink(missing_ok=True)
    report.touch(mode=0o644)
    chown_agent(report, agent)  # kbx-seed runs as agent; /run/kbx is root's
    env = base_env(agent, config)
    result = run_as(
        agent,
        ["kbx-seed", "--report", str(report)],
        env,
        capture_output=True,
        timeout=START_TIMEOUT,
        cwd=str(HOME),
    )
    output = (result.stdout + result.stderr).decode(errors="replace").strip()
    for line in output.splitlines():
        log(f"seed: {line}")
    try:
        return json.loads(report.read_text())
    except (OSError, ValueError):
        return {"kbx-seed": {"ok": False, "error": f"exit {result.returncode}", "written": []}}


def module_env(item: Mapping[str, Any]) -> dict[str, str]:
    env = dict(item.get("env") or {})
    env.update(item.get("option_env") or {})
    env["KBX_MODULE"] = str(item["name"])
    env["KBX_MODULE_DIR"] = str(STAGE / "modules" / str(item["name"]))
    return env


def run_start(item: Mapping[str, Any], config: Mapping[str, Any], user: User | None) -> int:
    name = str(item["name"])
    script = STAGE / "modules" / name / "start.sh"
    runner = user or User.lookup(pwd.getpwuid(os.getuid()).pw_name)
    env = base_env(runner, config, module_env(item))
    try:
        result = run_as(
            runner,
            [str(script)],
            env,
            cwd=str(script.parent),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=START_TIMEOUT,
        )
    except subprocess.TimeoutExpired:
        log(f"start {name}: timed out after {START_TIMEOUT}s")
        return 124
    except OSError as exc:
        log(f"start {name}: cannot run: {exc}")
        return 126
    for line in result.stdout.decode(errors="replace").splitlines():
        log(f"start {name}: {line}")
    return result.returncode


# --- supervisor ---


@dataclass
class Service:
    name: str
    argv: list[str]
    user: User
    env: dict[str, str]
    cwd: str
    log_path: Path
    process: subprocess.Popen[bytes] | None = None
    state: str = "stopped"
    exits: collections.deque[float] = field(default_factory=collections.deque[float])
    next_start: float = 0.0
    last_status: int | None = None

    def start(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        switch = os.getuid() == 0 and self.user.uid != 0
        with self.log_path.open("ab") as handle:
            try:
                process = subprocess.Popen(
                    self.argv,
                    env=self.env,
                    cwd=self.cwd,
                    stdin=subprocess.DEVNULL,
                    stdout=handle,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    user=self.user.uid if switch else None,
                    group=self.user.gid if switch else None,
                    extra_groups=list(self.user.groups) if switch else None,
                )
            except OSError as exc:
                log(f"service {self.name}: cannot start: {exc}")
                self.process = None
                self.exited(127)
                return
        self.process = process
        self.state = "running"
        log(f"service {self.name}: started (pid {process.pid})")

    def exited(self, status: int) -> None:
        now = time.monotonic()
        self.process = None
        self.last_status = status
        self.exits.append(now)
        while self.exits and now - self.exits[0] > 60:
            self.exits.popleft()
        if len(self.exits) > MAX_RESTARTS:
            self.state = "failed"
            log(f"service {self.name}: exited {status}; more than {MAX_RESTARTS} restarts in a minute, giving up")
            return
        delay = min(30.0, 2.0 ** (len(self.exits) - 1))
        self.state = "backoff"
        self.next_start = now + delay
        log(f"service {self.name}: exited {status}; restarting in {delay:.0f}s")

    def status(self) -> dict[str, Any]:
        return {
            "state": self.state,
            "pid": self.process.pid if self.process else None,
            "last_exit": self.last_status,
            "log": str(self.log_path),
        }


def _ignore(*_: object) -> None:
    """A handler that only exists so the signal wakes select()."""


class Supervisor:
    def __init__(self, services: list[Service]) -> None:
        self.services = services
        self.stopping = False
        self.read_fd, self.write_fd = os.pipe()
        os.set_blocking(self.read_fd, False)
        os.set_blocking(self.write_fd, False)

    def install_signals(self) -> None:
        signal.set_wakeup_fd(self.write_fd)
        signal.signal(signal.SIGCHLD, _ignore)
        signal.signal(signal.SIGTERM, self._stop)
        signal.signal(signal.SIGINT, self._stop)

    def _stop(self, *_: object) -> None:
        self.stopping = True

    def statuses(self) -> dict[str, Any]:
        return {service.name: service.status() for service in self.services}

    def publish(self) -> None:
        write_json(SERVICES, self.statuses())

    def start_all(self) -> None:
        for service in self.services:
            service.start()
        self.publish()

    def step(self) -> bool:
        """Reap and restart. Returns True if any state changed."""
        changed = False
        now = time.monotonic()
        for service in self.services:
            if service.process is not None:
                status = service.process.poll()
                if status is not None:
                    service.exited(status)
                    changed = True
            elif service.state == "backoff" and now >= service.next_start:
                service.start()
                changed = True
        return changed

    def run(self) -> None:
        while not self.stopping:
            if self.step():
                self.publish()
            pending = [s.next_start for s in self.services if s.state == "backoff"]
            timeout = max(0.0, min(pending) - time.monotonic()) if pending else 5.0
            try:
                select.select([self.read_fd], [], [], min(timeout, 5.0))
                os.read(self.read_fd, 4096)
            except (BlockingIOError, InterruptedError):
                pass
        self.shutdown()

    def shutdown(self) -> None:
        log("init: stopping services")
        for service in self.services:
            if service.process is not None:
                try:
                    os.killpg(service.process.pid, signal.SIGTERM)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 15
        for service in self.services:
            if service.process is None:
                continue
            try:
                service.process.wait(timeout=max(0.1, deadline - time.monotonic()))
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(service.process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
            service.state = "stopped"


def wait_for_docker(timeout: float = 60) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with socket.socket(socket.AF_UNIX) as sock:
            sock.settimeout(1)
            try:
                sock.connect("/var/run/docker.sock")
                return True
            except OSError:
                pass
        time.sleep(0.5)
    return False


def load_config() -> dict[str, Any]:
    try:
        return json.loads((STAGE / "config.json").read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        log(f"init: no usable {STAGE}/config.json ({exc}); running without modules")
        return {"modules": [], "skills": False, "dns": [], "docker": {}}


def boot() -> int:
    global _log_handle
    shutil.rmtree(RUN, ignore_errors=True)
    RUN.mkdir(parents=True, mode=0o755, exist_ok=True)
    SERVICE_LOGS.mkdir(parents=True, exist_ok=True)
    _log_handle = LOG.open("a")
    log(f"init: boot {boot_id()}")
    prepare_cgroups()
    # The template carries the image's ids; the remap then moves the whole home.
    copy_home_template(User.lookup("agent"))
    try:
        remap_agent(os.environ)
    except (OSError, subprocess.CalledProcessError) as exc:
        log(f"ids: cannot give agent the host user's ids: {exc}")
    agent = User.lookup("agent")
    root = User.lookup("root")
    SESSIONS.mkdir(mode=0o700, exist_ok=True)
    chown_agent(SESSIONS, agent)

    config = load_config()
    fix_dns(config.get("dns") or [])
    storage, docker_args = setup_docker_storage(config)
    link_skills(config, agent)

    seed = run_seed(agent, config)
    starts: dict[str, Any] = {}
    for item in config.get("modules", []):
        start = item.get("start")
        if not start:
            continue
        user = User.lookup(start.get("user", "agent"))
        status = run_start(item, config, user)
        starts[item["name"]] = {"ok": status == 0, "status": status}

    services = [
        Service(
            "dockerd",
            ["dockerd", *docker_args],
            root,
            base_env(root, config),
            "/",
            Path("/var/log/dockerd.log"),
        )
    ]
    for item in config.get("modules", []):
        spec = item.get("service")
        if not spec:
            continue
        name = str(item["name"])
        user = User.lookup(spec.get("user", "agent"))
        services.append(
            Service(
                name,
                [str(STAGE / "modules" / name / "service")],
                user,
                base_env(user, config, module_env(item)),
                str(STAGE / "modules" / name),
                SERVICE_LOGS / f"{name}.log",
            )
        )
    supervisor = Supervisor(services)
    supervisor.install_signals()
    supervisor.start_all()
    docker_ready = wait_for_docker()
    if not docker_ready:
        log("docker: dockerd did not become ready within 60s; see /var/log/dockerd.log")
    supervisor.step()
    supervisor.publish()
    write_json(
        READY,
        {
            "boot_id": boot_id(),
            "time": time.time(),
            "seed": seed,
            "start": starts,
            "services": supervisor.statuses(),
            "docker": {"storage": storage, "ready": docker_ready},
        },
    )
    log("init: ready")
    supervisor.run()
    log("init: stopped")
    return 0


def status() -> int:
    try:
        ready = json.loads(READY.read_text())
    except (OSError, ValueError):
        return 1
    if ready.get("boot_id") != boot_id():
        return 1
    try:
        ready["services"] = json.loads(SERVICES.read_text())
    except (OSError, ValueError):
        pass
    print(json.dumps(ready))
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        if os.getuid() != 0:
            print("kbx-init: must run as root (it is the container's init)", file=sys.stderr)
            return 1
        return boot()
    if args == ["status"]:
        return status()
    if len(args) == 2 and args[0] == "run-start":
        config = load_config()
        for item in config.get("modules", []):
            if item.get("name") == args[1] and item.get("start"):
                return run_start(item, config, None)
        print(f"kbx-init: module {args[1]!r} has no start.sh or is not enabled", file=sys.stderr)
        return 1
    print("usage: kbx-init [status | run-start MODULE]", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
