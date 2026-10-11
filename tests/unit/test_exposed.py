"""Mount mode: the warning about secret-looking files the agent can read."""

from __future__ import annotations

from kbx import exposed
from tests.unit.helpers import TempHome
from tests.unit.helpers import git as run_git


class LooksSecretTest(TempHome):
    def test_names(self) -> None:
        for rel in [".env", ".env.local", "config/prod.env", ".envrc", "certs/dev.pem", "tls.key", "id_ed25519",
                    "credentials.json", "gcp-credentials.json", "service-account.json", "secrets/", ".npmrc",
                    "infra/terraform.tfstate", "prod.tfvars", "config/master.key", ".aws/", "SECRETS.yaml"]:  # fmt: skip
            self.assertTrue(exposed.looks_secret(rel), rel)
        for rel in [".env.example", ".env.sample", ".env.template", ".env.dist", "credentials.example.json",
                    "README.md", "node_modules/", "keys.py", "environment.ts", "secret_santa.py"]:  # fmt: skip
            self.assertFalse(exposed.looks_secret(rel), rel)


class SecretFilesTest(TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo()

    def test_untracked_and_ignored_count_tracked_do_not(self) -> None:
        (self.repo / ".gitignore").write_text(".env\nsecrets/\nnode_modules/\n")
        (self.repo / "config").mkdir()
        (self.repo / "config" / ".env.production").write_text("KEY=x\n")
        (self.repo / "config" / "app.toml").write_text("")
        run_git(self.repo, "add", ".gitignore", "config/app.toml")
        run_git(self.repo, "commit", "-q", "-m", "config")
        (self.repo / ".env").write_text("KEY=x\n")
        (self.repo / ".env.example").write_text("KEY=\n")
        (self.repo / "secrets").mkdir()
        (self.repo / "secrets" / "token").write_text("x\n")
        (self.repo / "node_modules" / "pkg").mkdir(parents=True)
        (self.repo / "node_modules" / "pkg" / "test.pem").write_text("")
        (self.repo / "tracked.pem").write_text("")
        run_git(self.repo, "add", "tracked.pem")
        run_git(self.repo, "commit", "-q", "-m", "fixture")
        self.assertEqual(exposed.secret_files(self.repo), [".env", "config/.env.production", "secrets/"])

    def test_warning_lists_and_truncates(self) -> None:
        self.assertIsNone(exposed.warning(self.repo))
        for i in range(12):
            (self.repo / f"k{i:02}.pem").write_text("")
        message = exposed.warning(self.repo)
        assert message is not None
        self.assertIn("    k00.pem", message)
        self.assertNotIn("k11.pem", message)
        self.assertIn("… and 2 more", message)
        self.assertIn('mode = "clone"', message)

    def test_not_a_repository(self) -> None:
        self.assertEqual(exposed.secret_files(self.temp), [])
