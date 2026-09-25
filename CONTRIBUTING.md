# Contributing

## Ground rules

- **No personal content in the repository.** Built-in modules are generic
  features; anything opinionated is off by default or lives in `examples/`.
- **Host package (`kbx/`) is stdlib only.** The sandbox package
  (`kbx_sandbox/`) may use `python3-tomlkit` and `python3-xlib`, both Debian
  packages. The two packages share no code: the host resolves everything into
  the stage's `config.json`, and sandbox code reads only that.
- **Docker through the CLI**, only via `kbx/docker.py`.
- **Software at build time, never at start** (see [docs/modules.md](docs/modules.md)).
- Errors users can act on raise `KbxError` (one line, non-zero exit); tracebacks
  only with `KBX_DEBUG=1`.
- Code ported from elsewhere must be owned by its contributors or compatibly
  licensed.

## Checks

```sh
./check          # ruff (lint + format check), pyright strict, shellcheck, unit tests
```

The dev tools are optional (missing ones are skipped); `kbx` itself never needs
them. Install for example with `uv tool install ruff pyright` and your
distribution's `shellcheck`. The clipboard bridge tests need `Xvfb`,
`python3-xlib` and `xclip`; the seed tests need `python3-tomlkit`. They skip or
fail clearly without them.

`./check` also reruns the unit tests with an empty `HOME` and no config, so
nothing may depend on a particular machine.

## Tests

- `tests/unit/`: always run. A fake `docker` (`tests/unit/fakedocker.py`) on
  `PATH` keeps container state in a temp dir and runs `docker exec` commands
  on the host with `/home/agent` mapped into it, so git flows (including the
  malicious-repo test) run for real without a VM.
- `tests/integration/`: against a real sandbox from the built image. Skipped
  unless `KBX_INTEGRATION=1`.

  ```sh
  kbx build
  KBX_INTEGRATION=1 python3 -m unittest discover -s tests/integration -t . -v
  ```

  `KBX_RUNTIME=runc KBX_UNSAFE_NO_FIREWALL_CHECK=1` runs them without Kata or
  the firewall (development only; not isolated). The network tests run only
  when the kbx firewall is active; the firewall-toggle test also needs
  `KBX_TEST_FIREWALL_TOGGLE=1` and passwordless sudo.

## Manual checks before a release

- Remote control: a Claude session appears in the claude.ai/app list, survives
  detach and a host terminal close. Codex pairs, survives detach, and comes
  back after `kbx stop` plus relaunch.
- Image paste: Ctrl+V in Claude, Codex and pi after copying an image on the
  host (Wayland and X11).
- `host/spike.sh` passes on a fresh host.
