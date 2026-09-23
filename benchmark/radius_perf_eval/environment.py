"""Per-trial environment driver.

This is the benchmark reset mechanism. It deliberately does not reuse the
repository's ``docker-compose.yml`` or ``make local-up``, which are developer
conveniences with fixed ports, shared volumes, and floating image tags.

Each trial gets a unique Compose project, freshly created volumes, dynamically
assigned loopback-only host ports, digest-pinned images, and per-run throwaway
credentials. The driver fails closed: if the observed environment differs from
the declared specification in any respect, the manifest is not signed off.
"""

from __future__ import annotations

import json
import secrets
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable

from . import incidents as incidents_module
from .compose import (
    BASE_COMPOSE_FILE,
    ComposeError,
    ComposeProject,
    ResidualResources,
    container_env,
    container_resource_limits,
)
from .docker_cli import DockerError, daemon_info, docker
from .images import PinnedImage, build_local_image, resolve_registry_image, verify_container_image
from .incidents import MYSQL_POOL_DELAY_V1, IncidentVariant, IncidentVerification
from .load import LoadProfile, LoadResult, run_load
from .manifest import EnvironmentManifest, hash_fixture, hash_text
from .telemetry import PrometheusClient, TelemetryWindow, capture_window

SERVICES = ("mysql", "valkey", "catalog-api", "prometheus")

# Services whose containers must have no route off the host. catalog-api and
# prometheus are excluded because Docker cannot publish a host port from an
# internal-only network; that limitation is recorded in the manifest rather
# than silently claimed as blocked.
EGRESS_BLOCKED_SERVICES = ("mysql", "valkey")


@dataclass(frozen=True)
class ServiceResources:
    cpus: str
    memory: str

    @property
    def nano_cpus(self) -> int:
        return int(round(float(self.cpus) * 1_000_000_000))

    @property
    def memory_bytes(self) -> int:
        unit = self.memory[-1].lower()
        amount = float(self.memory[:-1])
        factor = {"k": 1024, "m": 1024**2, "g": 1024**3}[unit]
        return int(amount * factor)


@dataclass(frozen=True)
class EnvironmentSpec:
    """The declared environment. Reality is checked against this, not the reverse."""

    mysql_image: str = "mysql:8.4"
    valkey_image: str = "valkey/valkey:8-alpine"
    # MCR equivalent; Docker Hub has no MCR mirror for MySQL or Valkey.
    prometheus_image: str = "mcr.microsoft.com/oss/v2/prometheus/prometheus:v3.5.0"
    catalog_api_build_tag: str = "radius-perf-eval/catalog-api:trial"

    mysql_database: str = "catalog"
    mysql_max_connections: int = 150
    expected_seed_count: int = 10

    cache_enabled: bool = False
    cache_ttl: str = "30s"
    db_read_delay: str = "25ms"
    db_max_open_conns: int = 25
    db_max_idle_conns: int = 25
    db_conn_max_lifetime: str = "5m"
    dependency_timeout: str = "3s"
    max_list_limit: int = 100

    resources: dict[str, ServiceResources] = field(
        default_factory=lambda: {
            "mysql": ServiceResources(cpus="1.0", memory="1g"),
            "valkey": ServiceResources(cpus="0.5", memory="256m"),
            "catalog-api": ServiceResources(cpus="1.0", memory="512m"),
            "prometheus": ServiceResources(cpus="0.5", memory="512m"),
        }
    )

    def baseline_container_env(self) -> dict[str, str]:
        return {
            "DB_READ_DELAY": self.db_read_delay,
            "DB_MAX_OPEN_CONNS": str(self.db_max_open_conns),
            "DB_MAX_IDLE_CONNS": str(self.db_max_idle_conns),
            "CACHE_ENABLED": str(self.cache_enabled).lower(),
            "CACHE_TTL": self.cache_ttl,
            "DEPENDENCY_TIMEOUT": self.dependency_timeout,
            "MAX_LIST_LIMIT": str(self.max_list_limit),
        }


@dataclass
class PhaseMeasurement:
    phase: str
    load: LoadResult
    telemetry: TelemetryWindow

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "load": self.load.to_dict(),
            "telemetry": self.telemetry.to_dict(),
        }


