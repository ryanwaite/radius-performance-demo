"""Deterministic closed-loop HTTP load generator.

k6 is deliberately not used here. The benchmark needs a load source with no
external binary dependency, a fixed and seeded request sequence, an explicit
warm-up exclusion, and raw per-request latencies the driver can reduce into
variance statistics. The profile is closed-loop with a fixed worker count, so
throughput is governed by the application's service time rather than by host
scheduling of an open-loop arrival process.
"""

from __future__ import annotations

import http.client
import math
import random
import statistics
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import urlsplit


@dataclass(frozen=True)
class LoadProfile:
    """A fixed, declarable workload. Two runs with the same profile issue the
    same request sequence per worker."""

    name: str
    concurrency: int = 8
    duration_seconds: float = 30.0
    warmup_seconds: float = 6.0
    seed: int = 1842
    list_limit: int = 10
    product_ids: int = 10
    request_timeout_seconds: float = 10.0

    # A request is a "stall" when it exceeds this multiple of the phase's own
    # median latency. The median is used rather than a fixed threshold so the
    # definition self-calibrates to each phase: the healthy phase runs at ~29ms
    # and the incident phase at ~506ms, and a single absolute bound could not
    # describe both. 3x sits far above normal jitter in both phases (measured
    # maxima are ~1.4x the median) and still catches the 1.85s outlier that
    # distorted an early ten-cycle run.
    stall_factor: float = 3.0

    # An absolute latency bound for this phase, reported alongside the
    # phase-relative stall rate. The relative rule adapts to each phase,
    # which is what makes it usable across a 29ms and a 506ms workload, but
    # it also moves when the median moves: a uniformly slower environment
    # raises the threshold with it and can report zero stalls while being
    # plainly worse. The absolute count cannot do that.
    absolute_excursion_seconds: float = 1.0

    def to_dict(self) -> dict[str, float | int | str]:
        return {
            "name": self.name,
            "concurrency": self.concurrency,
            "durationSeconds": self.duration_seconds,
            "warmupSeconds": self.warmup_seconds,
            "seed": self.seed,
            "listLimit": self.list_limit,
            "productIds": self.product_ids,
            "requestTimeoutSeconds": self.request_timeout_seconds,
            "stallFactor": self.stall_factor,
            "absoluteExcursionSeconds": self.absolute_excursion_seconds,
        }


@dataclass(frozen=True)
class Sample:
    started_at: float
    latency_seconds: float
    status: int
    path: str


@dataclass(frozen=True)
class StallEvent:
    """One stalled request, with enough time context to test a periodicity claim.

    A stall was previously recorded as a bare latency. That is enough to say a
    stall happened and how big it was, and not enough to say anything about
    *when*. Two stalls at a similar elapsed time into a suite would point at a
    periodic host or database event -- a Docker Desktop VM task, a macOS
    background job, a MySQL purge or checkpoint -- and two at unrelated times
    would not. Both timestamps are kept because they answer different
    questions: wall clock is comparable against host logs, and suite-elapsed is
    comparable across suites that started at different times of day.
    """

    latency_seconds: float
    epoch: float
    wall_clock: str
    phase_elapsed_seconds: float
    suite_elapsed_seconds: float | None

    def to_dict(self) -> dict[str, object]:
        return {
            "latencySeconds": round(self.latency_seconds, 6),
            "epoch": self.epoch,
            "wallClock": self.wall_clock,
            "phaseElapsedSeconds": round(self.phase_elapsed_seconds, 3),
            "suiteElapsedSeconds": (
                None
                if self.suite_elapsed_seconds is None
                else round(self.suite_elapsed_seconds, 3)
            ),
        }


