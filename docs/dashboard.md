# The dashboard: `kbx dash`

`kbx dash`, or plain `kbx` on a terminal, shows every kbx sandbox on the host
and runs kbx commands on the one you select. It needs nothing beyond Python's
standard library. The design and its reasons are in
[PLAN.md](../PLAN.md#dashboard-kbx-dash).

```
 kbx dash · 3 sandboxes · updated 1s ago
  PROJECT        STATE     MODE   GUARD      SESSIONS         CPU     MEM       FLAGS
▶ ~/Projects/kbx running   mount  watching   claude:waiting,codex:done   4.21%   1.3GiB
  ~/code/webapp  paused    mount  ALERT      -                -       -
  ~/code/api     running   clone  -          pi               0.50%   640MiB    stale
────────────────────────────────────────────────────────────────────────────────────
kbx-kbx-1a2b3c4d · ~/Projects/kbx · mount mode · running · details 4s old
Guard
  ✓ watching the checkout's git hooks, config and protected files
Health
  ✓ seeds, start.sh and services ok
Git (mount mode)
  3 commit(s) on HEAD since the sandbox started
    a1b2c3d Add dashboard
  2 uncommitted path(s) in the checkout
Agents
  claude  2.3.1   2.3.4 published; u updates
          login: ✓ claude.ai (max)
  codex   0.61.0   current
          login: ✗ not logged in; sign in when Codex starts
          remote control: App server daemon is running
Image and config
  ✓ matches the current image and config
```

## The list

| Column | Meaning |
| --- | --- |
| STATE | the container's state. `paused` usually means the guard stopped the agent |
| MODE | `mount` (your checkout) or `clone` (a private clone) |
| GUARD | `watching`; **`ALERT`**: the guard neutralized a change, see the details; **`UNGUARDED`**: a running mount-mode sandbox with no guard process, press `S`; `off`: disabled in the config |
| SESSIONS | the agents running in dtach sessions, with what each is doing when the `notify` module reports it: `working`, `waiting` (for an approval or answer; the row is highlighted) or `done` (its turn ended). Codex reports only the end of a turn and approval requests, so it can show `done` while it works on your next prompt |
| CPU, MEM | from `docker stats`, running sandboxes only |
| FLAGS | `stale`: recreate to pick up the current image or mode; `orphan`: the project directory is gone; `health`: a module failed |

## The details

For the selected sandbox:

- **Guard:** with an alert, the same report as `kbx resume`, diffs included.
- **Health:** failed seeds, `start.sh` and services from the last start.
- **Git:** mount mode: commits on `HEAD` since the sandbox started and
  uncommitted paths. Clone mode: unfetched branches, uncommitted changes and
  stashes in the sandbox clone.
- **Agents:** installed versions and whether each agent is logged in. `U` looks up the latest published versions
  on the npm registry (a request from the host, only when you press it). Also
  the Codex remote-control daemon's status, and a note when it still runs an
  older Codex.
- **Image and config:** the image lags the enabled modules (`kbx build`), the
  container predates the current image, or its mode differs from the config
  (`kbx recreate`).

The details refresh every 15 seconds while selected; `g` refreshes now.

## Keys

| Key | Does |
| --- | --- |
| Enter | attach to the running agent, or ask which agent to start |
| `c` `x` `p` | `kbx claude` / `kbx codex` / `kbx pi` |
| `s` | `kbx shell` |
| `S` | `kbx start` (starts a missing guard too) |
| `t` | `kbx stop` (asks first) |
| `r` | `kbx recreate` (asks first) |
| `D` | `kbx rm` (asks, and lists unfetched work) |
| `R` / `A` | `kbx resume` / `kbx resume --accept` |
| `f` / `y` | `kbx fetch` / `kbx sync` (clone mode) |
| `u` / `U` | `kbx update` / check for newer agent versions |
| `d` | `kbx diff` in your pager |
| `l` | `kbx logs` |
| `b` | `kbx build` |
| ↑ ↓ `j` `k`, PgUp PgDn | select, scroll the details |
| Tab | full-screen details, and back (Esc too) |
| ← → | in full-screen details: the previous / next sandbox |
| `?` / `q` | help / quit |

Commands run exactly as they do on the command line, in the sandbox's project
directory, with the terminal handed over. Attach and shell return to the
dashboard when you detach (`Ctrl-\`). Commands that print and exit wait for
Enter. For an orphan, whose project directory is gone, only stop and remove
work, and the dashboard runs them itself.

## Small screens and phones

The layout follows the terminal's width, down to 24 columns, so it works in
Termux on a phone:

- Below 60 columns each sandbox takes two lines: the project, then its state,
  mode, guard, sessions and flags.
- Between 60 columns and the full table, columns are dropped in this order:
  memory, CPU, mode, then the project path's full width. Flags, sessions,
  guard and state stay longest.
- Details, help and prompts wrap instead of being cut, so a question always
  shows its `[y/N]`.
- Tab shows the details on the whole screen. There, ↑ ↓ scroll, which is also
  what a swipe sends in Termux, and ← → switch sandboxes. Tab, Esc, the
  arrows, PgUp and PgDn are all on Termux's default extra-keys row.

```
 kbx dash · 4 sandboxes
▶ ~/Projects/kbx
    running · mount · watching ·
    claude,codex · 4.21% · 1.3GiB
  ~/code/webapp
    paused · mount · ALERT
  ~/code/api
    running · clone · pi · 0.50% ·
    640MiB · stale
  ~/tmp/old
    exited · mount · orphan
───────────────────────────────────────
kbx-kbx-1a2b3c4d · ~/Projects/kbx ·
  mount mode · running · details 4s old
Guard
  ✓ watching the checkout's git hooks,
    config and protected files
Health
  ✓ seeds, start.sh and services ok
Git (mount mode)
  3 commit(s) on HEAD since the sandbox
Enter attach · Tab details · ? keys · q
```

## Safety

- In mount mode the dashboard never runs git on the host in your checkout:
  a `git status` there would run whatever `core.fsmonitor` or filter the agent
  planted. It asks git inside the sandbox instead, so a stopped sandbox shows
  no git state.
- Text from a sandbox (commit subjects, branch names, the guard's diffs) is
  drawn with control characters replaced, so it cannot drive your terminal.
- The dashboard is not part of the protection. The guard runs whether it is
  open or not; closing it changes nothing.
