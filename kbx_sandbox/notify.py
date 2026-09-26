"""kbx-notify: what each agent session is doing, for the host.

The agents call it from their own hooks (seeded by the `notify` module):

  kbx-notify hook claude     Claude Code hooks, with the event JSON on stdin
  kbx-notify codex JSON      Codex's `notify` program, with the event as argument

Each call records the session's state (working, waiting for you, done) in
/run/kbx/agents/<agent>.json and appends it to /run/kbx/agents/events.jsonl.
Only sessions the launcher started count: it sets KBX_SESSION=<agent> in them,
so an agent run by hand in `kbx shell` stays quiet.

  kbx-notify follow          for the host: every new event as a JSON line, and
                             the live sessions every TICK seconds

A hook must never slow down or break its agent: failures are ignored and
nothing is printed (Claude adds a UserPromptSubmit hook's output to the prompt).
"""

from __future__ import annotations

import json
import os
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import session

STATES = session.STATES
EVENTS = STATES / "events.jsonl"
MAX_EVENTS = 256 * 1024  # bytes; the log starts over beyond this (the host follows it live)
MAX_MESSAGE = 300
TICK = 30.0
POLL = 0.5

WORKING, WAITING, DONE = "working", "waiting", "done"


def _message(value: object) -> str:
    text = " ".join(str(value or "").split())
    return text if len(text) <= MAX_MESSAGE else text[: MAX_MESSAGE - 1] + "…"


def current(agent: str, root: Path = STATES) -> str | None:
    try:
        return json.loads((root / f"{agent}.json").read_text(encoding="utf-8")).get("state")
    except (OSError, ValueError, AttributeError):
        return None


def record(agent: str, state: str, message: str = "", root: Path = STATES) -> None:
    event = {"agent": agent, "state": state, "message": _message(message), "at": time.time()}
    line = json.dumps(event) + "\n"
    temp = root / f".{agent}.json.tmp"
    temp.write_text(line, encoding="utf-8")
    os.replace(temp, root / f"{agent}.json")
    events = root / EVENTS.name
    try:
        if events.stat().st_size > MAX_EVENTS:
            events.unlink()
    except FileNotFoundError:
        pass
    with open(events, "a", encoding="utf-8") as handle:
        handle.write(line)


def claude_state(event: Mapping[str, Any]) -> tuple[str, str] | None:
    """(state, message) for one Claude Code hook event, or None to record nothing."""
    name = event.get("hook_event_name")
    if name in ("UserPromptSubmit", "PostToolUse"):
        return WORKING, ""
    if name == "Stop":
        return DONE, str(event.get("last_assistant_message") or "")
    if name == "Notification":
        kind = event.get("notification_type")
        message = str(event.get("message") or "")
        if kind in ("permission_prompt", "elicitation_dialog"):
            return WAITING, message
        if kind is None and message and "waiting for your input" not in message:
            return WAITING, message  # versions without notification_type
    return None  # idle_prompt repeats Stop; auth_success and others are not states


def codex_state(event: Mapping[str, Any]) -> tuple[str, str] | None:
    kind = event.get("type")
    if kind == "agent-turn-complete":
        return DONE, str(event.get("last-assistant-message") or "")
    if kind == "approval-requested":
        return WAITING, "Codex needs your approval"
    return None


def hook(agent: str, raw: str, env: Mapping[str, str], root: Path = STATES) -> None:
    if env.get("KBX_SESSION") != agent:
        return
    event = json.loads(raw)
    if not isinstance(event, dict):
        return
    if agent == "claude" and event.get("hook_event_name") == "SessionEnd":
        (root / "claude.json").unlink(missing_ok=True)
        return
    found = claude_state(event) if agent == "claude" else codex_state(event)
    if found is None:
        return
    state, message = found
    if state == WORKING and current(agent, root) == WORKING:
        return  # PostToolUse fires for every tool; write only on a change
    record(agent, state, message, root)


def _emit(data: Mapping[str, Any]) -> None:
    sys.stdout.write(json.dumps(data) + "\n")
    sys.stdout.flush()


def follow(root: Path = STATES, tick: float = TICK, poll: float = POLL) -> None:
    events = root / EVENTS.name
    try:
        offset = events.stat().st_size
    except OSError:
        offset = 0
    _emit({"type": "sessions", "sessions": session.status()})
    next_tick = time.monotonic() + tick
    while True:
        try:
            size = events.stat().st_size
        except OSError:
            size = 0
        if size < offset:
            offset = 0  # started over
        if size > offset:
            with open(events, "rb") as handle:
                handle.seek(offset)
                data = handle.read(size - offset)
            end = data.rfind(b"\n") + 1  # complete lines only
            offset += end
            for line in data[:end].splitlines():
                try:
                    event = json.loads(line)
                except ValueError:
                    continue
                if isinstance(event, dict):
                    agent = str(event.get("agent", ""))
                    event["attached"] = session.attached(session.SESSIONS / f"{agent}.sock")
                    _emit({"type": "event", **event})
        if time.monotonic() >= next_tick:
            next_tick = time.monotonic() + tick
            _emit({"type": "sessions", "sessions": session.status()})
        time.sleep(poll)


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if args == ["follow"]:
        try:
            follow()
        except (BrokenPipeError, KeyboardInterrupt):
            pass
        return 0
    if len(args) == 2 and args[0] == "hook" and args[1] == "claude":
        agent, raw = "claude", sys.stdin.read()
    elif len(args) >= 2 and args[0] == "codex":
        agent, raw = "codex", args[-1]  # Codex appends the event JSON
    else:
        print(__doc__, file=sys.stderr)
        return 2
    try:
        hook(agent, raw, os.environ)
    except (OSError, ValueError):
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