@dataclass(frozen=True)
class LoadResult:
    profile: LoadProfile
    started_at: float
    ended_at: float
    measured_start: float
    measured_end: float
    measured_start_epoch: float
    measured_end_epoch: float
    total_requests: int
    measured_requests: int
    error_requests: int
    throughput_rps: float
    latency_p50_seconds: float
    latency_p95_seconds: float
    latency_p99_seconds: float
    latency_mean_seconds: float
    latency_max_seconds: float
    error_rate: float
    stall_threshold_seconds: float
    stall_count: int
    stall_rate: float
    excursion_count: int = 0
    excursion_rate: float = 0.0
    stall_latencies_seconds: tuple[float, ...] = ()
    stall_events: tuple[StallEvent, ...] = ()
    latency_distribution: tuple[float, ...] = ()
    status_counts: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "profile": self.profile.to_dict(),
            "startedAt": self.started_at,
            "endedAt": self.ended_at,
            "measuredWindow": {
                "startMonotonic": self.measured_start,
                "endMonotonic": self.measured_end,
                "startEpoch": self.measured_start_epoch,
                "endEpoch": self.measured_end_epoch,
            },
            "totalRequests": self.total_requests,
            "measuredRequests": self.measured_requests,
            "errorRequests": self.error_requests,
            "throughputRps": self.throughput_rps,
            "latencyP50Seconds": self.latency_p50_seconds,
            "latencyP95Seconds": self.latency_p95_seconds,
            "latencyP99Seconds": self.latency_p99_seconds,
            "latencyMeanSeconds": self.latency_mean_seconds,
            "latencyMaxSeconds": self.latency_max_seconds,
            "errorRate": self.error_rate,
            "stalls": {
                "thresholdSeconds": self.stall_threshold_seconds,
                "count": self.stall_count,
                "rate": self.stall_rate,
                "latenciesSeconds": list(self.stall_latencies_seconds),
                "events": [event.to_dict() for event in self.stall_events],
            },
            "absoluteExcursions": {
                "thresholdSeconds": self.profile.absolute_excursion_seconds,
                "count": self.excursion_count,
                "rate": self.excursion_rate,
            },
            # Full sorted sample, so later analysis can re-derive any threshold
            # rather than being stuck with the one chosen here.
            "latencyDistributionSeconds": [
                round(value, 6) for value in self.latency_distribution
            ],
            "statusCounts": dict(self.status_counts),
        }


def detect_stalls(
    latencies: list[float], factor: float
) -> tuple[float, list[float]]:
    """Classify requests that exceed ``factor`` times the median latency.

    The threshold is relative to the sample's own median so that the same
    definition applies to a 29ms healthy phase and a 506ms incident phase. A
    stall is strictly greater than the threshold, so a perfectly uniform
    sample yields none.
    """
    if not latencies:
        return math.nan, []
    threshold = percentile(latencies, 0.50) * factor
    return threshold, sorted(
        (value for value in latencies if value > threshold), reverse=True
    )


def stall_events(
    samples: list[Sample],
    threshold: float,
    *,
    epoch_offset: float,
    phase_start: float,
    suite_start_epoch: float | None,
) -> list[StallEvent]:
    """Timestamp every stalled request, ordered by when it happened."""
    if math.isnan(threshold):
        return []
    events = []
    for sample in samples:
        if sample.latency_seconds <= threshold:
            continue
        epoch = sample.started_at + epoch_offset
        events.append(
            StallEvent(
                latency_seconds=sample.latency_seconds,
                epoch=epoch,
                wall_clock=datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat(),
                phase_elapsed_seconds=sample.started_at - phase_start,
                suite_elapsed_seconds=(
                    None if suite_start_epoch is None else epoch - suite_start_epoch
                ),
            )
        )
    return sorted(events, key=lambda event: event.epoch)


