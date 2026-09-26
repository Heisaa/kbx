"""The dashboard: collection (kbx/status.py), rendering and keys (kbx/dash.py),
and one run under a pseudo-terminal."""

from __future__ import annotations

import contextlib
import curses
import io
import json
import os
import pty
import select
import subprocess
import sys
import time
from pathlib import Path
from typing import Any
from unittest import mock

from kbx import config, dash, git, guard, paths, sandbox, status
from kbx.docker import Docker
from kbx.status import Details, Row, Snapshot
from tests.unit.helpers import KBX, TempHome
from tests.unit.helpers import git as run_git


def snapshot(rows: list[Row], **values: Any) -> Snapshot:
    fields: dict[str, Any] = {
        "rows": rows,
        "details": {},
        "reports": {},
        "image_id": "",
        "drift": [],
        "latest": None,
        "latest_error": "",
        "error": "",
        "listed_at": time.time(),
        "busy": frozenset[str](),
    }
    fields.update(values)
    return Snapshot(**fields)


def texts(lines: list[tuple[str, str]]) -> str:
    return "\n".join(text for text, _ in lines)


class DashBase(TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.paths = paths.resolve({"HOME": str(self.home)})
        self.paths.stage.mkdir(parents=True)
        self.cfg = config.load(self.paths, {})
        self.docker = Docker()
        self.add_image()

    def create(self, repo: Path, mode: str = "mount", run: bool = True) -> sandbox.Sandbox:
        sb = sandbox.for_project(repo, mode)
        with contextlib.redirect_stdout(io.StringIO()):
            sandbox.ensure_created(self.docker, sb, self.cfg, self.paths)
            if run:
                sandbox.ensure_running(self.docker, sb, timeout=5)
        return sb

    def fake_guard(self, name: str) -> None:
        """A process that looks like `kbx _guard NAME` to guard.running()."""
        process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)", "_guard", name])
        self.addCleanup(process.wait)
        self.addCleanup(process.kill)
        self.paths.runtime_dir.mkdir(parents=True, exist_ok=True)
        (self.paths.runtime_dir / f"guard-{name}.pid").write_text(f"{process.pid}\n")

    def alert(self, name: str, findings: list[dict[str, str]] | None = None) -> None:
        target = guard.Guard(self.paths, name).alert_file
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"findings": findings or []}))


