"""`kbx dash`: a terminal dashboard over every sandbox on the host.

Drawing is split from curses: `list_lines` and `detail_lines` turn a
`status.Snapshot` into (text, style) lines, and `App` only paints them. Every
action that has a command runs that command (`kbx <command>` in the project
directory) with the terminal handed over, so prompts, attach and detach work
exactly as they do outside the dashboard. See PLAN.md, "Dashboard".
"""

from __future__ import annotations

import curses
import io
import locale
import os
import shutil
import signal
import subprocess
import sys
import textwrap
import time
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from types import FrameType
from typing import IO

from . import agents, guard, status
from . import sandbox as sandbox_mod
from .config import Config
from .docker import Docker
from .errors import KbxError
from .modules import Resolved
from .paths import Paths
from .status import Details, Monitor, Row, Snapshot
from .text import clean

Line = tuple[str, str]  # text, style: normal, bold, dim, ok, warn, alert, head

GUARD_LABELS = {
    "alert": ("ALERT", "alert"),
    "unguarded": ("UNGUARDED", "alert"),
    "watching": ("watching", "ok"),
    "off": ("off", "warn"),
    "-": ("-", "dim"),
}

HELP = [
    ("Enter", "attach to the running agent (asks which if none or several)"),
    ("c x p", "kbx claude / codex / pi (starts it, or reattaches)"),
    ("s", "kbx shell"),
    ("S", "kbx start (also starts a missing guard)"),
    ("t", "kbx stop"),
    ("r", "kbx recreate (keeps volumes)"),
    ("D", "kbx rm (asks, and lists unfetched work)"),
    ("R", "kbx resume (after the guard paused the sandbox)"),
    ("A", "kbx resume --accept (the change was yours)"),
    ("f y", "kbx fetch / kbx sync (clone mode)"),
    ("u", "kbx update (all agents without a running session)"),
    ("U", "check the latest published agent versions (npm registry)"),
    ("d", "kbx diff (uncommitted changes, from the sandbox's git)"),
    ("l", "kbx logs"),
    ("b", "kbx build"),
    ("↑ ↓ j k", "select a sandbox (full-screen details: scroll)"),
    ("Tab", "full-screen details, and back (Esc too)"),
    ("← →", "full-screen details: the previous / next sandbox"),
    ("PgUp PgDn", "scroll the details"),
    ("g", "refresh now"),
    ("?", "this help"),
    ("q", "quit"),
]


def _width(char: str) -> int:
    if unicodedata.combining(char):
        return 0
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def cut(text: str, width: int) -> str:
    """At most `width` terminal columns, with … when cut."""
    if width <= 0:
        return ""
    used = 0
    for index, char in enumerate(text):
        used += _width(char)
        if used > width:
            keep = text[:index]
            while keep and sum(map(_width, keep)) > width - 1:
                keep = keep[:-1]
            return keep + "…"
    return text


def pad(text: str, width: int) -> str:
    text = cut(text, width)
    return text + " " * max(0, width - sum(map(_width, text)))


def short_path(path: Path, home: Path) -> str:
    try:
        return "~/" + str(path.relative_to(home)) if path != home else "~"
    except ValueError:
        return str(path)


def ago(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.0f}s"
    if seconds < 3600:
        return f"{seconds // 60:.0f}m"
    return f"{seconds // 3600:.0f}h"


# --- rendering ---


def flags(row: Row, config: Config, snap: Snapshot) -> list[str]:
    found: list[str] = []
    if row.orphan:
        found.append("orphan")
    elif status.stale(row, config, snap.image_id):
        found.append("stale")
    details = snap.details.get(row.name)
    if details is not None and details.health:
        found.append("health")
    return found


