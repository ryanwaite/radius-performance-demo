"""Thin, explicit wrapper around the Docker and Docker Compose CLIs.

The benchmark driver never talks to the Docker socket directly and never
exposes it to an agent-facing container. Every interaction goes through this
module so command construction, timeouts, and failure handling stay auditable.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from dataclasses import dataclass
from typing import Any, Mapping, Sequence


class DockerError(RuntimeError):
    """Raised when a Docker or Compose command fails or the daemon is absent."""


@dataclass(frozen=True)
class CommandResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


def run(
    args: Sequence[str],
    *,
    env: Mapping[str, str] | None = None,
    timeout: float = 180.0,
    check: bool = True,
    cwd: str | None = None,
) -> CommandResult:
    """Run a command, capturing output, with a mandatory timeout."""
    full_env = dict(os.environ)
    if env:
        full_env.update(env)
    try:
        completed = subprocess.run(
            list(args),
            env=full_env,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as exc:
        raise DockerError(f"command timed out after {timeout}s: {' '.join(args)}") from exc

    result = CommandResult(
        args=tuple(args),
        returncode=completed.returncode,
        stdout=completed.stdout or "",
        stderr=completed.stderr or "",
    )
    if check and not result.ok:
        raise DockerError(
            f"command failed ({result.returncode}): {' '.join(args)}\n"
            f"stdout: {result.stdout.strip()}\nstderr: {result.stderr.strip()}"
        )
    return result


def docker(*args: str, **kwargs: Any) -> CommandResult:
    return run(["docker", *args], **kwargs)


def docker_json(*args: str, **kwargs: Any) -> Any:
    result = docker(*args, **kwargs)
    text = result.stdout.strip()
    if not text:
        return None
    return json.loads(text)


def daemon_info() -> dict[str, str]:
    """Return daemon identity, or raise DockerError when it is unreachable.

    The driver calls this first so a stopped Docker Desktop fails loudly
    instead of being silently interpreted as a clean environment.
    """
    if shutil.which("docker") is None:
        raise DockerError("docker CLI not found on PATH")
    probe = docker(
        "version",
        "--format",
        "{{.Server.Version}}|{{.Server.Os}}|{{.Server.Arch}}",
        check=False,
        timeout=30,
    )
    if not probe.ok or not probe.stdout.strip():
        raise DockerError(
            "Docker daemon is not reachable. Start Docker Desktop and retry.\n"
            f"{probe.stderr.strip() or probe.stdout.strip()}"
        )
    version, os_name, arch = (probe.stdout.strip().split("|") + ["", "", ""])[:3]
    compose = docker("compose", "version", "--short", check=False, timeout=30)
    return {
        "serverVersion": version,
        "serverOs": os_name,
        "serverArch": arch,
        "composeVersion": compose.stdout.strip() if compose.ok else "unknown",
    }
