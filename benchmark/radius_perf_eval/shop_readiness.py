"""Application-level readiness for the Astronomy Shop's 28 services.

`docker compose up --wait` is not readiness. It waits for each container's
own healthcheck, which is the application reporting on itself, and eight of
the shop's 28 services declare no healthcheck at all. A container can be
running, and can even call itself healthy, while the thing a trial depends on
is not yet true: the database accepts connections but has no schema, the
broker is up but the consumer group has not joined, the search cluster is red.

So every service gets a probe that is evaluated from outside the service, and
sign-off requires all of them. The probes fall into three groups.

Only the ingress HTTP probe uses an ephemeral host port. Other HTTP probes
run on the internal bridge. TCP probes run from the Python load-generator
container, which joins that same bridge.

Exec probes run a command inside a container, for the services whose
readiness is not an HTTP fact: the broker, the databases, the cache.

Consumer-group probes exist because `accounting` and `fraud-detection`
declare no ports whatsoever. They are Kafka consumers, and the only
externally observable statement of their readiness is that they have joined
their consumer group on the broker. Asking the service would mean trusting
its self-report; asking the broker does not.
"""

from __future__ import annotations

import json
import socket
import subprocess
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Callable, Mapping

# Services that expose no port at all. Their readiness is read from Kafka.
KAFKA_CONSUMERS: tuple[str, ...] = ("accounting", "fraud-detection")

# The broker every consumer-group probe is asked through.
KAFKA_SERVICE = "kafka"

# Used to reach services that are unpublished and distroless. Pinned by
# digest like everything else the driver runs; a floating tag here would be a
# version of the fixture that nobody recorded.
PROBE_IMAGE = (
    "curlimages/curl@sha256:"
    "d43bdb28bae0be0998f3be83199bfb2b81e0a30b034b6d7586ce7e05de34c3fd"
)


class ReadinessError(RuntimeError):
    """A probe could not be evaluated, as distinct from evaluating to false.

    These are kept apart deliberately. "The service is not ready" and "I could
    not tell whether the service is ready" are different facts, and collapsing
    them lets an unreachable probe read as a passing one.
    """


@dataclass(frozen=True)
class Probe:
    """One externally evaluated statement about a service."""

    service: str
    kind: str
    detail: str
    evaluate: Callable[["ProbeContext"], "ProbeResult"]

    def to_dict(self) -> dict[str, str]:
        return {"service": self.service, "kind": self.kind, "detail": self.detail}


@dataclass(frozen=True)
class ProbeResult:
    service: str
    kind: str
    ready: bool
    observed: str
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "kind": self.kind,
            "ready": self.ready,
            "observed": self.observed,
            "error": self.error,
        }


@dataclass
class ProbeContext:
    """What a probe needs to reach the stack, rediscovered per attempt."""

    project: str
    compose_file: str
    ports: Mapping[str, Mapping[int, int]]
    timeout: float = 5.0

    def host_port(self, service: str, container_port: int) -> int:
        mapped = self.ports.get(service, {})
        if container_port not in mapped:
            raise ReadinessError(
                f"{service}: container port {container_port} is not published; "
                f"known ports {sorted(mapped)}"
            )
        return mapped[container_port]

    def exec_in(
        self, service: str, argv: list[str] | None = None,
        timeout_scale: float = 4.0, **kwargs: Any,
    ) -> subprocess.CompletedProcess:
        argv = list(argv or kwargs.get("argv") or [])
        return subprocess.run(
            ["docker", "compose", "-f", self.compose_file, "-p", self.project,
             "exec", "-T", service, *argv],
            capture_output=True, text=True, timeout=self.timeout * timeout_scale,
        )


def discover_ports(project: str, compose_file: str) -> dict[str, dict[int, int]]:
    """Read the host ports Compose actually assigned, right now.

    Called before every readiness sweep rather than once at startup. Ephemeral
    ports are reassigned whenever a container is recreated, and a probe that
    reuses a remembered port after a restart is testing whatever now holds
    that port, which may be nothing or may be another trial.
    """
    result = subprocess.run(
        ["docker", "compose", "-f", compose_file, "-p", project, "ps",
         "--format", "json"],
        capture_output=True, text=True, timeout=60,
    )
    if result.returncode != 0:
        raise ReadinessError(f"could not list services: {result.stderr.strip()}")

    ports: dict[str, dict[int, int]] = {}
    for line in result.stdout.strip().splitlines():
        if not line.strip():
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        service = entry.get("Service")
        if not service:
            continue
        mapped = ports.setdefault(service, {})
        for publisher in entry.get("Publishers") or []:
            published = publisher.get("PublishedPort")
            target = publisher.get("TargetPort")
            if published and target:
                mapped[int(target)] = int(published)
    return ports


