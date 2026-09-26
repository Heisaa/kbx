"""Per-agent argv, updates and prelaunch steps (remote control, Codex auth/search)."""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass

from .config import Config
from .docker import Docker
from .errors import KbxError
from .modules import Resolved
from .sandbox import Sandbox

PI_PACKAGE = "@earendil-works/pi-coding-agent"
VERSION = re.compile(r"\d+\.\d+\.\d+(?:[-+][0-9A-Za-z.-]+)?")


@dataclass(frozen=True)
class Agent:
    name: str
    binary: str
    home_prefix: str  # seeds under this path are reapplied before a launch
    update: tuple[str, ...]


AGENTS: dict[str, Agent] = {
    "claude": Agent("claude", "claude", ".claude/", ("claude", "update")),
    "codex": Agent("codex", "codex", ".codex/", ("update-codex-native",)),
    "pi": Agent("pi", "pi", ".pi/", ("npm", "install", "-g", "--no-fund", "--no-audit", f"{PI_PACKAGE}@latest")),
}


def get(name: str) -> Agent:
    try:
        return AGENTS[name]
    except KeyError:
        raise KbxError(f"unknown agent {name!r}; choose one of {', '.join(AGENTS)}") from None


def insert_remote_control(args: Sequence[str]) -> list[str]:
    """Add Claude's --remote-control after the options and before any `--`.

    The flag takes an optional name, so it must not precede a positional
    argument. Nothing is added if the user already passed it.
    """
    result: list[str] = []
    added = False
    for arg in args:
        if arg.startswith("--remote-control"):
            added = True
        if arg == "--" and not added:
            result.append("--remote-control")
            added = True
        result.append(arg)
    if not added:
        result.append("--remote-control")
    return result


def enabled(resolved: list[Resolved], name: str) -> bool:
    return any(item.name == name for item in resolved)


def command(agent: Agent, args: Sequence[str], config: Config, resolved: list[Resolved]) -> list[str]:
    launcher = config.launcher
    if agent.name == "claude":
        argv = list(args)
        if launcher.debug and "--debug" not in argv:
            argv = ["--debug", *argv]
        if launcher.remote_control:
            argv = insert_remote_control(argv)
        return ["claude", *argv]
    if agent.name == "codex":
        extra: list[str] = []
        if launcher.codex_search:
            extra += ["--search", "--enable", "standalone_web_search", "-c", "suppress_unstable_features_warning=true"]
        if enabled(resolved, "codex-chatgpt-auth"):
            # Keep the login/provider choice even if a session rewrites the config.
            extra += ["-c", "forced_login_method=chatgpt", "-c", "model_provider=openai"]
        return ["codex", *extra, *args]
    return ["pi", *args]


def version(docker: Docker, sandbox: Sandbox, agent: Agent) -> str | None:
    result = docker.exec(sandbox.name, [agent.binary, "--version"], check=False, timeout=60)
    match = VERSION.search(result.stdout.decode("utf-8", "replace"))
    return match.group(0) if match else None


def update(docker: Docker, sandbox: Sandbox, agent: Agent, *, strict: bool) -> None:
    """Update one agent in the home volume. Failures warn unless strict."""
    before = version(docker, sandbox, agent)
    print(f"→ Checking {agent.name} updates ({before or 'unknown'})…")
    status = docker.exec_passthrough(sandbox.name, list(agent.update), timeout=900)
    if status != 0:
        if strict:
            raise KbxError(f"updating {agent.name} failed (exit {status})")
        print(f"⚠ {agent.name} update failed; starting the installed version.", file=sys.stderr)
        return
    after = version(docker, sandbox, agent)
    if before and after and before != after:
        print(f"✓ {agent.name} updated: {before} → {after}")
    else:
        print(f"✓ {agent.name} is current ({after or before or 'unknown'})")


def reseed(docker: Docker, sandbox: Sandbox, agent: Agent) -> None:
    """Reapply seeds under this agent's config directory (enforced keys, new defaults)."""
    status = docker.exec_passthrough(sandbox.name, ["kbx-seed", "--only-prefix", agent.home_prefix, "--quiet"])
    if status != 0:
        print(f"⚠ reapplying {agent.name} defaults failed; see `kbx seed --status`", file=sys.stderr)


