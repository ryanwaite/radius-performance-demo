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
from .checks import (
    CheckPlan,
    ComposeModel,
    generate_check_plan,
    parse_compose_config,
    redact_env,
    summarise_problems,
)
from .compose import (
    BASE_COMPOSE_FILE,
    ComposeError,
    ComposeProject,
    ResidualResources,
    container_env,
    container_networks,
    container_resource_limits,
)
from .docker_cli import DockerError, daemon_info, docker
from .images import PinnedImage, build_local_image, resolve_registry_image, verify_container_image
from .incidents import MYSQL_POOL_DELAY_V1, IncidentVariant, IncidentVerification
from .load import LoadProfile, LoadResult, run_load
from .manifest import CATALOG_APPLICATION_GATES, EnvironmentManifest, hash_fixture, hash_text
from .telemetry import PrometheusClient, TelemetryWindow, capture_window

# Why each service that can reach the network is allowed to. Reconciliation
# fails for any reachable service absent from this mapping, so a service added
# to the edge network cannot quietly acquire egress: someone has to say why.
EGRESS_EXCEPTIONS: dict[str, str] = {
    "catalog-api": (
        "joins the edge network because Docker cannot publish a host port from an "
        "internal-only network; the image is distroless with no shell or package manager"
    ),
    "prometheus": "joins the edge network to publish its query port for the driver",
}


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
        """The healthy values the incident overlay departs from.

        Verification no longer reads this: the environment check compares the
        daemon's view of each container against the Compose file's declaration,
        which covers every service rather than this one list. It remains as the
        driver's own record of the healthy configuration.
        """
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
        self.compose_model: ComposeModel | None = None
        self.check_plan: CheckPlan | None = None
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
        self._generate_check_plan()

        self.project.up(wait_timeout=300)
        self._discover_ports()
        self._stop("create")

    def _generate_check_plan(self) -> CheckPlan:
        """Derive the per-service checks from the Compose file itself.

        Both gates recorded here are load-bearing. Without
        `check-plan-generated`, a run that never reached this point would have
        an empty derived requirement and could sign off having verified
        nothing. Without `check-plan-covers-compose-services`, a plan that
        omitted a service would omit that service's gates from the requirement
        too, so the omission would erase its own evidence.
        """
        assert self.project is not None
        self._start("generateCheckPlan")
        model = parse_compose_config(self.project.config_json())
        plan = generate_check_plan(
            model,
            readiness_probes=set(self._readiness_probes()),
            egress_exceptions=EGRESS_EXCEPTIONS,
        )
        self.compose_model = model
        self.check_plan = plan
        self.manifest.check_plan = plan.to_dict()
        self.manifest.require_gates(plan.gate_names | CATALOG_APPLICATION_GATES)
        self.manifest.add_gate(
            "check-plan-generated",
            bool(plan.checks),
            f"{len(plan.checks)} checks over {len(plan.services)} services",
        )
        self.manifest.add_gate(
            "check-plan-covers-compose-services",
            plan.complete,
            summarise_problems(plan.problems),
        )
        self._stop("generateCheckPlan")
        return plan

    def _discover_ports(self) -> None:
        """Re-read every published port from Compose after container creation.

        Recreating a container reassigns its ephemeral host port, so this runs
        again after injection and reversion. The set of ports comes from the
        Compose model rather than a list here, so a service that starts
        publishing a port is discovered rather than ignored.
        """
        assert self.project is not None
        model = self.compose_model
        published: dict[str, tuple[int, ...]] = (
            {name: model.services[name].published_ports for name in model.service_names}
            if model is not None
            else {"catalog-api": (8080,), "prometheus": (9090,)}
        )
        ports: dict[str, Any] = {}
        for service, container_ports in sorted(published.items()):
            for container_port in container_ports:
                host, host_port = self.project.port(service, container_port)
                ports[service] = {
                    "host": host,
                    "hostPort": host_port,
                    "containerPort": container_port,
                }
        self.ports = ports
        api = ports["catalog-api"]
        prom = ports["prometheus"]
        self.api_base_url = f"http://{api['host']}:{api['hostPort']}"
        self.prometheus_base_url = f"http://{prom['host']}:{prom['hostPort']}"
        self.prometheus = PrometheusClient(self.prometheus_base_url)
        self.manifest.ports = self.ports

    # -- verification ---------------------------------------------------------

    def verify_start_state(self) -> bool:
        """Execute the generated plan: images, limits, environment, egress, readiness.

        Every check here is driven by the plan rather than by a list in this
        file, so the set of things verified grows with the Compose file instead
        of with someone remembering to update a tuple.
        """
        assert self.project is not None
        self._start("verifyStartState")
        manifest = self.manifest
        spec = self.spec
        if self.check_plan is None or self.compose_model is None:
            self._generate_check_plan()
        plan = self.check_plan
        model = self.compose_model
        assert plan is not None and model is not None

        containers = {service: self.project.container_id(service) for service in plan.services}

        for check in plan.for_kind("image-pinned"):
            self._check_image_pinned(check.service, containers[check.service], model)
        limits: dict[str, Any] = {}
        for check in plan.for_kind("resource-limits"):
            limits[check.service] = self._check_resource_limits(
                check.service, containers[check.service], model
            )
        manifest.resource_limits = limits

        observed_env: dict[str, Any] = {}
        for check in plan.for_kind("environment-variables"):
            observed_env[check.service] = self._check_environment(
                check.service, containers[check.service], model
            )
        manifest.environment_variables = observed_env

        posture: dict[str, Any] = {"networks": {}, "egressBlocked": {}, "exceptions": {}}
        for check in plan.for_kind("egress"):
            self._check_egress(check.service, containers[check.service], model, posture)
        posture["exceptions"] = {
            service: reason
            for service, reason in plan.egress_exceptions.items()
        }

        readiness_ok = self._verify_readiness()
        manifest.readiness_verified = readiness_ok
        manifest.add_gate("application-readiness", readiness_ok)

        seed_count = incidents_module.count_seed_rows(
            self.project, spec.mysql_database, self.mysql_root_password
        )
        manifest.seed_count = seed_count
        manifest.add_gate(
            "mysql-seed-rows",
            seed_count == spec.expected_seed_count,
            f"expected {spec.expected_seed_count}, observed {seed_count}",
        )

        cache_keys = incidents_module.count_cache_keys(self.project)
        manifest.cache_keys = cache_keys
        manifest.add_gate("valkey-empty", cache_keys == 0, f"observed {cache_keys} keys")

        posture["catalogApiImageHasNoShell"] = self._check_image_hermetic()
        manifest.network_posture = posture
        self._stop("verifyStartState")
        return not manifest.failed_gates

    # -- generated checks -----------------------------------------------------

    def _check_image_pinned(self, service: str, container: str, model: ComposeModel) -> None:
        declared = model.services[service].image
        pinned = self.images.get(service)
        if pinned is not None:
            matches, actual = verify_container_image(container, pinned)
            detail = f"expected {pinned.image_id}, observed {actual}"
        else:
            # A service the driver did not pin itself. The Compose declaration
            # is still required to be a digest, and the daemon must agree.
            actual = str(
                json.loads(docker("inspect", container, timeout=60).stdout)[0].get("Image") or ""
            )
            matches = bool(actual) and (
                "@sha256:" in declared or declared.startswith("sha256:")
            )
            detail = f"declared {declared}, observed image {actual}"
        self.manifest.add_gate(f"image-pinned:{service}", matches, detail)

    def _check_resource_limits(
        self, service: str, container: str, model: ComposeModel
    ) -> dict[str, Any]:
        declared = model.services[service]
        observed = container_resource_limits(container)
        ok = (
            declared.limits_declared
            and observed["nanoCpus"] == declared.nano_cpus
            and observed["memoryBytes"] == declared.memory_bytes
        )
        self.manifest.add_gate(
            f"resource-limits:{service}",
            ok,
            f"declared nanoCpus={declared.nano_cpus} memoryBytes={declared.memory_bytes}; "
            f"observed {observed}",
        )
        return {
            "declared": {
                "nanoCpus": declared.nano_cpus,
                "memoryBytes": declared.memory_bytes,
            },
            "observed": observed,
        }

    def _check_environment(
        self, service: str, container: str, model: ComposeModel
    ) -> dict[str, Any]:
        """Every variable the Compose file declares must be set in the container.

        Values are compared in full and recorded redacted: per-run MySQL
        credentials appear in `mysql`'s environment and inside catalog-api's
        DSN, and manifests outlive the trial.

        A service that declares no environment, such as valkey or prometheus
        which are configured by command arguments, passes this check having
        examined nothing. That is a correct result and a vacuous one, so the
        declared count is recorded and the detail says so rather than reading
        as a verification that happened.
        """
        declared = dict(model.services[service].environment)
        observed = container_env(container)
        mismatched = sorted(
            key for key, value in declared.items() if observed.get(key) != value
        )
        if not declared:
            detail = "no environment declared; nothing to verify (coverage 0)"
        elif mismatched:
            detail = f"{len(declared)} declared; mismatched: {mismatched}"
        else:
            detail = f"{len(declared)} declared variables all match"
        self.manifest.add_gate(f"environment-variables:{service}", not mismatched, detail)
        return {
            "declaredCount": len(declared),
            "declared": redact_env(declared),
            "observed": redact_env({key: observed.get(key, "<unset>") for key in declared}),
            "mismatched": mismatched,
        }

    def _check_egress(
        self, service: str, container: str, model: ComposeModel, posture: dict[str, Any]
    ) -> None:
        """Network attachment matches the declaration, and internal means internal.

        For every service this compares the networks the daemon reports against
        the networks the Compose file declares. For services on internal-only
        networks it additionally proves there is no route off the host, which
        is the assertion that would otherwise be taken on trust.
        """
        assert self.project is not None
        expected = model.observed_network_names(service)
        actual = container_networks(container)
        attached_ok = set(actual) == set(expected)
        posture["networks"][service] = {"declared": list(expected), "observed": list(actual)}

        reachable = model.egress_reachable(service)
        blocked_ok = True
        detail = f"declared networks {list(expected)}, observed {list(actual)}"
        if not reachable:
            result = self.project.exec(
                service,
                [
                    "sh",
                    "-c",
                    "timeout 4 getent hosts pypi.org >/dev/null 2>&1 && echo OPEN || echo BLOCKED",
                ],
                timeout=30,
                check=False,
            )
            blocked_ok = "BLOCKED" in result.stdout
            posture["egressBlocked"][service] = blocked_ok
            detail = f"{detail}; egress probe {result.stdout.strip() or '<no output>'}"
        self.manifest.add_gate(f"egress:{service}", attached_ok and blocked_ok, detail)

    def _check_image_hermetic(self) -> bool:
        shell_absent = not docker(
            "run", "--rm", "--entrypoint", "sh", self.images["catalog-api"].reference, "-c", "exit 0",
            check=False,
            timeout=60,
        ).ok
        self.manifest.add_gate("catalog-api-image-hermetic", shell_absent)
        return shell_absent

    # -- readiness ------------------------------------------------------------

    def _readiness_probes(self) -> dict[str, Callable[[], tuple[bool, str]]]:
        """One application-level readiness probe per service.

        A service in the Compose file with no entry here fails reconciliation,
        so the stack cannot grow a service whose readiness nobody checks.
        Container health is not enough: these ask each service to do the work
        the trial depends on.
        """
        return {
            "mysql": self._ready_mysql,
            "valkey": self._ready_valkey,
            "catalog-api": self._ready_catalog_api,
            "prometheus": self._ready_prometheus,
        }

    def _ready_mysql(self) -> tuple[bool, str]:
        assert self.project is not None
        ok = _wait_for(
            lambda: self.project.exec(  # type: ignore[union-attr]
                "mysql",
                [
                    "mysql",
                    "-uroot",
                    f"-p{self.mysql_root_password}",
                    "-N",
                    "-B",
                    "-e",
                    "SELECT 1",
                ],
                timeout=30,
                check=False,
            ).stdout.strip()
            == "1",
            attempts=60,
        )
        return ok, "server answers SELECT 1"

    def _ready_valkey(self) -> tuple[bool, str]:
        assert self.project is not None
        ok = _wait_for(
            lambda: "PONG"
            in self.project.exec(  # type: ignore[union-attr]
                "valkey", ["valkey-cli", "ping"], timeout=30, check=False
            ).stdout.upper(),
            attempts=40,
        )
        return ok, "server answers PING"

    def _ready_catalog_api(self) -> tuple[bool, str]:
        spec = self.spec

        def products_seeded() -> bool:
            status, body = _http_get(f"{self.api_base_url}/api/products?limit=50")
            if status != 200:
                return False
            return json.loads(body).get("count") == spec.expected_seed_count

        def metrics_exposed() -> bool:
            status, body = _http_get(f"{self.api_base_url}/metrics")
            return status == 200 and "catalog_http_requests_total" in body

        steps = {
            "healthz": lambda: _http_get(f"{self.api_base_url}/healthz")[0] == 200,
            "readyz": lambda: _http_get(f"{self.api_base_url}/readyz")[0] == 200,
            "products-seeded": products_seeded,
            "metrics": metrics_exposed,
        }
        failed = [name for name, probe in steps.items() if not _wait_for(probe)]
        return not failed, "all endpoints ready" if not failed else f"failed: {failed}"

    def _ready_prometheus(self) -> tuple[bool, str]:
        assert self.prometheus is not None
        ready = self.prometheus.ready()
        scraping = _wait_for(self.prometheus.scrape_target_up, attempts=40, interval=1.0)
        return ready and scraping, f"ready={ready} scrapingTarget={scraping}"

    def _verify_readiness(self) -> bool:
        """Run every registered probe and record one gate per service."""
        results: list[bool] = []
        for service, probe in sorted(self._readiness_probes().items()):
            ok, detail = probe()
            results.append(ok)
            self.manifest.add_gate(f"readiness:{service}", ok, detail)
        return all(results)

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
