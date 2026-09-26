from __future__ import annotations

import textwrap
from pathlib import Path

from kbx import config, image, modules, paths
from kbx.errors import KbxError
from tests.unit.helpers import TempHome


def write_module(root: Path, name: str, toml: str, files: dict[str, str] | None = None) -> Path:
    path = root / name
    path.mkdir(parents=True)
    (path / "module.toml").write_text(textwrap.dedent(toml))
    for rel, content in (files or {}).items():
        target = path / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
        if rel in ("build.sh", "service", "start.sh"):
            target.chmod(0o755)
    return path


class ModulesTest(TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.paths = paths.resolve({"HOME": str(self.home)})
        self.paths.user_modules.mkdir(parents=True)

    def cfg(self, text: str = "") -> config.Config:
        self.paths.config_file.write_text(text)
        return config.load(self.paths, {})

    def test_builtin_modules_validate(self) -> None:
        found = modules.discover(self.paths)
        self.assertEqual(
            set(found),
            {
                "claude-statusline",
                "clipboard",
                "codex-chatgpt-auth",
                "codex-reset-fast",
                "codex-statusline",
                "node-toolchain",
                "notify",
                "playwright",
            },
        )
        resolved = modules.resolve(found, self.cfg())
        self.assertEqual([r.name for r in resolved], ["clipboard", "codex-chatgpt-auth", "notify"])
        issues = [i for i in modules.check(found, resolved) if i.level == "error"]
        self.assertEqual(issues, [])

    def test_all_builtins_enabled_have_no_conflicts(self) -> None:
        found = modules.discover(self.paths)
        text = "[modules]\n" + "".join(f"{name} = true\n" for name in found)
        resolved = modules.resolve(found, self.cfg(text))
        self.assertEqual(len(resolved), len(found))
        self.assertEqual([i for i in modules.check(found, resolved) if i.level == "error"], [])

    def test_example_templates_validate(self) -> None:
        for entry in sorted((paths.CHECKOUT / "examples" / "modules").iterdir()):
            if entry.is_dir():
                module = modules.load_module(entry, "user")
                self.assertFalse(module.default)
                modules.seed_units(module)

    def test_user_module_replaces_builtin(self) -> None:
        write_module(self.paths.user_modules, "clipboard", 'description = "mine"\ndefault = false\n')
        found = modules.discover(self.paths)
        self.assertEqual(found["clipboard"].source, "user")
        self.assertEqual(found["clipboard"].description, "mine")
        self.assertNotIn("clipboard", [r.name for r in modules.resolve(found, self.cfg())])

    def test_options_defaults_and_overrides(self) -> None:
        found = modules.discover(self.paths)
        resolved = modules.resolve(
            found, self.cfg("[modules]\nnode-toolchain = true\n[options.node-toolchain]\nversion = 24\n")
        )
        node = next(r for r in resolved if r.name == "node-toolchain")
        self.assertEqual(node.options, {"version": "24", "apt": "", "npm": ""})
        self.assertEqual(node.option_env()["KBX_OPT_NODE_TOOLCHAIN_VERSION"], "24")

    def test_options_reject_bad_values(self) -> None:
        found = modules.discover(self.paths)
        for text in (
            '[options.node-toolchain]\nversion = "latest; rm -rf /"\n',
            '[options.node-toolchain]\napt = "curl && evil"\n',
            '[options.node-toolchain]\nnope = "1"\n',
            '[options.nonexistent]\nx = "1"\n',
            "[modules]\nnonexistent = true\n",
            "[options.playwright]\nversion = true\n",
        ):
            with self.subTest(text=text), self.assertRaises(KbxError):
                modules.resolve(found, self.cfg(text))

    def test_bool_and_enum_options(self) -> None:
        write_module(
            self.paths.user_modules,
            "opts",
            """\
            description = "options"
            default = true
            [options]
            flag = { default = false }
            mode = { default = "a", enum = ["a", "b"] }
            """,
        )
        found = modules.discover(self.paths)
        resolved = modules.resolve(found, self.cfg('[options.opts]\nflag = true\nmode = "b"\n'))
        self.assertEqual(next(r for r in resolved if r.name == "opts").options, {"flag": "true", "mode": "b"})
        with self.assertRaises(KbxError):
            modules.resolve(found, self.cfg('[options.opts]\nmode = "c"\n'))
        with self.assertRaises(KbxError):
            modules.resolve(found, self.cfg('[options.opts]\nflag = "yes"\n'))

    def test_invalid_manifests(self) -> None:
        cases = {
            "nodesc": "default = true\n",
            "nodefault": 'description = "x"\n',
            "extra": 'description = "x"\ndefault = true\ncolour = "red"\n',
            "badservice": 'description = "x"\ndefault = true\n[service]\nuser = "nobody"\n',
            "nofile": 'description = "x"\ndefault = true\n[service]\nuser = "agent"\n',
            "badagent": 'description = "x"\ndefault = true\n[start]\nbefore_launch = ["emacs"]\n',
            "badpattern": 'description = "x"\ndefault = true\n[options]\nv = { default = "1", pattern = "[" }\n',
            "baddefault": 'description = "x"\ndefault = true\n[options]\nv = { default = "x", pattern = "^[0-9]+$" }\n',
            "badenv": 'description = "x"\ndefault = true\n[env]\nKBX_X = "1"\n',
        }
        for name, text in cases.items():
            path = write_module(self.temp / "bad", name, text)
            with self.subTest(name=name), self.assertRaises(KbxError):
                modules.load_module(path, "user")
        with self.assertRaises(KbxError):
            modules.load_module(write_module(self.temp / "bad", "Upper", 'description = "x"\ndefault = true\n'), "user")

    def test_seed_conflicts_and_enforce(self) -> None:
        write_module(
            self.paths.user_modules,
            "a",
            'description = "a"\ndefault = true\n[seed]\nenforce = [".claude/settings.json:model", ".x.toml:nope"]\n',
            {"home/.claude/settings.json": '{"model": "a", "env": {"X": "1"}}', "home/.claude/CLAUDE.md": "a"},
        )
        write_module(
            self.paths.user_modules,
            "b",
            'description = "b"\ndefault = true\n',
            {"home/.claude/settings.json": '{"env": {"X": "2"}, "other": 1}', "home/.claude/CLAUDE.md": "b"},
        )
        write_module(
            self.paths.user_modules,
            "c",
            'description = "c"\ndefault = true\n',
            {"home/.claude/settings.json": '{"other2": {}}'},
        )
        found = modules.discover(self.paths)
        resolved = modules.resolve(found, self.cfg())
        errors = [i.message for i in modules.check(found, resolved) if i.level == "error"]
        self.assertIn("seed conflict: a and b both seed .claude/settings.json:env.X", errors)
        self.assertIn("seed conflict: a and b both seed .claude/CLAUDE.md", errors)
        self.assertTrue(any("'.x.toml:nope' is not a seeded key" in e for e in errors))
        self.assertFalse(any(" c " in e for e in errors), errors)

    def test_invalid_seed_json_is_reported(self) -> None:
        write_module(
            self.paths.user_modules,
            "broken",
            'description = "x"\ndefault = true\n',
            {"home/.claude/settings.json": "{nope"},
        )
        found = modules.discover(self.paths)
        errors = [i.message for i in modules.check(found, modules.resolve(found, self.cfg())) if i.level == "error"]
        self.assertTrue(any("invalid JSON" in e for e in errors))

    def test_non_executable_scripts_and_secrets(self) -> None:
        path = write_module(
            self.paths.user_modules,
            "leaky",
            'description = "x"\ndefault = false\n',
            {
                "start.sh": "#!/bin/sh\ntrue\n",
                "home/.config/token.txt": "ghp_" + "a" * 36 + "\n",
                "home/.ssh/id_ed25519": "x",
            },
        )
        (path / "start.sh").chmod(0o644)
        found = modules.discover(self.paths)
        issues = modules.check(found, modules.resolve(found, self.cfg()))
        messages = [i.message for i in issues]
        self.assertTrue(any("start.sh is not executable" in m for m in messages))
        self.assertTrue(any("token.txt looks like a credential" in m for m in messages))
        self.assertTrue(any("id_ed25519 looks like a credential" in m for m in messages))


class DockerfileTest(TempHome):
    def test_one_layer_per_enabled_module_in_name_order(self) -> None:
        p = paths.resolve({"HOME": str(self.home)})
        p.user_modules.mkdir(parents=True)
        write_module(
            p.user_modules, "aaa-first", 'description = "x"\ndefault = true\n', {"build.sh": "#!/bin/sh\ntrue\n"}
        )
        write_module(p.user_modules, "zzz-nobuild", 'description = "x"\ndefault = true\n')
        p.config_file.write_text("[modules]\nplaywright = true\nnode-toolchain = true\n")
        found = modules.discover(p)
        resolved = modules.resolve(found, config.load(p, {}))
        text = image.render(p.checkout, resolved)
        layers = [line.split()[3] for line in text.splitlines() if line.startswith("# --- module:")]
        self.assertEqual(layers, ["aaa-first", "clipboard", "node-toolchain", "playwright"])
        self.assertIn("ARG KBX_OPT_NODE_TOOLCHAIN_VERSION", text)
        self.assertIn("RUN --mount=type=bind,source=modules/playwright,target=/opt/kbx/build-module", text)
        self.assertTrue(text.index("module: playwright") < text.index("COPY --from=agents"))
        self.assertTrue(text.startswith((p.checkout / "image" / "Dockerfile.core").read_text().rstrip("\n")))

    def test_module_hash_tracks_build_inputs(self) -> None:
        p = paths.resolve({"HOME": str(self.home)})
        p.user_modules.mkdir(parents=True)
        path = write_module(
            p.user_modules,
            "tool",
            'description = "x"\ndefault = true\n[options]\nv = { default = "1" }\n',
            {"build.sh": "#!/bin/sh\necho 1\n"},
        )
        found = modules.discover(p)
        first = image.modules_hash(modules.resolve(found, config.load(p, {})))
        p.config_file.write_text('[options.tool]\nv = "2"\n')
        second = image.modules_hash(modules.resolve(found, config.load(p, {})))
        (path / "build.sh").write_text("#!/bin/sh\necho 2\n")
        third = image.modules_hash(modules.resolve(modules.discover(p), config.load(p, {})))
        self.assertEqual(len({first, second, third}), 3)
        (path / "home").mkdir()
        (path / "home" / ".x").write_text("seed changes need no rebuild")
        self.assertEqual(third, image.modules_hash(modules.resolve(modules.discover(p), config.load(p, {}))))