def _http_get(url: str, timeout: float = 5.0) -> tuple[int, str]:
    try:
        with urllib.request.urlopen(url, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        return exc.code, ""
    except Exception:
        return 0, ""


def _wait_for(
    predicate: Callable[[], bool], *, attempts: int = 60, interval: float = 1.0
) -> bool:
    for _ in range(attempts):
        try:
            if predicate():
                return True
        except Exception:
            pass
        time.sleep(interval)
    return False


def _throwaway_secret() -> str:
    """Per-run, local-only sandbox credential.

    Alphanumeric so it needs no escaping inside the Go MySQL DSN. These values
    exist for the lifetime of one Compose project and are never committed,
    reused, or written to a shared store.
    """
    return "s" + secrets.token_hex(16)


class TrialEnvironment:
    """One isolated trial environment, created and destroyed as a unit."""

    def __init__(
        self,
        run_id: str,
        *,
        repo_root: Path,
        spec: EnvironmentSpec | None = None,
        incident: IncidentVariant = MYSQL_POOL_DELAY_V1,
        results_dir: Path | None = None,
        pull: bool = True,
        suite_start_epoch: float | None = None,
    ) -> None:
        self.run_id = run_id
        self.repo_root = Path(repo_root).resolve()
        self.spec = spec or EnvironmentSpec()
        self.incident = incident
        self.results_dir = Path(results_dir) if results_dir else self.repo_root / "benchmark" / "results"
        self.pull = pull
        self.suite_start_epoch = suite_start_epoch

        self.compose_project_name = f"radius-eval-{run_id}"
        self.mysql_app_user = "catalog"
        self.mysql_app_password = _throwaway_secret()
        self.mysql_root_password = _throwaway_secret()

        self.images: dict[str, PinnedImage] = {}
        self.project: ComposeProject | None = None
        self.ports: dict[str, Any] = {}
        self.api_base_url = ""
        self.prometheus_base_url = ""
        self.prometheus: PrometheusClient | None = None
        self.measurements: list[PhaseMeasurement] = []
        self.incident_verifications: list[IncidentVerification] = []
        self.manifest = EnvironmentManifest(
            run_id=run_id, compose_project=self.compose_project_name
        )
        self._timers: dict[str, float] = {}

    # -- timing ---------------------------------------------------------------

    def _start(self, name: str) -> None:
        self._timers[name] = time.monotonic()

    def _stop(self, name: str) -> None:
        if name in self._timers:
            self.manifest.timings_ms[name] = round(
                (time.monotonic() - self._timers.pop(name)) * 1000, 3
            )

    # -- compose environment --------------------------------------------------

    def _compose_env(self) -> dict[str, str]:
        spec = self.spec
        env = {
            "FIXTURE_ROOT": str(self.repo_root),
            "MYSQL_IMAGE": self.images["mysql"].reference,
            "VALKEY_IMAGE": self.images["valkey"].reference,
            "CATALOG_API_IMAGE": self.images["catalog-api"].reference,
            "PROMETHEUS_IMAGE": self.images["prometheus"].reference,
            "MYSQL_DATABASE": spec.mysql_database,
            "MYSQL_MAX_CONNECTIONS": str(spec.mysql_max_connections),
            "MYSQL_APP_USER": self.mysql_app_user,
            "MYSQL_APP_PASSWORD": self.mysql_app_password,
            "MYSQL_ROOT_PASSWORD": self.mysql_root_password,
            "CACHE_ENABLED": str(spec.cache_enabled).lower(),
            "CACHE_TTL": spec.cache_ttl,
            "DB_READ_DELAY": spec.db_read_delay,
            "DB_MAX_OPEN_CONNS": str(spec.db_max_open_conns),
            "DB_MAX_IDLE_CONNS": str(spec.db_max_idle_conns),
            "DB_CONN_MAX_LIFETIME": spec.db_conn_max_lifetime,
            "DEPENDENCY_TIMEOUT": spec.dependency_timeout,
            "MAX_LIST_LIMIT": str(spec.max_list_limit),
        }
        for service, resources in spec.resources.items():
            key = service.replace("-", "_").upper()
            env[f"{key}_CPUS"] = resources.cpus
            env[f"{key}_MEMORY"] = resources.memory
        env.update(self.incident.overlay_env)
        return env

    # -- lifecycle ------------------------------------------------------------

    def pin_images(self) -> dict[str, PinnedImage]:
        self._start("pinImages")
        spec = self.spec
        self.images = {
            "mysql": resolve_registry_image("mysql", spec.mysql_image, pull=self.pull),
            "valkey": resolve_registry_image("valkey", spec.valkey_image, pull=self.pull),
            "prometheus": resolve_registry_image(
                "prometheus", spec.prometheus_image, pull=self.pull
            ),
            "catalog-api": build_local_image(
                "catalog-api", str(self.repo_root), spec.catalog_api_build_tag
            ),
        }
        self.manifest.image_digests = {
            name: image.to_dict() for name, image in sorted(self.images.items())
        }
        self._stop("pinImages")
        return self.images

    def create(self) -> None:
        """Create the environment and verify it matches the declaration."""
        self._start("create")
        self.manifest.daemon = daemon_info()

        fixture_hash, fixture_files = hash_fixture(self.repo_root)
        self.manifest.fixture_hash = fixture_hash
        self.manifest.fixture_files = fixture_files

        if not self.images:
            self.pin_images()

        self.project = ComposeProject(
            project=self.compose_project_name,
            env=self._compose_env(),
            files=[BASE_COMPOSE_FILE],
        )
        # Fail closed before creating anything if the slate is not clean.
        self.project.assert_absent()
        self.manifest.compose_config_hash = hash_text(self.project.config())

        self.project.up(wait_timeout=300)
        self._discover_ports()
        self._stop("create")

    def _discover_ports(self) -> None:
        assert self.project is not None
        api_host, api_port = self.project.port("catalog-api", 8080)
        prom_host, prom_port = self.project.port("prometheus", 9090)
        self.ports = {
            "catalog-api": {"host": api_host, "hostPort": api_port, "containerPort": 8080},
            "prometheus": {"host": prom_host, "hostPort": prom_port, "containerPort": 9090},
        }
        self.api_base_url = f"http://{api_host}:{api_port}"
        self.prometheus_base_url = f"http://{prom_host}:{prom_port}"
        self.prometheus = PrometheusClient(self.prometheus_base_url)
        self.manifest.ports = self.ports

    # -- verification ---------------------------------------------------------

    def verify_start_state(self) -> bool:
        """Verify images, data, cache, configuration, limits, network, readiness."""
        assert self.project is not None
        self._start("verifyStartState")
        project = self.project
        manifest = self.manifest
        spec = self.spec

        for service in SERVICES:
            container = project.container_id(service)
            matches, actual = verify_container_image(container, self.images[service])
            manifest.add_gate(
                f"image-pinned:{service}",
                matches,
                f"expected {self.images[service].image_id}, observed {actual}",
            )

        limits: dict[str, Any] = {}
        for service in SERVICES:
            container = project.container_id(service)
            observed = container_resource_limits(container)
            declared = spec.resources[service]
            limits[service] = {
                "declared": {"cpus": declared.cpus, "memory": declared.memory},
                "observed": observed,
            }
            manifest.add_gate(
                f"resource-limits:{service}",
                observed["nanoCpus"] == declared.nano_cpus
                and observed["memoryBytes"] == declared.memory_bytes,
                f"declared cpus={declared.cpus} memory={declared.memory}; observed {observed}",
            )
        manifest.resource_limits = limits

        api_env = container_env(project.container_id("catalog-api"))
        expected_env = spec.baseline_container_env()
        env_ok = all(api_env.get(key) == value for key, value in expected_env.items())
        manifest.environment_variables = {
            "catalog-api": {
                "expected": expected_env,
                "observed": {key: api_env.get(key, "<unset>") for key in expected_env},
            }
        }
        manifest.add_gate("environment-variables:catalog-api", env_ok)

        readiness_ok = self._verify_readiness()
        manifest.readiness_verified = readiness_ok
        manifest.add_gate("application-readiness", readiness_ok)

        seed_count = incidents_module.count_seed_rows(
            project, spec.mysql_database, self.mysql_root_password
        )
        manifest.seed_count = seed_count
        manifest.add_gate(
            "mysql-seed-rows",
            seed_count == spec.expected_seed_count,
            f"expected {spec.expected_seed_count}, observed {seed_count}",
        )

        cache_keys = incidents_module.count_cache_keys(project)
        manifest.cache_keys = cache_keys
        manifest.add_gate("valkey-empty", cache_keys == 0, f"observed {cache_keys} keys")

        manifest.network_posture = self._verify_network_posture()
        self._stop("verifyStartState")
        return not manifest.failed_gates

    def _verify_readiness(self) -> bool:
        """Application-level readiness, beyond container health."""
        spec = self.spec
        checks: list[bool] = []

        checks.append(_wait_for(lambda: _http_get(f"{self.api_base_url}/healthz")[0] == 200))
        checks.append(_wait_for(lambda: _http_get(f"{self.api_base_url}/readyz")[0] == 200))

        def products_seeded() -> bool:
            status, body = _http_get(f"{self.api_base_url}/api/products?limit=50")
            if status != 200:
                return False
            payload = json.loads(body)
            return payload.get("count") == spec.expected_seed_count

        checks.append(_wait_for(products_seeded))

        def metrics_exposed() -> bool:
            status, body = _http_get(f"{self.api_base_url}/metrics")
            return status == 200 and "catalog_http_requests_total" in body

        checks.append(_wait_for(metrics_exposed))

        assert self.prometheus is not None
        checks.append(self.prometheus.ready())
        checks.append(_wait_for(self.prometheus.scrape_target_up, attempts=40, interval=1.0))
        return all(checks)

    def _verify_network_posture(self) -> dict[str, Any]:
        """Negative test: the data tier must not be able to leave the host.

        Trial containers fetch nothing from package registries at runtime; this
        proves it for the services that can be fully isolated, and records the
        honest exception for the two that must publish host ports.
        """
        assert self.project is not None
        posture: dict[str, Any] = {"egressBlocked": {}, "exceptions": {}}

        for service in EGRESS_BLOCKED_SERVICES:
            result = self.project.exec(
                service,
                ["sh", "-c", "timeout 4 getent hosts pypi.org >/dev/null 2>&1 && echo OPEN || echo BLOCKED"],
                timeout=30,
                check=False,
            )
            blocked = "BLOCKED" in result.stdout
            posture["egressBlocked"][service] = blocked
            self.manifest.add_gate(f"egress-blocked:{service}", blocked, result.stdout.strip())

        posture["exceptions"]["catalog-api"] = (
            "joins the edge network because Docker cannot publish a host port from an "
            "internal-only network; the image is distroless with no shell or package manager"
        )
        posture["exceptions"]["prometheus"] = (
            "joins the edge network to publish its query port for the driver"
        )

        shell_absent = not docker(
            "run", "--rm", "--entrypoint", "sh", self.images["catalog-api"].reference, "-c", "exit 0",
            check=False,
            timeout=60,
        ).ok
        posture["catalogApiImageHasNoShell"] = shell_absent
        self.manifest.add_gate("catalog-api-image-hermetic", shell_absent)
        return posture

    # -- incident -------------------------------------------------------------

    def verify_incident(self, *, expect_active: bool) -> IncidentVerification:
        assert self.project is not None
        verification = incidents_module.verify(
            self.project,
            self.incident,
            self.api_base_url,
            expect_active=expect_active,
            app_user=self.mysql_app_user,
            root_password=self.mysql_root_password,
        )
        self.incident_verifications.append(verification)
        label = "active" if expect_active else "inactive"
        self.manifest.add_gate(
            f"incident-{label}-verified",
            verification.ok,
            json.dumps([check.to_dict() for check in verification.failures()]),
        )
        return verification

    def inject_incident(self) -> IncidentVerification:
        assert self.project is not None
        self._start("injectIncident")
        incidents_module.inject(self.project, self.incident)
        self._discover_ports()
        self._verify_readiness()
        verification = self.verify_incident(expect_active=True)
        self.manifest.incident_active = verification.ok
        self.manifest.incident = {
            **self.incident.to_dict(),
            "verifications": [v.to_dict() for v in self.incident_verifications],
        }
        self._stop("injectIncident")
        return verification

    def revert_incident(self) -> IncidentVerification:
        assert self.project is not None
        self._start("revertIncident")
        incidents_module.revert(self.project, self.incident)
        self._discover_ports()
        self._verify_readiness()
        verification = self.verify_incident(expect_active=False)
        self.manifest.incident_active = not verification.ok
        self.manifest.incident = {
            **self.incident.to_dict(),
            "verifications": [v.to_dict() for v in self.incident_verifications],
        }
        self._stop("revertIncident")
        return verification

    # -- measurement ----------------------------------------------------------

    def measure(self, phase: str, profile: LoadProfile) -> PhaseMeasurement:
        assert self.prometheus is not None
        self._start(f"measure:{phase}")
        load_result = run_load(
            self.api_base_url, profile, suite_start_epoch=self.suite_start_epoch
        )
        window = capture_window(
            self.prometheus,
            phase=phase,
            start_epoch=load_result.measured_start_epoch,
            end_epoch=load_result.measured_end_epoch,
        )
        measurement = PhaseMeasurement(phase=phase, load=load_result, telemetry=window)
        self.measurements.append(measurement)
        self._stop(f"measure:{phase}")
        return measurement

    # -- teardown -------------------------------------------------------------

    def destroy(self) -> ResidualResources:
        self._start("destroy")
        if self.project is None:
            residue = ResidualResources()
        else:
            residue = self.project.destroy()
        self.manifest.cleanup_residue = residue.to_dict()
        self.manifest.cleanup_verified = residue.clean
        self.manifest.add_gate("cleanup-verified", residue.clean, residue.describe())
        self._stop("destroy")
        return residue

    # -- artifacts ------------------------------------------------------------

    def write_artifacts(self) -> Path:
        run_dir = self.results_dir / self.run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        self.manifest.write(run_dir / "environment-manifest.json")
        (run_dir / "measurements.json").write_text(
            json.dumps(
                [measurement.to_dict() for measurement in self.measurements],
                indent=2,
                sort_keys=True,
            )
            + "\n",
            encoding="utf-8",
        )
        return run_dir
