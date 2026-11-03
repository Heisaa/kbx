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
            '[launcher]\nmemory_budget = "lots"\n',
            '[launcher]\nshm_budget = "0g"\n',
            '[runtime]\nmemory_overhead = "off"\n',
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

    def test_memory_budgets(self) -> None:
        cfg = self.load()
        self.assertEqual(cfg.launcher.memory_budget, "auto")
        self.assertEqual(cfg.launcher.shm_budget, "auto")
        self.assertEqual(cfg.runtime.memory_overhead, "2g")
        cfg = self.load(
            '[launcher]\nmemory_budget = "20g"\nshm_budget = "12g"\n[runtime]\nmemory_overhead = "4g"\n',
            {"KBX_MEMORY_BUDGET": "16G", "KBX_SHM_BUDGET": "off"},
        )
        self.assertEqual(cfg.launcher.memory_budget, "16g")
        self.assertEqual(cfg.launcher.shm_budget, "off")
        self.assertEqual(cfg.runtime.memory_overhead, "4g")

    def test_workspace(self) -> None:
        cfg = self.load()
        self.assertEqual(cfg.workspace.mode, "mount")
        self.assertTrue(cfg.workspace.guard)
        self.assertIn(".husky", cfg.workspace.protect)
        cfg = self.load('[workspace]\nmode = "clone"\nprotect = ["tools/hooks/", ".envrc"]\n')
        self.assertEqual(cfg.workspace.mode, "clone")
        self.assertEqual(cfg.workspace.protect, ("tools/hooks", ".envrc"))
        cfg = self.load('[workspace]\nmode = "clone"\n', {"KBX_WORKSPACE": "mount", "KBX_GUARD": "0"})
        self.assertEqual(cfg.workspace.mode, "mount")
        self.assertFalse(cfg.workspace.guard)
        for text in (
            '[workspace]\nmode = "shared"\n',
            '[workspace]\nprotect = ["/etc/passwd"]\n',
            '[workspace]\nprotect = ["../up"]\n',
            '[workspace]\nprotect = [".git/hooks"]\n',
            "[workspace]\nprotect = [1]\n",
            "[workspace]\nguard = 1\n",
        ):
            with self.subTest(text=text), self.assertRaises(KbxError):
                self.load(text)


class ProfileConfigTest(ConfigTest):
    def setUp(self) -> None:
        super().setUp()
        self.work = self.temp / "work"
        self.work.mkdir()
        (self.temp / "personal").mkdir()

    def profiles(self, extra: str = "", env: dict[str, str] | None = None) -> config.Config:
        return self.load(
            f'[launcher]\nmemory = "8g"\nidle_stop = "5m"\n[workspace]\nmode = "clone"\n'
            f'[profiles.work]\nroots = ["{self.work}", "{self.temp}/elsewhere"]\nmemory = "16g"\n'
            f'private = ["target"]\n{extra}'
            f'[profiles.personal]\nroots = ["{self.temp}/personal"]\n',
            env,
        )

    def test_profiles_inherit_and_override(self) -> None:
        cfg = self.profiles()
        work = cfg.profiles["work"]
        self.assertEqual(work.roots, (self.work.resolve(), self.temp / "elsewhere"))
        self.assertEqual(work.launcher.memory, "16g")
        self.assertEqual(work.launcher.idle_stop, 300)
        self.assertEqual(work.workspace.private, ("target",))
        self.assertEqual(work.workspace.mode, "mount", "a profile always shares its roots")
        self.assertEqual(cfg.profiles["personal"].launcher.memory, "8g")
        effective = cfg.for_profile("work")
        self.assertEqual(effective.launcher.memory, "16g")
        self.assertEqual(effective.network, cfg.network)
        self.assertEqual(cfg.launcher.memory, "8g")

    def test_env_overrides_win_over_profiles(self) -> None:
        cfg = self.profiles(env={"KBX_MEMORY": "4g", "KBX_WORKSPACE": "clone"})
        self.assertEqual(cfg.profiles["work"].launcher.memory, "4g")
        self.assertEqual(cfg.profiles["work"].workspace.mode, "mount")

    def test_profile_for(self) -> None:
        cfg = self.profiles(f'[profiles.inner]\nroots = ["{self.temp}/x"]\n')
        api = self.work / "team" / "api"
        api.mkdir(parents=True)
        found = config.profile_for(cfg, api, {})
        self.assertEqual(found.name if found else None, "work")
        self.assertIsNone(config.profile_for(cfg, self.temp, {}))
        self.assertIsNone(config.profile_for(cfg, api, {"KBX_PROFILE": "none"}))
        forced = config.profile_for(cfg, self.temp, {"KBX_PROFILE": "personal"})
        self.assertEqual(forced.name if forced else None, "personal")
        with self.assertRaises(KbxError) as ctx:
            config.profile_for(cfg, api, {"KBX_PROFILE": "nope"})
        self.assertIn("no such profile", str(ctx.exception))

    def test_profile_rejections(self) -> None:
        bad = {
            "[profiles.work]\nroots = []\n": "at least one directory",
            '[profiles.work]\nroots = ["relative"]\n': "absolute",
            '[profiles.work]\nroots = ["~"]\n': "whole home directory",
            '[profiles.work]\nroots = ["/"]\n': "whole home directory",
            '[profiles.work]\nroots = ["~/.local"]\n': "kbx's own files",
            '[profiles.Work]\nroots = ["/srv/a"]\n': "profile name",
            '[profiles.work]\nroots = ["/srv/a"]\nmode = "clone"\n': "unknown setting",
            '[profiles.work]\nroots = ["/srv/a"]\nmemory = "lots"\n': "memory",
            '[profiles.a]\nroots = ["/srv/a"]\n[profiles.b]\nroots = ["/srv/a/b"]\n': "profiles a and b",
            '[profiles.a]\nroots = ["/srv/a", "/srv/a"]\n': "twice",
        }
        for text, message in bad.items():
            with self.subTest(text=text), self.assertRaises(KbxError) as ctx:
                self.load(text)
            self.assertIn(message, str(ctx.exception))
