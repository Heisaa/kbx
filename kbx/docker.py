"""The only place that runs `docker`. Tests replace the binary on PATH."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from collections.abc import Mapping, Sequence
from typing import IO, Any

from .errors import KbxError

AGENT = "agent"
ROOT = "root"


class Docker:
    def __init__(self, binary: str | None = None, verbose: bool = False) -> None:
        self.binary = binary or os.environ.get("KBX_DOCKER", "docker")
        self.verbose = verbose

    def _resolve(self) -> str:
        found = shutil.which(self.binary)
        if not found:
            raise KbxError(f"{self.binary!r} not found on PATH; install Docker Engine first")
        return found

    def run(
        self,
        args: Sequence[str],
        *,
        input: bytes | None = None,
        stdin: IO[bytes] | int | None = None,
        stdout: IO[bytes] | int | None = subprocess.PIPE,
        stderr: IO[bytes] | int | None = subprocess.PIPE,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        argv = [self._resolve(), *args]
        # Output of ours must appear before whatever docker writes to the terminal.
        sys.stdout.flush()
        sys.stderr.flush()
        if self.verbose:
            print("+ docker " + " ".join(args[:6]) + (" …" if len(args) > 6 else ""))
        try:
            result = subprocess.run(
                argv,
                input=input,
                stdin=stdin if input is None else None,
                stdout=stdout,
                stderr=stderr,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            raise KbxError(f"docker {args[0]} timed out after {timeout:.0f}s") from None
        if check and result.returncode != 0:
            raise KbxError(f"docker {' '.join(args[:2])} failed: {_last_line(result.stderr)}")
        return result

    def text(self, args: Sequence[str], **kwargs: Any) -> str:
        return self.run(args, **kwargs).stdout.decode("utf-8", "replace").strip()

    def ok(self, args: Sequence[str], timeout: float | None = None) -> bool:
        return self.run(args, check=False, timeout=timeout).returncode == 0

    def inspect(self, kind: str, name: str) -> dict[str, Any] | None:
        result = self.run([kind, "inspect", name], check=False)
        if result.returncode != 0:
            return None
        data = json.loads(result.stdout or b"[]")
        return data[0] if data else None

    def inspect_many(self, kind: str, names: Sequence[str]) -> list[dict[str, Any]]:
        """One call for several objects; missing ones are left out."""
        if not names:
            return []
        result = self.run([kind, "inspect", *names], check=False)
        try:
            data = json.loads(result.stdout or b"[]")
        except ValueError:
            return []
        return [item for item in data if isinstance(item, dict)] if isinstance(data, list) else []  # pyright: ignore[reportUnknownVariableType]

    @staticmethod
    def exec_args(
        name: str,
        argv: Sequence[str],
        *,
        user: str = AGENT,
        workdir: str | None = None,
        env: Mapping[str, str] | None = None,
        interactive: bool = False,
        tty: bool = False,
    ) -> list[str]:
        args = ["exec"]
        if interactive:
            args.append("-i")
        if tty:
            args.append("-t")
        args += ["-u", user]
        if workdir:
            args += ["-w", workdir]
        for key, value in (env or {}).items():
            args += ["-e", f"{key}={value}"]
        return [*args, name, *argv]

    def exec(
        self,
        name: str,
        argv: Sequence[str],
        *,
        user: str = AGENT,
        workdir: str | None = None,
        env: Mapping[str, str] | None = None,
        input: bytes | None = None,
        stdin: IO[bytes] | int | None = None,
        stdout: IO[bytes] | int | None = subprocess.PIPE,
        stderr: IO[bytes] | int | None = subprocess.PIPE,
        check: bool = True,
        timeout: float | None = None,
    ) -> subprocess.CompletedProcess[bytes]:
        interactive = input is not None or stdin is not None
        args = self.exec_args(name, argv, user=user, workdir=workdir, env=env, interactive=interactive)
        return self.run(
            args,
            input=input,
            stdin=stdin,
            stdout=stdout,
            stderr=stderr,
            check=check,
            timeout=timeout,
        )

    def exec_text(self, name: str, argv: Sequence[str], **kwargs: Any) -> str:
        return self.exec(name, argv, **kwargs).stdout.decode("utf-8", "replace").strip()

    def exec_passthrough(self, name: str, argv: Sequence[str], **kwargs: Any) -> int:
        """Run with the terminal's stdout/stderr, returning the exit status."""
        return self.exec(name, argv, stdout=None, stderr=None, check=False, **kwargs).returncode

    def binary_path(self) -> str:
        return self._resolve()


def _last_line(data: bytes | None) -> str:
    lines = [line for line in (data or b"").decode("utf-8", "replace").splitlines() if line.strip()]
    return lines[-1].strip() if lines else "no error output"
