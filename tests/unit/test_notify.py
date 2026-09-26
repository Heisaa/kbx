"""The sandbox side of notifications: hooks → states, session status, the follow stream, logins."""

from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from kbx_sandbox import login, notify, session

SESSION = {"KBX_SESSION": "claude"}


class Base(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = Path(tempfile.mkdtemp(prefix="kbx-notify-"))
        self.addCleanup(shutil.rmtree, self.temp)
        self.states = self.temp / "agents"
        self.states.mkdir()
        self.sessions = self.temp / "sessions"
        self.sessions.mkdir()

    def hook(self, event: dict[str, object], agent: str = "claude", env: dict[str, str] | None = None) -> None:
        notify.hook(agent, json.dumps(event), env if env is not None else {"KBX_SESSION": agent}, self.states)

    def state(self, agent: str = "claude") -> dict[str, object]:
        return json.loads((self.states / f"{agent}.json").read_text())

    def events(self) -> list[dict[str, object]]:
        path = self.states / "events.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def live_session(self, agent: str) -> socket.socket:
        """A listening socket, as a dtach master keeps one."""
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        server.bind(str(self.sessions / f"{agent}.sock"))
        server.listen(1)
        os.chmod(self.sessions / f"{agent}.sock", 0o600)  # dtach's mode while no client is attached
        self.addCleanup(server.close)
        return server


class HookTest(Base):
    def test_claude_events(self) -> None:
        self.hook({"hook_event_name": "UserPromptSubmit", "prompt": "fix it"})
        self.assertEqual(self.state()["state"], "working")
        self.hook({"hook_event_name": "PostToolUse", "tool_name": "Bash"})
        self.assertEqual(len(self.events()), 1, "PostToolUse writes only on a change")
        self.hook({"hook_event_name": "Notification", "notification_type": "permission_prompt", "message": "Bash?"})
        self.assertEqual((self.state()["state"], self.state()["message"]), ("waiting", "Bash?"))
        self.hook({"hook_event_name": "PostToolUse", "tool_name": "Bash"})
        self.assertEqual(self.state()["state"], "working", "back to work after an approval")
        self.hook({"hook_event_name": "Stop", "last_assistant_message": "Done.\n\nAll   tests pass."})
        self.assertEqual((self.state()["state"], self.state()["message"]), ("done", "Done. All tests pass."))
        self.hook({"hook_event_name": "Notification", "notification_type": "idle_prompt", "message": "waiting"})
        self.assertEqual(self.state()["state"], "done", "idle_prompt repeats Stop")
        self.assertEqual([e["state"] for e in self.events()], ["working", "waiting", "working", "done"])
        self.hook({"hook_event_name": "SessionEnd", "reason": "exit"})
        self.assertFalse((self.states / "claude.json").exists())

    def test_older_notification_without_type(self) -> None:
        self.hook({"hook_event_name": "Notification", "message": "Claude is waiting for your input"})
        self.assertFalse((self.states / "claude.json").exists())
        self.hook({"hook_event_name": "Notification", "message": "Claude needs your permission to use Bash"})
        self.assertEqual(self.state()["state"], "waiting")

    def test_only_sessions_kbx_started(self) -> None:
        self.hook({"hook_event_name": "Stop"}, env={})
        self.hook({"hook_event_name": "Stop"}, env={"KBX_SESSION": "codex"})
        self.assertEqual(self.events(), [])

    def test_codex(self) -> None:
        self.hook({"type": "agent-turn-complete", "last-assistant-message": "Refactored."}, agent="codex")
        self.assertEqual((self.state("codex")["state"], self.state("codex")["message"]), ("done", "Refactored."))
        self.hook({"type": "approval-requested"}, agent="codex")
        self.assertEqual(self.state("codex")["state"], "waiting")
        self.hook({"type": "something-new"}, agent="codex")
        self.assertEqual(len(self.events()), 2)

    def test_long_message_is_cut_and_log_starts_over(self) -> None:
        self.hook({"hook_event_name": "Stop", "last_assistant_message": "x" * 1000})
        self.assertEqual(len(str(self.state()["message"])), notify.MAX_MESSAGE)
        with mock.patch.object(notify, "MAX_EVENTS", 10):
            self.hook({"hook_event_name": "UserPromptSubmit"})
        self.assertEqual([e["state"] for e in self.events()], ["working"])

    def test_main_never_fails_or_prints(self) -> None:
        env = {**os.environ, "KBX_SESSION": "claude", "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
        code = (
            "import sys; from kbx_sandbox import notify; notify.STATES = notify.EVENTS = None; sys.exit(notify.main())"
        )
        result = subprocess.run(
            [sys.executable, "-c", code, "hook", "claude"],
            input="not json",
            capture_output=True,
            text=True,
            env=env,
            check=False,
        )
        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, "", ""))


