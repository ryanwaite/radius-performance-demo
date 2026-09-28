"""CPU limits for the Astronomy Shop, fitted from measurement and verified.

Upstream declares ``deploy.resources.limits.memory`` on every service and
``cpus`` on none, so 28 services compete freely for the host's cores. That is
a variance source underneath every measurement we would take.

Two things here, and the second is the one that matters.

**Fitting.** A limit is ``max(multiplier x healthy peak, floor)``. The
multiplier and floor are frozen constants, and the fitted numbers are
committed in ``cpu-limits.json`` so a change is a reviewable diff rather than
a recomputation that silently tracks whatever the last run happened to do.

**Acceptance.** The arithmetic is not evidence. A ``docker stats`` peak is an
average over a sampling interval, so a service with a sub-second spike can sit
far below its limit on paper and still be throttled in practice. The limit is
therefore accepted only if the kernel says it never throttled: ``nr_throttled``
from each container's cgroup ``cpu.stat``.

Two details that decide whether that acceptance test means anything:

*Delta, not cumulative.* ``nr_throttled`` counts from container start, and
startup throttling is expected and harmless: image decompression, JIT warmup,
schema migration. What corrupts a measurement is throttling *during the
window*. Readings are taken at window open and window close and subtracted.
Cumulative totals are recorded too, so startup throttling stays visible rather
than being hidden by the subtraction.

*Every service, including the ones we cannot exec into.* ``flagd`` and
``flagd-ui`` are distroless, so ``docker exec cat`` is impossible, and they are
unpublished by ``hide-flag-services`` so nothing can be reached over the
network either. An exempt service is exactly where an unnoticed throttle would
hide. Readings therefore come from the host cgroup hierarchy through a sidecar
run with ``--cgroupns=host`` and a read-only bind of ``/sys/fs/cgroup``, which
covers every container in the project uniformly. The sidecar needs no
``--privileged`` (verified: the read succeeds without it) and never receives
the Docker socket.
"""

from __future__ import annotations

import hashlib
import json
import math
import subprocess
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

__all__ = [
    "FLOOR_CORES",
    "MULTIPLIER",
    "CpuLimitError",
    "ServiceThrottle",
    "ThrottleReading",
    "ThrottleVerdict",
    "cpu_limits_path",
    "fit_limit",
    "fit_limits",
    "fitted_limits_hash",
    "load_fitted_limits",
    "parse_demand_stream",
    "read_throttling",
    "sample_cpu_demand",
    "verdict_from_readings",
]

#: Frozen fitting rule. Fitted numbers live in ``cpu-limits.json``; these two
#: constants say how those numbers were derived and must not drift with them.
#:
#: Both numbers are measured, and the floor was raised twice as measurement
#: corrected the one before it.
#:
#: The first fit used a 0.25 floor against ``docker stats`` peaks, and the
#: kernel throttled 19 of 28 services. ``email`` was the clearest case: a
#: sampled peak of 0.05 cores against a true peak of 0.87, understated
#: seventeenfold, throttled at 11% of its periods. Worse, the load generator
#: was itself throttled, so it offered less load, so downstream services saw
#: lighter traffic and looked healthy. A binding limit does not merely add
#: noise, it hides the fact that it is adding noise.
#:
#: Refitting from kernel counters at a 1.0 floor cleared the steady-state
#: window but not the kernel's lifetime counters: kafka had spent 131
#: throttled periods starting up, ad 60, fraud-detection 39. A probe at
#: uniform quotas then bracketed the answer. At 4.0 cores kafka still
#: throttled one period during startup; at 8.0 every service was clean for
#: its whole life. One period out of thousands means 4.0 sits on the edge,
#: and a limit on the edge binds on some runs and not others, which is the
#: run-to-run variance this driver exists to remove. So the floor is the
#: proven-clean value, not the smallest value that nearly worked.
#:
#: These limits are guard rails, not constraints. They are uniform in
#: presence so the shape of the Compose file cannot reveal which service is
#: faulted, they bound a runaway container, and verification proves they
#: never bind in healthy operation. They are not the mechanism of any fault:
#: the shop's faults are flag-driven. The multiplier still governs any
#: service whose measured peak exceeds half the floor.
MULTIPLIER = 2.0
FLOOR_CORES = 8.0

