"""`kbx diff`: review the agent's changes without running host git on them.

The rule of kbx/git.py holds: the host never runs git in a repository the
agent can write, because `.git/config` and `.gitattributes` can make git run
programs (fsmonitor, filters). So git runs in the sandbox, at the working
directory, and the host only reads its output, which is untrusted text. Git
prints no colour; every control character becomes `?` and the host adds the
colours itself, so a file cannot hide lines (conceal, black on black) or
drive the terminal, and an escape sequence in a file stays visible.

What it shows: the working tree against HEAD, untracked files included (as
new files, through a throwaway index; the real one is not touched), or against
the commit HEAD was at when the sandbox started (`--since-start`, recorded on
the host at each start), or against any revision.

The agent is root in its VM and could make its own git lie. This is a
convenient view of the work, not a proof about it; the guard and your own
review before running anything on the host remain the protection.
"""

from __future__ import annotations

import re
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO

from . import git
from .docker import Docker
from .errors import KbxError
from .paths import Paths
from .sandbox import Sandbox
from .text import UNSAFE

# $1 "--stat" or "", $2 base revision or "", then pathspecs.
SCRIPT = r"""
stat=$1 base=$2
shift 2
g() { git -c core.fsmonitor=false -c core.quotePath=false "$@"; }
tmp=$(mktemp) || exit 1
trap 'rm -f "$tmp"' EXIT
index=$(g rev-parse --git-path index) || exit 1
if [ -f "$index" ]; then cp "$index" "$tmp"; else rm -f "$tmp"; fi
export GIT_INDEX_FILE="$tmp"
g ls-files -z -o --exclude-standard -- "$@" | xargs -0 -r git -c core.fsmonitor=false add -N -- >/dev/null || exit 1
head=HEAD
g rev-parse -q --verify HEAD >/dev/null || head=$(g hash-object -t tree /dev/null)
if [ -n "$base" ]; then
  g log --no-show-signature --no-decorate --color=never --format='%h %s' --end-of-options "$base..HEAD" -- || exit 1
  echo
  head=$base
fi
# shellcheck disable=SC2086  # $stat is empty or one flag
g diff --no-ext-diff --no-textconv --color=never $stat --end-of-options "$head" -- "$@"
"""

RESET, BOLD, RED, GREEN, YELLOW, CYAN = "\x1b[m", "\x1b[1m", "\x1b[31m", "\x1b[32m", "\x1b[33m", "\x1b[36m"
HUNK = re.compile(r"^(@@ [^@]* @@)(.*)$")
STAT = re.compile(r"^( .* \| +[0-9]+ )(\+*)(-*)$")


def clean_line(line: str) -> str:
    """One output line, safe for a terminal: an escape in a file shows as `?`."""
    return UNSAFE.sub("?", line.rstrip("\n").replace("\r", ""))


class Colorizer:
    """Colours git's plain output on the host, by line type (git's own colours
    cannot be told apart from escape sequences inside the files)."""

    def __init__(self, log: bool) -> None:
        self.log = log  # commit subjects come first, up to a blank line
        self.header = False

    def __call__(self, line: str) -> str:
        if self.log:
            if not line:
                self.log = False
                return line
            sha, _, subject = line.partition(" ")
            return f"{YELLOW}{sha}{RESET} {subject}"
        if line.startswith("diff --git "):
            self.header = True
        if self.header:
            if line.startswith("@@"):
                self.header = False
            else:
                return f"{BOLD}{line}{RESET}"
        if line.startswith("@@"):
            match = HUNK.match(line)
            return f"{CYAN}{match.group(1)}{RESET}{match.group(2)}" if match else f"{CYAN}{line}{RESET}"
        if line.startswith("+"):
            return f"{GREEN}{line}{RESET}"
        if line.startswith("-"):
            return f"{RED}{line}{RESET}"
        stat = STAT.match(line)
        if stat:
            return f"{stat.group(1)}{GREEN}{stat.group(2)}{RED}{stat.group(3)}{RESET}"
        return line


# --- start points ---


def _start_file(paths: Paths, name: str) -> Path:
    return paths.data_dir / "starts" / name


def record_start(docker: Docker, paths: Paths, sandbox: Sandbox) -> None:
    """Remember the commit HEAD is at as the sandbox starts, for `--since-start`."""
    target = _start_file(paths, sandbox.name)
    result = docker.exec(
        sandbox.name, ["git", "-C", sandbox.workdir, "rev-parse", "-q", "--verify", "HEAD"], check=False, timeout=60
    )
    sha = result.stdout.decode("utf-8", "replace").strip()
    target.parent.mkdir(parents=True, exist_ok=True)
    if result.returncode == 0 and git.SHA.match(sha):
        target.write_text(sha + "\n")
    else:
        target.unlink(missing_ok=True)


def start_point(paths: Paths, name: str) -> str:
    try:
        sha = _start_file(paths, name).read_text().strip()
    except OSError:
        sha = ""
    if not git.SHA.match(sha):
        raise KbxError(
            "no start point recorded for this sandbox (it starts recording at its next start); use --since REV"
        )
    return sha


def forget_start(paths: Paths, name: str) -> None:
    _start_file(paths, name).unlink(missing_ok=True)


# --- output ---


def pager(env: Mapping[str, str]) -> subprocess.Popen[bytes] | None:
    command = env.get("KBX_PAGER") or env.get("GIT_PAGER") or env.get("PAGER") or "less"
    if command in ("", "cat"):
        return None
    pager_env = dict(env)
    pager_env.setdefault("LESS", "FRX")  # quit if one screen, keep colours, no screen clear
    return subprocess.Popen(command, shell=True, stdin=subprocess.PIPE, env=pager_env)  # noqa: S602 (the user's pager)


def run(
    docker: Docker,
    sandbox: Sandbox,
    *,
    base: str,
    stat: bool,
    pathspecs: Sequence[str],
    env: Mapping[str, str],
    out: IO[bytes] | None = None,
) -> int:
    tty = out is None and sys.stdout.isatty()
    paint = Colorizer(bool(base)) if tty and not env.get("NO_COLOR") else None
    argv = ["sh", "-c", SCRIPT, "sh", "--stat" if stat else "", base, *pathspecs]
    args = docker.exec_args(sandbox.name, argv, workdir=sandbox.workdir)
    process = subprocess.Popen(
        [docker.binary_path(), *args], stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE
    )
    view = pager(env) if tty else None
    sink = view.stdin if view is not None and view.stdin is not None else (out or sys.stdout.buffer)
    assert process.stdout is not None and process.stderr is not None
    try:
        for raw in process.stdout:
            line = clean_line(raw.decode("utf-8", "replace"))
            sink.write(((paint(line) if paint else line) + "\n").encode())
        sink.flush()
    except BrokenPipeError:
        process.kill()  # the pager was closed early
    finally:
        if view is not None:
            try:
                sink.close()
            except BrokenPipeError:
                pass
            view.wait()
    errors = process.stderr.read().decode("utf-8", "replace")
    status = process.wait()
    if status not in (0, -9):
        lines = [clean_line(line) for line in errors.splitlines() if line.strip()]
        raise KbxError(f"git in the sandbox failed: {lines[-1] if lines else f'exit {status}'}")
    return 0


def revision_ok(value: str) -> bool:
    return bool(value) and not value.startswith("-") and not any(ch.isspace() or ord(ch) < 32 for ch in value)
