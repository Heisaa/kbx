from __future__ import annotations

from pathlib import Path

from kbx import config, paths
from kbx.errors import KbxError
from tests.unit.helpers import TempHome


class PathsTest(TempHome):
    def test_defaults_follow_home(self) -> None:
        p = paths.resolve({"HOME": "/h"})
        self.assertEqual(p.config_file, Path("/h/.config/kbx/config.toml"))
        self.assertEqual(p.user_modules, Path("/h/.config/kbx/modules"))
        self.assertEqual(p.stage, Path("/h/.local/share/kbx/stage"))
        self.assertEqual(p.runtime_dir, Path("/h/.local/share/kbx/run"))

    def test_xdg_overrides(self) -> None:
        p = paths.resolve(
            {"HOME": "/h", "XDG_CONFIG_HOME": "/c", "XDG_DATA_HOME": "/d", "XDG_RUNTIME_DIR": "/run/user/7"}
        )
        self.assertEqual(p.config_file, Path("/c/kbx/config.toml"))
        self.assertEqual(p.stage, Path("/d/kbx/stage"))
        self.assertEqual(p.runtime_dir, Path("/run/user/7/kbx"))

    def test_relative_xdg_values_are_ignored(self) -> None:
        p = paths.resolve({"HOME": "/h", "XDG_CONFIG_HOME": "relative", "XDG_RUNTIME_DIR": "rel"})
        self.assertEqual(p.config_dir, Path("/h/.config/kbx"))
        self.assertEqual(p.runtime_dir, Path("/h/.local/share/kbx/run"))

    def test_builtin_modules_are_found_relative_to_the_checkout(self) -> None:
        self.assertTrue((paths.CHECKOUT / "modules" / "clipboard" / "module.toml").is_file())
        self.assertEqual(paths.resolve({"HOME": "/h"}, checkout=Path("/x")).builtin_modules, Path("/x/modules"))


class ConfigTest(TempHome):
    def load(self, text: str | None = None, env: dict[str, str] | None = None) -> config.Config:
        p = paths.resolve({"HOME": str(self.home)})
        if text is not None:
            p.config_dir.mkdir(parents=True, exist_ok=True)
            p.config_file.write_text(text)
        return config.load(p, env or {})

    def test_no_file_gives_defaults(self) -> None:
        cfg = self.load()
        self.assertTrue(cfg.launcher.auto_update)
        self.assertEqual(cfg.launcher.detach_key, "^\\")
        self.assertEqual(cfg.launcher.dns, ("1.1.1.1", "9.9.9.9"))
        self.assertEqual(cfg.network.gateway, "172.30.0.1")
        self.assertEqual(cfg.runtime.name, "kata")
        self.assertEqual(cfg.image.name, "kbx-agent")
        self.assertEqual(cfg.skills.sources, (self.home / ".agents/skills",))

    def test_file_values(self) -> None:
        cfg = self.load(
            '[launcher]\nmemory = "16g"\ncpus = 8\ndns = ["8.8.8.8"]\ndetach_key = "none"\n'
            '[network]\nsubnet = "10.99.0.0/24"\nbridge = "br-x"\n'
            "[modules]\nplaywright = true\n[options.node-toolchain]\nversion = 24\n"
            '[skills]\nsources = ["~/skills", "/abs"]\n'
        )
        self.assertEqual(cfg.launcher.memory, "16g")
        self.assertEqual(cfg.launcher.cpus, 8)
        self.assertEqual(cfg.launcher.dns, ("8.8.8.8",))
        self.assertEqual(cfg.network.gateway, "10.99.0.1")
        self.assertEqual(cfg.modules, {"playwright": True})
        self.assertEqual(cfg.options["node-toolchain"], {"version": 24})
        self.assertEqual(cfg.skills.sources, (self.home / "skills", Path("/abs")))

    def test_env_overrides_launcher(self) -> None:
        cfg = self.load(
            "[launcher]\nremote_control = true\n",
            {"KBX_REMOTE_CONTROL": "false", "KBX_CPUS": "2", "KBX_DNS": "8.8.8.8, 1.0.0.1", "KBX_DEBUG": "1"},
        )
        self.assertFalse(cfg.launcher.remote_control)
        self.assertEqual(cfg.launcher.cpus, 2)
        self.assertEqual(cfg.launcher.dns, ("8.8.8.8", "1.0.0.1"))
        self.assertTrue(cfg.launcher.debug)

    def test_rejections(self) -> None:
        bad = [
            "[launcher]\nauto_updat = true\n",
            '[launcher]\nmemory = "lots"\n',
            "[launcher]\ncpus = 0\n",
            '[launcher]\ndns = ["not-an-ip"]\n',
            '[launcher]\ndetach_key = "ab"\n',
            '[network]\nsubnet = "fd00::/64"\n',
            '[network]\nbridge = "a-very-long-bridge-name"\n',
            '[runtime]\nprivileges = "root"\n',
            '[runtime]\ndocker_storage = "zfs"\n',
            '[modules]\nplaywright = "yes"\n',
            '[skills]\nsources = ["relative/path"]\n',
            "[unknown]\n",
            "not toml = = =",
        ]
        for text in bad:
            with self.subTest(text=text), self.assertRaises(KbxError):
                self.load(text)
        with self.assertRaises(KbxError):
            self.load(None, {"KBX_AUTO_UPDATE": "maybe"})
