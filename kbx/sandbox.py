"""Sandbox naming, network, create/start/stop/recreate/rm and readiness."""

from __future__ import annotations

import errno
import hashlib
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .docker import ROOT, Docker
from .errors import KbxError
from .paths import Paths

HOME = "/home/agent"
LABEL_PROJECT = "kbx.project"
LABEL_NAME = "kbx.name"
LABEL_WORKSPACE = "kbx.workspace"
LABEL_GITDIR = "kbx.gitdir"
READY_TIMEOUT = 600.0
FIREWALL_PORT = 9  # discard: nothing should answer, and nothing may reach it

# Inner Docker's storage lives here; kbx-init mounts or points dockerd at it.
DOCKER_VOLUME_TARGET = "/var/lib/kbx-docker"


# Guest paths a mounted checkout may neither be nor sit under (nor contain).
SYSTEM_PATHS = (
    "/proc", "/sys", "/dev", "/run", "/etc", "/usr", "/bin", "/sbin", "/lib", "/lib64", "/boot",
    "/opt/kbx", "/var/lib/docker", DOCKER_VOLUME_TARGET,
)  # fmt: skip
SYSTEM_PARENTS = ("/", "/home", HOME, "/opt", "/var", "/var/lib", "/root", "/tmp")


@dataclass(frozen=True)
class Sandbox:
    name: str
    project: str
    project_dir: Path
    # "mount" (the host checkout, at the same path) or "clone" (see config.py).
    mode: str = "clone"

    @property
    def clone_dir(self) -> str:
        """Where clone mode keeps the sandbox's own clone (kept in the home volume)."""
        return f"{HOME}/work/{self.project}"

    @property
    def workdir(self) -> str:
        return str(self.project_dir) if self.mode == "mount" else self.clone_dir

    @property
    def home_volume(self) -> str:
        return f"{self.name}-home"

    @property
    def docker_volume(self) -> str:
        return f"{self.name}-docker"


def sanitize(basename: str) -> str:
    text = basename.lower().replace("_", "-")
    text = re.sub(r"[^a-z0-9.-]", "", text)[:32]
    text = text.lstrip(".-")
    return text or "project"


def for_project(project_dir: Path, mode: str = "clone") -> Sandbox:
    absolute = project_dir.resolve()
    project = sanitize(absolute.name)
    digest = hashlib.sha256(str(absolute).encode()).hexdigest()[:8]
    return Sandbox(name=f"kbx-{project}-{digest}", project=project, project_dir=absolute, mode=mode)


def _inside(parent: Path, path: Path) -> bool:
    return path == parent or parent in path.parents


def git_dirs(root: Path) -> tuple[Path, Path]:
    """The checkout's git directory and the repository's common directory.

    Both are `root/.git` for a main checkout. For a linked worktree `.git` is a
    file pointing into the main repository's `.git/worktrees/`, whose
    `commondir` leads back to that `.git`; a submodule checkout points into
    `.git/modules/` of its superproject.
    """
    entry = root / ".git"
    if entry.is_dir() and not entry.is_symlink():
        return entry, entry
    gitdir = None
    if entry.is_file() and not entry.is_symlink():
        text = entry.read_text(errors="replace").strip()
        if text.startswith("gitdir:"):
            gitdir = Path(os.path.normpath(root / text[len("gitdir:") :].strip()))
    if gitdir is None or not (gitdir / "HEAD").is_file():
        raise KbxError(f"{entry} does not lead to a git directory")
    common = gitdir
    commondir = gitdir / "commondir"
    if commondir.is_file():
        common = Path(os.path.normpath(gitdir / commondir.read_text(errors="replace").strip()))
    if not common.is_dir():
        raise KbxError(f"{gitdir}/commondir does not lead to a git directory")
    return gitdir, common


def shared_gitdir(sandbox: Sandbox) -> Path | None:
    """Mount mode: the repository directory outside the checkout that is mounted too."""
    _, common = git_dirs(sandbox.project_dir)
    return None if _inside(sandbox.project_dir, common) else common


def _check_path(text: str) -> None:
    if any(c in text for c in ',"\n\\'):
        raise KbxError(
            f"{text!r} cannot be bind-mounted (comma, quote or newline in the path); use KBX_WORKSPACE=clone"
        )
    if text in SYSTEM_PARENTS or any(
        text == p or text.startswith(p + "/") or p.startswith(text + "/") for p in SYSTEM_PATHS
    ):
        raise KbxError(
            f"{text} would shadow system paths inside the sandbox; move the project or use KBX_WORKSPACE=clone"
        )


