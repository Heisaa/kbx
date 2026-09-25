"""Git through bundles against a real sandbox, including the malicious-repo test."""

from __future__ import annotations

from tests.integration.base import SandboxCase


class GitIntegrationTest(SandboxCase):
    def test_fetch_and_malicious_config(self) -> None:
        clone = "~/work/inttest"
        self.out(
            f"cd {clone} && git checkout -q -b agent/feature && echo hi > feature.txt && "
            "git add feature.txt && git commit -q -m feature"
        )
        marker = self.temp / "PWNED"
        payload = f"#!/bin/sh\\ntouch {marker}\\n"
        self.out(
            f"cd {clone} && printf '{payload}' > .git/evil.sh && chmod +x .git/evil.sh && "
            "cp .git/evil.sh .git/hooks/post-merge && cp .git/evil.sh .git/post-merge && "
            'git config core.fsmonitor "$PWD/.git/evil.sh" && git config core.hooksPath "$PWD/.git"'
        )
        result = self.kbx("fetch")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kbx/agent/feature", result.stdout)
        self.git("merge", "-q", "--ff-only", "kbx/agent/feature")
        self.git("status")
        self.assertTrue((self.repo / "feature.txt").exists())
        self.assertFalse(marker.exists())

    def test_sync_and_rm_guard(self) -> None:
        self.git("checkout", "-q", "-b", "hostside")
        (self.repo / "host.txt").write_text("h\n")
        self.git("add", "host.txt")
        self.git("commit", "-q", "-m", "host")
        self.git("checkout", "-q", "main")
        self.assertEqual(self.kbx("sync").returncode, 0)
        self.assertTrue(self.out("cd ~/work/inttest && git rev-parse host/hostside"))
        self.out("cd ~/work/inttest && git checkout -q -b unfetched && git commit -q --allow-empty -m wip")
        result = self.kbx("rm")  # no terminal: never confirms
        self.assertEqual(result.returncode, 1)
        self.assertIn("unfetched", result.stdout)
        self.assertIn("Nothing deleted", result.stdout)
        self.assertEqual(self.out("echo alive"), "alive")
