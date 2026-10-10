"""Admission of local Kata VMs against the host RAM and /dev/shm they would share."""

from __future__ import annotations

import json
import os
import shutil
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .config import Config
from .docker import Docker
from .errors import KbxError

GIB = 1024**3
MIB = 1024**2
RUNC = ("runc", "io.containerd.runc.v2")
KATA_CONFIGS = (
    Path("/etc/kata-containers/configuration.toml"),
    Path("/opt/kata/share/defaults/kata-containers/configuration.toml"),
)
KATA_RS_CONFIGS = (
    Path("/etc/kata-containers/runtime-rs/configuration.toml"),
    Path("/opt/kata/share/defaults/kata-containers/runtime-rs/configuration.toml"),
)


def size_bytes(value: str) -> int:
    suffix = value[-1].lower()
    power = "kmgt".find(suffix) + 1
    return int(value[:-1]) * 1024**power if power else int(value)


def gib(value: int) -> str:
    return f"{value / GIB:.1f} GiB"


@dataclass(frozen=True)
class Capacity:
    memory_total: int
    memory_available: int
    shm_total: int
    shm_available: int


def capacity() -> Capacity:
    try:
        memory = {}
        for line in Path("/proc/meminfo").read_text().splitlines():
            key, _, value = line.partition(":")
            memory[key] = int(value.split()[0]) * 1024
        shm = os.statvfs("/dev/shm")
        return Capacity(
            memory["MemTotal"], memory["MemAvailable"], shm.f_blocks * shm.f_frsize, shm.f_bavail * shm.f_frsize
        )
    except (OSError, ValueError, KeyError) as exc:
        raise KbxError(f"cannot check host memory capacity: {exc}") from None


def kata_settings() -> tuple[int, bool]:
    """Read standard Kata configs; unknown/custom runtimes use conservative defaults.

    A known Cloud Hypervisor configuration uses memfd rather than /dev/shm.
    Unreadable or ambiguous configurations are treated as QEMU with virtio-fs.
    """
    shim = shutil.which("containerd-shim-kata-v2")
    runtime_rs = bool(shim and "runtime-rs" in Path(shim).resolve().parts)
    for path in KATA_RS_CONFIGS if runtime_rs else KATA_CONFIGS:
        try:
            data = tomllib.loads(path.read_text())
        except FileNotFoundError:
            continue
        except (OSError, ValueError):
            break
        hypervisors = data.get("hypervisor", {})
        if not isinstance(hypervisors, dict) or len(hypervisors) != 1:
            break
        name, settings = next(iter(hypervisors.items()))
        if not isinstance(settings, dict):
            break
        base = settings.get("default_memory", 2048)
        if isinstance(base, bool) or not isinstance(base, int) or base <= 0:
            break
        return base * MIB, runtime_rs or name != "clh"
    return 2 * GIB, True


def _local(docker: Docker) -> None:
    endpoint = (os.environ.get("DOCKER_HOST") if not os.environ.get("DOCKER_CONTEXT") else None) or docker.text(
        ["context", "inspect", "--format", "{{.Endpoints.docker.Host}}"]
    )
    if not endpoint.startswith("unix://"):
        raise KbxError(
            "memory admission requires a local Docker Unix socket; host RAM and /dev/shm cannot be "
            "checked for a remote daemon. Run kbx on the Docker host."
        )


def _inspect(docker: Docker, names: list[str]) -> list[dict[str, Any]]:
    # Unlike inspect_many, admission must fail if inspection is incomplete.
    try:
        data = json.loads(docker.text(["container", "inspect", *names]))
    except ValueError:
        raise KbxError("cannot inspect containers for the memory budget; retry the start") from None
    if not isinstance(data, list) or len(data) != len(names) or not all(isinstance(item, dict) for item in data):
        raise KbxError("incomplete container inspection for the memory budget; retry the start")
    return data