def _http_probe(
    service: str, container_port: int, path: str, expect: tuple[int, ...] = (200,)
) -> Probe:
    def evaluate(context: ProbeContext) -> ProbeResult:
        port = context.host_port(service, container_port)
        url = f"http://127.0.0.1:{port}{path}"
        try:
            with urllib.request.urlopen(url, timeout=context.timeout) as response:
                status = response.status
                body = response.read(400).decode("utf-8", "replace")
        except urllib.error.HTTPError as error:
            status, body = error.code, ""
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as error:
            return ProbeResult(service, "http", False, "", f"{type(error).__name__}: {error}")
        return ProbeResult(
            service, "http", status in expect, f"{url} -> {status} {body[:120]}"
        )

    return Probe(service, "http", f"GET {path} on container port {container_port}", evaluate)


def _tcp_probe(service: str, container_port: int) -> Probe:
    def evaluate(context: ProbeContext) -> ProbeResult:
        code = (
            "import socket;"
            f"s=socket.create_connection(({service!r},{container_port}),{context.timeout!r});"
            "s.close();print('TCP_OK')"
        )
        result = context.exec_in("load-generator", ["python", "-c", code])
        return ProbeResult(
            service, "tcp", result.returncode == 0 and result.stdout.strip() == "TCP_OK",
            f"{service}:{container_port}: {result.stdout.strip()}",
            result.stderr.strip()[:200],
        )

    return Probe(service, "tcp", f"TCP connect to container port {container_port}", evaluate)


def _internal_http_probe(
    service: str, container_port: int, path: str, expect: tuple[int, ...] = (200,)
) -> Probe:
    """Probe a service that is unreachable from the host, from the network.

    `flagd` and `flagd-ui` are unpublished on purpose, and both images are
    distroless, so there is no shell to exec into and no host port to call.
    A throwaway container attached to the project network can reach them. It
    is also a better probe than an exec would be: nothing inside the service
    is trusted, and the request crosses the network the application uses.
    """

    def evaluate(context: ProbeContext) -> ProbeResult:
        url = f"http://{service}:{container_port}{path}"
        result = subprocess.run(
            ["docker", "run", "--rm", "--network", f"{context.project}_default",
             "--label", f"com.docker.compose.project={context.project}",
             PROBE_IMAGE, "-s", "-o", "/dev/null", "-w", "%{http_code}",
             "--max-time", str(int(context.timeout)), url],
            capture_output=True, text=True, timeout=context.timeout * 6,
        )
        code = result.stdout.strip()
        if result.returncode != 0 or not code.isdigit():
            return ProbeResult(
                service, "internal-http", False, code,
                result.stderr.strip()[:200] or "probe container failed",
            )
        return ProbeResult(
            service, "internal-http", int(code) in expect, f"{url} -> {code}"
        )

    return Probe(
        service, "internal-http",
        f"GET {path} on container port {container_port}, from inside the network",
        evaluate,
    )


def _exec_probe(
    service: str, argv: list[str], expect: str, detail: str,
    timeout_scale: float = 4.0,
) -> Probe:
    def evaluate(context: ProbeContext) -> ProbeResult:
        try:
            result = context.exec_in(service, argv, timeout_scale=timeout_scale)
        except subprocess.TimeoutExpired:
            return ProbeResult(service, "exec", False, "", "probe timed out")
        output = (result.stdout + result.stderr).strip()
        ready = result.returncode == 0 and expect in output
        return ProbeResult(service, "exec", ready, output[:200])

    return Probe(service, "exec", detail, evaluate)


def _consumer_group_probe(service: str) -> Probe:
    """Readiness read from the broker, not from the service.

    `accounting` and `fraud-detection` publish no port and expose no endpoint.
    Their readiness is that they have joined their consumer group, which the
    broker knows and the service would only be able to assert about itself.
    """

    def evaluate(context: ProbeContext) -> ProbeResult:
        try:
            result = context.exec_in(
                KAFKA_SERVICE,
                timeout_scale=12.0,
                argv=["/bin/sh", "-c",
                 "/opt/kafka/bin/kafka-consumer-groups.sh "
                 "--bootstrap-server kafka:9092 --list"],
            )
        except subprocess.TimeoutExpired:
            return ProbeResult(service, "consumer-group", False, "", "probe timed out")
        if result.returncode != 0:
            return ProbeResult(
                service, "consumer-group", False, "",
                f"could not query broker: {result.stderr.strip()[:200]}",
            )
        groups = [line.strip() for line in result.stdout.splitlines() if line.strip()]
        joined = [g for g in groups if service in g]
        return ProbeResult(
            service, "consumer-group", bool(joined),
            f"groups={groups[:10]} matched={joined}",
        )

    return Probe(
        service, "consumer-group",
        f"{service} has joined its consumer group on {KAFKA_SERVICE}",
        evaluate,
    )