NARROW = 60  # below this many columns each sandbox takes two lines, like on a phone
MIN_PROJECT = 12
MAX_PROJECT = 48
ORDER = ("STATE", "MODE", "GUARD", "SESSIONS", "CPU", "MEM", "FLAGS")
# When the table does not fit, columns go from the end of this list first;
# PROJECT here means its full width (it always gets MIN_PROJECT).
PRIORITY = ("STATE", "GUARD", "SESSIONS", "FLAGS", "PROJECT", "MODE", "CPU", "MEM")


def width_of(text: str) -> int:
    return sum(map(_width, text))


def wrap(text: str, width: int, hang: int | None = None) -> list[str]:
    """Lines of at most `width` columns; continuation lines indented by `hang`
    (default: the line's own indent + 2)."""
    if width_of(text) <= width:
        return [text]
    body = text.lstrip(" ")
    indent = len(text) - len(body)
    rest = indent + 2 if hang is None else hang
    if width - rest < 8:
        rest = min(indent, 2)
    return textwrap.wrap(
        body,
        width,
        initial_indent=" " * indent,
        subsequent_indent=" " * rest,
        break_on_hyphens=False,
        replace_whitespace=False,
    ) or [text]


def wrap_lines(lines: Sequence[Line], width: int) -> list[Line]:
    return [(part, style) for text, style in lines for part in wrap(text, width)]


@dataclass(frozen=True)
class Column:
    title: str
    width: int


def cells(row: Row, config: Config, snap: Snapshot, home: Path) -> dict[str, str]:
    if row.sessions is None:
        live = "…" if row.running else "-"
    else:
        marks = dict(zip(row.sessions, row.activity, strict=False))
        live = ",".join(f"{name}:{marks[name]}" if marks.get(name) else name for name in row.sessions) or "-"
    return {
        "PROJECT": clean(short_path(row.project_dir, home)),
        "STATE": clean(row.state),
        "MODE": clean(row.mode),
        "GUARD": GUARD_LABELS.get(row.guard, (clean(row.guard), "normal"))[0],
        "SESSIONS": clean(live),
        "CPU": clean(row.cpu or "-"),
        "MEM": clean(row.memory or "-"),
        "FLAGS": " ".join(flags(row, config, snap)),
    }


def fit_columns(table: Sequence[dict[str, str]], width: int) -> list[Column]:
    """The project and as many other columns as fit, each as wide as its content."""
    room = width - 3  # the marker, and the last cell curses cannot write
    natural = {name: max([width_of(name), *(width_of(c[name]) for c in table)]) for name in ("PROJECT", *ORDER)}
    project = MIN_PROJECT
    used = MIN_PROJECT
    chosen: set[str] = set()
    for name in PRIORITY:
        if name == "PROJECT":
            project = max(MIN_PROJECT, min(natural["PROJECT"], MAX_PROJECT, room - used + MIN_PROJECT))
            used += project - MIN_PROJECT
        elif used + 1 + natural[name] <= room:
            chosen.add(name)
            used += 1 + natural[name]
    project = max(project, min(natural["PROJECT"], MAX_PROJECT, room - used + project))
    return [Column("PROJECT", project), *(Column(name, natural[name]) for name in ORDER if name in chosen)]


def row_style(row: Row) -> str:
    """The row's severity: what needs attention stands out."""
    if row.guard in ("alert", "unguarded"):
        return "alert"
    if row.state == "paused" or "waiting" in row.activity:
        return "warn"
    return "normal" if row.running else "dim"


def header_line(snap: Snapshot, width: int, position: str = "") -> Line:
    count = len(snap.rows)
    parts = ["kbx dash", position or f"{count} sandbox{'es' if count != 1 else ''}"]
    if snap.drift:
        parts.append("image out of date")
    if snap.error and snap.rows:
        parts.append(f"refresh failed: {clean(snap.error)}")
    if snap.listed_at:
        parts.append(f"updated {ago(max(0.0, time.time() - snap.listed_at))} ago")
    text = " " + parts[0]
    for part in parts[1:]:  # whole parts only: a cut "updated 1s ago" says nothing
        if width_of(f"{text} · {part}") <= width - 1:
            text = f"{text} · {part}"
    return pad(text, width - 1), "head"