#: Read-only, unprivileged. Pinned by digest like every other image we run.
SIDECAR_IMAGE = (
    "curlimages/curl@sha256:"
    "d43bdb28bae0be0998f3be83199bfb2b81e0a30b034b6d7586ce7e05de34c3fd"
)

_CGROUP_ROOT = "/sys/fs/cgroup"


class CpuLimitError(RuntimeError):
    """A limit could not be fitted, applied, or verified.

    Deliberately distinct from "the limit was exceeded". Not being able to
    read the kernel's throttling counters is an absence of evidence, and must
    never be recorded as evidence of absence.
    """


def fit_limit(peak_cores: float) -> float:
    """The frozen rule, rounded up to hundredths.

    Rounding up rather than to nearest guarantees the stored number is never
    below what the rule produces, so the manifest cannot drift under the rule
    through accumulated rounding.
    """
    if peak_cores < 0:
        raise CpuLimitError(f"negative peak: {peak_cores!r}")
    return math.ceil(max(MULTIPLIER * peak_cores, FLOOR_CORES) * 100) / 100


def fit_limits(peaks: Mapping[str, float]) -> dict[str, float]:
    """Fit every service. A service with no measurement is an error.

    Defaulting an unmeasured service to the floor would quietly give it a
    limit nobody fitted, which is how a service ends up throttled for reasons
    no one can reconstruct.
    """
    if not peaks:
        raise CpuLimitError("no peaks supplied; nothing to fit")
    return {name: fit_limit(peak) for name, peak in sorted(peaks.items())}


@dataclass(frozen=True)
class DemandSample:
    """Observed CPU demand for one service, derived from the kernel.

    ``peak_cores`` is the highest rate seen between two consecutive readings.
    It is not a ``docker stats`` percentage: that is an average over a
    multi-second interval, which averages sub-second bursts away entirely.
    Fitting from those averages produced limits that the kernel then throttled
    at 17% of periods, which is the reason this type exists.
    """

    service: str
    peak_cores: float
    mean_cores: float
    intervals: int
    gap_intervals: int = 0


def parse_demand_stream(
    text: str,
    id_to_service: Mapping[str, str],
    *,
    expected_interval: float = 0.25,
) -> dict[str, DemandSample]:
    """Turn the sampler's raw output into per-service demand.

    Rates come from the *observed* elapsed time between readings rather than
    the requested interval, so loop overhead lengthens the interval instead of
    inflating the rate.
    """
    frames: list[tuple[float, dict[str, int]]] = []
    clock: float | None = None
    current: dict[str, int] = {}

    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("@"):
            if clock is not None:
                frames.append((clock, current))
            try:
                clock = float(line[1:])
            except ValueError:
                clock = None
            current = {}
            continue
        parts = line.split()
        if len(parts) == 2 and clock is not None:
            try:
                current[parts[0]] = int(parts[1])
            except ValueError:
                continue
    if clock is not None:
        frames.append((clock, current))

    if len(frames) < 2:
        raise CpuLimitError(
            f"demand sampling produced {len(frames)} frame(s); at least two "
            "are needed to compute a rate, and one frame is a cumulative "
            "counter rather than a measurement"
        )

    totals: dict[str, float] = {}
    elapsed: dict[str, float] = {}
    peaks: dict[str, float] = {}
    counts: dict[str, int] = {}
    gaps: dict[str, int] = {}
    # Per-service last-seen, not consecutive frames. A single failed read
    # would otherwise drop the service from the whole run, turning a transient
    # glitch into a lost measurement window.
    last: dict[str, tuple[float, int]] = {}

    for clock_value, frame in frames:
        for full_id, usage in frame.items():
            service = id_to_service.get(full_id)
            if service is None:
                continue
            previous = last.get(service)
            last[service] = (clock_value, usage)
            if previous is None:
                continue
            t0, u0 = previous
            dt = clock_value - t0
            if dt <= 0:
                # Clock resolution is 10ms; a tie carries no information and a
                # negative step would invert the rate.
                continue
            delta = usage - u0
            if delta < 0:
                # usage_usec is monotonic per container, so a decrease means
                # the container was replaced. That is not a negative rate.
                continue
            cores = (delta / 1_000_000.0) / dt
            peaks[service] = max(peaks.get(service, 0.0), cores)
            totals[service] = totals.get(service, 0.0) + delta / 1_000_000.0
            elapsed[service] = elapsed.get(service, 0.0) + dt
            counts[service] = counts.get(service, 0) + 1
            if dt > expected_interval * 1.5:
                # A rate averaged over a long gap understates a burst, which
                # is the exact error this module was written to remove, so
                # gaps are counted rather than passed over in silence.
                gaps[service] = gaps.get(service, 0) + 1

    return {
        service: DemandSample(
            service=service,
            peak_cores=peaks[service],
            mean_cores=(
                totals[service] / elapsed[service] if elapsed[service] > 0 else 0.0
            ),
            intervals=counts[service],
            gap_intervals=gaps.get(service, 0),
        )
        for service in sorted(peaks)
    }


