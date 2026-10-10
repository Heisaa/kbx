from __future__ import annotations

import concurrent.futures
import contextlib
import io
import json
import threading
import unittest
from dataclasses import replace
from typing import Any
from unittest import mock

from kbx import config, paths, resources, sandbox
from kbx.docker import Docker
from kbx.errors import KbxError
from tests.unit.helpers import TempHome

GIB = resources.GIB


def vm(name: str, memory: int = 8 * GIB, status: str = "running", runtime: str = "kata") -> dict[str, Any]:
    return {
        "Name": "/" + name,
        "HostConfig": {"Memory": memory, "Runtime": runtime},
        "State": {"Status": status},
    }


class BudgetTest(unittest.TestCase):
    def setUp(self) -> None:
        self.docker = mock.create_autospec(Docker, instance=True)
        self.cfg = config.Config()
        self.target = vm("new", status="exited")
        self.active: list[dict[str, Any]] = []
        self.docker.text.side_effect = self.text
        self.capacity = resources.Capacity(32 * GIB, 24 * GIB, 16 * GIB, 16 * GIB)
        self.addCleanup(mock.patch.stopall)
        mock.patch.dict("os.environ", {}, clear=True).start()
        self.measure = mock.patch("kbx.resources.capacity", return_value=self.capacity).start()
        mock.patch("kbx.resources.kata_settings", return_value=(2 * GIB, True)).start()

    def text(self, args: list[str]) -> str:
        if args[:2] == ["container", "inspect"]:
            table = {"new": self.target, **{c["Name"].lstrip("/"): c for c in self.active}}
            return json.dumps([table[name] for name in args[2:]])
        if args[:2] == ["ps", "--quiet"]:
            return "\n".join(c["Name"].lstrip("/") for c in self.active)
        if args[0] == "context":
            return "unix:///var/run/docker.sock"
        if args[0] == "info":
            return "runc"
        raise AssertionError(args)

    def budgets(self, **values: str) -> config.Config:
        return replace(self.cfg, launcher=replace(self.cfg.launcher, **values))

    def check(self, cfg: config.Config | None = None) -> str:
        with contextlib.redirect_stderr(io.StringIO()) as err:
            resources.check(self.docker, "new", cfg or self.cfg)
        return err.getvalue()

    def test_running_vms_are_charged_by_use_not_limits(self) -> None:
        self.active = [vm("old"), vm("another", status="paused")]
        self.measure.return_value = replace(self.capacity, memory_available=20 * GIB, shm_available=12 * GIB)
        self.assertEqual(self.check(), "")
        self.assertFalse(any(c.args[0][0] == "ps" for c in self.docker.text.call_args_list))

    def test_vm_that_cannot_grow_to_its_limit_starts_with_a_warning(self) -> None:
        self.measure.return_value = replace(self.capacity, shm_available=8 * GIB)
        warning = self.check()
        self.assertIn("can grow to 10.0 GiB, but /dev/shm has 6.4 GiB free", warning)
        self.assertNotIn("host RAM", warning)

    def test_vm_that_cannot_boot_is_refused(self) -> None:
        for field, free, what in (("shm_available", 3 * GIB, "/dev/shm"), ("memory_available", 7 * GIB, "host RAM")):
            with self.subTest(field=field):
                self.measure.return_value = replace(self.capacity, **{field: free})
                with self.assertRaisesRegex(KbxError, f"needs 2.0 GiB to boot, but {what}"):
                    self.check()

    def test_old_container_limit_is_used_instead_of_current_config(self) -> None:
        self.target["HostConfig"]["Memory"] = 16 * GIB
        self.assertIn("can grow to 18.0 GiB", self.check(self.budgets(memory="1g")))

    def test_clh_avoids_mount_check_but_still_checks_ram(self) -> None:
        mock.patch("kbx.resources.kata_settings", return_value=(2 * GIB, False)).start()
        self.measure.return_value = replace(self.capacity, shm_available=1 * GIB)
        self.assertEqual(self.check(), "")
        self.measure.return_value = replace(self.capacity, memory_available=7 * GIB)
        with self.assertRaisesRegex(KbxError, "host RAM"):
            self.check()

    def test_other_runtime_keeps_shm_check_with_clh_config(self) -> None:
        mock.patch("kbx.resources.kata_settings", return_value=(2 * GIB, False)).start()
        self.target["HostConfig"]["Runtime"] = "custom-kata"
        self.measure.return_value = replace(self.capacity, shm_available=3 * GIB)
        with self.assertRaisesRegex(KbxError, "/dev/shm"):
            self.check()

    def test_high_base_memory_and_override_are_needed_to_boot(self) -> None:
        for base, override in ((6 * GIB, "2g"), (2 * GIB, "6g")):
            with self.subTest(base=base, override=override):
                mock.patch("kbx.resources.kata_settings", return_value=(base, True)).start()
                cfg = replace(self.cfg, runtime=replace(self.cfg.runtime, memory_overhead=override))
                self.measure.return_value = replace(self.capacity, shm_available=7 * GIB)
                with self.assertRaisesRegex(KbxError, "needs 6.0 GiB to boot"):
                    self.check(cfg)

    def test_explicit_memory_budget_caps_limits_but_cannot_remove_reserve(self) -> None:
        self.active = [vm("old")]
        self.check(self.budgets(memory_budget="20g"))
        self.active.append(vm("another", status="paused"))
        with self.assertRaisesRegex(KbxError, "would total 30.0 GiB"):
            self.check(self.budgets(memory_budget="20g"))
        with self.assertRaisesRegex(KbxError, "memory_budget 25.6 GiB"):
            self.check(self.budgets(memory_budget="100g"))

    def test_explicit_shm_budget_does_not_count_touched_vm_pages_twice(self) -> None:
        self.target["HostConfig"]["Memory"] = 4 * GIB
        self.active = [vm("old", memory=4 * GIB)]
        self.measure.return_value = replace(self.capacity, shm_available=11 * GIB)
        self.check(self.budgets(shm_budget="14g"))
        self.measure.return_value = replace(self.capacity, shm_available=7 * GIB)
        with self.assertRaisesRegex(KbxError, "/dev/shm has 9.0 GiB in use.*shm_budget 14.0 GiB"):
            self.check(self.budgets(shm_budget="14g"))

    def test_unlimited_vm_is_refused_only_by_an_explicit_budget(self) -> None:
        self.active = [vm("unlimited", memory=0)]
        self.check()
        with self.assertRaisesRegex(KbxError, "without a positive Docker memory limit"):
            self.check(self.budgets(memory_budget="20g"))

    def test_stopped_and_runc_containers_do_not_count_against_a_budget(self) -> None:
        self.active = [vm("stopped", status="exited"), vm("native", runtime="runc", memory=0), vm("old")]
        self.check(self.budgets(memory_budget="20g"))

    def test_remote_daemon_is_refused(self) -> None:
        with (
            mock.patch.dict("os.environ", {"DOCKER_HOST": "ssh://remote"}),
            self.assertRaisesRegex(KbxError, "local Docker Unix socket"),
        ):
            resources.check(self.docker, "new", self.cfg)

    def test_context_takes_precedence_over_docker_host(self) -> None:
        original = self.text

        def text(args: list[str]) -> str:
            return "ssh://remote" if args[0] == "context" else original(args)

        self.docker.text.side_effect = text
        with (
            mock.patch.dict("os.environ", {"DOCKER_CONTEXT": "remote", "DOCKER_HOST": "unix:///local"}),
            self.assertRaisesRegex(KbxError, "local Docker Unix socket"),
        ):
            resources.check(self.docker, "new", self.cfg)

    def test_incomplete_inspection_cannot_approve_a_start(self) -> None:
        self.docker.text.side_effect = None
        self.docker.text.return_value = "[]"
        with self.assertRaisesRegex(KbxError, "incomplete container inspection"):
            resources.check(self.docker, "new", self.cfg)


