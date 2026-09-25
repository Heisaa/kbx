from __future__ import annotations

import json
import os
import pwd
import shutil
import socket
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from kbx import clipd, paths
from kbx_sandbox import init, session


def current_user() -> init.User:
    return init.User.lookup(pwd.getpwuid(os.getuid()).pw_name)


class SupervisorTest(unittest.TestCase):
    def setUp(self) -> None:
        quiet = mock.patch.object(init, "log", lambda message: None)
        quiet.start()
        self.addCleanup(quiet.stop)
        self.temp = Path(tempfile.mkdtemp(prefix="kbx-init-"))
        self.addCleanup(shutil.rmtree, self.temp, True)
        patcher = mock.patch.object(init, "SERVICES", self.temp / "services.json")
        patcher.start()
        self.addCleanup(patcher.stop)

    def service(self, name: str, script: str) -> init.Service:
        user = current_user()
        return init.Service(
            name, ["sh", "-c", script], user, {"PATH": "/usr/bin:/bin"}, str(self.temp), self.temp / f"{name}.log"
        )

    def test_backoff_then_failed(self) -> None:
        service = self.service("x", "exit 3")
        for _ in range(init.MAX_RESTARTS):
            service.exited(3)
            self.assertEqual(service.state, "backoff")
        service.exited(3)
        self.assertEqual(service.state, "failed")
        self.assertEqual(service.last_status, 3)

    def test_backoff_grows(self) -> None:
        service = self.service("x", "exit 1")
        delays: list[float] = []
        for _ in range(4):
            before = time.monotonic()
            service.exited(1)
            delays.append(round(service.next_start - before))
        self.assertEqual(delays, [1, 2, 4, 8])

    def test_restarts_a_crashing_service(self) -> None:
        crashing = self.service("crash", "echo run >> runs; exit 1")
        steady = self.service("steady", "sleep 30")
        supervisor = init.Supervisor([crashing, steady])
        supervisor.start_all()
        runs = self.temp / "runs"
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline and (not runs.exists() or runs.read_text().count("run") < 2):
            supervisor.step()
            time.sleep(0.1)
        self.assertGreaterEqual((self.temp / "runs").read_text().count("run"), 2)
        self.assertEqual(steady.state, "running")
        status = json.loads((self.temp / "services.json").read_text())
        self.assertEqual(set(status), {"crash", "steady"})
        supervisor.shutdown()
        self.assertEqual(steady.state, "stopped")

    def test_status_requires_current_boot(self) -> None:
        ready = self.temp / "ready"
        with mock.patch.object(init, "READY", ready), mock.patch.object(init, "boot_id", return_value="now"):
            self.assertEqual(init.status(), 1)
            ready.write_text(json.dumps({"boot_id": "earlier"}))
            self.assertEqual(init.status(), 1)
            ready.write_text(json.dumps({"boot_id": "now"}))
            with mock.patch("sys.stdout"):
                self.assertEqual(init.status(), 0)

    def test_size_parsing(self) -> None:
        self.assertEqual(init._size_bytes("64g"), 64 << 30)
        self.assertEqual(init._size_bytes("512M"), 512 << 20)


class SessionTest(unittest.TestCase):
    def test_live_and_stale_sockets(self) -> None:
        root = Path(tempfile.mkdtemp(prefix="kbx-s-"))
        self.addCleanup(shutil.rmtree, root, True)
        live = socket.socket(socket.AF_UNIX)
        live.bind(str(root / "claude.sock"))
        live.listen(1)
        self.addCleanup(live.close)
        stale = socket.socket(socket.AF_UNIX)
        stale.bind(str(root / "codex.sock"))
        stale.close()  # the socket file stays, nothing listens
        (root / "pi.sock").write_text("not a socket")
        self.assertEqual(session.live_sessions(root), ["claude"])


class ClipdRegistrationTest(unittest.TestCase):
    def test_register_and_liveness(self) -> None:
        home = Path(tempfile.mkdtemp(prefix="kbx-c-"))
        self.addCleanup(shutil.rmtree, home, True)
        p = paths.resolve({"HOME": str(home), "XDG_RUNTIME_DIR": str(home / "run")})
        entry = home / "fake-entry"
        entry.write_text("import time; time.sleep(0.1)\n")
        clipd.register(p, "kbx-x-1", os.getpid(), entry)
        lock, pids, pidfile = clipd._files(p, "kbx-x-1")
        self.assertIn(str(os.getpid()), pids.read_text())
        self.assertTrue(pidfile.exists())
        self.assertEqual(clipd._live_attaches(pids, lock), 1)
        pids.write_text("999999 123\n")
        self.assertEqual(clipd._live_attaches(pids, lock), 0)

    def test_backend_detection(self) -> None:
        self.assertIsNone(clipd.backend({}))
        self.assertIn("wl-clipboard", clipd.missing_tools_hint({"WAYLAND_DISPLAY": "wayland-0"}))
