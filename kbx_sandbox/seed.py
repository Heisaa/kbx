"""kbx-seed: apply enabled modules' home/ trees to the agent's home.

Merge unit by file type: each key of a JSON or TOML file (recursively into
objects/tables; TOML written with tomlkit so comments survive), and whole
files otherwise. A three-way merge against the hash of what kbx last wrote
decides each unit:

  absent, never seeded                 → write the default
  equal to last seeded, default changed → update (you never touched it)
  different from last seeded           → leave it; it's yours
  absent, but previously seeded        → leave it absent; you deleted it
  listed in enforce                    → always set
"""

from __future__ import annotations

import argparse
import difflib
import hashlib
import json
import os
import sys
import tempfile
from collections.abc import MutableMapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import tomlkit

STAGE = Path("/opt/kbx/stage")
STATE_REL = ".local/state/kbx/seeds.json"

Key = tuple[str, ...]


class SeedError(Exception):
    pass


@dataclass(frozen=True)
class ModuleSpec:
    name: str
    home: Path
    enforce: tuple[str, ...] = ()
    copy: tuple[str, ...] = ()


def load_modules(stage: Path) -> list[ModuleSpec]:
    data = json.loads((stage / "config.json").read_text(encoding="utf-8"))
    specs: list[ModuleSpec] = []
    for item in data.get("modules", []):
        if not item.get("home"):
            continue
        name = str(item["name"])
        specs.append(
            ModuleSpec(
                name=name,
                home=stage / "modules" / name / "home",
                enforce=tuple(item.get("enforce", [])),
                copy=tuple(item.get("copy", [])),
            )
        )
    return specs


def kind_of(rel: str, copy: Sequence[str]) -> str:
    if rel in copy:
        return "file"
    if rel.endswith(".json"):
        return "json"
    if rel.endswith(".toml"):
        return "toml"
    return "file"


def value_hash(value: Any) -> str:
    text = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str, ensure_ascii=False)
    return hashlib.sha256(text.encode()).hexdigest()