class KataSettingsTest(TempHome):
    def test_read_config_or_fall_back_conservatively(self) -> None:
        path = self.temp / "kata.toml"
        with (
            mock.patch("kbx.resources.shutil.which", return_value=None),
            mock.patch("kbx.resources.KATA_CONFIGS", (path,)),
        ):
            self.assertEqual(resources.kata_settings(), (2 * GIB, True))
            path.write_text("[hypervisor.clh]\ndefault_memory = 4096\n")
            self.assertEqual(resources.kata_settings(), (4 * GIB, False))
            path.write_text("[hypervisor.qemu]\ndefault_memory = 3072\n")
            self.assertEqual(resources.kata_settings(), (3 * GIB, True))
            for text in ("[hypervisor.clh]\n[hypervisor.qemu]\n", "bad = [", "[hypervisor.clh]\ndefault_memory = -1"):
                path.write_text(text)
                self.assertEqual(resources.kata_settings(), (2 * GIB, True))

    def test_runtime_rs_does_not_read_a_leftover_go_clh_config(self) -> None:
        go_config = self.temp / "go.toml"
        go_config.write_text("[hypervisor.clh]\ndefault_memory = 2048\n")
        rs_config = self.temp / "rs.toml"
        rs_config.write_text("[hypervisor.qemu]\ndefault_memory = 4096\n")
        with (
            mock.patch("kbx.resources.shutil.which", return_value="/opt/kata/runtime-rs/bin/containerd-shim-kata-v2"),
            mock.patch("kbx.resources.KATA_CONFIGS", (go_config,)),
            mock.patch("kbx.resources.KATA_RS_CONFIGS", (rs_config,)),
        ):
            self.assertEqual(resources.kata_settings(), (4 * GIB, True))


