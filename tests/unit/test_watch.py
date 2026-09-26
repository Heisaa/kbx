"""The host watcher (notifications, idle stop), `kbx diff` output, and their dashboard parts."""

from __future__ import annotations

import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from kbx import config, dash, paths, review, sandbox, status, watch
from kbx.errors import KbxError
from tests.unit.helpers import TempHome


def event(**values: Any) -> watch.Event | None:
    return watch.parse_event({"agent": "claude", "state": "done", "message": "ok", "attached": False, **values})


class ParseTest(unittest.TestCase):
    def test_event_validation(self) -> None:
        self.assertEqual(event(), watch.Event("claude", "done", "ok", False))
        self.assertIsNone(event(agent="evil"))
        self.assertIsNone(event(state="rm -rf"))
        self.assertEqual(event(message=["x"]).message, "")  # type: ignore[union-attr]
        self.assertEqual(event(message="a\x1b]0;title\x07b\nc").message, "a?]0;title?b c")  # type: ignore[union-attr]
        self.assertFalse(event(attached="yes").attached)  # type: ignore[union-attr]

    def test_notification_text(self) -> None:
        found = event(state="waiting", message="<a href='x'>click</a> & more")
        assert found is not None
        title, body = watch.notification(found, Path("/home/me/api"))
        self.assertEqual(title, "claude needs you · api")
        self.assertEqual(body, "&lt;a href='x'&gt;click&lt;/a&gt; &amp; more")
        empty = event(message="")
        assert empty is not None
        self.assertEqual(watch.notification(empty, Path("/p")), ("claude is done · p", "finished its turn"))


class NotifierTest(unittest.TestCase):
    def sent(self, mode: str, events: list[watch.Event | None], times: list[float] | None = None) -> list[str]:
        titles: list[str] = []
        notifier = watch.Notifier(mode, Path("/p"))
        with mock.patch.object(watch, "send", side_effect=lambda title, body: titles.append(title) or True):
            for index, item in enumerate(events):
                assert item is not None
                notifier.handle(item, (times or [])[index] if times else float(index))
        return titles

    def test_modes(self) -> None:
        both = [event(attached=True), event(state="waiting", attached=False), event(state="working")]
        self.assertEqual(self.sent("detached", both), ["claude needs you · p"])
        self.assertEqual(self.sent("always", both), ["claude is done · p", "claude needs you · p"])
        self.assertEqual(self.sent("off", both), [])

    def test_repeats_are_dropped_for_a_while(self) -> None:
        same = [event(), event(), event()]
        self.assertEqual(len(self.sent("always", same, [0.0, 10.0, 100.0])), 2)
        self.assertEqual(len(self.sent("always", [event(), event(message="new")])), 2)

    def test_a_flood_is_capped(self) -> None:
        flood = [event(message=str(n)) for n in range(20)]
        self.assertEqual(len(self.sent("always", flood, [n * 0.5 for n in range(20)])), watch.MAX_PER_MINUTE)


class IdleTest(unittest.TestCase):
    def setUp(self) -> None:
        self.proc = Path(tempfile.mkdtemp(prefix="kbx-proc-"))
        self.addCleanup(shutil.rmtree, self.proc)

    def process(self, pid: int, *argv: str) -> None:
        (self.proc / str(pid)).mkdir()
        (self.proc / str(pid) / "cmdline").write_bytes(b"\0".join(a.encode() for a in argv) + b"\0")

    def test_interactive_execs(self) -> None:
        self.process(10, "docker", "exec", "-i", "-t", "-u", "agent", "kbx-p-1", "bash", "-l")
        self.process(11, "/usr/bin/docker", "exec", "-it", "kbx-p-1", "sh")
        self.process(12, "docker", "exec", "-u", "agent", "kbx-p-1", "kbx-notify", "follow")  # no tty
        self.process(13, "docker", "exec", "-i", "-t", "kbx-other", "bash")
        self.process(14, "python3", "docker", "exec", "-t", "kbx-p-1")
        (self.proc / "self").mkdir()
        self.assertEqual(watch.interactive_execs("kbx-p-1", self.proc), 2)

    def test_due(self) -> None:
        idle = watch.Idle(limit=100, since=0)
        self.assertFalse(idle.due(500, shells=0), "sessions unknown: not idle")
        idle.sessions = {"claude": {}}
        self.assertFalse(idle.due(1000, shells=0))
        idle.sessions = {}
        self.assertFalse(idle.due(1050, shells=0))
        self.assertFalse(idle.due(1200, shells=1), "a shell keeps it busy")
        self.assertTrue(idle.due(1300, shells=0))
        self.assertFalse(watch.Idle(limit=0, since=0, sessions={}).due(10**9, shells=0), "0 = never")


class StubDocker:
    def __init__(self, state: str = "running") -> None:
        self.status = state
        self.stopped = False

    def inspect(self, kind: str, name: str) -> dict[str, Any]:
        labels = {sandbox.LABEL_PROJECT: "/home/me/api", sandbox.LABEL_WORKSPACE: "clone"}
        return {"State": {"Status": "exited" if self.stopped else self.status}, "Config": {"Labels": labels}}

    def run(self, args: list[str], **kwargs: Any) -> None:
        if args[:1] == ["stop"]:
            self.stopped = True