def list_view(
    snap: Snapshot, config: Config, home: Path, selected: int, width: int
) -> tuple[list[Line], list[list[Line]]]:
    """The column header (if any) and one group of lines per sandbox."""
    if snap.error and not snap.rows:
        return [], [[(line, "alert") for line in wrap(f"  {clean(snap.error)}", width - 1)]]
    if not snap.rows:
        if not snap.listed_at:
            return [], [[("  …", "dim")]]
        text = "  No kbx sandboxes yet. Start one in a project with `kbx claude`, `kbx codex` or `kbx pi`."
        return [], [[(line, "dim") for line in wrap(text, width - 1)]]
    table = [cells(row, config, snap, home) for row in snap.rows]
    if width < NARROW:
        return [], [
            card(row, cell, index == selected, width)
            for index, (row, cell) in enumerate(zip(snap.rows, table, strict=True))
        ]
    cols = fit_columns(table, width)
    head = [("  " + " ".join(pad(c.title, c.width) for c in cols), "bold")]
    groups: list[list[Line]] = []
    for index, (row, cell) in enumerate(zip(snap.rows, table, strict=True)):
        marker = "▶ " if index == selected else "  "
        text = marker + " ".join(pad(cell[c.title], c.width) for c in cols)
        groups.append([(text, "select" if index == selected else row_style(row))])
    return head, groups


def card(row: Row, cell: dict[str, str], selected: bool, width: int) -> list[Line]:
    """Two lines for a narrow terminal: the project, then what matters about it."""
    parts = [cell["STATE"], cell["MODE"]]
    if row.guard != "-":
        parts.append(cell["GUARD"])
    parts += [cell[name] for name in ("SESSIONS", "CPU", "MEM") if cell[name] not in ("-", "")]
    if cell["FLAGS"]:
        parts.append(cell["FLAGS"])
    first = ("▶ " if selected else "  ") + cell["PROJECT"]
    about = [(line, row_style(row)) for line in wrap("    " + " · ".join(parts), width - 1, hang=4)]
    return [(first, "select" if selected else ("bold" if row.running else "dim")), *about]


def list_lines(snap: Snapshot, config: Config, home: Path, selected: int, width: int) -> list[Line]:
    head, groups = list_view(snap, config, home, selected, width)
    return [*head, *(line for group in groups for line in group)]


def _section(title: str) -> Line:
    return title, "bold"


def _not_running(row: Row, note: str) -> Line:
    if row.state == "paused":
        return "  paused; R resumes it" if row.guard == "alert" else "  paused", "dim"
    return f"  {note}", "dim"


def guard_section(row: Row, snap: Snapshot) -> list[Line]:
    lines = [_section("Guard")]
    if row.guard == "alert":
        lines.append(("  ⚠ The guard stopped changes that host tools would run, and neutralized them.", "alert"))
        lines += [("  " + clean(line), "warn") for line in snap.reports.get(row.name, [])]
        lines.append(("  R: review and resume (asks whether they were yours) · A: accept them as yours", "bold"))
    elif row.guard == "unguarded":
        lines.append(("  ✗ No guard is watching this running sandbox.", "alert"))
        lines.append(("    The agent could plant git hooks or config that your git would run.", "alert"))
        lines.append(("    S (kbx start) checks the checkout and starts the guard.", "bold"))
    elif row.guard == "watching":
        lines.append(("  ✓ watching the checkout's git hooks, config and protected files", "ok"))
    elif row.guard == "off":
        lines.append(("  ⚠ off in the config ([workspace] guard = false)", "warn"))
    elif row.mode == "clone":
        lines.append(("  not needed: clone mode keeps the checkout out of the sandbox", "dim"))
    else:
        lines.append(("  starts with the sandbox", "dim"))
    return lines


