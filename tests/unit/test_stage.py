from __future__ import annotations

import json
import os
import tomllib

from kbx import config, modules, paths, stage
from kbx_sandbox import seed
from tests.unit.helpers import TempHome


class StageTest(TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.paths = paths.resolve({"HOME": str(self.home)})
        self.skills = self.home / ".agents" / "skills"
        (self.skills / "review").mkdir(parents=True)
        (self.skills / "review" / "SKILL.md").write_text("v1\n")

    def restage(self, text: str = "") -> list[str]:
        self.paths.config_dir.mkdir(parents=True, exist_ok=True)
        self.paths.config_file.write_text(text)
        cfg = config.load(self.paths, {})
        resolved = modules.resolve(modules.discover(self.paths), cfg)
        return stage.build(self.paths, cfg, resolved)

    def test_contents(self) -> None:
        self.restage("[modules]\nplaywright = true\ncodex-reset-fast = true\n")
        root = self.paths.stage
        self.assertTrue((root / "modules/clipboard/service").is_file())
        self.assertTrue(os.access(root / "modules/clipboard/service", os.X_OK))
        self.assertFalse((root / "modules/clipboard/build.sh").exists(), "build.sh never reaches the stage")
        self.assertFalse((root / "modules/playwright/build").exists(), "build/ never reaches the stage")
        self.assertTrue((root / "modules/codex-chatgpt-auth/home/.codex/config.toml").is_file())
        self.assertTrue((root / "modules/codex-reset-fast/reset_fast.py").is_file())
        self.assertFalse((root / "modules/node-toolchain").exists(), "disabled modules are not staged")
        self.assertEqual((root / "skills/review/SKILL.md").read_text(), "v1\n")
        data = json.loads((root / "config.json").read_text())
        names = [m["name"] for m in data["modules"]]
        self.assertEqual(
            names, ["agent-permissions", "clipboard", "codex-chatgpt-auth", "codex-reset-fast", "notify", "playwright"]
        )
        reset = next(m for m in data["modules"] if m["name"] == "codex-reset-fast")
        self.assertEqual(reset["start"], {"user": "agent", "before_launch": ["codex"]})
        auth = next(m for m in data["modules"] if m["name"] == "codex-chatgpt-auth")
        self.assertIn(".codex/config.toml:forced_login_method", auth["enforce"])
        self.assertEqual(data["env"]["PLAYWRIGHT_BROWSERS_PATH"], "/opt/playwright-browsers")
        self.assertEqual(data["dns"], ["1.1.1.1", "9.9.9.9"])

    def test_restage_keeps_directory_inodes(self) -> None:
        self.restage()
        root = self.paths.stage
        before = {rel: os.stat(root / rel).st_ino for rel in (".", "skills", "skills/review", "modules")}
        (self.skills / "review" / "SKILL.md").write_text("v2\n")
        (self.skills / "new").mkdir()
        (self.skills / "new" / "SKILL.md").write_text("new\n")
        self.restage()
        after = {rel: os.stat(root / rel).st_ino for rel in before}
        self.assertEqual(before, after)
        self.assertEqual((root / "skills/review/SKILL.md").read_text(), "v2\n")
        self.assertTrue((root / "skills/new/SKILL.md").is_file())

    def test_permission_defaults_seed_before_each_agent_launch(self) -> None:
        self.restage()
        home = self.temp / "sandbox-home"
        home.mkdir()
        for prefix in (".claude/", ".codex/"):
            self.assertEqual(
                seed.main(["--stage", str(self.paths.stage), "--home", str(home), "--only-prefix", prefix, "--quiet"]),
                0,
            )
        claude = json.loads((home / ".claude/settings.json").read_text())
        self.assertEqual(claude["permissions"]["defaultMode"], "bypassPermissions")
        codex = tomllib.loads((home / ".codex/config.toml").read_text())
        self.assertEqual(codex["approval_policy"], "never")
        self.assertEqual(codex["sandbox_mode"], "danger-full-access")
        self.assertEqual(codex["forced_login_method"], "chatgpt")

    def test_permission_defaults_preserve_existing_settings(self) -> None:
        self.restage()
        home = self.temp / "sandbox-home"
        (home / ".claude").mkdir(parents=True)
        (home / ".codex").mkdir()
        (home / ".claude/settings.json").write_text('{"permissions": {"defaultMode": "plan"}, "model": "opus"}')
        (home / ".codex/config.toml").write_text('sandbox_mode = "workspace-write"\n')
        self.assertEqual(seed.main(["--stage", str(self.paths.stage), "--home", str(home), "--quiet"]), 0)
        claude = json.loads((home / ".claude/settings.json").read_text())
        self.assertEqual(claude["permissions"]["defaultMode"], "plan")
        self.assertEqual(claude["model"], "opus")
        codex = tomllib.loads((home / ".codex/config.toml").read_text())
        self.assertEqual(codex["sandbox_mode"], "workspace-write")
        self.assertEqual(codex["approval_policy"], "never")

    def test_removed_files_disappear(self) -> None:
        self.restage("[modules]\nclaude-statusline = true\n")
        self.assertTrue((self.paths.stage / "modules/claude-statusline").is_dir())
        (self.skills / "review" / "SKILL.md").unlink()
        (self.skills / "review").rmdir()
        self.restage()
        self.assertFalse((self.paths.stage / "modules/claude-statusline").exists())
        self.assertFalse((self.paths.stage / "skills/review").exists())

    def test_unchanged_files_are_not_rewritten(self) -> None:
        self.restage()
        target = self.paths.stage / "skills/review/SKILL.md"
        inode = os.stat(target).st_ino
        self.restage()
        self.assertEqual(os.stat(target).st_ino, inode)

    def test_symlinks_are_dereferenced(self) -> None:
        outside = self.temp / "outside"
        outside.mkdir()
        (outside / "SKILL.md").write_text("linked\n")
        (self.skills / "linked").symlink_to(outside)
        (self.skills / "review" / "ref.md").symlink_to(outside / "SKILL.md")
        (self.skills / "review" / "broken").symlink_to(self.temp / "missing")
        warnings = self.restage()
        root = self.paths.stage
        self.assertFalse((root / "skills/linked").is_symlink())
        self.assertEqual((root / "skills/linked/SKILL.md").read_text(), "linked\n")
        self.assertFalse((root / "skills/review/ref.md").is_symlink())
        self.assertFalse((root / "skills/review/broken").exists())
        self.assertTrue(any("broken" in w for w in warnings))
        for current, dirs, files in os.walk(root):
            for name in dirs + files:
                self.assertFalse(os.path.islink(os.path.join(current, name)), name)

    def test_first_skill_source_wins(self) -> None:
        second = self.temp / "team-skills"
        (second / "review").mkdir(parents=True)
        (second / "review" / "SKILL.md").write_text("team\n")
        (second / "other").mkdir()
        (second / "other" / "SKILL.md").write_text("other\n")
        warnings = self.restage(f'[skills]\nsources = ["~/.agents/skills", "{second}", "/does/not/exist"]\n')
        self.assertEqual((self.paths.stage / "skills/review/SKILL.md").read_text(), "v1\n")
        self.assertTrue((self.paths.stage / "skills/other/SKILL.md").is_file())
        self.assertTrue(any("shadowed" in w for w in warnings))

    def test_skills_off(self) -> None:
        self.restage()
        self.restage("[launcher]\nshared_skills = false\n")
        self.assertEqual(list((self.paths.stage / "skills").iterdir()), [])
        self.assertFalse(json.loads((self.paths.stage / "config.json").read_text())["skills"])

    def test_type_changes(self) -> None:
        self.restage()
        # A file where a directory now belongs (and vice versa) is replaced.
        target = self.paths.stage / "skills/review"
        for child in target.iterdir():
            child.unlink()
        target.rmdir()
        target.write_text("stale file")
        self.restage()
        self.assertTrue((self.paths.stage / "skills/review/SKILL.md").is_file())
