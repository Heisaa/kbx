"""Nothing from the host's home, SSH agent or Docker daemon reaches the sandbox."""

from __future__ import annotations

import os

from tests.integration.base import SandboxCase


class SecretsTest(SandboxCase):
    def test_environment(self) -> None:
        env = self.out("env")
        self.assertNotIn("SSH_AUTH_SOCK", env)
        self.assertNotIn(str(self.home), env)
        self.assertNotIn(os.environ.get("USER", "\0"), env.replace("USER=agent", ""))

    def test_filesystem(self) -> None:
        self.assertEqual(self.out("ls /home"), "agent")
        self.assertEqual(
            self.out(
                "find / -xdev \\( -name 'id_rsa*' -o -name 'id_ed25519*' -o -name 'id_ecdsa*' \\) 2>/dev/null | head"
            ),
            "",
        )
        # The Docker socket inside is the sandbox's own daemon, never the host's.
        self.assertEqual(self.out("docker info --format '{{.Name}}'"), self.name)

    def test_mounts(self) -> None:
        mounts = self.out("findmnt -rn -o TARGET,SOURCE")
        for line in mounts.splitlines():
            target, _, source = line.partition(" ")
            if str(self.home) in source:
                self.assertEqual(target, "/opt/kbx/stage", line)
        self.assertNotIn(str(self.repo), mounts)
        self.assertIn("/opt/kbx/stage", mounts)
