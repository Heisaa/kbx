"""Load `config.toml`, apply `KBX_*` overrides and validate into frozen dataclasses.

The file is optional: every setting has a safe default. Unknown keys are
errors, so a typo never silently falls back to a default.
"""

from __future__ import annotations

import ipaddress
import re
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .errors import KbxError
from .paths import Paths, expand_user

OptionValue = str | bool | int

_SIZE = re.compile(r"^[1-9][0-9]*[kmgt]?$", re.IGNORECASE)
_BRIDGE = re.compile(r"^[A-Za-z0-9_.-]{1,15}$")
_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_IMAGE = re.compile(r"^[a-z0-9][a-z0-9._/:@-]*$")
_DETACH = re.compile(r"^(\^.|.)$")
_MODULE = re.compile(r"^[a-z0-9][a-z0-9-]*$")


@dataclass(frozen=True)
class LauncherConfig:
    auto_update: bool = True
    remote_control: bool = True
    codex_search: bool = True
    shared_skills: bool = True
    detach_key: str = "^\\"
    memory: str = "8g"
    cpus: float = 4
    dns: tuple[str, ...] = ("1.1.1.1", "9.9.9.9")
    debug: bool = False
    docker_disk: str = "64g"


@dataclass(frozen=True)
class NetworkConfig:
    name: str = "kbx"
    subnet: str = "172.30.0.0/24"
    bridge: str = "br-kbx"

    @property
    def gateway(self) -> str:
        net = ipaddress.ip_network(self.subnet)
        return str(next(net.hosts()))


@dataclass(frozen=True)
class ImageConfig:
    name: str = "kbx-agent"


@dataclass(frozen=True)
class RuntimeConfig:
    name: str = "kata"
    # "caps" (explicit capabilities) or "privileged". Kata 4 cannot start a
    # --privileged container: its agent fails to recreate the host device list
    # (risk R1, see host/README.md).
    privileges: str = "caps"
    # "loop" (a loop-mounted ext4 image on the volume) or "volume" (dockerd's
    # data root on the volume itself). Under Kata the volume is virtio-fs, which
    # overlayfs cannot use as its upper layer (risk R2).
    docker_storage: str = "loop"


# Files in the checkout that host tools run without asking: hook scripts and
# hook-framework configs (run by `git commit` on the host), editor settings and
# tasks. In mount mode the guard reverts agent changes to them.
DEFAULT_PROTECT = (
    ".husky",
    ".githooks",
    ".lefthook",
    ".pre-commit-config.yaml",
    ".pre-commit-config.yml",
    "lefthook.yml",
    "lefthook.yaml",
    ".lefthook.yml",
    ".lefthook.yaml",
    "lefthook-local.yml",
    "lefthook-local.yaml",
    ".lefthook-local.yml",
    ".lefthook-local.yaml",
    ".vscode/settings.json",
    ".vscode/tasks.json",
)


@dataclass(frozen=True)
class WorkspaceConfig:
    # "mount": the sandbox works in the host checkout, bind-mounted at the same
    # path. "clone": a private clone in the sandbox; git crosses as bundles.
    mode: str = "mount"
    # Mount mode: the host-side guard (kbx/guard.py). Off only for development.
    guard: bool = True
    protect: tuple[str, ...] = DEFAULT_PROTECT


@dataclass(frozen=True)
class SkillsConfig:
    sources: tuple[Path, ...] = ()


@dataclass(frozen=True)
class Config:
    launcher: LauncherConfig = field(default_factory=LauncherConfig)
    network: NetworkConfig = field(default_factory=NetworkConfig)
    image: ImageConfig = field(default_factory=ImageConfig)
    runtime: RuntimeConfig = field(default_factory=RuntimeConfig)
    workspace: WorkspaceConfig = field(default_factory=WorkspaceConfig)
    skills: SkillsConfig = field(default_factory=SkillsConfig)
    modules: Mapping[str, bool] = field(default_factory=dict[str, bool])
    options: Mapping[str, Mapping[str, OptionValue]] = field(default_factory=dict[str, Mapping[str, OptionValue]])


_TRUE = {"1", "true", "yes", "on"}
_FALSE = {"0", "false", "no", "off"}


def parse_bool(value: str, where: str) -> bool:
    lowered = value.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise KbxError(f"{where} must be true or false, not {value!r}")


