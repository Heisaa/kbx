"""Network isolation: internet yes; host, LAN, link-local and metadata no.

Needs the host firewall (host/install-firewall). The firewall-toggle test
also needs KBX_TEST_FIREWALL_TOGGLE=1 and passwordless sudo, because it stops
and restarts kbx-firewall.service.
"""

from __future__ import annotations

import http.server
import ipaddress
import os
import shutil
import subprocess
import threading
import unittest

from tests.integration.base import SandboxCase, firewall_active


def host_addresses() -> list[str]:
    """This host's global IPv4 addresses (LAN, VPN…), excluding Docker bridges."""
    if not shutil.which("ip"):
        raise unittest.SkipTest("needs iproute2 (ip) on the host")
    out = subprocess.run(
        ["ip", "-4", "-o", "addr", "show", "scope", "global"], capture_output=True, text=True, check=False
    ).stdout
    addresses: list[str] = []
    for line in out.splitlines():
        fields = line.split()
        if len(fields) > 3 and not fields[1].startswith(("docker", "br-", "veth")):
            addresses.append(fields[3].split("/")[0])
    return addresses


def default_gateway() -> str | None:
    if not shutil.which("ip"):
        return None
    out = subprocess.run(
        ["ip", "-4", "route", "show", "default"], capture_output=True, text=True, check=False
    ).stdout.split()
    return out[out.index("via") + 1] if "via" in out else None


PROBE = """
import socket, sys
s = socket.socket(); s.settimeout(3)
try:
    s.connect((sys.argv[1], int(sys.argv[2]))); print("open")
except socket.timeout:
    print("dropped")
except OSError as e:
    print("error", e.errno)
"""


@unittest.skipUnless(firewall_active(), "kbx firewall not active on this host")
class NetworkTest(SandboxCase):
    def probe(self, host: str, port: int) -> str:
        return self.out(f"python3 -c '{PROBE}' {host} {port}")

    def test_internet_is_reachable(self) -> None:
        for url in (
            "https://api.anthropic.com",
            "https://chatgpt.com",
            "https://registry.npmjs.org",
            "https://github.com",
        ):
            with self.subTest(url=url):
                result = self.sh(f"curl -s -o /dev/null -m 15 -w '%{{http_code}}' {url}")
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotEqual(result.stdout, "000")

    def test_gateway_is_blocked(self) -> None:
        for port in (22, 53, 80, 2375, 8000):
            with self.subTest(port=port):
                self.assertEqual(self.probe("172.30.0.1", port), "dropped")
        self.assertNotEqual(self.sh("ping -c1 -W2 172.30.0.1").returncode, 0)

    def test_metadata_and_private_ranges_are_blocked(self) -> None:
        targets = ["169.254.169.254", "192.168.1.1", "10.0.0.1", "100.100.100.100", *host_addresses()]
        gateway = default_gateway()
        if gateway:
            targets.append(gateway)
        for host in targets:
            with self.subTest(host=host):
                self.assertNotEqual(self.probe(host, 80), "open")

    def test_host_services_are_blocked(self) -> None:
        server = http.server.ThreadingHTTPServer(("0.0.0.0", 8000), http.server.SimpleHTTPRequestHandler)  # noqa: S104
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            for host in ["172.30.0.1", *host_addresses()]:
                if ipaddress.ip_address(host).is_loopback:
                    continue
                with self.subTest(host=host):
                    self.assertNotEqual(self.probe(host, 8000), "open")
        finally:
            server.shutdown()
            server.server_close()

    def test_host_docker_internal_does_not_resolve(self) -> None:
        self.assertNotEqual(self.sh("getent hosts host.docker.internal").returncode, 0)

    def test_inner_containers_are_blocked_too(self) -> None:
        for host in host_addresses()[:2] or ["192.168.1.1"]:
            with self.subTest(host=host):
                result = self.sh(f"docker run --rm alpine wget -q -T 3 -O /dev/null http://{host}:8000/", timeout=300)
                self.assertNotEqual(result.returncode, 0)

    @unittest.skipUnless(os.environ.get("KBX_TEST_FIREWALL_TOGGLE") == "1", "set KBX_TEST_FIREWALL_TOGGLE=1")
    def test_launch_refuses_without_firewall(self) -> None:
        subprocess.run(["sudo", "-n", "systemctl", "stop", "kbx-firewall.service"], check=True)
        try:
            result = self.kbx("start")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("host firewall is not active", result.stderr)
        finally:
            subprocess.run(["sudo", "-n", "systemctl", "start", "kbx-firewall.service"], check=True)
        self.assertEqual(self.kbx("start").returncode, 0)