class StatusTest(DashBase):
    def test_guard_state(self) -> None:
        name = "kbx-x-1"
        self.assertEqual(status.guard_state(self.paths, self.cfg, name, "running", "mount"), "unguarded")
        self.assertEqual(status.guard_state(self.paths, self.cfg, name, "exited", "mount"), "-")
        self.assertEqual(status.guard_state(self.paths, self.cfg, name, "running", "clone"), "-")
        off = config.load(self.paths, {"KBX_GUARD": "0"})
        self.assertEqual(status.guard_state(self.paths, off, name, "running", "mount"), "off")
        self.fake_guard(name)
        self.assertEqual(status.guard_state(self.paths, self.cfg, name, "running", "mount"), "watching")
        self.assertEqual(status.guard_state(self.paths, self.cfg, name, "paused", "mount"), "watching")
        self.alert(name)
        # An alert wins in every state: the seal can find something after a stop.
        self.assertEqual(status.guard_state(self.paths, self.cfg, name, "exited", "clone"), "alert")

    def test_list_rows(self) -> None:
        mounted = self.create(self.make_repo("alpha"), "mount")
        cloned = self.create(self.make_repo("beta"), "clone", run=False)
        gone = self.create(self.make_repo("gamma"), "clone", run=False)
        subprocess.run(["rm", "-rf", str(gone.project_dir)], check=True)
        self.clear_calls()
        rows = {row.name: row for row in status.list_rows(self.docker, self.paths, self.cfg)}
        self.assertEqual(set(rows), {mounted.name, cloned.name, gone.name})
        self.assertEqual((rows[mounted.name].state, rows[mounted.name].mode), ("running", "mount"))
        self.assertEqual(rows[mounted.name].guard, "unguarded")
        self.assertTrue(rows[mounted.name].started_at.startswith("2020-01-01T"))
        self.assertEqual((rows[cloned.name].state, rows[cloned.name].mode), ("created", "clone"))
        self.assertFalse(rows[cloned.name].orphan)
        self.assertTrue(rows[gone.name].orphan)
        self.assertEqual(rows[mounted.name].sandbox, mounted)
        # One ps and one inspect for all of them; nothing runs inside a sandbox.
        self.assertEqual([c[:2] for c in self.calls()], [["ps", "-a"], ["container", "inspect"]])

    def test_stale(self) -> None:
        row = Row("kbx-a-1", self.home, "running", "clone", image_id="sha256:old")
        notes = status.stale(row, self.cfg, "sha256:new")
        self.assertTrue(any("clone mode, the config says mount" in n for n in notes))
        self.assertTrue(any("older image" in n for n in notes))
        same = Row("kbx-a-1", self.home, "running", "mount", image_id="sha256:new")
        self.assertEqual(status.stale(same, self.cfg, "sha256:new"), [])
        self.assertEqual(status.stale(same, self.cfg, ""), [])

    def test_image_state(self) -> None:
        image_id, drift = status.image_state(self.docker, self.paths, config.load(self.paths, {}), [])
        self.assertEqual(image_id, "")
        self.assertTrue(drift, "an unlabelled image is out of date")
        missing = config.load(self.paths, {"KBX_IMAGE": "nope"})
        self.assertEqual(
            status.image_state(self.docker, self.paths, missing, []), ("", ["image 'nope' not found; run `kbx build`"])
        )

    def test_git_since(self) -> None:
        self.assertEqual(status.git_since("2026-09-25T10:02:03.123456789Z"), "2026-09-25 10:02:03 +0000")
        self.assertIsNone(status.git_since("0001-01-01T00:00:00Z"))
        self.assertIsNone(status.git_since(""))

    def test_mount_git_runs_in_the_sandbox(self) -> None:
        repo = self.make_repo("alpha")
        sb = self.create(repo, "mount")
        (repo / "one.txt").write_text("1\n")
        run_git(repo, "add", "one.txt")
        run_git(repo, "commit", "-q", "-m", "agent's first")
        (repo / "dirty.txt").write_text("x\n")
        self.clear_calls()
        lines = status.mount_git(self.docker, sb, "2020-01-01T00:00:00.5Z")
        # The initial commit is older than 2020 in no test; count what is after the start.
        self.assertIn("agent's first", "\n".join(lines))
        self.assertIn("1 uncommitted path(s) in the checkout", lines)
        calls = self.calls()
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][0], "exec")
        self.assertEqual(calls[0][calls[0].index("-w") + 1], str(repo))
        self.assertIn("--no-optional-locks", calls[0][-4])

    def test_mount_git_counts_only_after_the_start(self) -> None:
        repo = self.make_repo("alpha")
        sb = self.create(repo, "mount")
        lines = status.mount_git(self.docker, sb, "2090-01-01T00:00:00Z")
        self.assertEqual(lines[0], "0 commit(s) on HEAD since the sandbox started")
        self.assertEqual(lines[-1], "no uncommitted changes")

    def test_clone_git(self) -> None:
        repo = self.make_repo("beta")
        sb = self.create(repo, "clone")
        self.assertIn("not seeded", status.clone_git(self.docker, sb)[0])
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            git.seed(self.docker, sb)
        clone = self.sandbox_home / "work" / "beta"
        env = {k: v for k, v in os.environ.items() if k != "GIT_CONFIG_GLOBAL"}
        env.update(
            HOME=str(self.sandbox_home),
            GIT_AUTHOR_NAME="a",
            GIT_AUTHOR_EMAIL="a@x",
            GIT_COMMITTER_NAME="a",
            GIT_COMMITTER_EMAIL="a@x",
        )
        self.assertEqual(status.clone_git(self.docker, sb), ["every sandbox branch is on the host"])
        (clone / "new.txt").write_text("n\n")
        run_git(clone, "add", "new.txt", env=env)
        run_git(clone, "commit", "-q", "-m", "work", env=env)
        (clone / "wip.txt").write_text("w\n")
        lines = status.clone_git(self.docker, sb)
        self.assertTrue(any(line.startswith("unfetched: main (1 commit(s))") for line in lines), lines)
        self.assertTrue(any("uncommitted change" in line for line in lines), lines)

    def test_health(self) -> None:
        sb = self.create(self.make_repo("alpha"))
        self.assertEqual(status.health(self.docker, sb), [])
        self.behave(
            ready_record={
                "seed": {"a": {"ok": False, "error": "bad toml"}},
                "start": {"b": {"ok": False, "status": 3}},
                "services": {"clipboard": {"state": "failed", "last_exit": 1}, "ok": {"state": "running"}},
            }
        )
        self.assertEqual(
            status.health(self.docker, sb),
            [
                "seed failed for module a: bad toml",
                "start.sh failed for module b (exit 3)",
                "service clipboard failed (last exit 1)",
            ],
        )
        self.behave(ready=False)
        self.assertIsNone(status.health(self.docker, sb))

    def test_collect_details(self) -> None:
        repo = self.make_repo("alpha")
        self.create(repo)
        self.behave(versions={"claude": "2.0.1", "codex": "0.9.0"}, daemon="daemon running, codex 0.8.0")
        row = next(iter(status.list_rows(self.docker, self.paths, self.cfg)))
        details = status.collect_details(self.docker, row)
        self.assertEqual(details.state, "running")
        self.assertEqual(details.health, [])
        self.assertEqual(details.versions, {"claude": "2.0.1", "codex": "0.9.0", "pi": "1.0.0"})
        self.assertEqual(details.daemon, status.Daemon(True, "daemon running, codex 0.8.0", "0.8.0"))
        self.assertTrue(details.git[0].endswith("since the sandbox started"), details.git)
        self.assertEqual(details.errors, [])
        stopped = status.collect_details(self.docker, Row(row.name, repo, "exited", "mount"))
        self.assertEqual((stopped.health, stopped.git, stopped.versions), (None, [], {}))

    def test_stats(self) -> None:
        self.behave(stats={"kbx-a": ["1.50%", "512MiB"]})
        self.assertEqual(status.stats(self.docker, ["kbx-a", "kbx-b"]), {"kbx-a": ("1.50%", "512MiB")})
        self.assertEqual(status.stats(self.docker, []), {})

    def test_latest_versions(self) -> None:
        answers = {"claude-code": b'{"version": "2.1.0"}', "codex": b'{"version": "\\u001b[31m"}'}
        urls: list[str] = []

        def fake_open(url: str, timeout: float) -> Any:
            urls.append(url)
            for key, body in answers.items():
                if url.endswith(f"{key}/latest"):
                    return contextlib.nullcontext(io.BytesIO(body))
            raise OSError("offline")

        with mock.patch("urllib.request.urlopen", fake_open):
            found = status.latest_versions()
        self.assertEqual(found, {"claude": "2.1.0", "codex": None, "pi": None})
        self.assertTrue(all(url.startswith("https://registry.npmjs.org/@") for url in urls), urls)
        self.assertIn("https://registry.npmjs.org/@anthropic-ai%2Fclaude-code/latest", urls)

    def test_monitor(self) -> None:
        repo = self.make_repo("alpha")
        sb = self.create(repo)
        self.behave(sessions=["codex"], stats={sb.name: ["3.0%", "1GiB"]})
        monitor = status.Monitor(self.docker, self.paths, self.cfg, [])
        monitor.start()
        self.addCleanup(monitor.stop)
        monitor.select(sb.name)
        deadline = time.monotonic() + 20
        while True:
            snap = monitor.snapshot()
            rows = snap.rows
            if rows and rows[0].sessions and rows[0].cpu and sb.name in snap.details:
                break
            if time.monotonic() > deadline:
                self.fail(f"monitor did not fill the snapshot: {snap}")
            time.sleep(0.1)
        self.assertEqual(rows[0].sessions, ("codex",))
        self.assertEqual((rows[0].cpu, rows[0].memory), ("3.0%", "1GiB"))
        self.assertEqual(snap.details[sb.name].state, "running")
        self.assertTrue(snap.drift)
        # A paused monitor runs nothing.
        monitor.pause()
        time.sleep(0.3)
        self.clear_calls()
        time.sleep(2.5)
        self.assertEqual(self.calls(), [])
        monitor.resume()
        deadline = time.monotonic() + 10
        while not self.calls():
            if time.monotonic() > deadline:
                self.fail("monitor did not resume")
            time.sleep(0.1)

    def test_monitor_reports_alerts(self) -> None:
        repo = self.make_repo("alpha")
        sb = self.create(repo)
        self.alert(sb.name, [{"time": "t", "path": ".git/hooks/pre-commit", "detail": "new hook", "action": "removed"}])
        monitor = status.Monitor(self.docker, self.paths, self.cfg, [])
        monitor._list_once()  # pyright: ignore[reportPrivateUsage]
        snap = monitor.snapshot()
        self.assertEqual(snap.rows[0].guard, "alert")
        self.assertIn(".git/hooks/pre-commit: new hook", "\n".join(snap.reports[sb.name]))


