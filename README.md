# kbx

**Self-hosted sandboxes for AI coding agents on a Linux workstation.** Each
project gets its own lightweight VM ([Kata Containers](https://katacontainers.io))
with Claude Code, Codex and pi installed and a full Docker engine. Your checkout
is mounted into it: you and the agent edit and commit in the same repository,
and you push from the host yourself.

Nothing else from your machine goes in: no SSH keys or agent socket, no cloud
credentials or tokens, no other part of `$HOME`, and no network path to the host
or your LAN. The internet stays open. A guard on the host keeps the agent from
planting git hooks, git config or hook-framework files that your own tools
would run. A stricter clone mode keeps the checkout out of the sandbox entirely.

- **For:** individual developers on Linux (KVM required) who use terminal coding
  agents, including Claude and Codex remote control.
- **Not for:** multi-user hosting, macOS/Windows hosts, or per-domain egress
  control.
- kbx is independent of Docker Sandboxes (`sbx`): similar idea and the same
  shared-checkout workflow, fully open-source stack, no egress proxy or
  credential injection.

## Quick start

Needs Linux with KVM, Python 3.11+, git, and Docker Engine with the Kata
runtime. `host/install.sh` checks all of it and installs what is missing
(Arch, Debian/Ubuntu, Fedora, openSUSE); see [docs/host-setup.md](docs/host-setup.md).

```sh
git clone <this repo> ~/src/kbx
~/src/kbx/host/install.sh                     # checks and installs Docker, Kata, firewall,
                                              # clipboard tools; links ~/.local/bin/kbx
kbx build                                     # build the image (a few minutes)

cd ~/code/myproject                           # any git repo with a commit
kbx claude                                    # or: kbx codex, kbx pi
```

The first run creates the sandbox with your checkout mounted at the same path
and attaches, skipping the agents' first-run screens (theme picker, folder
trust). Log in once inside (`/login` in Claude, "Sign in with Device Code" in
Codex); logins persist in the sandbox's home volume.

Claude and Codex default to full permissions inside the VM, which provides the
isolation. The built-in `agent-permissions` module seeds these defaults;
explicit settings you have already chosen inside a sandbox are preserved.
See [configuration](docs/configuration.md#modules-and-optionsmodule) to customize them.

Detach with `Ctrl-\` (configurable). The agent keeps running, and so does
remote control. `kbx claude` or `kbx attach` reattaches. While you are
detached, a desktop notification tells you when an agent finishes a turn or
waits for an approval. A sandbox with no agent session and no shell for 2 hours
stops itself to free its memory; the next kbx command starts it again.

The agent's edits and commits show up in your checkout as it makes them, and
yours show up in the sandbox. Build directories are the exception: the sandbox
keeps its own `target/` and `.venv` (mounted over yours inside the VM), so a
build on one side does not force a full rebuild on the other. Review with `kbx diff` (uncommitted and new
files; `--since-start` adds the commits made since the sandbox started), which
runs git in the sandbox rather than on your host, before you run anything on
the host, then push as usual.

If the agent changes something your tools would run (a git hook, `core.fsmonitor`
or other git config, `.husky/`, `.pre-commit-config.yaml`, `.vscode/settings.json`),
the guard puts the trusted version back, pauses the sandbox and tells you.
`kbx resume` shows the diff and resumes it; `kbx resume --accept` takes the
change back if it was yours. See [docs/git-workflow.md](docs/git-workflow.md).

## Commands

| Command | Does |
| --- | --- |
| `kbx claude\|codex\|pi [args…]` | create/start the sandbox, update the agent, attach (reattach if running) |
| `kbx attach [agent]` | reattach to a running session |
| `kbx shell` | bash in the sandbox, as `agent` |
| `kbx start` | create/start the sandbox without attaching |
| `kbx resume [--accept]` | after the guard paused the sandbox: show what it stopped, resume |
| `kbx fetch [branch…]` | clone mode: sandbox branches → host `refs/remotes/kbx/*` |
| `kbx sync` | clone mode: host branches → sandbox `refs/remotes/host/*` |
| `kbx update` | update all agents now |
| `kbx rc-start` | start Codex remote control without the TUI |
| `kbx rc-pair` | start Codex remote control and print a pairing code |
| `kbx logs` | startup, dockerd and module logs |
| `kbx stop` / `recreate` / `rm` | lifecycle; `recreate` keeps volumes (and switches mode), `rm` asks (clone mode: lists unfetched work) |
| `kbx build` | build the image from core + enabled modules |
| `kbx check` | validate config and modules |
| `kbx seed [--dry-run\|--status\|--reset M]` | manage home defaults |
| `kbx diff [--stat] [--since-start\|--since REV] [path…]` | the agent's changes, from git in the sandbox, safe to view on the host |
| `kbx host [-c\|-r [ID]] [--model M] [prompt]` | the exception: Claude Code on the host, locked down, after you confirm; see below |
| `kbx ls` | list sandboxes and sessions |
| `kbx` / `kbx dash` | dashboard over all sandboxes: guard alerts, what each agent is doing, health, git, agent versions and logins; runs the commands above |

## When it has to run on the host: `kbx host`

Some tasks a VM cannot do. `kbx host` runs Claude Code on the host itself, as a
deliberate exception: it shows what the session may do and asks before it
starts. The session is locked down as far as Claude Code allows. Claude asks
before every tool use (auto and bypass modes are off), and commands run in its
OS sandbox (bubblewrap on Linux; it must start or Claude exits). Commands can
write only the project, never its git directory or hook and editor files, and
can read nothing in your home outside the project and a few toolchain
directories. They get no network and no Unix sockets (no Docker, SSH agent or
D-Bus), and a scrubbed environment without your shell's tokens. The session
has its own Claude login and history, and ignores your Claude settings, MCP
servers and plugins. Adjust it in `[host]` ([configuration](docs/configuration.md#host)).

You do not install Claude Code on the host for this. kbx downloads its own copy
on first use, checks the release signature against Anthropic's key (kept in
this repository) and the checksum, and keeps it in its data directory, off your
`PATH`. So there is no `claude` command that runs unsandboxed by mistake. The
copy never updates itself (Claude's updater would install a regular copy into
`~/.local/bin`); kbx updates it at launch.

It is still much weaker than a sandbox: same kernel, same user, and Claude
itself runs unsandboxed with your approval as the main control. Needs
`bubblewrap`, `socat` and `gpg` on the host.

## Threat model in short

The assumed attacker is the agent itself, turned by prompt injection. It is
root inside its VM. kbx protects your host: separate guest kernel, nothing of
yours mounted but the checkout, a firewall that drops everything from the
sandbox bridge to the host and private ranges, and the guard, which reverts
changes to what host tools run from the repository (git hooks and config,
`commondir`, hook-framework and editor files, also in submodules and nested
repositories) and pauses the sandbox.

Accepted risks: logins inside the sandbox can be stolen (use a separate account
or a spend-limited key); the open internet allows exfiltration of the project
source, **including untracked files like `.env`** in mount mode; **code the
agent wrote is untrusted** (run it in the sandbox, not on the host); the guard
reacts within milliseconds but does not block the write itself, and only
protects while it runs; and while you are attached, images you copy on the host
are pushed into that sandbox for pasting. Clone mode (`[workspace] mode =
"clone"`) keeps the checkout out entirely and git crosses only as bundles. Full
details: [PLAN.md](PLAN.md#threat-model).

## Documentation

- [docs/host-setup.md](docs/host-setup.md): Kata, Docker runtime, firewall, spike checks
- [docs/configuration.md](docs/configuration.md): `~/.config/kbx/config.toml`
- [docs/modules.md](docs/modules.md): module format, seeds, writing your own
- [docs/git-workflow.md](docs/git-workflow.md): mount mode and the guard, clone mode (fetch/sync), worktrees
- [docs/dashboard.md](docs/dashboard.md): `kbx dash`, what it shows and its keys
- [docs/migrating-from-sbx.md](docs/migrating-from-sbx.md): coming from Docker Sandboxes kits
- [PLAN.md](PLAN.md): design and rationale

## Development

```sh
./check                                   # ruff, pyright, shellcheck, unit tests
KBX_INTEGRATION=1 python3 -m unittest discover -s tests/integration -t .
```

Unit tests use a fake `docker` on `PATH` and need nothing else (the clipboard
bridge tests also use Xvfb and python3-xlib when present). Integration tests
need a built image; see [CONTRIBUTING.md](CONTRIBUTING.md).

Linux only. Licensed under the MIT licence ([LICENSE](LICENSE)).