def _full_ids_by_service(
    project: str, *, timeout: float, runner
) -> dict[str, str]:
    """Map full container id to Compose service name for one project.

    Full ids, because the cgroup directories are named by full id. Selection
    is by Compose project label rather than by name prefix: upstream sets an
    explicit ``container_name`` on every service, so names carry no project
    prefix and a name-based filter silently matches nothing.
    """
    listing = runner(
        [
            "docker", "ps", "-a",
            "--filter", f"label=com.docker.compose.project={project}",
            "--format", "{{.ID}}\t{{.Label \"com.docker.compose.service\"}}",
        ],
        capture_output=True, text=True, timeout=timeout,
    )
    if listing.returncode != 0:
        raise CpuLimitError(f"could not list containers: {listing.stderr.strip()}")

    short_ids = [
        line.split("\t", 1)[0].strip()
        for line in listing.stdout.splitlines()
        if "\t" in line and line.split("\t", 1)[0].strip()
    ]
    if not short_ids:
        raise CpuLimitError(
            f"project {project!r} has no containers; there is nothing to read, "
            "which is not the same as nothing being throttled"
        )

    inspect = runner(
        ["docker", "inspect", "-f",
         "{{.Id}}\t{{index .Config.Labels \"com.docker.compose.service\"}}",
         *short_ids],
        capture_output=True, text=True, timeout=timeout,
    )
    if inspect.returncode != 0:
        raise CpuLimitError(f"could not inspect containers: {inspect.stderr.strip()}")

    full: dict[str, str] = {}
    for line in inspect.stdout.splitlines():
        if "\t" in line:
            full_id, service = line.split("\t", 1)
            if full_id.strip() and service.strip():
                full[full_id.strip()] = service.strip()
    return full