class RenderTest(DashBase):
    def row(self, **values: Any) -> Row:
        fields: dict[str, Any] = {
            "name": "kbx-alpha-1",
            "project_dir": self.home / "code/alpha",
            "state": "running",
            "mode": "mount",
        }
        fields.update(values)
        return Row(**fields)

    def test_clean(self) -> None:
        self.assertEqual(dash.clean("\x1b[31mred\x07"), "?[31mred?")
        self.assertEqual(dash.clean("a\u202eb\u009bc"), "a?b?c")
        self.assertEqual(dash.clean("tab\there\nnext"), "tab  here next")
        self.assertEqual(dash.clean("ünïcödé ✓"), "ünïcödé ✓")

    def test_cut_and_pad(self) -> None:
        self.assertEqual(dash.cut("abcdef", 4), "abc…")
        self.assertEqual(dash.cut("abc", 4), "abc")
        self.assertEqual(dash.cut("漢字漢字", 5), "漢字…")
        self.assertEqual(dash.cut("abc", 0), "")
        self.assertEqual(dash.pad("ab", 4), "ab  ")
        self.assertEqual(dash.pad("漢", 3), "漢 ")

    def test_short_path(self) -> None:
        self.assertEqual(dash.short_path(self.home / "code/x", self.home), "~/code/x")
        self.assertEqual(dash.short_path(self.home, self.home), "~")
        self.assertEqual(dash.short_path(Path("/srv/x"), self.home), "/srv/x")

    def test_list_lines(self) -> None:
        rows = [
            self.row(sessions=("claude", "codex"), cpu="1.0%", memory="1GiB", guard="watching"),
            self.row(name="kbx-beta-2", project_dir=self.home / "code/beta", guard="alert", state="paused"),
            self.row(name="kbx-gamma-3", project_dir=Path("/gone"), state="exited", mode="clone", orphan=True),
        ]
        lines = dash.list_lines(snapshot(rows), self.cfg, self.home, 0, 140)
        self.assertIn("PROJECT", lines[0][0])
        self.assertTrue(lines[1][0].startswith("▶ ~/code/alpha"))
        self.assertIn("claude,codex", lines[1][0])
        self.assertIn("watching", lines[1][0])
        self.assertEqual(lines[1][1], "select")
        self.assertIn("ALERT", lines[2][0])
        self.assertEqual(lines[2][1], "alert")
        self.assertIn("orphan", lines[3][0])
        self.assertEqual(lines[3][1], "dim")
        self.assertTrue(all(len(text) <= 140 for text, _ in lines))

    def test_list_lines_empty_and_error(self) -> None:
        self.assertIn("No kbx sandboxes", texts(dash.list_lines(snapshot([]), self.cfg, self.home, 0, 100)))
        self.assertIn("…", texts(dash.list_lines(snapshot([], listed_at=0.0), self.cfg, self.home, 0, 100)))
        failed = snapshot([], error="docker ps failed: \x1b]0;pwned\x07")
        text = texts(dash.list_lines(failed, self.cfg, self.home, 0, 100))
        self.assertIn("docker ps failed", text)
        self.assertNotIn("\x1b", text)

    def test_detail_alert_is_cleaned(self) -> None:
        row = self.row(guard="alert", state="paused")
        report = ["The kbx guard stopped these changes:", "      +core.fsmonitor = \x1b[2J evil"]
        text = texts(dash.detail_lines(row, snapshot([row], reports={row.name: report}), self.cfg, self.home))
        self.assertIn("The guard stopped changes", text)
        self.assertIn("+core.fsmonitor = ?[2J evil", text)
        self.assertIn("R: review and resume", text)
        self.assertNotIn("\x1b", text)

    def test_detail_unguarded(self) -> None:
        row = self.row(guard="unguarded")
        text = texts(dash.detail_lines(row, snapshot([row]), self.cfg, self.home))
        self.assertIn("No guard is watching", text)
        self.assertIn("S (kbx start)", text)

    def test_detail_sections(self) -> None:
        row = self.row(guard="watching", image_id="sha256:old")
        details = Details(
            at=time.time(),
            state="running",
            health=["service clipboard failed (last exit 1)"],
            git=["2 commit(s) on HEAD since the sandbox started", "  abc123 fix \x1b[1m", "no uncommitted changes"],
            versions={"claude": "2.0.0", "codex": "0.9.0", "pi": None},
            daemon=status.Daemon(True, "running", "0.8.0"),
        )
        snap = snapshot(
            [row],
            details={row.name: details},
            image_id="sha256:new",
            drift=["module x changed; run `kbx build && kbx recreate`"],
            latest={"claude": "2.1.0", "codex": "0.9.0", "pi": "1.0.0"},
        )
        lines = dash.detail_lines(row, snap, self.cfg, self.home)
        text = texts(lines)
        self.assertIn("⚠ service clipboard failed", text)
        self.assertIn("abc123 fix ?[1m", text)
        self.assertIn("claude  2.0.0   2.1.0 published; u updates", text)
        self.assertIn(
            "codex   0.9.0   current\n          remote control: running\n          the daemon runs 0.8.0", text
        )
        self.assertIn("pi      not found", text)
        self.assertIn("older image", text)
        self.assertIn("module x changed", text)
        self.assertIn(("  ✓ watching the checkout's git hooks, config and protected files", "ok"), lines)

    def test_empty_dashboard_explains_the_image(self) -> None:
        app = dash.App.__new__(dash.App)
        app.show_help, app.selected, app.config, app.home = False, 0, self.cfg, self.home
        body = texts(app.body(snapshot([], drift=["image 'kbx-agent' not found; run `kbx build`"]), 80))
        self.assertIn("not found; run `kbx build`", body)
        self.assertEqual(app.body(snapshot([]), 80), [])

    def test_detail_waiting_and_stopped(self) -> None:
        running = self.row()
        text = texts(
            dash.detail_lines(running, snapshot([running], busy=frozenset({running.name})), self.cfg, self.home)
        )
        self.assertIn("refreshing…", text)
        self.assertIn("…", text)
        stopped = self.row(state="exited", mode="clone")
        text = texts(dash.detail_lines(stopped, snapshot([stopped]), self.cfg, self.home))
        self.assertIn("start the sandbox to see its clone", text)
        self.assertIn("not needed: clone mode", text)
        self.assertIn("clone mode, the config says mount", text)


