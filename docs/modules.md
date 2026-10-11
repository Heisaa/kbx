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
| `notify` | on | Seeds Claude Code hooks (`UserPromptSubmit`, `PostToolUse`, `Notification`, `Stop`, `SessionEnd`) and Codex's `notify` to run `kbx-notify`, which records what each session does for the host's notifications and the dashboard |
| `node-toolchain` | off | `build.sh`: a chosen Node (`version`), extra `apt` and global `npm` packages |
| `playwright` | off | `build.sh`: Playwright + Chromium + a `chromium` wrapper (~500 MB). `[env]` sets the browsers path |
| `claude-statusline` | off | `~/.claude/statusline.mjs` and the `statusLine` setting |
| `codex-statusline` | off | `tui.status_line = ["context-used", "five-hour-limit", "weekly-limit"]` |
| `codex-reset-fast` | off | `start.sh`: remove a persisted `service_tier = "fast"` at every start and before each Codex launch |
| `voice` | off | `build.sh`: PulseAudio, alsa-utils and a fixed ALSA pulse plugin (about 200 MB). `start.sh` + `service`: a PulseAudio server whose microphone and speaker the host connects while attached. Host side is `kbx`'s voice link |

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

## Clipboard (image paste and text copy)

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
  Without them kbx warns once and runs without clipboard sync.
- **Privacy:** while you are attached, every image you copy on the host is
  pushed into that sandbox. Text never is.
- **Copying out:** new text copies to the sandbox's X11 clipboard (including
  Codex's mouse selection and `/copy`) are forwarded to the host clipboard
  with `wl-copy` or `xclip`. A host-initiated `docker exec` stream listens for
  selection changes while any session is attached; the sandbox never opens
  a connection to the host. Copies are UTF-8 text, limited to 1 MiB, and are
  never logged. Existing clipboard contents are not replayed on reattach.
  Agents that emit OSC 52 can also copy through dtach to your terminal.
  Text paste still uses the terminal's own paste.
- After upgrading the clipboard code, run `kbx build && kbx recreate` in each
  project to install the updated helpers. `recreate` keeps the home and Docker
  volumes but stops running sessions. Close all attachments for that sandbox
  and allow the host clipboard watcher to exit before reattaching, so it
  loads the updated host code too.

## Voice input (Claude and Codex)

The `voice` module is off by default. Turn it on with `voice = true` under
`[modules]`, then `kbx build && kbx recreate`. Claude's `/voice` (hold Space to
talk) and Codex's `/voice` (a spoken conversation; Codex answers out loud) then
use the microphone and speakers of the host you are attached from.

The VM has no sound hardware, and the sandbox never connects to the host:

- In the sandbox, a PulseAudio server is the default ALSA device. Its
  microphone is fed, and its speaker drained, through FIFOs in
  `/run/kbx-voice`. Claude records with `arecord`; Codex's voice helper opens
  ALSA directly.
- While any session is attached, `kbx` keeps one `docker exec` stream open to
  the module's `link.py`, which reports when a program starts or stops
  recording or playing. The host runs `pacat` (PulseAudio or PipeWire through
  `pipewire-pulse`), or `arecord`/`aplay`, **only while** a program in the
  sandbox records or plays. Your desktop's microphone indicator shows it.
  Audio is raw 48 kHz mono PCM.
- Host tools: `pacat` (`pulseaudio-utils`; `libpulse` on Arch) or
  `alsa-utils`. Without them kbx warns once and runs without voice.
- **Privacy:** while you are attached, the agent can record from your
  microphone whenever it likes, not only when you ask it to listen (it is root
  in its VM and controls its own audio server). kbx sends a desktop
  notification when recording starts after 10 minutes without any, and logs
  every start and stop in `voiced-<sandbox>.log` (`kbx logs` prints the path).
  Nothing is recorded while you are detached. Leave the module off if that is
  not acceptable.
- Use headphones with Codex: its echo cancellation cannot account for the
  extra playback delay through the host, so it may hear its own voice.
- Debian's ALSA pulse plugin reports a transient error while a new stream
  waits for its first timing update (a sign bug in `pulse_delay()`), which
  Codex treats as a lost speaker. The module builds the plugin from the
  pinned upstream source with that one comparison fixed.

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