def sample_cpu_demand(
    project: str,
    *,
    duration_seconds: float,
    interval_seconds: float = 0.25,
    timeout: float | None = None,
    runner=subprocess.run,
) -> dict[str, DemandSample]:
    """Sample per-service CPU demand from the kernel for a whole project.

    One long-lived sidecar loops internally rather than one container per
    sample, because container start-up costs about a second and would set a
    floor on the sampling interval far above the bursts we are trying to see.

    The clock is ``/proc/uptime``, not ``date +%s%N``: busybox ``date`` ignores
    ``%N`` and returns whole seconds, which silently collapses every sub-second
    interval to a zero time delta.
    """
    if duration_seconds <= 0:
        raise CpuLimitError("duration_seconds must be positive")
    if interval_seconds <= 0:
        raise CpuLimitError("interval_seconds must be positive")

    full = _full_ids_by_service(project, timeout=timeout or 120.0, runner=runner)

    reads = "; ".join(
        f'printf "%s " {full_id}; '
        f'awk "/usage_usec/{{print \\$2}}" /hostcg/docker/{full_id}/cpu.stat '
        f'2>/dev/null || echo'
        for full_id in full
    )
    script = (
        f'end=$(awk "{{print \\$1 + {duration_seconds}}}" /proc/uptime); '
        f'while :; do '
        f'now=$(cut -d" " -f1 /proc/uptime); '
        f'echo "@$now"; {reads}; '
        f'stop=$(awk -v n="$now" -v e="$end" "BEGIN{{print (n>=e)?1:0}}"); '
        f'[ "$stop" = "1" ] && break; '
        f'sleep {interval_seconds}; done'
    )

    result = runner(
        [
            "docker", "run", "--rm", "--cgroupns=host",
            "-v", f"{_CGROUP_ROOT}:/hostcg:ro",
            "--entrypoint", "sh", SIDECAR_IMAGE, "-c", script,
        ],
        capture_output=True, text=True,
        timeout=timeout or (duration_seconds + 120.0),
    )
    if result.returncode != 0:
        raise CpuLimitError(f"demand sidecar failed: {result.stderr.strip()[:400]}")

    samples = parse_demand_stream(
        result.stdout, full, expected_interval=interval_seconds
    )
    missing = sorted(set(full.values()) - set(samples))
    if missing:
        raise CpuLimitError(
            f"no demand samples for {missing}; a service that was not measured "
            "must not be fitted from a peak of zero"
        )
    return samples


def cpu_limits_path(repo_root: Path) -> Path:
    return repo_root / "benchmark/apps/astronomy-shop/cpu-limits.json"


def fitted_limits_hash(payload: Mapping[str, Any]) -> str:
    """Hash the parts of the fit that change what actually runs.

    Over the rule, the fitted limits, and the host class they were fitted on.
    The peaks and means that motivated the fit are provenance, and re-measuring
    them on the same host will move the last decimal place without changing a
    single quota, so including them would make the hash report a fixture change
    that is not one. Everything that reaches a container is covered.

    The host class is in the basis even though it reaches no container, because
    it decides whether these numbers may be used at all. Left out, the label
    could be edited to name any machine and the hash would still verify, which
    would turn the class check below into a formality that agrees with whatever
    the file claims.
    """
    basis = {
        "rule": dict(payload.get("rule", {})),
        "limitCores": dict(payload.get("limitCores", {})),
        "hostClass": (payload.get("fittedFrom") or {}).get("hostClass"),
    }
    canonical = json.dumps(basis, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def load_fitted_limits(repo_root: Path, host_class: str) -> dict[str, Any]:
    """Load the frozen limits for this host class, or refuse.

    The class is required rather than optional. These limits were fitted from
    one machine's measured demand, and applying them elsewhere would silently
    impose a 10-core laptop's quotas on a host that may have two. A default
    would be read as agreement by every caller that forgot to pass one.
    """
    path = cpu_limits_path(repo_root)
    if not path.exists():
        raise CpuLimitError(
            f"{path} is missing; fit limits before running, because an "
            "unlimited service is the variance this module exists to remove"
        )
    payload = json.loads(path.read_text())
    stored_rule = payload.get("rule", {})
    # The rule that produced the file must still be the rule in force. If they
    # diverge, the committed numbers describe a fitting nobody can reproduce.
    if (
        stored_rule.get("multiplier") != MULTIPLIER
        or stored_rule.get("floorCores") != FLOOR_CORES
    ):
        raise CpuLimitError(
            f"{path} was fitted with rule {stored_rule!r}, but the frozen rule "
            f"is multiplier={MULTIPLIER} floorCores={FLOOR_CORES}; refit or "
            "restore the constants"
        )
    # Limits fitted on one host class say nothing about another. This is the
    # same refusal offered_load.load_band makes, and for the same reason.
    stored_class = (payload.get("fittedFrom") or {}).get("hostClass")
    if stored_class != host_class:
        raise CpuLimitError(
            f"{path} was fitted on host class {stored_class!r} but this host "
            f"is {host_class!r}; fit limits here rather than borrowing them"
        )
    # The hash goes into provenance, so it has to be recomputed from the file
    # rather than trusted. An earlier revision carried a hash that matched no
    # basis anyone could reconstruct, which is provenance that proves nothing.
    stored_hash = payload.get("manifestHash")
    actual_hash = fitted_limits_hash(payload)
    if stored_hash != actual_hash:
        raise CpuLimitError(
            f"{path} records manifestHash {stored_hash!r} but its contents "
            f"hash to {actual_hash!r}; the file was edited without refitting"
        )
    # A limit that does not follow the rule would make the rule a comment.
    for service, peak in sorted(payload.get("peakCores", {}).items()):
        expected = fit_limit(peak)
        stored = payload.get("limitCores", {}).get(service)
        if stored != expected:
            raise CpuLimitError(
                f"{path}: {service} has limit {stored} but the rule applied "
                f"to its peak of {peak} cores gives {expected}"
            )
    return payload


@dataclass(frozen=True)
class ThrottleReading:
    """One container's cgroup counters at one instant."""

    service: str
    nr_periods: int
    nr_throttled: int
    throttled_usec: int
    quota_cores: float | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "nrPeriods": self.nr_periods,
            "nrThrottled": self.nr_throttled,
            "throttledUsec": self.throttled_usec,
            "quotaCores": self.quota_cores,
        }