class Screen:
    """Enough of a curses window for App.draw: a grid of characters."""

    def __init__(self, height: int, width: int) -> None:
        self.height, self.width = height, width
        self.rows: list[str] = []
        self.erase()

    def erase(self) -> None:
        self.rows = [""] * self.height

    def getmaxyx(self) -> tuple[int, int]:
        return self.height, self.width

    def addstr(self, y: int, x: int, text: str, attr: int = 0) -> None:
        assert x == 0 and 0 <= y < self.height, (y, x)
        assert dash.width_of(text) < self.width, f"line {y} is too wide: {text!r}"
        self.rows[y] = text

    def refresh(self) -> None:
        pass

    def text(self) -> str:
        return "\n".join(self.rows)


class NarrowTest(DashBase):
    def rows(self) -> list[Row]:
        home = self.home
        return [
            Row("kbx-kbx-1", home / "Projects/kbx", "running", "mount", guard="watching", sessions=("claude", "codex"), cpu="4.21%", memory="1.3GiB"),
            Row("kbx-web-2", home / "code/webapp", "paused", "mount", guard="alert"),
            Row("kbx-api-3", home / "code/api", "running", "clone", sessions=("pi",), cpu="0.50%", memory="640MiB"),
        ]  # fmt: skip

    def app(self, height: int, width: int) -> tuple[dash.App, Screen]:
        app = dash.App.__new__(dash.App)
        screen = Screen(height, width)
        app.screen = screen  # pyright: ignore[reportAttributeAccessIssue]
        app.config, app.home, app.styles = self.cfg, self.home, {}
        app.selected, app.scroll, app.show_help, app.zoom, app.message = 0, 0, False, False, ""
        app.capture = dash.Capture()
        return app, screen

    def test_wrap(self) -> None:
        self.assertEqual(dash.wrap("short", 10), ["short"])
        self.assertEqual(dash.wrap("  one two three four", 12), ["  one two", "    three", "    four"])
        self.assertEqual(dash.wrap("  key  text that wraps", 18, hang=7), ["  key  text that", "       wraps"])
        # Too narrow for the hang: fall back to a small indent rather than a column of words.
        self.assertEqual(dash.wrap("  key  text that wraps", 14, hang=7), ["  key  text", "  that wraps"])
        long = dash.wrap("  /a/very/long/path/without/spaces/at/all", 16)
        self.assertTrue(all(len(line) <= 16 for line in long), long)
        self.assertEqual("".join(line.strip() for line in long), "/a/very/long/path/without/spaces/at/all")

    def test_columns_drop_by_priority(self) -> None:
        table = [dash.cells(row, self.cfg, snapshot(self.rows()), self.home) for row in self.rows()]
        titles = {w: [c.title for c in dash.fit_columns(table, w)] for w in (60, 66, 90)}
        self.assertEqual(titles[90], ["PROJECT", "STATE", "MODE", "GUARD", "SESSIONS", "CPU", "MEM", "FLAGS"])
        self.assertNotIn("MEM", titles[66])
        self.assertIn("GUARD", titles[60])
        self.assertIn("STATE", titles[60])
        for width, columns in ((w, dash.fit_columns(table, w)) for w in range(60, 130, 3)):
            used = 2 + sum(c.width for c in columns) + len(columns) - 1
            self.assertLess(used, width, (width, columns))
            project = next(c for c in columns if c.title == "PROJECT")
            if width >= 72:
                self.assertEqual(project.width, len("~/Projects/kbx"), width)

    def test_cards_when_narrow(self) -> None:
        head, groups = dash.list_view(snapshot(self.rows()), self.cfg, self.home, 1, 40)
        self.assertEqual(head, [])
        self.assertEqual(len(groups), 3)
        self.assertEqual(groups[1][0], ("▶ ~/code/webapp", "select"))
        self.assertEqual(groups[1][1], ("    paused · mount · ALERT", "alert"))
        self.assertIn("claude,codex", texts(groups[0]))
        self.assertTrue(all(len(text) < 40 for group in groups for text, _ in group))

    def test_header_and_footer_fit(self) -> None:
        snap = snapshot(self.rows(), drift=["x"])
        self.assertEqual(dash.header_line(snap, 30)[0].rstrip(), " kbx dash · 3 sandboxes")
        self.assertIn("image out of date · updated", dash.header_line(snap, 80)[0])
        self.assertIn("2/3", dash.header_line(snap, 40, "2/3")[0])
        for width in (24, 40, 60, 120):
            self.assertLess(dash.width_of(dash.footer(width, False)), width)
            self.assertLess(dash.width_of(dash.footer(width, True)), width)
        self.assertIn("Tab details", dash.footer(45, False))

    def test_help_when_narrow(self) -> None:
        lines = dash.help_lines(34)
        self.assertIn(("  Enter", "bold"), lines)
        self.assertTrue(all(len(text) < 34 for text, _ in lines))
        self.assertIn("  Enter", texts(dash.help_lines(100)).split(" attach")[0])

    def test_draw_at_phone_sizes(self) -> None:
        rows = self.rows()
        report = ["The kbx guard stopped these changes in /home/someone/code/webapp:", "      +\tfsmonitor = /tmp/x.sh"]
        snap = snapshot(
            rows, reports={rows[1].name: report}, drift=["module x changed; run `kbx build && kbx recreate`"]
        )
        for height, width in ((40, 40), (20, 30), (12, 24), (15, 90), (50, 60)):
            for zoom, help_shown in ((False, False), (True, False), (False, True)):
                app, screen = self.app(height, width)
                app.selected, app.zoom, app.show_help = 1, zoom, help_shown
                app.draw(snap)  # Screen asserts that every line fits
                self.assertIn("kbx dash", screen.rows[0])
        app, screen = self.app(30, 40)
        app.selected = 1
        app.draw(snap)
        self.assertIn("▶ ~/code/webapp", screen.text())
        app.zoom = True
        app.draw(snap)
        self.assertNotIn("~/Projects/kbx", screen.text(), "full-screen details hide the list")
        self.assertIn("The guard stopped changes", screen.text())
        self.assertIn("2/3", screen.rows[0])

    def test_long_prompt_wraps_and_keeps_the_question(self) -> None:
        app, screen = self.app(20, 32)
        app.message = "Recreate kbx-webapp-5e6f7a8b from the current image (volumes are kept)? [y/N] "
        app.draw(snapshot(self.rows()))
        self.assertIn("[y/N]", screen.rows[-1])
        self.assertIn("Recreate", screen.text())

    def test_small_list_scrolls_to_the_selection(self) -> None:
        rows = [Row(f"kbx-p{i}-1", self.home / f"p{i}", "running", "mount") for i in range(12)]
        app, screen = self.app(16, 40)
        app.selected = 10
        app.draw(snapshot(rows))
        self.assertIn("▶ ~/p10", screen.text())

    def test_navigation(self) -> None:
        app, _ = self.app(30, 40)
        snap = snapshot(self.rows())
        app.handle(curses.KEY_DOWN, snap, snap.rows[0])
        self.assertEqual(app.selected, 1)
        app.handle(ord("\t"), snap, snap.rows[1])
        self.assertTrue(app.zoom)
        app.handle(curses.KEY_DOWN, snap, snap.rows[1])  # a swipe on a phone
        self.assertEqual((app.selected, app.scroll), (1, 1))
        app.handle(curses.KEY_RIGHT, snap, snap.rows[1])
        self.assertEqual((app.selected, app.scroll), (2, 0))
        app.handle(curses.KEY_RIGHT, snap, snap.rows[2])
        self.assertEqual(app.selected, 2, "stays on the last one")
        app.handle(27, snap, snap.rows[2])  # Esc leaves the full screen
        self.assertFalse(app.zoom)
        app.handle(ord("?"), snap, snap.rows[2])
        app.handle(curses.KEY_DOWN, snap, snap.rows[2])
        self.assertEqual((app.selected, app.scroll), (2, 1), "help scrolls, the selection stays")
        app.handle(27, snap, snap.rows[2])
        self.assertFalse(app.show_help)


