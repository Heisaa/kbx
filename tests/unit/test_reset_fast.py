"""modules/codex-reset-fast/reset_fast.py against temporary CODEX_HOMEs."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

SCRIPT = Path(__file__).resolve().parents[2] / "modules" / "codex-reset-fast" / "reset_fast.py"


class ResetFastTest(unittest.TestCase):
    def run_script(self, config: str | None) -> tuple[int, str | None, str]:
        with tempfile.TemporaryDirectory() as home:
            path = Path(home) / "config.toml"
            if config is not None:
                path.write_text(config)
            result = subprocess.run(
                [sys.executable, str(SCRIPT)],
                env=dict(os.environ, CODEX_HOME=home),
                capture_output=True,
                text=True,
                check=False,
            )
            return result.returncode, path.read_text() if path.exists() else None, result.stderr

    def test_removes_root_and_profile_fast(self) -> None:
        status, text, _ = self.run_script(
            '# keep me\nservice_tier = "fast"\nmodel = "x"\n\n[profiles.work]\nservice_tier = "fast"\nmodel = "y"\n\n[tui]\nservice_tier = "fast"\n'
        )
        self.assertEqual(status, 0)
        assert text is not None
        self.assertIn("# keep me", text)
        self.assertNotIn('service_tier = "fast"\nmodel = "x"', text)
        self.assertIn('[profiles.work]\nmodel = "y"', text)
        self.assertIn('[tui]\nservice_tier = "fast"', text, "only root and profiles are Codex's tier")

    def test_leaves_flex_and_missing_file(self) -> None:
        status, text, _ = self.run_script('service_tier = "flex"\n')
        self.assertEqual((status, text), (0, 'service_tier = "flex"\n'))
        self.assertEqual(self.run_script(None)[:2], (0, None))

    def test_multiline_string_is_never_touched(self) -> None:
        config = 'notes = """\nservice_tier = "fast"\n"""\n'
        self.assertEqual(self.run_script(config)[:2], (0, config))

    def test_inline_table_fails_safely(self) -> None:
        config = 'profiles = { work = { service_tier = "fast" } }\n'
        status, text, err = self.run_script(config)
        self.assertNotEqual(status, 0)
        self.assertEqual(text, config)
        self.assertIn("by hand", err)