@dataclass(frozen=True)
class ServiceThrottle:
    """What happened to one service across a measurement window."""

    service: str
    periods: int
    throttled: int
    throttled_usec: int
    quota_cores: float | None
    cumulative_throttled: int

    @property
    def percent(self) -> float:
        """Throttled periods as a percentage of periods in the window.

        Zero periods means the window was too short for the scheduler to
        account anything, which is unmeasured rather than clean; callers must
        treat it through ``measured`` rather than reading this as 0.0.
        """
        if self.periods <= 0:
            return 0.0
        return 100.0 * self.throttled / self.periods

    @property
    def measured(self) -> bool:
        return self.periods > 0

    def to_dict(self) -> dict[str, Any]:
        return {
            "service": self.service,
            "periods": self.periods,
            "throttled": self.throttled,
            "throttledUsec": self.throttled_usec,
            "percent": round(self.percent, 4),
            "quotaCores": self.quota_cores,
            "cumulativeThrottled": self.cumulative_throttled,
            "measured": self.measured,
        }


@dataclass(frozen=True)
class ThrottleVerdict:
    services: tuple[ServiceThrottle, ...]
    unmeasured: tuple[str, ...] = ()
    missing: tuple[str, ...] = ()
    lifetime_bound: tuple[str, ...] = ()
    error: str = ""

    @property
    def accepted(self) -> bool:
        """Every expected service was measured and never once throttled.

        An unmeasured or missing service fails. A verdict that passed because
        a service was absent from the reading would be the same vacuity we
        keep finding: a check that examined nothing and reported success.

        The criterion is the kernel's lifetime counter rather than a sampled
        window, because measurement showed most throttling happens before any
        window opens, while containers start, and startup throttling makes
        readiness timing depend on host contention. An earlier version also
        gated on a window budget. That was removed rather than kept: periods
        throttled inside a window are a subset of those counted since
        container start, so this criterion subsumes it and the threshold
        would have been unreachable decoration. Per-service window figures
        are still reported, because they localise *when* throttling happened,
        which is a diagnostic the lifetime total cannot give.
        """
        return (
            not self.error
            and not self.unmeasured
            and not self.missing
            and not self.lifetime_bound
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "accepted": self.accepted,
            "services": [s.to_dict() for s in self.services],
            "unmeasured": list(self.unmeasured),
            "missing": list(self.missing),
            "lifetimeBound": list(self.lifetime_bound),
            "error": self.error,
        }


def _parse_cpu_stat(text: str) -> dict[str, int]:
    values: dict[str, int] = {}
    for line in text.splitlines():
        parts = line.split()
        if len(parts) == 2:
            try:
                values[parts[0]] = int(parts[1])
            except ValueError:
                continue
    return values