def build_probes() -> tuple[Probe, ...]:
    """One probe per service, covering all 28.

    Kept as an explicit table because each service's readiness means something
    different, but `missing_probes` reconciles it against the Compose file so
    the table cannot fall behind a service being added.
    """
    probes: list[Probe] = [
        # Edge and application services.
        _http_probe("frontend-proxy", 8080, "/", expect=(200, 301, 302)),
        _internal_http_probe("frontend", 8080, "/", expect=(200, 301, 302)),
        _internal_http_probe("image-provider", 8081, "/Banner.png"),
        _internal_http_probe("telemetry-docs", 8000, "/", expect=(200, 301, 302)),
        _internal_http_probe("quote", 8090, "/", expect=(200, 404)),

        # Observability back ends, each asked the question that matters.
        _internal_http_probe("grafana", 3000, "/api/health"),
        _internal_http_probe("prometheus", 9090, "/-/ready"),
        _internal_http_probe("jaeger", 16686, "/jaeger/ui/api/services"),

        # opamp-server publishes no port, so its readiness is read by asking
        # it from inside its own network namespace rather than from the host.
        _exec_probe(
            "opamp-server",
            ["/bin/sh", "-c",
             "wget -qO- http://localhost:4321/ >/dev/null 2>&1 && echo OPAMP_OK"],
            "OPAMP_OK",
            "opamp-server answers on its internal port 4321",
        ),
        # The collector's health_check extension is not published, so the
        # externally observable fact is that it accepts OTLP.
        _tcp_probe("otel-collector", 4317),

        # The flag service and its UI are deliberately unpublished by the
        # `hide-flag-services` transform, so they cannot be reached from the
        # host at all. That is the point: an agent that can read the flag
        # state can read the answer. Their readiness is therefore asked from
        # inside the network.
        _internal_http_probe("flagd", 8014, "/healthz"),
        _internal_http_probe("flagd-ui", 4000, "/"),

        # gRPC services have no HTTP surface; a TCP connect is the honest
        # externally observable statement short of speaking gRPC.
        _tcp_probe("ad", 9555),
        _tcp_probe("cart", 7070),
        _tcp_probe("checkout", 5050),
        _tcp_probe("currency", 7001),
        _tcp_probe("email", 6060),
        _tcp_probe("payment", 50051),
        _tcp_probe("product-catalog", 3550),
        _tcp_probe("recommendation", 9001),
        _tcp_probe("shipping", 50050),
        _tcp_probe("load-generator", 8089),

        # Data stores: asked to do their actual job, not just accept a socket.
        _exec_probe(
            "astronomy-db",
            ["pg_isready", "-U", "postgres"],
            "accepting connections",
            "postgres accepts connections",
        ),
        _exec_probe(
            "valkey-cart",
            ["valkey-cli", "ping"],
            "PONG",
            "valkey answers PING",
        ),
        _exec_probe(
            "opensearch",
            ["/bin/sh", "-c",
             "curl -s localhost:9200/_cluster/health || "
             "wget -qO- localhost:9200/_cluster/health"],
            '"status"',
            "opensearch reports cluster health",
        ),
        _exec_probe(
            "kafka",
            ["/bin/sh", "-c",
             "/opt/kafka/bin/kafka-topics.sh --bootstrap-server kafka:9092 "
             "--list >/dev/null 2>&1 && echo KAFKA_OK"],
            "KAFKA_OK",
            "kafka broker lists topics",
            timeout_scale=12.0,
        ),
    ]
    probes.extend(_consumer_group_probe(name) for name in KAFKA_CONSUMERS)
    return tuple(probes)


def missing_probes(services: Mapping[str, Any] | list[str]) -> dict[str, list[str]]:
    """Reconcile the probe table against the Compose file, both directions.

    A service in the file with no probe is a hole in readiness. A probe naming
    a service that is not in the file is a probe that can never run, and which
    would otherwise sit in the table looking like coverage.
    """
    names = set(services if isinstance(services, list) else services.keys())
    probed = {probe.service for probe in build_probes()}
    return {
        "servicesWithoutProbe": sorted(names - probed),
        "probesWithoutService": sorted(probed - names),
    }


