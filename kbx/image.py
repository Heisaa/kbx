"""Generate the Dockerfile (core + one layer per module with a build.sh) and build it."""

from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
import time
from collections.abc import Iterable
from pathlib import Path

from .config import Config
from .docker import Docker
from .errors import KbxError
from .modules import Resolved
from .paths import Paths

MODULES_LABEL = "kbx.modules-hash"
CORE_LABEL = "kbx.core-hash"
MODULE_LABEL_PREFIX = "kbx.module."

# Files that make up the core image; a change to any of them means rebuild.
CORE_SOURCES = ("image", "kbx_sandbox")


def _files(root: Path) -> Iterable[Path]:
    for current, dirs, files in os.walk(root):
        dirs[:] = sorted(d for d in dirs if d != "__pycache__")
        for name in sorted(files):
            if not name.endswith(".pyc"):
                yield Path(current) / name


def core_hash(checkout: Path) -> str:
    digest = hashlib.sha256()
    for part in CORE_SOURCES:
        for path in _files(checkout / part):
            digest.update(str(path.relative_to(checkout)).encode() + b"\0")
            digest.update(path.read_bytes() + b"\0")
    return digest.hexdigest()[:16]


def build_modules(resolved: list[Resolved]) -> list[Resolved]:
    return [item for item in resolved if item.module.has_build]


def module_hash(item: Resolved) -> str:
    """Hash of one module's build.sh, build/ files and option values."""
    digest = hashlib.sha256()
    digest.update((item.module.path / "build.sh").read_bytes() + b"\0")
    build_dir = item.module.path / "build"
    if build_dir.is_dir():
        for path in _files(build_dir):
            digest.update(str(path.relative_to(build_dir)).encode() + b"\0" + path.read_bytes())
    for key, value in sorted(item.options.items()):
        digest.update(f"{key}={value}\0".encode())
    return digest.hexdigest()[:16]


def modules_hash(resolved: list[Resolved]) -> str:
    digest = hashlib.sha256()
    for item in build_modules(resolved):
        digest.update(f"{item.name}={module_hash(item)}\0".encode())
    return digest.hexdigest()[:16]


def render(checkout: Path, resolved: list[Resolved]) -> str:
    core = (checkout / "image" / "Dockerfile.core").read_text(encoding="utf-8")
    lines = [core.rstrip("\n"), ""]
    for item in build_modules(resolved):
        lines.append(f"# --- module: {item.name} ({item.module.source}) ---")
        env = item.option_env()
        for key in env:
            lines.append(f"ARG {key}")
        lines.append(
            f"RUN --mount=type=bind,source=modules/{item.name},target=/opt/kbx/build-module \\\n"
            f"    KBX_MODULE={item.name} KBX_MODULE_DIR=/opt/kbx/build-module \\\n"
            f"    bash /opt/kbx/build-module/build.sh"
        )
        lines.append("")
    lines += [
        "# The agents' home directory, copied into each new sandbox's home volume by kbx-init.",
        "COPY --from=agents --chown=1000:1000 /home/agent/ /opt/kbx/home-template/",
        "",
    ]
    return "\n".join(lines)


def _context(checkout: Path, resolved: list[Resolved], target: Path) -> None:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for part in CORE_SOURCES:
        shutil.copytree(checkout / part, target / part, ignore=ignore)
    for item in build_modules(resolved):
        dest = target / "modules" / item.name
        dest.mkdir(parents=True)
        shutil.copy2(item.module.path / "build.sh", dest / "build.sh")
        if (item.module.path / "build").is_dir():
            shutil.copytree(item.module.path / "build", dest / "build", ignore=ignore, symlinks=False)
    (target / "Dockerfile").write_text(render(checkout, resolved), encoding="utf-8")


def build(
    docker: Docker,
    paths: Paths,
    config: Config,
    resolved: list[Resolved],
    *,
    no_cache: bool = False,
    pull: bool = False,
    refresh_agents: bool = False,
) -> None:
    labels = {
        MODULES_LABEL: modules_hash(resolved),
        CORE_LABEL: core_hash(paths.checkout),
    }
    for item in build_modules(resolved):
        labels[MODULE_LABEL_PREFIX + item.name] = module_hash(item)
    with tempfile.TemporaryDirectory(prefix="kbx-build-") as temp:
        context = Path(temp)
        _context(paths.checkout, resolved, context)
        args = ["build", "-t", config.image.name, "-f", str(context / "Dockerfile")]
        for key, value in labels.items():
            args += ["--label", f"{key}={value}"]
        for item in build_modules(resolved):
            for key, value in item.option_env().items():
                args += ["--build-arg", f"{key}={value}"]
        if refresh_agents:
            args += ["--build-arg", f"KBX_AGENTS_REFRESH={int(time.time())}"]
        if no_cache:
            args.append("--no-cache")
        if pull:
            args.append("--pull")
        args.append(str(context))
        env_note = "" if os.environ.get("DOCKER_BUILDKIT", "1") != "0" else " (BuildKit is required)"
        print(f"→ Building {config.image.name}{env_note}")
        status = docker.run(args, stdout=None, stderr=None, check=False).returncode
        if status != 0:
            raise KbxError(f"docker build failed (exit {status})")
    print(f"✓ Built {config.image.name}")


def image_labels(docker: Docker, name: str) -> dict[str, str] | None:
    data = docker.inspect("image", name)
    if data is None:
        return None
    labels = (data.get("Config") or {}).get("Labels") or {}
    return {str(k): str(v) for k, v in labels.items()}


def drift(docker: Docker, paths: Paths, config: Config, resolved: list[Resolved]) -> list[str]:
    """Warnings when the image no longer matches the modules or core sources."""
    labels = image_labels(docker, config.image.name)
    if labels is None:
        return []
    warnings: list[str] = []
    if labels.get(MODULES_LABEL) != modules_hash(resolved):
        built = {
            key[len(MODULE_LABEL_PREFIX) :]: value
            for key, value in labels.items()
            if key.startswith(MODULE_LABEL_PREFIX)
        }
        wanted = {item.name: module_hash(item) for item in build_modules(resolved)}
        for name in sorted(set(built) | set(wanted)):
            if name not in built:
                warnings.append(f"module {name} was enabled; run `kbx build && kbx recreate`")
            elif name not in wanted:
                warnings.append(f"module {name} was disabled; run `kbx build && kbx recreate`")
            elif built[name] != wanted[name]:
                warnings.append(f"module {name} changed; run `kbx build && kbx recreate`")
    if labels.get(CORE_LABEL) != core_hash(paths.checkout):
        warnings.append("the core image sources changed; run `kbx build && kbx recreate`")
    return warnings
