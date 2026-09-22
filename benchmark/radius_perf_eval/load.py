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
        }


@dataclass(frozen=True)
class Sample:
    started_at: float
    latency_seconds: float
    status: int
    path: str


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
            "statusCounts": dict(self.status_counts),
        }


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


def run_load(base_url: str, profile: LoadProfile) -> LoadResult:
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
        status_counts=status_counts,
    )
