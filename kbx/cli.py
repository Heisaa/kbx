"""Command-line entry point: argparse subcommands, dispatch and exit codes."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path

from . import (
    __version__,
    agents,
    clipd,
    dash,
    exposed,
    git,
    guard,
    host,
    image,
    private,
    review,
    sandbox,
    session,
    stage,
    voice,
    watch,
)
from . import config as config_mod
from . import modules as modules_mod
from . import paths as paths_mod
from .config import Config
from .docker import ROOT, Docker
from .errors import KbxError
from .modules import Module, Resolved
from .paths import Paths
from .sandbox import Sandbox

ENTRY = paths_mod.CHECKOUT / "bin" / "kbx"

USAGE = """\
kbx [dash]                          dashboard over all sandboxes (on a terminal)
kbx claude|codex|pi [agent args…]   create/start the sandbox, update, attach
kbx attach [claude|codex|pi|SESSION]   reattach to a running session
kbx shell                           bash in the sandbox (as agent)
kbx start                           create/start the sandbox (and seed the clone), no attach
kbx resume [--accept]               after the guard paused the sandbox: review, resume
kbx diff [--stat] [--since-start|--since REV] [path…]   the agent's changes, from the sandbox's git
kbx fetch [branch…]                 clone mode: sandbox branches → host refs/remotes/kbx/*
kbx sync                            clone mode: host branches → sandbox refs/remotes/host/*
kbx update                          update all agents without launching
kbx rc-start                        start Codex remote control without the TUI
kbx rc-pair                         start Codex remote control and print a pairing code
kbx logs                            startup, dockerd and module logs
kbx stop | recreate | rm            lifecycle (rm asks before deleting volumes)
kbx build                           generate the Dockerfile and build the image
kbx check                           validate modules and config
kbx seed [--dry-run|--status|--reset M]   manage home defaults
kbx ls                              list sandboxes and their sessions
kbx host [-c|-r [ID]] [--model M] [prompt]   Claude on the host, locked down (asks first)

Under a root of a [profiles.<name>] in config.toml, commands use that profile's
sandbox (sessions per project); KBX_PROFILE=<name>|none overrides.
"""


@dataclass
class Context:
    env: Mapping[str, str]
    paths: Paths
    docker: Docker
    config: Config = field(default_factory=Config)
    modules: dict[str, Module] = field(default_factory=dict[str, Module])
    resolved: list[Resolved] = field(default_factory=list[Resolved])

    @classmethod
    def load(cls, env: Mapping[str, str]) -> Context:
        paths = paths_mod.resolve(env)
        cfg = config_mod.load(paths, env)
        found = modules_mod.discover(paths)
        return cls(env, paths, Docker(), cfg, found, modules_mod.resolve(found, cfg))

    def module_env(self) -> dict[str, str]:
        merged: dict[str, str] = {}
        for item in self.resolved:
            merged.update(item.module.env)
        return merged


def warn(message: str) -> None:
    print(f"⚠ {message}", file=sys.stderr)


def current_sandbox(ctx: Context, cwd: Path | None = None) -> Sandbox:
    """The sandbox this directory works in: its profile's when a profile's roots
    contain it (or KBX_PROFILE names one), else the project's own, in the mode
    its container was created with. A profile's settings replace ctx.config."""
    here = cwd or Path.cwd()
    profile = config_mod.profile_for(ctx.config, here, ctx.env)
    if profile is not None:
        if ctx.env.get("KBX_WORKSPACE", "").strip() == "clone":
            raise KbxError(f"profile {profile.name} shares its roots (mount mode); KBX_WORKSPACE=clone does not apply")
        ctx.config = ctx.config.for_profile(profile.name)
        return sandbox.for_profile(profile.name, profile.roots, git.profile_project(here, profile.roots))
    sb = sandbox.for_project(git.project_root(here), ctx.config.workspace.mode)
    existing = sandbox.workspace_mode(ctx.docker, sb)
    return sb if existing in (None, sb.mode) else replace(sb, mode=existing)


def _check_profile(ctx: Context, sb: Sandbox, exists: bool) -> None:
    """Profile: the roots exist, the container mounts what the config lists, and
    the project is reachable inside it."""
    missing = [root for root in sb.roots if not root.is_dir()]
    if missing:
        raise KbxError(
            f"{missing[0]} does not exist; create it or remove it from [profiles.{sb.profile}] roots in config.toml"
        )
    if exists:
        mounted = sandbox.mounted_roots(sandbox.labels(ctx.docker, sb.name))
        if set(mounted) != set(sb.roots):
            if sandbox.root_of(replace(sb, profile_roots=mounted), sb.project_dir) is None:
                raise KbxError(
                    f"{sb.project_dir} is in profile {sb.profile}'s roots, but {sb.name} was created before; "
                    "`kbx recreate` mounts the roots from the config"
                )
            warn(f"profile {sb.profile}'s roots changed since {sb.name} was created; `kbx recreate` mounts them")
    unreachable = sandbox.unreachable_gitdir(sb)
    if unreachable is not None:
        warn(
            f"this worktree's repository {unreachable} is outside profile {sb.profile}'s roots, "
            "so git does not work in it inside the sandbox; add its directory to the roots"
        )
    if (sb.project_dir / ".git").exists():
        alone = sandbox.for_project(sb.project_dir, "mount")
        if ctx.docker.inspect("container", alone.name) is not None:
            warn(
                f"{alone.name}, a sandbox for this project alone, still exists; this project now uses "
                f"profile {sb.profile}. `KBX_PROFILE=none kbx rm` here removes the old one"
            )


def guarded(ctx: Context, sb: Sandbox) -> bool:
    return sb.mode == "mount" and ctx.config.workspace.guard


def mounted(ctx: Context, sb: Sandbox) -> Sandbox:
    """The sandbox with the roots its container mounts, which the guard must
    watch (the config may list others until `kbx recreate`)."""
    if not sb.profile:
        return sb
    roots = sandbox.mounted_roots(sandbox.labels(ctx.docker, sb.name))
    return replace(sb, profile_roots=roots) if roots else sb


def prepare(ctx: Context, *, seed_clone: bool = True) -> Sandbox:
    """Launch sequence steps 1-4: config, stage, create, start, firewall, then
    the guard (mount mode) or the clone (clone mode)."""
    sb = current_sandbox(ctx)
    if sb.profile:
        _check_profile(ctx, sb, sandbox.state(ctx.docker, sb) is not None)
    if sb.mode != ctx.config.workspace.mode:
        warn(
            f"{sb.name} was created in {sb.mode} mode, but the config says {ctx.config.workspace.mode}; "
            "`kbx recreate` switches (volumes are kept)"
        )
    for message in image.drift(ctx.docker, ctx.paths, ctx.config, ctx.resolved):
        warn(message)
    for message in stage.build(ctx.paths, ctx.config, ctx.resolved):
        warn(message)
    if sb.mode == "mount":
        sandbox.check_mountable(sb)
        shared = sandbox.shared_gitdir(sb)
        exists = sandbox.state(ctx.docker, sb) is not None
        if exists and sandbox.mounted_gitdir(ctx.docker, sb) != (str(shared) if shared else None):
            warn("this worktree's repository directory changed since the sandbox was created; `kbx recreate` mounts it")
        if not exists:
            _confirm_exposed(sb, "Create the sandbox anyway?")
    sandbox.ensure_created(ctx.docker, sb, ctx.config, ctx.paths)
    starting = sandbox.state(ctx.docker, sb) not in ("running", "paused")
    if guarded(ctx, sb) and starting:
        guard.before_start(ctx.paths, mounted(ctx, sb), ctx.config.workspace.protect)
    sandbox.ensure_running(ctx.docker, sb, config=ctx.config, paths=ctx.paths)
    if guarded(ctx, sb):  # before anything below can fail and leave it unwatched
        guard.ensure(ctx.paths, mounted(ctx, sb), ctx.docker, ENTRY, ctx.config.workspace.protect)
    elif sb.mode == "mount":
        warn("the kbx guard is off ([workspace] guard = false): the agent can plant git hooks the host runs")
    sandbox.firewall_check(ctx.docker, sb, ctx.config, dict(ctx.env))
    watch.ensure(ctx.paths, sb, ctx.config, ENTRY)
    if sb.mode == "mount":
        for message in private.mount(ctx.docker, sb, ctx.config.workspace.private):
            warn(message)
        if starting:
            git.write_mount_note(ctx.docker, sb)
    elif seed_clone and not git.is_seeded(ctx.docker, sb):
        git.seed(ctx.docker, sb)
    if starting:
        review.forget_start(ctx.paths, sb)  # a profile's other projects too: they record at their next launch
    if starting or (sb.profile and not review.has_start(ctx.paths, sb)):
        review.record_start(ctx.docker, ctx.paths, sb)
    return sb


def _confirm_exposed(sb: Sandbox, question: str, roots: Sequence[Path] = ()) -> None:
    """Mount mode, before the checkout (or a profile's roots) is first shared: list
    the secret-looking files the agent will see and, in a terminal, ask before
    going on. `roots`: a profile's roots to check (default: all of them)."""
    if sb.profile:
        message = exposed.roots_warning(roots or sb.roots)
    else:
        message = exposed.warning(sb.project_dir)
    if not message:
        return
    warn(message)
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        return
    if not _ask(question, True):
        if sb.profile:
            raise KbxError(
                f"nothing changed; move the secrets out of the roots of profile {sb.profile}, or keep "
                "those projects out of the profile, then run kbx again"
            )
        raise KbxError(
            "nothing changed; move the secrets out of the checkout "
            'or set [workspace] mode = "clone", then run kbx again'
        )


def existing_running(ctx: Context) -> Sandbox:
    sb = current_sandbox(ctx)
    status = sandbox.state(ctx.docker, sb)
    if status is None:
        raise KbxError("no sandbox for this project yet; start one with `kbx claude|codex|pi`")
    if status == "paused":
        raise KbxError(sandbox.paused_message(sb))
    if status != "running":
        raise KbxError(f"sandbox {sb.name} is {status}; start it with `kbx claude|codex|pi` or `kbx shell`")
    return sb


def _clipboard(ctx: Context, sb: Sandbox) -> None:
    if not agents.enabled(ctx.resolved, "clipboard"):
        return
    if clipd.backend(ctx.env) is None:
        marker = ctx.paths.runtime_dir / "clipboard-warned"
        if not marker.exists():
            warn(f"clipboard sync is off: {clipd.missing_tools_hint(ctx.env)} on the host")
            try:
                ctx.paths.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                marker.touch()
            except OSError:
                pass
        return
    try:
        clipd.register(ctx.paths, sb.name, os.getpid(), ENTRY)
    except OSError as exc:
        warn(f"clipboard sync is off: cannot start the clipboard watcher ({exc})")


def _voice(ctx: Context, sb: Sandbox) -> None:
    if not agents.enabled(ctx.resolved, "voice"):
        return
    if voice.tools() is None:
        marker = ctx.paths.runtime_dir / "voice-warned"
        if not marker.exists():
            warn(f"voice input is off: {voice.MISSING_TOOLS} on the host")
            try:
                ctx.paths.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                marker.touch()
            except OSError:
                pass
        return
    try:
        voice.register(ctx.paths, sb.name, os.getpid(), ENTRY)
    except OSError as exc:
        warn(f"voice input is off: cannot start the voice link ({exc})")


def _attach(ctx: Context, sb: Sandbox, key: str, inner: Sequence[str]) -> None:
    """Hand the terminal to session `key` (`claude`, or `claude@<project>` in a profile)."""
    agent = sandbox.session_agent(key)
    _clipboard(ctx, sb)
    _voice(ctx, sb)
    if guarded(ctx, sb):
        guard.register_tty(ctx.paths, sb.name)
    host_env = dict(os.environ)
    host_env["HERDR_AGENT"] = agent  # Herdr sees `docker`, not the agent
    detach = ctx.config.launcher.detach_key
    if detach.lower() not in ("", "none"):
        print(f"  Detach with {detach.replace('^', 'Ctrl-')}; reattach with `kbx attach {agent}`.")
    session.exec_attach(
        ctx.docker,
        sb,
        inner,
        workdir=sb.workdir,
        # Marks the session kbx started; kbx-notify reports only these.
        env=session.session_env({**ctx.module_env(), "KBX_SESSION": key}),
        host_env=host_env,
    )


def _where(sb: Sandbox) -> str:
    return f"{sb.name}, in {sb.project_dir}" if sb.profile else sb.name


def cmd_agent(ctx: Context, name: str, args: Sequence[str]) -> int:
    agent = agents.get(name)
    sb = prepare(ctx)
    detach = ctx.config.launcher.detach_key
    key = sb.session_key(agent.name)
    if session.alive(ctx.docker, sb, key):
        # Never update or touch remote control under a live session.
        print(f"→ Reattaching to the running {agent.name} session in {_where(sb)}")
        if args:
            warn("agent arguments are ignored when reattaching to a running session")
        _attach(ctx, sb, key, session.dtach_attach(key, detach))
        return 0
    if ctx.config.launcher.auto_update:
        others = _agent_sessions(ctx, sb, agent.name)
        if others:
            # The binary is shared by every project of a profile; it updates when none runs.
            print(f"→ Not updating {agent.name}: it runs in {', '.join(map(sandbox.session_label, others))}")
        else:
            agents.update(ctx.docker, sb, agent, strict=False)
    agents.prelaunch(ctx.docker, sb, agent, ctx.config, ctx.resolved)
    argv = agents.command(agent, args, ctx.config, ctx.resolved)
    print(f"→ Starting {agent.name} in {_where(sb)}")
    _attach(ctx, sb, key, session.dtach_start(key, detach, argv))
    return 0


def _agent_sessions(ctx: Context, sb: Sandbox, agent: str) -> list[str]:
    """Live sessions of `agent` in this sandbox (any project of a profile)."""
    return [key for key in sandbox.sessions(ctx.docker, sb.name) if sandbox.session_agent(key) == agent]


def attach_target(sb: Sandbox, live: Sequence[str], name: str | None) -> str:
    """The session `kbx attach [name]` means. `name`: an agent (this project's
    session of it), a full session name from `kbx ls`, or None (this project's
    only session)."""
    if name is not None and "@" in name:
        if not sb.profile or not sandbox.valid_session(name):
            raise KbxError(f"{name!r} is not a session of this sandbox")
        if name not in live:
            raise KbxError(f"no running session {name}; `kbx ls` lists them")
        return name
    others = [key for key in live if not sb.owns_session(key)]
    elsewhere = f"; in other projects of profile {sb.profile}: {', '.join(others)}" if others else ""
    if name is None:
        mine = [key for key in live if sb.owns_session(key)]
        if len(mine) == 1:
            return mine[0]
        if mine:
            names = ", ".join(sandbox.session_agent(key) for key in mine)
            raise KbxError(f"several sessions are running ({names}); name one")
        raise KbxError(f"no running sessions{' in this project' if sb.profile else ''}{elsewhere}")
    agent = agents.get(name)
    key = sb.session_key(agent.name)
    if key not in live:
        raise KbxError(f"no running {agent.name} session; start one with `kbx {agent.name}`{elsewhere}")
    return key


def cmd_attach(ctx: Context, args: argparse.Namespace) -> int:
    sb = existing_running(ctx)
    key = attach_target(sb, sandbox.sessions(ctx.docker, sb.name), args.agent)
    sandbox.firewall_check(ctx.docker, sb, ctx.config, dict(ctx.env))
    if guarded(ctx, sb):
        guard.ensure(ctx.paths, mounted(ctx, sb), ctx.docker, ENTRY, ctx.config.workspace.protect)
    watch.ensure(ctx.paths, sb, ctx.config, ENTRY)
    _attach(ctx, sb, key, session.dtach_attach(key, ctx.config.launcher.detach_key))
    return 0


def cmd_shell(ctx: Context, args: argparse.Namespace) -> int:
    sb = prepare(ctx)
    _clipboard(ctx, sb)
    _voice(ctx, sb)
    if guarded(ctx, sb):
        guard.register_tty(ctx.paths, sb.name)
    session.exec_attach(
        ctx.docker,
        sb,
        ["bash", "-l"],
        workdir=sb.workdir,
        env=session.session_env(ctx.module_env()),
        host_env=dict(os.environ),
    )
    return 0


def cmd_start(ctx: Context, args: argparse.Namespace) -> int:
    sb = prepare(ctx)
    where = f"working in {sb.workdir}" if sb.mode == "mount" else f"clone at {sb.workdir}"
    if sb.profile:
        where = f"profile {sb.profile} ({', '.join(map(str, sb.roots))}); {where}"
    print(f"✓ {sb.name} is running; {where}")
    return 0


def cmd_resume(ctx: Context, args: argparse.Namespace) -> int:
    sb = current_sandbox(ctx)
    status = sandbox.state(ctx.docker, sb)
    lines = guard.report(ctx.paths, sb)
    if not lines and status != "paused":
        print("Nothing to resume: the guard has not stopped anything.")
        return 0
    for line in lines:
        print(line)
    if lines:
        accept = args.accept or _ask("Were these changes yours? Restore them", False)
        for note in guard.resolve(ctx.paths, mounted(ctx, sb), ctx.config.workspace.protect, accept=accept):
            print(f"  {note}")
        if not accept:
            print("Kept the neutralized versions; the agent's stay at the quarantine paths above.")
    if status == "paused":
        ctx.docker.run(["unpause", sb.name])
    if status in ("running", "paused") and guarded(ctx, sb):
        guard.ensure(ctx.paths, mounted(ctx, sb), ctx.docker, ENTRY, ctx.config.workspace.protect)
    print(f"✓ {sb.name} " + ("resumed" if status == "paused" else "can start again"))
    return 0


def _pathspecs(sb: Sandbox, paths: Sequence[str], cwd: Path) -> list[str]:
    """Host paths as pathspecs from the repository root, where git runs in the sandbox."""
    result: list[str] = []
    for item in paths:
        target = Path(os.path.normpath(cwd / item))
        if target != sb.project_dir and sb.project_dir not in target.parents:
            raise KbxError(f"{item} is outside {sb.project_dir}")
        result.append(
            ":(top,literal)" + (target.relative_to(sb.project_dir).as_posix() if target != sb.project_dir else ".")
        )
    return result


def cmd_diff(ctx: Context, args: argparse.Namespace) -> int:
    sb = existing_running(ctx)
    if sb.mode == "clone" and not git.is_seeded(ctx.docker, sb):
        raise KbxError(f"the sandbox clone {sb.workdir} does not exist yet")
    if args.since_start and args.since:
        raise KbxError("use --since-start or --since, not both")
    base = review.start_point(ctx.paths, sb) if args.since_start else (args.since or "")
    if base and not review.revision_ok(base):
        raise KbxError(f"not a revision: {base!r}")
    if sb.mode == "clone":
        print(f"(the sandbox's clone at {sb.workdir}; `kbx fetch` brings its commits to the host)", file=sys.stderr)
    specs = _pathspecs(sb, args.paths, Path.cwd())
    return review.run(ctx.docker, sb, base=base, stat=args.stat, pathspecs=specs, env=ctx.env)


def cmd_fetch(ctx: Context, args: argparse.Namespace) -> int:
    sb = existing_or_start(ctx)
    git.fetch(ctx.docker, sb, list(args.branches))
    return 0


def cmd_sync(ctx: Context, args: argparse.Namespace) -> int:
    sb = existing_or_start(ctx)
    git.sync(ctx.docker, sb)
    return 0


def existing_or_start(ctx: Context) -> Sandbox:
    """Start an existing sandbox if needed (git commands never create one)."""
    sb = current_sandbox(ctx)
    if sb.profile:
        raise KbxError(f"not needed in profile {sb.profile}: its projects are shared, so commits show up on both sides")
    status = sandbox.state(ctx.docker, sb)
    if status is None:
        raise KbxError("no sandbox for this project yet; start one with `kbx claude|codex|pi`")
    if sb.mode == "mount":
        # A clone left over from clone mode stays reachable while the sandbox runs.
        if status != "running" or not git.is_seeded(ctx.docker, sb):
            raise KbxError(
                "not needed in mount mode: the sandbox works in your checkout, so commits show up on both sides"
            )
        return sb
    stage.build(ctx.paths, ctx.config, ctx.resolved)
    sandbox.ensure_running(ctx.docker, sb, config=ctx.config, paths=ctx.paths)
    if not git.is_seeded(ctx.docker, sb):
        raise KbxError(f"the sandbox clone {sb.workdir} does not exist yet")
    return sb


def cmd_update(ctx: Context, args: argparse.Namespace) -> int:
    sb = prepare(ctx, seed_clone=False)
    for agent in agents.AGENTS.values():
        if _agent_sessions(ctx, sb, agent.name):
            print(f"→ Skipping {agent.name}: a session of it is running (it updates at its next fresh start)")
            continue
        agents.update(ctx.docker, sb, agent, strict=True)
    return 0


def cmd_rc_start(ctx: Context, args: argparse.Namespace) -> int:
    sb = prepare(ctx)
    agents.codex_remote_control(ctx.docker, sb)
    return 0


def cmd_rc_pair(ctx: Context, args: argparse.Namespace) -> int:
    sb = prepare(ctx)
    if not agents.codex_remote_control(ctx.docker, sb):
        return 1
    return ctx.docker.exec_passthrough(sb.name, ["codex", "remote-control", "pair"], timeout=60)


LOGS_SCRIPT = r"""
for f in /var/log/kbx-startup.log /var/log/dockerd.log /var/log/kbx/*.log; do
  [ -f "$f" ] || continue
  printf '\n==> %s <==\n' "$f"
  tail -n "$1" "$f"
done
"""


def cmd_logs(ctx: Context, args: argparse.Namespace) -> int:
    sb = current_sandbox(ctx)
    status = sandbox.state(ctx.docker, sb)
    if status is None:
        raise KbxError("no sandbox for this project yet")
    print(f"sandbox {sb.name}: {status}")
    if status != "running":
        ctx.docker.run(["logs", "--tail", str(args.lines), sb.name], stdout=None, stderr=None, check=False)
        print("\n(start it with `kbx shell` to see the logs inside)")
        return 0
    ready = sandbox.ready_status(ctx.docker, sb)
    if ready:
        print(json.dumps({k: ready.get(k) for k in ("seed", "start", "services")}, indent=2))
    ctx.docker.exec_passthrough(sb.name, ["sh", "-c", LOGS_SCRIPT, "sh", str(args.lines)], user=ROOT)
    print(
        "\nAgent logs inside the sandbox:\n"
        "  ~/.claude/debug/*.txt      (KBX_DEBUG=true kbx claude → claude --debug)\n"
        "  ~/.codex/log/              and `codex app-server daemon status`\n"
        f"Clipboard watcher (host): {ctx.paths.log_dir}/clipd-{sb.name}.log\n"
        f"Voice link (host, voice module): {ctx.paths.log_dir}/voiced-{sb.name}.log\n"
        f"Guard (host, mount mode): {ctx.paths.log_dir}/guard-{sb.name}.log\n"
        "Host: journalctl -t kata (containerd shim), and firewall drop counters:\n"
        "  sudo iptables -vnL KBX-INPUT; sudo iptables -vnL KBX-FORWARD"
    )
    return 0


def _stop(ctx: Context, sb: Sandbox) -> None:
    sandbox.stop(ctx.docker, sb)
    if guarded(ctx, sb):
        actions = guard.seal(ctx.paths, sb.name)
        if actions:
            warn(f"the kbx guard neutralized {guard.summary(actions)}; `kbx resume` shows the details")


def cmd_stop(ctx: Context, args: argparse.Namespace) -> int:
    sb = current_sandbox(ctx)
    if sandbox.state(ctx.docker, sb) is None:
        raise KbxError("no sandbox for this project")
    _stop(ctx, sb)
    print(f"✓ Stopped {sb.name}")
    return 0


def cmd_recreate(ctx: Context, args: argparse.Namespace) -> int:
    sb = current_sandbox(ctx)
    if sandbox.state(ctx.docker, sb) is None:
        raise KbxError("no sandbox for this project")
    stage.build(ctx.paths, ctx.config, ctx.resolved)
    if ctx.docker.inspect("image", ctx.config.image.name) is None:
        raise KbxError(f"image {ctx.config.image.name!r} not found; run `kbx build` first")
    new = replace(sb, mode=ctx.config.workspace.mode)
    if new.mode == "mount":
        sandbox.check_mountable(new)
        if sb.mode != "mount":
            _confirm_exposed(new, "Recreate it in mount mode anyway?")
    if sb.profile:
        _check_profile(ctx, sb, False)
        added = [root for root in sb.roots if root not in sandbox.mounted_roots(sandbox.labels(ctx.docker, sb.name))]
        if added:
            _confirm_exposed(sb, "Recreate it with these roots anyway?", added)
    _stop(ctx, sb)
    sandbox.remove_container(ctx.docker, sb)
    sandbox.ensure_created(ctx.docker, new, ctx.config, ctx.paths)
    print(f"✓ Recreated {sb.name} from {ctx.config.image.name} in {new.mode} mode; volumes kept.")
    if sb.mode == "clone" and new.mode == "mount":
        print(f"  The old clone stays at {sb.clone_dir}; `kbx fetch` still reaches it while the sandbox runs.")
    print("  It starts at the next launch.")
    return 0


def _ask(prompt: str, default: bool) -> bool:
    if not sys.stdin.isatty():
        return False
    suffix = " [Y/n] " if default else " [y/N] "
    try:
        answer = input(prompt + suffix).strip().lower()
    except EOFError:
        return False
    return default if not answer else answer in ("y", "yes")


def cmd_rm(ctx: Context, args: argparse.Namespace) -> int:
    sb = current_sandbox(ctx)
    status = sandbox.state(ctx.docker, sb)
    exists = status is not None
    volumes = [v for v in (sb.home_volume, sb.docker_volume) if ctx.docker.inspect("volume", v)]
    if not exists and not volumes:
        print(f"Nothing to remove for {f'profile {sb.profile}' if sb.profile else sb.project_dir}.")
        return 0
    inspected = False
    if sb.profile:
        print(f"Profile {sb.profile}'s directories are not touched: {', '.join(map(str, sb.roots))}")
        print("  Agent logins and history in the sandbox's home volume are deleted with it.")
        inspected = True
    elif exists and sb.mode == "mount" and (status != "running" or not git.is_seeded(ctx.docker, sb)):
        # The work is in the checkout; only an old clone from clone mode could hold more.
        print(f"Your checkout {sb.project_dir} is not touched.")
        inspected = True
    elif exists:
        try:
            stage.build(ctx.paths, ctx.config, ctx.resolved)
            sandbox.ensure_running(ctx.docker, sb, timeout=180, config=ctx.config, paths=ctx.paths)
            if git.is_seeded(ctx.docker, sb):
                pending = git.unfetched(ctx.docker, sb)
                changes = git.local_changes(ctx.docker, sb)
                inspected = True
                if pending:
                    print("Branches with commits that exist only in the sandbox:")
                    for item in pending:
                        print(f"  {item.branch}  ({item.commits} commit(s))")
                    if _ask("Fetch them to the host now (kbx fetch)?", True):
                        git.fetch(ctx.docker, sb, [])
                        pending = git.unfetched(ctx.docker, sb)
                for note in changes:
                    warn(f"sandbox clone has {note}; these are lost on removal")
                if pending:
                    warn("unfetched commits will be lost")
            else:
                inspected = True
        except KbxError as exc:
            warn(f"could not inspect the sandbox clone ({exc})")
    if not inspected:
        warn("the sandbox clone could not be checked for unfetched work; anything in it will be lost")
    if not args.yes and not _ask(f"Delete {sb.name} and its volumes ({', '.join(volumes) or 'none'})?", False):
        print("Nothing deleted.")
        return 1
    sandbox.remove_container(ctx.docker, sb)
    sandbox.remove_volumes(ctx.docker, sb)
    guard.remove(ctx.paths, sb.name)
    review.forget_start(ctx.paths, sb)
    print(f"✓ Removed {sb.name}")
    return 0


def cmd_build(ctx: Context, args: argparse.Namespace) -> int:
    if args.print:
        print(image.render(ctx.paths.checkout, ctx.resolved))
        return 0
    image.build(
        ctx.docker,
        ctx.paths,
        ctx.config,
        ctx.resolved,
        no_cache=args.no_cache,
        pull=args.pull,
        refresh_agents=args.refresh_agents,
    )
    return 0


def cmd_check(env: Mapping[str, str]) -> int:
    paths = paths_mod.resolve(env)
    errors = 0
    try:
        cfg = config_mod.load(paths, env)
        print(f"✓ config: {paths.config_file}{'' if paths.config_file.exists() else ' (absent; defaults)'}")
        found = modules_mod.discover(paths)
        resolved = modules_mod.resolve(found, cfg)
    except KbxError as exc:
        print(f"✗ {exc}")
        return 1
    enabled = {item.name for item in resolved}
    for name, module in found.items():
        parts = [p for p, present in zip(
            modules_mod.PARTS, (module.has_build, module.has_home, module.has_service, module.has_start), strict=True
        ) if present]  # fmt: skip
        mark = "on " if name in enabled else "off"
        print(f"  [{mark}] {name:<22} {module.source:<7} {', '.join(parts) or '-':<30} {module.description}")
    for issue in modules_mod.check(found, resolved):
        prefix = "✗" if issue.level == "error" else "⚠"
        errors += issue.level == "error"
        print(f"{prefix} {issue.message}")
    for profile in cfg.profiles.values():
        print(f"  profile {profile.name}: {', '.join(map(str, profile.roots))}")
        for root in profile.roots:
            if not root.is_dir():
                print(f"⚠ profile {profile.name}: {root} does not exist")
    if not any(source.is_dir() for source in cfg.skills.sources) and cfg.launcher.shared_skills:
        print(f"  skills: no source directory found ({', '.join(map(str, cfg.skills.sources))})")
    print("✓ no problems found" if not errors else f"✗ {errors} error(s)")
    return 1 if errors else 0


def cmd_seed(ctx: Context, args: argparse.Namespace) -> int:
    sb = prepare(ctx, seed_clone=False)
    if args.reset:
        if args.reset not in {item.name for item in ctx.resolved}:
            raise KbxError(f"module {args.reset!r} is not enabled")
        if not args.yes and not _ask(
            f"Restore every default of {args.reset}, overwriting your changes to those keys?", False
        ):
            print("Nothing changed.")
            return 1
        argv = ["kbx-seed", "--reset", args.reset]
    elif args.dry_run:
        argv = ["kbx-seed", "--dry-run"]
    elif args.status:
        argv = ["kbx-seed", "--status"]
    else:
        argv = ["kbx-seed"]
    return ctx.docker.exec_passthrough(sb.name, argv)


def cmd_dash(ctx: Context, args: argparse.Namespace) -> int:
    return dash.run(ctx.docker, ctx.paths, ctx.config, ctx.resolved, entry=ENTRY)


def cmd_host(ctx: Context, args: argparse.Namespace) -> int:
    if not (sys.stdin.isatty() and sys.stdout.isatty()):
        raise KbxError("kbx host needs a terminal: it asks before it starts, and Claude asks before every step")
    project = git.project_root(Path.cwd())
    extra = host.claude_args(resume=args.resume, continue_=args.continue_, model=args.model, prompt=args.prompt)
    host.preflight(ctx.env, ctx.paths)
    for line in host.summary(project, ctx.config, ctx.paths):
        print(line)
    profile = config_mod.profile_for(ctx.config, project, ctx.env)
    if profile is not None:
        sb = sandbox.for_profile(profile.name, profile.roots, project)
    else:
        sb = sandbox.for_project(project, ctx.config.workspace.mode)
    if sandbox.state(ctx.docker, sb) == "running":
        print(f"  note: {sb.name} is running for this project too; both can edit the checkout.")
    if not _ask("Start Claude on the host?", False):
        print("Not started.")
        return 1
    argv, env = host.prepare(ctx.paths, ctx.config, project, ctx.env, extra)
    os.chdir(project)
    sys.stdout.flush()
    os.execvpe(argv[0], argv, env)


def cmd_ls(ctx: Context, args: argparse.Namespace) -> int:
    rows = sandbox.list_sandboxes(ctx.docker)
    if not rows:
        print("No kbx sandboxes.")
        return 0
    for row in rows:
        live = sandbox.sessions(ctx.docker, row["name"]) if row["state"] == "running" else []
        where = row["project"]
        if row["profile"]:
            roots = sandbox.mounted_roots(sandbox.labels(ctx.docker, row["name"]))
            where = f"profile {row['profile']}: {', '.join(map(str, roots))}"
        print(f"{row['name']:<48} {row['state']:<10} {','.join(live) or '-':<18} {where}")
    return 0


def parser() -> argparse.ArgumentParser:
    top = argparse.ArgumentParser(
        prog="kbx",
        description="Agent sandboxes on Kata Containers.",
        usage=USAGE,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    top.add_argument("--version", action="version", version=f"kbx {__version__}")
    sub = top.add_subparsers(dest="command", metavar="COMMAND")
    attach = sub.add_parser("attach", help="reattach to a running session")
    attach.add_argument("agent", nargs="?", help="claude, codex or pi; or a session name from `kbx ls`")
    sub.add_parser("shell", help="bash in the sandbox")
    sub.add_parser("start", help="create/start the sandbox without attaching")
    resume = sub.add_parser("resume", help="review what the guard stopped and resume the sandbox")
    resume.add_argument("--accept", action="store_true", help="take the agent's quarantined versions back")
    diff = sub.add_parser("diff", help="the agent's changes (uncommitted, untracked), from the sandbox's git")
    diff.add_argument("--stat", action="store_true", help="a summary per file")
    diff.add_argument("--since-start", action="store_true", help="everything since the sandbox started, commits too")
    diff.add_argument("--since", metavar="REV", help="everything since REV, commits too")
    diff.add_argument("paths", nargs="*", help="limit to these paths")
    fetch = sub.add_parser("fetch", help="sandbox branches → host refs/remotes/kbx/*")
    fetch.add_argument("branches", nargs="*")
    sub.add_parser("sync", help="host branches → sandbox refs/remotes/host/*")
    sub.add_parser("update", help="update all agents")
    sub.add_parser("rc-start", help="start Codex remote control")
    sub.add_parser("rc-pair", help="start Codex remote control and print a pairing code")
    logs = sub.add_parser("logs", help="show logs")
    logs.add_argument("-n", "--lines", type=int, default=40)
    sub.add_parser("stop", help="stop the sandbox")
    sub.add_parser("recreate", help="recreate the container, keeping volumes")
    rm = sub.add_parser("rm", help="remove the sandbox and its volumes")
    rm.add_argument("-y", "--yes", action="store_true", help="do not ask for confirmation")
    build = sub.add_parser("build", help="build the image")
    build.add_argument("--no-cache", action="store_true")
    build.add_argument("--pull", action="store_true", help="pull a newer debian base image")
    build.add_argument("--refresh-agents", action="store_true", help="reinstall the agents in the image")
    build.add_argument("--print", action="store_true", help="print the generated Dockerfile")
    sub.add_parser("check", help="validate modules and config")
    seed = sub.add_parser("seed", help="manage home defaults")
    group = seed.add_mutually_exclusive_group()
    group.add_argument("--dry-run", action="store_true")
    group.add_argument("--status", action="store_true")
    group.add_argument("--reset", metavar="MODULE")
    seed.add_argument("-y", "--yes", action="store_true")
    host_parser = sub.add_parser("host", help="Claude Code on the host, locked down; asks first")
    host_parser.add_argument(
        "-c", "--continue", dest="continue_", action="store_true", help="continue the last session"
    )
    host_parser.add_argument("-r", "--resume", nargs="?", const="", metavar="ID", help="resume a session")
    host_parser.add_argument("--model", help="model for the session")
    host_parser.add_argument("prompt", nargs="?", help="a first prompt")
    sub.add_parser("ls", help="list sandboxes")
    sub.add_parser("dash", help="terminal dashboard over all sandboxes")
    return top


COMMANDS = {
    "attach": cmd_attach,
    "shell": cmd_shell,
    "start": cmd_start,
    "resume": cmd_resume,
    "diff": cmd_diff,
    "fetch": cmd_fetch,
    "sync": cmd_sync,
    "update": cmd_update,
    "rc-start": cmd_rc_start,
    "rc-pair": cmd_rc_pair,
    "logs": cmd_logs,
    "stop": cmd_stop,
    "recreate": cmd_recreate,
    "rm": cmd_rm,
    "build": cmd_build,
    "seed": cmd_seed,
    "host": cmd_host,
    "ls": cmd_ls,
    "dash": cmd_dash,
}


def dispatch(argv: Sequence[str], env: Mapping[str, str]) -> int:
    if argv and argv[0] in agents.AGENTS:
        # Everything after the agent name belongs to the agent, verbatim.
        return cmd_agent(Context.load(env), argv[0], argv[1:])
    if argv and argv[0] == "_clipd" and len(argv) == 2:
        return clipd.run(paths_mod.resolve(env), argv[1])
    if argv and argv[0] == "_voiced" and len(argv) == 2:
        return voice.run(paths_mod.resolve(env), argv[1])
    if argv and argv[0] == "_guard" and len(argv) == 2:
        return guard.run(paths_mod.resolve(env), argv[1])
    if argv and argv[0] == "_watch" and len(argv) == 2:
        return watch.run(paths_mod.resolve(env), argv[1])
    args = parser().parse_args(argv)
    if args.command is None:
        if sys.stdin.isatty() and sys.stdout.isatty():
            return cmd_dash(Context.load(env), args)
        parser().print_help()
        return 2
    if args.command == "check":
        return cmd_check(env)
    return COMMANDS[args.command](Context.load(env), args)


def main(argv: Sequence[str] | None = None) -> int:
    env = os.environ
    try:
        return dispatch(list(sys.argv[1:] if argv is None else argv), env)
    except KbxError as exc:
        if env.get("KBX_DEBUG", "").strip().lower() in ("1", "true", "yes", "on"):
            traceback.print_exc()
        print(f"kbx: {exc}", file=sys.stderr)
        return exc.code
    except KeyboardInterrupt:
        print("", file=sys.stderr)
        return 130