def check_mountable(sandbox: Sandbox) -> None:
    """Mount mode: the checkout (and its repository directory, if that lies
    elsewhere) at paths that shadow nothing in the guest."""
    _check_path(str(sandbox.project_dir))
    shared = shared_gitdir(sandbox)
    if shared is not None:
        _check_path(str(shared))


def mounted_gitdir(docker: Docker, sandbox: Sandbox) -> str | None:
    """The repository directory an existing mount-mode container mounts besides the checkout."""
    data = docker.inspect("container", sandbox.name) or {}
    return ((data.get("Config") or {}).get("Labels") or {}).get(LABEL_GITDIR)


def workspace_mode(docker: Docker, sandbox: Sandbox) -> str | None:
    """The mode an existing container was created with (None if there is none)."""
    data = docker.inspect("container", sandbox.name)
    if data is None:
        return None
    labels = (data.get("Config") or {}).get("Labels") or {}
    return str(labels.get(LABEL_WORKSPACE) or "clone")  # containers from before mount mode


def ensure_network(docker: Docker, config: Config) -> None:
    net = config.network
    data = docker.inspect("network", net.name)
    if data is None:
        print(f"→ Creating Docker network {net.name} ({net.subnet}, bridge {net.bridge})")
        docker.run(
            [
                "network", "create", "--driver", "bridge", "--ipv6=false",
                "--subnet", net.subnet,
                "-o", f"com.docker.network.bridge.name={net.bridge}",
                "--label", "kbx=1",
                net.name,
            ]
        )  # fmt: skip
        return
    configs = (data.get("IPAM") or {}).get("Config") or []
    subnets = {c.get("Subnet") for c in configs}
    bridge = (data.get("Options") or {}).get("com.docker.network.bridge.name")
    if net.subnet not in subnets or bridge != net.bridge:
        raise KbxError(
            f"Docker network {net.name!r} exists with subnet {sorted(s for s in subnets if s)} and "
            f"bridge {bridge!r}, but kbx is configured for {net.subnet} / {net.bridge}. "
            "Fix [network] in config.toml, or remove the network after removing its sandboxes."
        )


def privilege_args(config: Config) -> list[str]:
    if config.runtime.privileges == "privileged":
        return ["--privileged"]
    # Enough for an inner dockerd without --privileged (which Kata 4 cannot
    # start, risk R1). kbx-init makes the cgroup mount writable and creates the
    # loop device nodes these rules allow (the guest kernel's own loop driver;
    # no host device is passed). Keep in sync with caps_flags in host/spike.sh.
    args = [
        "--cap-add", "SYS_ADMIN", "--cap-add", "NET_ADMIN", "--cap-add", "SYS_PTRACE",
        "--cap-add", "MKNOD", "--security-opt", "seccomp=unconfined",
        "--security-opt", "apparmor=unconfined", "--security-opt", "systempaths=unconfined",
        "--device-cgroup-rule", "c 10:237 rwm",  # /dev/loop-control
        "--device-cgroup-rule", "b 7:* rwm",  # /dev/loop*
    ]  # fmt: skip
    if Path("/dev/fuse").exists():  # for fuse-overlayfs inside; absent without the fuse module
        args += ["--device", "/dev/fuse"]
    return args


