"""Git in/out through bundles, with the sandbox clone emulated by the fake docker."""

from __future__ import annotations

import contextlib
import io
import os
import subprocess

from kbx import config, git, paths, sandbox
from kbx.docker import Docker
from kbx.errors import KbxError
from tests.unit.helpers import TempHome
from tests.unit.helpers import git as run_git


class GitTest(TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo("project")
        run_git(self.repo, "remote", "add", "origin", "https://user:secret@example.com/org/project.git")
        self.paths = paths.resolve({"HOME": str(self.home)})
        self.paths.stage.mkdir(parents=True)
        self.cfg = config.load(self.paths, {})
        self.docker = Docker()
        self.sb = sandbox.for_project(self.repo)
        self.add_image()
        with contextlib.redirect_stdout(io.StringIO()):
            sandbox.ensure_created(self.docker, self.sb, self.cfg, self.paths)
            sandbox.ensure_running(self.docker, self.sb, timeout=5)
        self.clone = self.sandbox_home / "work" / "project"
        self.sandbox_env = {k: v for k, v in os.environ.items() if k not in ("GIT_CONFIG_GLOBAL",)}
        self.sandbox_env["HOME"] = str(self.sandbox_home)

    def sgit(self, *args: str) -> str:
        """git as the agent, inside the sandbox clone."""
        return run_git(self.clone, *args, env=self.sandbox_env)

    def seed(self) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            git.seed(self.docker, self.sb)
        return out.getvalue()

    def fetch(self, *branches: str) -> str:
        out = io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(out):
            git.fetch(self.docker, self.sb, list(branches))
        return out.getvalue()

    def sandbox_commit(self, branch: str, name: str, content: str = "x\n") -> str:
        if branch not in self.sgit("branch", "--format=%(refname:short)").split():
            self.sgit("checkout", "-q", "-b", branch)
        else:
            self.sgit("checkout", "-q", branch)
        (self.clone / name).write_text(content)
        self.sgit("add", name)
        self.sgit("commit", "-q", "-m", f"add {name}")
        return self.sgit("rev-parse", "HEAD")

    def test_seed(self) -> None:
        (self.repo / "untracked.env").write_text("SECRET=1\n")
        output = self.seed()
        self.assertIn("uncommitted or untracked", output)
        self.assertTrue(git.is_seeded(self.docker, self.sb))
        self.assertEqual((self.clone / "README.md").read_text(), "hello\n")
        self.assertFalse((self.clone / "untracked.env").exists())
        self.assertEqual(self.sgit("remote"), "host")
        self.assertEqual(self.sgit("config", "user.email"), "test@example.com")
        self.assertEqual(self.sgit("config", "kbx.upstream"), "https://example.com/org/project.git")
        note = (self.sandbox_home / "work" / "KBX.md").read_text()
        self.assertIn("https://example.com/org/project.git", note)
        self.assertNotIn("secret", note)
        gitconfig = (self.sandbox_home / ".gitconfig").read_text()
        self.assertNotIn("credential", gitconfig)
        self.assertNotIn("url", gitconfig)

    def test_fetch_new_branch(self) -> None:
        self.seed()
        sha = self.sandbox_commit("feature", "feature.txt")
        output = self.fetch()
        self.assertEqual(run_git(self.repo, "rev-parse", "refs/remotes/kbx/feature"), sha)
        self.assertIn("kbx/feature", output)
        self.assertIn("1 commit(s) not on any host branch", output)
        # the host's own branches are never touched by a fetch
        self.assertEqual(run_git(self.repo, "branch", "--format=%(refname:short)"), "main")

    def test_fetch_selected_and_unknown_branches(self) -> None:
        self.seed()
        self.sandbox_commit("a", "a.txt")
        self.sandbox_commit("b", "b.txt")
        self.fetch("a")
        run_git(self.repo, "rev-parse", "refs/remotes/kbx/a")
        with self.assertRaises(subprocess.CalledProcessError):
            run_git(self.repo, "rev-parse", "--verify", "refs/remotes/kbx/b")
        with self.assertRaises(KbxError):
            self.fetch("nope")

    def test_branch_without_new_commits_and_prune(self) -> None:
        self.seed()
        self.sgit("branch", "same")
        self.sandbox_commit("gone", "g.txt")
        self.fetch()
        main = run_git(self.repo, "rev-parse", "main")
        self.assertEqual(run_git(self.repo, "rev-parse", "refs/remotes/kbx/same"), main)
        self.sgit("checkout", "-q", "main")
        self.sgit("branch", "-q", "-D", "gone")
        output = self.fetch()
        self.assertIn("pruned kbx/gone", output)
        self.assertEqual(run_git(self.repo, "for-each-ref", "--format=%(refname)", "refs/remotes/kbx/gone"), "")

    def test_malicious_repo_cannot_run_code_on_the_host(self) -> None:
        self.seed()
        self.sandbox_commit("feature", "feature.txt")
        # Everything below is written by the agent inside the sandbox clone.
        # The payload only fires when run in the host's repository, since the
        # emulated sandbox shares this machine's filesystem.
        marker = self.temp / "PWNED"
        payload = f'#!/bin/sh\ncase "$PWD" in {self.repo}*) touch {marker} ;; esac\n'
        git_dir = self.clone / ".git"
        for path in (
            git_dir / "evil.sh",
            git_dir / "hooks" / "post-merge",
            git_dir / "post-merge",
            git_dir / "hooks" / "reference-transaction",
        ):
            path.write_text(payload)
            path.chmod(0o755)
        self.sgit("config", "core.fsmonitor", str(git_dir / "evil.sh"))
        self.sgit("config", "core.hooksPath", str(git_dir))
        self.sgit("config", "core.sshCommand", str(git_dir / "evil.sh"))
        self.sgit("config", "filter.x.smudge", str(git_dir / "evil.sh"))
        host_config = (self.repo / ".git" / "config").read_text()
        self.fetch()
        run_git(self.repo, "merge", "-q", "--ff-only", "kbx/feature")
        run_git(self.repo, "status")
        self.assertTrue((self.repo / "feature.txt").exists())
        self.assertFalse(marker.exists(), "sandbox-written git config or hooks ran on the host")
        self.assertEqual((self.repo / ".git" / "config").read_text(), host_config)
        hooks = sorted(p.name for p in (self.repo / ".git" / "hooks").iterdir() if not p.name.endswith(".sample"))
        self.assertEqual(hooks, [])

    def test_sync(self) -> None:
        self.seed()
        agent_sha = self.sandbox_commit("feature", "f.txt")
        run_git(self.repo, "checkout", "-q", "-b", "hostwork")
        (self.repo / "h.txt").write_text("h\n")
        run_git(self.repo, "add", "h.txt")
        run_git(self.repo, "commit", "-q", "-m", "host work")
        with contextlib.redirect_stdout(io.StringIO()):
            git.sync(self.docker, self.sb)
        self.assertEqual(
            self.sgit("rev-parse", "refs/remotes/host/hostwork"), run_git(self.repo, "rev-parse", "hostwork")
        )
        self.assertEqual(self.sgit("rev-parse", "feature"), agent_sha)
        # a branch deleted on the host disappears from host/* at the next sync
        run_git(self.repo, "checkout", "-q", "main")
        run_git(self.repo, "branch", "-q", "-D", "hostwork")
        with contextlib.redirect_stdout(io.StringIO()):
            git.sync(self.docker, self.sb)
        self.assertNotIn("host/hostwork", self.sgit("branch", "-r"))

    def test_unfetched_and_local_changes(self) -> None:
        self.seed()
        self.assertEqual(git.unfetched(self.docker, self.sb), [])
        self.sandbox_commit("feature", "a.txt")
        self.sandbox_commit("feature", "b.txt")
        pending = git.unfetched(self.docker, self.sb)
        self.assertEqual([(p.branch, p.commits) for p in pending], [("feature", 2)])
        self.fetch()
        self.assertEqual(git.unfetched(self.docker, self.sb), [])
        (self.clone / "dirty.txt").write_text("x")
        notes = git.local_changes(self.docker, self.sb)
        self.assertTrue(any("uncommitted" in n for n in notes))
        self.sgit("stash", "-q", "-u")
        notes = git.local_changes(self.docker, self.sb)
        self.assertTrue(any("stash" in n for n in notes))


class PreconditionTest(TempHome):
    def test_not_a_repository(self) -> None:
        plain = self.temp / "plain"
        plain.mkdir()
        with self.assertRaises(KbxError) as ctx:
            git.project_root(plain)
        self.assertIn("git init && git commit --allow-empty -m init", str(ctx.exception))

    def test_empty_repository(self) -> None:
        repo = self.make_repo("empty", commit=False)
        with self.assertRaises(KbxError) as ctx:
            git.project_root(repo)
        self.assertIn("no commits", str(ctx.exception))

    def test_subdirectory_resolves_to_top_level(self) -> None:
        repo = self.make_repo("top")
        (repo / "sub" / "dir").mkdir(parents=True)
        self.assertEqual(git.project_root(repo / "sub" / "dir"), repo.resolve())

    def test_linked_worktree_is_its_own_project(self) -> None:
        repo = self.make_repo("main-checkout")
        tree = self.temp / "wt"
        run_git(repo, "worktree", "add", "-q", str(tree), "-b", "wt")
        self.assertEqual(git.project_root(tree), tree.resolve())

    def test_strip_credentials(self) -> None:
        strip = git._strip_credentials  # pyright: ignore[reportPrivateUsage]
        self.assertEqual(strip("https://tok:x@github.com/a/b.git"), "https://github.com/a/b.git")
        self.assertEqual(strip("ssh://git@github.com:22/a/b"), "ssh://git@github.com:22/a/b")
        self.assertEqual(strip("git@github.com:a/b.git"), "git@github.com:a/b.git")
        self.assertEqual(strip("/local/path"), "")
        self.assertEqual(strip("https://github.com/a/b?token=x"), "https://github.com/a/b")
