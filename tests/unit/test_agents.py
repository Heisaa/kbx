from __future__ import annotations

import unittest

from kbx import agents, config, modules, paths, session
from kbx.errors import KbxError
from tests.unit.helpers import TempHome


class RemoteControlTest(unittest.TestCase):
    def test_insertion(self) -> None:
        rc = agents.insert_remote_control
        self.assertEqual(rc([]), ["--remote-control"])
        self.assertEqual(rc(["--resume"]), ["--resume", "--remote-control"])
        self.assertEqual(rc(["-p", "x", "--", "prompt"]), ["-p", "x", "--remote-control", "--", "prompt"])
        self.assertEqual(rc(["--remote-control", "phone"]), ["--remote-control", "phone"])
        self.assertEqual(rc(["--remote-control=phone", "--", "x"]), ["--remote-control=phone", "--", "x"])
        self.assertEqual(rc(["--", "--remote-control"]), ["--remote-control", "--", "--remote-control"])
        # Never before a positional argument: the flag takes an optional name.
        self.assertEqual(rc(["resume", "literal $() argument"]), ["resume", "literal $() argument", "--remote-control"])


class CommandTest(TempHome):
    def resolved(self, text: str = "") -> tuple[config.Config, list[modules.Resolved]]:
        p = paths.resolve({"HOME": str(self.home)})
        p.config_dir.mkdir(parents=True, exist_ok=True)
        p.config_file.write_text(text)
        cfg = config.load(p, {})
        return cfg, modules.resolve(modules.discover(p), cfg)

    def test_claude(self) -> None:
        cfg, res = self.resolved()
        self.assertEqual(
            agents.command(agents.get("claude"), ["--resume"], cfg, res), ["claude", "--resume", "--remote-control"]
        )
        cfg, res = self.resolved("[launcher]\nremote_control = false\ndebug = true\n")
        self.assertEqual(agents.command(agents.get("claude"), ["x"], cfg, res), ["claude", "--debug", "x"])

    def test_codex(self) -> None:
        cfg, res = self.resolved()
        argv = agents.command(agents.get("codex"), ["resume"], cfg, res)
        self.assertEqual(argv[0], "codex")
        self.assertEqual(argv[-1], "resume")
        self.assertIn("--search", argv)
        self.assertIn("forced_login_method=chatgpt", argv)
        cfg, res = self.resolved("[launcher]\ncodex_search = false\n[modules]\ncodex-chatgpt-auth = false\n")
        self.assertEqual(agents.command(agents.get("codex"), [], cfg, res), ["codex"])

    def test_pi(self) -> None:
        cfg, res = self.resolved()
        self.assertEqual(agents.command(agents.get("pi"), ["-c"], cfg, res), ["pi", "-c"])

    def test_unknown_agent(self) -> None:
        with self.assertRaises(KbxError):
            agents.get("emacs")


class DtachTest(unittest.TestCase):
    def test_start_or_attach(self) -> None:
        self.assertEqual(
            session.dtach_start("claude", "^\\", ["claude", "--remote-control"]),
            ["dtach", "-A", "/run/kbx/sessions/claude.sock", "-e", "^\\", "-r", "winch", "claude", "--remote-control"],
        )

    def test_detach_key_disabled(self) -> None:
        self.assertEqual(
            session.dtach_attach("pi", "none"), ["dtach", "-a", "/run/kbx/sessions/pi.sock", "-E", "-r", "winch"]
        )
        self.assertIn("-E", session.dtach_start("pi", "", ["pi"]))
