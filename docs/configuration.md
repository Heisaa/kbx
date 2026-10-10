# Configuration

All settings live in `$XDG_CONFIG_HOME/kbx/config.toml` (usually
`~/.config/kbx/config.toml`). The file is optional; every setting has a safe
default. Unknown keys are errors, so a typo never silently falls back to a
default. Projects cannot configure kbx: project files are untrusted.

Start from the annotated [`examples/config.toml`](../examples/config.toml) and
run `kbx check` after editing.

## Paths

| What | Where |
| --- | --- |
| config | `$XDG_CONFIG_HOME/kbx/config.toml` (`~/.config/kbx/`) |
| user modules | `$XDG_CONFIG_HOME/kbx/modules/<name>/` |
| stage (mounted read-only into sandboxes) | `$XDG_DATA_HOME/kbx/stage/` (`~/.local/share/kbx/`) |
| logs (clipboard watcher, guard) | `$XDG_DATA_HOME/kbx/log/` |
| guard baselines, trusted copies, quarantine | `$XDG_DATA_HOME/kbx/guard/<sandbox>/` |
| runtime (clipd and guard pidfiles) | `$XDG_RUNTIME_DIR/kbx/` |
| built-in modules | `<checkout>/modules/`, found relative to the resolved `bin/kbx` |

## `[launcher]`

Each can be overridden for one run with `KBX_<NAME>`, e.g.
`KBX_REMOTE_CONTROL=false kbx claude` or `KBX_DNS=8.8.8.8,1.1.1.1`.