def create_args(sandbox: Sandbox, config: Config, paths: Paths) -> list[str]:
    """`docker create` arguments. Host paths mounted: the stage (read-only) and,
    in mount mode, the checkout at its own path."""
    args = [
        "create",
        "--name", sandbox.name,
        "--hostname", sandbox.name,
        "--runtime", config.runtime.name,
        "--network", config.network.name,
    ]  # fmt: skip
    for server in config.launcher.dns:
        args += ["--dns", server]
    args += [
        "--memory", config.launcher.memory,
        "--cpus", f"{config.launcher.cpus:g}",
        *privilege_args(config),
        # Fresh on every start (and every Kata VM boot): readiness marker, sessions.
        "--tmpfs", "/run:rw,exec,mode=755",
        "--stop-timeout", "20",
        "-e", f"SANDBOX_NAME={sandbox.name}",
        "-v", f"{sandbox.home_volume}:{HOME}",
        "-v", f"{sandbox.docker_volume}:{DOCKER_VOLUME_TARGET}",
        "--mount", f"type=bind,source={paths.stage},target=/opt/kbx/stage,readonly",
    ]  # fmt: skip
    if sandbox.mode == "mount":
        # kbx-init gives `agent` these ids, so files keep their owner on both sides.
        args += [
            "--mount", f"type=bind,source={sandbox.project_dir},target={sandbox.project_dir}",
            "-e", f"KBX_HOST_UID={os.getuid()}",
            "-e", f"KBX_HOST_GID={os.getgid()}",
        ]  # fmt: skip
        shared = shared_gitdir(sandbox)
        if shared is not None:
            # A linked worktree: git inside needs the main repository's .git,
            # at the path the worktree's .git file names. Only that directory:
            # the main checkout's files stay out.
            args += ["--mount", f"type=bind,source={shared},target={shared}", "--label", f"{LABEL_GITDIR}={shared}"]
    args += [
        "--label", f"{LABEL_PROJECT}={sandbox.project_dir}",
        "--label", f"{LABEL_NAME}={sandbox.name}",
        "--label", f"{LABEL_WORKSPACE}={sandbox.mode}",
        config.image.name,
    ]  # fmt: skip
    return args


def state(docker: Docker, sandbox: Sandbox) -> str | None:
    """Container status ("running", "exited", …) or None if it does not exist."""
    data = docker.inspect("container", sandbox.name)
    if data is None:
        return None
    labels = (data.get("Config") or {}).get("Labels") or {}
    if labels.get(LABEL_PROJECT) != str(sandbox.project_dir):
        raise KbxError(
            f"container {sandbox.name} exists but belongs to {labels.get(LABEL_PROJECT)!r}; refusing to use it"
        )
    return str((data.get("State") or {}).get("Status", "unknown"))


def ensure_created(docker: Docker, sandbox: Sandbox, config: Config, paths: Paths) -> bool:
    """Create the container if missing. Returns True if it was created."""
    if state(docker, sandbox) is not None:
        return False
    if docker.inspect("image", config.image.name) is None:
        raise KbxError(f"image {config.image.name!r} not found; run `kbx build` first")
    ensure_network(docker, config)
    print(f"→ Creating sandbox {sandbox.name} (runtime {config.runtime.name})")
    if config.runtime.name in ("runc", "io.containerd.runc.v2"):
        print("⚠ runtime runc shares the host kernel: this is NOT an isolated sandbox", file=sys.stderr)
    docker.run(create_args(sandbox, config, paths))
    return True


def ready_status(docker: Docker, sandbox: Sandbox) -> dict[str, Any] | None:
    result = docker.exec(sandbox.name, ["kbx-init", "status"], user=ROOT, check=False, timeout=30)
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except ValueError:
        return None


def startup_log(docker: Docker, sandbox: Sandbox, lines: int = 60) -> str:
    result = docker.exec(sandbox.name, ["tail", "-n", str(lines), "/var/log/kbx-startup.log"], user=ROOT, check=False)
    return result.stdout.decode("utf-8", "replace")


def ensure_running(docker: Docker, sandbox: Sandbox, timeout: float = READY_TIMEOUT) -> dict[str, Any]:
    status = state(docker, sandbox)
    if status is None:
        raise KbxError(f"sandbox {sandbox.name} does not exist")
    if status == "paused":
        raise KbxError(paused_message(sandbox))
    if status != "running":
        print(f"→ Starting {sandbox.name}")
        docker.run(["start", sandbox.name])
    return wait_ready(docker, sandbox, timeout)


def wait_ready(docker: Docker, sandbox: Sandbox, timeout: float = READY_TIMEOUT) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    announced = False
    while True:
        status = state(docker, sandbox)
        if status != "running":
            logs = docker.run(["logs", "--tail", "40", sandbox.name], check=False)
            output = (logs.stdout + logs.stderr).decode("utf-8", "replace")
            raise KbxError(f"sandbox {sandbox.name} stopped during startup ({status}):\n{output}")
        ready = ready_status(docker, sandbox)
        if ready is not None:
            _report(ready)
            return ready
        if time.monotonic() > deadline:
            raise KbxError(
                f"sandbox {sandbox.name} was not ready after {timeout:.0f}s. Startup log:\n"
                + startup_log(docker, sandbox)
            )
        if not announced:
            print("→ Waiting for sandbox startup…")
            announced = True
        time.sleep(0.5)


