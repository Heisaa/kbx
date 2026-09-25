"""XDG path resolution. Nothing here assumes a particular machine."""

from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

# The checkout this code runs from; built-in modules live beside the package.
CHECKOUT = Path(__file__).resolve().parent.parent


@dataclass(frozen=True)
class Paths:
    home: Path
    config_dir: Path
    data_dir: Path
    runtime_dir: Path
    checkout: Path

    @property
    def config_file(self) -> Path:
        return self.config_dir / "config.toml"

    @property
    def user_modules(self) -> Path:
        return self.config_dir / "modules"

    @property
    def builtin_modules(self) -> Path:
        return self.checkout / "modules"

    @property
    def stage(self) -> Path:
        return self.data_dir / "stage"

    @property
    def log_dir(self) -> Path:
        return self.data_dir / "log"

    @property
    def guard_dir(self) -> Path:
        return self.data_dir / "guard"


def _xdg(env: Mapping[str, str], name: str, fallback: Path) -> Path:
    # The spec says relative values are invalid and must be ignored.
    value = env.get(name, "")
    return Path(value) if value and os.path.isabs(value) else fallback


def resolve(env: Mapping[str, str] | None = None, checkout: Path = CHECKOUT) -> Paths:
    env = os.environ if env is None else env
    home_value = env.get("HOME") or str(Path.home())
    home = Path(home_value)
    data_dir = _xdg(env, "XDG_DATA_HOME", home / ".local/share") / "kbx"
    runtime = env.get("XDG_RUNTIME_DIR", "")
    runtime_dir = Path(runtime) / "kbx" if runtime and os.path.isabs(runtime) else data_dir / "run"
    return Paths(
        home=home,
        config_dir=_xdg(env, "XDG_CONFIG_HOME", home / ".config") / "kbx",
        data_dir=data_dir,
        runtime_dir=runtime_dir,
        checkout=checkout,
    )


def expand_user(value: str, home: Path) -> Path:
    """Expand a leading `~` against the given home (not the process's)."""
    if value == "~":
        return home
    if value.startswith("~/"):
        return home / value[2:]
    return Path(value)
