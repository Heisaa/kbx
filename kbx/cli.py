"""Command-line entry point: argparse subcommands, dispatch and exit codes."""

from __future__ import annotations

import argparse
import json
import os
import sys
import traceback
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from . import __version__, agents, clipd, git, image, sandbox, session, stage
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
kbx claude|codex|pi [agent args…]   create/start the sandbox, update, attach
kbx attach [claude|codex|pi]        reattach to a running session
kbx shell                           bash in the sandbox (as agent)
kbx start                           create/start the sandbox and seed the clone, no attach
kbx fetch [branch…]                 sandbox branches → host refs/remotes/kbx/*
kbx sync                            host branches → sandbox refs/remotes/host/*
kbx update                          update all agents without launching
kbx rc-start                        start Codex remote control without the TUI
kbx logs                            startup, dockerd and module logs
kbx stop | recreate | rm            lifecycle (rm asks before deleting volumes)
kbx build                           generate the Dockerfile and build the image
kbx check                           validate modules and config
kbx seed [--dry-run|--status|--reset M]   manage home defaults
kbx ls                              list sandboxes and their sessions
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


def current_sandbox(cwd: Path | None = None) -> Sandbox:
    return sandbox.for_project(git.project_root(cwd or Path.cwd()))


def prepare(ctx: Context, *, seed_clone: bool = True) -> Sandbox:
    """Launch sequence steps 1-4: config, stage, create, start, firewall, seed."""
    sb = current_sandbox()
    for message in image.drift(ctx.docker, ctx.paths, ctx.config, ctx.resolved):
        warn(message)
    for message in stage.build(ctx.paths, ctx.config, ctx.resolved):
        warn(message)
    sandbox.ensure_created(ctx.docker, sb, ctx.config, ctx.paths)
    sandbox.ensure_running(ctx.docker, sb)
    sandbox.firewall_check(ctx.docker, sb, ctx.config, dict(ctx.env))
    if seed_clone and not git.is_seeded(ctx.docker, sb):
        git.seed(ctx.docker, sb)
    return sb


def existing_running(ctx: Context) -> Sandbox:
    sb = current_sandbox()
    status = sandbox.state(ctx.docker, sb)
    if status is None:
        raise KbxError("no sandbox for this project yet; start one with `kbx claude|codex|pi`")
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
        env=session.session_env(ctx.module_env()),
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
    _attach(ctx, sb, name, session.dtach_attach(name, ctx.config.launcher.detach_key))
    return 0


def cmd_shell(ctx: Context, args: argparse.Namespace) -> int:
    sb = prepare(ctx)
    _clipboard(ctx, sb)
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
    print(f"✓ {sb.name} is running; clone at {sb.workdir}")
    return 0


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
    sb = current_sandbox()
    if sandbox.state(ctx.docker, sb) is None:
        raise KbxError("no sandbox for this project yet; start one with `kbx claude|codex|pi`")
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
    sb = current_sandbox()
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
        "Host: journalctl -t kata (containerd shim), and firewall drop counters:\n"
        "  sudo iptables -vnL KBX-INPUT; sudo iptables -vnL KBX-FORWARD"
    )
    return 0


def cmd_stop(ctx: Context, args: argparse.Namespace) -> int:
    sb = current_sandbox()
    if sandbox.state(ctx.docker, sb) is None:
        raise KbxError("no sandbox for this project")
    sandbox.stop(ctx.docker, sb)
    print(f"✓ Stopped {sb.name}")
    return 0


def cmd_recreate(ctx: Context, args: argparse.Namespace) -> int:
    sb = current_sandbox()
    if sandbox.state(ctx.docker, sb) is None:
        raise KbxError("no sandbox for this project")
    stage.build(ctx.paths, ctx.config, ctx.resolved)
    if ctx.docker.inspect("image", ctx.config.image.name) is None:
        raise KbxError(f"image {ctx.config.image.name!r} not found; run `kbx build` first")
    sandbox.stop(ctx.docker, sb)
    sandbox.remove_container(ctx.docker, sb)
    sandbox.ensure_created(ctx.docker, sb, ctx.config, ctx.paths)
    print(f"✓ Recreated {sb.name} from {ctx.config.image.name}; volumes kept. It starts at the next launch.")
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
    sb = current_sandbox()
    exists = sandbox.state(ctx.docker, sb) is not None
    volumes = [v for v in (sb.home_volume, sb.docker_volume) if ctx.docker.inspect("volume", v)]
    if not exists and not volumes:
        print(f"Nothing to remove for {sb.project_dir}.")
        return 0
    inspected = False
    if exists:
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
    sub.add_parser("ls", help="list sandboxes")
    return top


COMMANDS = {
    "attach": cmd_attach,
    "shell": cmd_shell,
    "start": cmd_start,
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
    "ls": cmd_ls,
}


def dispatch(argv: Sequence[str], env: Mapping[str, str]) -> int:
    if argv and argv[0] in agents.AGENTS:
        # Everything after the agent name belongs to the agent, verbatim.
        return cmd_agent(Context.load(env), argv[0], argv[1:])
    if argv and argv[0] == "_clipd" and len(argv) == 2:
        return clipd.run(paths_mod.resolve(env), argv[1])
    args = parser().parse_args(argv)
    if args.command is None:
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
