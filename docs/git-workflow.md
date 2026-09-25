# Git workflow

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

## Several agents at once

Claude, Codex and pi share one sandbox and one clone. Two agents editing the
same working tree overwrite each other's changes. Give each its own worktree:

```sh
kbx shell
git worktree add ~/work/myproject-codex -b codex/task
cd ~/work/myproject-codex && codex
```

or start one session, then in it ask the agent to work in a new worktree.
`kbx fetch` picks up branches from every worktree, since they share the ref
store.
