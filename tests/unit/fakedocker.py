#!/usr/bin/env python3
"""A fake `docker` for unit tests, installed on PATH as `docker`.

State lives in $FAKE_DOCKER_STATE/state.json; every call is appended to
calls.jsonl. `docker exec` runs the command on the host with /home/agent and
/tmp/kbx- mapped into $FAKE_DOCKER_STATE, so git flows run for real, while
sandbox-only tools (kbx-init, kbx-session, agents…) are emulated.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

ROOT = Path(os.environ["FAKE_DOCKER_STATE"])
STATE = ROOT / "state.json"
HOME = ROOT / "home"


def load() -> dict[str, Any]:
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {"containers": {}, "images": {}, "networks": {}, "volumes": {}}


def save(state: dict[str, Any]) -> None:
    # Atomic: the dashboard tests run several fake dockers at once.
    temp = STATE.with_name(f"state.{os.getpid()}.tmp")
    temp.write_text(json.dumps(state, indent=1))
    os.replace(temp, STATE)


def record(args: list[str]) -> None:
    with (ROOT / "calls.jsonl").open("a") as handle:
        handle.write(json.dumps(args) + "\n")


def mapped(text: str) -> str:
    # One pass, so a mapped path (itself under /tmp) is never mapped again.
    targets = {"/home/agent": str(HOME), "/tmp/kbx-": str(ROOT / "tmp") + "/kbx-"}
    return re.sub("/home/agent|/tmp/kbx-", lambda m: targets[m.group(0)], text)


def labels_from(args: list[str]) -> dict[str, str]:
    labels: dict[str, str] = {}
    for index, arg in enumerate(args):
        if arg == "--label" and index + 1 < len(args):
            key, _, value = args[index + 1].partition("=")
            labels[key] = value
    return labels


def option(args: list[str], name: str) -> str | None:
    return args[args.index(name) + 1] if name in args else None


def emulate(state: dict[str, Any], argv: list[str], stdin: bytes) -> int | None:
    """Sandbox-only commands. None means: run it for real."""
    behave = state.get("behave", {})
    if argv[:2] == ["kbx-init", "status"]:
        if not behave.get("ready", True):
            return 1
        print(json.dumps(behave.get("ready_record", {"boot_id": "x", "seed": {}, "start": {}, "services": {}})))
        return 0
    if argv[:1] in (["kbx-init"], ["kbx-seed"], ["kbx-onboard"], ["tail"]):
        return 0
    if argv[:2] == ["kbx-session", "list"]:
        print(json.dumps(behave.get("sessions", [])))
        return 0
    if argv[:2] == ["kbx-session", "status"]:
        states = behave.get("session_states", {})
        print(json.dumps({name: states.get(name, {"attached": False}) for name in behave.get("sessions", [])}))
        return 0
    if argv == ["kbx-notify", "follow"]:
        for line in behave.get("follow", []):
            print(json.dumps(line), flush=True)
        time.sleep(float(behave.get("follow_hold", 60)))  # a live stream stays open
        return 0
    if argv[:1] == ["kbx-login-status"]:
        logins = behave.get("logins", {})
        names = argv[1:] or ["claude", "codex", "pi"]
        print(json.dumps({n: logins.get(n, {"ok": True, "detail": "fake login"}) for n in names}))
        return 0
    if argv[:2] == ["kbx-session", "alive"]:
        return 0 if argv[2] in behave.get("sessions", []) else 1
    if argv[:2] == ["python3", "-c"] and "sys.exit(0 if exc.errno" in argv[2]:
        status = behave.get("firewall", 0)
        if status:
            print("111")
        return int(status)
    if len(argv) == 2 and argv[1] == "--version" and argv[0] in ("claude", "codex", "pi"):
        versions = behave.get("versions", {})
        print(versions.get(argv[0], "1.0.0"))
        return 0
    if argv[0] in ("claude", "update-codex-native", "npm") and argv[1:2] != ["--version"]:
        if argv[0] == "claude" and argv[1:] == ["update"]:
            versions = behave.setdefault("versions", {})
            versions["claude"] = behave.get("claude_after", versions.get("claude", "1.0.0"))
            save(state)
        return int(behave.get("update_status", 0))
    if argv[:3] == ["codex", "login", "status"]:
        if behave.get("codex_login", True):
            print("Logged in using ChatGPT")
            return 0
        print("Not logged in")
        return 1
    if argv[:2] == ["codex", "app-server"]:
        print(behave.get("daemon", "daemon running (codex 1.0.0)"))
        return int(behave.get("daemon_status", 0))
    if argv[:2] == ["codex", "remote-control"]:
        return 0
    if argv[:1] == ["dtach"] or argv == ["bash", "-l"]:
        # The final attach: record what would run in the terminal.
        (ROOT / "attached.json").write_text(json.dumps({"argv": argv, "herdr": os.environ.get("HERDR_AGENT")}))
        print("fake-attached")
        return 0
    if argv[:1] == ["kbx-clip-put"]:
        (ROOT / "clip").write_bytes(stdin)
        return 0
    return None


def do_exec(state: dict[str, Any], args: list[str]) -> int:
    index = 1
    workdir: str | None = None
    env = dict(os.environ)
    env["HOME"] = str(HOME)
    # The "sandbox" has its own global git config, like the real one.
    for key in ("GIT_CONFIG_GLOBAL", "XDG_CONFIG_HOME", "GIT_DIR", "GIT_WORK_TREE"):
        env.pop(key, None)
    interactive = False
    tty = False
    while index < len(args) and args[index].startswith("-"):
        flag = args[index]
        if flag in ("-i", "-t", "-it"):
            interactive = interactive or "i" in flag
            tty = tty or "t" in flag
            index += 1
        elif flag in ("-u", "-w", "-e"):
            value = args[index + 1]
            if flag == "-w":
                workdir = mapped(value)
            elif flag == "-e":
                key, _, val = value.partition("=")
                env[key] = mapped(val)
            index += 2
        else:
            index += 1
    name = args[index]
    argv = [mapped(a) for a in args[index + 1 :]]
    container = state["containers"].get(name)
    if container is None or container["State"]["Status"] != "running":
        print(f"Error response from daemon: container {name} is not running", file=sys.stderr)
        return 1
    # A terminal never reaches EOF; only piped input is read.
    stdin = sys.stdin.buffer.read() if interactive and not tty else b""
    emulated = emulate(state, argv, stdin)
    if emulated is not None:
        return emulated
    (ROOT / "tmp").mkdir(exist_ok=True)
    HOME.mkdir(exist_ok=True)
    result = subprocess.run(argv, input=stdin, cwd=workdir or str(HOME), env=env, check=False)
    return result.returncode


def main() -> int:
    args = sys.argv[1:]
    record(args)
    state = load()
    if not args:
        return 0
    command = args[0]
    if command == "inspect" or (len(args) > 1 and args[1] == "inspect"):
        kind, names = (args[0], args[2:]) if args[1:2] == ["inspect"] else ("container", args[1:])
        table = {"container": "containers", "image": "images", "network": "networks", "volume": "volumes"}[kind]
        found = []
        for name in names:
            item = state[table].get(name)
            if item is None:
                print(f"Error: No such {kind}: {name}", file=sys.stderr)
                continue
            found.append({"Name": f"/{name}", **item} if kind == "container" else item)
        print(json.dumps(found))
        return 0 if len(found) == len(names) else 1
    if args[:2] == ["network", "create"]:
        name = args[-1]
        bridge = next((a.split("=", 1)[1] for a in args if a.startswith("com.docker.network.bridge.name=")), None)
        state["networks"][name] = {
            "IPAM": {"Config": [{"Subnet": option(args, "--subnet")}]},
            "Options": {"com.docker.network.bridge.name": bridge},
        }
        save(state)
        return 0
    if command == "create":
        name = option(args, "--name")
        assert name is not None
        image = next((state["images"][a] for a in reversed(args) if a in state["images"]), {})
        state["containers"][name] = {
            "State": {"Status": "created", "StartedAt": "0001-01-01T00:00:00Z"},
            "Config": {"Labels": labels_from(args)},
            "Image": image.get("Id", ""),
            "Args": args,
        }
        for index, arg in enumerate(args):
            if arg == "-v":
                volume = args[index + 1].split(":", 1)[0]
                if not volume.startswith("/"):
                    state["volumes"].setdefault(volume, {})
        save(state)
        return 0
    if command == "start":
        container = state["containers"][args[1]]
        container["State"]["Status"] = "running"
        container["State"]["StartedAt"] = state.get("behave", {}).get("started_at", "2020-01-01T00:00:00.123456789Z")
        container["Starts"] = container.get("Starts", 0) + 1
        save(state)
        return 0
    if command == "stop":
        state["containers"][args[-1]]["State"]["Status"] = "exited"
        save(state)
        return 0
    if command in ("pause", "unpause"):
        container = state["containers"][args[1]]
        wanted, other = ("running", "paused") if command == "pause" else ("paused", "running")
        if container["State"]["Status"] != wanted:
            print(f"Error response from daemon: container {args[1]} is not {wanted}", file=sys.stderr)
            return 1
        container["State"]["Status"] = other
        save(state)
        return 0
    if command == "rm":
        state["containers"].pop(args[-1], None)
        save(state)
        return 0
    if args[:2] == ["volume", "rm"]:
        state["volumes"].pop(args[2], None)
        save(state)
        return 0
    if command == "build":
        tag = option(args, "-t")
        assert tag is not None
        builds = state.get("builds", 0) + 1
        state["builds"] = builds
        state["images"][tag] = {"Id": f"sha256:build{builds}", "Config": {"Labels": labels_from(args)}}
        dockerfile = option(args, "-f")
        if dockerfile:
            (ROOT / "Dockerfile.last").write_text(Path(dockerfile).read_text())
        save(state)
        return int(state.get("behave", {}).get("build_status", 0))
    if command == "ps":
        for name, container in state["containers"].items():
            labels = container["Config"]["Labels"]
            if "kbx.name" in labels:
                print(f"{name}\t{container['State']['Status']}\t{labels.get('kbx.project', '')}")
        return 0
    if command == "logs":
        return 0
    if command == "stats":
        usage = state.get("behave", {}).get("stats", {})
        for name in args[1:]:
            if name in usage:
                print(f"{name}\t{usage[name][0]}\t{usage[name][1]} / 8GiB")
        return 0
    if command == "exec":
        return do_exec(state, args)
    print(f"fakedocker: unsupported command {args}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
