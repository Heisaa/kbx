# Profiles: one sandbox for many projects

By default every project gets its own sandbox: its own VM, home volume, logins
and guard. A **profile** puts every project under a few directories into one
sandbox instead. For example, all work projects share one VM and your personal
projects another:

```toml
# ~/.config/kbx/config.toml
[profiles.work]
roots = ["~/work", "~/src/acme"]
memory = "16g"
cpus = 8

[profiles.personal]
roots = ["~/Projects"]
```

Run kbx in any directory under a root, and it uses that root's profile:

```sh
cd ~/work/api && kbx claude      # starts kbx-profile-work, Claude in ~/work/api
cd ~/work/web && kbx codex       # the same sandbox, Codex in ~/work/web
cd ~/Projects/blog && kbx claude # kbx-profile-personal
cd ~/scratch/x && kbx claude     # under no root: a sandbox for this project alone
```

Compared with a sandbox per project:

- **One VM per profile.** One memory limit covers all its projects
  (`memory`/`cpus` per profile), and the inner Docker, caches and installed
  tools are shared.
- **One login per profile.** Log in to Claude and Codex once per profile. Use
  your work account in `work` and your personal one in `personal`: they never
  share a home volume.
- **Sessions per project.** Each project has its own Claude, Codex and pi
  session in the profile's sandbox (`claude@api`, `codex@web`).
  `kbx claude` in `~/work/api` starts or reattaches the one for `api`. Sessions
  in other projects keep running.
- **Every root is mounted at its own path**, all of it: not only repositories
  but every file under it. Projects you clone into a root later show up in the
  sandbox right away, with no `kbx recreate`.

## Settings

| Key | |
| --- | --- |
| `roots` | Required. Directories mounted into the profile's sandbox, at their own paths. `~/` expands. A root may not be your home directory or contain it, may not contain kbx's own config or data, and may not overlap another root (of this or any other profile) |
| `memory`, `cpus`, `idle_stop` | Like `[launcher]`, for this profile's sandbox. `KBX_<NAME>` still overrides them for one run |
| `private`, `protect` | Like `[workspace]`, for this profile's projects (each project's paths are relative to it) |

Everything else (modules, image, network, skills, other launcher settings)
is shared with the rest of kbx. A profile always uses mount mode. Clone mode
stays per project, so `KBX_WORKSPACE=clone` is refused in a profile, and so
are `kbx fetch` and `kbx sync`.

`KBX_PROFILE=<name>` picks a profile for one command, for example
`KBX_PROFILE=work kbx shell` from anywhere: kbx then works in the first root if
the current directory is outside the profile. `KBX_PROFILE=none` forces a
sandbox for the project alone, even under a root.

## What a project is

In a profile, the project is the nearest directory upward from where you run
kbx that has a `.git`, up to the root. It needs no commit. Outside any
repository it is the current directory itself, so `cd ~/work && kbx claude`
starts a Claude that works across all of `~/work`. To find the repository,
kbx only reads the file system. It does not run git on the host.

Per project:

- the agent's session, its working directory and its folder trust
  (`kbx-onboard`) and, for Codex remote control, its project;
- `kbx attach`, `kbx diff` and `kbx shell`;
- the sandbox's own build directories (`[workspace] private`): each project
  keeps its own `target/` and `.venv`;
- `kbx diff --since-start`, from the first kbx command in that project after
  the sandbox started.

Per profile: the VM and its lifecycle (`kbx stop`, `recreate`, `rm` and the
idle stop all affect every project), logins, agent versions, and the guard.

## Sessions

```sh
kbx attach                  # this project's session (asks you to name one if several)
kbx attach codex            # this project's Codex
kbx attach codex@web-1a2b3c # any session of the profile, by the name `kbx ls` shows
kbx ls                      # every sandbox, its sessions, and the profile's roots
```

A session's full name is `<agent>@<project>-<hash>`. The hash keeps two
projects with the same directory name apart. Desktop notifications name the
project.

Agents are installed once per profile. kbx updates an agent at launch only
while no session of it is running in any project of the profile (`kbx update`
too). Otherwise it says so and starts the installed version.

## The guard in a profile

The guard watches every repository under every root, with the same checks a
single checkout gets: git config, hooks, `commondir`, the hooks path, and the
`protect` files of each project. A finding anywhere pauses the whole sandbox,
so every agent in the profile freezes until you run `kbx resume` in any of its
projects. It also checks that a repository's `.git` still points where it did.
If an agent turns a project's `.git` into a pointer to somewhere else, the
guard moves the pointer away.

A project that disappears along with its `.git` (you deleted it) is not a
finding. A repository that appears while the sandbox runs is found at once
when it sits directly in a root, and within a minute deeper down. Its hooks,
config and `protect` files are then new compared with the baseline. Cloning
a project into a root therefore pauses the sandbox if that project has a
`.husky/`, `.pre-commit-config.yaml` or `.vscode/settings.json`. `kbx resume
--accept` takes it in as yours. Or clone while the profile's sandbox is
stopped: the baseline taken at the next start trusts it.

The guard skips dependency and cache directories when it walks for
repositories (`node_modules`, `target` beside a `Cargo.toml`, virtualenvs and
Python caches).

## Changing roots

Mounts are fixed when the sandbox is created. After you add or remove a root,
kbx warns, and refuses to work in a project the sandbox does not mount yet.
`kbx recreate` mounts the roots from the config. Before it does, it lists
secret-looking files in the added roots. The guard always watches what the
sandbox actually mounts.

If a project had a sandbox of its own before it came under a root, kbx now
uses the profile's and says the old one still exists.
`KBX_PROFILE=none kbx rm` in the project removes the old one.

## Security: what a profile shares

A profile trades isolation between its projects for convenience:

- **Every agent in a profile can read and change every project in it.** That
  includes untracked files such as `.env` in all of them. A prompt injection
  picked up in one repository (an issue, a dependency, a web page) reaches all
  the others. Before it creates a profile's sandbox, kbx lists the
  secret-looking files in all its roots and asks.
- **One login per profile.** A stolen token reaches every project the profile
  holds.
- **Profiles stay apart from each other** and from project sandboxes:
  separate VMs, home volumes and guards, and no root may belong to two
  profiles.

Keep projects you would not hand to the same agent in different profiles, or
outside every root.
