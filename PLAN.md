# kbx: agent sandboxes on Kata Containers

## What kbx is

**kbx is a general-purpose, self-hosted sandbox for running AI coding agents
on a Linux workstation.** It is meant for any developer who wants to let an
agent work autonomously on a project without giving it access to the rest of
their machine: SSH keys, cloud credentials, browser data, other projects, or
services on the host and LAN.

Each project gets its own lightweight VM (Kata Containers) with the agents
and a full Docker engine. By default the project checkout is mounted into it,
so the developer and the agent both edit and commit in the same repository; a
host-side guard keeps the agent from planting code that host tools run. A
stricter clone mode gives the sandbox a private clone instead, and git crosses
only as bundles. The developer pushes from the host either way.

- **For:** individual developers on Linux who use terminal coding agents.
  v1 supports Claude Code, Codex and pi, including Claude and Codex remote
  control.
- **Not for:** multi-user or server hosting, macOS/Windows hosts (v1), or
  fine-grained egress control (the internet is open; see non-goals).
- **Relationship to Docker Sandboxes (`sbx`):** similar idea, independent
  implementation. kbx does not use or depend on sbx. It trades sbx's egress
  proxy and credential injection for a fully open-source, inspectable stack.

This document is the implementation plan. It is part of the kbx repository
and contains no personal configuration. User-specific settings belong in
each user's own config directory (see [Shareability](#shareability)).
`kbx` is a working name; check for collisions before publishing.

## Goals

1. **Isolate coding agents from the host**: no access to SSH keys, the
   ssh-agent socket, cloud credentials, tokens, browser data or the rest of
   `$HOME`, and no network path to the host or LAN.
2. **Remote control must work** for Claude Code and Codex.
3. **Agents:** Claude Code, Codex and pi.
4. **Git works on both sides.** In mount mode (default) the agent and the
   developer commit in the same checkout. In clone mode the agent commits in a
   private clone and the host fetches the results. The host pushes itself.
5. **Docker works inside the sandbox.**
6. **Image paste works in all three agents.**
7. **Extensible through modules.** Generic features ship as built-in modules.
   Personal defaults are user modules outside the repo. Together they must be
   able to reproduce a typical sbx kit setup: skills, agent defaults and
   instruction files, statuslines, ChatGPT auth for Codex, Fast-mode reset,
   the Node toolchain, and Playwright + Chromium.
8. **Shareable:** installs from a plain clone anywhere, uses no hard-coded
   personal paths, and follows XDG directories.

## Non-goals (deliberately left out)

These Docker Sandboxes features are deliberately not included. Each one needs
a decision before it is added.

- Egress proxy, per-domain policy and approval flow. The internet is open.
- Credential injection. Agent logins live inside the sandbox; nothing from the
  host is injected.
- Git credentials or `gh` auth inside the sandbox. The agent cannot push.
- Presets (`personal`/`minimal`) and pinned release mode.
- sbx's clone mode with a git-daemon (kbx's clone mode uses bundles).
- Port publishing helpers. Use `docker` flags manually if needed.
- Compatibility with sbx's kit format (YAML specs, args templating,
  create-time snapshot). kbx has its own simpler module format (phase 4) and
  never reads sbx kits.

## Key design decisions