class AdmissionTest(TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.paths = paths.resolve()
        self.cfg = config.Config()
        self.docker = Docker()
        self.add_image()
        self.first = sandbox.for_project(self.temp / "first")
        self.second = sandbox.for_project(self.temp / "second")
        for sb in (self.first, self.second):
            sandbox.ensure_created(self.docker, sb, self.cfg, self.paths)

    def test_concurrent_starts_only_admit_one_vm(self) -> None:
        self.cfg = replace(self.cfg, launcher=replace(self.cfg.launcher, shm_budget="12g"))
        barrier = threading.Barrier(2)

        def start(sb: sandbox.Sandbox) -> str:
            barrier.wait(timeout=5)
            try:
                sandbox.ensure_running(self.docker, sb, timeout=5, config=self.cfg, paths=self.paths)
            except KbxError as exc:
                return str(exc)
            return "started"

        with (
            mock.patch(
                "kbx.resources.capacity", return_value=resources.Capacity(32 * GIB, 24 * GIB, 16 * GIB, 16 * GIB)
            ),
            mock.patch("kbx.resources.kata_settings", return_value=(2 * GIB, True)),
            contextlib.redirect_stdout(io.StringIO()),
            concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool,
        ):
            results = list(pool.map(start, (self.first, self.second)))
        self.assertEqual(results.count("started"), 1)
        self.assertTrue(any("/dev/shm" in result for result in results))
        self.assertEqual(sum(c[0] == "start" for c in self.calls()), 1)

    def test_running_sandbox_reattaches_without_new_admission(self) -> None:
        self.docker.run(["start", self.first.name])
        with mock.patch("kbx.resources.check", side_effect=AssertionError("must not reserve twice")):
            sandbox.ensure_running(self.docker, self.first, timeout=5, config=self.cfg, paths=self.paths)

    def test_refused_start_leaves_container_stopped(self) -> None:
        with (
            mock.patch("kbx.resources.check", side_effect=KbxError("no memory")),
            self.assertRaisesRegex(KbxError, "no memory"),
        ):
            sandbox.ensure_running(self.docker, self.first, timeout=5, config=self.cfg, paths=self.paths)
        self.assertEqual(sandbox.state(self.docker, self.first), "created")
        self.assertFalse(any(c[0] == "start" for c in self.calls()))

    def test_stopping_releases_budget_for_another_project(self) -> None:
        self.cfg = replace(self.cfg, launcher=replace(self.cfg.launcher, shm_budget="12g"))
        with (
            mock.patch(
                "kbx.resources.capacity", return_value=resources.Capacity(32 * GIB, 24 * GIB, 16 * GIB, 16 * GIB)
            ),
            mock.patch("kbx.resources.kata_settings", return_value=(2 * GIB, True)),
        ):
            sandbox.ensure_running(self.docker, self.first, timeout=5, config=self.cfg, paths=self.paths)
            sandbox.stop(self.docker, self.first)
            sandbox.ensure_running(self.docker, self.second, timeout=5, config=self.cfg, paths=self.paths)
        self.assertEqual(sandbox.state(self.docker, self.second), "running")