def health_section(row: Row, details: Details | None) -> list[Line]:
    lines = [_section("Health")]
    if not row.running:
        return [*lines, _not_running(row, row.state)]
    if details is None or details.state != row.state:
        return [*lines, ("  …", "dim")]
    if details.health is None:
        return [*lines, ("  no ready record yet (starting?); l shows the logs", "warn")]
    if not details.health:
        return [*lines, ("  ✓ seeds, start.sh and services ok", "ok")]
    return [*lines, *[(f"  ⚠ {clean(p)}", "warn") for p in details.health], ("  l shows the logs", "dim")]


def git_section(row: Row, details: Details | None) -> list[Line]:
    lines = [_section(f"Git ({row.mode} mode)")]
    if not row.running:
        note = "start the sandbox to see its git state" if row.mode == "mount" else "start the sandbox to see its clone"
        return [*lines, _not_running(row, note)]
    if details is None or details.state != row.state:
        return [*lines, ("  …", "dim")]
    for text in details.git or ["-"]:
        style = (
            "warn"
            if text.startswith(("unfetched", "git in the sandbox failed")) or "uncommitted change" in text
            else "normal"
        )
        lines.append(("  " + clean(text), style))
    if row.mode == "clone":
        lines.append(("  f: kbx fetch · y: kbx sync", "dim"))
    return lines


def agents_section(row: Row, details: Details | None, snap: Snapshot) -> list[Line]:
    lines = [_section("Agents")]
    if not row.running:
        return [*lines, _not_running(row, "start the sandbox to see the installed versions")]
    if details is None or details.state != row.state:
        return [*lines, ("  …", "dim")]
    for name in agents.AGENTS:
        installed = details.versions.get(name)
        text = f"  {name:<7} {clean(installed or 'not found')}"
        style = "normal" if installed else "warn"
        latest = (snap.latest or {}).get(name)
        if latest and installed and latest != installed:
            text += f"   {latest} published; u updates"
            style = "warn"
        elif latest and installed:
            text += "   current"
        lines.append((text, style))
        login = details.logins.get(name)
        if installed and login is not None:
            ok, detail = login
            mark = {True: "✓", False: "✗", None: "?"}[ok]
            lines.append((f"          login: {mark} {clean(detail)}", {True: "dim", False: "warn", None: "dim"}[ok]))
        if name == "codex" and details.daemon is not None:
            daemon = details.daemon
            lines.append((f"          remote control: {clean(daemon.line)}", "normal" if daemon.ok else "dim"))
            if daemon.version and installed and daemon.version != installed:
                note = f"          the daemon runs {clean(daemon.version)}; it switches at the next sandbox start"
                lines.append((note, "dim"))
    if snap.latest_error:
        lines.append(("  " + clean(snap.latest_error), "dim"))
    elif snap.latest is None:
        lines.append(("  U checks for newer versions", "dim"))
    return lines


def staleness_section(row: Row, config: Config, snap: Snapshot) -> list[Line]:
    notes = status.stale(row, config, snap.image_id) + snap.drift
    lines = [_section("Image and config")]
    if not notes:
        return [*lines, ("  ✓ matches the current image and config", "ok")]
    return [*lines, *[(f"  ⚠ {clean(n)}", "warn") for n in notes]]


def detail_lines(row: Row, snap: Snapshot, config: Config, home: Path) -> list[Line]:
    details = snap.details.get(row.name)
    title = f"{row.name} · {short_path(row.project_dir, home)} · {row.mode} mode · {row.state}"
    if details is not None and row.running:
        title += f" · details {ago(max(0.0, time.time() - details.at))} old"
    if row.name in snap.busy:
        title += " · refreshing…"
    lines: list[Line] = [(clean(title), "bold")]
    if row.orphan:
        lines.append(("  ⚠ The project directory is gone: only t (stop) and D (remove) work here.", "warn"))
    lines += guard_section(row, snap)
    lines += health_section(row, details)
    lines += git_section(row, details)
    lines += agents_section(row, details, snap)
    lines += staleness_section(row, config, snap)
    if details is not None and details.errors:
        lines.append(_section("Errors"))
        lines += [("  " + clean(e), "warn") for e in details.errors]
    return lines