def _committed(docker: Docker, target: dict[str, Any], name: str, overhead: int) -> int:
    """Stored limits plus base VM RAM of every active VM and the target: the worst case."""
    active_names = docker.text(["ps", "--quiet"]).splitlines()  # includes paused VMs, across all projects
    active = _inspect(docker, active_names) if active_names else []
    default_runtime: str | None = None
    committed = 0
    for container in [*active, target]:
        if container is not target and container.get("Name") == target.get("Name"):
            continue
        if container is not target and container.get("State", {}).get("Status") not in (
            "running",
            "paused",
            "restarting",
        ):
            continue
        settings = container.get("HostConfig") or {}
        kind = settings.get("Runtime")
        if not kind:
            if default_runtime is None:
                default_runtime = docker.text(["info", "--format", "{{.DefaultRuntime}}"])
            kind = default_runtime
        if kind in RUNC:
            continue
        limit = settings.get("Memory", 0)
        if isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0:
            raise KbxError(
                f"cannot budget VM {container.get('Name', name)} without a positive Docker memory limit; "
                "set [launcher] memory and recreate kbx sandboxes, or limit the other VM"
            )
        committed += limit + overhead
    return committed


def _headroom(name: str, what: str, free: int, reserve: int, boot: int, requested: int, hint: str) -> None:
    """Refuse a VM that cannot boot in what is free now; warn if it cannot grow to its limit."""
    if free - reserve < boot:
        raise KbxError(
            f"refusing to start {name}: it needs {gib(boot)} to boot, but {what} has {gib(free)} free "
            f"and {gib(reserve)} is reserved for the host. Stop an unused sandbox{hint}."
        )
    if free - reserve < requested:
        print(
            f"⚠ {name} can grow to {gib(requested)}, but {what} has {gib(free - reserve)} free beyond the "
            f"{gib(reserve)} host reserve; stop an unused sandbox if it runs short{hint}.",
            file=sys.stderr,
        )


def check(docker: Docker, name: str, config: Config) -> None:
    """Caller holds the start lock until Docker has started the admitted VM.

    Running VMs are charged by the RAM and /dev/shm they have already touched,
    which the host's free memory reflects, not by their limits: guest memory is
    allocated lazily. The new VM is refused only if it could not boot; if its
    full limit does not fit, kbx warns and starts it. An explicit budget size is
    a hard cap on the stored limits of all active VMs.
    """
    ram_option = config.launcher.memory_budget
    shm_option = config.launcher.shm_budget
    if ram_option == shm_option == "off":
        return
    target = _inspect(docker, [name])[0]
    host = target.get("HostConfig") or {}
    runtime = host.get("Runtime") or docker.text(["info", "--format", "{{.DefaultRuntime}}"])
    if runtime in RUNC:
        return
    _local(docker)
    available = capacity()
    base, uses_shm = kata_settings() if runtime == "kata" else (2 * GIB, True)
    overhead = max(base, size_bytes(config.runtime.memory_overhead))
    requested = int(host.get("Memory") or 0) + overhead
    explicit = ram_option not in ("auto", "off") or shm_option not in ("auto", "off")
    committed = _committed(docker, target, name, overhead) if explicit else 0
    ram_reserve = max(4 * GIB, available.memory_total // 5)
    shm_reserve = max(512 * MIB, available.shm_total // 10)
    hint = " or lower [launcher] memory and run `kbx recreate`"
    if ram_option != "off":
        if ram_option != "auto":
            # An explicit budget can tighten the host reserve, never eliminate it.
            budget = min(size_bytes(ram_option), available.memory_total - ram_reserve)
            if committed > budget:
                raise KbxError(
                    f"refusing to start {name}: VM memory limits would total {gib(committed)} "
                    f"(memory_budget {gib(max(0, budget))}). Stop an unused sandbox{hint}."
                )
        _headroom(name, "host RAM", available.memory_available, ram_reserve, overhead, requested, hint)
    if shm_option != "off":
        usage = available.shm_total - available.shm_available
        if shm_option != "auto":
            budget = min(size_bytes(shm_option), available.shm_total - shm_reserve)
            # Usage already holds the pages running VMs have touched; charging it on top
            # of their limits would count them twice.
            demand = max(committed, usage + requested)
            if demand > budget:
                raise KbxError(
                    f"refusing to start {name}: /dev/shm has {gib(usage)} in use and VM memory limits "
                    f"would total {gib(committed)} (shm_budget {gib(max(0, budget))}). "
                    f"Stop an unused sandbox{hint}."
                )
        if uses_shm:
            hint_shm = f"{hint}; a full /dev/shm crashes VMs (see docs/host-setup.md)"
            _headroom(name, "/dev/shm", available.shm_available, shm_reserve, overhead, requested, hint_shm)
