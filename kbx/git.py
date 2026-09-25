"""Git in and out of the sandbox, through bundles only.

The rule: the host never runs git against a repository the agent can write.
The host runs git only in its own clone (`git -C <project>`); the sandbox
clone lives in the home volume, and only bundle data crosses the boundary.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import tempfile
import urllib.parse
from dataclasses import dataclass
from pathlib import Path

from .docker import Docker
from .errors import KbxError
from .sandbox import HOME, Sandbox

SHA = re.compile(r"^[0-9a-f]{40}([0-9a-f]{24})?$")
HOST_BUNDLE = "/tmp/kbx-host.bundle"
NOTE = f"{HOME}/work/KBX.md"


def _git_env() -> dict[str, str]:
    env = dict(os.environ)
    env["GIT_TERMINAL_PROMPT"] = "0"
    return env


def host_git(root: Path, *args: str, check: bool = True, **kwargs: object) -> subprocess.CompletedProcess[str]:
    """Run git in the host's own repository."""
    result = subprocess.run(
        ["git", "-C", str(root), *args],
        capture_output=True,
        text=True,
        check=False,
        env=_git_env(),
        **kwargs,  # type: ignore[arg-type]
    )
    if check and result.returncode != 0:
        detail = (result.stderr.strip().splitlines() or ["no output"])[-1]
        raise KbxError(f"git {args[0]} failed: {detail}")
    return result