def help_lines(width: int) -> list[Line]:
    key_width = max(len(key) for key, _ in HELP)
    lines: list[Line] = [("Keys", "bold")]
    for key, text in HELP:
        if width < NARROW:  # the key on its own line, so the text keeps the width
            lines.append((f"  {key}", "bold"))
            lines += [(part, "normal") for part in wrap(f"    {text}", width - 1, hang=4)]
            continue
        line = f"  {key:<{key_width}} {text}"
        lines += [(part, "normal") for part in wrap(line, width - 1, hang=key_width + 3)]
    lines.append(("", "normal"))
    notes = (
        "Commands run in the sandbox's project directory, exactly as on the command line. "
        "Attach and shell come back here when you detach."
    )
    lines += [(part, "dim") for part in wrap(notes, width - 1)]
    return lines


# The longest that fits is shown.
FOOTERS = (
    "Enter attach · s shell · S start · t stop · R resume · f/y fetch/sync · Tab details · ? keys · q quit",
    "Enter attach · s shell · t stop · Tab details · ? keys · q quit",
    "Enter attach · Tab details · ? keys · q",
    "? keys · q quit",
)
ZOOM_FOOTERS = (
    "↑↓ scroll · ←→ other sandbox · Tab/Esc back to the list · Enter attach · ? keys · q quit",
    "↑↓ scroll · ←→ sandbox · Tab back · ? keys",
    "↑↓ scroll · Tab back",
)


def footer(width: int, zoom: bool) -> str:
    options = ZOOM_FOOTERS if zoom else FOOTERS
    return next((text for text in options if width_of(text) <= width - 1), options[-1])


# --- terminal ---


class Capture(io.TextIOBase):
    """Stands in for stdout/stderr while curses owns the screen; keeps the last line."""

    def __init__(self) -> None:
        self.last = ""

    def write(self, text: str) -> int:
        for line in text.splitlines():
            if line.strip():
                self.last = line.strip()
        return len(text)

    def flush(self) -> None:
        pass


@dataclass(frozen=True)
class Action:
    args: tuple[str, ...]
    interactive: bool = False  # hands over the terminal: no "press Enter" on success
    confirm: str = ""


def _noop(_signum: int, _frame: FrameType | None) -> None:
    pass


