"""Deterministic, reversible incident injection with out-of-band verification.

Injection is a Compose overlay applied from the control plane. The overlay file
lives under ``benchmark/`` and is never part of an agent-visible fixture.

Verification never trusts the application's own report. Each check names the
authority it came from:

``docker-daemon``
    Container configuration read back from the Docker daemon.
``mysql-server``
    Live connection accounting read from MySQL itself, which the application
    cannot fabricate.
``external-probe``
    Wall-clock latency measured by the driver over the network, outside the
    application process.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from .compose import COMPOSE_DIR, ComposeError, ComposeProject, container_env
from .load import LoadProfile, run_load


@dataclass(frozen=True)
class IncidentVariant:
    """One hidden, seeded parameterisation of a public scenario concept."""

    scenario: str
    version: str
    variant_id: str
    seed: int
    overlay_file: Path
    overlay_env: dict[str, str]
    expected_container_env: dict[str, str]
    baseline_container_env: dict[str, str]
    max_app_db_connections: int
    min_active_request_seconds: float
    max_inactive_request_seconds: float

    @property
    def scenario_id(self) -> str:
        return f"{self.scenario}/{self.version}"

    def to_dict(self) -> dict[str, object]:
        return {
            "scenario": self.scenario,
            "version": self.version,
            "scenarioId": self.scenario_id,
            "variantId": self.variant_id,
            "seed": self.seed,
            "overlayFile": self.overlay_file.name,
            "expectedContainerEnv": dict(self.expected_container_env),
            "baselineContainerEnv": dict(self.baseline_container_env),
            "maxAppDbConnections": self.max_app_db_connections,
            "minActiveRequestSeconds": self.min_active_request_seconds,
            "maxInactiveRequestSeconds": self.max_inactive_request_seconds,
        }


@dataclass(frozen=True)
class VerificationCheck:
    name: str
    source: str
    passed: bool
    expected: str
    observed: str

    def to_dict(self) -> dict[str, object]:
        return {
            "name": self.name,
            "source": self.source,
            "passed": self.passed,
            "expected": self.expected,
            "observed": self.observed,
        }


@dataclass(frozen=True)
class IncidentVerification:
    expected_active: bool
    checks: tuple[VerificationCheck, ...] = ()

    @property
    def ok(self) -> bool:
        return bool(self.checks) and all(check.passed for check in self.checks)

    def to_dict(self) -> dict[str, object]:
        return {
            "expectedActive": self.expected_active,
            "verified": self.ok,
            "checks": [check.to_dict() for check in self.checks],
        }

    def failures(self) -> list[VerificationCheck]:
        return [check for check in self.checks if not check.passed]


MYSQL_POOL_DELAY_V1 = IncidentVariant(
    scenario="mysql-pool-delay",
    version="v1",
    variant_id="mysql-pool-delay/v1/seed-1842",
    seed=1842,
    overlay_file=COMPOSE_DIR / "incident-mysql-pool-delay.yml",
    overlay_env={
        "INCIDENT_DB_READ_DELAY": "250ms",
        "INCIDENT_DB_MAX_OPEN_CONNS": "2",
        "INCIDENT_DB_MAX_IDLE_CONNS": "1",
    },
    expected_container_env={
        "DB_READ_DELAY": "250ms",
        "DB_MAX_OPEN_CONNS": "2",
        "DB_MAX_IDLE_CONNS": "1",
    },
    baseline_container_env={
        "DB_READ_DELAY": "25ms",
        "DB_MAX_OPEN_CONNS": "25",
        "DB_MAX_IDLE_CONNS": "25",
    },
    # With the pool capped at 2, MySQL itself must never see a third concurrent
    # session from the application user no matter how much load is applied.
    max_app_db_connections=2,
    # A single request must cost at least the injected read delay.
    min_active_request_seconds=0.20,
    # Without the incident a single request must be far below that.
    max_inactive_request_seconds=0.12,
)

INCIDENTS: dict[str, IncidentVariant] = {
    MYSQL_POOL_DELAY_V1.scenario_id: MYSQL_POOL_DELAY_V1,
}


def inject(project: ComposeProject, variant: IncidentVariant, *, wait_timeout: int = 180) -> None:
    """Activate the incident by converging catalog-api onto the overlay."""
    files: Sequence[Path] = [*project.files, variant.overlay_file]
    project.up(
        files=files,
        services=("catalog-api",),
        wait_timeout=wait_timeout,
        force_recreate=True,
    )


def revert(project: ComposeProject, variant: IncidentVariant, *, wait_timeout: int = 180) -> None:
    """Deactivate the incident by converging back onto the base declaration."""
    project.up(
        files=project.files,
        services=("catalog-api",),
        wait_timeout=wait_timeout,
        force_recreate=True,
    )


def _mysql_scalar(project: ComposeProject, sql: str, root_password: str) -> str:
    result = project.exec(
        "mysql",
        ["mysql", "--batch", "--skip-column-names", "--execute", sql],
        env_vars={"MYSQL_PWD": root_password},
        timeout=45,
    )
    return result.stdout.strip().splitlines()[0].strip() if result.stdout.strip() else ""


def count_app_db_connections(project: ComposeProject, app_user: str, root_password: str) -> int:
    """Ask MySQL how many sessions the application user currently holds."""
    sql = (
        "SELECT COUNT(*) FROM information_schema.PROCESSLIST "
        f"WHERE USER = '{app_user}'"
    )
    return int(_mysql_scalar(project, sql, root_password) or 0)


def count_seed_rows(project: ComposeProject, database: str, root_password: str) -> int:
    return int(_mysql_scalar(project, f"SELECT COUNT(*) FROM `{database}`.products", root_password) or 0)


def count_cache_keys(project: ComposeProject) -> int:
    """Read Valkey's own key count. Zero is required at trial start."""
    result = project.exec("valkey", ["valkey-cli", "DBSIZE"], timeout=30)
    text = result.stdout.strip().splitlines()
    return int(text[0].strip()) if text else 0


