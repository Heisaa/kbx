"""Mount mode: secret-looking files in the checkout that the agent can read.

kbx does not hide them (the agent is root in its VM and could undo any mask
there). It lists them once, before a mount-mode sandbox is created, and in a
terminal asks whether to go on, so you can move real secrets out before the
agent sees the checkout.

Only untracked files count (ignored ones included): committed files travel
with the repository anyway, and `.env.example` and the like are templates.
Fully untracked directories are judged by their name only (`secrets/`), so
this is a reminder, not an audit.
"""

from __future__ import annotations

import re
from fnmatch import fnmatchcase
from pathlib import Path

from .git import host_git

PATTERNS = (
    ".env", ".env.*", "*.env", ".envrc",
    "*.pem", "*.key", "*.p12", "*.pfx", "*.jks", "*.keystore",
    "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519",
    "credentials", "credentials.*", "*credentials*.json", "service-account*.json", "serviceaccount*.json",
    "client_secret*.json",
    "secrets", ".secrets", "secrets.*",
    ".netrc", ".npmrc", ".pypirc", ".git-credentials", ".dockercfg",
    "*.tfvars", "*.tfstate", "*.tfstate.*",
    "master.key",
    ".aws", ".ssh", ".gnupg",
)  # fmt: skip
TEMPLATE_WORDS = frozenset({"example", "sample", "template", "dist", "defaults"})
SHOWN = 10


def looks_secret(rel: str) -> bool:
    name = rel.rstrip("/").rsplit("/", 1)[-1].lower()
    if TEMPLATE_WORDS.intersection(re.split(r"[._-]", name)):
        return False
    return any(fnmatchcase(name, pattern) for pattern in PATTERNS)


def secret_files(project: Path) -> list[str]:
    """Untracked paths in the checkout whose names look like secrets; [] if git fails."""
    # --directory lists a fully untracked directory (node_modules/, secrets/) as one entry.
    result = host_git(project, "ls-files", "-z", "--others", "--directory", "--no-empty-directory", check=False)
    if result.returncode != 0:
        return []
    return sorted(rel for rel in result.stdout.split("\0") if rel and looks_secret(rel))


def warning(project: Path) -> str | None:
    found = secret_files(project)
    if not found:
        return None
    listed = [f"    {rel}" for rel in found[:SHOWN]]
    if len(found) > SHOWN:
        listed.append(f"    … and {len(found) - SHOWN} more")
    return "\n".join(
        [
            "the agent will be able to read these files in your checkout, and the internet is open, "
            "so a prompt-injected agent could send them out:",
            *listed,
            "  Keep only secrets here you could afford to lose (test or spend-limited keys), "
            'or use [workspace] mode = "clone", which leaves untracked files on the host.',
        ]
    )
