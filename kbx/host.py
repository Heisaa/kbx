"""`kbx host`: Claude Code on the host, locked down, as a deliberate exception.

For the rare task a sandbox cannot do. It is much weaker than a kbx sandbox:
the same kernel and user, no VM between the agent and your machine. So every
launch asks first, and the session gets as little as Claude Code can be
limited to (all of it written into one generated settings file):

- kbx runs its own copy of Claude Code, never on PATH (kbx/hostclaude.py),
  so there is no `claude` command that runs outside this by mistake. Its
  state (login, history) lives in a kbx directory (CLAUDE_CONFIG_DIR); `--restricted`
  ignores user, project and local settings, and no MCP servers load.
- Tools: Bash, Read, Edit, Write, Glob, Grep; no web tools. `--restricted`
  keeps the file tools inside the project and has a person approve changes
  to settings, git and tool configuration.
- Every tool use asks: permission mode `manual`, auto mode and bypass mode
  disabled, sandboxed commands not auto-approved.
- Commands run in Claude's OS sandbox (bubblewrap + seccomp on Linux), which
  must start or Claude exits: no reading $HOME except the project and
  [host] allow_read (toolchains), nor /run/user, /mnt or /media; writes only
  in the project and [host] allow_write; no git directory or guarded file
  writes; no network except [host] allowed_domains; no Unix sockets (so no
  Docker, D-Bus or agent sockets); `dangerouslyDisableSandbox` is ignored.
- A scrubbed environment: only basic variables and [host] env, so tokens in
  your shell's environment do not reach it.

What stays open: the Claude process itself is not sandboxed (it needs your
Claude login and the network to the API), and commands still read the rest
of the system outside $HOME. Approve each command knowing that.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from . import hostclaude
from . import sandbox as sandbox_mod
from .config import Config
from .errors import KbxError
from .paths import Paths

TOOLS = "Bash,Read,Edit,Write,Glob,Grep"
BASE_ENV = (
    "PATH",
    "HOME",
    "USER",
    "LOGNAME",
    "SHELL",
    "TERM",
    "COLORTERM",
    "TERM_PROGRAM",
    "LANG",
    "LC_ALL",
    "LC_CTYPE",
    "TZ",
)
# Project files a host tool would pick up and run, beyond the guard's list.
HOST_PROTECT = (".claude", ".mcp.json", ".envrc", ".vscode", ".idea")


def config_dir(paths: Paths) -> Path:
    return paths.data_dir / "host-claude"


def git_paths(project: Path) -> list[Path]:
    """The checkout's git directories, and the `.git` file of a linked worktree."""
    try:
        gitdir, common = sandbox_mod.git_dirs(project)
    except KbxError:
        return [project / ".git"]
    found = [gitdir, common]
    if (project / ".git").is_file():
        found.append(project / ".git")
    return list(dict.fromkeys(found))


def outermost(items: Sequence[Path]) -> list[Path]:
    """Without paths inside another listed path: the sandbox cannot mount one inside the other."""
    unique = list(dict.fromkeys(items))
    return [path for path in unique if not any(other in path.parents for other in unique)]


def settings(project: Path, home: Path, config: Config) -> dict[str, Any]:
    """The generated settings: everything the session may do, in one place."""
    no_write = outermost(
        [
            *git_paths(project),
            *(project / rel for rel in config.workspace.protect),
            *(project / rel for rel in HOST_PROTECT),
        ]
    )
    uid = os.getuid()
    return {
        "permissions": {
            "defaultMode": "manual",
            "disableAutoMode": "disable",
            "disableBypassPermissionsMode": "disable",
            # `//` makes a permission-rule path absolute.
            "deny": [f"Edit(/{path})" for path in no_write] + [f"Edit(/{path}/**)" for path in no_write],
        },
        "sandbox": {
            "enabled": True,
            "failIfUnavailable": True,
            "allowUnsandboxedCommands": False,
            "autoAllowBashIfSandboxed": False,
            "network": {"allowedDomains": list(config.host.allowed_domains)},
            "filesystem": {
                "denyRead": [str(home), f"/run/user/{uid}", "/mnt", "/media", "/run/media"],
                "allowRead": [str(project), *(str(path) for path in config.host.allow_read)],
                "allowWrite": [str(project), *(str(path) for path in config.host.allow_write)],
                "denyWrite": [str(path) for path in no_write],
            },
        },
    }


def environment(env: Mapping[str, str], config: Config, paths: Paths) -> dict[str, str]:
    names = (*BASE_ENV, *config.host.env)
    clean = {name: env[name] for name in names if name in env}
    clean["CLAUDE_CONFIG_DIR"] = str(config_dir(paths))
    clean.update(hostclaude.NO_SELF_UPDATE)
    return clean


