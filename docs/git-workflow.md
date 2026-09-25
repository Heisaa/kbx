# Git workflow

kbx has two workspace modes (`[workspace] mode` in
[configuration.md](configuration.md), or `KBX_WORKSPACE` for one run):

- **mount** (default): your checkout is mounted into the sandbox. You and the
  agent work in the same repository. A guard on the host keeps the agent from
  planting code your tools would run.
- **clone**: the sandbox has its own clone. Git crosses only as bundles, with
  `kbx fetch` and `kbx sync`.

The mode is fixed when the sandbox is created. `kbx recreate` switches to the
configured mode and keeps the volumes.

# Mount mode

The project is the git top-level containing your current directory. It is
mounted into the sandbox **at the same path**, so absolute paths (virtualenvs,
build caches, compiler output, `docker run -v "$PWD":/src` inside the sandbox)
work on both sides. The agent runs as `agent` with your uid and gid, so the files
it writes are yours. Files it writes as root (for example from a container it
starts) stay owned by root on the host.

- **Commit on either side.** Branches, stashes, the index and the working tree
  are shared. There is nothing to fetch. `kbx fetch` and `kbx sync` only reach a
  clone left over from clone mode.
- **Review before you run.** The agent's edits land in your checkout as it
  makes them. Look at `git diff` / `git log -p` before running tests, builds or
  the app on the host. Better, run them in the sandbox.
- **The agent cannot push.** It has no git credentials. You push from the host.
- **Untracked files are shared too.** The agent can read `.env` files and other
  secrets in the checkout. The internet is open, so it could send them out. Use
  clone mode for projects where that matters.
- **Linked worktrees work too.** A worktree is its own project with its own
  sandbox. Its `.git` is a file pointing into the main repository's `.git`, so
  kbx mounts that directory as well, at its own path. The main checkout's files
  stay out. The agent sees every branch of the repository and can commit to
  any of them, as a worktree on the host can. The guard watches the shared
  `.git` and the worktree's `.git` file. If the main checkout (or another
  worktree) has a sandbox too, each guard watches the same `.git`: a change
  there pauses both sandboxes, and each needs its own `kbx resume`. The same
  goes for submodule checkouts and `--separate-git-dir` repositories.

## The guard

