"""`kbx host`: the generated policy, environment and checks (no Claude needed)."""

from __future__ import annotations

import json
import os
from pathlib import Path

from kbx import config, host, paths
from kbx.errors import KbxError
from tests.unit.helpers import TempHome


class HostTest(TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo("proj")
        self.paths = paths.resolve(os.environ)

    def cfg(self, text: str = "") -> config.Config:
        self.paths.config_dir.mkdir(parents=True, exist_ok=True)
        self.paths.config_file.write_text(text)
        return config.load(self.paths, {})

    def test_settings(self) -> None:
        cfg = self.cfg('[host]\nallowed_domains = ["pypi.org"]\nallow_write = ["~/scratch"]\n')
        found = host.settings(self.repo, self.home, cfg)
        perms, box = found["permissions"], found["sandbox"]
        self.assertEqual(
            (perms["defaultMode"], perms["disableAutoMode"], perms["disableBypassPermissionsMode"]),
            ("manual", "disable", "disable"),
        )
        self.assertTrue(box["enabled"] and box["failIfUnavailable"])
        self.assertFalse(box["allowUnsandboxedCommands"] or box["autoAllowBashIfSandboxed"])
        self.assertEqual(box["network"], {"allowedDomains": ["pypi.org"]})
        files = box["filesystem"]
        self.assertEqual(files["denyRead"][0], str(self.home))
        self.assertIn(f"/run/user/{os.getuid()}", files["denyRead"])
        self.assertEqual(files["allowRead"][:2], [str(self.repo), str(self.home / ".local/bin")])
        self.assertEqual(files["allowWrite"], [str(self.repo), str(self.home / "scratch")])
        for rel in (".git", ".husky", ".vscode", ".claude", ".envrc"):
            self.assertIn(str(self.repo / rel), files["denyWrite"])
        self.assertNotIn(str(self.repo / ".vscode/settings.json"), files["denyWrite"], "inside .vscode already")
        self.assertIn(f"Edit(/{self.repo}/.git/**)", perms["deny"])

    def test_linked_worktree_protects_the_shared_git_dir(self) -> None:
        tree = self.temp / "tree"
        from tests.unit.helpers import git

        git(self.repo, "worktree", "add", "-q", str(tree))
        deny = host.settings(tree, self.home, self.cfg())["sandbox"]["filesystem"]["denyWrite"]
        self.assertIn(str(self.repo / ".git"), deny)
        self.assertIn(str(tree / ".git"), deny)

    def test_environment_is_scrubbed(self) -> None:
        env = {"PATH": "/bin", "HOME": "/h", "TERM": "xterm", "AWS_SECRET_ACCESS_KEY": "x", "HTTPS_PROXY": "p"}
        self.assertEqual(
            host.environment(env, self.cfg(), self.paths),
            {
                "PATH": "/bin",
                "HOME": "/h",
                "TERM": "xterm",
                "CLAUDE_CONFIG_DIR": str(host.config_dir(self.paths)),
                "DISABLE_AUTOUPDATER": "1",
                "DISABLE_UPDATES": "1",
                "DISABLE_INSTALLATION_CHECKS": "1",
            },
        )
        passed = host.environment(env, self.cfg('[host]\nenv = ["HTTPS_PROXY"]\n'), self.paths)
        self.assertEqual(passed["HTTPS_PROXY"], "p")

    def test_claude_args(self) -> None:
        self.assertEqual(host.claude_args(resume=None, continue_=False, model=None, prompt=None), [])
        self.assertEqual(host.claude_args(resume="", continue_=False, model=None, prompt=None), ["--resume"])
        self.assertEqual(
            host.claude_args(resume="abc", continue_=False, model="opus", prompt="--settings x"),
            ["--resume", "abc", "--model", "opus", "--", "--settings x"],
        )

    def test_onboarding_keeps_state(self) -> None:
        state = self.temp / "state"
        state.mkdir()
        (state / ".claude.json").write_text(json.dumps({"userID": "u"}))
        host.skip_onboarding(state, self.repo)
        data = json.loads((state / ".claude.json").read_text())
        self.assertEqual(data["userID"], "u")
        self.assertTrue(data["projects"][str(self.repo)]["hasTrustDialogAccepted"])
        self.assertEqual((state / ".claude.json").stat().st_mode & 0o777, 0o600)

    def test_preflight(self) -> None:
        tools = self.temp / "tools"
        tools.mkdir()
        with self.assertRaisesRegex(KbxError, "bwrap and socat"):
            host.preflight({"PATH": str(tools)}, self.paths)
        for name, script in (("bwrap", "exit 0"), ("socat", "exit 0")):
            (tools / name).write_text(f"#!/bin/sh\n{script}\n")
            (tools / name).chmod(0o755)
        with self.assertRaisesRegex(KbxError, "gpg"):
            host.preflight({"PATH": str(tools)}, self.paths)  # a download is due: its signature needs gpg
        (tools / "bwrap").write_text("#!/bin/sh\necho 'bwrap: No permissions to create new namespace' >&2; exit 1\n")
        (tools / "gpg").write_text("#!/bin/sh\n")
        (tools / "gpg").chmod(0o755)
        with self.assertRaisesRegex(KbxError, "user namespaces"):
            host.preflight({"PATH": f"{tools}:/usr/bin:/bin"}, self.paths)

    def test_outermost(self) -> None:
        a, b, c = Path("/p/.vscode"), Path("/p/.vscode/settings.json"), Path("/p/.git")
        self.assertEqual(host.outermost([b, a, c, a]), [a, c])

    def test_config_errors(self) -> None:
        for text in ('[host]\nallowed_domains = ["https://x.org"]\n', '[host]\nenv = ["A B"]\n', "[host]\nx = 1\n"):
            with self.assertRaises(KbxError):
                self.cfg(text)
