"""Mount mode: which build directories the sandbox keeps to itself."""

from __future__ import annotations

from pathlib import Path

from kbx import private, sandbox
from tests.unit.helpers import TempHome


class PrivateTest(TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.project = self.temp / "project"
        self.project.mkdir()

    def test_markers_and_existing_directories(self) -> None:
        entries = ["target", ".venv", "node_modules", "build"]
        self.assertEqual(private.wanted(self.project, entries), [])
        (self.project / "Cargo.toml").write_text("")
        (self.project / "uv.lock").write_text("")
        self.assertEqual(private.wanted(self.project, entries), ["target", ".venv"])
        (self.project / "build").mkdir()  # no marker known: only when it exists
        self.assertEqual(private.wanted(self.project, entries), ["target", ".venv", "build"])

    def test_nested_entry_uses_its_own_directory(self) -> None:
        (self.project / "backend").mkdir()
        (self.project / "backend" / "Cargo.toml").write_text("")
        self.assertEqual(private.wanted(self.project, ["target", "backend/target"]), ["backend/target"])

    def test_symlink_stays_shared(self) -> None:
        (self.project / "Cargo.toml").write_text("")
        (self.project / "target").symlink_to(self.temp)
        self.assertEqual(private.wanted(self.project, ["target"]), [])

    def test_script_mounts_from_the_home_volume(self) -> None:
        self.assertIn('store="$store_dir/$rel"', private.SCRIPT)
        self.assertIn("runuser -u agent -- mkdir", private.SCRIPT)
        self.assertIn("mountpoint -q", private.SCRIPT)
        self.assertTrue(Path(private.__file__).is_file())

    def test_each_profile_project_has_its_own_store(self) -> None:
        roots = (self.temp,)
        one = sandbox.for_profile("work", roots, self.project)
        other = self.temp / "other"
        other.mkdir()
        two = sandbox.for_profile("work", roots, other)
        self.assertEqual(private.store(sandbox.for_project(self.project, "mount")), "/home/agent/.cache/kbx-private")
        self.assertTrue(private.store(one).startswith("/home/agent/.cache/kbx-private/project-"))
        self.assertNotEqual(private.store(one), private.store(two))
