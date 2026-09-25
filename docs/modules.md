# Modules

A module is a directory with up to four optional parts. Each part is handled
by one shared, tested piece of kbx, so modules hold data and small scripts,
not their own merge logic.

```
<name>/
├── module.toml   # description, default on/off, options, seed overrides
├── build.sh      # image layer: install software        → kbx build
├── build/        # files only build.sh needs (never staged)
├── home/         # declarative seed tree for /home/agent → every start
├── service       # long-running process, supervised      → every start
└── start.sh      # one-shot per boot; escape hatch       → every start
```

| Part | Runs | Applies after a change | Use it for |
| --- | --- | --- | --- |
| `build.sh` | `kbx build`, as root, one Dockerfile `RUN` layer per module | `kbx build && kbx recreate` | apt/npm packages, binaries, anything in `/usr` or `/opt` |
| `home/` | every boot, as `agent`, via `kbx-seed` | next start | default settings, instruction files, scripts in `$HOME` |
| `service` | every boot, supervised by `kbx-init` | next start | daemons |
| `start.sh` | every boot, after seeding, before services | next start | imperative fix-ups that aren't "add a default" |

**Software is installed at build time, never at start.** Starts are fast and
work offline. Everything that runs at start reads the staged copy of the
module, so seed, service and config changes show up at the next start without
a rebuild. The home volume is never touched by a rebuild.

Search path: user modules in `$XDG_CONFIG_HOME/kbx/modules/`, then built-ins.
A user module with a built-in's name **replaces** it entirely. There are no
dependencies between modules: build layers run in name order on top of the
core image, and a module may depend only on core (Node, Python, Docker, git,
`/usr/local/lib/kbx/install-node`, …).

## module.toml

```toml
description = "Claude Code status line"   # required
default = false                           # required: on unless config says otherwise

[options]                                 # typed; overridable in [options.<name>]
version = { default = "26", pattern = '^([0-9]+|[0-9]+\.[0-9]+\.[0-9]+)?$', description = "…" }
mode = { default = "a", enum = ["a", "b"] }
verbose = { default = false }             # a boolean option

[service]
user = "agent"                            # or "root"

[start]
user = "agent"                            # or "root"
before_launch = ["codex"]                 # also rerun before each launch of these agents

[seed]
enforce = [".codex/config.toml:forced_login_method"]   # always set
copy = [".config/tool/state.json"]                     # whole file, even if JSON/TOML

[env]                                     # passed to agent sessions and services
PLAYWRIGHT_BROWSERS_PATH = "/opt/playwright-browsers"
```

Options reach `build.sh` as build args and `start.sh`/`service` as environment
variables named `KBX_OPT_<MODULE>_<OPTION>` (upper case, `-` → `_`). The host
validates them before anything runs. Scripts also get `KBX_MODULE` and
`KBX_MODULE_DIR` (their own directory, e.g. to reach files in `build/`).

## Seeds: `home/` and `kbx-seed`

`kbx-seed` applies every enabled module's `home/` tree to `/home/agent` at each
start, merging by file type:

| File | Merge unit |
| --- | --- |
| `*.json` | Each key, recursively into objects. Arrays and scalars are single values |
| `*.toml` | Each key, recursively into tables; written with tomlkit, so comments and formatting survive |
| anything else, and `[seed] copy` paths | The whole file, keeping its mode bits |

It records a hash of every value it wrote (`~/.local/state/kbx/seeds.json`) and
does a three-way merge:

| Current state in `$HOME` | Action |
| --- | --- |
| absent, never seeded | write the default |
| equal to what kbx last seeded, and the default changed | update it (you never touched it) |
| different from what kbx last seeded | leave it; it's yours |
| absent, but previously seeded | leave it absent; you deleted it |
| listed in `enforce` | always set |

So updated defaults reach existing sandboxes, and a key you delete stays
deleted. A TOML file containing only comments seeds nothing.

Safety: invalid JSON/TOML (or a non-object root) fails that module's seed
without writing anything; writes are atomic; symlinks are never followed out
of `/home/agent`.

```sh
kbx seed --dry-run      # diff of what would change
kbx seed --status       # every unit: default, update pending, user-owned, deleted, enforced
kbx seed --reset NAME   # restore a module's defaults (asks first)
```

Seeds under an agent's directory (`.claude/`, `.codex/`, `.pi/`) are also
reapplied right before that agent launches.

## Services

`kbx-init` runs each enabled module's `service` in the foreground under a small
supervisor, as `[service] user`, with output in `/var/log/kbx/<name>.log`. It
restarts a service with exponential backoff and gives up (state `failed`) after
more than 5 exits in a minute. `dockerd` is supervised the same way. Status:
`kbx logs`.

