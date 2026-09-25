# kbx

**Self-hosted sandboxes for AI coding agents on a Linux workstation.** Each
project gets its own lightweight VM ([Kata Containers](https://katacontainers.io))
with Claude Code, Codex and pi installed, a full Docker engine and a private git
clone. The agent works freely inside. You review its branches on the host and
push them yourself.

Nothing from your machine goes in: no SSH keys or agent socket, no cloud
credentials or tokens, no `$HOME`, no project mount, and no network path to the
host or your LAN. The internet stays open.

- **For:** individual developers on Linux (KVM required) who use terminal coding
  agents, including Claude and Codex remote control.
- **Not for:** multi-user hosting, macOS/Windows hosts, or per-domain egress
  control.
- kbx is independent of Docker Sandboxes (`sbx`): similar idea, fully
  open-source stack, no egress proxy or credential injection.

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

The first run creates the sandbox, clones the project into it from a git bundle
and attaches. Log in once inside (`/login` in Claude, `codex login
--device-auth` in `kbx shell`); logins persist in the sandbox's home volume.

Detach with `Ctrl-\` (configurable). The agent keeps running, and so does
remote control. `kbx claude` or `kbx attach` reattaches.

When the agent has committed work:

```sh
kbx fetch                          # sandbox branches → refs/remotes/kbx/*
git log -p main..kbx/<branch>      # review on the host
git merge kbx/<branch> && git push # from your own clone, as usual
```

## Commands

| Command | Does |
| --- | --- |
| `kbx claude\|codex\|pi [args…]` | create/start the sandbox, update the agent, attach (reattach if running) |
| `kbx attach [agent]` | reattach to a running session |
| `kbx shell` | bash in the sandbox, as `agent` |
| `kbx start` | create/start the sandbox and seed the clone without attaching |
| `kbx fetch [branch…]` | sandbox branches → host `refs/remotes/kbx/*` |
| `kbx sync` | host branches → sandbox `refs/remotes/host/*` |
| `kbx update` | update all agents now |
| `kbx rc-start` | start Codex remote control without the TUI |
| `kbx logs` | startup, dockerd and module logs |
| `kbx stop` / `recreate` / `rm` | lifecycle; `recreate` keeps volumes, `rm` lists unfetched work and asks |
| `kbx build` | build the image from core + enabled modules |
| `kbx check` | validate config and modules |
| `kbx seed [--dry-run\|--status\|--reset M]` | manage home defaults |
| `kbx ls` | list sandboxes and sessions |

## Threat model in short

The assumed attacker is the agent itself, turned by prompt injection. It is
root inside its VM. kbx protects your host: separate guest kernel, nothing of
yours mounted, a firewall that drops everything from the sandbox bridge to the
host and private ranges, and git that only crosses as bundle data (the host
never runs git in a repository the agent can write).

Accepted risks: logins inside the sandbox can be stolen (use a separate account
or a spend-limited key); the open internet allows exfiltration of the project
source; **code the agent wrote is untrusted** (run it in the sandbox, not on the
host); repo-defined hooks (husky, pre-commit, lefthook) run on the host when you
commit or push after merging, so review changes to them; and while you are
attached, images you copy on the host are pushed into that sandbox for pasting.
Full details: [PLAN.md](PLAN.md#threat-model).

## Documentation

- [docs/host-setup.md](docs/host-setup.md): Kata, Docker runtime, firewall, spike checks
- [docs/configuration.md](docs/configuration.md): `~/.config/kbx/config.toml`
- [docs/modules.md](docs/modules.md): module format, seeds, writing your own
- [docs/git-workflow.md](docs/git-workflow.md): seeding, fetch/sync, worktrees
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