def _table(data: Mapping[str, Any], key: str, where: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise KbxError(f"{where}: [{key}] must be a table")
    return dict(value)  # pyright: ignore[reportUnknownArgumentType]


def _reject_unknown(table: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(table) - allowed)
    if unknown:
        raise KbxError(f"{where}: unknown setting(s) {', '.join(unknown)}")


def _bool(table: Mapping[str, Any], key: str, default: bool, where: str) -> bool:
    value = table.get(key, default)
    if not isinstance(value, bool):
        raise KbxError(f"{where}.{key} must be true or false")
    return value


def _str(table: Mapping[str, Any], key: str, default: str, where: str) -> str:
    value = table.get(key, default)
    if not isinstance(value, str):
        raise KbxError(f"{where}.{key} must be a string")
    return value


def _launcher(table: dict[str, Any], env: Mapping[str, str], where: str) -> LauncherConfig:
    defaults = LauncherConfig()
    allowed = set(LauncherConfig.__dataclass_fields__)
    _reject_unknown(table, allowed, where)
    # Per-run overrides: KBX_<NAME>, e.g. KBX_REMOTE_CONTROL=false.
    for key in sorted(allowed):
        raw = env.get("KBX_" + key.upper())
        if raw is None:
            continue
        origin = "KBX_" + key.upper()
        default = getattr(defaults, key)
        if isinstance(default, bool):
            table[key] = parse_bool(raw, origin)
        elif key == "cpus":
            try:
                table[key] = float(raw)
            except ValueError:
                raise KbxError(f"{origin} must be a number") from None
        elif key == "dns":
            table[key] = [part.strip() for part in raw.split(",") if part.strip()]
        else:
            table[key] = raw
    detach = _str(table, "detach_key", defaults.detach_key, where)
    if detach.lower() not in ("", "none") and not _DETACH.match(detach):
        raise KbxError(f'{where}.detach_key must be one character, "^X" notation, or "none"')
    memory = _str(table, "memory", defaults.memory, where)
    if not _SIZE.match(memory):
        raise KbxError(f"{where}.memory must look like 8g or 4096m")
    disk = _str(table, "docker_disk", defaults.docker_disk, where)
    if not _SIZE.match(disk):
        raise KbxError(f"{where}.docker_disk must look like 64g")
    cpus = table.get("cpus", defaults.cpus)
    if isinstance(cpus, bool) or not isinstance(cpus, int | float) or cpus <= 0:
        raise KbxError(f"{where}.cpus must be a positive number")
    dns_raw: Any = table.get("dns", list(defaults.dns))
    if not isinstance(dns_raw, list) or not dns_raw:
        raise KbxError(f"{where}.dns must be a non-empty list of IP addresses")
    dns: list[str] = []
    for item in dns_raw:  # pyright: ignore[reportUnknownVariableType]
        try:
            dns.append(str(ipaddress.ip_address(str(item))))  # pyright: ignore[reportUnknownArgumentType]
        except ValueError:
            raise KbxError(f"{where}.dns: {item!r} is not an IP address") from None
    return LauncherConfig(
        auto_update=_bool(table, "auto_update", defaults.auto_update, where),
        remote_control=_bool(table, "remote_control", defaults.remote_control, where),
        codex_search=_bool(table, "codex_search", defaults.codex_search, where),
        shared_skills=_bool(table, "shared_skills", defaults.shared_skills, where),
        detach_key=detach,
        memory=memory,
        cpus=float(cpus),
        dns=tuple(dns),
        debug=_bool(table, "debug", defaults.debug, where),
        docker_disk=disk,
    )


def _network(table: dict[str, Any], where: str) -> NetworkConfig:
    defaults = NetworkConfig()
    _reject_unknown(table, set(NetworkConfig.__dataclass_fields__), where)
    name = _str(table, "name", defaults.name, where)
    if not _NAME.match(name):
        raise KbxError(f"{where}.name is not a valid Docker network name")
    subnet = _str(table, "subnet", defaults.subnet, where)
    try:
        net = ipaddress.ip_network(subnet)
    except ValueError:
        raise KbxError(f"{where}.subnet {subnet!r} is not a network like 172.30.0.0/24") from None
    if not isinstance(net, ipaddress.IPv4Network) or net.prefixlen > 29:
        raise KbxError(f"{where}.subnet must be an IPv4 network of /29 or larger")
    bridge = _str(table, "bridge", defaults.bridge, where)
    if not _BRIDGE.match(bridge):
        raise KbxError(f"{where}.bridge must be 1-15 characters of [A-Za-z0-9_.-]")
    return NetworkConfig(name=name, subnet=str(net), bridge=bridge)


def _image(table: dict[str, Any], env: Mapping[str, str], where: str) -> ImageConfig:
    _reject_unknown(table, set(ImageConfig.__dataclass_fields__), where)
    name = env.get("KBX_IMAGE") or _str(table, "name", ImageConfig.name, where)
    if not _IMAGE.match(name):
        raise KbxError(f"{where}.name {name!r} is not a valid image name")
    return ImageConfig(name=name)


def _runtime(table: dict[str, Any], env: Mapping[str, str], where: str) -> RuntimeConfig:
    defaults = RuntimeConfig()
    _reject_unknown(table, set(RuntimeConfig.__dataclass_fields__), where)
    name = env.get("KBX_RUNTIME") or _str(table, "name", defaults.name, where)
    if not _NAME.match(name):
        raise KbxError(f"{where}.name {name!r} is not a valid runtime name")
    privileges = _str(table, "privileges", defaults.privileges, where)
    if privileges not in ("privileged", "caps"):
        raise KbxError(f'{where}.privileges must be "privileged" or "caps"')
    storage = _str(table, "docker_storage", defaults.docker_storage, where)
    if storage not in ("loop", "volume"):
        raise KbxError(f'{where}.docker_storage must be "loop" or "volume"')
    return RuntimeConfig(name=name, privileges=privileges, docker_storage=storage)


def _workspace(table: dict[str, Any], env: Mapping[str, str], where: str) -> WorkspaceConfig:
    defaults = WorkspaceConfig()
    _reject_unknown(table, set(WorkspaceConfig.__dataclass_fields__), where)
    mode = env.get("KBX_WORKSPACE") or _str(table, "mode", defaults.mode, where)
    if mode not in ("mount", "clone"):
        raise KbxError(f'{where}.mode must be "mount" or "clone" (KBX_WORKSPACE too)')
    raw_guard = env.get("KBX_GUARD")
    guard = (
        parse_bool(raw_guard, "KBX_GUARD") if raw_guard is not None else _bool(table, "guard", defaults.guard, where)
    )
    raw: Any = table.get("protect", list(defaults.protect))
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):  # pyright: ignore[reportUnknownVariableType]
        raise KbxError(f"{where}.protect must be a list of paths relative to the project")
    protect: list[str] = []
    for item in [str(entry) for entry in raw]:  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
        parts = Path(item).parts
        if not parts or Path(item).is_absolute() or ".." in parts or parts[0] == ".git":
            raise KbxError(f"{where}.protect: {item!r} must be a relative path inside the project, outside .git")
        protect.append(str(Path(item)))
    return WorkspaceConfig(mode=mode, guard=guard, protect=tuple(protect))