def skip_onboarding(state_dir: Path, project: Path) -> None:
    """Theme picker and folder trust, as kbx-onboard does in a sandbox (the
    folder is trusted by launching this; everything else still asks)."""
    path = state_dir / ".claude.json"
    try:
        data: Any = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError):
        return  # leave a file we cannot read to Claude
    if not isinstance(data, dict) or not isinstance(data.setdefault("projects", {}), dict):
        return
    project_state = data["projects"].setdefault(str(project), {})
    if not isinstance(project_state, dict):
        return
    if data.get("hasCompletedOnboarding") is True and project_state.get("hasTrustDialogAccepted") is True:
        return
    data["hasCompletedOnboarding"] = True
    project_state["hasTrustDialogAccepted"] = True
    temp = path.with_name(path.name + ".kbx-tmp")
    temp.write_text(json.dumps(data, indent=2) + "\n")
    temp.chmod(0o600)
    os.replace(temp, path)


def preflight(env: Mapping[str, str], paths: Paths) -> None:
    """Refuses before asking when the session could not start as promised."""
    if not sys.platform.startswith("linux"):
        raise KbxError("kbx host needs Linux")
    missing = [tool for tool in ("bwrap", "socat") if shutil.which(tool, path=env.get("PATH")) is None]
    if missing:
        raise KbxError(
            f"Claude's command sandbox needs {' and '.join(missing)} on the host (packages bubblewrap and socat)"
        )
    if hostclaude.installed(paths) is None and shutil.which("gpg", path=env.get("PATH")) is None:
        raise KbxError("downloading Claude Code for kbx host verifies its signature with gpg (package gnupg)")
    bwrap = shutil.which("bwrap", path=env.get("PATH")) or "bwrap"
    probe = subprocess.run(
        [bwrap, "--ro-bind", "/", "/", "--dev", "/dev", "--proc", "/proc", "true"],
        capture_output=True,
        text=True,
        timeout=30,
        check=False,
    )
    if probe.returncode != 0:
        detail = (probe.stderr.strip().splitlines() or ["no output"])[-1]
        raise KbxError(
            f"bubblewrap cannot create a sandbox here ({detail}); unprivileged user namespaces may be "
            "disabled (on Ubuntu, AppArmor restricts them)"
        )


def summary(project: Path, config: Config, paths: Paths) -> list[str]:
    network = ", ".join(config.host.allowed_domains) or "none"
    extra_write = ", ".join(map(str, config.host.allow_write)) or "nothing else"
    return [
        "⚠ kbx host runs Claude Code on this machine, outside any VM: same kernel, same user.",
        f"  project:  {project}",
        f"  claude:   {claude_note(paths, config)}",
        "  asks:     before every tool use (auto and bypass modes are off)",
        f"  commands: sandboxed; write only the project and {extra_write}; no git dir or hook files;",
        f"            no reading your home outside the project and [host] allow_read; network: {network}",
        "  not sandboxed: Claude itself, and your answers to its questions.",
    ]


def claude_note(paths: Paths, config: Config) -> str:
    have = hostclaude.installed(paths)
    if have is None:
        return f"kbx's own copy, downloaded now ({config.host.channel} channel, ~230 MB, signature checked)"
    update = " (checked for updates)" if config.launcher.auto_update else ""
    return f"kbx's own copy, {have[0]}{update}; not on your PATH"


def command(claude: str, settings_path: Path, args: Sequence[str]) -> list[str]:
    return [
        claude,
        "--restricted",
        "--tools",
        TOOLS,
        "--strict-mcp-config",
        "--settings",
        str(settings_path),
        "--permission-mode",
        "manual",
        *args,
    ]


def claude_args(*, resume: str | None, continue_: bool, model: str | None, prompt: str | None) -> list[str]:
    """The only Claude options `kbx host` passes on: none of them loosen the settings."""
    args: list[str] = []
    if continue_:
        args.append("--continue")
    if resume is not None:
        args += ["--resume", resume] if resume else ["--resume"]
    if model:
        args += ["--model", model]
    if prompt:
        args += ["--", prompt]
    return args


def prepare(
    paths: Paths, config: Config, project: Path, env: Mapping[str, str], args: Sequence[str]
) -> tuple[list[str], dict[str, str]]:
    """Everything up to the exec, once the user said yes: kbx's Claude, state, settings."""
    claude = hostclaude.ensure(paths, config.host.channel, update=config.launcher.auto_update)
    state = config_dir(paths)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    skip_onboarding(state, project)
    paths.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    # One file per project, rewritten at each launch (the exec leaves no chance to clean up).
    settings_path = paths.runtime_dir / f"host-settings-{sandbox_mod.for_project(project).name}.json"
    temp = settings_path.with_name(settings_path.name + ".tmp")
    temp.write_text(json.dumps(settings(project, paths.home, config), indent=2) + "\n")
    temp.chmod(0o600)
    os.replace(temp, settings_path)
    return command(str(claude), settings_path, args), environment(env, config, paths)