def evaluate_all(context: ProbeContext) -> tuple[list[ProbeResult], bool]:
    """Run every probe and report whether all of them are satisfied.

    A probe that cannot be evaluated is recorded as not ready with the reason,
    never skipped and never allowed to abort the sweep. Skipping it would let
    an unreachable probe read as a passing one, and aborting would hide the
    state of every probe after it.
    """
    results: list[ProbeResult] = []
    for probe in build_probes():
        try:
            results.append(probe.evaluate(context))
        except ReadinessError as error:
            results.append(
                ProbeResult(probe.service, probe.kind, False, "", str(error))
            )
        except Exception as error:  # noqa: BLE001 - recorded, not swallowed
            results.append(
                ProbeResult(
                    probe.service, probe.kind, False, "",
                    f"{type(error).__name__}: {error}",
                )
            )
    return results, bool(results) and all(result.ready for result in results)


# ---------------------------------------------------------------------------
# Flag state: read from the flag service, never from the application
# ---------------------------------------------------------------------------

# flagd's OFREP surface. Port 8014 carries health; 8016 carries evaluation.
FLAGD_OFREP_PORT = 8016
FLAGD_BULK_PATH = "/ofrep/v1/evaluate/flags"


@dataclass(frozen=True)
class FlagState:
    """What the flag service says, and whether it matches the baseline."""

    readable: bool
    resolved: Mapping[str, str]
    expected: Mapping[str, str]
    unexpected_on: tuple[str, ...]
    missing: tuple[str, ...]
    error: str = ""

    @property
    def baseline_clean(self) -> bool:
        """Every fault flag off, every expected flag accounted for, state read.

        Unreadable state fails rather than passes. A gate that cannot see the
        flags has not observed a clean baseline, it has observed nothing, and
        those are the same only if you are willing to score a trial whose
        fault state you never checked.
        """
        return bool(self.expected) and self.readable and not self.unexpected_on and not self.missing

    def to_dict(self) -> dict[str, Any]:
        return {
            "readable": self.readable,
            "baselineClean": self.baseline_clean,
            "resolved": dict(self.resolved),
            "expected": dict(self.expected),
            "unexpectedOn": list(self.unexpected_on),
            "missing": list(self.missing),
            "error": self.error,
        }


def read_flag_state(project: str, timeout: float = 10.0) -> tuple[bool, dict[str, str], str]:
    """Ask flagd directly for every flag's resolved variant.

    `hide-flag-services` unpublishes flagd, so this runs from a throwaway
    container on the project network. That is deliberate: the flag service is
    out of the agent's reach, and the gate reaches it the same way nothing
    else in the stack can.

    The variant is used rather than the value because several of the shop's
    flags are graded, carrying numeric or duration payloads whose neutral
    setting is not the boolean false.
    """
    result = subprocess.run(
        ["docker", "run", "--rm", "--network", f"{project}_default",
         "--label", f"com.docker.compose.project={project}", PROBE_IMAGE,
         "-s", "-X", "POST", "-H", "Content-Type: application/json", "-d", "{}",
         "--max-time", str(int(timeout)),
         f"http://flagd:{FLAGD_OFREP_PORT}{FLAGD_BULK_PATH}"],
        capture_output=True, text=True, timeout=timeout * 6,
    )
    if result.returncode != 0 or not result.stdout.strip():
        return False, {}, (result.stderr.strip() or "no response from flagd")[:300]
    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as error:
        return False, {}, f"unparseable flagd response: {error}"

    flags = payload.get("flags")
    if not isinstance(flags, list) or not flags:
        return False, {}, "flagd returned no flags"
    resolved = {
        str(entry.get("key")): str(entry.get("variant"))
        for entry in flags
        if entry.get("key") is not None
    }
    return True, resolved, ""


def evaluate_flag_gate(project: str, expected: Mapping[str, str], timeout: float = 10.0) -> FlagState:
    """Fail unless every flag sits at the variant the baseline declares."""
    readable, resolved, error = read_flag_state(project, timeout=timeout)
    if not readable:
        return FlagState(False, {}, expected, (), tuple(sorted(expected)), error)

    unexpected_on = tuple(
        sorted(
            name
            for name, variant in resolved.items()
            if name not in expected or variant != expected[name]
        )
    )
    missing = tuple(sorted(set(expected) - set(resolved)))
    return FlagState(True, resolved, expected, unexpected_on, missing, "")
