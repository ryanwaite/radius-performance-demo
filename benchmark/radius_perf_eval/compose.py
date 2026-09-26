"""Unique-project Docker Compose control with verifiable teardown.

Nothing here reuses the repository's developer-convenience ``docker-compose.yml``
or ``make local-up``. Every trial gets its own Compose project name, its own
freshly created named volumes, its own network, and dynamically assigned
loopback-only host ports.
"""

from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Mapping, Sequence

from .docker_cli import CommandResult, DockerError, docker, run

COMPOSE_DIR = Path(__file__).resolve().parent / "compose"
BASE_COMPOSE_FILE = COMPOSE_DIR / "base.yml"

_PROJECT_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,62}$")
_PORT_RE = re.compile(r"^(?P<host>.+):(?P<port>\d+)$")


class ComposeError(DockerError):
    """Raised for Compose-specific lifecycle failures."""


@dataclass(frozen=True)
class ResidualResources:
    """What the Docker daemon still reports for a project after teardown."""

    containers: tuple[str, ...] = ()
    volumes: tuple[str, ...] = ()
    networks: tuple[str, ...] = ()
    orphans: tuple[str, ...] = ()

    @property
    def clean(self) -> bool:
        return not (self.containers or self.volumes or self.networks or self.orphans)

    def to_dict(self) -> dict[str, list[str]]:
        return {
            "containers": list(self.containers),
            "volumes": list(self.volumes),
            "networks": list(self.networks),
            "orphans": list(self.orphans),
        }

    def describe(self) -> str:
        if self.clean:
            return "no residual containers, volumes, networks, or orphans"
        return json.dumps(self.to_dict(), sort_keys=True)


def parse_port_mapping(text: str) -> tuple[str, int]:
    """Parse ``docker compose port`` output into (host, port).

    Handles IPv4 (``127.0.0.1:55403``) and bracketed IPv6 (``[::1]:55403``).
    """
    candidate = (text or "").strip().splitlines()
    if not candidate:
        raise ComposeError("empty port mapping output")
    match = _PORT_RE.match(candidate[-1].strip())
    if not match:
        raise ComposeError(f"unparsable port mapping: {text!r}")
    host = match.group("host").strip()
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if host in ("0.0.0.0", "::", ""):
        host = "127.0.0.1"
    port = int(match.group("port"))
    if port <= 0:
        raise ComposeError(f"invalid host port in mapping: {text!r}")
    return host, port


def parse_resource_lines(stdout: str) -> tuple[str, ...]:
    """Normalise ``docker ... ls -q``-style output into a tuple of identifiers."""
    return tuple(line.strip() for line in (stdout or "").splitlines() if line.strip())


