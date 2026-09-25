from __future__ import annotations

import contextlib
import io
import os
import subprocess
from pathlib import Path

from kbx import config, paths, sandbox
from kbx.docker import Docker
from kbx.errors import KbxError
from tests.unit.helpers import TempHome


class NamingTest(TempHome):
    def test_sanitize(self) -> None:
        self.assertEqual(sandbox.sanitize("My_Project"), "my-project")
        self.assertEqual(sandbox.sanitize(".sbx"), "sbx")
        self.assertEqual(sandbox.sanitize("Ünïcode Name!"), "ncodename")
        self.assertEqual(sandbox.sanitize("..."), "project")
        self.assertEqual(sandbox.sanitize("x" * 50), "x" * 32)

    def test_name_includes_path_hash(self) -> None:
        a = sandbox.for_project(Path("/work/a/app"))
        b = sandbox.for_project(Path("/work/b/app"))
        self.assertTrue(a.name.startswith("kbx-app-"))
        self.assertNotEqual(a.name, b.name)
        self.assertEqual(a.home_volume, f"{a.name}-home")
        self.assertEqual(a.docker_volume, f"{a.name}-docker")
        self.assertEqual(a.workdir, "/home/agent/work/app")
        self.assertLessEqual(len(a.name), 63)


class CreateArgsTest(TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.paths = paths.resolve({"HOME": str(self.home)})
        self.cfg = config.load(self.paths, {})
        self.sb = sandbox.for_project(self.temp / "proj")

    def test_clone_mode_mounts_only_the_stage_from_the_host(self) -> None:
        args = sandbox.create_args(self.sb, self.cfg, self.paths)
        mounts: list[str] = []
        for index, arg in enumerate(args):
            if arg in ("-v", "--volume", "--mount"):
                mounts.append(args[index + 1])
        binds = [m for m in mounts if m.startswith("/") or "type=bind" in m]
        self.assertEqual(binds, [f"type=bind,source={self.paths.stage},target=/opt/kbx/stage,readonly"])
        named = sorted(m.split(":")[0] for m in mounts if not m.startswith("/") and "type=bind" not in m)
        self.assertEqual(named, [self.sb.docker_volume, self.sb.home_volume])
        # Besides the stage bind mount itself, no host path appears anywhere.
        joined = " ".join(a for a in args if not a.startswith("type=bind,source="))
        for forbidden in ("docker.sock", "SSH_AUTH_SOCK", "--env-file", str(self.home), str(self.sb.project_dir) + ":"):
            self.assertNotIn(forbidden, joined)
        self.assertNotIn("--network=host", joined)
        self.assertNotIn("-p", args)

    def test_mount_mode_mounts_the_checkout_at_its_own_path(self) -> None:
        repo = self.make_repo("checkout")
        sb = sandbox.for_project(repo, "mount")
        self.assertEqual(sb.workdir, str(repo))
        self.assertEqual(sb.clone_dir, "/home/agent/work/checkout")
        args = sandbox.create_args(sb, self.cfg, self.paths)
        binds = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
        self.assertEqual(
            binds,
            [
                f"type=bind,source={self.paths.stage},target=/opt/kbx/stage,readonly",
                f"type=bind,source={sb.project_dir},target={sb.project_dir}",
            ],
        )
        self.assertIn("kbx.workspace=mount", args)
        self.assertIn(f"KBX_HOST_UID={os.getuid()}", args)
        self.assertIn(f"KBX_HOST_GID={os.getgid()}", args)
        self.assertNotIn("docker.sock", " ".join(args))

    def test_check_mountable(self) -> None:
        repo = self.make_repo("ok")
        sandbox.check_mountable(sandbox.for_project(repo, "mount"))
        for path in ("/usr/src/x", "/etc/app", "/opt/kbx/stage", "/home", "/tmp", "/home/agent"):
            with self.subTest(path=path), self.assertRaises(KbxError):
                sandbox.check_mountable(sandbox.Sandbox("kbx-x", "x", Path(path), "mount"))
        odd = self.make_repo("a,b")
        with self.assertRaises(KbxError):
            sandbox.check_mountable(sandbox.for_project(odd, "mount"))
        broken = self.make_repo("broken")
        subprocess.run(["rm", "-rf", str(broken / ".git")], check=True)
        (broken / ".git").write_text("gitdir: /nowhere\n")
        with self.assertRaises(KbxError):
            sandbox.check_mountable(sandbox.for_project(broken, "mount"))

    def test_linked_worktree_mounts_the_repository_directory(self) -> None:
        repo = self.make_repo("main")
        worktree = self.temp / "wt"
        subprocess.run(["git", "-C", str(repo), "worktree", "add", "-q", str(worktree)], check=True)
        self.assertEqual(sandbox.git_dirs(worktree), (repo / ".git" / "worktrees" / "wt", repo / ".git"))
        self.assertEqual(sandbox.git_dirs(repo), (repo / ".git", repo / ".git"))
        sb = sandbox.for_project(worktree, "mount")
        sandbox.check_mountable(sb)
        args = sandbox.create_args(sb, self.cfg, self.paths)
        binds = [args[i + 1] for i, a in enumerate(args) if a == "--mount"]
        self.assertEqual(
            binds[1:],
            [
                f"type=bind,source={worktree},target={worktree}",
                f"type=bind,source={repo}/.git,target={repo}/.git",
            ],
        )
        self.assertIn(f"kbx.gitdir={repo}/.git", args)

    def test_submodule_and_separate_git_dir_checkouts(self) -> None:
        library = self.make_repo("library")
        superproject = self.make_repo("super")
        subprocess.run(
            [
                "git",
                "-C",
                str(superproject),
                "-c",
                "protocol.file.allow=always",
                "submodule",
                "add",
                "-q",
                str(library),
                "lib",
            ],
            check=True,
        )
        module = superproject / ".git" / "modules" / "lib"
        self.assertEqual(sandbox.git_dirs(superproject / "lib"), (module, module))
        self.assertEqual(sandbox.shared_gitdir(sandbox.for_project(superproject / "lib", "mount")), module)
        separate = self.temp / "separate"
        subprocess.run(
            ["git", "init", "-q", "--separate-git-dir", str(self.temp / "store.git"), str(separate)], check=True
        )
        self.assertEqual(sandbox.git_dirs(separate), (self.temp / "store.git", self.temp / "store.git"))

    def test_runtime_network_dns_and_size(self) -> None:
        args = sandbox.create_args(self.sb, self.cfg, self.paths)
        self.assertEqual(args[args.index("--runtime") + 1], "kata")
        self.assertEqual(args[args.index("--network") + 1], "kbx")
        dns = [args[i + 1] for i, a in enumerate(args) if a == "--dns"]
        self.assertEqual(dns, ["1.1.1.1", "9.9.9.9"])
        self.assertEqual(args[args.index("--memory") + 1], "8g")
        self.assertEqual(args[args.index("--cpus") + 1], "4")
        self.assertNotIn("--privileged", args)
        self.assertIn("SYS_ADMIN", args)
        self.assertIn("b 7:* rwm", args)
        self.assertIn(f"SANDBOX_NAME={self.sb.name}", args)
        self.assertIn(f"kbx.project={self.sb.project_dir}", args)
        self.assertIn("/run:rw,exec,mode=755", args)
        self.assertEqual(args[-1], "kbx-agent")

    def test_privileged_mode(self) -> None:
        self.paths.config_dir.mkdir(parents=True)
        self.paths.config_file.write_text('[runtime]\nprivileges = "privileged"\n')
        args = sandbox.create_args(self.sb, config.load(self.paths, {}), self.paths)
        self.assertIn("--privileged", args)
        self.assertNotIn("SYS_ADMIN", args)


class LifecycleTest(TempHome):
    def setUp(self) -> None:
        super().setUp()
        quiet = contextlib.redirect_stdout(io.StringIO())
        quiet.__enter__()
        self.addCleanup(quiet.__exit__, None, None, None)
        self.paths = paths.resolve({"HOME": str(self.home)})
        self.cfg = config.load(self.paths, {})
        self.docker = Docker()
        self.sb = sandbox.for_project(self.temp / "proj")
        self.paths.stage.mkdir(parents=True)

    def test_create_requires_image(self) -> None:
        with self.assertRaises(KbxError) as ctx:
            sandbox.ensure_created(self.docker, self.sb, self.cfg, self.paths)
        self.assertIn("kbx build", str(ctx.exception))

    def test_create_start_ready(self) -> None:
        self.add_image()
        self.assertTrue(sandbox.ensure_created(self.docker, self.sb, self.cfg, self.paths))
        self.assertFalse(sandbox.ensure_created(self.docker, self.sb, self.cfg, self.paths))
        state = self.docker_state()
        self.assertIn("kbx", state["networks"])
        self.assertEqual(state["networks"]["kbx"]["Options"]["com.docker.network.bridge.name"], "br-kbx")
        ready = sandbox.ensure_running(self.docker, self.sb, timeout=5)
        self.assertEqual(ready["boot_id"], "x")
        self.assertEqual(self.docker_state()["containers"][self.sb.name]["State"]["Status"], "running")

    def test_readiness_timeout_shows_log(self) -> None:
        self.add_image()
        sandbox.ensure_created(self.docker, self.sb, self.cfg, self.paths)
        self.behave(ready=False)
        with self.assertRaises(KbxError) as ctx:
            sandbox.ensure_running(self.docker, self.sb, timeout=1)
        self.assertIn("not ready", str(ctx.exception))

    def test_foreign_container_is_refused(self) -> None:
        state = self.docker_state()
        state["containers"][self.sb.name] = {
            "State": {"Status": "running"},
            "Config": {"Labels": {"kbx.project": "/elsewhere"}},
        }
        self.write_state(state)
        with self.assertRaises(KbxError):
            sandbox.state(self.docker, self.sb)

    def test_network_mismatch_is_an_error(self) -> None:
        state = self.docker_state()
        state["networks"]["kbx"] = {"IPAM": {"Config": [{"Subnet": "10.0.0.0/24"}]}, "Options": {}}
        self.write_state(state)
        with self.assertRaises(KbxError):
            sandbox.ensure_network(self.docker, self.cfg)

    def test_firewall_check_fails_closed(self) -> None:
        self.add_image()
        sandbox.ensure_created(self.docker, self.sb, self.cfg, self.paths)
        sandbox.ensure_running(self.docker, self.sb, timeout=5)
        sandbox.firewall_check(self.docker, self.sb, self.cfg, {})  # dropped → fine
        self.behave(firewall=3)
        with self.assertRaises(KbxError) as ctx:
            sandbox.firewall_check(self.docker, self.sb, self.cfg, {})
        self.assertIn("kbx-firewall.service", str(ctx.exception))
        probe = [c for c in self.calls() if "python3" in c][-1]
        self.assertIn("172.30.0.1", probe)
        sandbox.firewall_check(self.docker, self.sb, self.cfg, {"KBX_UNSAFE_NO_FIREWALL_CHECK": "1"})

    def test_stop_and_pause(self) -> None:
        self.add_image()
        sandbox.ensure_created(self.docker, self.sb, self.cfg, self.paths)
        sandbox.ensure_running(self.docker, self.sb, timeout=5)
        self.assertTrue(sandbox.pause(self.docker, self.sb))
        self.assertEqual(sandbox.state(self.docker, self.sb), "paused")
        with self.assertRaises(KbxError) as ctx:
            sandbox.ensure_running(self.docker, self.sb, timeout=5)
        self.assertIn("kbx resume", str(ctx.exception))
        sandbox.stop(self.docker, self.sb)
        self.assertEqual(sandbox.state(self.docker, self.sb), "exited")
        self.assertIn(["unpause", self.sb.name], self.calls())
