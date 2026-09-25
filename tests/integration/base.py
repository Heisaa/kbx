"""Integration harness: a real sandbox from the built image.

Skipped unless KBX_INTEGRATION=1. Uses the real `docker`, the image named by
KBX_IMAGE (default kbx-agent) and the runtime named by KBX_RUNTIME (default
kata). Each test class gets a throwaway HOME, config and git project, and
removes its sandbox and volumes afterwards.

  KBX_INTEGRATION=1 python3 -m unittest discover -s tests/integration -t .
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import ClassVar

ROOT = Path(__file__).resolve().parents[2]
KBX = ROOT / "bin" / "kbx"
ENABLED = os.environ.get("KBX_INTEGRATION") == "1"
sys.path.insert(0, str(ROOT))

from kbx import sandbox as sandbox_mod  # noqa: E402


def firewall_active() -> bool:
    """True if the kbx firewall chains exist on this host (needs sudo -n)."""
    result = subprocess.run(["sudo", "-n", "iptables", "-n", "-L", "KBX-INPUT"], capture_output=True, check=False)
    return result.returncode == 0


@unittest.skipUnless(ENABLED, "set KBX_INTEGRATION=1 to run against a real sandbox")
class SandboxCase(unittest.TestCase):
    temp: ClassVar[Path]
    home: ClassVar[Path]
    repo: ClassVar[Path]
    env: ClassVar[dict[str, str]]
    name: ClassVar[str]
    config_text: ClassVar[str] = ""

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp = Path(tempfile.mkdtemp(prefix="kbxint-"))
        cls.home = cls.temp / "home"
        (cls.home / ".config" / "kbx").mkdir(parents=True)
        (cls.home / ".config" / "kbx" / "config.toml").write_text(cls.config_text)
        (cls.home / ".agents" / "skills" / "hello").mkdir(parents=True)
        (cls.home / ".agents" / "skills" / "hello" / "SKILL.md").write_text("---\nname: hello\n---\nv1\n")
        gitconfig = cls.temp / "gitconfig"
        gitconfig.write_text("[user]\n\tname = Int Test\n\temail = int@example.com\n[init]\n\tdefaultBranch = main\n")
        cls.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(cls.home),
            "LANG": "C.UTF-8",
            "GIT_CONFIG_GLOBAL": str(gitconfig),
            "GIT_CONFIG_NOSYSTEM": "1",
        }
        # Per-run settings (KBX_RUNTIME, KBX_IMAGE, KBX_DNS, …) apply to the tested kbx.
        for key, value in os.environ.items():
            if (key.startswith("KBX_") and key != "KBX_INTEGRATION") or key == "DOCKER_HOST":
                cls.env[key] = value
        cls.repo = cls.temp / "inttest"
        cls.repo.mkdir()
        cls.git("init", "-q")
        (cls.repo / "README.md").write_text("integration\n")
        cls.git("add", "README.md")
        cls.git("commit", "-q", "-m", "initial")
        cls.name = sandbox_mod.for_project(cls.repo).name
        result = cls.kbx("start")
        if result.returncode != 0:
            cls.tearDownClass()
            raise RuntimeError(f"kbx start failed:\n{result.stdout}\n{result.stderr}")

    @classmethod
    def tearDownClass(cls) -> None:
        cls.kbx("rm", "--yes")
        shutil.rmtree(cls.temp, ignore_errors=True)

    @classmethod
    def git(cls, *args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(cls.repo), *args],
            check=True,
            capture_output=True,
            text=True,
            env={**os.environ, "GIT_CONFIG_GLOBAL": str(cls.temp / "gitconfig"), "GIT_CONFIG_NOSYSTEM": "1"},
        ).stdout.strip()

    @classmethod
    def kbx(cls, *args: str, timeout: float = 900) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(KBX), *args],
            cwd=str(cls.repo),
            env=cls.env,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def sh(self, script: str, user: str = "agent", timeout: float = 300) -> subprocess.CompletedProcess[str]:
        """Run a shell script inside the sandbox."""
        return subprocess.run(
            ["docker", "exec", "-u", user, "-e", "DISPLAY=:0", self.name, "bash", "-lc", script],
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )

    def out(self, script: str, user: str = "agent") -> str:
        result = self.sh(script, user)
        self.assertEqual(result.returncode, 0, f"{script}\n{result.stdout}\n{result.stderr}")
        return result.stdout.strip()