def _parse_cpu_max(text: str) -> float | None:
    """``cpu.max`` is ``<quota> <period>``, or ``max <period>`` when unset."""
    parts = text.split()
    if len(parts) != 2:
        return None
    quota, period = parts
    if quota == "max":
        return None
    try:
        return int(quota) / int(period)
    except (ValueError, ZeroDivisionError):
        return None


def read_throttling(
    project: str,
    *,
    timeout: float = 120.0,
    runner=subprocess.run,
) -> dict[str, ThrottleReading]:
    """Read every container in ``project`` from the host cgroup hierarchy.

    One sidecar reads all of them, rather than one exec per container, so the
    readings are close to simultaneous and the two distroless services are
    covered by the same mechanism as everything else.
    """
    full = _full_ids_by_service(project, timeout=timeout, runner=runner)

    script_lines = []
    for full_id in full:
        script_lines.append(
            f'echo "==={full_id}"; '
            f'cat /hostcg/docker/{full_id}/cpu.stat 2>/dev/null; '
            f'echo "---max"; '
            f'cat /hostcg/docker/{full_id}/cpu.max 2>/dev/null'
        )
    result = runner(
        [
            "docker", "run", "--rm", "--cgroupns=host",
            "-v", f"{_CGROUP_ROOT}:/hostcg:ro",
            "--entrypoint", "sh", SIDECAR_IMAGE, "-c", "; ".join(script_lines),
        ],
        capture_output=True, text=True, timeout=timeout,
    )
    if result.returncode != 0:
        raise CpuLimitError(f"cgroup sidecar failed: {result.stderr.strip()[:400]}")

    readings: dict[str, ThrottleReading] = {}
    current: str | None = None
    stat_text: list[str] = []
    max_text = ""
    in_max = False

    def flush() -> None:
        if current is None:
            return
        service = full.get(current)
        if not service:
            return
        values = _parse_cpu_stat("\n".join(stat_text))
        if "nr_periods" not in values:
            return
        readings[service] = ThrottleReading(
            service=service,
            nr_periods=values.get("nr_periods", 0),
            nr_throttled=values.get("nr_throttled", 0),
            throttled_usec=values.get("throttled_usec", 0),
            quota_cores=_parse_cpu_max(max_text),
        )

    for line in result.stdout.splitlines():
        if line.startswith("==="):
            flush()
            current, stat_text, max_text, in_max = line[3:].strip(), [], "", False
        elif line.strip() == "---max":
            in_max = True
        elif in_max:
            max_text = line.strip()
        else:
            stat_text.append(line)
    flush()
    return readings


def verdict_from_readings(
    opened: Mapping[str, ThrottleReading],
    closed: Mapping[str, ThrottleReading],
    expected: Mapping[str, float],
) -> ThrottleVerdict:
    """Judge a window, and judge each container's whole life alongside it."""
    services: list[ServiceThrottle] = []
    unmeasured: list[str] = []
    missing: list[str] = []
    bound: list[str] = []

    for name in sorted(expected):
        start, end = opened.get(name), closed.get(name)
        if start is None or end is None:
            missing.append(name)
            continue
        entry = ServiceThrottle(
            service=name,
            periods=end.nr_periods - start.nr_periods,
            throttled=end.nr_throttled - start.nr_throttled,
            throttled_usec=end.throttled_usec - start.throttled_usec,
            quota_cores=end.quota_cores,
            cumulative_throttled=end.nr_throttled,
        )
        services.append(entry)
        if not entry.measured:
            unmeasured.append(name)
        # Counted from container start, so this catches throttling spent
        # before the window opened, which is where nearly all of it was.
        # Independent of how many periods elapsed inside the window, so an
        # unmeasured service is still judged on its lifetime.
        if entry.cumulative_throttled > 0:
            bound.append(name)

    # A reading for a service nobody expected means the project contains
    # something the fitted set does not describe.
    for name in sorted(set(closed) - set(expected)):
        missing.append(f"{name} (present but not fitted)")

    return ThrottleVerdict(
        services=tuple(services),
        unmeasured=tuple(unmeasured),
        missing=tuple(missing),
        lifetime_bound=tuple(bound),
    )