class KeysTest(DashBase):
    def app(self, answers: str = "") -> dash.App:
        app = dash.App.__new__(dash.App)
        keys = iter(answers)
        app.ask = lambda question: next(keys, "")  # type: ignore[method-assign]
        app.confirm = lambda question: next(keys, "") == "y"  # type: ignore[method-assign]
        return app

    def row(self, **values: Any) -> Row:
        fields: dict[str, Any] = {"name": "kbx-a-1", "project_dir": self.home, "state": "running", "mode": "mount"}
        fields.update(values)
        return Row(**fields)

    def test_attach(self) -> None:
        one = self.row(sessions=("codex",))
        self.assertEqual(self.app().action("\n", one), dash.Action(("attach", "codex"), interactive=True))
        several = self.row(sessions=("codex", "claude"))
        self.assertEqual(self.app("c").action("\n", several), dash.Action(("attach", "claude"), interactive=True))
        none = self.row(sessions=())
        self.assertEqual(self.app("p").action("\n", none), dash.Action(("pi",), interactive=True))
        self.assertIsNone(self.app("z").action("\n", none))
        stopped = self.row(state="exited")
        self.assertEqual(self.app("x").action("a", stopped), dash.Action(("codex",), interactive=True))

    def test_commands(self) -> None:
        row = self.row()
        app = self.app()
        expected = {
            "c": ("claude",), "x": ("codex",), "p": ("pi",), "s": ("shell",), "S": ("start",), "t": ("stop",),
            "r": ("recreate",), "D": ("rm",), "R": ("resume",), "A": ("resume", "--accept"), "f": ("fetch",),
            "y": ("sync",), "u": ("update",), "l": ("logs",),
        }  # fmt: skip
        for char, args in expected.items():
            action = app.action(char, row)
            self.assertIsNotNone(action, char)
            assert action is not None
            self.assertEqual(action.args, args, char)
        self.assertTrue(app.action("t", row).confirm)  # type: ignore[union-attr]
        self.assertTrue(app.action("r", row).confirm)  # type: ignore[union-attr]
        self.assertTrue(app.action("A", row).confirm)  # type: ignore[union-attr]
        self.assertFalse(app.action("D", row).confirm, "kbx rm asks itself")  # type: ignore[union-attr]
        self.assertIsNone(app.action("Z", row))

    def test_run_command(self) -> None:
        repo = self.make_repo("alpha")
        out = self.temp / "out"
        saved = os.dup(1)
        try:
            with open(out, "wb") as handle, contextlib.redirect_stdout(io.StringIO()) as echoed:
                os.dup2(handle.fileno(), 1)
                code = dash.run_command(KBX, ["ls"], repo)
        finally:
            os.dup2(saved, 1)
            os.close(saved)
        self.assertEqual(code, 0)
        self.assertIn(f"$ kbx ls    (in {repo})", echoed.getvalue())
        self.assertIn("No kbx sandboxes", out.read_text())

    def test_orphan_remove(self) -> None:
        repo = self.make_repo("gone")
        sb = self.create(repo, "clone", run=False)
        self.alert(sb.name)
        row = next(iter(status.list_rows(self.docker, self.paths, self.cfg)))
        app = self.app("ny")
        app.docker, app.paths = self.docker, self.paths
        app.monitor = mock.Mock()
        app.message = ""
        app.capture = dash.Capture()
        app.draw = lambda snap: None  # type: ignore[method-assign]
        app.orphan("D", row)
        self.assertIn(sb.name, self.docker_state()["containers"], "declined: kept")
        app.orphan("D", row)
        self.assertNotIn(sb.name, self.docker_state()["containers"])
        self.assertEqual(self.docker_state()["volumes"], {})
        self.assertFalse((self.paths.guard_dir / sb.name).exists())


