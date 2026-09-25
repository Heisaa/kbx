"""Drop service_tier = "fast" (root and profiles) without rewriting other TOML."""

import copy
import os
import re
import tomllib
from pathlib import Path


def has_fast(config):
    if config.get("service_tier") == "fast":
        return True
    return any(isinstance(p, dict) and p.get("service_tier") == "fast" for p in config.get("profiles", {}).values())


def normalize(config):
    """Config with every Fast tier removed and empty profile tables dropped."""
    result = copy.deepcopy(config)
    if result.get("service_tier") == "fast":
        del result["service_tier"]
    profiles = result.get("profiles")
    if isinstance(profiles, dict):
        for name, profile in list(profiles.items()):
            if isinstance(profile, dict) and profile.get("service_tier") == "fast":
                del profile["service_tier"]
            if profile == {}:
                del profiles[name]
        if not profiles:
            del result["profiles"]
    return result


home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex")
path = home / "config.toml"
if not path.exists():
    raise SystemExit(0)
source = path.read_text()
current = tomllib.loads(source)
if not has_fast(current):
    raise SystemExit(0)

# Remove one candidate line at a time and re-parse the whole file. A
# candidate is kept only if the result differs from the current config by
# nothing but removed Fast tiers, so a match inside a multiline string or
# an unrelated table can never be removed.
pattern = re.compile(r'^\s*(?:profiles\.[^.=\s]+\.)?service_tier\s*=\s*"fast"\s*(?:#.*)?$')
lines = source.splitlines(keepends=True)
progress = True
while has_fast(current) and progress:
    progress = False
    for index, line in enumerate(lines):
        if not pattern.match(line):
            continue
        candidate = lines[:index] + lines[index + 1 :]
        try:
            parsed = tomllib.loads("".join(candidate))
        except tomllib.TOMLDecodeError:
            continue
        if parsed != current and normalize(parsed) == normalize(current):
            lines, current, progress = candidate, parsed, True
            break

if has_fast(current):
    raise SystemExit('Cannot safely remove service_tier = "fast"; edit ' + str(path) + " by hand")
path.write_text("".join(lines))