class SessionStatusTest(Base):
    def test_state_attached_and_stale(self) -> None:
        self.live_session("claude")
        self.live_session("codex")
        (self.sessions / "pi.sock").touch()  # stale: no master
        notify.record("claude", "waiting", "Bash?", self.states)
        old = self.states / "codex.json"
        old.write_text(json.dumps({"agent": "codex", "state": "done", "at": 1.0}))  # an earlier session
        os.chmod(self.sessions / "claude.sock", 0o700)  # dtach: a client is attached
        found = session.status(self.sessions, self.states)
        self.assertEqual(sorted(found), ["claude", "codex"])
        self.assertTrue(found["claude"]["attached"])
        self.assertEqual((found["claude"]["state"], found["claude"]["message"]), ("waiting", "Bash?"))
        self.assertEqual(found["codex"], {"attached": False})


class FollowTest(Base):
    def test_follow_streams_new_events_and_sessions(self) -> None:
        self.live_session("claude")
        notify.record("claude", "working", "", self.states)  # before follow: not replayed
        script = (
            "import sys; from pathlib import Path; from kbx_sandbox import notify, session;"
            f"session.SESSIONS = Path({str(self.sessions)!r}); session.STATES = Path({str(self.states)!r});"
            f"notify.follow(Path({str(self.states)!r}), tick=0.3, poll=0.05)"
        )
        env = {**os.environ, "PYTHONPATH": str(Path(__file__).resolve().parents[2])}
        process = subprocess.Popen([sys.executable, "-c", script], stdout=subprocess.PIPE, text=True, env=env)
        self.addCleanup(process.kill)
        assert process.stdout is not None
        first = json.loads(process.stdout.readline())
        self.assertEqual(first["type"], "sessions")
        self.assertIn("claude", first["sessions"])
        time.sleep(0.2)
        notify.record("claude", "done", "ok", self.states)
        lines = [json.loads(process.stdout.readline()) for _ in range(2)]
        event = next(line for line in lines if line["type"] == "event")
        self.assertEqual((event["agent"], event["state"], event["attached"]), ("claude", "done", False))


class LoginTest(unittest.TestCase):
    def test_pi(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="kbx-login-"))
        self.addCleanup(shutil.rmtree, home)
        self.assertFalse(login.pi(home)["ok"])
        (home / ".pi/agent").mkdir(parents=True)
        (home / ".pi/agent/auth.json").write_text(json.dumps({"openai": {"key": "secret"}, "anthropic": {}}))
        self.assertEqual(login.pi(home), {"ok": True, "detail": "anthropic, openai"})

    def test_claude_parses_auth_status(self) -> None:
        answer = subprocess.CompletedProcess(
            [], 0, json.dumps({"loggedIn": True, "authMethod": "claude.ai", "subscriptionType": "max"}), ""
        )
        with mock.patch.object(login, "_run", return_value=answer):
            self.assertEqual(login.claude(), {"ok": True, "detail": "claude.ai (max)"})
        with mock.patch.object(login, "_run", return_value=None):
            self.assertIsNone(login.claude()["ok"])

    def test_main_selects_agents(self) -> None:
        checks = {"claude": lambda: {"ok": True, "detail": "a"}, "codex": lambda: {"ok": False, "detail": "b"}}
        with mock.patch.dict(login.CHECKS, checks), mock.patch("sys.stdout") as out:
            self.assertEqual(login.main(["codex"]), 0)
        self.assertEqual(json.loads(out.write.call_args_list[0].args[0]), {"codex": {"ok": False, "detail": "b"}})
        self.assertEqual(login.main(["nope"]), 2)


if __name__ == "__main__":
    unittest.main()
