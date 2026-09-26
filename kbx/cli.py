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

from . import __version__, agents, clipd, dash, git, guard, host, image, private, review, sandbox, session, stage, watch
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
kbx attach [claude|codex|pi]        reattach to a running session
kbx shell                           bash in the sandbox (as agent)
kbx start                           create/start the sandbox (and seed the clone), no attach
kbx resume [--accept]               after the guard paused the sandbox: review, resume
kbx diff [--stat] [--since-start|--since REV] [path…]   the agent's changes, from the sandbox's git
kbx fetch [branch…]                 clone mode: sandbox branches → host refs/remotes/kbx/*
kbx sync                            clone mode: host branches → sandbox refs/remotes/host/*
kbx update                          update all agents without launching
kbx rc-start                        start Codex remote control without the TUI
kbx logs                            startup, dockerd and module logs
kbx stop | recreate | rm            lifecycle (rm asks before deleting volumes)
kbx build                           generate the Dockerfile and build the image
kbx check                           validate modules and config
kbx seed [--dry-run|--status|--reset M]   manage home defaults
kbx ls                              list sandboxes and their sessions
kbx host [-c|-r [ID]] [--model M] [prompt]   Claude on the host, locked down (asks first)
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
    """This project's sandbox, in the mode its container was created with."""
    sb = sandbox.for_project(git.project_root(cwd or Path.cwd()), ctx.config.workspace.mode)
    existing = sandbox.workspace_mode(ctx.docker, sb)
    return sb if existing in (None, sb.mode) else replace(sb, mode=existing)


def guarded(ctx: Context, sb: Sandbox) -> bool:
    return sb.mode == "mount" and ctx.config.workspace.guard


def prepare(ctx: Context, *, seed_clone: bool = True) -> Sandbox:
    """Launch sequence steps 1-4: config, stage, create, start, firewall, then
    the guard (mount mode) or the clone (clone mode)."""
    sb = current_sandbox(ctx)
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
    sandbox.ensure_created(ctx.docker, sb, ctx.config, ctx.paths)
    starting = sandbox.state(ctx.docker, sb) not in ("running", "paused")
    if guarded(ctx, sb) and starting:
        guard.before_start(ctx.paths, sb, ctx.config.workspace.protect)
    sandbox.ensure_running(ctx.docker, sb)
    if guarded(ctx, sb):  # before anything below can fail and leave it unwatched
        guard.ensure(ctx.paths, sb, ctx.docker, ENTRY, ctx.config.workspace.protect)
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
        review.record_start(ctx.docker, ctx.paths, sb)
    return sb


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
            warn(f"image paste is off: {clipd.missing_tools_hint(ctx.env)} on the host")
            try:
                ctx.paths.runtime_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                marker.touch()
            except OSError:
                pass
        return
    try:
        clipd.register(ctx.paths, sb.name, os.getpid(), ENTRY)
    except OSError as exc:
        warn(f"image paste is off: cannot start the clipboard watcher ({exc})")


def _attach(ctx: Context, sb: Sandbox, agent: str, inner: Sequence[str]) -> None:
    _clipboard(ctx, sb)
    if guarded(ctx, sb):
        guard.register_tty(ctx.paths, sb.name)
    host_env = dict(os.environ)
    host_env["HERDR_AGENT"] = agent  # Herdr sees `docker`, not the agent
    key = ctx.config.launcher.detach_key
    if key.lower() not in ("", "none"):
        print(f"  Detach with {key.replace('^', 'Ctrl-')}; reattach with `kbx attach {agent}`.")
    session.exec_attach(
        ctx.docker,
        sb,
        inner,
        workdir=sb.workdir,
        # Marks the session kbx started; kbx-notify reports only these.
        env=session.session_env({**ctx.module_env(), "KBX_SESSION": agent}),
        host_env=host_env,
    )


def cmd_agent(ctx: Context, name: str, args: Sequence[str]) -> int:
    agent = agents.get(name)
    sb = prepare(ctx)
    key = ctx.config.launcher.detach_key
    if session.alive(ctx.docker, sb, agent.name):
        # Never update or touch remote control under a live session.
        print(f"→ Reattaching to the running {agent.name} session in {sb.name}")
        if args:
            warn("agent arguments are ignored when reattaching to a running session")
        _attach(ctx, sb, agent.name, session.dtach_attach(agent.name, key))
        return 0
    if ctx.config.launcher.auto_update:
        agents.update(ctx.docker, sb, agent, strict=False)
    agents.prelaunch(ctx.docker, sb, agent, ctx.config, ctx.resolved)
    argv = agents.command(agent, args, ctx.config, ctx.resolved)
    print(f"→ Starting {agent.name} in {sb.name}")
    _attach(ctx, sb, agent.name, session.dtach_start(agent.name, key, argv))
    return 0


def cmd_attach(ctx: Context, args: argparse.Namespace) -> int:
    sb = existing_running(ctx)
    live = sandbox.sessions(ctx.docker, sb.name)
    name: str | None = args.agent
    if name is None:
        if len(live) != 1:
            raise KbxError(
                "no running sessions" if not live else f"several sessions are running ({', '.join(live)}); name one"
            )
        name = live[0]
    agents.get(name)
    if name not in live:
        raise KbxError(f"no running {name} session; start one with `kbx {name}`")
    sandbox.firewall_check(ctx.docker, sb, ctx.config, dict(ctx.env))
    if guarded(ctx, sb):
        guard.ensure(ctx.paths, sb, ctx.docker, ENTRY, ctx.config.workspace.protect)
    watch.ensure(ctx.paths, sb, ctx.config, ENTRY)
    _attach(ctx, sb, name, session.dtach_attach(name, ctx.config.launcher.detach_key))
    return 0


def cmd_shell(ctx: Context, args: argparse.Namespace) -> int:
    sb = prepare(ctx)
    _clipboard(ctx, sb)
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
        for note in guard.resolve(ctx.paths, sb, ctx.config.workspace.protect, accept=accept):
            print(f"  {note}")
        if not accept:
            print("Kept the neutralized versions; the agent's stay at the quarantine paths above.")
    if status == "paused":
        ctx.docker.run(["unpause", sb.name])
    if status in ("running", "paused") and guarded(ctx, sb):
        guard.ensure(ctx.paths, sb, ctx.docker, ENTRY, ctx.config.workspace.protect)
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
    base = review.start_point(ctx.paths, sb.name) if args.since_start else (args.since or "")
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
    sandbox.ensure_running(ctx.docker, sb)
    if not git.is_seeded(ctx.docker, sb):
        raise KbxError(f"the sandbox clone {sb.workdir} does not exist yet")
    return sb


def cmd_update(ctx: Context, args: argparse.Namespace) -> int:
    sb = prepare(ctx, seed_clone=False)
    for agent in agents.AGENTS.values():
        if session.alive(ctx.docker, sb, agent.name):
            print(f"→ Skipping {agent.name}: its session is running (it updates at its next fresh start)")
            continue
        agents.update(ctx.docker, sb, agent, strict=True)
    return 0


def cmd_rc_start(ctx: Context, args: argparse.Namespace) -> int:
    sb = prepare(ctx)
    agents.codex_remote_control(ctx.docker, sb)
    return 0


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
        f"Guard (host, mount mode): {ctx.paths.log_dir}/guard-{sb.name}.log\n"
        "Host: journalctl -t kata (containerd shim), and firewall drop counters:\n"
        "  sudo iptables -vnL KBX-INPUT; sudo iptables -vnL KBX-FORWARD"
    )
    return 0


def _stop(ctx: Context, sb: Sandbox) -> None:
    sandbox.stop(ctx.docker, sb)
    if guarded(ctx, sb):
        actions = guard.Guard(ctx.paths, sb.name).seal()
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
        print(f"Nothing to remove for {sb.project_dir}.")
        return 0
    inspected = False
    if exists and sb.mode == "mount" and (status != "running" or not git.is_seeded(ctx.docker, sb)):
        # The work is in the checkout; only an old clone from clone mode could hold more.
        print(f"Your checkout {sb.project_dir} is not touched.")
        inspected = True
    elif exists:
        try:
            stage.build(ctx.paths, ctx.config, ctx.resolved)
            sandbox.ensure_running(ctx.docker, sb, timeout=180)
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
    review.forget_start(ctx.paths, sb.name)
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
        print(f"{row['name']:<48} {row['state']:<10} {','.join(live) or '-':<18} {row['project']}")
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
    attach.add_argument("agent", nargs="?", choices=list(agents.AGENTS))
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
