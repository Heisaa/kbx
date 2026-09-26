"""kbx's own copy of Claude Code for `kbx host`, never on PATH.

`kbx host` does not use a Claude Code installed on the host. It keeps its own
copy in $XDG_DATA_HOME/kbx/host-claude-bin/<version>/claude, so no `claude`
command exists that runs outside kbx's lockdown by mistake.

Download, as the official installer does but without its `claude install`
step (which creates ~/.local/bin/claude): the channel's version from
downloads.claude.ai, that version's manifest.json, the binary for this
platform, its SHA-256 against the manifest. In addition, the manifest's
detached PGP signature must be good and made by Anthropic's release key,
kept in this repository (host/claude-code-release.asc) and pinned by
fingerprint, so a changed download server alone cannot swap the binary.

The copy never updates itself: Claude's auto-updater would install a regular
copy into ~/.local/bin (it does, within a minute, when left on), so `kbx host`
runs it with DISABLE_AUTOUPDATER, DISABLE_UPDATES and
DISABLE_INSTALLATION_CHECKS. kbx updates it at launch instead, when
[launcher] auto_update is on.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from .errors import KbxError
from .paths import CHECKOUT, Paths

BASE = "https://downloads.claude.ai/claude-code-releases"
KEY = CHECKOUT / "host" / "claude-code-release.asc"
# "Anthropic Claude Code Release Signing <security@anthropic.com>"
FINGERPRINT = "31DDDE24DDFAB679F42D7BD2BAA929FF1A7ECACE"
VERSION = re.compile(r"^[0-9]+\.[0-9]+\.[0-9]+(-[0-9A-Za-z.-]+)?$")
SHA256 = re.compile(r"^[0-9a-f]{64}$")
MAX_MANIFEST = 1 << 20
TIMEOUT = 60
# What keeps the copy from installing or updating itself outside kbx.
NO_SELF_UPDATE = {"DISABLE_AUTOUPDATER": "1", "DISABLE_UPDATES": "1", "DISABLE_INSTALLATION_CHECKS": "1"}

Fetch = Callable[[str], bytes]


def bin_dir(paths: Paths) -> Path:
    return paths.data_dir / "host-claude-bin"


def installed(paths: Paths) -> tuple[str, Path] | None:
    """The current copy: (version, binary), or None."""
    try:
        version = (bin_dir(paths) / "current").read_text().strip()
    except OSError:
        return None
    binary = bin_dir(paths) / version / "claude"
    if not VERSION.match(version) or not os.access(binary, os.X_OK):
        return None
    return version, binary


def platform_name() -> str:
    if not sys.platform.startswith("linux"):
        raise KbxError("kbx host needs Linux")
    arch = {"x86_64": "x64", "amd64": "x64", "aarch64": "arm64", "arm64": "arm64"}.get(platform.machine().lower())
    if arch is None:
        raise KbxError(f"no Claude Code build for {platform.machine()}")
    musl = any(Path(f"/lib/libc.musl-{m}.so.1").exists() for m in ("x86_64", "aarch64"))
    return f"linux-{arch}-musl" if musl else f"linux-{arch}"


def fetch(url: str, limit: int = MAX_MANIFEST) -> bytes:
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT) as response:  # noqa: S310 (fixed https base)
            data = response.read(limit + 1)
    except (OSError, urllib.error.URLError) as exc:
        raise KbxError(f"cannot download {url}: {exc}") from None
    if len(data) > limit:
        raise KbxError(f"{url} is larger than expected")
    return data


def channel_version(channel: str, get: Fetch = fetch) -> str:
    version = get(f"{BASE}/{channel}").decode("utf-8", "replace").strip()
    if not VERSION.match(version):
        raise KbxError(f"downloads.claude.ai gave no version for {channel!r} (unreachable, or not in your region?)")
    return version


def verify_signature(manifest: bytes, signature: bytes, key: Path = KEY) -> None:
    """The manifest must carry a good signature by the pinned release key."""
    if shutil.which("gpg") is None:
        raise KbxError("verifying Claude Code's release signature needs gpg on the host (package gnupg)")
    with tempfile.TemporaryDirectory(prefix="kbx-gpg-") as home:
        base = ["gpg", "--homedir", home, "--batch", "--no-autostart", "--no-tty"]
        imported = subprocess.run([*base, "--import", str(key)], capture_output=True, timeout=60, check=False)
        if imported.returncode != 0:
            raise KbxError(f"cannot import {key}")
        (Path(home) / "manifest.json").write_bytes(manifest)
        (Path(home) / "manifest.json.sig").write_bytes(signature)
        result = subprocess.run(
            [*base, "--status-fd", "1", "--verify", f"{home}/manifest.json.sig", f"{home}/manifest.json"],
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )
    # VALIDSIG <signing key fpr> … <primary key fpr>: a good signature, whatever the trust db says.
    for line in result.stdout.splitlines():
        fields = line.split()
        if fields[:2] == ["[GNUPG:]", "VALIDSIG"] and fields[-1].upper() == FINGERPRINT:
            return
    raise KbxError("Claude Code's release manifest is not signed by Anthropic's release key; not installing it")


def expected(manifest: bytes, version: str, name: str) -> tuple[str, int | None]:
    """(sha256, size) of this platform's binary from a verified manifest."""
    try:
        data: Any = json.loads(manifest)
        entry = data["platforms"][name]
        checksum = str(entry["checksum"])
    except (ValueError, KeyError, TypeError):
        raise KbxError(f"Claude Code {version} has no {name} build in its manifest") from None
    if data.get("version") != version or not SHA256.match(checksum):
        raise KbxError(f"Claude Code's manifest for {version} does not match")
    size = entry.get("size")
    return checksum, size if isinstance(size, int) and size > 0 else None