def project_root(cwd: Path) -> Path:
    """The git top-level containing cwd (a linked worktree is its own project)."""
    try:
        result = subprocess.run(
            ["git", "-C", str(cwd), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            check=False,
            env=_git_env(),
        )
    except FileNotFoundError:
        raise KbxError("git is not installed on the host") from None
    if result.returncode != 0:
        raise KbxError(
            f"{cwd} is not inside a git repository. kbx needs one with at least one commit:\n"
            "  git init && git commit --allow-empty -m init"
        )
    root = Path(result.stdout.strip())
    if host_git(root, "rev-parse", "--verify", "-q", "HEAD^{commit}", check=False).returncode != 0:
        raise KbxError(f"{root} has no commits yet. Create one first:\n  git commit --allow-empty -m init")
    return root


def _strip_credentials(url: str) -> str:
    try:
        parts = urllib.parse.urlsplit(url)
    except ValueError:
        return ""
    if parts.scheme in ("http", "https", "ssh", "git") and parts.hostname:
        netloc = parts.hostname + (f":{parts.port}" if parts.port else "")
        if parts.scheme == "ssh" and parts.username:
            netloc = f"{parts.username}@{netloc}"
        return urllib.parse.urlunsplit((parts.scheme, netloc, parts.path, "", ""))
    if re.match(r"^[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[^\s]+$", url):  # scp-like ssh
        return url
    return ""


def is_seeded(docker: Docker, sandbox: Sandbox) -> bool:
    result = docker.exec(sandbox.name, ["test", "-d", f"{sandbox.workdir}/.git"], check=False)
    return result.returncode == 0


SEED_SCRIPT = r"""
set -eu
mkdir -p "$(dirname "$1")"
cat > /tmp/kbx-seed.bundle
git clone -q --origin host /tmp/kbx-seed.bundle "$1"
git -C "$1" remote set-url host "$2"
rm -f /tmp/kbx-seed.bundle
"""


def seed(docker: Docker, sandbox: Sandbox) -> None:
    root = sandbox.project_dir
    print(f"→ Seeding the sandbox clone from {root}")
    if host_git(root, "status", "--porcelain", check=False).stdout.strip():
        print(
            "⚠ The host tree has uncommitted or untracked changes; they are not copied "
            "(only commits are). Provide project secrets explicitly if needed.",
            file=sys.stderr,
        )
    bundle = subprocess.Popen(
        ["git", "-C", str(root), "bundle", "create", "-", "--all"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_env(),
    )
    assert bundle.stdout is not None
    result = docker.exec(
        sandbox.name,
        ["sh", "-c", SEED_SCRIPT, "sh", sandbox.workdir, HOST_BUNDLE],
        stdin=bundle.stdout,
        check=False,
    )
    bundle.stdout.close()
    _, bundle_err = bundle.communicate()
    if bundle.returncode != 0:
        raise KbxError(f"git bundle failed on the host: {bundle_err.decode().strip()}")
    if result.returncode != 0:
        raise KbxError(f"cloning in the sandbox failed: {result.stderr.decode().strip()}")
    for key in ("user.name", "user.email"):
        value = host_git(root, "config", "--get", key, check=False).stdout.strip()
        if value:
            docker.exec(sandbox.name, ["git", "config", "--global", key, value])
    upstream = _strip_credentials(host_git(root, "remote", "get-url", "origin", check=False).stdout.strip())
    if upstream:
        docker.exec(sandbox.name, ["git", "-C", sandbox.workdir, "config", "kbx.upstream", upstream])
    docker.exec(sandbox.name, ["sh", "-c", 'cat > "$1"', "sh", NOTE], input=_note(sandbox, upstream).encode())
    print(f"✓ Cloned into {sandbox.workdir}")


def _note(sandbox: Sandbox, upstream: str) -> str:
    return f"""# kbx sandbox

`{sandbox.workdir}` is a clone of the host project, seeded from a git bundle.

- The remote `host` holds the host's branches as `host/*`. The developer runs
  `kbx sync` on the host to refresh them; then `git fetch host` works here too.
- You cannot push. The developer fetches your branches with `kbx fetch` and
  pushes from the host. Commit your work on a branch.
- Upstream repository: {upstream or "(none recorded)"}
- When several agents work at once, give each its own worktree:
  `git worktree add ~/work/{sandbox.project}-<task> -b <branch>`.
"""


SYNC_SCRIPT = r"""
set -eu
cat > "$2.tmp"
mv "$2.tmp" "$2"
git -C "$1" fetch --prune host "+refs/heads/*:refs/remotes/host/*"
"""


def sync(docker: Docker, sandbox: Sandbox) -> None:
    """Host branches → sandbox refs/remotes/host/*; the agent's branches are untouched."""
    root = sandbox.project_dir
    bundle = subprocess.Popen(
        ["git", "-C", str(root), "bundle", "create", "-", "--branches"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        env=_git_env(),
    )
    assert bundle.stdout is not None
    result = docker.exec(
        sandbox.name,
        ["sh", "-c", SYNC_SCRIPT, "sh", sandbox.workdir, HOST_BUNDLE],
        stdin=bundle.stdout,
        stderr=None,
        check=False,
    )
    bundle.stdout.close()
    _, bundle_err = bundle.communicate()
    if bundle.returncode != 0:
        raise KbxError(f"git bundle failed on the host: {bundle_err.decode().strip()}")
    if result.returncode != 0:
        raise KbxError("fetching the host bundle in the sandbox failed")
    print("✓ Host branches are available in the sandbox as host/*")


@dataclass(frozen=True)
class Branch:
    name: str
    sha: str


def _valid_branch(root: Path, name: str) -> bool:
    if not name or name.startswith("-") or any(c in name for c in "\0\n\r\t "):
        return False
    return host_git(root, "check-ref-format", f"refs/heads/{name}", check=False).returncode == 0


def sandbox_branches(docker: Docker, sandbox: Sandbox) -> list[Branch]:
    out = docker.exec_text(
        sandbox.name,
        ["git", "-C", sandbox.workdir, "for-each-ref", "--format=%(objectname) %(refname)", "refs/heads/"],
    )
    branches: list[Branch] = []
    for line in out.splitlines():
        sha, _, ref = line.partition(" ")
        name = ref.removeprefix("refs/heads/")
        if not SHA.match(sha) or not ref.startswith("refs/heads/") or not _valid_branch(sandbox.project_dir, name):
            print(f"⚠ ignoring sandbox ref with an unexpected name: {line!r}", file=sys.stderr)
            continue
        branches.append(Branch(name, sha))
    return branches


def _bundle_out(docker: Docker, sandbox: Sandbox, refs: list[str], incremental: bool, dest: Path) -> bool:
    """Write a sandbox bundle to dest. False if there is nothing new to bundle."""
    argv = ["git", "-C", sandbox.workdir, "bundle", "create", "-", *refs]
    if incremental:
        argv += ["--not", "--remotes=host"]
    with dest.open("wb") as handle:
        result = docker.exec(sandbox.name, argv, stdout=handle, check=False)
    if result.returncode == 0:
        return True
    error = result.stderr.decode("utf-8", "replace")
    if "empty bundle" in error:
        return False
    raise KbxError(f"creating the bundle in the sandbox failed: {error.strip()}")


def fetch(docker: Docker, sandbox: Sandbox, requested: list[str]) -> None:
    """Sandbox branches → host refs/remotes/kbx/*, parsed with fsck enabled."""
    root = sandbox.project_dir
    branches = {b.name: b for b in sandbox_branches(docker, sandbox)}
    if requested:
        missing = [name for name in requested if name not in branches]
        if missing:
            raise KbxError(f"no such branch in the sandbox: {', '.join(missing)}")
        selected = [branches[name] for name in requested]
    else:
        selected = list(branches.values())
    if not selected:
        print("No branches in the sandbox.")
        return
    refs = [f"refs/heads/{b.name}" for b in selected]
    with tempfile.TemporaryDirectory(prefix="kbx-fetch-") as temp:
        path = Path(temp) / "out.bundle"
        have_bundle = _bundle_out(docker, sandbox, refs, incremental=True, dest=path)
        if have_bundle and host_git(root, "bundle", "verify", "-q", str(path), check=False).returncode != 0:
            # The host lacks a prerequisite (e.g. a host branch was deleted and
            # garbage-collected); fall back to a self-contained bundle.
            have_bundle = _bundle_out(docker, sandbox, refs, incremental=False, dest=path)
            host_git(root, "bundle", "verify", "-q", str(path))
        in_bundle: set[str] = set()
        if have_bundle:
            for line in host_git(root, "bundle", "list-heads", str(path)).stdout.splitlines():
                _, _, ref = line.partition(" ")
                if ref.startswith("refs/heads/"):
                    in_bundle.add(ref.removeprefix("refs/heads/"))
            refspecs = [f"+refs/heads/{name}:refs/remotes/kbx/{name}" for name in sorted(in_bundle)]
            if refspecs:
                host_git(
                    root,
                    "-c", "transfer.fsckObjects=true",
                    "-c", "fetch.fsckObjects=true",
                    "fetch", "--quiet", "--no-tags", "--no-write-fetch-head", str(path), *refspecs,
                )  # fmt: skip
    # Branches whose tips the host already has were left out of the bundle.
    for branch in selected:
        if branch.name in in_bundle:
            continue
        if host_git(root, "cat-file", "-e", f"{branch.sha}^{{commit}}", check=False).returncode != 0:
            print(f"⚠ {branch.name}: commit {branch.sha[:12]} is missing on the host; skipped", file=sys.stderr)
            continue
        host_git(root, "update-ref", f"refs/remotes/kbx/{branch.name}", branch.sha)
    if not requested:
        _prune(root, set(branches))
    _summary(root, selected)


def _prune(root: Path, existing: set[str]) -> None:
    out = host_git(root, "for-each-ref", "--format=%(refname)", "refs/remotes/kbx/").stdout
    for ref in out.splitlines():
        name = ref.removeprefix("refs/remotes/kbx/")
        if name not in existing:
            host_git(root, "update-ref", "-d", ref)
            print(f"  pruned kbx/{name} (deleted in the sandbox)")


def _summary(root: Path, selected: list[Branch]) -> None:
    print("Fetched into refs/remotes/kbx/:")
    width = max(len(b.name) for b in selected)
    for branch in selected:
        ref = f"refs/remotes/kbx/{branch.name}"
        if host_git(root, "rev-parse", "-q", "--verify", ref, check=False).returncode != 0:
            continue
        ahead = host_git(root, "rev-list", "--count", ref, "--not", "--branches").stdout.strip()
        print(f"  kbx/{branch.name:<{width}}  {branch.sha[:10]}  {ahead} commit(s) not on any host branch")
    print("Review with e.g. `git log -p main..kbx/<branch>`; merge and push from the host.")


@dataclass(frozen=True)
class Unfetched:
    branch: str
    commits: int


def unfetched(docker: Docker, sandbox: Sandbox) -> list[Unfetched]:
    """Sandbox branches whose tips are neither on host/* nor fetched to kbx/*."""
    root = sandbox.project_dir
    result: list[Unfetched] = []
    for branch in sandbox_branches(docker, sandbox):
        count = docker.exec_text(
            sandbox.name,
            ["git", "-C", sandbox.workdir, "rev-list", "--count", branch.sha, "--not", "--remotes=host"],
        )
        if count == "0":
            continue
        on_host = host_git(root, "cat-file", "-e", f"{branch.sha}^{{commit}}", check=False).returncode == 0
        if on_host:
            left = host_git(root, "rev-list", "--count", branch.sha, "--not", "--remotes=kbx").stdout.strip()
            if left == "0":
                continue
        result.append(Unfetched(branch.name, int(count) if count.isdigit() else 0))
    return result


def local_changes(docker: Docker, sandbox: Sandbox) -> list[str]:
    """Uncommitted changes (in every worktree) and stashes in the sandbox clone."""
    notes: list[str] = []
    listing = docker.exec_text(
        sandbox.name, ["git", "-C", sandbox.workdir, "worktree", "list", "--porcelain"], check=False
    )
    trees = [line[len("worktree ") :] for line in listing.splitlines() if line.startswith("worktree ")]
    for tree in trees or [sandbox.workdir]:
        status = docker.exec_text(sandbox.name, ["git", "-C", tree, "status", "--porcelain"], check=False)
        if status:
            notes.append(f"{tree}: {len(status.splitlines())} uncommitted change(s)")
    stashes = docker.exec_text(sandbox.name, ["git", "-C", sandbox.workdir, "stash", "list"], check=False)
    if stashes:
        notes.append(f"{len(stashes.splitlines())} stash entr(y/ies)")
    return notes
