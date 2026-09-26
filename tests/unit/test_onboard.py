from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path

from kbx_sandbox import onboard

WORKDIR = "/home/me/proj"


class OnboardTest(unittest.TestCase):
    def setUp(self) -> None:
        self.home = Path(tempfile.mkdtemp(prefix="kbx-onboard-"))
        self.addCleanup(shutil.rmtree, self.home)

    def run_main(self, *args: str) -> tuple[int, str]:
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            status = onboard.main([*args, "--home", str(self.home)])
        return status, err.getvalue()

    def claude_json(self) -> dict[str, object]:
        return json.loads((self.home / ".claude.json").read_text())

    def test_claude_fresh_home(self) -> None:
        self.assertEqual(self.run_main("claude", WORKDIR), (0, ""))
        self.assertEqual(
            self.claude_json(),
            {"hasCompletedOnboarding": True, "projects": {WORKDIR: {"hasTrustDialogAccepted": True}}},
        )
        self.assertEqual(os.stat(self.home / ".claude.json").st_mode & 0o777, 0o600)

    def test_claude_keeps_other_state(self) -> None:
        path = self.home / ".claude.json"
        path.write_text(json.dumps({"userID": "u", "projects": {"/other": {"x": 1}, WORKDIR: {"lastCost": 2}}}))
        path.chmod(0o640)
        self.assertTrue(onboard.claude(self.home, WORKDIR))
        data = self.claude_json()
        self.assertEqual(data["userID"], "u")
        expected = {"/other": {"x": 1}, WORKDIR: {"lastCost": 2, "hasTrustDialogAccepted": True}}
        self.assertEqual(data["projects"], expected)
        self.assertEqual(os.stat(path).st_mode & 0o777, 0o640)
        self.assertFalse(onboard.claude(self.home, WORKDIR), "second run changes nothing")

    def test_claude_invalid_json_is_left_alone(self) -> None:
        (self.home / ".claude.json").write_text("{broken")
        status, err = self.run_main("claude", WORKDIR)
        self.assertEqual(status, 1)
        self.assertIn("cannot read", err)
        self.assertEqual((self.home / ".claude.json").read_text(), "{broken")

    def test_codex_trusts_workdir_and_keeps_formatting(self) -> None:
        path = self.home / ".codex" / "config.toml"
        path.parent.mkdir()
        path.write_text('# mine\nmodel = "o3"  # pinned\n')
        self.assertEqual(self.run_main("codex", WORKDIR), (0, ""))
        text = path.read_text()
        self.assertTrue(text.startswith('# mine\nmodel = "o3"  # pinned\n'))
        self.assertIn(f'[projects."{WORKDIR}"]\ntrust_level = "trusted"\n', text)
        self.assertNotIn("[projects]\n", text)

    def test_codex_keeps_an_existing_trust_level(self) -> None:
        path = self.home / ".codex" / "config.toml"
        path.parent.mkdir()
        path.write_text(f'[projects."{WORKDIR}"]\ntrust_level = "untrusted"\n')
        self.assertFalse(onboard.codex(self.home, WORKDIR))
        self.assertIn('trust_level = "untrusted"', path.read_text())


if __name__ == "__main__":
    unittest.main()