| Setting | Default | Effect |
| --- | --- | --- |
| `auto_update` | `true` | Update the launched agent before starting it (never when reattaching) |
| `remote_control` | `true` | Claude: `--remote-control`. Codex: login check + `codex remote-control start`, and the project is registered with the daemon so remote threads open in it |
| `skip_onboarding` | `true` | Before Claude/Codex launch: mark first-run screens done and trust the working directory (`kbx-onboard`). Login stays yours |
| `notify` | `"detached"` | Desktop notification (`notify-send`) when an agent finishes a turn or waits for an approval: `"detached"` (only when no terminal shows that session), `"always"`, or `"off"`. Needs the `notify` module |
| `idle_stop` | `"5m"` | Stop the sandbox after this long with no agent session and no interactive shell (`"90m"`, `"3600"` seconds, or `"off"`). The short default stops a sandbox soon after you close its last session (a detached session keeps it running) and leaves time to open another. Stopping ends the inner Docker's containers too, and Codex remote control started with `kbx rc-start` while no Codex session is open; set `"off"` if you rely on that |
| `codex_search` | `true` | Codex: `--search --enable standalone_web_search` |
| `shared_skills` | `true` | Stage `[skills] sources` and link them as `~/.agents/skills` and `~/.claude/skills` |
| `detach_key` | `"^\\"` | dtach detach key (`Ctrl-\`). One character, `^X` notation, or `"none"` |
| `memory` / `cpus` | `"8g"` / `4` | VM size, applied at create (`kbx recreate` to change) |
| `memory_budget` | `"auto"` | Auto: before a VM starts, compare its limit plus base VM RAM with the host RAM free now, keeping at least 4 GiB or 20% (whichever is larger) for the desktop. kbx refuses only if the VM could not boot, and warns if it could not grow to its limit. A size such as `"20g"` also caps the total stored limits of all active VMs (the worst case); `"off"` disables the RAM check |
| `shm_budget` | `"auto"` | The same check against free `/dev/shm`, keeping at least 512 MiB or 10% of the mount free. Skipped when the standard Kata config selects Cloud Hypervisor. A size caps the larger of the total VM limits and current `/dev/shm` use plus the new VM; `"off"` disables this check |
| `dns` | `["1.1.1.1", "9.9.9.9"]` | Resolvers passed to the container |
| `debug` | `false` | `claude --debug`. `KBX_DEBUG=1` also prints tracebacks for kbx errors |
| `docker_disk` | `"64g"` | Size of the sparse ext4 image backing the inner Docker (grows, never shrinks) |

## `[network]`

| Setting | Default | |
| --- | --- | --- |
| `name` | `"kbx"` | Docker network kbx creates on first launch |
| `subnet` | `"172.30.0.0/24"` | IPv4 only. The gateway (first address) is what the firewall check probes |
| `bridge` | `"br-kbx"` | Must match `/etc/kbx/firewall.conf` (`host/install-firewall --bridge`) |

## `[image]`, `[runtime]`

| Setting | Default | |
| --- | --- | --- |
| `image.name` | `"kbx-agent"` | Image `kbx build` produces and sandboxes use (`KBX_IMAGE` overrides) |
| `runtime.name` | `"kata"` | Docker runtime name from `daemon.json` (`KBX_RUNTIME` overrides). `runc` works for development but is **not isolated** |
| `runtime.memory_overhead` | `"2g"` | Base VM RAM added to each container's stored Docker memory limit for admission. Raise this for a custom Kata configuration/runtime with a larger base VM. kbx also reads `default_memory` from the standard Kata config and uses the larger value |
| `runtime.privileges` | `"caps"` | Explicit capabilities for the inner dockerd. `"privileged"` does not work under Kata 4 (its agent cannot recreate the host device list), but does under `runc` (risk R1) |
| `runtime.docker_storage` | `"loop"` | A sparse ext4 image on the volume, loop-mounted as dockerd's data root (overlayfs cannot use Kata's virtio-fs volume as its upper layer). `"volume"` uses the volume directly, which works under `runc` (risk R2) |

Starts across projects are serialized through `$XDG_RUNTIME_DIR/kbx/start.lock`
(or the normal runtime directory fallback). Guest memory is allocated lazily, so
running VMs are charged by what they have already touched, which host free RAM
and `/dev/shm` already reflect, not by their limits. Reattaching to a running
sandbox runs no check. An explicit budget size counts every running, paused and
restarting VM container on the daemon, including VMs outside kbx, by its stored
Docker memory limit; VM containers without a positive limit then block
admission. Native `runc` containers are never counted as VMs.

Admission is supported for a local Docker Unix socket. The lock coordinates
launches by the same user with the same kbx runtime directory; direct Docker
starts, other users, and later allocations by unrelated host applications are
outside that coordination. Disabling both budgets also disables admission.
See [host memory setup](host-setup.md#vm-memory-and-the-desktop) for hypervisor
selection and verification.

## `[workspace]`

| Setting | Default | |
| --- | --- | --- |
| `mode` | `"mount"` | `"mount"`: the checkout is mounted at its own path; you and the agent share it. `"clone"`: a private clone, git crosses as bundles (`kbx fetch`/`sync`). Applied at create; `kbx recreate` switches. `KBX_WORKSPACE` overrides |
| `guard` | `true` | Mount mode: the host-side guard that reverts agent changes to git hooks, git config and `protect` paths, and pauses the sandbox. `KBX_GUARD` overrides |
| `protect` | hook-framework and editor files | Paths relative to the project (files or directories) guarded like hooks. The default list: `.husky`, `.githooks`, `.lefthook`, `.pre-commit-config.yaml`/`.yml`, `lefthook.yml`/`.yaml` and their dotted and `-local` variants, `.vscode/settings.json`, `.vscode/tasks.json`. Setting it replaces the list. Takes effect at the next start |
| `private` | `["target", ".venv"]` | Mount mode: build directories the sandbox keeps to itself, so builds on each side do not invalidate the other's. A directory from the sandbox's home volume is mounted over each, inside the VM only; the host keeps its own. Applies when the directory exists or its tool's file is beside it (`Cargo.toml` for `target`; `pyproject.toml`, `requirements.txt`, `uv.lock` and similar for `.venv`/`venv`; `package.json` for `node_modules`). Paths may be nested (`"backend/target"`). Checked at every launch |

See [git-workflow.md](git-workflow.md) for what each mode means in practice.

## `[skills]`

```toml
[skills]
sources = ["~/.agents/skills", "~/team/skills"]
```

Each entry of each source directory is a skill (usually a directory with
`SKILL.md`). Missing directories are skipped; on a name clash the first source
wins, with a warning. Skills are copied into the stage with symlinks
dereferenced, so edits show up in running sandboxes at the next launch.

## `[host]`

For `kbx host`, Claude Code on the host. The rest of the policy is fixed; see
[the README](../README.md#when-it-has-to-run-on-the-host-kbx-host).

| Setting | Default | |
| --- | --- | --- |
| `allow_read` | toolchain directories: `~/.local/bin`, `~/.local/lib`, `~/.local/share/mise`, `~/.cargo/bin`, `~/.rustup`, `~/.nvm`, `~/.pyenv`, `~/.volta`, `~/.bun/bin`, `~/.deno/bin`, `~/go/bin`, `~/.sdkman/candidates` | What commands may read in your home besides the project. Setting it replaces the list. A tool whose link points elsewhere in your home (pipx, `~/.local/bin/claude`) needs that target too |
| `allow_write` | `[]` | Writable besides the project |
| `allowed_domains` | `[]` | Network for commands (`"pypi.org"`, `"*.npmjs.org"`); none by default |
| `channel` | `"stable"` | Release channel of kbx's own Claude Code: `"stable"` or `"latest"`. Checked at each launch when `[launcher] auto_update` is on |
| `env` | `[]` | Environment variables passed through besides `PATH`, `HOME`, `USER`, `LOGNAME`, `SHELL`, `TERM`, `COLORTERM`, `TERM_PROGRAM`, `LANG`, `LC_ALL`, `LC_CTYPE`, `TZ` (e.g. `HTTPS_PROXY`) |

The host session runs kbx's own Claude Code from
`$XDG_DATA_HOME/kbx/host-claude-bin/<version>/claude` (downloaded on first use,
never on your `PATH`), keeps its Claude state in
`$XDG_DATA_HOME/kbx/host-claude/` (log in there once) and its generated
settings in `$XDG_RUNTIME_DIR/kbx/host-settings-<sandbox>.json`. A Claude Code
installed on the host is not used.

## `[modules]` and `[options.<module>]`

```toml
[modules]
playwright = true          # override the module's own default
my-claude-defaults = true  # a user module

[options.node-toolchain]
version = "26"
npm = "typescript pnpm"
```

`kbx check` lists every module, whether it is on, where it comes from and which
parts it has. Options are validated against the module's `pattern`/`enum`
before anything runs. See [modules.md](modules.md).

The built-in `agent-permissions` module is enabled by default. It seeds Codex's
`approval_policy = "never"` and `sandbox_mode = "danger-full-access"`, and
Claude's `permissions.defaultMode = "bypassPermissions"`. The VM provides
isolation. These are defaults, so existing explicit settings and later edits
inside the sandbox are preserved. They apply at sandbox start and before a
new agent launch; reattaching an existing session does not change its mode.
`kbx host` uses its own restricted settings.

To supply permission defaults through your own module, disable
`agent-permissions` under `[modules]` first to avoid seed conflicts. Disabling
it stops seeding; it does not remove settings already written to a sandbox.
To apply the full-access defaults to an existing sandbox that has explicit
permission settings, use `kbx seed --reset agent-permissions --yes`, then
start a new agent session.

## Environment variables

| Variable | Effect |
| --- | --- |
| `KBX_<LAUNCHER_SETTING>` | Per-run override of a `[launcher]` setting |
| `KBX_IMAGE`, `KBX_RUNTIME` | Per-run image and runtime name |
| `KBX_WORKSPACE`, `KBX_GUARD` | Per-run `[workspace] mode` and `guard` (the mode only matters when the sandbox is created) |
| `KBX_DEBUG=1` | Tracebacks for errors, and `claude --debug` |
| `KBX_DOCKER` | Path or name of the docker CLI |
| `KBX_UNSAFE_NO_FIREWALL_CHECK=1` | Skip the launch-time firewall check. Only for development with `runc` on a machine without the firewall |

## What applies when

| Change | Takes effect |
| --- | --- |
| Seeds (`home/`), services, `start.sh`, skills, options used at start | Next sandbox start; agent-specific seeds before each launch of that agent |
| A module's `build.sh` or build options, enabling a module with `build.sh` | `kbx build && kbx recreate` (kbx warns at launch) |
| `memory`, `cpus`, `dns`, runtime, network, `[workspace] mode` | `kbx recreate` |
| `[workspace] protect` | Next sandbox start |
| `auto_update`, `remote_control`, `detach_key`, `debug` | Next launch |
