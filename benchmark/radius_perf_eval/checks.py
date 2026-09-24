"""Per-service environment checks, generated from the Compose file.

The driver used to enumerate its checks by hand: a ``SERVICES`` tuple, an
``EGRESS_BLOCKED_SERVICES`` tuple, and a ``REQUIRED_GATES`` frozenset naming
four services. Each of those was correct for the four-service catalog stack and
each was a latent complete-inventory failure, because a service added to the
Compose file acquired no checks and the manifest still signed off with every
gate it knew about passing. The check that was supposed to prove the
environment matched its declaration could not see the part of the declaration
it had never been told about.

Here the enumeration is derived instead. ``docker compose config --format json``
is the authority on what the stack contains: it resolves interpolation, merges
overlays, and normalises units, so the model below describes what will actually
run rather than what the template appears to say. The check plan is generated
from that model, and reconciliation fails when the plan and the file disagree
in either direction.

Nothing in this module talks to the Docker daemon. It turns text into a plan;
``environment.py`` executes the plan. That split is what lets the generation
logic be tested with no daemon present.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Collection, Iterable, Mapping, Sequence

# The five per-service properties every service must have a check for. A
# service in the Compose file missing any of these fails reconciliation.
REQUIRED_CHECK_KINDS: tuple[str, ...] = (
    "image-pinned",
    "resource-limits",
    "environment-variables",
    "egress",
    "readiness",
)

_MEMORY_SUFFIXES = {"b": 1, "k": 1024, "m": 1024**2, "g": 1024**3, "t": 1024**4}

# Environment values matching these are compared but never recorded verbatim in
# the manifest. Per-run MySQL credentials appear in `mysql`'s environment and
# inside catalog-api's DSN, and manifests are written to artifact directories
# that outlive the trial.
_SECRET_KEY_RE = re.compile(r"PASSWORD|SECRET|TOKEN|CREDENTIAL|_DSN$|^DSN$", re.IGNORECASE)

REDACTED = "<redacted>"


class CheckPlanError(ValueError):
    """Raised when a Compose model cannot be parsed into a check plan."""


def parse_memory(value: Any) -> int:
    """Bytes from a Compose memory limit.

    ``docker compose config --format json`` renders limits as a decimal byte
    string, but a hand-written or hand-edited model may carry ``512m``. Both
    are accepted so that the parser's behaviour does not depend on which
    producer wrote the model.
    """
    if isinstance(value, bool):
        raise CheckPlanError(f"invalid memory limit: {value!r}")
    if isinstance(value, int):
        return int(value)
    if isinstance(value, float):
        return int(value)
    text = str(value).strip().lower()
    if not text:
        raise CheckPlanError("empty memory limit")
    if text.isdigit():
        return int(text)
    suffix = text[-1]
    if suffix in _MEMORY_SUFFIXES and text[:-1].replace(".", "", 1).isdigit():
        return int(float(text[:-1]) * _MEMORY_SUFFIXES[suffix])
    if text.endswith("ib") and text[-3] in _MEMORY_SUFFIXES:
        return int(float(text[:-3]) * _MEMORY_SUFFIXES[text[-3]])
    raise CheckPlanError(f"unparsable memory limit: {value!r}")


def parse_nano_cpus(value: Any) -> int:
    if isinstance(value, bool):
        raise CheckPlanError(f"invalid cpu limit: {value!r}")
    try:
        return int(round(float(value) * 1_000_000_000))
    except (TypeError, ValueError) as exc:
        raise CheckPlanError(f"unparsable cpu limit: {value!r}") from exc


def redact_env(env: Mapping[str, str]) -> dict[str, str]:
    """Environment safe to write into an artifact."""
    return {
        key: (REDACTED if _SECRET_KEY_RE.search(key) else str(value))
        for key, value in sorted(env.items())
    }


@dataclass(frozen=True)
class NetworkModel:
    name: str
    internal: bool = False
    full_name: str = ""

    @property
    def observed_name(self) -> str:
        """The name the daemon reports, which Compose prefixes with the project."""
        return self.full_name or self.name


@dataclass(frozen=True)
class ServiceModel:
    """One service exactly as Compose will create it."""

    name: str
    image: str
    environment: Mapping[str, str] = field(default_factory=dict)
    networks: tuple[str, ...] = ()
    nano_cpus: int | None = None
    memory_bytes: int | None = None
    published_ports: tuple[int, ...] = ()

    @property
    def limits_declared(self) -> bool:
        return self.nano_cpus is not None and self.memory_bytes is not None


@dataclass(frozen=True)
class ComposeModel:
    services: Mapping[str, ServiceModel]
    networks: Mapping[str, NetworkModel] = field(default_factory=dict)
    volumes: tuple[str, ...] = ()

    @property
    def service_names(self) -> tuple[str, ...]:
        return tuple(sorted(self.services))

    def egress_reachable(self, service: str) -> bool:
        """True when the service sits on any network with a gateway.

        A service attached only to ``internal: true`` networks has no route off
        the host and can be proved to have none. Anything else can reach the
        outside world and must say why.
        """
        model = self.services[service]
        if not model.networks:
            # No explicit network means Compose's default bridge, which routes.
            return True
        for network in model.networks:
            declared = self.networks.get(network)
            if declared is None or not declared.internal:
                return True
        return False

    def observed_network_names(self, service: str) -> tuple[str, ...]:
        """The network names the daemon will report for this service."""
        model = self.services[service]
        return tuple(
            sorted(
                self.networks[name].observed_name if name in self.networks else name
                for name in model.networks
            )
        )


def parse_compose_config(text: str) -> ComposeModel:
    """Build a model from ``docker compose config --format json`` output."""
    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CheckPlanError(f"compose config is not JSON: {exc}") from exc
    if not isinstance(payload, dict):
        raise CheckPlanError("compose config is not an object")

    raw_networks = payload.get("networks") or {}
    networks = {
        name: NetworkModel(
            name=name,
            internal=bool((spec or {}).get("internal", False)),
            full_name=str((spec or {}).get("name") or name),
        )
        for name, spec in raw_networks.items()
    }

    raw_services = payload.get("services") or {}
    if not raw_services:
        raise CheckPlanError("compose config declares no services")

    services: dict[str, ServiceModel] = {}
    for name, spec in raw_services.items():
        spec = spec or {}
        limits = ((spec.get("deploy") or {}).get("resources") or {}).get("limits") or {}
        cpus = limits.get("cpus")
        memory = limits.get("memory")
        service_networks = spec.get("networks") or {}
        if isinstance(service_networks, Mapping):
            network_names = tuple(sorted(service_networks))
        else:
            network_names = tuple(sorted(str(n) for n in service_networks))
        ports = tuple(
            sorted(
                int(entry["target"])
                for entry in (spec.get("ports") or [])
                if isinstance(entry, Mapping) and entry.get("target") is not None
            )
        )
        services[name] = ServiceModel(
            name=name,
            image=str(spec.get("image") or ""),
            environment={
                str(k): "" if v is None else str(v)
                for k, v in (spec.get("environment") or {}).items()
            },
            networks=network_names,
            nano_cpus=None if cpus is None else parse_nano_cpus(cpus),
            memory_bytes=None if memory is None else parse_memory(memory),
            published_ports=ports,
        )

    return ComposeModel(
        services=services,
        networks=networks,
        volumes=tuple(sorted(payload.get("volumes") or {})),
    )


@dataclass(frozen=True)
class ServiceCheck:
    kind: str
    service: str
    detail: str = ""

    @property
    def gate_name(self) -> str:
        return f"{self.kind}:{self.service}"

    def to_dict(self) -> dict[str, Any]:
        return {"kind": self.kind, "service": self.service, "detail": self.detail}


@dataclass(frozen=True)
class CheckPlan:
    """What must be verified, and what is wrong with the plan itself."""

    checks: tuple[ServiceCheck, ...]
    services: tuple[str, ...]
    egress_exceptions: Mapping[str, str] = field(default_factory=dict)
    problems: tuple[str, ...] = ()

    @property
    def gate_names(self) -> frozenset[str]:
        return frozenset(check.gate_name for check in self.checks)

    @property
    def complete(self) -> bool:
        return not self.problems

    def for_kind(self, kind: str) -> tuple[ServiceCheck, ...]:
        return tuple(check for check in self.checks if check.kind == kind)

    def without_service(self, service: str) -> "CheckPlan":
        """A plan with one service's checks removed.

        This exists for the suppression control: a plan that silently drops a
        service must not be able to produce a signed-off manifest. Because the
        required gate set is derived from the plan, dropping the checks also
        drops the requirement, so the only thing that can catch it is
        reconciliation against the Compose file.
        """
        return CheckPlan(
            checks=tuple(c for c in self.checks if c.service != service),
            services=self.services,
            egress_exceptions=self.egress_exceptions,
            problems=self.problems,
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "services": list(self.services),
            "checks": [check.to_dict() for check in self.checks],
            "gateNames": sorted(self.gate_names),
            "egressExceptions": dict(self.egress_exceptions),
            "problems": list(self.problems),
            "complete": self.complete,
        }


def reconcile(
    checks: Iterable[ServiceCheck],
    model: ComposeModel,
    *,
    readiness_probes: Collection[str] = (),
    egress_exceptions: Mapping[str, str] | None = None,
) -> tuple[str, ...]:
    """Problems that make a plan untrustworthy, in both directions.

    A service in the file with no check is the failure this module exists to
    prevent. A check naming a service that is not in the file is the mirror
    image: it would otherwise sit in the required gate set forever, never be
    recorded, and be read as an unrelated bug.
    """
    egress_exceptions = egress_exceptions or {}
    problems: list[str] = []
    checks = tuple(checks)
    declared = set(model.services)

    by_service: dict[str, set[str]] = {}
    for check in checks:
        by_service.setdefault(check.service, set()).add(check.kind)

    for service in sorted(declared):
        kinds = by_service.get(service, set())
        missing = [kind for kind in REQUIRED_CHECK_KINDS if kind not in kinds]
        for kind in missing:
            problems.append(f"service {service!r} in the compose file has no {kind} check")

    for service in sorted(set(by_service) - declared):
        problems.append(
            f"check names service {service!r}, which is not in the compose file"
        )

    for service in sorted(declared):
        if service not in readiness_probes:
            problems.append(f"service {service!r} has no application-level readiness probe")
        if model.egress_reachable(service) and service not in egress_exceptions:
            problems.append(
                f"service {service!r} can reach the network and declares no egress exception"
            )
        if not model.services[service].limits_declared:
            problems.append(f"service {service!r} declares no cpu and memory limits")
        if "@sha256:" not in model.services[service].image and not model.services[
            service
        ].image.startswith("sha256:"):
            problems.append(f"service {service!r} uses an unpinned image reference")

    return tuple(problems)


def generate_check_plan(
    model: ComposeModel,
    *,
    readiness_probes: Collection[str] = (),
    egress_exceptions: Mapping[str, str] | None = None,
) -> CheckPlan:
    """Derive one check of each required kind for every service in the file."""
    egress_exceptions = dict(egress_exceptions or {})
    checks: list[ServiceCheck] = []
    for service in model.service_names:
        spec = model.services[service]
        reachable = model.egress_reachable(service)
        for kind in REQUIRED_CHECK_KINDS:
            detail = ""
            if kind == "egress":
                detail = (
                    f"reachable; exception: {egress_exceptions.get(service, '<none declared>')}"
                    if reachable
                    else "internal-only networks; must prove no egress"
                )
            elif kind == "resource-limits":
                detail = f"declared nanoCpus={spec.nano_cpus} memoryBytes={spec.memory_bytes}"
            elif kind == "environment-variables":
                detail = f"{len(spec.environment)} declared variables"
            elif kind == "image-pinned":
                detail = spec.image
            checks.append(ServiceCheck(kind=kind, service=service, detail=detail))

    problems = reconcile(
        checks,
        model,
        readiness_probes=readiness_probes,
        egress_exceptions=egress_exceptions,
    )
    return CheckPlan(
        checks=tuple(checks),
        services=model.service_names,
        egress_exceptions={
            name: reason
            for name, reason in egress_exceptions.items()
            if name in model.services
        },
        problems=problems,
    )


def summarise_problems(problems: Sequence[str], limit: int = 5) -> str:
    if not problems:
        return "none"
    shown = "; ".join(problems[:limit])
    if len(problems) > limit:
        shown += f"; (+{len(problems) - limit} more)"
    return shown