class TerminalTest(DashBase):
    def test_needs_a_terminal(self) -> None:
        result = self.run_kbx("dash")
        self.assertEqual(result.returncode, 1)
        self.assertIn("needs a terminal", result.stderr)
        plain = self.run_kbx()
        self.assertEqual(plain.returncode, 2)
        self.assertIn("usage", plain.stdout)

    def spawn(self, columns: int, lines: int) -> Terminal:
        master, slave = pty.openpty()
        env = dict(self.env, TERM="xterm", COLUMNS=str(columns), LINES=str(lines))
        process = subprocess.Popen(
            [sys.executable, str(KBX)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            cwd=str(self.home),
            env=env,
            close_fds=True,
        )
        os.close(slave)
        self.addCleanup(os.close, master)
        self.addCleanup(lambda: process.poll() is None and process.kill())
        return Terminal(self, process, master)

    def test_dash_under_a_pty(self) -> None:
        repo = self.make_repo("alpha")
        sb = self.create(repo, "mount")
        self.behave(sessions=["claude"])
        term = self.spawn(140, 40)
        term.read_until(repo.name.encode())
        term.read_until(b"claude")
        term.read_until(b"UNGUARDED")
        term.send(b"?")
        term.read_until(b"Keys")
        term.send(b"t")  # stop asks first; n declines
        term.read_until(b"[y/N]")
        term.send(b"n")
        time.sleep(0.5)
        self.assertEqual(self.docker_state()["containers"][sb.name]["State"]["Status"], "running")
        term.send(b"t")
        term.read_until(b"[y/N]")
        term.send(b"y")
        term.read_until(b"Press Enter to return")
        self.assertIn(b"Stopped", term.seen[-2000:], term.seen[-2000:])
        self.assertEqual(self.docker_state()["containers"][sb.name]["State"]["Status"], "exited")
        term.send(b"\n")
        time.sleep(0.5)
        term.send(b"q")
        self.assertEqual(term.process.wait(timeout=20), 0)

    def test_narrow_pty(self) -> None:
        repo = self.make_repo("alpha")
        self.create(repo, "mount")
        term = self.spawn(40, 30)
        term.read_until(b"\xe2\x96\xb6 ")  # ▶, the selected card
        term.read_until(b"Tab details")
        term.send(b"\t")
        term.read_until(b"Tab back")  # curses redraws only what changed; the footer changes whole
        term.send(b"\x1b")  # Esc leaves the full screen, it does not quit
        term.read_until(b"Tab details")
        self.assertIsNone(term.process.poll())
        term.send(b"q")
        self.assertEqual(term.process.wait(timeout=20), 0)


class Terminal:
    """A dashboard process on a pseudo-terminal, and everything it wrote."""

    def __init__(self, test: TerminalTest, process: subprocess.Popen[bytes], master: int) -> None:
        self.test, self.process, self.master = test, process, master
        self.seen = b""
        self.pos = 0  # markers count from the last key sent: they answer it

    def send(self, data: bytes) -> None:
        self.pos = len(self.seen)
        os.write(self.master, data)

    def read_until(self, marker: bytes, timeout: float = 20) -> None:
        deadline = time.monotonic() + timeout
        while marker not in self.seen[self.pos :]:
            if time.monotonic() > deadline:
                self.process.kill()
                self.test.fail(f"{marker!r} never appeared; screen: {self.seen[-3000:]!r}")
            ready, _, _ = select.select([self.master], [], [], 0.2)
            if ready:
                try:
                    self.seen += os.read(self.master, 65536)
                except OSError:
                    self.test.fail(f"the dashboard exited before {marker!r}; screen: {self.seen[-3000:]!r}")
