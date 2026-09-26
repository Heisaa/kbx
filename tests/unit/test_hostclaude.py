"""kbx's own Claude Code for `kbx host`: signature, checksum, update, no network in tests."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import unittest
from pathlib import Path
from unittest import mock

from kbx import hostclaude, paths
from kbx.errors import KbxError
from tests.unit.helpers import TempHome

FIXTURES = Path(__file__).resolve().parent / "fixtures"
MANIFEST = (FIXTURES / "claude-2.1.274-manifest.json").read_bytes()
SIGNATURE = (FIXTURES / "claude-2.1.274-manifest.json.sig").read_bytes()
HAS_GPG = shutil.which("gpg") is not None


@unittest.skipUnless(HAS_GPG, "needs gpg")
class SignatureTest(unittest.TestCase):
    def test_real_release_manifest(self) -> None:
        hostclaude.verify_signature(MANIFEST, SIGNATURE)

    def test_changed_manifest(self) -> None:
        changed = MANIFEST.replace(b'"linux-x64"', b'"linux-x65"')
        with self.assertRaisesRegex(KbxError, "not signed by Anthropic"):
            hostclaude.verify_signature(changed, SIGNATURE)

    def test_other_key(self) -> None:
        with mock.patch.object(hostclaude, "FINGERPRINT", "0" * 40), self.assertRaises(KbxError):
            hostclaude.verify_signature(MANIFEST, SIGNATURE)

    def test_the_repository_key_is_the_pinned_one(self) -> None:
        self.assertIn(b"BEGIN PGP PUBLIC KEY BLOCK", hostclaude.KEY.read_bytes())


class ManifestTest(unittest.TestCase):
    def test_expected(self) -> None:
        checksum, size = hostclaude.expected(MANIFEST, "2.1.274", "linux-x64")
        self.assertEqual(len(checksum), 64)
        self.assertGreater(size or 0, 1 << 20)
        with self.assertRaisesRegex(KbxError, "does not match"):
            hostclaude.expected(MANIFEST, "2.1.275", "linux-x64")
        with self.assertRaisesRegex(KbxError, "no linux-riscv build"):
            hostclaude.expected(MANIFEST, "2.1.274", "linux-riscv")


class DownloadTest(TempHome):
    def setUp(self) -> None:
        super().setUp()
        self.paths = paths.resolve(os.environ)
        self.binary = self.temp / "claude.bin"
        self.binary.write_bytes(b"#!/bin/sh\necho fake\n")

    def manifest(self, version: str, content: bytes) -> bytes:
        entry = {"checksum": hashlib.sha256(content).hexdigest(), "size": len(content)}
        return json.dumps({"version": version, "platforms": {hostclaude.platform_name(): entry}}).encode()

    def server(self, versions: dict[str, str], manifests: dict[str, bytes]) -> hostclaude.Fetch:
        def get(url: str) -> bytes:
            rest = url.removeprefix(hostclaude.BASE + "/")
            if rest in versions:
                return versions[rest].encode()
            version, _, name = rest.partition("/")
            if name == "manifest.json":
                return manifests[version]
            if name == "manifest.json.sig":
                return b"sig"
            raise KbxError(f"cannot download {url}")

        return get

    def test_download_update_and_cleanup(self) -> None:
        with mock.patch.object(hostclaude, "verify_signature"):
            first = self.fetch_version("1.0.0", b"one")
            self.assertEqual(hostclaude.installed(self.paths), ("1.0.0", first))
            self.assertEqual(first.read_bytes(), b"one")
            self.assertTrue(os.access(first, os.X_OK))
            second = self.fetch_version("1.1.0", b"two")
        self.assertEqual(hostclaude.installed(self.paths), ("1.1.0", second))
        self.assertFalse(first.exists(), "the old version is removed")

    def fetch_version(self, version: str, content: bytes, served: bytes | None = None) -> Path:
        source = self.temp / f"serve-{version}"
        source.write_bytes(content if served is None else served)
        get = self.server({"stable": version}, {version: self.manifest(version, content)})
        real = hostclaude.download
        with mock.patch.object(
            hostclaude, "download", side_effect=lambda p, v, g: real(p, v, g, binary_url=source.as_uri())
        ):
            return hostclaude.ensure(self.paths, "stable", update=True, get=get)

    def test_checksum_mismatch_installs_nothing(self) -> None:
        with mock.patch.object(hostclaude, "verify_signature"), self.assertRaisesRegex(KbxError, "does not match"):
            self.fetch_version("1.0.0", b"expected", served=b"tampered")
        self.assertIsNone(hostclaude.installed(self.paths))
        self.assertEqual(list((hostclaude.bin_dir(self.paths) / "1.0.0").iterdir()), [])

    def test_bad_signature_installs_nothing(self) -> None:
        refuse = KbxError("not signed by Anthropic's release key")
        with mock.patch.object(hostclaude, "verify_signature", side_effect=refuse), self.assertRaises(KbxError):
            self.fetch_version("1.0.0", b"one")
        self.assertIsNone(hostclaude.installed(self.paths))

    def test_offline_keeps_the_current_copy(self) -> None:
        with mock.patch.object(hostclaude, "verify_signature"):
            first = self.fetch_version("1.0.0", b"one")

        def offline(url: str) -> bytes:
            raise KbxError("cannot download")

        self.assertEqual(hostclaude.ensure(self.paths, "stable", update=True, get=offline), first)
        self.assertEqual(hostclaude.ensure(self.paths, "stable", update=False, get=offline), first)
        shutil.rmtree(hostclaude.bin_dir(self.paths))
        with self.assertRaises(KbxError):
            hostclaude.ensure(self.paths, "stable", update=True, get=offline)

    def test_bad_version_text(self) -> None:
        get = self.server({"stable": "<html>error</html>"}, {})
        with self.assertRaisesRegex(KbxError, "no version"):
            hostclaude.ensure(self.paths, "stable", update=True, get=get)


if __name__ == "__main__":
    unittest.main()