@dataclass
class ComposeProject:
    """One ephemeral Compose project belonging to exactly one trial."""

    project: str
    env: dict[str, str]
    files: list[Path] = field(default_factory=lambda: [BASE_COMPOSE_FILE])

    def __post_init__(self) -> None:
        if not _PROJECT_RE.match(self.project):
            raise ComposeError(f"invalid compose project name: {self.project!r}")
        self.files = [Path(f) for f in self.files]
        for file in self.files:
            if not file.is_file():
                raise ComposeError(f"compose file not found: {file}")

    # -- command construction -------------------------------------------------

    def _args(self, files: Sequence[Path] | None = None) -> list[str]:
        selected = list(files) if files is not None else self.files
        args = ["docker", "compose", "--project-name", self.project]
        for file in selected:
            args += ["--file", str(file)]
        return args

    def _run(
        self,
        *args: str,
        files: Sequence[Path] | None = None,
        timeout: float = 300.0,
        check: bool = True,
    ) -> CommandResult:
        return run(self._args(files) + list(args), env=self.env, timeout=timeout, check=check)

    # -- lifecycle ------------------------------------------------------------

    def config(self, files: Sequence[Path] | None = None) -> str:
        """Fully interpolated Compose configuration, used for config hashing."""
        return self._run("config", files=files, timeout=120).stdout

    def config_json(self, files: Sequence[Path] | None = None) -> str:
        """The same configuration as JSON, used to generate the check plan.

        Compose resolves interpolation, merges overlays, and normalises units
        here, so this describes what will actually run rather than what the
        template appears to say.
        """
        return self._run("config", "--format", "json", files=files, timeout=120).stdout

    def up(
        self,
        *,
        files: Sequence[Path] | None = None,
        services: Iterable[str] = (),
        wait_timeout: int = 300,
        force_recreate: bool = False,
    ) -> CommandResult:
        args = ["up", "--detach", "--wait", "--wait-timeout", str(wait_timeout), "--no-build"]
        if force_recreate:
            args.append("--force-recreate")
        args.extend(services)
        return self._run(*args, files=files, timeout=wait_timeout + 120)

    def down(self, *, timeout: int = 120) -> CommandResult:
        return self._run(
            "down",
            "--volumes",
            "--remove-orphans",
            "--timeout",
            "20",
            timeout=timeout,
            check=False,
        )

    # -- inspection -----------------------------------------------------------

    def container_id(self, service: str) -> str:
        result = self._run("ps", "--quiet", service, timeout=60)
        ids = parse_resource_lines(result.stdout)
        if len(ids) != 1:
            raise ComposeError(f"expected exactly one container for {service}, got {ids!r}")
        return ids[0]

    def port(self, service: str, container_port: int) -> tuple[str, int]:
        result = self._run("port", service, str(container_port), timeout=60)
        return parse_port_mapping(result.stdout)

    def endpoint(self, service: str, container_port: int) -> str:
        host, port = self.port(service, container_port)
        return f"http://{host}:{port}"

    def exec(
        self,
        service: str,
        command: Sequence[str],
        *,
        env_vars: Mapping[str, str] | None = None,
        timeout: float = 60.0,
        check: bool = True,
    ) -> CommandResult:
        args = ["exec", "--no-TTY"]
        for key, value in (env_vars or {}).items():
            args += ["--env", f"{key}={value}"]
        return self._run(*args, service, *command, timeout=timeout, check=check)

    def logs(self, service: str, *, tail: int = 200) -> str:
        result = self._run("logs", "--no-color", "--tail", str(tail), service, timeout=60, check=False)
        return result.stdout + result.stderr

    # -- residue --------------------------------------------------------------

    def residual_resources(self) -> ResidualResources:
        """Ask the daemon directly what is left for this project.

        Label filters catch anything Compose created. The extra name-prefix scan
        catches resources whose labels were stripped or that were created out of
        band, which a label-only check would miss.
        """
        label = f"com.docker.compose.project={self.project}"
        prefix = f"{self.project}[-_]"

        containers = set(
            parse_resource_lines(
                docker("ps", "--all", "--quiet", "--filter", f"label={label}", timeout=60).stdout
            )
        )
        named_containers = {
            name
            for name in parse_resource_lines(
                docker(
                    "ps", "--all", "--format", "{{.Names}}", timeout=60
                ).stdout
            )
            if re.match(prefix, name)
        }

        volumes = set(
            parse_resource_lines(
                docker("volume", "ls", "--quiet", "--filter", f"label={label}", timeout=60).stdout
            )
        )
        named_volumes = {
            name
            for name in parse_resource_lines(
                docker("volume", "ls", "--format", "{{.Name}}", timeout=60).stdout
            )
            if re.match(prefix, name)
        }

        networks = set(
            parse_resource_lines(
                docker("network", "ls", "--quiet", "--filter", f"label={label}", timeout=60).stdout
            )
        )
        named_networks = {
            name
            for name in parse_resource_lines(
                docker("network", "ls", "--format", "{{.Name}}", timeout=60).stdout
            )
            if re.match(prefix, name)
        }

        return ResidualResources(
            containers=tuple(sorted(containers | named_containers)),
            volumes=tuple(sorted(volumes | named_volumes)),
            networks=tuple(sorted(networks | named_networks)),
            orphans=(),
        )

    def assert_absent(self) -> ResidualResources:
        """Fail closed if any resource for this project already exists."""
        residue = self.residual_resources()
        if not residue.clean:
            raise ComposeError(
                f"project {self.project} is not a clean slate: {residue.describe()}"
            )
        return residue

    def destroy(self, *, retries: int = 3, settle_seconds: float = 1.5) -> ResidualResources:
        """Tear down and verify. Returns the final (expected empty) residue."""
        residue = ResidualResources(containers=("unknown",))
        for attempt in range(retries):
            self.down()
            time.sleep(settle_seconds)
            residue = self.residual_resources()
            if residue.clean:
                return residue
            # Escalate: remove whatever the daemon still reports for this project.
            for container in residue.containers:
                docker("rm", "--force", "--volumes", container, check=False, timeout=60)
            for volume in residue.volumes:
                docker("volume", "rm", "--force", volume, check=False, timeout=60)
            for network in residue.networks:
                docker("network", "rm", network, check=False, timeout=60)
            time.sleep(settle_seconds * (attempt + 1))
        return self.residual_resources()


def inspect_container(container: str) -> Mapping[str, object]:
    result = docker("inspect", container, timeout=60)
    payload = json.loads(result.stdout)
    if not payload:
        raise ComposeError(f"container not found: {container}")
    return payload[0]


def container_env(container: str) -> dict[str, str]:
    """Read a container's environment from the Docker daemon, not the app.

    This is deliberately an out-of-band source: the application never reports
    its own configuration to the verifier.
    """
    details = inspect_container(container)
    config = details.get("Config") or {}
    env: dict[str, str] = {}
    for entry in config.get("Env") or []:
        key, _, value = str(entry).partition("=")
        env[key] = value
    return env


def container_resource_limits(container: str) -> dict[str, int]:
    details = inspect_container(container)
    host_config = details.get("HostConfig") or {}
    return {
        "nanoCpus": int(host_config.get("NanoCpus") or 0),
        "memoryBytes": int(host_config.get("Memory") or 0),
        "pidsLimit": int(host_config.get("PidsLimit") or 0),
    }


def container_networks(container: str) -> tuple[str, ...]:
    """Network names the daemon reports for a container.

    Read from the daemon rather than from the Compose file, so that the check
    compares an observation against a declaration instead of restating one.
    """
    details = inspect_container(container)
    settings = details.get("NetworkSettings") or {}
    return tuple(sorted(str(name) for name in (settings.get("Networks") or {})))
