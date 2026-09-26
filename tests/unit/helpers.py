"""Shared fixtures: temporary homes, a fake docker on PATH, throwaway git repos."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from unittest import mock

ROOT = Path(__file__).resolve().parents[2]
FAKE = Path(__file__).resolve().parent / "fakedocker.py"
KBX = ROOT / "bin" / "kbx"


def git(cwd: Path, *args: str, env: Mapping[str, str] | None = None) -> str:
    return subprocess.run(
        ["git", "-C", str(cwd), *args], check=True, capture_output=True, text=True, env=dict(env or os.environ)
    ).stdout.strip()


class TempHome(unittest.TestCase):
    """A clean HOME and XDG environment, with a fake docker on PATH."""

    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="kbxtest-"))
        self.addCleanup(subprocess.run, ["rm", "-rf", str(self.temp)], check=False)
        self.home = self.temp / "home"
        self.home.mkdir()
        self.state_dir = self.temp / "docker"
        self.state_dir.mkdir()
        bin_dir = self.temp / "bin"
        bin_dir.mkdir()
        (bin_dir / "docker").write_text(f'#!/bin/sh\nexec {sys.executable} {FAKE} "$@"\n')
        (bin_dir / "docker").chmod(0o755)
        gitconfig = self.temp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Test User\n\temail = test@example.com\n[init]\n\tdefaultBranch = main\n")
        self.env: dict[str, str] = {
            "PATH": f"{bin_dir}:{os.environ.get('PATH', '/usr/bin:/bin')}",
            "HOME": str(self.home),
            "LANG": "C.UTF-8",
            "FAKE_DOCKER_STATE": str(self.state_dir),
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
            # No host watcher daemons unless a test asks for one (tests/unit/test_watch.py).
            "KBX_NOTIFY": "off",
            "KBX_IDLE_STOP": "off",
        }
        patcher = mock.patch.dict(os.environ, self.env, clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    # --- fake docker ---

    def docker_state(self) -> dict[str, Any]:
        path = self.state_dir / "state.json"
        if not path.exists():
            return {"containers": {}, "images": {}, "networks": {}, "volumes": {}}
        return json.loads(path.read_text())

    def write_state(self, state: dict[str, Any]) -> None:
        temp = self.state_dir / "state.test.tmp"
        temp.write_text(json.dumps(state))
        os.replace(temp, self.state_dir / "state.json")

    def behave(self, **values: Any) -> None:
        state = self.docker_state()
        state.setdefault("behave", {}).update(values)
        self.write_state(state)

    def add_image(self, name: str = "kbx-agent", labels: dict[str, str] | None = None) -> None:
        state = self.docker_state()
        state["images"][name] = {"Config": {"Labels": labels or {}}}
        self.write_state(state)

    def calls(self) -> list[list[str]]:
        path = self.state_dir / "calls.jsonl"
        if not path.exists():
            return []
        lines = path.read_text().splitlines()
        if lines and not lines[-1].endswith("]"):
            lines.pop()  # a call still being written
        return [json.loads(line) for line in lines]

    def clear_calls(self) -> None:
        (self.state_dir / "calls.jsonl").unlink(missing_ok=True)

    @property
    def sandbox_home(self) -> Path:
        return self.state_dir / "home"

    # --- projects ---

    def make_repo(self, name: str = "project", commit: bool = True) -> Path:
        repo = self.temp / name
        repo.mkdir(parents=True)
        git(repo, "init", "-q")
        if commit:
            (repo / "README.md").write_text("hello\n")
            git(repo, "add", "README.md")
            git(repo, "commit", "-q", "-m", "initial")
        return repo

    def run_kbx(
        self, *args: str, cwd: Path | None = None, extra_env: Mapping[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = dict(self.env)
        env.update(extra_env or {})
        return subprocess.run(
            [sys.executable, str(KBX), *args],
            cwd=str(cwd or self.home),
            env=env,
            capture_output=True,
            text=True,
            check=False,
            timeout=120,
        )
