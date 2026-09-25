# Migrating from Docker Sandboxes (sbx) kits

kbx grew out of a Docker Sandboxes setup built from custom kits and a launcher
script. It never reads sbx kits: migrating means rebuilding your preferences
once as a kbx config plus user modules, usually kept in your dotfiles.

## What changes

| sbx | kbx |
| --- | --- |
| Host-mounted workspace | The same by default (mount mode, at the same path), plus a host-side guard against planted git hooks and config |
| Clone mode + git-daemon | Clone mode: a private clone; `kbx fetch`/`kbx sync` via bundles |
| Egress proxy, per-domain policy, credential injection | Open internet; logins live in the sandbox; nothing injected |
| Kits applied at create time (YAML specs, templating) | Modules: build layer, declarative seeds, services, `start.sh`; seeds and services apply at every start |
| `sbx run` | `kbx claude\|codex\|pi`, attached through dtach (detach with `Ctrl-\`) |
| One sandbox per agent and project | One sandbox per project; all three agents share it |
| Clipboard fetched from the host on demand | Host pushes images while attached; one X11 clipboard serves all agents |

## Launcher variables

| sbx launcher | kbx |
| --- | --- |
| `SBX_AUTO_UPDATE` | `[launcher] auto_update` |
| `SBX_REMOTE_CONTROL` | `[launcher] remote_control` |
| `SBX_CODEX_SEARCH` | `[launcher] codex_search` |
| `SBX_CODEX_AUTH=chatgpt\|preserve` | module `codex-chatgpt-auth` on/off |
| `SBX_RESET_FAST` | module `codex-reset-fast` |
| `SBX_STATUSLINE` | modules `claude-statusline`, `codex-statusline` |
| `SBX_AGENT_DEFAULTS` | a user module (start from `examples/modules/claude-defaults`) |
| `SBX_CLIPBOARD` | module `clipboard` |
| `SBX_NODE_TOOLCHAIN`, `SBX_NODE_VERSION` | module `node-toolchain`, option `version` |
| `SBX_SHARED_SKILLS` | `[launcher] shared_skills`, `[skills] sources` |
| `SBX_PRESET`, `SBX_RELEASE_MODE` | not carried over |

Each `[launcher]` setting can still be overridden per run, e.g.
`KBX_REMOTE_CONTROL=false kbx claude`.

## Kits

| Kit | kbx |
| --- | --- |
| claude-defaults (`settings.json` keys "only if absent", `CLAUDE.md` "only if missing") | A user module with `home/.claude/settings.json` and `home/.claude/CLAUDE.md`. The three-way merge also lets updated defaults reach sandboxes you never changed |
| codex-defaults (auth, reset-fast) | Built-ins `codex-chatgpt-auth` (on) and `codex-reset-fast` (off) |
| codex-statusline, claude-statusline | Built-ins of the same names (off) |
| node-toolchain (`packages.conf`) | Built-in `node-toolchain`: `version`, `apt`, `npm` options; installed at build time, not every start |
| codex-clipboard | Built-in `clipboard`, now for Claude and pi too |
| shared-skills | `[skills] sources`; skills are staged, not imported into a store |
| Playwright in the image | Built-in `playwright` (off) |

## Steps

1. Set up the host ([host-setup.md](host-setup.md)) and `kbx build`.
2. `cp examples/config.toml ~/.config/kbx/config.toml`; enable the modules you
   used (`claude-statusline`, `codex-reset-fast`, `node-toolchain`, …).
3. For personal defaults, copy templates from `examples/modules/` into
   `~/.config/kbx/modules/`, fill them in, enable them, `kbx check`.
4. Point `[skills] sources` at your skill directories.
5. Launch in a project; log in once per sandbox (`/login` in Claude,
   `codex login --device-auth` in `kbx shell`). For Codex remote control,
   enable MFA on the ChatGPT account first (enrollment otherwise fails with
   HTTP 403 `Multi-factor authentication required`), then
   `codex remote-control pair` inside once.

Personal settings (model choices, instruction files, skills) never go into the
kbx repository.