class WatcherTest(TempHome):
    def watcher(self, docker: StubDocker, idle: str = "1h") -> watch.Watcher:
        p = paths.resolve(os.environ)
        cfg = config.load(p, {"KBX_IDLE_STOP": idle, "KBX_NOTIFY": "off"})
        return watch.Watcher(p, "kbx-api-1", docker, cfg)  # type: ignore[arg-type]

    def test_idle_stop(self) -> None:
        docker = StubDocker()
        found = self.watcher(docker)
        found.line(b'{"type": "sessions", "sessions": {}}')
        with (
            mock.patch.object(watch, "interactive_execs", return_value=0),
            mock.patch.object(watch.time, "monotonic") as clock,
        ):
            clock.return_value = found.idle.since + 60
            self.assertTrue(found.check())
            clock.return_value = found.idle.since + 3600
            self.assertFalse(found.check(), "stopped, so the watcher ends")
        self.assertTrue(docker.stopped)

    def test_not_while_paused_or_after_a_guard_alert(self) -> None:
        docker = StubDocker("paused")
        found = self.watcher(docker)
        found.idle.sessions = {}
        with (
            mock.patch.object(watch, "interactive_execs", return_value=0),
            mock.patch.object(watch.time, "monotonic") as clock,
        ):
            clock.return_value = found.idle.since + 7200
            self.assertTrue(found.check())
            docker.status = "running"
            clock.return_value += 3599
            self.assertTrue(found.check(), "the pause counted as activity")
            alert = watch.guard.Guard(found.paths, "kbx-api-1").alert_file
            alert.parent.mkdir(parents=True)
            alert.write_text("{}")
            clock.return_value += 7200
            self.assertTrue(found.check(), "the guard's alert waits for the user")
        self.assertFalse(docker.stopped)

    def test_ends_when_the_sandbox_stops(self) -> None:
        self.assertFalse(self.watcher(StubDocker("exited")).check())

    def test_not_started_when_off(self) -> None:
        p = paths.resolve(os.environ)
        cfg = config.load(p, {"KBX_IDLE_STOP": "off", "KBX_NOTIFY": "off"})
        self.assertFalse(watch.wanted(cfg))
        watch.ensure(p, sandbox.for_project(Path("/x")), cfg, Path("/nonexistent"))
        self.assertFalse(list(p.runtime_dir.glob("watch-*")) if p.runtime_dir.exists() else [])


class ConfigTest(unittest.TestCase):
    def test_durations(self) -> None:
        self.assertEqual(config.parse_duration("4h", "x"), 14400)
        self.assertEqual(config.parse_duration("90m", "x"), 5400)
        self.assertEqual(config.parse_duration("45", "x"), 45)
        self.assertEqual(config.parse_duration(600, "x"), 600)
        self.assertEqual(config.parse_duration("off", "x"), 0)
        for bad in ("4 hours", "-1h", True, 1.5):
            with self.assertRaises(KbxError):
                config.parse_duration(bad, "x")


class ReviewTest(unittest.TestCase):
    def test_clean_line(self) -> None:
        self.assertEqual(review.clean_line("+\x1b[8mhidden\x1b[0m\r\n"), "+?[8mhidden?[0m")
        self.assertEqual(review.clean_line("\ttab \u202eevil\n"), "\ttab ?evil")

    def test_colors(self) -> None:
        paint = review.Colorizer(log=True)
        out = [
            paint(line)
            for line in [
                "abc1234 agent commit",
                "",
                "diff --git a/x b/x",
                "--- a/x",
                "+++ b/x",
                "@@ -1 +1 @@ def f():",
                "-old",
                "+new",
                " same",
                " x | 3 ++-",
            ]
        ]
        self.assertEqual(out[0], "\x1b[33mabc1234\x1b[m agent commit")
        self.assertEqual(out[3], "\x1b[1m--- a/x\x1b[m")
        self.assertEqual(out[5], "\x1b[36m@@ -1 +1 @@\x1b[m def f():")
        self.assertEqual(out[6:9], ["\x1b[31m-old\x1b[m", "\x1b[32m+new\x1b[m", " same"])
        self.assertEqual(out[9], " x | 3 \x1b[32m++\x1b[31m-\x1b[m")

    def test_revision_check(self) -> None:
        self.assertTrue(review.revision_ok("main~3"))
        for bad in ("", "--output=/tmp/x", "a b", "a\nb"):
            self.assertFalse(review.revision_ok(bad))


class DashTest(TempHome):
    def test_session_states_and_fallback(self) -> None:
        self.add_image()
        state = self.docker_state()
        state["containers"]["kbx-a-1"] = {"State": {"Status": "running"}, "Config": {"Labels": {}}}
        state["behave"] = {"sessions": ["claude", "codex"], "session_states": {"claude": {"state": "waiting"}}}
        self.write_state(state)
        from kbx.docker import Docker

        self.assertEqual(sandbox.session_states(Docker(), "kbx-a-1"), {"claude": "waiting", "codex": ""})

    def test_cells_and_style(self) -> None:
        row = status.Row(
            "kbx-a-1", Path("/p"), "running", "mount", sessions=("claude", "codex"), activity=("waiting", "")
        )
        snap = mock.Mock(latest=None, drift=[], image_id="")
        cells = dash.cells(row, config.Config(), snap, Path("/home"))
        self.assertEqual(cells["SESSIONS"], "claude:waiting,codex")
        self.assertEqual(dash.row_style(row), "warn")

    def test_agents_section_shows_logins(self) -> None:
        row = status.Row("kbx-a-1", Path("/p"), "running", "mount")
        details = status.Details(at=0, state="running", versions={"claude": "2.0.0", "codex": "1.0.0", "pi": None})
        details.logins = {"claude": (True, "claude.ai (max)"), "codex": (False, "not logged in"), "pi": (None, "?")}
        snap = mock.Mock(latest=None, latest_error="")
        text = [line for line, _ in dash.agents_section(row, details, snap)]
        self.assertIn("          login: ✓ claude.ai (max)", text)
        self.assertIn("          login: ✗ not logged in", text)
        self.assertFalse(any("?" in line for line in text if "login" in line), "no login line without the agent")


if __name__ == "__main__":
    unittest.main()