## Built-in modules

| Module | Default | Parts |
| --- | --- | --- |
| `clipboard` | on | `build.sh`: Xvfb, python3-xlib, xclip. `service`: Xvfb `:0` + the clipboard bridge. Host side is `kbx`'s clipboard watcher |
| `codex-chatgpt-auth` | on | Seeds `forced_login_method = "chatgpt"` and `model_provider = "openai"` (enforced). Needed for Codex remote control; turn off to keep API-key auth |
| `node-toolchain` | off | `build.sh`: a chosen Node (`version`), extra `apt` and global `npm` packages |
| `playwright` | off | `build.sh`: Playwright + Chromium + a `chromium` wrapper (~500 MB). `[env]` sets the browsers path |
| `claude-statusline` | off | `~/.claude/statusline.mjs` and the `statusLine` setting |
| `codex-statusline` | off | `tui.status_line = ["context-used", "five-hour-limit", "weekly-limit"]` |
| `codex-reset-fast` | off | `start.sh`: remove a persisted `service_tier = "fast"` at every start and before each Codex launch |

Notes worth knowing:

- **Codex Fast mode persists.** `/fast` writes `service_tier = "fast"` to the
  root of `config.toml`, so every later session starts in Fast mode and uses
  plan quota at the Fast rate. `codex-reset-fast` removes only that line (root
  or `[profiles.*]`), re-parsing to make sure nothing else changes; `flex`
  stays. To remove the toggle entirely instead, seed `features.fast_mode = false`.
- **Node** is installed from nodejs.org with its SHA-256 verified, unpacked to
  `/opt/node/<version>` and linked into `/usr/local/bin`. Global npm packages
  from the module go to `/usr/local`, never into Node's own directory (which
  the next upgrade deletes). The agent's own `npm i -g` goes to `~/.local`.
- **Status line quota fields** (`rate_limits`) exist only for Pro/Max plans and
  only after the first response of a session; the Claude status line omits
  what is missing.

## Clipboard (image paste)

One X11 clipboard inside the sandbox serves all three agents: Claude and pi
run `xclip`, Codex reads X11 directly. The sandbox never connects to the host,
so the host pushes:

- While any session is attached, `kbx` runs a watcher on the host
  (`wl-paste --watch` on Wayland; `clipnotify` + `xclip` on X11, which also
  covers GNOME through XWayland). On every clipboard change it pushes a PNG
  (up to 64 MiB) into the sandbox with `kbx-clip-put`, or a **clear** when the
  clipboard holds no image, so a stale image never gets pasted after you copy
  text. It stops when the last session detaches.
- In the sandbox, the bridge owns `CLIPBOARD` on Xvfb `:0` while an image is
  present and advertises exactly `TARGETS`, `TIMESTAMP` and `image/png`.
- Ctrl+V pastes images in all three agents; your terminal or multiplexer must
  pass Ctrl+V through. Text paste is still the terminal's own paste.
- Host tools: `wl-clipboard` (Wayland) or `xclip` + `clipnotify` (X11).
  Without them kbx warns once and runs without image paste.
- **Privacy:** while you are attached, every image you copy on the host is
  pushed into that sandbox. Text never is.
- **Copying out** (`/copy`) works when the agent emits OSC 52, which passes
  through dtach to your terminal. Copies an agent writes only to the X11
  clipboard stay in the sandbox; forwarding them is not implemented.

## Writing a user module

1. `mkdir -p ~/.config/kbx/modules/my-defaults` (or copy a template from
   [`examples/modules/`](../examples/modules/)).
2. Write `module.toml` with at least `description` and `default`.
3. Add only the parts you need; make scripts executable.
4. Enable it in `config.toml` if `default = false`.
5. `kbx check` validates TOML, options, seed conflicts (two modules seeding
   the same key or file), `enforce`/`copy` entries, executable bits, runs
   `shellcheck` when installed, and warns about files that look like
   credentials. The stage must never hold secrets.
6. Launch. If the module has a `build.sh`, kbx tells you to
   `kbx build && kbx recreate`.

Example: a personal Claude default plus a helper script.

```
~/.config/kbx/modules/my-claude/
├── module.toml                 description = "My Claude defaults"  default = true
└── home/
    ├── .claude/settings.json   {"model": "opus", "outputStyle": "Concise"}
    ├── .claude/CLAUDE.md       instructions for every session
    └── .local/bin/review       an executable script
```