def _skills(table: dict[str, Any], home: Path, where: str) -> SkillsConfig:
    _reject_unknown(table, {"sources"}, where)
    raw: Any = table.get("sources", ["~/.agents/skills"])
    if not isinstance(raw, list) or not all(isinstance(item, str) for item in raw):  # pyright: ignore[reportUnknownVariableType]
        raise KbxError(f"{where}.sources must be a list of directory paths")
    sources = [expand_user(item, home) for item in raw]  # pyright: ignore[reportUnknownArgumentType, reportUnknownVariableType]
    for source in sources:
        if not source.is_absolute():
            raise KbxError(f"{where}.sources: {source} must be absolute or start with ~/")
    return SkillsConfig(sources=tuple(sources))


def _modules(table: dict[str, Any], where: str) -> dict[str, bool]:
    result: dict[str, bool] = {}
    for name, value in table.items():
        if not _MODULE.match(name):
            raise KbxError(f"{where}: {name!r} is not a valid module name")
        if not isinstance(value, bool):
            raise KbxError(f"{where}.{name} must be true or false")
        result[name] = value
    return result


def _options(table: dict[str, Any], where: str) -> dict[str, dict[str, OptionValue]]:
    result: dict[str, dict[str, OptionValue]] = {}
    for module, values in table.items():
        if not isinstance(values, dict):
            raise KbxError(f"{where}.{module} must be a table of option values")
        options: dict[str, OptionValue] = {}
        for key, value in values.items():  # pyright: ignore[reportUnknownVariableType]
            if not isinstance(value, str | bool | int):
                raise KbxError(f"{where}.{module}.{key} must be a string, number or boolean")
            options[str(key)] = value  # pyright: ignore[reportUnknownArgumentType]
        result[module] = options
    return result


def load(paths: Paths, env: Mapping[str, str]) -> Config:
    """Read the user's config file (if any) and validate it."""
    path = paths.config_file
    data: dict[str, Any] = {}
    if path.exists():
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except tomllib.TOMLDecodeError as exc:
            raise KbxError(f"{path}: invalid TOML: {exc}") from None
        except OSError as exc:
            raise KbxError(f"{path}: cannot read: {exc.strerror}") from None
    return parse(data, paths, env, str(path))


def parse(data: Mapping[str, Any], paths: Paths, env: Mapping[str, str], where: str) -> Config:
    sections = {"launcher", "network", "image", "runtime", "workspace", "skills", "modules", "options"}
    _reject_unknown(data, sections, where)
    return Config(
        launcher=_launcher(_table(data, "launcher", where), env, "[launcher]"),
        network=_network(_table(data, "network", where), "[network]"),
        image=_image(_table(data, "image", where), env, "[image]"),
        runtime=_runtime(_table(data, "runtime", where), env, "[runtime]"),
        workspace=_workspace(_table(data, "workspace", where), env, "[workspace]"),
        skills=_skills(_table(data, "skills", where), paths.home, "[skills]"),
        modules=_modules(_table(data, "modules", where), "[modules]"),
        options=_options(_table(data, "options", where), "[options]"),
    )
