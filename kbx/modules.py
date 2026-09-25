"""Discover, parse and validate modules; resolve selection and options.

Search path: user modules (`$XDG_CONFIG_HOME/kbx/modules`) first, then the
built-in modules of this checkout. A user module with a built-in's name
replaces it entirely.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tomllib
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .config import Config, OptionValue
from .errors import KbxError
from .paths import Paths

NAME = re.compile(r"^[a-z0-9][a-z0-9-]*$")
OPTION = re.compile(r"^[a-z0-9][a-z0-9_-]*$")
ENV_KEY = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
AGENTS = ("claude", "codex", "pi")
USERS = ("agent", "root")
PARTS = ("build.sh", "home", "service", "start.sh")


@dataclass(frozen=True)
class OptionSpec:
    name: str
    default: str
    kind: str  # "string" or "bool"
    pattern: str | None = None
    enum: tuple[str, ...] | None = None
    description: str = ""

    def validate(self, value: OptionValue, where: str) -> str:
        if self.kind == "bool":
            if not isinstance(value, bool):
                raise KbxError(f"{where} must be true or false")
            return "true" if value else "false"
        if isinstance(value, bool):
            raise KbxError(f"{where} must be a string")
        text = str(value)
        if "\n" in text or "\0" in text:
            raise KbxError(f"{where} must be a single line")
        if self.enum is not None and text not in self.enum:
            raise KbxError(f"{where} must be one of: {', '.join(self.enum)}")
        if self.pattern is not None and not re.fullmatch(self.pattern, text):
            raise KbxError(f"{where} {text!r} does not match {self.pattern!r}")
        return text


@dataclass(frozen=True)
class Module:
    name: str
    path: Path
    source: str  # "user" or "builtin"
    description: str
    default: bool
    options: Mapping[str, OptionSpec] = field(default_factory=dict[str, OptionSpec])
    service_user: str = "agent"
    start_user: str = "agent"
    before_launch: tuple[str, ...] = ()
    enforce: tuple[str, ...] = ()
    copy: tuple[str, ...] = ()
    env: Mapping[str, str] = field(default_factory=dict[str, str])

    @property
    def has_build(self) -> bool:
        return (self.path / "build.sh").is_file()

    @property
    def has_home(self) -> bool:
        return (self.path / "home").is_dir()

    @property
    def has_service(self) -> bool:
        return (self.path / "service").is_file()

    @property
    def has_start(self) -> bool:
        return (self.path / "start.sh").is_file()


@dataclass(frozen=True)
class Resolved:
    module: Module
    options: Mapping[str, str]

    @property
    def name(self) -> str:
        return self.module.name

    def option_env(self) -> dict[str, str]:
        return {option_env_name(self.name, key): value for key, value in self.options.items()}


def option_env_name(module: str, option: str) -> str:
    return f"KBX_OPT_{module}_{option}".upper().replace("-", "_")


def _expect(data: Mapping[str, Any], allowed: set[str], where: str) -> None:
    unknown = sorted(set(data) - allowed)
    if unknown:
        raise KbxError(f"{where}: unknown key(s) {', '.join(unknown)}")


def _str_list(value: Any, where: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):  # pyright: ignore[reportUnknownVariableType]
        raise KbxError(f"{where} must be a list of strings")
    return tuple(value)  # pyright: ignore[reportUnknownArgumentType]


def _sub(data: Mapping[str, Any], key: str, where: str) -> dict[str, Any]:
    value = data.get(key, {})
    if not isinstance(value, dict):
        raise KbxError(f"{where}: [{key}] must be a table")
    return dict(value)  # pyright: ignore[reportUnknownArgumentType]


def _option(name: str, raw: Any, where: str) -> OptionSpec:
    if not OPTION.match(name):
        raise KbxError(f"{where}: invalid option name {name!r}")
    if not isinstance(raw, dict):
        raise KbxError(f'{where}.{name} must be a table like {{ default = "" }}')
    spec: dict[str, Any] = dict(raw)  # pyright: ignore[reportUnknownArgumentType]
    _expect(spec, {"default", "pattern", "enum", "description"}, f"{where}.{name}")
    if "default" not in spec:
        raise KbxError(f"{where}.{name} needs a default")
    default = spec["default"]
    description = spec.get("description", "")
    if not isinstance(description, str):
        raise KbxError(f"{where}.{name}.description must be a string")
    if isinstance(default, bool):
        if "pattern" in spec or "enum" in spec:
            raise KbxError(f"{where}.{name}: boolean options take no pattern or enum")
        return OptionSpec(name, "true" if default else "false", "bool", description=description)
    if not isinstance(default, str | int):
        raise KbxError(f"{where}.{name}.default must be a string, integer or boolean")
    pattern = spec.get("pattern")
    if pattern is not None:
        if not isinstance(pattern, str):
            raise KbxError(f"{where}.{name}.pattern must be a string")
        try:
            re.compile(pattern)
        except re.error as exc:
            raise KbxError(f"{where}.{name}.pattern is not a valid regex: {exc}") from None
    enum = _str_list(spec["enum"], f"{where}.{name}.enum") if "enum" in spec else None
    option = OptionSpec(name, str(default), "string", pattern, enum, description)
    option.validate(str(default), f"{where}.{name}.default")
    return option


def load_module(path: Path, source: str) -> Module:
    name = path.name
    where = f"{path}/module.toml"
    if not NAME.match(name):
        raise KbxError(f"{path}: module names must match [a-z0-9][a-z0-9-]*")
    manifest = path / "module.toml"
    if not manifest.is_file():
        raise KbxError(f"{where}: missing")
    try:
        data = tomllib.loads(manifest.read_text(encoding="utf-8"))
    except tomllib.TOMLDecodeError as exc:
        raise KbxError(f"{where}: invalid TOML: {exc}") from None
    _expect(data, {"description", "default", "options", "service", "start", "seed", "env"}, where)
    description = data.get("description")
    if not isinstance(description, str) or not description:
        raise KbxError(f"{where}: description (a non-empty string) is required")
    default = data.get("default")
    if not isinstance(default, bool):
        raise KbxError(f"{where}: default (true or false) is required")
    options = {key: _option(key, raw, f"{where} [options]") for key, raw in _sub(data, "options", where).items()}

    service = _sub(data, "service", where)
    _expect(service, {"user"}, f"{where} [service]")
    service_user = service.get("user", "agent")
    if service_user not in USERS:
        raise KbxError(f'{where} [service].user must be "agent" or "root"')
    if service and not (path / "service").is_file():
        raise KbxError(f"{where}: [service] is set but there is no service file")

    start = _sub(data, "start", where)
    _expect(start, {"user", "before_launch"}, f"{where} [start]")
    start_user = start.get("user", "agent")
    if start_user not in USERS:
        raise KbxError(f'{where} [start].user must be "agent" or "root"')
    before = _str_list(start.get("before_launch", []), f"{where} [start].before_launch")
    for agent in before:
        if agent not in AGENTS:
            raise KbxError(f"{where} [start].before_launch: unknown agent {agent!r}")
    if start and not (path / "start.sh").is_file():
        raise KbxError(f"{where}: [start] is set but there is no start.sh")

    seed = _sub(data, "seed", where)
    _expect(seed, {"enforce", "copy"}, f"{where} [seed]")
    enforce = _str_list(seed.get("enforce", []), f"{where} [seed].enforce")
    copy = _str_list(seed.get("copy", []), f"{where} [seed].copy")

    env_table = _sub(data, "env", where)
    env: dict[str, str] = {}
    for key, value in env_table.items():
        if not ENV_KEY.match(key) or key.startswith("KBX_"):
            raise KbxError(f"{where} [env]: invalid variable name {key!r}")
        if not isinstance(value, str) or "\n" in value or "\0" in value:
            raise KbxError(f"{where} [env].{key} must be a single-line string")
        env[key] = value

    return Module(
        name=name,
        path=path,
        source=source,
        description=description,
        default=default,
        options=options,
        service_user=service_user,
        start_user=start_user,
        before_launch=before,
        enforce=enforce,
        copy=copy,
        env=env,
    )


def _candidates(root: Path) -> Iterator[Path]:
    if not root.is_dir():
        return
    for entry in sorted(root.iterdir()):
        if entry.is_dir() and not entry.name.startswith("."):
            yield entry


def discover(paths: Paths) -> dict[str, Module]:
    """All modules by name; user modules replace built-ins of the same name."""
    found: dict[str, Module] = {}
    for entry in _candidates(paths.builtin_modules):
        found[entry.name] = load_module(entry, "builtin")
    for entry in _candidates(paths.user_modules):
        found[entry.name] = load_module(entry, "user")
    return dict(sorted(found.items()))


def resolve(modules: Mapping[str, Module], config: Config) -> list[Resolved]:
    """Enabled modules in name order, with validated option values."""
    for name in config.modules:
        if name not in modules:
            raise KbxError(f"[modules]: unknown module {name!r} (see `kbx check`)")
    for name, values in config.options.items():
        if name not in modules:
            raise KbxError(f"[options.{name}]: unknown module {name!r}")
        for key in values:
            if key not in modules[name].options:
                raise KbxError(f"[options.{name}]: module has no option {key!r}")
    resolved: list[Resolved] = []
    for name, module in modules.items():
        overrides = config.options.get(name, {})
        options: dict[str, str] = {}
        for key, spec in module.options.items():
            where = f"[options.{name}].{key}"
            value = (
                overrides[key]
                if key in overrides
                else (spec.default == "true" if spec.kind == "bool" else spec.default)
            )
            options[key] = spec.validate(value, where)
        if config.modules.get(name, module.default):
            resolved.append(Resolved(module, options))
    return resolved


# --- seed units (mirrors kbx_sandbox.seed; the packages share no code) ---

SeedKey = tuple[str, ...]


def seed_kind(relpath: str, copy: tuple[str, ...]) -> str:
    if relpath in copy:
        return "file"
    if relpath.endswith(".json"):
        return "json"
    if relpath.endswith(".toml"):
        return "toml"
    return "file"


def _leaves(value: Any, prefix: SeedKey) -> Iterator[SeedKey]:
    if isinstance(value, dict) and value:
        for key, item in value.items():  # pyright: ignore[reportUnknownVariableType]
            yield from _leaves(item, (*prefix, str(key)))  # pyright: ignore[reportUnknownArgumentType]
    else:
        yield prefix


def home_files(module: Module) -> Iterator[str]:
    home = module.path / "home"
    if not home.is_dir():
        return
    for root, dirs, files in os.walk(home, followlinks=True):
        dirs.sort()
        for name in sorted(files):
            yield str((Path(root) / name).relative_to(home))


def seed_units(module: Module) -> list[tuple[str, SeedKey]]:
    """(relpath, key path) for every seed unit; () is a whole file."""
    units: list[tuple[str, SeedKey]] = []
    for rel in home_files(module):
        kind = seed_kind(rel, module.copy)
        source = module.path / "home" / rel
        if kind == "file":
            units.append((rel, ()))
            continue
        try:
            text = source.read_text(encoding="utf-8")
            data: Any = json.loads(text) if kind == "json" else tomllib.loads(text)
        except (ValueError, OSError) as exc:
            raise KbxError(f"{source}: invalid {kind.upper()}: {exc}") from None
        if not isinstance(data, dict):
            raise KbxError(f"{source}: the top level must be an object")
        if data:
            units.extend((rel, key) for key in _leaves(data, ()))
    return units


def parse_enforce(entry: str) -> tuple[str, SeedKey]:
    rel, _, key = entry.partition(":")
    return rel, tuple(key.split(".")) if key else ()


# --- kbx check ---


@dataclass(frozen=True)
class Issue:
    level: str  # "error" or "warning"
    message: str


_SECRET_NAMES = re.compile(
    r"^(id_(rsa|dsa|ecdsa|ed25519)|.*\.(pem|key|p12|pfx)|\.env(\..*)?|credentials.*|auth\.json|\.netrc|\.npmrc|\.pypirc)$",
    re.IGNORECASE,
)
_SECRET_CONTENT = re.compile(
    rb"BEGIN [A-Z ]*PRIVATE KEY|sk-ant-[A-Za-z0-9_-]{10,}|sk-[A-Za-z0-9]{32,}|gh[pousr]_[A-Za-z0-9]{30,}|github_pat_[A-Za-z0-9_]{30,}|AKIA[0-9A-Z]{16}|xox[baprs]-[A-Za-z0-9-]{10,}"
)


def _overlap(a: SeedKey, b: SeedKey) -> bool:
    shorter = min(len(a), len(b))
    return a[:shorter] == b[:shorter]


def check(modules: Mapping[str, Module], resolved: list[Resolved]) -> list[Issue]:
    issues: list[Issue] = []
    enabled = {r.name for r in resolved}
    owners: dict[str, list[tuple[str, SeedKey]]] = {}
    for name, module in modules.items():
        try:
            units = seed_units(module)
        except KbxError as exc:
            issues.append(Issue("error", str(exc)))
            continue
        unit_set = set(units)
        for entry in module.enforce:
            rel, key = parse_enforce(entry)
            if (rel, key) not in unit_set:
                issues.append(Issue("error", f"{name}: [seed].enforce {entry!r} is not a seeded key or file"))
        for rel in module.copy:
            if not (module.path / "home" / rel).is_file():
                issues.append(Issue("error", f"{name}: [seed].copy {rel!r} does not exist under home/"))
        for part in ("build.sh", "service", "start.sh"):
            script = module.path / part
            if script.is_file() and not os.access(script, os.X_OK):
                issues.append(Issue("error", f"{name}: {part} is not executable (chmod +x {script})"))
        issues.extend(_scan_secrets(module))
        if name in enabled:
            for rel, key in units:
                owners.setdefault(rel, []).append((name, key))
    for rel, claims in sorted(owners.items()):
        for index, (first, key_a) in enumerate(claims):
            for second, key_b in claims[index + 1 :]:
                if first != second and _overlap(key_a, key_b):
                    what = rel if not key_a or not key_b else f"{rel}:{'.'.join(max(key_a, key_b, key=len))}"
                    issues.append(Issue("error", f"seed conflict: {first} and {second} both seed {what}"))
    issues.extend(_shellcheck(modules))
    return issues


def _scan_secrets(module: Module) -> Iterator[Issue]:
    if module.source != "user":
        return
    for root, _, files in os.walk(module.path, followlinks=True):
        for name in files:
            path = Path(root) / name
            flagged = bool(_SECRET_NAMES.match(name))
            if not flagged:
                try:
                    with path.open("rb") as handle:
                        flagged = bool(_SECRET_CONTENT.search(handle.read(1 << 20)))
                except OSError:
                    continue
            if flagged:
                yield Issue(
                    "warning", f"{module.name}: {path} looks like a credential; the stage must never hold secrets"
                )


def _shellcheck(modules: Mapping[str, Module]) -> Iterator[Issue]:
    binary = shutil.which("shellcheck")
    if not binary:
        return
    for name, module in modules.items():
        for part in ("build.sh", "service", "start.sh"):
            script = module.path / part
            if not script.is_file():
                continue
            first = script.read_bytes()[:64].split(b"\n", 1)[0]
            if not re.search(rb"\b(ba|da)?sh\b", first):
                continue
            result = subprocess.run([binary, "--format=gcc", str(script)], capture_output=True, text=True, check=False)
            for line in result.stdout.splitlines():
                level = "error" if ": error:" in line else "warning"
                issues_text = line.replace(str(module.path) + "/", f"{name}/")
                yield Issue(level, f"shellcheck {issues_text}")