def probe_peak_app_connections(
    project: ComposeProject,
    base_url: str,
    app_user: str,
    root_password: str,
    *,
    concurrency: int = 8,
    seconds: float = 4.0,
    sample_interval: float = 0.15,
) -> int:
    """Saturate the API briefly and record the peak MySQL-side session count.

    This is the load-bearing independent check: the connection ceiling is
    observed at the database server, so a misreporting or compromised
    application cannot make a constrained pool look unconstrained.
    """
    profile = LoadProfile(
        name="incident-probe",
        concurrency=concurrency,
        duration_seconds=seconds,
        warmup_seconds=0.0,
        seed=7,
    )
    peak = 0
    errors: list[Exception] = []

    def _drive() -> None:
        try:
            run_load(base_url, profile)
        except Exception as exc:  # pragma: no cover - defensive
            errors.append(exc)

    driver = threading.Thread(target=_drive, name="incident-probe-load", daemon=True)
    driver.start()
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        try:
            peak = max(peak, count_app_db_connections(project, app_user, root_password))
        except Exception:  # transient exec failures must not abort the probe
            pass
        time.sleep(sample_interval)
    driver.join(timeout=seconds + 30)
    if errors:
        raise ComposeError(f"incident probe load failed: {errors[0]}")
    return peak


def probe_single_request_seconds(base_url: str, *, attempts: int = 5) -> float:
    """Median wall-clock cost of one unconcurrent request, measured externally."""
    profile = LoadProfile(
        name="latency-probe",
        concurrency=1,
        duration_seconds=max(1.0, attempts * 0.4),
        warmup_seconds=0.0,
        seed=11,
    )
    result = run_load(base_url, profile)
    return result.latency_p50_seconds


def verify(
    project: ComposeProject,
    variant: IncidentVariant,
    base_url: str,
    *,
    expect_active: bool,
    app_user: str,
    root_password: str,
) -> IncidentVerification:
    """Check incident state from three independent authorities."""
    checks: list[VerificationCheck] = []

    container = project.container_id("catalog-api")
    observed_env = container_env(container)
    expected_env = (
        variant.expected_container_env if expect_active else variant.baseline_container_env
    )
    for key, expected_value in sorted(expected_env.items()):
        actual = observed_env.get(key, "<unset>")
        checks.append(
            VerificationCheck(
                name=f"container-env:{key}",
                source="docker-daemon",
                passed=actual == expected_value,
                expected=expected_value,
                observed=actual,
            )
        )

    peak = probe_peak_app_connections(project, base_url, app_user, root_password)
    if expect_active:
        passed = 0 < peak <= variant.max_app_db_connections
        expectation = f"1..{variant.max_app_db_connections} concurrent sessions"
    else:
        passed = peak > variant.max_app_db_connections
        expectation = f">{variant.max_app_db_connections} concurrent sessions"
    checks.append(
        VerificationCheck(
            name="mysql-peak-app-connections",
            source="mysql-server",
            passed=passed,
            expected=expectation,
            observed=str(peak),
        )
    )

    latency = probe_single_request_seconds(base_url)
    if expect_active:
        passed = latency >= variant.min_active_request_seconds
        expectation = f">={variant.min_active_request_seconds:.3f}s"
    else:
        passed = latency <= variant.max_inactive_request_seconds
        expectation = f"<={variant.max_inactive_request_seconds:.3f}s"
    checks.append(
        VerificationCheck(
            name="external-single-request-latency",
            source="external-probe",
            passed=passed,
            expected=expectation,
            observed=f"{latency:.4f}s",
        )
    )

    return IncidentVerification(expected_active=expect_active, checks=tuple(checks))
