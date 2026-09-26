"""kbx-onboard: skip an agent's first-run screens for one working directory.

Run as `agent` before each launch. Logging in is left to the agent (`/login`
in Claude, the sign-in screen in Codex); this only answers the questions that
come before or after it:

  claude  ~/.claude.json: hasCompletedOnboarding (theme picker, intro screens)
          and projects[DIR].hasTrustDialogAccepted (folder trust dialog)
  codex   ~/.codex/config.toml: projects[DIR].trust_level (folder access)

The sandbox is the trust boundary, so trusting the working directory inside
it gives the agent nothing it could not already do. A Codex trust_level that
is already set (to anything) is left alone.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import tomlkit

from .seed import write_atomic


class OnboardError(Exception):
    pass


def _mode(path: Path) -> int:
    return os.stat(path).st_mode & 0o777 if path.exists() else 0o600


def claude(home: Path, workdir: str) -> bool:
    """Returns whether ~/.claude.json changed."""
    path = home / ".claude.json"
    try:
        data: Any = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
    except (OSError, ValueError) as exc:
        raise OnboardError(f"cannot read {path}: {exc}") from None
    if not isinstance(data, dict):
        raise OnboardError(f"{path} is not a JSON object")
    projects = data.setdefault("projects", {})
    project = projects.setdefault(workdir, {}) if isinstance(projects, dict) else None
    if not isinstance(project, dict):
        raise OnboardError(f"{path}: projects is not an object")
    if data.get("hasCompletedOnboarding") is True and project.get("hasTrustDialogAccepted") is True:
        return False
    data["hasCompletedOnboarding"] = True
    project["hasTrustDialogAccepted"] = True
    write_atomic(path, (json.dumps(data, indent=2) + "\n").encode(), _mode(path))
    return True


def codex(home: Path, workdir: str) -> bool:
    """Returns whether ~/.codex/config.toml changed."""
    path = home / ".codex" / "config.toml"
    try:
        doc = tomlkit.parse(path.read_text(encoding="utf-8")) if path.exists() else tomlkit.document()
    except (OSError, ValueError) as exc:
        raise OnboardError(f"cannot read {path}: {exc}") from None
    if "projects" not in doc:
        doc["projects"] = tomlkit.table(is_super_table=True)
    projects: Any = doc["projects"]
    if not isinstance(projects, dict):
        raise OnboardError(f"{path}: projects is not a table")
    if workdir not in projects:
        projects[workdir] = tomlkit.table()
    project: Any = projects[workdir]
    if not isinstance(project, dict):
        raise OnboardError(f"{path}: projects.{workdir!r} is not a table")
    if "trust_level" in project:
        return False
    project["trust_level"] = "trusted"
    write_atomic(path, tomlkit.dumps(doc).encode(), _mode(path))
    return True


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="kbx-onboard", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("agent", choices=("claude", "codex"))
    parser.add_argument("workdir")
    parser.add_argument("--home", type=Path, default=Path.home())
    args = parser.parse_args(argv)
    try:
        (claude if args.agent == "claude" else codex)(args.home, args.workdir)
    except (OnboardError, OSError) as exc:
        print(f"kbx-onboard: {exc}", file=sys.stderr)
        return 1
    return 0