def download(paths: Paths, version: str, get: Fetch = fetch, binary_url: str | None = None) -> Path:
    """Fetch, verify and store one version; returns the binary."""
    name = platform_name()
    manifest = get(f"{BASE}/{version}/manifest.json")
    verify_signature(manifest, get(f"{BASE}/{version}/manifest.json.sig"))
    checksum, size = expected(manifest, version, name)
    target = bin_dir(paths) / version
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, temp = tempfile.mkstemp(dir=target, prefix=".claude-")
    digest = hashlib.sha256()
    total = 0
    url = binary_url or f"{BASE}/{version}/{name}/claude"
    try:
        with os.fdopen(fd, "wb") as out, urllib.request.urlopen(url, timeout=TIMEOUT) as response:  # noqa: S310
            while chunk := response.read(1 << 20):
                total += len(chunk)
                if size is not None and total > size:
                    raise KbxError("the Claude Code download is larger than its manifest says")
                digest.update(chunk)
                out.write(chunk)
        if digest.hexdigest() != checksum or (size is not None and total != size):
            raise KbxError("the Claude Code download does not match its signed checksum; not installing it")
        os.chmod(temp, 0o755)
        os.replace(temp, target / "claude")
    except (OSError, urllib.error.URLError) as exc:
        raise KbxError(f"downloading Claude Code {version} failed: {exc}") from None
    finally:
        Path(temp).unlink(missing_ok=True)
    return target / "claude"


def activate(paths: Paths, version: str) -> None:
    """Make `version` current and remove the others."""
    current = bin_dir(paths) / "current"
    temp = current.with_name("current.tmp")
    temp.write_text(version + "\n")
    os.replace(temp, current)
    for entry in bin_dir(paths).iterdir():
        if entry.is_dir() and entry.name != version:
            shutil.rmtree(entry, ignore_errors=True)


def ensure(paths: Paths, channel: str, *, update: bool, get: Fetch = fetch) -> Path:
    """The binary to run: downloaded if missing, updated if asked and newer."""
    have = installed(paths)
    if have is not None and not update:
        return have[1]
    try:
        version = channel_version(channel, get)
    except KbxError:
        if have is None:
            raise
        print(f"⚠ could not check for a newer Claude Code; using {have[0]}", file=sys.stderr)
        return have[1]
    if have is not None and have[0] == version:
        return have[1]
    action = f"Updating kbx's Claude Code {have[0]} → {version}" if have else f"Downloading Claude Code {version}"
    print(f"→ {action} (only for kbx host; nothing goes on your PATH)…", flush=True)
    try:
        binary = download(paths, version, get)
    except KbxError as exc:
        if have is None:
            raise
        print(f"⚠ {exc}; using {have[0]}", file=sys.stderr)
        return have[1]
    activate(paths, version)
    print(f"✓ Claude Code {version}, signature and checksum verified")
    return binary
