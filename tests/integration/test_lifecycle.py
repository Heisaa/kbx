"""Image, init, readiness, services, seeds, skills and inner Docker."""

from __future__ import annotations

import json
import subprocess
import time

from tests.integration.base import SandboxCase


class LifecycleTest(SandboxCase):
    def ready(self) -> dict[str, object]:
        return json.loads(self.out("kbx-init status", user="root"))

    def test_01_agents_are_installed_in_home(self) -> None:
        for agent in ("claude", "codex", "pi"):
            with self.subTest(agent=agent):
                path = self.out(f'readlink -f "$(command -v {agent})"')
                self.assertTrue(path.startswith("/home/agent/"), path)
                self.out(f"{agent} --version")

    def test_02_ready_record_and_services(self) -> None:
        ready = self.ready()
        services = ready["services"]
        assert isinstance(services, dict)
        self.assertEqual(services["dockerd"]["state"], "running")
        self.assertEqual(services["clipboard"]["state"], "running")
        seed = ready["seed"]
        assert isinstance(seed, dict)
        self.assertTrue(all(result["ok"] for result in seed.values()), seed)
        docker = ready["docker"]
        assert isinstance(docker, dict)
        self.assertTrue(docker["ready"], docker)
        # The default; "volume (loop failed)" would mean kbx-init fell back.
        self.assertEqual(docker["storage"], "loop", docker)

    def test_03_inner_docker(self) -> None:
        result = self.sh("docker run --rm hello-world", timeout=600)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Hello from Docker!", result.stdout)
        driver = self.out("docker info --format '{{.Driver}}'")
        self.assertIn(driver, ("overlay2", "overlayfs"))  # overlayfs: containerd snapshotter

    def test_04_seeds_applied(self) -> None:
        config = self.out("cat ~/.codex/config.toml")
        self.assertIn('forced_login_method = "chatgpt"', config)
        self.assertIn('model_provider = "openai"', config)

    def test_05_skills_link_and_live_restage(self) -> None:
        self.assertEqual(self.out("readlink ~/.claude/skills"), "/opt/kbx/stage/skills")
        self.assertIn("v1", self.out("cat ~/.agents/skills/hello/SKILL.md"))
        (self.home / ".agents/skills/hello/SKILL.md").write_text("---\nname: hello\n---\nv2\n")
        self.assertEqual(self.kbx("seed", "--status").returncode, 0)  # restages
        self.assertIn("v2", self.out("cat ~/.claude/skills/hello/SKILL.md"))
        self.assertNotEqual(self.sh("touch /opt/kbx/stage/x").returncode, 0, "the stage must be read-only")

    def test_06_clipboard_round_trip(self) -> None:
        png = b"\x89PNG\r\n\x1a\n" + b"integration" * 100
        put = subprocess.run(
            ["docker", "exec", "-i", "-u", "agent", self.name, "kbx-clip-put", "image/png"],
            input=png,
            capture_output=True,
            check=False,
        )
        self.assertEqual(put.returncode, 0, put.stderr)
        time.sleep(1)
        self.assertIn("image/png", self.out("xclip -selection clipboard -t TARGETS -o"))
        got = subprocess.run(
            [
                "docker",
                "exec",
                "-u",
                "agent",
                "-e",
                "DISPLAY=:0",
                self.name,
                "xclip",
                "-selection",
                "clipboard",
                "-t",
                "image/png",
                "-o",
            ],
            capture_output=True,
            check=False,
        )
        self.assertEqual(got.stdout, png)
        self.assertNotEqual(self.sh("xclip -selection clipboard -t text/plain -o").returncode, 0)

    def test_07_home_survives_stop_start_and_recreate(self) -> None:
        self.out("mkdir -p ~/work && echo keep > ~/work/marker")
        before = self.ready()["boot_id"]
        self.assertEqual(self.kbx("stop").returncode, 0)
        result = self.kbx("start")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.out("cat ~/work/marker"), "keep")
        after = self.ready()
        self.assertEqual(after["services"]["dockerd"]["state"], "running")  # type: ignore[index]
        # Under Kata every start is a new VM boot; under runc the boot id is the host's.
        self.assertIsInstance(before, str)
        self.assertEqual(self.kbx("recreate").returncode, 0)
        self.assertEqual(self.kbx("start").returncode, 0)
        self.assertEqual(self.out("cat ~/work/marker"), "keep")
        self.assertTrue(self.out(f"ls {self.repo}/README.md"))  # mount mode: the checkout