def percentile(values: list[float], fraction: float) -> float:
    """Nearest-rank percentile. Deterministic and free of interpolation drift."""
    if not values:
        return math.nan
    if not 0.0 < fraction <= 1.0:
        raise ValueError("fraction must be in (0, 1]")
    ordered = sorted(values)
    rank = max(1, math.ceil(fraction * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _request_sequence(profile: LoadProfile, worker_index: int) -> random.Random:
    return random.Random((profile.seed << 8) ^ worker_index)


def _worker(
    host: str,
    port: int,
    profile: LoadProfile,
    worker_index: int,
    stop_at: float,
    stop_event: threading.Event,
    sink: list[Sample],
) -> None:
    rng = _request_sequence(profile, worker_index)
    connection = http.client.HTTPConnection(host, port, timeout=profile.request_timeout_seconds)
    local: list[Sample] = []
    try:
        while not stop_event.is_set() and time.monotonic() < stop_at:
            if rng.random() < 0.5:
                path = f"/api/products?limit={profile.list_limit}"
            else:
                path = f"/api/products/{rng.randint(1, profile.product_ids)}"

            started = time.monotonic()
            status = 0
            try:
                connection.request("GET", path)
                response = connection.getresponse()
                status = response.status
                response.read()
            except Exception:
                status = 0
                try:
                    connection.close()
                except Exception:
                    pass
                connection = http.client.HTTPConnection(
                    host, port, timeout=profile.request_timeout_seconds
                )
            local.append(Sample(started, time.monotonic() - started, status, path))
    finally:
        try:
            connection.close()
        except Exception:
            pass
        sink.extend(local)


def run_load(
    base_url: str,
    profile: LoadProfile,
    *,
    suite_start_epoch: float | None = None,
) -> LoadResult:
    """Drive the profile against ``base_url`` and reduce it to statistics."""
    parts = urlsplit(base_url)
    host = parts.hostname or "127.0.0.1"
    port = parts.port or 80

    samples: list[Sample] = []
    sinks: list[list[Sample]] = [[] for _ in range(profile.concurrency)]
    stop_event = threading.Event()

    epoch_offset = time.time() - time.monotonic()
    started_at = time.monotonic()
    stop_at = started_at + profile.duration_seconds
    threads = [
        threading.Thread(
            target=_worker,
            args=(host, port, profile, index, stop_at, stop_event, sinks[index]),
            name=f"load-{profile.name}-{index}",
            daemon=True,
        )
        for index in range(profile.concurrency)
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=profile.duration_seconds + profile.request_timeout_seconds + 30)
    stop_event.set()
    ended_at = time.monotonic()

    for sink in sinks:
        samples.extend(sink)

    measured_start = started_at + profile.warmup_seconds
    measured_end = stop_at
    measured = [s for s in samples if measured_start <= s.started_at < measured_end]

    latencies = [s.latency_seconds for s in measured]
    errors = [s for s in measured if s.status == 0 or s.status >= 500]
    status_counts: dict[str, int] = {}
    for sample in measured:
        key = str(sample.status) if sample.status else "transport_error"
        status_counts[key] = status_counts.get(key, 0) + 1

    window = max(measured_end - measured_start, 1e-9)

    # Stalls are counted directly rather than left to show up as variance in the
    # averaged throughput. Widening the measurement window makes throughput
    # robust to a rare stall, which is the point, but it also means a rising
    # stall rate would be quietly absorbed. Counting them restores that signal.
    stall_threshold, stalls = detect_stalls(latencies, profile.stall_factor)
    events = stall_events(
        measured,
        stall_threshold,
        epoch_offset=epoch_offset,
        phase_start=measured_start,
        suite_start_epoch=suite_start_epoch,
    )
    excursions = [
        value for value in latencies if value > profile.absolute_excursion_seconds
    ]

    return LoadResult(
        profile=profile,
        started_at=started_at,
        ended_at=ended_at,
        measured_start=measured_start,
        measured_end=measured_end,
        measured_start_epoch=measured_start + epoch_offset,
        measured_end_epoch=measured_end + epoch_offset,
        total_requests=len(samples),
        measured_requests=len(measured),
        error_requests=len(errors),
        throughput_rps=len(measured) / window,
        latency_p50_seconds=percentile(latencies, 0.50),
        latency_p95_seconds=percentile(latencies, 0.95),
        latency_p99_seconds=percentile(latencies, 0.99),
        latency_mean_seconds=statistics.fmean(latencies) if latencies else math.nan,
        latency_max_seconds=max(latencies) if latencies else math.nan,
        error_rate=(len(errors) / len(measured)) if measured else math.nan,
        stall_threshold_seconds=stall_threshold,
        stall_count=len(stalls),
        stall_rate=(len(stalls) / len(measured)) if measured else math.nan,
        stall_latencies_seconds=tuple(stalls[:10]),
        stall_events=tuple(events),
        excursion_count=len(excursions),
        excursion_rate=(len(excursions) / len(measured)) if measured else math.nan,
        latency_distribution=tuple(sorted(latencies)),
        status_counts=status_counts,
    )
