from __future__ import annotations

import contextlib
import io
import json
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any

from kbx_sandbox import seed


class SeedTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="kbx-seed-"))
        self.addCleanup(shutil.rmtree, self.temp)
        self.stage = self.temp / "stage"
        self.home = self.temp / "home"
        self.home.mkdir()
        self.modules: list[dict[str, Any]] = []

    def module(
        self, name: str, files: dict[str, str], enforce: list[str] | None = None, copy: list[str] | None = None
    ) -> None:
        root = self.stage / "modules" / name / "home"
        shutil.rmtree(root, ignore_errors=True)
        for rel, content in files.items():
            (root / rel).parent.mkdir(parents=True, exist_ok=True)
            (root / rel).write_text(content)
        self.modules = [m for m in self.modules if m["name"] != name]
        self.modules.append({"name": name, "home": True, "enforce": enforce or [], "copy": copy or []})
        self.modules.sort(key=lambda m: m["name"])
        (self.stage / "config.json").write_text(json.dumps({"modules": self.modules}))

    def run_seed(self, *args: str) -> tuple[int, str]:
        out = io.StringIO()
        err = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            status = seed.main(["--stage", str(self.stage), "--home", str(self.home), *args])
        return status, out.getvalue() + err.getvalue()

    def read_json(self, rel: str) -> Any:
        return json.loads((self.home / rel).read_text())

    def write(self, rel: str, text: str) -> None:
        (self.home / rel).parent.mkdir(parents=True, exist_ok=True)
        (self.home / rel).write_text(text)

    # --- JSON ---

    def test_json_three_way(self) -> None:
        settings = ".claude/settings.json"
        self.module("m", {settings: '{"model": "a", "keep": 1, "gone": 2, "nested": {"x": 1}}'})
        self.assertEqual(self.run_seed()[0], 0)
        # 1. absent, never seeded → written
        self.assertEqual(self.read_json(settings), {"model": "a", "keep": 1, "gone": 2, "nested": {"x": 1}})
        # the user changes one key and deletes another
        data = self.read_json(settings)
        data["keep"] = 99
        del data["gone"]
        data["mine"] = True
        self.write(settings, json.dumps(data))
        # the module's defaults change
        self.module("m", {settings: '{"model": "b", "keep": 2, "gone": 3, "nested": {"x": 2, "y": 1}}'})
        self.assertEqual(self.run_seed()[0], 0)
        result = self.read_json(settings)
        self.assertEqual(result["model"], "b")  # 2. untouched and default changed → updated
        self.assertEqual(result["keep"], 99)  # 3. user-owned → left alone
        self.assertNotIn("gone", result)  # 4. deleted → stays deleted
        self.assertEqual(result["nested"], {"x": 2, "y": 1})
        self.assertTrue(result["mine"])
        # a second run changes nothing
        before = (self.home / settings).read_text()
        self.run_seed()
        self.assertEqual((self.home / settings).read_text(), before)

    def test_json_enforce(self) -> None:
        settings = ".claude/settings.json"
        self.module("m", {settings: '{"model": "a", "other": 1}'}, enforce=[f"{settings}:model"])
        self.write(settings, '{"model": "user", "other": 5}')
        self.run_seed()
        self.assertEqual(self.read_json(settings), {"model": "a", "other": 5})
        self.write(settings, '{"other": 5}')
        self.run_seed()
        self.assertEqual(self.read_json(settings)["model"], "a")

    def test_json_adopts_equal_values(self) -> None:
        settings = ".claude/settings.json"
        self.write(settings, '{"model": "a"}')
        self.module("m", {settings: '{"model": "a"}'})
        self.run_seed()
        self.module("m", {settings: '{"model": "b"}'})
        self.run_seed()
        self.assertEqual(self.read_json(settings), {"model": "b"})

    def test_json_scalar_where_object_expected_is_users(self) -> None:
        settings = ".claude/settings.json"
        self.write(settings, '{"env": "custom"}')
        self.module("m", {settings: '{"env": {"A": "1"}}'})
        self.run_seed()
        self.assertEqual(self.read_json(settings), {"env": "custom"})
        self.module("m", {settings: '{"env": {"A": "1"}}'}, enforce=[f"{settings}:env.A"])
        self.run_seed()
        self.assertEqual(self.read_json(settings), {"env": {"A": "1"}})

    def test_invalid_json_fails_module_without_writing(self) -> None:
        self.write(".claude/settings.json", "{broken")
        self.module("a-bad", {".claude/settings.json": '{"model": "x"}', ".claude/CLAUDE.md": "hi"})
        self.module("b-good", {".codex/AGENTS.md": "ok"})
        status, output = self.run_seed()
        self.assertEqual(status, 1)
        self.assertIn("invalid JSON", output)
        self.assertEqual((self.home / ".claude/settings.json").read_text(), "{broken")
        self.assertFalse((self.home / ".claude/CLAUDE.md").exists(), "a failed module writes nothing")
        self.assertEqual((self.home / ".codex/AGENTS.md").read_text(), "ok")

    def test_non_object_json_root_fails(self) -> None:
        self.write(".claude/settings.json", "[1, 2]")
        self.module("m", {".claude/settings.json": '{"model": "x"}'})
        self.assertEqual(self.run_seed()[0], 1)
        self.assertEqual((self.home / ".claude/settings.json").read_text(), "[1, 2]")

    def test_two_modules_merge_into_one_file(self) -> None:
        self.module("a", {".claude/settings.json": '{"model": "a"}'})
        self.module("b", {".claude/settings.json": '{"statusLine": {"type": "command"}}'})
        self.assertEqual(self.run_seed()[0], 0)
        self.assertEqual(self.read_json(".claude/settings.json"), {"model": "a", "statusLine": {"type": "command"}})

    # --- TOML ---

    def test_toml_three_way_keeps_comments(self) -> None:
        config = ".codex/config.toml"
        self.write(config, '# my notes\nmodel = "o3"  # pinned\n\n[tui]\ntheme = "dark"\n')
        self.module("m", {config: 'approval = "never"\ngone = 1\n[tui]\nstatus_line = ["a"]\n'})
        self.run_seed()
        text = (self.home / config).read_text()
        self.assertIn("# my notes", text)
        self.assertIn('model = "o3"  # pinned', text)
        self.assertIn('status_line = ["a"]', text)
        self.assertIn('approval = "never"', text)
        text = text.replace('approval = "never"', 'approval = "on-request"').replace("gone = 1\n", "")
        self.write(config, text)
        self.module("m", {config: 'approval = "always"\ngone = 2\n[tui]\nstatus_line = ["a", "b"]\n'})
        self.run_seed()
        text = (self.home / config).read_text()
        self.assertIn('approval = "on-request"', text)
        self.assertNotIn("gone", text)
        self.assertIn('status_line = ["a", "b"]', text)
        self.assertIn("# my notes", text)

    def test_toml_enforce(self) -> None:
        config = ".codex/config.toml"
        self.module(
            "auth",
            {config: 'forced_login_method = "chatgpt"\nmodel_provider = "openai"\n'},
            enforce=[f"{config}:forced_login_method", f"{config}:model_provider"],
        )
        self.write(config, 'model_provider = "azure"  # mine\n[profiles.x]\nmodel = "y"\n')
        self.run_seed()
        text = (self.home / config).read_text()
        self.assertIn('forced_login_method = "chatgpt"', text)
        self.assertIn('model_provider = "openai"', text)
        self.assertIn("[profiles.x]", text)

    def test_invalid_toml_is_untouched(self) -> None:
        self.write(".codex/config.toml", "this is = = not toml")
        self.module("m", {".codex/config.toml": "a = 1\n"})
        self.assertEqual(self.run_seed()[0], 1)
        self.assertEqual((self.home / ".codex/config.toml").read_text(), "this is = = not toml")

    def test_comment_only_toml_seeds_nothing(self) -> None:
        self.module("m", {".codex/config.toml": '# model = "x"\n'})
        self.run_seed()
        self.assertFalse((self.home / ".codex/config.toml").exists())

    # --- whole files ---

    def test_whole_file_three_way_and_mode(self) -> None:
        self.module("m", {".claude/CLAUDE.md": "v1\n", ".local/bin/tool": "#!/bin/sh\n"})
        (self.stage / "modules/m/home/.local/bin/tool").chmod(0o755)
        self.run_seed()
        self.assertEqual((self.home / ".claude/CLAUDE.md").read_text(), "v1\n")
        self.assertTrue(os.access(self.home / ".local/bin/tool", os.X_OK))
        self.module("m", {".claude/CLAUDE.md": "v2\n", ".local/bin/tool": "#!/bin/sh\n"})
        self.run_seed()
        self.assertEqual((self.home / ".claude/CLAUDE.md").read_text(), "v2\n")  # updated
        self.write(".claude/CLAUDE.md", "mine\n")
        self.module("m", {".claude/CLAUDE.md": "v3\n", ".local/bin/tool": "#!/bin/sh\n"})
        self.run_seed()
        self.assertEqual((self.home / ".claude/CLAUDE.md").read_text(), "mine\n")  # user-owned
        (self.home / ".claude/CLAUDE.md").unlink()
        self.run_seed()
        self.assertFalse((self.home / ".claude/CLAUDE.md").exists())  # deleted stays deleted

    def test_copy_treats_json_as_whole_file(self) -> None:
        self.module("m", {".config/x.json": '{"a": 1}'}, copy=[".config/x.json"])
        self.write(".config/x.json", '{"b": 2}')
        self.run_seed()
        self.assertEqual(self.read_json(".config/x.json"), {"b": 2})  # user's whole file kept
        self.module("m", {".config/x.json": '{"a": 1}'}, copy=[".config/x.json"], enforce=[".config/x.json"])
        self.run_seed()
        self.assertEqual(self.read_json(".config/x.json"), {"a": 1})

    # --- safety ---

    def test_symlink_out_of_home_is_refused(self) -> None:
        outside = self.temp / "outside"
        outside.mkdir()
        (self.home / ".claude").symlink_to(outside)
        self.module("m", {".claude/settings.json": '{"a": 1}'})
        status, output = self.run_seed()
        self.assertEqual(status, 1)
        self.assertIn("outside", output)
        self.assertEqual(list(outside.iterdir()), [])

    def test_symlink_inside_home_is_followed(self) -> None:
        (self.home / "dotfiles").mkdir()
        (self.home / "dotfiles/settings.json").write_text('{"mine": 1}')
        (self.home / ".claude").mkdir()
        (self.home / ".claude/settings.json").symlink_to(self.home / "dotfiles/settings.json")
        self.module("m", {".claude/settings.json": '{"a": 1}'})
        self.assertEqual(self.run_seed()[0], 0)
        self.assertTrue((self.home / ".claude/settings.json").is_symlink())
        self.assertEqual(json.loads((self.home / "dotfiles/settings.json").read_text()), {"mine": 1, "a": 1})

    # --- commands ---

    def test_dry_run_writes_nothing(self) -> None:
        self.module("m", {".claude/settings.json": '{"a": 1}', ".claude/CLAUDE.md": "x\n"})
        status, output = self.run_seed("--dry-run")
        self.assertEqual(status, 0)
        self.assertIn("+++ b/.claude/settings.json", output)
        self.assertIn('"a": 1', output)
        self.assertFalse((self.home / ".claude").exists())
        self.assertFalse((self.home / seed.STATE_REL).exists())

    def test_status(self) -> None:
        self.module("m", {".claude/settings.json": '{"a": 1, "b": 1, "c": 1, "d": 1}'})
        self.run_seed()
        self.write(".claude/settings.json", '{"b": 1, "c": 5, "d": 1}')
        self.module("m", {".claude/settings.json": '{"a": 1, "b": 1, "c": 1, "d": 2, "e": 1}'})
        _, output = self.run_seed("--status")
        lines = {line.split()[-1]: line for line in output.splitlines() if ".json:" in line}
        self.assertIn("deleted by you", lines[".claude/settings.json:a"])
        self.assertIn("default", lines[".claude/settings.json:b"])
        self.assertIn("user-owned", lines[".claude/settings.json:c"])
        self.assertIn("update pending", lines[".claude/settings.json:d"])
        self.assertIn("not yet seeded", lines[".claude/settings.json:e"])

    def test_reset(self) -> None:
        self.module("m", {".claude/settings.json": '{"a": 1}', ".claude/CLAUDE.md": "v1\n"})
        self.module("other", {".codex/AGENTS.md": "o\n"})
        self.run_seed()
        self.write(".claude/settings.json", '{"a": 2, "mine": 1}')
        (self.home / ".claude/CLAUDE.md").unlink()
        self.write(".codex/AGENTS.md", "changed\n")
        self.assertEqual(self.run_seed("--reset", "m")[0], 0)
        self.assertEqual(self.read_json(".claude/settings.json"), {"a": 1, "mine": 1})
        self.assertEqual((self.home / ".claude/CLAUDE.md").read_text(), "v1\n")
        self.assertEqual((self.home / ".codex/AGENTS.md").read_text(), "changed\n")
        self.assertEqual(self.run_seed("--reset", "missing")[0], 1)

    def test_only_prefix(self) -> None:
        self.module("m", {".claude/CLAUDE.md": "c\n", ".codex/AGENTS.md": "x\n"})
        self.run_seed("--only-prefix", ".codex/")
        self.assertTrue((self.home / ".codex/AGENTS.md").exists())
        self.assertFalse((self.home / ".claude/CLAUDE.md").exists())

    def test_report_and_corrupt_state(self) -> None:
        self.module("m", {".claude/CLAUDE.md": "c\n"})
        (self.home / seed.STATE_REL).parent.mkdir(parents=True)
        (self.home / seed.STATE_REL).write_text("{nope")
        report = self.temp / "report.json"
        self.assertEqual(self.run_seed("--report", str(report))[0], 0)
        self.assertEqual(
            json.loads(report.read_text()), {"m": {"ok": True, "error": None, "written": [".claude/CLAUDE.md"]}}
        )
        self.assertTrue((self.home / seed.STATE_REL).with_suffix(".corrupt").exists())

    def test_decide_table(self) -> None:
        d = seed.decide
        self.assertEqual(d(False, None, None, "D", False), ("new", True))
        self.assertEqual(d(True, "D", None, "D", False), ("adopted", False))
        self.assertEqual(d(True, "U", None, "D", False), ("user", False))
        self.assertEqual(d(False, None, "L", "D", False), ("deleted", False))
        self.assertEqual(d(True, "L", "L", "D", False), ("updated", True))
        self.assertEqual(d(True, "L", "L", "L", False), ("default", False))
        self.assertEqual(d(True, "U", "L", "D", False), ("user", False))
        self.assertEqual(d(True, "U", "L", "D", True), ("enforced", True))
        self.assertEqual(d(True, "D", "L", "D", True), ("enforced", False))
