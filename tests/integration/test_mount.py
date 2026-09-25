"""Mount mode against a real sandbox: one checkout on both sides, and the guard."""

from __future__ import annotations

import json
import subprocess
import time

from tests.integration.base import SandboxCase


class MountIntegrationTest(SandboxCase):
    def status(self) -> str:
        result = subprocess.run(
            ["docker", "inspect", "-f", "{{.State.Status}}", self.name], capture_output=True, text=True, check=False
        )
        return result.stdout.strip()

    def wait_status(self, wanted: str, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while self.status() != wanted:
            if time.monotonic() > deadline:
                self.fail(f"sandbox is {self.status()}, not {wanted}")
            time.sleep(0.2)

    def test_1_both_sides_commit(self) -> None:
        repo = self.repo
        self.out(f"cd {repo} && echo agent > agent.txt && git add agent.txt && git commit -q -m agent")
        self.assertEqual(self.git("log", "-1", "--format=%s"), "agent")
        self.assertEqual((repo / "agent.txt").stat().st_uid, (repo / "README.md").stat().st_uid)
        (repo / "host.txt").write_text("host\n")
        self.git("add", "host.txt")
        self.git("commit", "-q", "-m", "host")
        self.assertEqual(self.out(f"cd {repo} && git log -1 --format=%s && git status --porcelain"), "host")

    def test_2_guard_reverts_and_pauses(self) -> None:
        marker = self.temp / "PWNED"
        self.out(
            f"cd {self.repo} && printf '#!/bin/sh\\ntouch {marker}\\n' > .git/evil.sh && chmod +x .git/evil.sh && "
            'git config core.fsmonitor "$PWD/.git/evil.sh" && cp .git/evil.sh .git/hooks/pre-commit'
        )
        self.wait_status("paused")
        self.git("status")
        self.git("commit", "-q", "--allow-empty", "-m", "after")
        self.assertFalse(marker.exists(), "host git ran a planted command")
        self.assertNotIn("fsmonitor", self.git("config", "--list"))
        result = self.kbx("resume")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("core.fsmonitor", result.stdout)
        self.assertIn(".git/hooks/pre-commit: added", result.stdout)
        self.assertEqual(self.status(), "running")
        self.assertEqual(self.out("echo alive"), "alive")

    def test_3_ready_and_ids(self) -> None:
        ready = json.loads(self.out("kbx-init status", user="root"))
        self.assertTrue(ready["docker"]["ready"])
        self.assertEqual(self.out("id -u"), str(self.repo.stat().st_uid))