class App:
    def __init__(
        self,
        screen: curses.window,
        monitor: Monitor,
        *,
        docker: Docker,
        paths: Paths,
        config: Config,
        entry: Path,
    ) -> None:
        self.screen = screen
        self.monitor = monitor
        self.docker = docker
        self.paths = paths
        self.config = config
        self.entry = entry
        self.home = paths.home
        self.selected = 0
        self.scroll = 0
        self.show_help = False
        self.zoom = False  # full-screen details
        self.message = ""
        self.capture = Capture()
        self.styles: dict[str, int] = {}
        self.real: tuple[IO[str], IO[str]] = (sys.stdout, sys.stderr)

    # --- setup and drawing ---

    def setup(self) -> None:
        try:
            curses.curs_set(0)
        except curses.error:
            pass
        self.screen.keypad(True)
        self.screen.timeout(250)
        plain = {"normal": curses.A_NORMAL, "bold": curses.A_BOLD, "dim": curses.A_DIM}
        self.styles = {
            **plain,
            "ok": curses.A_NORMAL,
            "warn": curses.A_BOLD,
            "alert": curses.A_BOLD | curses.A_REVERSE,
            "head": curses.A_REVERSE,
            "select": curses.A_REVERSE,
        }
        if curses.has_colors():
            curses.start_color()
            try:
                curses.use_default_colors()
                background = -1
            except curses.error:
                background = curses.COLOR_BLACK
            for number, color in ((1, curses.COLOR_GREEN), (2, curses.COLOR_YELLOW), (3, curses.COLOR_RED)):
                curses.init_pair(number, color, background)
            self.styles.update(
                ok=curses.color_pair(1),
                warn=curses.color_pair(2),
                alert=curses.color_pair(3) | curses.A_BOLD,
            )

    def put(self, y: int, text: str, style: str, width: int) -> None:
        try:
            self.screen.addstr(y, 0, cut(text, width - 1), self.styles.get(style, curses.A_NORMAL))
        except curses.error:
            pass  # the bottom-right cell, or a terminal that shrank mid-draw

    def draw(self, snap: Snapshot) -> None:
        self.screen.erase()
        height, width = self.screen.getmaxyx()
        if height < 6 or width < 24:
            self.put(0, "kbx dash: too small", "warn", width)
            self.screen.refresh()
            return
        full = self.zoom or self.show_help
        position = f"{self.selected + 1}/{len(snap.rows)}" if self.zoom and snap.rows else ""
        text, style = header_line(snap, width, position)
        self.put(0, text, style, width)
        if self.message:
            bottom = [(line, "bold") for line in wrap(self.message, width - 1)][-4:]
        elif self.capture.last:
            bottom = [(line, "bold") for line in wrap(f"note: {clean(self.capture.last)}", width - 1)][-2:]
        else:
            bottom = [(footer(width, self.zoom), "head")]
        y = 1
        if not full:
            y = self.draw_list(snap, width, height) + 1
            self.put(y - 1, "─" * (width - 1), "dim", width)
        body = wrap_lines(self.body(snap, width), width - 1)
        room = max(0, height - y - len(bottom))
        self.scroll = max(0, min(self.scroll, len(body) - room))
        for offset, (text, style) in enumerate(body[self.scroll : self.scroll + room]):
            self.put(y + offset, text, style, width)
        for offset, (text, style) in enumerate(bottom):
            self.put(height - len(bottom) + offset, text, style, width)
        self.screen.refresh()

    def draw_list(self, snap: Snapshot, width: int, height: int) -> int:
        """Draw the list from row 1, scrolled so the selection shows; returns the next free row."""
        head, groups = list_view(snap, self.config, self.home, self.selected, width)
        lines = [line for group in groups for line in group]
        starts = [sum(len(g) for g in groups[:index]) for index in range(len(groups))]
        room = max(len(groups[0]) if groups else 1, min(len(lines), height // 2 - len(head)))
        chosen = min(self.selected, len(groups) - 1)
        first = 0
        if groups:
            first = min(starts[chosen], max(0, starts[chosen] + len(groups[chosen]) - room))
        shown = [*head, *lines[first : first + room]]
        for offset, (text, style) in enumerate(shown):
            self.put(1 + offset, text, style, width)
        return 1 + len(shown)

    def body(self, snap: Snapshot, width: int) -> list[Line]:
        if self.show_help:
            return help_lines(width)
        row = self.current(snap)
        if row is None:
            return [_section("Image"), *[(f"  ⚠ {clean(n)}", "warn") for n in snap.drift]] if snap.drift else []
        return detail_lines(row, snap, self.config, self.home)

    def current(self, snap: Snapshot) -> Row | None:
        if not snap.rows:
            return None
        self.selected = max(0, min(self.selected, len(snap.rows) - 1))
        return snap.rows[self.selected]

    # --- input ---

    def ask(self, question: str) -> str:
        """One key from the footer prompt."""
        self.message = question
        self.draw(self.monitor.snapshot())
        self.screen.timeout(-1)
        try:
            key = self.screen.getch()
        finally:
            self.screen.timeout(250)
            self.message = ""
        return chr(key) if 0 <= key < 256 else ""

    def confirm(self, question: str) -> bool:
        return self.ask(f"{question} [y/N] ").lower() == "y"

    def loop(self) -> int:
        self.setup()
        sys.stdout = sys.stderr = self.capture
        try:
            while True:
                snap = self.monitor.snapshot()
                row = self.current(snap)
                self.monitor.select(row.name if row else None)
                self.draw(snap)
                key = self.screen.getch()
                if key == -1:
                    continue
                if key == ord("q") or (key == 27 and not self.zoom and not self.show_help):
                    return 0
                self.handle(key, snap, row)
        finally:
            sys.stdout, sys.stderr = self.real

    def handle(self, key: int, snap: Snapshot, row: Row | None) -> None:
        if key == curses.KEY_RESIZE:
            return
        self.capture.last = ""
        if self.navigate(key, snap):
            return
        if key == curses.KEY_HOME:
            self.selected, self.scroll = 0, 0
            return
        if key == curses.KEY_END:
            self.selected, self.scroll = max(0, len(snap.rows) - 1), 0
            return
        if key in (curses.KEY_NPAGE, ord(" ")):
            self.scroll += max(1, self.screen.getmaxyx()[0] // 3)
            return
        if key == curses.KEY_PPAGE:
            self.scroll = max(0, self.scroll - max(1, self.screen.getmaxyx()[0] // 3))
            return
        char = chr(key) if 0 <= key < 256 else ""
        if char == "?":
            self.show_help = not self.show_help
            self.scroll = 0
            return
        if char in ("\t", "\x1b"):  # Tab toggles; Esc only leaves (quitting is handled by the loop)
            self.zoom = not self.zoom and char == "\t" and not self.show_help
            self.show_help = False
            self.scroll = 0
            return
        self.show_help = False
        if char == "g":
            self.monitor.refresh()
            return
        if char == "U":
            self.monitor.check_latest()
            return
        if char == "b":
            self.run(Action(("build",)), self.home, None)
            return
        if row is None:
            return
        if row.orphan:
            self.orphan(char, row)
            return
        action = self.action(char, row)
        if action is not None:
            self.run(action, row.project_dir, row.name)

    def navigate(self, key: int, snap: Snapshot) -> bool:
        """Selection and scrolling. In full-screen details ↑↓ scroll (a phone's
        swipe sends them) and ←→ switch sandboxes."""
        full = self.zoom or self.show_help
        up, down = (curses.KEY_UP, ord("k")), (curses.KEY_DOWN, ord("j"))
        if full and key in (*up, *down):
            self.scroll = max(0, self.scroll + (-1 if key in up else 1))
            return True
        switch = {curses.KEY_LEFT: -1, curses.KEY_RIGHT: 1} if self.zoom else {}
        if not full:
            switch = {key: -1 for key in up} | {key: 1 for key in down}
        if key in switch:
            self.selected = max(0, min(self.selected + switch[key], len(snap.rows) - 1))
            self.scroll = 0
            return True
        return False

    def action(self, char: str, row: Row) -> Action | None:
        if char in ("\n", "\r", "a"):
            return self.attach(row)
        if char in ("c", "x", "p"):
            return Action(({"c": "claude", "x": "codex", "p": "pi"}[char],), interactive=True)
        simple = {
            "s": Action(("shell",), interactive=True),
            "S": Action(("start",)),
            "t": Action(("stop",), confirm=f"Stop {row.name}?"),
            "r": Action(("recreate",), confirm=f"Recreate {row.name} from the current image (volumes are kept)?"),
            "D": Action(("rm",)),
            "R": Action(("resume",)),
            "A": Action(
                ("resume", "--accept"), confirm="Restore the agent's versions of the changes the guard stopped?"
            ),
            "f": Action(("fetch",)),
            "y": Action(("sync",)),
            "u": Action(("update",)),
            "d": Action(("diff",), interactive=True),
            "l": Action(("logs",)),
        }
        return simple.get(char)

    def attach(self, row: Row) -> Action | None:
        live = list(row.sessions or ())
        if row.running and len(live) == 1:
            return Action(("attach", live[0]), interactive=True)
        which = "Attach to which agent?" if live else "No agent is running. Start which?"
        choice = self.ask(f"{which} [c]laude [x]codex [p]i ")
        name = {"c": "claude", "x": "codex", "p": "pi"}.get(choice)
        if name is None:
            return None
        return Action(("attach", name) if name in live else (name,), interactive=True)

    def orphan(self, char: str, row: Row) -> None:
        """The project directory is gone, so commands (which work from it) cannot run."""
        sb = row.sandbox
        try:
            if char == "t" and self.confirm(f"Stop {row.name}?"):
                self.message = f"Stopping {row.name}…"
                self.draw(self.monitor.snapshot())
                sandbox_mod.stop(self.docker, sb)
            elif char == "D" and self.confirm(
                f"Remove {row.name} with its volumes {sb.home_volume} and {sb.docker_volume}? This cannot be undone."
            ):
                self.message = f"Removing {row.name}…"
                self.draw(self.monitor.snapshot())
                sandbox_mod.remove_container(self.docker, sb)
                sandbox_mod.remove_volumes(self.docker, sb)
                guard.remove(self.paths, row.name)
        except KbxError as exc:
            self.capture.last = str(exc)
        finally:
            self.message = ""
        self.monitor.refresh(row.name)

    # --- running commands ---

    def run(self, action: Action, cwd: Path, name: str | None) -> None:
        if action.confirm and not self.confirm(action.confirm):
            return
        self.monitor.pause()
        curses.def_prog_mode()
        curses.endwin()
        sys.stdout, sys.stderr = self.real
        # Ctrl-C and Ctrl-\ belong to the command. A handler (unlike SIG_IGN) is
        # reset to the default in the child at exec.
        previous = {sig: signal.signal(sig, _noop) for sig in (signal.SIGINT, signal.SIGQUIT)}
        try:
            code = run_command(self.entry, action.args, cwd)
            if not action.interactive or code != 0:
                print(f"\n[kbx {' '.join(action.args)}: exit {code}] Press Enter to return to the dashboard.")
                try:
                    input()
                except (EOFError, KeyboardInterrupt):
                    pass
        finally:
            for sig, handler in previous.items():
                signal.signal(sig, handler)
            sys.stdout = sys.stderr = self.capture
            curses.reset_prog_mode()
            size = shutil.get_terminal_size()
            try:
                curses.resizeterm(size.lines, size.columns)
            except curses.error:
                pass
            self.screen.clear()
            self.monitor.resume(name)


def run_command(entry: Path, args: Sequence[str], cwd: Path) -> int:
    print(f"$ kbx {' '.join(args)}    (in {cwd})", flush=True)
    try:
        return subprocess.run([sys.executable, str(entry), *args], cwd=str(cwd), check=False).returncode
    except OSError as exc:
        print(f"kbx: cannot run the command: {exc}", file=sys.stderr)
        return 1


def set_locale() -> None:
    """curses draws UTF-8 only under a UTF-8 locale; LANG may name one that is not installed."""
    for name in ("", "C.UTF-8", "C.utf8"):
        try:
            locale.setlocale(locale.LC_ALL, name)
            return
        except locale.Error:
            continue


def run(
    docker: Docker,
    paths: Paths,
    config: Config,
    resolved: list[Resolved],
    *,
    entry: Path,
) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise KbxError("the dashboard needs a terminal (stdin and stdout must be a TTY); `kbx ls` lists sandboxes")
    set_locale()
    os.environ.setdefault("ESCDELAY", "25")  # Esc quits without the default one-second wait
    docker.binary_path()  # fail before the screen switches if docker is missing
    monitor = Monitor(docker, paths, config, resolved)
    monitor.start()
    try:
        return curses.wrapper(
            lambda screen: App(screen, monitor, docker=docker, paths=paths, config=config, entry=entry).loop()
        )
    finally:
        monitor.stop()