def bytes_hash(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _plain(value: Any) -> Any:
    unwrap = getattr(value, "unwrap", None)
    return unwrap() if callable(unwrap) else value


def leaves(value: Any, prefix: Key = ()) -> list[tuple[Key, Any]]:
    """Merge units of a parsed object: recurse into non-empty objects."""
    if isinstance(value, MutableMapping) and value:
        result: list[tuple[Key, Any]] = []
        for key, item in value.items():  # pyright: ignore[reportUnknownVariableType]
            result.extend(leaves(item, (*prefix, str(key))))  # pyright: ignore[reportUnknownArgumentType]
        return result
    return [(prefix, _plain(value))]


# A current value that exists but cannot hold the key (a scalar where an
# object is expected). It never matches a hash, so it is always the user's.
BLOCKED = "\0blocked"


class Document:
    """A parsed JSON or TOML file with key-path access."""

    def __init__(self, kind: str, text: str | None) -> None:
        self.kind = kind
        self.root: Any
        if kind == "json":
            try:
                self.root = {} if text is None or not text.strip() else json.loads(text)
            except ValueError as exc:
                raise SeedError(f"invalid JSON: {exc}") from None
            if not isinstance(self.root, dict):
                raise SeedError("the top level is not a JSON object")
        else:
            try:
                self.root = tomlkit.document() if text is None else tomlkit.parse(text)
            except Exception as exc:  # tomlkit raises several ParseError types
                raise SeedError(f"invalid TOML: {exc}") from None

    def get(self, key: Key) -> tuple[bool, Any]:
        node: Any = self.root
        for index, part in enumerate(key):
            if not isinstance(node, MutableMapping):
                return True, BLOCKED
            if part not in node:
                return False, None
            node = node[part]
            if index < len(key) - 1 and not isinstance(node, MutableMapping):
                return True, BLOCKED
        return True, _plain(node)

    def set(self, key: Key, value: Any) -> None:
        node: Any = self.root
        for index, part in enumerate(key[:-1]):
            child = node.get(part) if isinstance(node, MutableMapping) else None
            if not isinstance(child, MutableMapping):
                if self.kind == "json":
                    child = {}
                else:
                    # Intermediate tables above the leaf's parent get no header.
                    child = tomlkit.table(is_super_table=index < len(key) - 2)
                node[part] = child
                child = node[part]
            node = child
        node[key[-1]] = value

    def dump(self) -> str:
        if self.kind == "json":
            return json.dumps(self.root, indent=2, ensure_ascii=False) + "\n"
        return tomlkit.dumps(self.root)  # pyright: ignore[reportUnknownMemberType]


@dataclass
class UnitResult:
    module: str
    rel: str
    key: Key
    status: str  # new, default, updated, user, deleted, enforced, adopted
    changed: bool


@dataclass
class FileWrite:
    rel: str
    target: Path
    old: bytes | None
    new: bytes
    mode: int


@dataclass
class ModulePlan:
    name: str
    units: list[UnitResult] = field(default_factory=list[UnitResult])
    writes: list[FileWrite] = field(default_factory=list[FileWrite])
    state: dict[str, str | None] = field(default_factory=dict[str, "str | None"])
    error: str | None = None


def state_key(module: str, rel: str, key: Key) -> str:
    return json.dumps([module, rel, list(key)], ensure_ascii=False)


def decide(present: bool, current: str | None, last: str | None, default: str, enforced: bool) -> tuple[str, bool]:
    """(status, write?) for one unit. Hashes are compared, never values."""
    if enforced:
        return ("enforced", not present or current != default)
    if last is None:
        if not present:
            return ("new", True)
        if current == default:
            return ("adopted", False)
        return ("user", False)
    if not present:
        return ("deleted", False)
    if current == last:
        return ("updated", True) if default != last else ("default", False)
    if current == default:
        return ("adopted", False)
    return ("user", False)


def safe_target(home: Path, rel: str) -> Path:
    """Resolve rel under home; symlinks may never lead out of it."""
    if os.path.isabs(rel) or ".." in Path(rel).parts:
        raise SeedError(f"{rel}: invalid path")
    home_real = os.path.realpath(home)
    real = os.path.realpath(os.path.join(home_real, rel))
    if real != home_real and not real.startswith(home_real + os.sep):
        raise SeedError(f"{rel}: resolves to {real}, outside {home_real}; refusing to follow the symlink")
    if os.path.isdir(real):
        raise SeedError(f"{rel}: is a directory")
    return Path(real)


def module_files(spec: ModuleSpec) -> list[str]:
    files: list[str] = []
    for root, dirs, names in os.walk(spec.home):
        dirs.sort()
        for name in sorted(names):
            files.append(str((Path(root) / name).relative_to(spec.home)))
    return files


def _enforced(spec: ModuleSpec) -> set[tuple[str, Key]]:
    result: set[tuple[str, Key]] = set()
    for entry in spec.enforce:
        rel, _, key = entry.partition(":")
        result.add((rel, tuple(key.split(".")) if key else ()))
    return result


def plan_module(
    spec: ModuleSpec,
    home: Path,
    state: dict[str, str],
    *,
    reset: bool = False,
    only_prefix: str | None = None,
    overlay: dict[Path, bytes] | None = None,
) -> ModulePlan:
    """Plan one module. `overlay` holds files earlier modules will write."""
    overlay = {} if overlay is None else overlay
    plan = ModulePlan(spec.name)
    enforced = _enforced(spec)
    try:
        for rel in module_files(spec):
            if only_prefix and not rel.startswith(only_prefix):
                continue
            source = spec.home / rel
            target = safe_target(home, rel)
            kind = kind_of(rel, spec.copy)
            old = overlay[target] if target in overlay else (target.read_bytes() if target.exists() else None)
            if kind == "file":
                default = source.read_bytes()
                key = state_key(spec.name, rel, ())
                current = bytes_hash(old) if old is not None else None
                status, write = decide(
                    old is not None, current, state.get(key), bytes_hash(default), reset or (rel, ()) in enforced
                )
                plan.units.append(UnitResult(spec.name, rel, (), status, write))
                if write:
                    mode = 0o755 if os.stat(source).st_mode & 0o111 else 0o644
                    plan.writes.append(FileWrite(rel, target, old, default, mode))
                if write or status == "adopted":
                    plan.state[key] = bytes_hash(default)
                continue
            try:
                defaults = Document(kind, source.read_text(encoding="utf-8"))
            except SeedError as exc:
                raise SeedError(f"module default {rel}: {exc}") from None
            try:
                doc = Document(kind, old.decode("utf-8") if old is not None else None)
            except (SeedError, UnicodeDecodeError) as exc:
                raise SeedError(f"{rel}: {exc}; not changing this module") from None
            dirty = False
            for key_path, value in leaves(defaults.root):
                if not key_path:
                    continue  # an empty default file seeds nothing
                key = state_key(spec.name, rel, key_path)
                present, current_value = doc.get(key_path)
                current = value_hash(current_value) if present else None
                default_hash = value_hash(value)
                status, write = decide(
                    present, current, state.get(key), default_hash, reset or (rel, key_path) in enforced
                )
                plan.units.append(UnitResult(spec.name, rel, key_path, status, write))
                if write:
                    doc.set(key_path, value)
                    dirty = True
                if write or status == "adopted":
                    plan.state[key] = default_hash
            if dirty:
                new = doc.dump().encode()
                if new != old:
                    mode = os.stat(target).st_mode & 0o777 if target.exists() else 0o644
                    plan.writes.append(FileWrite(rel, target, old, new, mode))
    except (SeedError, OSError) as exc:
        return ModulePlan(spec.name, error=str(exc))
    return plan


def plan_all(
    specs: Sequence[ModuleSpec],
    home: Path,
    state: dict[str, str],
    *,
    reset: bool = False,
    only_prefix: str | None = None,
) -> list[ModulePlan]:
    """Plan modules in order; each sees the files the previous ones will write."""
    overlay: dict[Path, bytes] = {}
    plans: list[ModulePlan] = []
    for spec in specs:
        plan = plan_module(spec, home, state, reset=reset, only_prefix=only_prefix, overlay=overlay)
        if plan.error is None:
            for write in plan.writes:
                overlay[write.target] = write.new
        plans.append(plan)
    return plans


def write_atomic(path: Path, data: bytes, mode: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temp = tempfile.mkstemp(dir=path.parent, prefix=".kbx-seed-")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.chmod(temp, mode)
        os.replace(temp, path)
    except BaseException:
        Path(temp).unlink(missing_ok=True)
        raise


def load_state(home: Path) -> dict[str, str]:
    path = home / STATE_REL
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (ValueError, OSError) as exc:
        backup = path.with_suffix(".corrupt")
        print(f"kbx-seed: {path} is unreadable ({exc}); moved to {backup}", file=sys.stderr)
        os.replace(path, backup)
        return {}
    units = data.get("units", {}) if isinstance(data, dict) else {}  # pyright: ignore[reportUnknownMemberType, reportUnknownVariableType]
    return {str(k): str(v) for k, v in units.items()}  # pyright: ignore[reportUnknownVariableType, reportUnknownArgumentType]


def save_state(home: Path, state: dict[str, str]) -> None:
    body = json.dumps({"version": 1, "units": dict(sorted(state.items()))}, indent=1, ensure_ascii=False)
    write_atomic(home / STATE_REL, body.encode() + b"\n", 0o644)


def apply(plans: list[ModulePlan], home: Path, state: dict[str, str]) -> dict[str, dict[str, Any]]:
    report: dict[str, dict[str, Any]] = {}
    for plan in plans:
        if plan.error is not None:
            report[plan.name] = {"ok": False, "error": plan.error, "written": []}
            continue
        try:
            for write in plan.writes:
                write_atomic(write.target, write.new, write.mode)
        except OSError as exc:
            report[plan.name] = {"ok": False, "error": str(exc), "written": []}
            continue
        for key, value in plan.state.items():
            if value is None:
                state.pop(key, None)
            else:
                state[key] = value
        report[plan.name] = {"ok": True, "error": None, "written": [w.rel for w in plan.writes]}
    save_state(home, state)
    return report


def _unit_name(unit: UnitResult) -> str:
    return unit.rel + (":" + ".".join(unit.key) if unit.key else "")


def print_dry_run(plans: list[ModulePlan]) -> None:
    for plan in plans:
        if plan.error:
            print(f"✗ {plan.name}: {plan.error}")
            continue
        changes = [u for u in plan.units if u.changed]
        print(f"{plan.name}: {len(changes)} change(s)")
        for unit in changes:
            print(f"  {unit.status:<8} {_unit_name(unit)}")
        for write in plan.writes:
            try:
                old = (write.old or b"").decode().splitlines(keepends=True)
                new = write.new.decode().splitlines(keepends=True)
            except UnicodeDecodeError:
                print(f"  (binary) {write.rel}")
                continue
            sys.stdout.writelines(difflib.unified_diff(old, new, f"a/{write.rel}", f"b/{write.rel}"))


def print_status(plans: list[ModulePlan]) -> None:
    labels = {
        "new": "not yet seeded",
        "default": "default",
        "updated": "update pending",
        "user": "user-owned",
        "deleted": "deleted by you",
        "enforced": "enforced",
        "adopted": "default",
    }
    for plan in plans:
        if plan.error:
            print(f"✗ {plan.name}: {plan.error}")
            continue
        print(f"{plan.name}:")
        for unit in plan.units:
            label = labels[unit.status]
            if unit.status == "enforced":
                label += " (will be set)" if unit.changed else ""
            print(f"  {label:<24} {_unit_name(unit)}")


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kbx-seed", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--stage", type=Path, default=STAGE)
    parser.add_argument("--home", type=Path, default=Path.home())
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="show what would change")
    mode.add_argument("--status", action="store_true", help="show the state of every unit")
    mode.add_argument("--reset", metavar="MODULE", help="restore a module's defaults")
    parser.add_argument("--only-prefix", metavar="PATH", help="only files under this home-relative path")
    parser.add_argument("--report", type=Path, help="write a JSON result per module here")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args(argv)

    try:
        specs = load_modules(args.stage)
    except (OSError, ValueError, KeyError) as exc:
        print(f"kbx-seed: cannot read {args.stage}/config.json: {exc}", file=sys.stderr)
        return 1
    if args.reset:
        specs = [s for s in specs if s.name == args.reset]
        if not specs:
            print(f"kbx-seed: module {args.reset!r} is not enabled or has no home/ defaults", file=sys.stderr)
            return 1
    state = load_state(args.home)
    plans = plan_all(specs, args.home, state, reset=bool(args.reset), only_prefix=args.only_prefix)
    if args.dry_run:
        print_dry_run(plans)
        return 1 if any(p.error for p in plans) else 0
    if args.status:
        print_status(plans)
        return 1 if any(p.error for p in plans) else 0
    report = apply(plans, args.home, state)
    if args.report:
        args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    failed = False
    for name, result in report.items():
        if not result["ok"]:
            failed = True
            print(f"kbx-seed: {name}: {result['error']}", file=sys.stderr)
        elif result["written"] and not args.quiet:
            print(f"kbx-seed: {name}: wrote {', '.join(result['written'])}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
