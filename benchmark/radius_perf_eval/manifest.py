"""The signed-off environment manifest.

Every trial emits one manifest. It is the evidence that the environment matched
the scenario declaration at the moment the trial ran. The driver fails closed:
``signedOff`` is true only when every verification gate passed, and the
signature covers the manifest body so a later edit is detectable.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping

# Everything that defines a trial. Their combined digest is the fixtureHash;
# a change to any of them is a new environment version, not a drifting one.
#
# This deliberately covers more than the container build. An earlier version
# hashed six files -- the Dockerfile, the Go module files, the seed SQL and the
# Prometheus config -- and called the result a fixture hash, while the
# application source, the driver, the load profiles, the incident definition
# and the Compose template could all change without moving it. Two runs could
# then report the same fixtureHash while measuring different things, which is
# the one job the field has.
FIXTURE_PATHS: tuple[str, ...] = (
    "Dockerfile",
    "go.mod",
    "go.sum",
    "deploy/mysql/init/001-schema.sql",
    "deploy/mysql/init/002-seed.sql",
    "deploy/prometheus/prometheus.yml",
)

# Directory trees walked recursively, filtered to the suffixes below so that
# build output and caches cannot perturb the digest.
FIXTURE_TREES: tuple[str, ...] = (
    "cmd",
    "internal",
    "benchmark/radius_perf_eval",
)

FIXTURE_SUFFIXES: frozenset[str] = frozenset({".go", ".py", ".yml", ".yaml", ".sql"})


def fixture_files(root: Path) -> list[str]:
    """Every file contributing to the fixture hash, as sorted relative paths."""
    found = list(FIXTURE_PATHS)
    for tree in FIXTURE_TREES:
        base = root / tree
        if not base.is_dir():
            raise FileNotFoundError(f"fixture tree missing: {base}")
        for path in base.rglob("*"):
            if not path.is_file() or path.suffix not in FIXTURE_SUFFIXES:
                continue
            if "__pycache__" in path.parts:
                continue
            found.append(str(path.relative_to(root)))
    return sorted(set(found))


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return f"sha256:{digest.hexdigest()}"


def hash_text(text: str) -> str:
    return f"sha256:{hashlib.sha256(text.encode('utf-8')).hexdigest()}"


def hash_fixture(
    root: Path, relpaths: Iterable[str] | None = None
) -> tuple[str, dict[str, str]]:
    """Content-address the environment-defining files.

    Returns the combined digest and the per-file digests it was computed from,
    so a mismatch can be localised instead of merely reported.
    """
    relpaths = fixture_files(root) if relpaths is None else relpaths
    per_file: dict[str, str] = {}
    for relpath in sorted(relpaths):
        candidate = root / relpath
        if not candidate.is_file():
            raise FileNotFoundError(f"fixture path missing: {candidate}")
        per_file[relpath] = hash_file(candidate)
    combined = "\n".join(f"{name}  {digest}" for name, digest in sorted(per_file.items()))
    return hash_text(combined), per_file


# The gates that must be *present* for a manifest to mean anything. Sign-off
# used to require only that no recorded gate had failed, which quietly made an
# empty-ish manifest a passing one: a cycle that died before `compose up` still
# reached its teardown, recorded `cleanup-verified`, and signed off on the
# strength of that single gate while reporting readinessVerified false,
# seedCount 0 and no images at all. Seven cycles of an interrupted holdout run
# did exactly that.
#
# This is the same vacuity failure as a negative test that passes because its
# setup was a no-op: the check reports success because the thing it checks
# never ran. Absence is now a failure, so a manifest has to earn sign-off by
# demonstrating every verification, not by avoiding a recorded one.
#
# The per-service gates are no longer listed here. They are generated from the
# Compose file by `checks.py` and added through `require_gates`, because a
# hand-written list of four services was itself a complete-inventory failure:
# a fifth service would have acquired no gates, and the manifest would have
# signed off on the four it happened to know about.
LIFECYCLE_GATES: frozenset[str] = frozenset(
    {
        # Without this, a manifest that never generated a plan would have an
        # empty derived requirement and could sign off having checked nothing.
        "check-plan-generated",
        # Without this, a plan that silently omitted a service would also omit
        # that service's gates from the requirement, so the omission would be
        # invisible. This gate is the only thing that catches it.
        "check-plan-covers-compose-services",
        "application-readiness",
        "incident-active-verified",
        "cleanup-verified",
    }
)

# Data-level assertions about the catalog application specifically. These are
# not structural properties of the Compose file, so they are not generated;
# they are declared per application and change when the application does.
CATALOG_APPLICATION_GATES: frozenset[str] = frozenset(
    {
        "mysql-seed-rows",
        "valkey-empty",
        "catalog-api-image-hermetic",
    }
)


def canonical_json(payload: Mapping[str, Any]) -> str:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)


@dataclass
class Gate:
    """One pass/fail condition that the manifest sign-off depends on."""

    name: str
    passed: bool
    detail: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {"name": self.name, "passed": self.passed, "detail": self.detail}


@dataclass
class EnvironmentManifest:
    run_id: str
    compose_project: str
    image_digests: dict[str, Any] = field(default_factory=dict)
    fixture_hash: str = ""
    fixture_files: dict[str, str] = field(default_factory=dict)
    compose_config_hash: str = ""
    seed_count: int = 0
    cache_keys: int = -1
    resource_limits: dict[str, Any] = field(default_factory=dict)
    ports: dict[str, Any] = field(default_factory=dict)
    environment_variables: dict[str, Any] = field(default_factory=dict)
    network_posture: dict[str, Any] = field(default_factory=dict)
    incident: dict[str, Any] = field(default_factory=dict)
    incident_active: bool = False
    readiness_verified: bool = False
    cleanup_verified: bool = False
    cleanup_residue: dict[str, Any] = field(default_factory=dict)
    daemon: dict[str, Any] = field(default_factory=dict)
    timings_ms: dict[str, float] = field(default_factory=dict)
    gates: list[Gate] = field(default_factory=list)
    check_plan: dict[str, Any] = field(default_factory=dict)
    required_gates: frozenset[str] = LIFECYCLE_GATES

    def require_gates(self, names: Iterable[str]) -> frozenset[str]:
        """Add to the floor of gates this manifest must record to sign off.

        The driver calls this once the check plan exists. Before that the floor
        is the lifecycle set, which includes `check-plan-generated`, so a run
        that dies before generating a plan cannot sign off on the gates it did
        manage to record.
        """
        self.required_gates = self.required_gates | frozenset(names)
        return self.required_gates

    def add_gate(self, name: str, passed: bool, detail: str = "") -> Gate:
        gate = Gate(name=name, passed=passed, detail=detail)
        self.gates.append(gate)
        return gate

    @property
    def failed_gates(self) -> list[Gate]:
        return [gate for gate in self.gates if not gate.passed]

    @property
    def missing_gates(self) -> list[str]:
        """Required gates that were never recorded at all."""
        return sorted(self.required_gates - {gate.name for gate in self.gates})

    @property
    def signed_off(self) -> bool:
        return not self.missing_gates and not self.failed_gates

    def body(self) -> dict[str, Any]:
        return {
            "schemaVersion": "v1",
            "runId": self.run_id,
            "composeProject": self.compose_project,
            "imageDigests": self.image_digests,
            "fixtureHash": self.fixture_hash,
            "fixtureFiles": self.fixture_files,
            "composeConfigHash": self.compose_config_hash,
            "seedCount": self.seed_count,
            "cacheKeys": self.cache_keys,
            "resourceLimits": self.resource_limits,
            "ports": self.ports,
            "environmentVariables": self.environment_variables,
            "networkPosture": self.network_posture,
            "incident": self.incident,
            "incidentActive": self.incident_active,
            "readinessVerified": self.readiness_verified,
            "cleanupVerified": self.cleanup_verified,
            "cleanupResidue": self.cleanup_residue,
            "daemon": self.daemon,
            "timingsMs": self.timings_ms,
            "gates": [gate.to_dict() for gate in self.gates],
            "requiredGates": sorted(self.required_gates),
            "checkPlan": self.check_plan,
            "missingGates": self.missing_gates,
        }

    def to_dict(self) -> dict[str, Any]:
        body = self.body()
        body["signedOff"] = self.signed_off
        body["manifestDigest"] = hash_text(canonical_json(body))
        return body

    def write(self, path: Path) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(self.to_dict(), indent=2, sort_keys=True) + "\n", encoding="utf-8")
        return path