def _report(ready: dict[str, Any]) -> None:
    for name, result in sorted((ready.get("seed") or {}).items()):
        if not result.get("ok", False):
            print(f"⚠ seed failed for module {name}: {result.get('error')}", file=sys.stderr)
    for name, result in sorted((ready.get("start") or {}).items()):
        if not result.get("ok", False):
            print(f"⚠ start.sh failed for module {name} (exit {result.get('status')}); see `kbx logs`", file=sys.stderr)
    for name, result in sorted((ready.get("services") or {}).items()):
        if result.get("state") == "failed":
            print(f"⚠ service {name} failed; see `kbx logs`", file=sys.stderr)


FIREWALL_PROBE = r"""
import errno, socket, sys
s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
s.settimeout(float(sys.argv[3]))
try:
    s.connect((sys.argv[1], int(sys.argv[2])))
except socket.timeout:
    sys.exit(0)
except OSError as exc:
    print(exc.errno)
    sys.exit(0 if exc.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.EACCES, errno.EPERM) else 3)
sys.exit(3)
"""


def firewall_check(docker: Docker, sandbox: Sandbox, config: Config, env: dict[str, str]) -> None:
    """Fail closed unless a packet to the host (bridge gateway) is dropped."""
    if env.get("KBX_UNSAFE_NO_FIREWALL_CHECK") == "1":
        print("⚠ KBX_UNSAFE_NO_FIREWALL_CHECK=1: not checking the host firewall", file=sys.stderr)
        return
    gateway = config.network.gateway
    result = docker.exec(
        sandbox.name,
        ["python3", "-c", FIREWALL_PROBE, gateway, str(FIREWALL_PORT), "2"],
        check=False,
        timeout=30,
    )
    if result.returncode == 0:
        return
    detail = result.stdout.decode().strip()
    reason = (
        "the host answered" if detail in (str(errno.ECONNREFUSED), str(errno.ECONNRESET), "") else f"errno {detail}"
    )
    raise KbxError(
        f"host firewall is not active: a connection from the sandbox to {gateway} was not dropped "
        f"({reason}). Refusing to attach.\n"
        "  Start it with: sudo systemctl start kbx-firewall.service\n"
        "  (install it with: sudo host/install-firewall — see docs/host-setup.md)"
    )


def paused_message(sandbox: Sandbox) -> str:
    return (
        f"sandbox {sandbox.name} is paused. The kbx guard pauses a sandbox when the agent changes "
        "something host tools would run; `kbx resume` shows what happened and resumes it"
    )


def pause(docker: Docker, sandbox: Sandbox) -> bool:
    """Freeze the sandbox; stop it if the runtime cannot pause. True if it is no longer running."""
    if docker.ok(["pause", sandbox.name], timeout=60):
        return True
    return docker.ok(["stop", "-t", "5", sandbox.name], timeout=60)


def stop(docker: Docker, sandbox: Sandbox) -> None:
    status = state(docker, sandbox)
    if status == "paused":
        docker.run(["unpause", sandbox.name])
        status = "running"
    if status == "running":
        docker.run(["stop", sandbox.name], stdout=None)


def remove_container(docker: Docker, sandbox: Sandbox) -> None:
    if state(docker, sandbox) is not None:
        docker.run(["rm", "-f", sandbox.name])


def remove_volumes(docker: Docker, sandbox: Sandbox) -> None:
    for volume in (sandbox.home_volume, sandbox.docker_volume):
        if docker.inspect("volume", volume) is not None:
            docker.run(["volume", "rm", volume])


def list_sandboxes(docker: Docker) -> list[dict[str, str]]:
    out = docker.text(
        [
            "ps", "-a", "--filter", f"label={LABEL_NAME}",
            "--format", "{{.Names}}\t{{.State}}\t{{.Label \"" + LABEL_PROJECT + "\"}}",
        ]
    )  # fmt: skip
    rows: list[dict[str, str]] = []
    for line in out.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            rows.append({"name": parts[0], "state": parts[1], "project": parts[2]})
    return rows


def sessions(docker: Docker, name: str) -> list[str]:
    result = docker.exec(name, ["kbx-session", "list"], check=False, timeout=15)
    if result.returncode != 0:
        return []
    try:
        data = json.loads(result.stdout)
    except ValueError:
        return []
    return [str(item) for item in data] if isinstance(data, list) else []  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]
