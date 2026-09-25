# Module templates

Starting points for personal defaults. kbx never loads these directly.

```sh
cp -r examples/modules/claude-defaults ~/.config/kbx/modules/my-claude-defaults
$EDITOR ~/.config/kbx/modules/my-claude-defaults/home/.claude/settings.json
```

Then enable it in `~/.config/kbx/config.toml`:

```toml
[modules]
my-claude-defaults = true
```

Run `kbx check`, then launch. Seeds apply at the next sandbox start (and
agent-specific ones before each launch of that agent). Values you change inside
a sandbox stay yours; see `docs/modules.md` for the three-way merge.

| Template | Files |
| --- | --- |
| `claude-defaults` | `~/.claude/settings.json` (`model`), empty `~/.claude/CLAUDE.md` |
| `codex-defaults` | `~/.codex/config.toml` (commented examples), empty `~/.codex/AGENTS.md` |
| `pi-defaults` | `~/.pi/agent/settings.json`, empty `~/.pi/agent/AGENTS.md` |

An empty instruction file is still seeded as a file. Delete it from the
module if you do not want one.
