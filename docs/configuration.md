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
| logs (clipboard watcher) | `$XDG_DATA_HOME/kbx/log/` |
| runtime (clipd pidfiles) | `$XDG_RUNTIME_DIR/kbx/` |
| built-in modules | `<checkout>/modules/`, found relative to the resolved `bin/kbx` |

## `[launcher]`

Each can be overridden for one run with `KBX_<NAME>`, e.g.
`KBX_REMOTE_CONTROL=false kbx claude` or `KBX_DNS=8.8.8.8,1.1.1.1`.

| Setting | Default | Effect |
| --- | --- | --- |
| `auto_update` | `true` | Update the launched agent before starting it (never when reattaching) |
| `remote_control` | `true` | Claude: `--remote-control`. Codex: login check + `codex remote-control start` |
| `codex_search` | `true` | Codex: `--search --enable standalone_web_search` |
| `shared_skills` | `true` | Stage `[skills] sources` and link them as `~/.agents/skills` and `~/.claude/skills` |
| `detach_key` | `"^\\"` | dtach detach key (`Ctrl-\`). One character, `^X` notation, or `"none"` |
| `memory` / `cpus` | `"8g"` / `4` | VM size, applied at create (`kbx recreate` to change) |
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
| `runtime.privileges` | `"caps"` | Explicit capabilities for the inner dockerd. `"privileged"` does not work under Kata 4 (its agent cannot recreate the host device list), but does under `runc` (risk R1) |
| `runtime.docker_storage` | `"loop"` | A sparse ext4 image on the volume, loop-mounted as dockerd's data root (overlayfs cannot use Kata's virtio-fs volume as its upper layer). `"volume"` uses the volume directly, which works under `runc` (risk R2) |

## `[skills]`

```toml
[skills]
sources = ["~/.agents/skills", "~/team/skills"]
```

Each entry of each source directory is a skill (usually a directory with
`SKILL.md`). Missing directories are skipped; on a name clash the first source
wins, with a warning. Skills are copied into the stage with symlinks
dereferenced, so edits show up in running sandboxes at the next launch.

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

## Environment variables

| Variable | Effect |
| --- | --- |
| `KBX_<LAUNCHER_SETTING>` | Per-run override of a `[launcher]` setting |
| `KBX_IMAGE`, `KBX_RUNTIME` | Per-run image and runtime name |
| `KBX_DEBUG=1` | Tracebacks for errors, and `claude --debug` |
| `KBX_DOCKER` | Path or name of the docker CLI |
| `KBX_UNSAFE_NO_FIREWALL_CHECK=1` | Skip the launch-time firewall check. Only for development with `runc` on a machine without the firewall |

## What applies when

| Change | Takes effect |
| --- | --- |
| Seeds (`home/`), services, `start.sh`, skills, options used at start | Next sandbox start; agent-specific seeds before each launch of that agent |
| A module's `build.sh` or build options, enabling a module with `build.sh` | `kbx build && kbx recreate` (kbx warns at launch) |
| `memory`, `cpus`, `dns`, runtime, network | `kbx recreate` |
| `auto_update`, `remote_control`, `detach_key`, `debug` | Next launch |