Git runs whatever the repository tells it to. `core.fsmonitor` runs on every
`git status` (editors poll it). Hooks run on commit, checkout and push. A
`.git/commondir` file makes git read a different directory's config. Hook
frameworks and editors run files from the tree. Kata cannot mount those paths
read-only in a way root in the VM could not undo
([PLAN.md](../PLAN.md#workspace-mount-mode-and-the-guard) explains why). So a
guard process on the host watches them while the sandbox runs:

| Watched | Flagged when |
| --- | --- |
| `.git/config`, `config.worktree` (also for submodules and nested repositories) | a new entry is outside the safe list: anything that runs a program (`core.fsmonitor`, `core.pager`, `core.sshCommand`, `*.textconv`, `filter.*`, `alias.*`, `credential.*`, …), reads other config (`include.*`), or points git elsewhere (`core.worktree`, `url.*`, `http.*`) |
| hooks: `.git/hooks`, submodules', nested repositories', and an in-tree `core.hooksPath` | any file added, changed or removed (`*.sample` ignored) |
| `commondir` files | pointing anywhere but their own repository |
| a worktree's `.git` file | pointing anywhere but its git directory |
| `[workspace] protect` paths | added, changed or removed. Defaults: `.husky/`, `.githooks/`, `.lefthook/`, pre-commit and lefthook configs, `.vscode/settings.json`, `.vscode/tasks.json` |

Your own settings stay untouched: the baseline is whatever is there when the
sandbox starts, and only entries that are new since then are flagged. Ordinary
work never trips it: commits, branches, stashes, `git worktree add`, `git
clone` of a nested repository, remote URLs, `branch.*` settings, `git
submodule update`.

When the guard finds something, it:

1. Neutralizes it: removes the new config entries, or moves the file to
   quarantine and puts the trusted version back.
2. Pauses the sandbox (`docker pause`). The agent freezes mid-step.
3. Writes a note to your attached terminals and sends a desktop notification.
   Detach from a frozen session with `Ctrl-P Ctrl-Q`.

Then:

```sh
kbx resume            # shows each change with a diff, asks whether they were yours, resumes
kbx resume --accept   # the same, restoring the changes without asking
```

Without a terminal, `kbx resume` keeps the neutralized versions.

`kbx resume --accept` is for changes that were legitimate, typically your own.
Some of your own actions trip the guard while a sandbox runs: installing hooks
(`pre-commit install`, `npx husky`, `git lfs install`), adding config
outside the safe list, or editing a protected file. Make those changes while
the sandbox is stopped (`kbx stop`), or accept them afterwards. The same goes
for the agent's first `npm install` in a project that uses husky: it installs
hooks, which you then review and accept.

How it holds up:

- It watches with inotify, so a planted file is usually reverted within
  milliseconds. Changes it misses by timing are caught at the next check,
  within a second.
- It runs only while kbx has started it. `kbx claude|codex|pi`, `attach` and
  `shell` start it if it is missing, and check first. When the sandbox stops, it
  checks once more and seals the state. A start without a seal (after a crash
  or reboot) checks the old baseline before taking a new one, and refuses to
  start on findings until you run `kbx resume`.
- Nested repositories are searched every minute. A repository the agent
  creates in the tree is checked like the main one, because your editor may
  run git in it.
- It does not review ordinary code. A changed `Makefile`, `package.json`
  script or test is yours to review before you run it.
- Logs: `kbx logs` names the file. Quarantined versions stay in
  `$XDG_DATA_HOME/kbx/guard/<sandbox>/quarantine/`.

`[workspace] guard = false` turns it off, with a warning at every launch.

# Clone mode

The rule: **the host never runs git against a repository the agent can
write.** The sandbox clone lives only in the sandbox's home volume, and only
git bundle data crosses the boundary. On the host, kbx runs git only in your
own clone (`git -C <project>`).

## Seeding (first launch)

The project is the git top-level containing your current directory (a linked
worktree is its own project, with its own sandbox). It must have at least one
commit:

```sh
git init && git commit --allow-empty -m init
```

On the first launch kbx streams `git bundle create - --all` into the sandbox
and clones it to `~/work/<project>`, with the remote named `host`.

- **Only commits travel.** Uncommitted changes and untracked files (including
  `.env`) stay on the host; kbx warns if the tree is dirty. Provide project
  secrets explicitly if the agent needs them.
- `user.name` and `user.email` are copied into the sandbox's git config.
  Nothing else is (no `credential.*`, `url.*`, `core.sshCommand`, includes).
- The `origin` URL, with any credentials stripped, is recorded as
  `git config kbx.upstream` and in `~/work/KBX.md`, a short note for agents.
- Submodules must be fetched from their upstream inside the sandbox (public
  HTTPS works). Private submodules are out of scope.

The agent **cannot push**: the sandbox has no git credentials.

## Host → sandbox: `kbx sync`

```sh
kbx sync
```

Bundles your host branches (`refs/heads/*`) into the sandbox as
`refs/remotes/host/*` (pruning ones you deleted). The agent's own branches
are never touched. After a sync, `git fetch host` inside also works.

## Sandbox → host: `kbx fetch`

```sh
kbx fetch                 # every sandbox branch
kbx fetch agent/feature   # just these
```

kbx bundles the sandbox branches (only commits the host does not have yet),
verifies the bundle on the host and fetches it with `transfer.fsckObjects`
into `refs/remotes/kbx/*`. Hooks and config from the sandbox repo never travel
in a bundle. Tags are left out, because a tag can shadow a branch name in
review. `kbx/*` refs for branches deleted in the sandbox are pruned when you
fetch everything.

Then review and push from your own clone, as usual:

```sh
git log -p main..kbx/agent/feature
git switch -c feature kbx/agent/feature   # or merge / cherry-pick
git push
```

**Be careful after merging.** Code the agent wrote is untrusted: run tests in
the sandbox, not on the host. Repo-defined hooks run on the host when you
commit or push: husky (`core.hooksPath=.husky`), `.pre-commit-config.yaml`,
`lefthook.yml`. So can tools acting on checked-out files: a changed `.envrc`
(direnv asks to re-allow), `.lfsconfig`, editor workspace trust. Review
changes to those files in the diff. `git push --no-verify` skips push hooks.

## Removing a sandbox: `kbx rm`

The home volume holds the **only copy** of commits you have not fetched.
`kbx rm` starts the sandbox if needed, lists every branch whose tip is neither
on `host/*` nor already fetched into `kbx/*`, offers to run `kbx fetch`,
warns about uncommitted changes and stashes (in every worktree), and asks
before deleting. Without a terminal it never confirms; pass `--yes` to skip
the question. `kbx recreate` keeps the volumes and needs no check.

# Several agents at once

Claude, Codex and pi share one sandbox and one checkout (or clone). Two
agents editing the same working tree overwrite each other's changes. Give each
its own worktree:

```sh
kbx shell
git worktree add ~/work/myproject-codex -b codex/task
cd ~/work/myproject-codex && codex
```

or start one session, then in it ask the agent to work in a new worktree.
In mount mode, keep such worktrees outside the checkout, as above: their
branches appear on the host right away, since they share the ref store. The
host sees their bookkeeping in `.git/worktrees/` with a path it cannot reach,
so do not run `git worktree prune` on the host while they are in use. In clone
mode, `kbx fetch` picks up branches from every worktree.