| Topic | Decision |
| --- | --- |
| Runtime | Kata Containers (needed for Docker-in-sandbox with a separate kernel) |
| Network | Full internet; no host, LAN, link-local or metadata access |
| Workspace | Mount mode (default): the checkout at its own path, guarded from the host. Clone mode: a private clone, bundles in and out |
| Granularity | One sandbox per project; claude, codex and pi installed in the same sandbox and sharing the clone |
| Sessions | Detach/reattach without tmux keybindings (see [Sessions](#sessions-detach-without-tmux)) |
| Features | Image paste (**all three agents**), optional Playwright + Chromium, `HERDR_AGENT` hint for Herdr users |
| Pi | Install + skills; optional pi defaults through a user module (template in `examples/`) |
| Agent updates | At launch only, for the launched agent, never while its session is running; installed in the home volume so updates persist |
| Extensibility | Own module format (build layer / declarative seeds / services); built-in modules in the repo, user modules in `$XDG_CONFIG_HOME/kbx/modules` |
| Personal setup | Never in the repo: user config + user modules only |

---

## Threat model

**Assumed attacker:** an agent working against you because of prompt injection
from a web page, issue, dependency or repo file. It runs arbitrary code as the
sandbox user, including root inside the VM (it has Docker).

| Asset | Protected by |
| --- | --- |
| SSH keys, `~/.ssh/config`, ssh-agent socket | Not mounted, no `SSH_AUTH_SOCK`, no host network path |
| Other host secrets (`~/.aws`, `gh` token, `.env` files, browser data) | Nothing from `$HOME` is mounted except a read-only skills copy |
| Host shell config (passphrase capture via `.bashrc`/`PATH`) | No write access to any host path outside the checkout (mount mode) |
| Host execution through git (hooks, `core.fsmonitor`, `core.sshCommand`, filters, `commondir`) | Mount mode: the guard reverts such changes as they happen and pauses the sandbox (see [Workspace](#workspace-mount-mode-and-the-guard)). Clone mode: the host never runs git in an agent-written `.git`; it only fetches bundle data |
| Hook-framework and editor files (`.husky/`, `.pre-commit-config.yaml`, `lefthook.yml`, `.vscode/settings.json`) | Mount mode: guarded like hooks (`[workspace] protect`). Clone mode: review them in the diff |
| Host kernel | Separate guest kernel (Kata VM) |
| Host services (localhost ports, Docker socket, LAN, router, VPN) | Host firewall drops everything from the sandbox bridge to host and private ranges |

**Accepted risks**

- Agent logins inside the sandbox (Claude OAuth, ChatGPT, pi provider keys)
  can be stolen. Use a separate account or API key with spend limits if this
  matters.
- Open internet allows data exfiltration of the project source.
- **Code the agent wrote is untrusted.** Running `npm test`, `make` or the app
  on the host throws away the isolation. In mount mode the agent's edits land
  in your checkout as it makes them, so review `git diff` before running
  anything on the host; run and test inside the sandbox.
- **Mount mode shares the whole checkout**, untracked files included: the
  agent can read `.env` files in it (and send them out), and a crafted file can
  target a host tool the guard does not know. The guard is a watcher, not a
  wall: between the agent's write and its repair there are milliseconds, and it
  only protects while it runs (kbx starts it with the sandbox and checks for
  missed changes before trusting a new baseline). Use clone mode where that is
  not enough.
- **Pushing from the host with a normal checkout** (a deliberate design
  choice: users keep their usual git workflow and tooling). In clone mode, hook
  scripts that live in the repo run on the host when you commit or push after
  merging agent changes: husky (`core.hooksPath=.husky`), the pre-commit
  framework's `.pre-commit-config.yaml`, or `lefthook.yml`. So can other tools
  that act on checked-out files: a changed `.envrc` under direnv (direnv asks
  for a re-`allow`), `.lfsconfig` pointing git-lfs at another server, and
  editor workspace trust. Review changes to these files in the diff before
  merging. `git push --no-verify` skips the push hooks.
- The clipboard channel pushes every image you copy while attached into the
  sandbox (see [Clipboard](#clipboard-image-paste-for-all-agents)).

---

## Architecture

```
host
├── docker engine ── runtime: io.containerd.kata.v2
│   └── network "kbx" (bridge, 172.30.0.0/24, IPv4 only)
│       └── container kbx-<project>-<hash>   ← one Kata VM per project
│           ├── tini → kbx-init (Python: seed, start.sh, supervise services)
│           ├── dtach sessions: claude / codex / pi
│           ├── codex app-server daemon (remote control)
│           ├── Xvfb :0 + clipboard bridge (image paste, all agents)
│           └── volumes:
│               kbx-<id>-home    → /home/agent        (logins, agent config, git clone)
│               kbx-<id>-docker  → /var/lib/docker    (inner docker; see risk R2)
│               stage  (ro bind) → /opt/kbx/stage (modules, config, skills)
│               checkout (bind)  → same path          (mount mode only)
├── nftables/iptables: kbx-firewall (drop sandbox → host / private ranges)
├── kbx launcher (Python)
├── kbx-guard (mount mode, while the sandbox runs: reverts planted hooks/config, pauses)
└── kbx-clipd (while attached: pushes clipboard images / clears into the sandbox)
```

The container is long-lived: `kbx` starts it if stopped and reattaches if it
is running. Everything outside the two volumes is disposable, so
`kbx recreate` rebuilds the container from the current image and keeps the
volumes. With sbx, kits applied only at creation. Here, seed and service
changes apply at the next start, and image changes after a recreate; nothing
sandbox-side is lost either way.

---

## Shareability

Rules that keep kbx usable by anyone:

- **No personal content in the repo.** Built-in modules are generic features.
  Opinionated defaults (a model choice, an output style, an instruction file,
  a statusline layout) are either off by default or shipped only as templates
  under `examples/`.
- **XDG paths only**, with fallbacks:
  - config: `$XDG_CONFIG_HOME/kbx/config.toml` (`~/.config/kbx/…`)
  - user modules: `$XDG_CONFIG_HOME/kbx/modules/<name>/`
  - stage and state: `$XDG_DATA_HOME/kbx/` (`~/.local/share/kbx/…`)
  - runtime (clipd pidfiles): `$XDG_RUNTIME_DIR/kbx/`
- **The checkout location is irrelevant.** `kbx` finds its built-in modules
  relative to its own resolved path and never assumes a sibling directory.
- **Everything environment-specific is configurable**, with safe defaults:
  bridge subnet and name, DNS servers, VM size, image name, Kata runtime name,
  detach key, and skills source directories.
- **No personal skills are bundled.** Skills come from directories the user
  lists in config (default: `~/.agents/skills` if it exists).
- **Host integration is generic.** `HERDR_AGENT` is set for Herdr users and is
  harmless otherwise. Clipboard tools are detected (Wayland or X11).
- **Licence** (e.g. MIT or Apache-2.0) and a `CONTRIBUTING.md` before
  publishing. Code ported from earlier sbx kits (see the appendix) must be
  owned by the contributors or compatibly licensed.
- **Linux hosts only for v1** (Kata needs KVM). This is stated in the README.

---

## Language and tooling

**Python 3.11+ for everything that is a program, bash only for module
scripts.** No framework.

| Component | Language | Notes |
| --- | --- | --- |
| `kbx` launcher (host) | Python, **stdlib only** | `tomllib`, `argparse`, `subprocess`, `hashlib`, `shutil`. No Python packages to install. |
| `kbx-init` + supervisor, `kbx-seed`, `kbx-clip-put` (sandbox) | Python | Only dependencies: `python3-tomlkit` (comment-preserving TOML for seeds) and `python3-xlib` (clipboard), both **Debian packages**, so no pip or venv |
| Clipboard bridge | Python | X11 selection logic ported from an earlier sbx kit (see appendix); new file-based backend |
| `build.sh`, `start.sh`, `host/kbx-firewall.sh` | Bash | apt, iptables and one-liners; checked with `shellcheck` |

**Host requirements:** Python 3.11+, git, Docker Engine with Kata (phase 0),
and for image paste: `wl-clipboard` (`wl-paste`) on Wayland, or `xclip` +
`clipnotify` on X11. If the clipboard tools are missing, `kbx` warns once and
runs without image paste instead of failing.

Conventions:

- **Docker through the CLI** (`subprocess.run(["docker", …])`), not the
  Docker SDK. There is nothing to install, and flags such as `--runtime kata`
  match the spike notes exactly. All calls go through one small
  `kbx/docker.py` wrapper, which is also what the tests fake.
- **The launcher hands the terminal to `docker exec` when it's done.** The
  final attach is `os.execvpe("docker", ["docker", "exec", "-it", …, "dtach",
  …], env)`. No Python process sits between your terminal and the agent, and
  terminal multiplexers such as Herdr see a plain `docker` process.
- **Two packages that share no code:** `kbx/` runs on the host and
  `kbx_sandbox/` is installed in the image. The host resolves everything
  (module selection, options, seed `enforce`/`copy` lists) into the stage's
  `config.json`, so sandbox code never parses `module.toml` or host config.
  This keeps the trust boundary obvious: the sandbox only reads data the host
  wrote.
- **No install step:** `kbx` is a small entry script that resolves its own
  symlink, puts the checkout on `sys.path` and runs `kbx.cli:main`. Clone the
  repo anywhere and `ln -s <clone>/kbx ~/.local/bin/kbx`. A `pipx install`
  path can be added later, since the host package has no dependencies.
- **Types and lint:** type hints throughout, `ruff` (lint + format) and
  `pyright` in strict mode for both packages, and `shellcheck` for module
  scripts. `./check` runs all of them plus the unit tests. The dev tools are
  optional; `kbx` itself never needs them.
- **Tests:** `unittest` (no pytest dependency). Unit tests use a fake `docker` on `PATH`. Integration
  tests run against a real Kata sandbox and are skipped unless
  `KBX_INTEGRATION=1`.
- **Errors:** a clear one-line message plus a non-zero exit for expected
  failures (`KbxError`), and tracebacks only with `KBX_DEBUG=1`.

---

## Phase 0: host spike (do this first)

These are the risky assumptions. Each is a short experiment with a clear
pass/fail. Do not start phase 1 until R1–R4 and R7 pass (or R7's fallback is
chosen). R5 and R6 are sizing and environment checks.

### Install

1. Check for KVM: `ls -l /dev/kvm`, and `kvm-ok` or check `vmx`/`svm` flags.
2. Install Kata 3.x (release tarball to `/opt/kata`). Link
   `containerd-shim-kata-v2` into `/usr/local/bin`.
3. Pick the hypervisor. Start with Cloud Hypervisor (faster boot, virtio-fs).
   Fall back to QEMU if a feature is missing.
4. Register the runtime in `/etc/docker/daemon.json`:

   ```json
   { "runtimes": { "kata": { "runtimeType": "io.containerd.kata.v2" } } }
   ```

5. Smoke test: `docker run --rm --runtime kata alpine uname -r` should print
   the **guest** kernel version, not the host's.

### Risks to settle

| # | Question | Experiment | Fallback |
| --- | --- | --- | --- |
| R1 | Does `--privileged` under Kata pass **host** devices into the guest? | Run with `--privileged`, then `ls /dev` in the guest and compare with the host | Set `privileged_without_host_devices` in the Kata config. If Docker ignores it, drop `--privileged` and use `--cap-add SYS_ADMIN,NET_ADMIN` plus explicit guest devices |
| R2 | Can inner `dockerd` use overlay2 on its storage? overlay2 does not work on virtio-fs | Start dockerd with `/var/lib/docker` on (a) a Docker volume and (b) an ext4 image file loop-mounted inside the guest | (b) is the likely answer: `kbx-<id>-docker` holds `docker.img` (sparse ext4), and init loop-mounts it. Last resort: `fuse-overlayfs` |
| R3 | DNS: Docker's embedded resolver (127.0.0.11) runs in the host netns and is unreachable from the guest; it is also a host service | `getent hosts github.com` inside | Always pass `--dns 1.1.1.1 --dns 9.9.9.9` (configurable) |
| R4 | Firewall: are sandbox → host packets dropped while the internet still works? | See [Network](#network-isolation) test list | Adjust chains (Docker's iptables-nft vs legacy) |
| R5 | VM sizing: default Kata VM is ~2 GB | Run with `--memory 8g --cpus 4` and check `free -g`/`nproc` inside | Set `default_memory`/`default_vcpus` in the Kata config |
| R6 | Nested virtualization: is the host itself a VM? | Check `systemd-detect-virt` | Needs nested KVM enabled on the outer hypervisor |
| R7 | Is image content under `/home/agent` copied into a **new, empty** named volume under Kata (Docker's copy-up)? Agents installed in the image depend on it | Create a container with a fresh `-v test:/home/agent` and check that `~/.local/bin/claude` exists | `kbx-init` does the copy itself on first boot, from `/opt/kbx/home-template` (the image's home, also kept there) into an empty volume |

Deliverable: `host/README.md` with exact install steps and the chosen
Kata/hypervisor settings, and `host/kata-configuration.toml` overrides.

---

## Phase 1: image

`image/Dockerfile.core`, based on `debian:trixie`.

Contents:

- User `agent`, uid 1000, passwordless sudo (the agent has root in the VM
  anyway through Docker).
- `tini` as PID 1, then `kbx-init`.
- Docker Engine + buildx + compose plugin (docker.com apt repo).
- git, git-lfs, curl, jq, ripgrep, python3 (3.11+), `python3-tomlkit`, `dtach`,
  `sudo`, build-essential.
- **Node** (core, because pi and the statusline scripts need it): current LTS
  from nodejs.org, with its checksum verified. The `node-toolchain` module
  installs a different version plus extra packages.
- **Agents, installed as `agent` into the home directory** so that updates
  persist (see [Updates](#updates)):
  - **Claude Code**: native installer
    (`curl -fsSL https://claude.ai/install.sh | bash`), which installs to
    `~/.local`.
  - **Codex**: native, via `image/update-codex-native` (part of this repo),
    run as `agent`. Check where the installer writes, and make sure it is
    under `/home/agent`.
  - **pi**: `npm install -g @mariozechner/pi-coding-agent` with
    `NPM_CONFIG_PREFIX=/home/agent/.local` (confirm the package name at build
    time).
  - `PATH` puts `/home/agent/.local/bin` first, so the volume's copies win
    over anything in `/usr`.
- The `kbx_sandbox` package (`kbx-init`, `kbx-seed`, `kbx-clip-put`), copied to
  `/usr/local/lib/kbx` with entry points in `/usr/local/bin`.
- **Module layers**: each enabled module's `build.sh` as its own `RUN` layer, in
  name order (phase 4). This covers Playwright/Chromium, clipboard dependencies,
  and the Node version.

The image is agent-agnostic. Build with `kbx build`, which generates the
Dockerfile (core + one layer per enabled module) and wraps `docker build`.
The image name is configurable (`[image] name`, default `kbx-agent`).

### init (PID 1 child)

`kbx-init` (Python) runs as root on every container start:

1. If R2 chose a loop image, create (first boot) and mount
   `/var/lib/docker/docker.img`.
2. Read `/opt/kbx/stage/config.json` (phase 4), and make sure the skills
   symlinks exist (`~/.agents/skills`, `~/.claude/skills` →
   `/opt/kbx/stage/skills`).
3. Run `kbx-seed` as `agent` for enabled modules.
4. Run each enabled module's `start.sh` in name order.
5. Start supervised services: `dockerd` (core, logs in `/var/log/dockerd.log`)
   and each enabled module's `service`.
6. Write `/run/kbx/ready`, containing `/proc/sys/kernel/random/boot_id`, the
   seed and start results per module, and service status. All init output
   goes to `/var/log/kbx-startup.log` with timestamps.
7. Stay running as the supervisor (tini reaps orphans).

**Readiness** replaces `lib/wait-startup.py`. Each Kata container start is a
fresh VM boot and `/run` is tmpfs, so a completion record from an earlier
boot can never be mistaken for the current one. The launcher polls `/run/kbx/ready` with a 10-minute timeout and
prints the startup log on failure.

---

## Phase 2: launcher core

`kbx` is the `kbx/` Python package (see
[Language and tooling](#language-and-tooling)). Principles: host-only
configuration, never executing project files, and symlink-resolving
self-location. Internal modules:

| Module | Responsibility |
| --- | --- |
| `cli.py` | argparse subcommands, dispatch, and `KbxError` → exit codes |
| `config.py` | load `config.toml`, apply `KBX_*` overrides, and validate into frozen dataclasses |
| `modules.py` | discover modules, parse and validate `module.toml` and options, detect conflicts (`kbx check`) |
| `stage.py` | build the stage (`cp -rL` equivalent, `config.json`, skills) by syncing in place, file by file; never replace a directory (bind-mount pinning) |
| `image.py` | generate the Dockerfile from core + module layers, compute the module hash, `kbx build` |
| `sandbox.py` | naming, create/start/stop/recreate/rm, readiness wait |
| `git.py` | clone mode: seed, `sync`, `fetch` via bundles (phase 3) |
| `guard.py` | mount mode: host-side guard of `.git` and protected files (see [Workspace](#workspace-mount-mode-and-the-guard)) |
| `agents.py` | per-agent prelaunch and argv (remote control, Codex auth/search, update commands) |
| `session.py` | dtach command lines and the final `execvpe` attach |
| `clipd.py` | host clipboard watcher (spawned in the background, reference-counted per sandbox) |
| `docker.py` | the only place that runs `docker` |

### Commands

```
kbx claude|codex|pi [agent args…]   create/start sandbox, sync skills, update, attach
kbx attach [claude|codex|pi]        reattach to a running session
kbx shell                           bash in the sandbox (as agent)
kbx resume [--accept]               after the guard paused the sandbox: review, resume
kbx fetch [branch…]                 clone mode: sandbox branches → host refs/remotes/kbx/*
kbx sync                            clone mode: host branches → sandbox refs/remotes/host/*
kbx update                          update all agents without launching
kbx logs                            startup log, dockerd log, agent debug log paths
kbx stop | recreate | rm            lifecycle (rm lists unfetched branches and asks before deleting volumes)
kbx build                           generate the Dockerfile from modules and build the image
kbx check                           validate modules and config
kbx seed [--dry-run|--status|--reset M]   manage home defaults (phase 4)
kbx ls                              list kbx sandboxes and their sessions
```

### Naming

`kbx-<project>-<short hash of absolute path>`, where the project part is
the directory basename lower-cased and reduced to `[a-z0-9.-]` (max 32
chars). The container, both volumes and the
labels (`kbx.project=/abs/path`) use it.

### Create (first run for a project)

```sh
docker create \
  --name "$NAME" --hostname "$NAME" \
  --runtime kata \
  --network "$NETWORK" --dns "$DNS1" --dns "$DNS2" \
  --memory "${KBX_MEMORY:-8g}" --cpus "${KBX_CPUS:-4}" \
  <privilege flags decided in R1> \
  -e SANDBOX_NAME="$NAME" \
  -v "$NAME-home:/home/agent" \
  -v "$NAME-docker:/var/lib/docker" \
  -v "$STAGE:/opt/kbx/stage:ro" \
  --label kbx.project="$PROJECT_DIR" \
  "$IMAGE"
```

Mount mode adds `--mount type=bind,source=$PROJECT_DIR,target=$PROJECT_DIR`,
`-e KBX_HOST_UID=… -e KBX_HOST_GID=…` and `--label kbx.workspace=mount`. For a
linked worktree it also mounts the repository's common `.git` directory at its
own path (`--label kbx.gitdir=…`).

The stage path on the host is fixed (`~/.local/share/kbx/stage`), and besides
the checkout in mount mode it is the **only** bind mount. A bind mount pins the directory inode it was created
with, so restaging never replaces the stage root or any directory inside it.
It syncs file by file in place: write each changed file to a temp name in the
same directory and rename it over the old one, then delete files that are
gone. An existing container therefore sees new contents without being
recreated. Mount nothing else. In particular: no other `$HOME` paths, no
`/var/run/docker.sock`, no `SSH_AUTH_SOCK`, and no `--env-file`.

`SANDBOX_NAME` is set so the Claude statusline keeps showing the sandbox name.

### Launch sequence (`kbx <agent>`)

1. Load `~/.config/kbx/config.toml` with env overrides, validate it, and
   compare the image's module hash (drift warning).
2. Rebuild the stage (phase 4) and create the container if it is missing.
3. Mount mode, if the container is stopped: the guard checks an unsealed
   state, then takes its baseline.
   Start the container if it is stopped, then wait for readiness.
   **Firewall check** (fail closed): from inside the sandbox, connect to the
   bridge gateway (from the configured subnet, `172.30.0.1` by default) on an unused port with a 2 s timeout. A timeout
   means the packet was dropped (firewall up). A connection refused/reset
   means the host answered (firewall missing), and kbx refuses to attach and
   prints how to start `kbx-firewall.service`. This runs on every launch and
   attach, because the rules can vanish after a reboot or a Docker restart.
4. Mount mode: make sure the guard runs (checking first if it did not).
   Clone mode, first run only: seed the git clone (phase 3).
5. **If a session for this agent is already running** (its dtach socket
   exists and has a live master), skip steps 6–7 and attach to it directly
   (step 8). The binary is never updated under a live session, and its remote
   control is left alone.
6. Update the agent if `auto_update` is on (see [Updates](#updates)).
7. Agent prelaunch steps: Codex reruns `codex-reset-fast` and `kbx-seed` for
   its modules, then starts remote control. Claude: add `--remote-control`. All agents: `DISPLAY=:0`.
8. Set `HERDR_AGENT=<agent>` for Herdr users (harmless otherwise), register with `clipd` (all agents), then attach.

### Configuration

All settings live in `~/.config/kbx/config.toml` (see
[Staging and configuration](#staging-and-configuration)). Module selection and
module options are there as well. `[launcher]` settings can be overridden per
run with `KBX_<NAME>` (for example `KBX_REMOTE_CONTROL=false kbx claude`):

| Setting | Default | Effect |
| --- | --- | --- |
| `auto_update` | `true` | Update the launched agent before starting it (skipped when reattaching) |
| `remote_control` | `true` | Claude: `--remote-control`. Codex: login check + `codex remote-control start` |
| `skip_onboarding` | `true` | Mark Claude/Codex first-run screens done and trust the working directory before launch |
| `notify` | `"detached"` | Desktop notifications from agent hooks: `detached`, `always` or `off` |
| `idle_stop` | `"2h"` | Stop after this long with no agent session and no shell; `"off"` never |
| `codex_search` | `true` | Codex search flags (as today) |
| `shared_skills` | `true` | Stage and mount skills |
| `detach_key` | `^\` | dtach detach key |
| `memory` / `cpus` | `8g` / `4` | VM size (applied at create; `kbx recreate` to change) |
| `dns` | `1.1.1.1, 9.9.9.9` | Resolvers passed to the container |
| `debug` | `false` | `claude --debug` etc. |

Additional sections: `[network]` (`subnet`, default `172.30.0.0/24`;
`bridge`, default `br-kbx`), `[image]` (`name`), `[runtime]` (`name`,
default `kata`), and `[skills]` (`sources`, default `["~/.agents/skills"]`,
skipped if missing).

---

## Workspace: mount mode and the guard

`[workspace] mode = "mount"` (default) bind-mounts the checkout into the
sandbox **at its own path**, so absolute paths (virtualenvs, build caches,
error messages, `docker run -v $PWD:…` inside) mean the same on both sides.
`kbx-init` gives `agent` the host user's uid/gid, so files keep their owner.
Both sides edit, stage and commit in the one repository; nothing needs
fetching. For a linked worktree (whose `.git` is a file pointing into the main
repository's `.git/worktrees/`), kbx also mounts the repository's common
directory at its own path, and the guard checks that the `.git` file still
points there. Clone mode (below) remains for projects that want the stricter
model.

**The problem.** The agent can now write `.git`, and the host runs git in it:
`core.fsmonitor` runs on every `git status` (editors poll it), hooks on every
commit, and a `.git/commondir` file makes git read another directory's config.
Hook frameworks (husky, pre-commit, lefthook) run in-tree files, and editors
act on their workspace settings.

**Why not read-only mounts.** The agent is root in the guest with
`CAP_SYS_ADMIN` (for the inner dockerd), so a read-only overlay inside the VM
can simply be unmounted. A read-only sub-mount made on the host does not help
either: Kata shares each bind mount into the VM with a plain, non-recursive
`MS_BIND` (see `bindMount` in Kata's `mount_linux.go`), so the guest sees the
files underneath, writable. No layout of `.git` fixes this: git needs to create
and rename files at the top of `.git`, right next to `config` and `commondir`.

**The guard** (`kbx/guard.py`) watches from the host instead, with inotify
(polling every second as a fallback), in the checkout's `.git`, its submodules
and worktrees, and repositories nested in the tree (searched every minute):

- config entries outside a known-safe list (no key that runs a program, reads
  another file as config, or points git elsewhere), new since the baseline,
- hooks, the in-tree `core.hooksPath` directory and `[workspace] protect`,
- `commondir` files that point anywhere but their own repository.

On a finding it neutralizes first (the agent's version goes to quarantine
under `$XDG_DATA_HOME/kbx/guard/<name>/`, the trusted one comes back), then
runs `docker pause`, writes a note to attached terminals and sends a desktop
notification. `kbx resume` shows the diffs and resumes; `--accept` takes the
changes back (for example a hook the user installed on purpose).

**Baseline.** Taken at every start, while no agent can run, so whatever the
user changes while the sandbox is stopped is trusted. When the sandbox stops,
the guard checks once more and seals the state. Without a seal (the guard died,
the host rebooted), the next start checks the old baseline before trusting the
new one, and refuses to start on findings until `kbx resume`.

## Phase 3: git in and out (clone mode)

The rule: **the host never runs git against a repository the agent can
write.** The sandbox clone lives only in the `home` volume, and only bundle
data crosses the boundary.

### Seeding (first run)

```sh
git -C "$PROJECT_DIR" bundle create - --all \
  | docker exec -i -u agent "$NAME" sh -c '
      cat > /tmp/seed.bundle &&
      git clone /tmp/seed.bundle ~/work/<project> &&
      git -C ~/work/<project> remote rename origin host &&
      rm /tmp/seed.bundle'
```

- `-C` means the host runs git **in its own repo**, which is safe.
- **Preconditions:** the project dir must be a git repository with at least
  one commit. Otherwise `kbx` stops with a clear message
  (`git init && git commit --allow-empty -m init`) instead of failing inside
  `git bundle`. A linked worktree on the host is fine, because
  `git bundle --all` reads the shared ref store.
- Uncommitted changes and untracked files (including `.env`) are **not**
  copied. Print a warning if the host tree is dirty. Omitting secrets this
  way is a feature; provide project secrets explicitly if they are needed.
- Copy `user.name` and `user.email` from host git config into the sandbox's
  global git config. Nothing else is copied (no `credential.*`, `url.*`,
  `core.sshCommand` or includes).
- The original `origin` URL is recorded as a plain text note so the agent
  knows the upstream, but no credentials are included.
- Submodules and LFS: submodules need a fetch from their upstream inside the
  sandbox (public repos work over HTTPS). Private submodules are out of scope
  for v1.

### Host → sandbox (`kbx sync`)

A host bundle of `refs/heads/*` is streamed in and fetched into
`refs/remotes/host/*`. The agent's own branches are never touched.

### Sandbox → host (`kbx fetch`)

```sh
docker exec -u agent "$NAME" git -C ~/work/<project> bundle create - --branches \
  > "$TMP/out.bundle"
git -C "$PROJECT_DIR" bundle verify "$TMP/out.bundle"
git -C "$PROJECT_DIR" -c transfer.fsckObjects=true \
  fetch "$TMP/out.bundle" '+refs/heads/*:refs/remotes/kbx/*'
```

- The bundle is untrusted data. Git parses it with fsck enabled. Hooks and
  config from the sandbox repo do not travel in a bundle.
- Then on the host: `git log -p main..kbx/<branch>`, check out or merge
  into your own branch, and `git push`, all from the host's clean repo. See
  the accepted risk about repo-defined hooks.
- Tags are left out by default, because a tag can shadow a branch name in
  review.
- `kbx fetch` prints the fetched refs and the commit counts relative to
  `host/*`.

### Removing a sandbox (`kbx rm`)

The home volume holds the **only copy** of commits you haven't fetched.
`kbx rm` first lists every sandbox branch whose tip isn't reachable from the
host's `refs/remotes/kbx/*` or `refs/remotes/host/*`, with commit counts, and
offers `kbx fetch` before asking for confirmation. It also warns about
uncommitted changes and stashes in the sandbox clone. `kbx recreate` keeps
the volumes, so it doesn't need this check.

### Worktrees

Agents create worktrees inside the sandbox clone as needed, and `kbx fetch` picks up
branches from all of them because they share the ref store.

**Running agents at the same time:** Claude, Codex and pi share one sandbox
and one clone. Two agents editing the same working tree will overwrite each
other's changes. When running more than one at once, give each its own
worktree (`git worktree add ~/work/<project>-codex -b codex/<task>`) and start
it there. This is documented in `docs/git-workflow.md`; there is no special
kbx support.

---

## Phase 4: modules

A **module** is a directory with up to four
optional parts. Each part is handled by one shared, tested piece of code, so
modules contain data and small scripts rather than their own merge logic.

```
modules/<name>/
├── module.toml   # description, default on/off, options, seed overrides
├── build.sh      # image layer: install software        → kbx build
├── home/         # declarative seed tree for /home/agent → every start
├── service       # long-running process, supervised      → every start
└── start.sh      # one-shot per boot; escape hatch       → every start
```

### What each part is for

| Part | When it runs | Applies after a change | Use it for |
| --- | --- | --- | --- |
| `build.sh` | `kbx build`, as root, one Dockerfile `RUN` layer per module | `kbx build` + `kbx recreate` (volumes kept) | apt/npm packages, binaries, anything in `/usr` or `/opt` |
| `home/` | init, every boot, as `agent`, via `kbx-seed` | next container start | default settings, instruction files, scripts in `$HOME` |
| `service` | init, every boot, supervised | next container start | daemons (clipboard bridge) |
| `start.sh` | init, every boot, after seeding, before services | next container start | imperative fix-ups that aren't "add a default" (Fast-mode reset) |

The key rule: **software is installed at build time, never at start.** Starts are fast and work offline, with no apt-lock
races, no Node download on every boot and no files arriving after the script
that needs them. Everything that runs at start reads from a staged copy of
the modules (below), so a change to seeds, services or config shows up at the
next start with no rebuild or recreate.

### module.toml

```toml
description = "Claude Code status line"
default = true            # enabled unless ~/.config/kbx/config.toml says otherwise

[options]                 # typed options with defaults; overridable in config
# version = { default = "26", pattern = '^([0-9]+|[0-9]+\.[0-9]+\.[0-9]+)?$' }

[service]
user = "agent"            # or "root"

[seed]
enforce = []              # keys always set, e.g. ".codex/config.toml:forced_login_method"
copy = []                 # paths copied as whole files even if JSON/TOML
```

- Options reach `build.sh` as build args, and `start.sh` and `service` as
  environment variables: `KBX_OPT_<MODULE>_<OPTION>` (upper-case,
  `-` → `_`). They are validated by the host against `pattern`/`enum` before
  anything runs. Seeds are static; there is no templating.
- There is no `requires`/dependency graph. Build layers run in module-name
  order on top of the core image, and a module may depend only on core.
  `kbx check` rejects conflicts: two modules seeding the same file key, or the
  same file with `copy`.

### `kbx-seed`: declarative home defaults

One Python tool (`kbx-seed` in `kbx_sandbox`, using stdlib + `tomlkit`) applies every
enabled module's `home/` tree to `/home/agent`. The merge depends on the file
type:

| File | Merge unit |
| --- | --- |
| `*.json` | Each key, recursively into objects. Arrays and scalars are single values. |
| `*.toml` | Each key, recursively into tables, written with `tomlkit` so comments and formatting are kept. |
| anything else (and `copy` paths) | Whole file, keeping the mode bits |

**Three-way merge instead of "only if absent".** `kbx-seed` records the
hash of every value it wrote in `~/.local/state/kbx/seeds.json`. For each unit:

| Current state in `$HOME` | Action |
| --- | --- |
| absent, never seeded | write the default |
| equal to what kbx last seeded, and the default has changed | **update** (you never touched it) |
| different from what kbx last seeded | leave it; it's yours |
| absent, but previously seeded | leave it absent; you deleted it |
| listed in `enforce` | always set |

This gives two useful properties. Updated defaults in a module reach existing
sandboxes. And a key you delete stays deleted, unlike a plain "add if
absent" approach, which re-adds it on every start.

Safety: invalid JSON/TOML or a non-object
root **fails that module's seed without writing**. Writes are atomic
(temp + rename). Symlinks in `$HOME` are never followed out of `/home/agent`.

Commands: `kbx seed --dry-run` shows the diff, `kbx seed --reset <module>`
restores that module's defaults on purpose (with confirmation), and
`kbx seed --status` lists units that are default, updated, user-owned or
deleted.

### Services

init runs each enabled module's `service` in the foreground under a small
supervisor. It restarts the service with exponential backoff (at most
5 restarts per minute before giving up and marking it failed), logs to
`/var/log/kbx/<module>.log`, and runs it as the `[service] user`. `dockerd` is
a core service under the same supervisor. Service status appears in
`/run/kbx/ready` and in `kbx logs`. The supervisor is part of `kbx-init`
(Python: `subprocess.Popen` per service, SIGCHLD-driven restarts, and SIGTERM
forwarded on container stop). If it outgrows that, swap in s6 without
changing the module format.

### Staging and configuration

**Host config**: `$XDG_CONFIG_HOME/kbx/config.toml` is the single place for
module selection and options. It is optional; kbx runs with built-in
defaults. Environment variables override single values for one-off runs.
Projects cannot configure kbx, because project files are untrusted.

**Module search path:** user modules in `$XDG_CONFIG_HOME/kbx/modules/`, then
built-in modules in `<checkout>/modules/`. A user module with the same name as
a built-in one **replaces** it entirely. That is how you customise a built-in
without editing the repo.

```toml
[modules]                  # override module.toml defaults
playwright = true
claude-statusline = true
my-claude-defaults = true  # a user module in ~/.config/kbx/modules/

[options.node-toolchain]
version = "26"

[launcher]
auto_update = true
remote_control = true
codex_search = true
detach_key = "^\\"
memory = "8g"
cpus = 4
dns = ["1.1.1.1", "9.9.9.9"]
```

**Staging**: on every launch, the host resolves the config and syncs
`$XDG_DATA_HOME/kbx/stage/`, dereferencing symlinks so the sandbox never sees
a link into the host filesystem. The stage contains:

- `modules/<name>/{module.toml,home,service,start.sh}` for **enabled** modules
  only (no `build.sh`; that part is in the image),
- `config.json`: resolved selection and validated options,
- `skills/`: the skills from `[skills] sources`, merged (the first source wins
  on a name clash, with a warning).

It is mounted **read-only** at `/opt/kbx/stage`. Skills reach the agents
through **symlinks**, not extra mounts: `kbx-init` makes `~/.agents/skills`
and `~/.claude/skills` symlinks to `/opt/kbx/stage/skills` (check pi's path;
add `~/.pi/agent/skills` if needed). A separate bind mount of
`stage/skills` would stay pinned to the old directory after a restage and
serve stale skills. Symlinks through the single stage mount always see the
current contents, and they stay read-only. The stage holds only module files,
skills and resolved config, never secrets. `kbx check` warns if a user module
contains files that look like credentials.

**Image drift**: `kbx build` labels the image with a hash of every enabled
module's `build.sh` plus its options. If a launch sees a different hash,
because you changed a `build.sh`, enabled a module with one, or changed a
build option, it warns: `module X changed; run kbx build && kbx recreate`.

### Built-in modules

Generic features only. Anything opinionated is off by default.

| Module | Default | Parts |
| --- | --- | --- |
| `clipboard` | **on** | `build.sh`: xvfb, python3-xlib, xclip. `service`: Xvfb + `kbx_sandbox/bridge.py` (`kbx-clip-put` is core). The host side (`clipd`) is launcher core and gated on this module |
| `codex-chatgpt-auth` | **on** | `home/.codex/config.toml` with `forced_login_method = "chatgpt"` and `model_provider = "openai"`, both in `enforce`. Required for Codex remote control; turn it off to keep API-key auth |
| `node-toolchain` | off | `build.sh`: a chosen Node version from nodejs.org with checksum, plus `libatomic1`. Options `version`, `apt` (extra apt packages), `npm` (extra global npm packages) |
| `playwright` | off | `build.sh`: Playwright + Chromium + a `chromium` wrapper. Adds about 500 MB |
| `claude-statusline` | off | `home/.claude/statusline.mjs` (sandbox name, model, dir, branch, context, plan quota) and `home/.claude/settings.json` (`statusLine`) |
| `codex-statusline` | off | `home/.codex/config.toml` with `tui.status_line = ["context-used", "five-hour-limit", "weekly-limit"]` |
| `codex-reset-fast` | off | `start.sh`: remove a persisted `service_tier = "fast"` from Codex config on every start, so Fast mode never silently carries over between sessions. The launcher also runs it before each Codex launch |

### User modules and templates

Personal defaults are **user modules**: the same format, in
`$XDG_CONFIG_HOME/kbx/modules/`, usually kept in the user's own dotfiles. The
repo ships starting points under `examples/modules/`, which are never loaded
automatically:

| Template | Content |
| --- | --- |
| `claude-defaults` | `home/.claude/settings.json` with placeholder keys (e.g. `model`) and an empty `home/.claude/CLAUDE.md` |
| `codex-defaults` | `home/.codex/config.toml` with commented examples, and `home/.codex/AGENTS.md` |
| `pi-defaults` | `home/.pi/agent/settings.json` and `home/.pi/agent/AGENTS.md` (paths to confirm against pi's docs) |

To use one: `cp -r examples/modules/pi-defaults ~/.config/kbx/modules/`, edit
it, and enable it in `config.toml`. That gives the "optional pi defaults for
all sandboxes" behaviour, and it works the same for Claude and Codex.

Notes:

- Four features (Claude defaults, both statuslines, Codex auth) are **pure
  data**; one tested `kbx-seed` does all the merging.
- Skills are launcher core (staging + symlinks), not a module, and are
  configured by `[skills] sources` and `[launcher] shared_skills`.
- Agents, Docker Engine, dtach, git, Node LTS and Python are **core**. They
  are always needed, and the launcher depends on them.

### Adding a module later

1. `mkdir ~/.config/kbx/modules/<name>` (or `modules/<name>` in the repo for a
   generic built-in) and write `module.toml` with at least `description` and
   `default`.
2. Add only the parts you need.
3. Run `kbx check`, which validates TOML, options, seed conflicts,
   executable bits, and `shellcheck` on scripts when it is installed.
4. Launch. If it has a `build.sh`, the drift warning tells you to rebuild.

## Phase 5: agents, auth and remote control

All logins happen **inside** the sandbox and persist in the `home` volume. A
login done once per project sandbox survives stops and recreates.

### Claude Code

- Log in once: `/login` with the claude.ai subscription. Remote control
  needs a claude.ai login, not an API key.
- First-run screens are skipped (`[launcher] skip_onboarding`): before each
  launch `kbx-onboard` sets `hasCompletedOnboarding` and the working
  directory's `hasTrustDialogAccepted` in `~/.claude.json`, and for Codex
  `projects."<workdir>".trust_level = "trusted"` (only if unset). The sandbox
  is the trust boundary, so the folder-trust prompt protects nothing here.
- Launch: the launcher inserts `--remote-control` after the options and
  before any `--` (the flag takes an optional name, so it must not precede a
  positional argument), unless the user already passed it. An optional name comes from `kbx claude
  --remote-control <name>`.
- Traffic is outbound HTTPS only, so nothing on the host needs to be published.
- The session runs in dtach, so remote control stays up while you are
  detached.

### Codex

- Log in once: `kbx shell` → `codex login --device-auth`. Enable MFA on the
  ChatGPT account first, because remote-control enrollment returns HTTP 403
  (`Multi-factor authentication required`) without it.
- On each launch: check that
  `codex login status` says ChatGPT, then run `codex remote-control start`,
  which is safe to repeat. Warn and continue on failure.
- Pair once: `codex remote-control pair` inside the sandbox.
- The app-server daemon runs inside the VM and survives detach. A container
  restart kills it, and the next `kbx codex` starts it again. Add
  `kbx rc-start` for starting remote control without opening the TUI.

### pi

- Log in or set keys inside the sandbox (`/login`, or provider API keys in
  pi's auth file). pi has no remote-control feature, and none is planned.

### Updates

**When:** only at launch (`kbx claude|codex|pi`), only for the agent being
launched, and only when `auto_update` is on (the default). No updates happen
at container start, in the background, or for agents you didn't open.
`kbx update` updates all three on demand.

**Never under a live session:** if that agent's dtach session is already
running, the launch just reattaches and does not update (launch sequence,
step 5). The update applies the next time that agent is started fresh.

**Codex remote-control daemon:** once remote control is on, the app-server
daemon is almost always running, so waiting for it to stop would mean Codex
never updates. Instead, a fresh `kbx codex` updates the binary (the installer
replaces it on disk; the running daemon keeps the old version in memory), and
**kbx never restarts the daemon itself**, because remote sessions from your
phone may be active. The daemon picks up the new version at the next container
start, or when you run `codex app-server daemon restart`. `kbx codex` prints
a note when the daemon's version differs from the installed binary.

**Commands:** `claude update`, `update-codex-native`, and
`npm update -g @mariozechner/pi-coding-agent` (with the home npm prefix). For
the agent being launched, failures are warnings and the installed version
starts. `kbx update` fails hard.

**Where updates land:** all three agents live under `/home/agent`, which is
the `kbx-<id>-home` volume, so updates survive `kbx stop` and
`kbx recreate`. The copy in the image only seeds a **new** sandbox (via
Docker's volume copy-up, risk R7). Rebuilding the image therefore does not
downgrade existing sandboxes, and a new sandbox starts at the image's
version and then updates at its first launch.

`kbx update` and the launch-time update both print before → after versions.

### Debuggability

Everything in the stack is inspectable (no closed proxy in the path), so make
it easy to find: `kbx logs` prints the paths and tails:

- `/var/log/kbx-startup.log`, `/var/log/dockerd.log`, `/var/log/kbx/<module>.log`
- `~/.claude/debug/*.txt` (launch with `KBX_DEBUG=true` → `claude --debug`)
- `~/.codex/log/*`, and `codex app-server daemon` status
- Host: `journalctl -t kata` / the containerd shim logs, and firewall drop
  counters (`nft list table inet kbx`)
- `tcpdump -i eth0` inside the VM works (it has root).

---

## Sessions: detach without tmux

Requirement: attach and detach only, with no key bindings that collide with
a host-side terminal multiplexer (Herdr, tmux, zellij…).

Use **dtach**. It does one thing: it keeps a program running on a socket and
lets you attach and detach. It has no prefix key, no panes, and no
scrollback. The only key it intercepts is the detach key (`-e`), and even that
can be disabled.

```sh
# start-or-attach (inside the sandbox, via docker exec -it)
dtach -A /run/kbx/sessions/claude.sock -e "$KBX_DETACH_KEY" -r winch \
  claude --remote-control …
```

- Detach key: `[launcher] detach_key`, default `Ctrl-\`. Pick one your
  multiplexer doesn't use. `-E` disables it entirely, and then you "detach" by
  closing the pane or terminal.
  `docker exec` ends, while the dtach master and the agent keep running.
- `-r winch`: on reattach the TUI gets SIGWINCH and redraws itself. Claude
  (Ink) and Codex (ratatui) both handle this. Verify pi.
- Output passes through unchanged, so **the host terminal's or multiplexer's
  scrollback and OSC 52 clipboard writes work**, which a nested tmux would get
  in the way of.
- One socket per agent, so claude, codex and pi can run side by side in the
  same sandbox. `kbx ls` lists sockets. `kbx attach` with no argument attaches
  if exactly one session exists.
- The sessions dir is on `/run` (tmpfs), so sockets vanish on container
  restart, which is correct because the agent is gone too.
- Host tools see `docker exec`, not the agent. For Herdr, `HERDR_AGENT` is
  added to the env passed to `os.execvpe` for the final `docker exec -it …`,
  so Herdr can still recognise the agent.

---

## Notifications, login status and the idle stop

**What the agents do.** The `notify` module (on by default) seeds hooks that
run `kbx-notify` in the sandbox: Claude Code's `UserPromptSubmit` and
`PostToolUse` (working), `Notification` with `permission_prompt` or
`elicitation_dialog` (waiting), `Stop` (done) and `SessionEnd`; Codex's
`notify` program (`agent-turn-complete`, `approval-requested`). It records
the state in `/run/kbx/agents/<agent>.json` and appends it to
`events.jsonl`. Only sessions the launcher started count (it sets
`KBX_SESSION=<agent>`). Hooks never print or fail, and `PostToolUse` writes
only on a change. `kbx-session status` reports each live session's state and
whether a terminal is attached: dtach sets the socket's execute bit while a
client is attached, and the socket's mtime is the session's start, so a state
from an earlier session is ignored.

**The host watcher.** `kbx _watch NAME` runs per running sandbox, started by
the launcher like the guard, and follows `kbx-notify follow` over one
long-lived `docker exec`, so the sandbox still never connects to the host. On
`waiting` or `done` it runs `notify-send` (`[launcher] notify`: only when no
terminal is attached to that session, always, or off), at most once a minute
for the same text and six a minute in all. The text is untrusted: type-checked, control characters
removed, cut short, markup escaped, and passed after `--`.

**Idle stop.** The same watcher stops the sandbox after `[launcher]
idle_stop` (2 h) with no agent session (from the stream's periodic session
list) and no interactive `docker exec` into it (host `/proc`), the way `kbx
stop` does; the guard seals as usual. Never while paused or with a guard
alert pending. An image without `kbx-notify` falls back to polling
`kbx-session list` for the idle stop only. The Codex app-server daemon does
not count: it outlives every Codex session, so counting it would keep most
sandboxes from ever stopping. Remote control started with `kbx rc-start` and
no open session therefore ends at an idle stop (`idle_stop = "off"` keeps it).

**Login status.** `kbx-login-status` answers from `claude auth status`,
`codex login status` and pi's `auth.json` provider names (no secrets). The
launcher says how to log in before attaching to a fresh session, and the
dashboard shows it per agent.

## The exception: `kbx host`

Some tasks need the host (devices, the desktop, the host's services). `kbx
host` runs Claude Code there, as a conscious exception: a terminal is
required, it prints what the session may do and asks, and it passes on only
`--continue`, `--resume`, `--model` and a prompt, so no flag can loosen it.
Everything else is one generated settings file plus flags, checked against
Claude Code 2.1.283:

- kbx's own Claude Code (`kbx/hostclaude.py`), so the host needs no Claude
  install and has no `claude` command that runs unlocked by mistake. From
  downloads.claude.ai, like the official installer but without its `claude
  install` step: the channel's version, `manifest.json`, whose detached PGP
  signature must be good and by Anthropic's release key (kept in
  `host/claude-code-release.asc`, fingerprint `31DD DE24 DDFA B679 F42D 7BD2
  BAA9 29FF 1A7E CACE` pinned in code), then the platform binary against the
  manifest's SHA-256 and size. Stored in `$XDG_DATA_HOME/kbx/host-claude-bin/`,
  the old version removed after an update. Run with `DISABLE_AUTOUPDATER`,
  `DISABLE_UPDATES` and `DISABLE_INSTALLATION_CHECKS`: without them the copy
  updates itself within a minute into `~/.local/bin/claude` and
  `~/.local/share/claude` (tested). kbx updates it at launch instead
  (`[launcher] auto_update`, `[host] channel`), keeping the current copy when
  offline.
- `--restricted` (user, project and local settings ignored; file tools
  confined to the project; a person approves settings, git and tool-config
  writes; no bypass), `--tools Bash,Read,Edit,Write,Glob,Grep` (no web tools),
  `--strict-mcp-config`, and `CLAUDE_CONFIG_DIR` in kbx's data directory, so
  the session has its own login and history.
- Permissions: mode `manual`, `disableAutoMode` and
  `disableBypassPermissionsMode`; `Edit(...)` denied for the git directories
  and guarded files.
- The command sandbox (bubblewrap, socat and seccomp): `enabled`,
  `failIfUnavailable`, `allowUnsandboxedCommands: false`,
  `autoAllowBashIfSandboxed: false`; `denyRead` $HOME, `/run/user/<uid>`,
  `/mnt`, `/media` with `allowRead` for the project and `[host] allow_read`;
  `allowWrite` the project and `[host] allow_write`; `denyWrite` the git
  directories (a linked worktree's shared one too), the guard's `protect`
  list and `.claude`, `.mcp.json`, `.envrc`, `.vscode`, `.idea`; network only
  to `[host] allowed_domains`. Unix sockets are blocked by seccomp on Linux.
- A scrubbed environment (basic variables and `[host] env`).
- A preflight refuses without `claude`, `bwrap` or `socat`, or when
  bubblewrap cannot create a namespace (Ubuntu's AppArmor restriction).

Tested with the real Claude in a VM: reads of `~/.claude.json`,
`~/.gitconfig` and `/run/user`, writes to `.git`, `.claude`, `.envrc` and
`.husky`, the network and Docker's socket are all refused; project writes
work; the Read tool is refused outside the project; Bash is refused when
nobody approves. Claude's sandbox leaves an empty `.git/config.worktree`
(git ignores it without `extensions.worktreeConfig`; the guard does too) and
`.claude/.cc-writes` in the project, and may write its own temp directory
`/tmp/claude-<uid>`.

Not covered: Claude itself (login, API traffic, the model sees what it
reads), commands reading the rest of the system outside $HOME, and whatever
you approve.

## Review: `kbx diff`

The host never runs git in a repository the agent can write, so `kbx diff`
runs git in the sandbox at the working directory, with fsmonitor, external
diff and textconv off, and untracked files added as intent-to-add in a
throwaway index (the real index is untouched). `--since-start` diffs against
the commit `HEAD` was at when the sandbox started, recorded on the host at
each start. Git prints no colour; the host replaces every control character
with `?` and colours lines itself, so a file cannot hide lines or drive the
terminal. It is a view the sandbox produces, not a proof: the agent is root
there.

## Clipboard: image paste for all agents

Image paste must work in Claude, Codex and pi. Each agent reads the clipboard
differently:

| Agent | How it reads a pasted image on Linux |
| --- | --- |
| Claude Code | Runs `xclip -selection clipboard -t TARGETS` / `-t image/png -o`, or `wl-paste` |
| pi | Runs `wl-paste --list-types` / `--type`, or `xclip -selection clipboard -t <mime> -o` |
| Codex | Reads X11 directly through arboard; does not run the CLI tools |

So **one X11 clipboard inside the sandbox serves all three**. The Xvfb display
`:0` plus `bridge.py`, which owns the `CLIPBOARD` selection, is
readable both by the real `xclip` (Claude, pi) and by arboard (Codex). No
per-agent shims are needed.

Design constraint: the sandbox may not open connections to the host, so the
clipboard cannot be fetched on demand (the way Docker Sandboxes does it).
Instead, **the host pushes, and the sandbox never calls out.**

```
host clipboard ──(kbx-clipd: watch)──► docker exec kbx-clip-put ──► ~/.cache/kbx-clipboard/
                                                                       │
                              Xvfb :0 ◄── bridge.py owns CLIPBOARD ◄────┘
                                 ▲
             xclip (claude, pi) ─┴─ arboard (codex)
```

- **Host (`kbx/clipd.py`)**: runs while **any** agent session is attached
  and stops when the last one detaches. There is one per sandbox, found through
  a pidfile. `os.execvpe` keeps the launcher's PID, so each attach registers
  its own PID (which becomes the `docker exec` process) with clipd. clipd exits
  when none of the registered PIDs is alive. It watches every clipboard change (Wayland:
  `wl-paste --watch`; X11: `clipnotify`):
  - if the new content has an image type → push it
    (`docker exec -i -u agent $NAME kbx-clip-put image/png < data`), with the
    existing 64 MiB limit;
  - otherwise → push **clear**. Without this, a stale image would be attached
    after you copy text on the host.
- **Sandbox (`kbx-clip-put`)**: atomically writes the image, its MIME type and
  a sequence number to `~/.cache/kbx-clipboard/`, then notifies the bridge.
- **Bridge (`bridge.py`)**: an X11 selection owner with INCR support for large
  images. Its source is that directory, and there is one channel per sandbox.
  It owns the selection when an image is present and releases it
  on clear.
  - **Advertise exact `TARGETS`** (`image/png`, plus `TARGETS`/`TIMESTAMP`) and
    **refuse any other target**. pi probes `xclip -t image/*` even when
    `TARGETS` lists no image, and an owner that answers arbitrary targets makes
    pi paste text as a fake `.png`
    ([earendil-works/pi#9786](https://github.com/earendil-works/pi/issues/9786)).
- **Environment**: every agent session starts with `DISPLAY=:0`. Install
  `xclip` in the image. **Do not** install `wl-clipboard` or set
  `WAYLAND_DISPLAY`, so Claude and pi take the xclip path.
- **Keys**: Ctrl+V pastes images in all three on Linux. The host terminal or
  multiplexer must pass Ctrl+V through. Text paste is
  still the terminal's own paste (bracketed paste), which never touches this
  channel.
- **Copy out (`/copy`)**: text copied inside the sandbox reaches the host
  through the terminal via OSC 52, which passes through dtach to the host
  terminal.
  Check at implementation time which agents emit OSC 52 when `DISPLAY` is set.
  For those that only write to X11, the bridge forwards `CLIPBOARD` text
  writes as OSC 52 to the attached pty. Otherwise `/copy` from that agent is
  left out of v1.
- **Privacy trade-off** (also listed under accepted risks): while you are
  attached to any session, every image you copy on the host is pushed into
  that sandbox. Text is never pushed, only a "clear".
- **Tests**: bridge tests on an isolated Xvfb display with a file source. Add
  per-agent checks: `xclip -selection clipboard -t TARGETS -o` lists
  `image/png`; `xclip -t text/plain -o` fails; the image round-trips
  byte-identical; arboard reads it (small Rust or Python test, or a manual
  Codex paste); a clear leaves no owner. Manual: Ctrl+V in Claude, Codex and
  pi.

---

## Network isolation

Docker network, created once by `kbx` from `[network]` config (defaults shown):

```sh
docker network create --driver bridge --ipv6=false \
  --subnet 172.30.0.0/24 -o com.docker.network.bridge.name=br-kbx kbx
```

Host firewall (`host/kbx-firewall.sh`, run by a systemd unit
`After=docker.service`, idempotent; reads the bridge name from
`/etc/kbx/firewall.conf`, installed by `host/install-firewall`):

```sh
# 1. Nothing from the sandbox bridge reaches the host itself (any host IP,
#    any port, including Docker's DNS, localhost-bound services via the
#    bridge gateway, and the Docker API if exposed on TCP).
iptables -I INPUT -i br-kbx -j DROP
iptables -I INPUT -i br-kbx -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT

# 2. Forwarded traffic: drop private, CGNAT, link-local and metadata ranges.
for net in 10.0.0.0/8 172.16.0.0/12 192.168.0.0/16 100.64.0.0/10 \
           169.254.0.0/16 127.0.0.0/8 224.0.0.0/4; do
  iptables -I DOCKER-USER -i br-kbx -d "$net" -j DROP
done
iptables -I DOCKER-USER -i br-kbx -m conntrack --ctstate ESTABLISHED,RELATED -j RETURN

# 3. IPv6: network created without IPv6; also drop at ip6tables for br-kbx.
ip6tables -I INPUT -i br-kbx -j DROP
ip6tables -I FORWARD -i br-kbx -j DROP
```

(In the final version this should be written as a dedicated nftables table if
the host's Docker uses iptables-nft. Rule order and idempotency are handled by
the script, which deletes and recreates its own chain.)

Notes:

- The host's **public** IP is reachable from the internet side anyway. The
  goal is that the sandbox cannot reach services bound to the host's
  localhost or LAN interfaces, which rule 1 covers.
- VPNs such as Tailscale use CGNAT (100.64/10), which is blocked. Other VPN
  ranges must be added if they are not RFC1918.
- Inner Docker containers NAT through the VM, so the same rules apply to them.

**Tests** (automated in `tests/integration/test_network.py`, run inside the sandbox):

| Target | Expected |
| --- | --- |
| `https://api.anthropic.com`, `https://chatgpt.com`, `https://registry.npmjs.org`, `https://github.com` | reachable |
| bridge gateway `172.30.0.1` (all ports, ICMP) | blocked |
| host LAN IP, router IP, any `192.168.*` | blocked |
| `169.254.169.254` | blocked |
| a test HTTP server on host `127.0.0.1:8000` and `0.0.0.0:8000` | blocked |
| `host.docker.internal` | not resolvable / blocked |
| inner `docker run alpine wget` to the host LAN IP | blocked |

---

## Dashboard: `kbx dash`

A terminal dashboard over every kbx sandbox on the host, in the spirit of
sbx's. `kbx dash` opens it, and so does plain `kbx` on a terminal (without a
terminal, plain `kbx` still prints the usage). It uses only the standard
library (`curses`), so kbx still has nothing to install.

sbx's dashboard shows egress policy, approvals and credentials; kbx has none
of those (see [Non-goals](#non-goals-deliberately-left-out)). Its place goes
to what kbx does have: the guard, the workspace mode and clone-mode git.

### What it shows

The list has one row per sandbox (`docker ps` on the `kbx.name` label):

| Column | Source |
| --- | --- |
| name, project | container name, `kbx.project` label (`~` for `$HOME`) |
| state | container state; `paused` is highlighted, because the guard pauses |
| mode | `kbx.workspace` label (`mount`/`clone`) |
| guard | `ALERT` (alert file pending), `UNGUARDED` (mount mode, running, guard on in the config, no guard process), `watching`, `off` (`[workspace] guard = false`), `-` (clone mode or not running) |
| sessions | `kbx-session list` in the sandbox (running only) |
| CPU, memory | `docker stats --no-stream` (running only; `-` if the runtime reports none) |
| flags | `stale`, `orphan`, `health` (see below) |

The detail pane shows the selected sandbox in sections:

1. **Guard.** Its state in words. With an alert: the `kbx resume` report
   (the findings and the diffs of the agent's versions) and the keys to resume
   or accept. When unguarded: why that matters and the key that starts a guard.
2. **Health.** The `kbx-init status` record: failed seeds, failed `start.sh`
   and failed services, or "all ok".
3. **Git.** Mount mode: commits on `HEAD` since the sandbox started (count
   and the last few subjects) and the number of uncommitted paths. Clone mode:
   unfetched branches and uncommitted changes and stashes in the clone
   (`git.unfetched`, `git.local_changes`), with fetch and sync keys.
4. **Agents.** The installed versions of claude, codex and pi; the latest
   published version once checked (a key, since it needs the network); whether
   the Codex remote-control daemon runs and on which version (as `kbx codex`
   notes, it may run an older Codex than the installed one).
5. **Staleness.** The image lags the enabled modules or core sources
   (`image.drift`: build, then recreate); the container was created from an
   older image than the current one (recreate); the container's mode differs
   from the config's (recreate switches); the project directory is gone
   (orphan).

### Actions

Everything that already has a command runs that command: the dashboard ends
curses, runs `kbx <command>` in the sandbox's project directory, and redraws
when it exits. So attach and shell hand over the terminal as usual and come
back to the dashboard on detach, and `kbx rm` and `kbx resume` ask their own
questions. Commands that print and exit wait for Enter first.

| Key | Runs |
| --- | --- |
| Enter | attach: the only live session, else ask which agent (starting it if none runs) |
| `c` `x` `p` | `kbx claude`, `kbx codex`, `kbx pi` |
| `s` | `kbx shell` |
| `S` | `kbx start` (also starts a missing guard) |
| `t` | `kbx stop` (asks first) |
| `r` | `kbx recreate` (asks first) |
| `D` | `kbx rm` (it asks, and lists unfetched work in clone mode) |
| `R` / `A` | `kbx resume` / `kbx resume --accept` (asks first) |
| `f` / `y` | `kbx fetch` / `kbx sync` (clone mode) |
| `u` / `U` | `kbx update` / check the latest published agent versions |
| `l` | `kbx logs` |
| `b` | `kbx build` |
| Tab / ← → | full-screen details / other sandbox there |
| `g` | refresh now; `?` help; `q` quit |

An orphan (its project directory is gone) cannot run commands, which work
from the project directory. The dashboard offers stop and remove for it
directly (container, volumes and guard state, after a typed confirmation).

### Narrow terminals

The dashboard must work in a phone's terminal (Termux, 40-odd columns). The
layout follows the width: two lines per sandbox below 60 columns; above
that, a table whose columns take their content's width and drop by priority
(state, guard, sessions and flags stay longest); details, help and prompts
wrap rather than cut. Tab shows the details on the whole screen, where ↑ ↓
scroll (a swipe in Termux sends them) and ← → switch sandboxes; every key
this needs is on Termux's default extra-keys row.

### Refresh and cost

A `docker exec` into a Kata VM takes a noticeable fraction of a second, so
the dashboard never runs one per sandbox on every redraw:

- every 2s: `docker ps` + one `docker inspect` for all sandboxes, and the
  guard's host files (pid, alert). No exec.
- every 10s, in a background thread: sessions of running sandboxes, and
  `docker stats`.
- the selected sandbox only, in the background, when selected and every 15s
  after: health, git, agent versions and remote control.
- only on request: the latest published agent versions.

The UI thread never waits for docker; the pane shows `…` until a result
arrives, and the age of the data it shows.

### Security

- **No host git in a shared checkout.** In mount mode the agent can write
  `.git/config` and `.gitattributes`: a `git status` from the host runs
  `core.fsmonitor` and clean filters, and the guard puts them back only after
  the write. A person running git has the same exposure, but a dashboard
  polling git every few seconds would run it with no one acting. So all git
  information in mount mode comes from inside the sandbox (`docker exec git …`
  at the same path); a stopped sandbox shows none. Clone mode already follows
  the rule of `kbx/git.py` (host git only in the host's own repository).
- **Sandbox output is untrusted text.** Commit subjects, branch names, the
  guard's diffs and status fields come from the agent. Every string is
  stripped of control characters (C0, C1, DEL) before it reaches the
  terminal, and cut to the pane width.
- **The dashboard is not part of the protection.** The launcher still starts
  the guard; the dashboard only shows when one is missing and offers `kbx
  start`, which starts it. Closing the dashboard changes nothing.
- **No listener.** A TUI needs no server, no port and no auth, unlike a web
  dashboard.
- The latest-version check contacts `registry.npmjs.org` from the host (for
  `@anthropic-ai/claude-code`, `@openai/codex` and pi's package), only when
  asked. The npm version is a stand-in for the native installers' version.

### Code

- `kbx/status.py`: collection, no curses. Dataclasses for a sandbox row and
  its details, the classification (guard column, flags) as pure functions,
  and the background refresher. Unit-tested against the fake docker.
- `kbx/dash.py`: the curses loop, drawing (rendering to lines first, so it is
  testable without a terminal), keys, confirmations and running commands.
- `kbx/cli.py`: the `dash` subcommand and plain `kbx` on a terminal.

---

## Phase 6: tests and docs

- `tests/unit/test_cli.py`, `test_config.py`, `test_sandbox.py`, …: launcher
  unit tests with a fake `docker`. Cover config validation, XDG path resolution,
  user-module override of built-ins, naming, create args (assert
  that **no host paths other than the read-only stage** are mounted), the
  remote-control argument insertion, and the dtach command line.
- `tests/unit/test_seed.py`: `kbx-seed` against temporary homes. Cover all five
  three-way cases for JSON keys, TOML keys (comments preserved) and whole
  files; `enforce`; invalid JSON/TOML leaves the file untouched; a symlink
  pointing out of `$HOME` is refused; `--reset`, `--dry-run`, and conflict
  detection in `kbx check`.
- `tests/unit/test_modules.py`: every module's `module.toml` validates, options
  reject bad values, and the generated Dockerfile has one layer per enabled
  module in name order.
- `tests/integration/test_network.py`: the table above, plus the launch-time
  firewall check: with `kbx-firewall.service` stopped, `kbx` must refuse to
  attach.
- `tests/unit/test_stage.py`: restaging never replaces a directory (inode of
  the stage root and `skills/` unchanged), removed files disappear, and a
  running container sees an edited skill without a restart.
- `tests/integration/test_git.py`: seed → commit in sandbox → `kbx fetch` → host sees
  `kbx/<branch>`. Then a **malicious-repo test**: the sandbox adds a
  `.git/hooks/post-merge`, sets `core.fsmonitor`, and sets `core.hooksPath`
  to a script that writes a marker file. After `kbx fetch` and a host
  merge, the marker must not exist. Also: `kbx rm` with unfetched commits
  lists them and does not delete without confirmation; a non-git dir and an
  empty repo give the precondition message.
- `tests/integration/test_secrets.py`: inside the sandbox, `env`, `ls -la /home /root /mnt`,
  `findmnt`, and a search for `id_*`/`SSH_AUTH_SOCK` should find nothing from
  the host.
- Remote-control checklist (manual): Claude session appears in the
  claude.ai/app list, survives detach, and survives a host terminal close.
  Codex pairs, survives detach, and comes back after `kbx stop` plus relaunch.
- A **clean-environment test**: run the unit tests with an empty `$HOME` and
  no config file, so nothing depends on any particular machine.
- Docs: `README.md` (what it is, threat model summary, quick start),
  `docs/host-setup.md` (Kata, firewall), `docs/configuration.md`,
  `docs/modules.md` (format + writing user modules), `docs/git-workflow.md`,
  and `docs/migrating-from-sbx.md` (a generic version of the appendix).

---

## Proposed repository layout

```
kbx/                        # its own repository; clone anywhere
├── PLAN.md
├── README.md
├── LICENSE
├── CONTRIBUTING.md
├── kbx                     # entry script: resolve symlink, run kbx.cli:main
├── check                   # ruff, pyright, shellcheck, unit tests
├── pyproject.toml          # tool config only (ruff, pyright); nothing to install
├── kbx/                    # host package (stdlib only)
│   ├── cli.py  config.py  modules.py  stage.py  image.py  paths.py (XDG)
│   ├── sandbox.py  git.py  guard.py  agents.py  session.py  clipd.py
│   ├── status.py  dash.py                      # the dashboard (`kbx dash`)
│   └── docker.py
├── kbx_sandbox/            # sandbox package (copied into the image)
│   ├── init.py             # kbx-init: seed, start.sh, supervisor, ready marker
│   ├── seed.py             # kbx-seed
│   ├── clip_put.py         # kbx-clip-put
│   └── bridge.py           # clipboard X11 bridge
├── image/
│   ├── Dockerfile.core     # core; module layers are appended by kbx build
│   └── update-codex-native
├── modules/                # built-in, generic modules
│   ├── clipboard/          codex-chatgpt-auth/  node-toolchain/
│   ├── playwright/         claude-statusline/   codex-statusline/
│   └── codex-reset-fast/
├── examples/
│   ├── config.toml         # annotated example config
│   └── modules/            # templates: claude-defaults, codex-defaults, pi-defaults
├── host/
│   ├── README.md           # Kata install notes from phase 0
│   ├── kata-configuration.toml
│   ├── kbx-firewall.sh
│   ├── kbx-firewall.service
│   └── install-firewall
├── docs/
└── tests/
    ├── unit/               # fake docker; always run
    └── integration/        # real Kata sandbox; KBX_INTEGRATION=1
```

## Order of work and milestones

1. **Phase 0 spike.** Exit when R1–R4 and R7 pass (or R7's fallback is
   chosen), and the smoke container gets internet while being blocked from
   the host.
2. **Repository skeleton** (`kbx` entry script, packages, `./check`, fake
   docker for tests), then **image + init + launcher core** with `kbx shell` only, including config,
   staging, `kbx-seed` (with its tests) and the supervisor, but no modules yet.
   Exit when inner `docker run hello-world` works and readiness works across
   stop/start.
3. **Git in/out**, including the malicious-repo test.
4. **Claude with remote control in dtach.** This is the most important
   milestone: remote control works while detached.
5. **Codex with remote control**, auth, reset-fast and search flags.
6. **Modules**: user-module search path and override, the data-only built-ins
   (codex-chatgpt-auth, claude-statusline, codex-statusline), then
   node-toolchain, playwright, codex-reset-fast, skills, pi, and the
   `examples/` templates.
7. **Clipboard** for all three agents.
8. Tests and docs, the clean-environment test, a licence, and a first tagged
   release.

## Open questions for later

- Should sandboxes get a **read-only** GitHub token (to read private
  dependencies or issues)? It is not planned, because it would add a
  credential to the sandbox.
- Pi's exact settings, instructions and skills paths need checking when
  writing the `pi-defaults` template.
- Final project name (check for collisions with existing tools called `kbx`).
- Which agents' `/copy` works through OSC 52 (see Clipboard).

---

## Appendix: migrating an existing sbx setup

kbx grew out of a Docker Sandboxes setup built from custom kits and a
launcher script (`sbx-agent`). This appendix records where kbx code came
from, and how those settings map to kbx, for anyone migrating a similar
setup. The port is **one-time**: kbx never reads sbx kits.

### Code ported into the repo (copied once, then maintained in kbx)

| kbx file | Ported from | Changes |
| --- | --- | --- |
| `kbx_sandbox/bridge.py` | codex-clipboard kit, `bridge.py` | HTTP/`SBX_HOST_SESSION_ID` source replaced by the file-based push channel; exact `TARGETS` |
| `modules/node-toolchain/build.sh` | node-toolchain kit, `setup.sh` + `packages.conf` | Runs at build time; `packages.conf` becomes module options; apt-lock wait dropped |
| `modules/playwright/build.sh` | `Dockerfile.codex`, `codex-image/chromium` | Unchanged logic |
| `image/update-codex-native` | `codex-image/update-codex-native` | Unchanged |
| `modules/codex-reset-fast/` | codex-defaults kit, `reset-fast.py` | Unchanged logic |
| `modules/claude-statusline/` | claude-statusline kit, `statusline.mjs` | Now data only; `SANDBOX_NAME` still set by kbx |
| `modules/codex-statusline/` | codex-statusline kit (inline Python) | Now data only (TOML seed) |
| `modules/codex-chatgpt-auth/` | codex-defaults kit, `auth.py` | Now data only, using `enforce` |
| `kbx/agents.py` | `sbx-agent` | Remote-control flag insertion, Codex login check, search flags, update commands |
| readiness in `kbx-init` | `lib/wait-startup.py` | Replaced by the boot-id ready marker |

### Setting up personal defaults

A user moving from sbx rebuilds their personal preferences as a
`~/.config/kbx/config.toml` plus user modules (usually kept in their own
dotfiles). Start from `examples/config.toml` and `examples/modules/`. Nothing
personal is added to this repository.

`sbx-agent` variables and their kbx equivalents:

| sbx-agent | kbx |
| --- | --- |
| `SBX_AUTO_UPDATE` | `[launcher] auto_update` |
| `SBX_REMOTE_CONTROL` | `[launcher] remote_control` |
| `SBX_CODEX_SEARCH` | `[launcher] codex_search` |
| `SBX_CODEX_AUTH=chatgpt\|preserve` | module `codex-chatgpt-auth` on/off |
| `SBX_RESET_FAST` | module `codex-reset-fast` |
| `SBX_STATUSLINE` | modules `claude-statusline`, `codex-statusline` |
| `SBX_AGENT_DEFAULTS` | user module `claude-defaults` |
| `SBX_CLIPBOARD` | module `clipboard` |
| `SBX_NODE_TOOLCHAIN`, `SBX_NODE_VERSION` | module `node-toolchain`, option `version` |
| `SBX_SHARED_SKILLS` | `[launcher] shared_skills` |
| `SBX_PRESET`, `SBX_RELEASE_MODE` | not carried over (non-goals) |