def run_before_launch(docker: Docker, sandbox: Sandbox, agent: Agent, resolved: list[Resolved]) -> None:
    """Rerun start.sh of modules that asked to run before this agent launches."""
    for item in resolved:
        if agent.name not in item.module.before_launch:
            continue
        status = docker.exec_passthrough(
            sandbox.name,
            ["kbx-init", "run-start", item.name],
            user=item.module.start_user,
        )
        if status != 0:
            print(f"⚠ {item.name}: start.sh failed before launching {agent.name}", file=sys.stderr)


def codex_remote_control(docker: Docker, sandbox: Sandbox) -> None:
    """Start Codex remote control if logged in with ChatGPT; warn otherwise."""
    status = docker.exec(sandbox.name, ["codex", "login", "status"], check=False, timeout=60)
    output = (status.stdout + status.stderr).decode("utf-8", "replace")
    if status.returncode != 0 or "ChatGPT" not in output:
        print("→ Codex remote control needs a ChatGPT login. Once, inside the sandbox:")
        print("    kbx shell   then   codex login --device-auth")
        print("  (enable MFA on the ChatGPT account first, or enrollment fails with HTTP 403)")
        return
    print("→ Starting Codex remote control…")
    start = docker.exec_passthrough(sandbox.name, ["codex", "remote-control", "start"], timeout=120)
    if start != 0:
        print(
            "⚠ Codex remote control could not start; continuing with local Codex. Check login, MFA and network access.",
            file=sys.stderr,
        )
        return
    _daemon_version_note(docker, sandbox)


def _daemon_version_note(docker: Docker, sandbox: Sandbox) -> None:
    installed = version(docker, sandbox, AGENTS["codex"])
    result = docker.exec(sandbox.name, ["codex", "app-server", "daemon", "status"], check=False, timeout=60)
    match = VERSION.search((result.stdout + result.stderr).decode("utf-8", "replace"))
    if installed and match and match.group(0) != installed:
        print(
            f"  note: the remote-control daemon runs Codex {match.group(0)}, installed is {installed}. "
            "It switches at the next sandbox start, or run `codex app-server daemon restart` "
            "inside (this ends active remote sessions)."
        )


def skip_onboarding(docker: Docker, sandbox: Sandbox, agent: Agent) -> None:
    """Mark first-run screens done and trust the workdir (see kbx_sandbox/onboard.py)."""
    if agent.name not in ("claude", "codex"):
        return
    status = docker.exec_passthrough(sandbox.name, ["kbx-onboard", agent.name, sandbox.workdir])
    if status != 0:
        print(f"⚠ could not skip {agent.name}'s first-run screens; on an older image, run `kbx build`", file=sys.stderr)


def login_status(docker: Docker, sandbox: Sandbox, names: Sequence[str] = ()) -> dict[str, tuple[bool | None, str]]:
    """Each agent's login from `kbx-login-status`: (logged in, or None if unknown; detail)."""
    result = docker.exec(sandbox.name, ["kbx-login-status", *names], check=False, timeout=90)
    if result.returncode != 0:
        raise KbxError("kbx-login-status failed (an image from before it? run `kbx build`)")
    data = json.loads(result.stdout)
    found: dict[str, tuple[bool | None, str]] = {}
    for name in AGENTS:
        item = data.get(name) if isinstance(data, dict) else None
        if isinstance(item, dict):
            ok = item.get("ok")
            found[name] = (ok if isinstance(ok, bool) else None, str(item.get("detail") or ""))
    return found


def login_hint(docker: Docker, sandbox: Sandbox, agent: Agent) -> None:
    """Say so before attaching when the agent is not logged in."""
    try:
        ok, _ = login_status(docker, sandbox, [agent.name]).get(agent.name, (None, ""))
    except (KbxError, ValueError):
        return
    if ok is False:
        how = {"claude": "type /login in Claude", "codex": 'choose "Sign in with Device Code" in Codex'}
        print(f"→ {agent.name} is not logged in: {how.get(agent.name, 'use /login')}. It persists in this sandbox.")


def prelaunch(docker: Docker, sandbox: Sandbox, agent: Agent, config: Config, resolved: list[Resolved]) -> None:
    reseed(docker, sandbox, agent)
    if config.launcher.skip_onboarding:
        skip_onboarding(docker, sandbox, agent)
    run_before_launch(docker, sandbox, agent, resolved)
    if agent.name == "codex" and config.launcher.remote_control:
        codex_remote_control(docker, sandbox)  # says how to log in if needed
    else:
        login_hint(docker, sandbox, agent)
