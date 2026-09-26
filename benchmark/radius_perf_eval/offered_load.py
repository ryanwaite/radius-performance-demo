"""Measure the load actually offered to the stack, and gate verdicts on it.

Every tolerance this driver freezes assumes the load was the same. Nothing
checked that. The CPU work made the cost of not checking concrete: a fit whose
positive control passed only because an upstream service had been throttled,
which flattened the arrival stream and left the service under test looking
idle. The measurement was wrong and nothing in the run record said so.

So the rule here is the one that failure earned. Record the independent
variable in every cycle and refuse a verdict when it moved. A cycle outside
the band is **not a failure of the system under test**. It is a failed
measurement, and it is counted rather than scored, because scoring it either
way would put a number nobody can trust into the results.

What is measured
----------------
Locust's own aggregated request counter, read from the load generator's web
API at two instants and differenced. Two reasons for that source over
Prometheus or the proxy's stats. It is the generator's own account of what it
issued, so it measures the input rather than what the system managed to serve.
And it is a cumulative counter, so a rate computed from two readings is a true
average over the window rather than an instantaneous sample that happened to
land on a burst.

A closed loop, and what that costs
----------------------------------
The upstream generator is closed-loop: a fixed number of simulated users, each
waiting for a response before issuing the next request. Offered load is
therefore **not fully independent** of the system's health. If the application
slows, achieved throughput falls even though the generator's configuration has
not changed.

That has a sharp consequence and it is not a detail. This band belongs to the
healthy baseline only. Applied to an incident phase it would refuse a verdict
precisely when an incident worked, which is the opposite of what a gate is
for. Incident phases need their own expectation, and the drop in achieved rate
there is a signal rather than a fault.

Within the healthy baseline the gate does what it is meant to. A throttled
generator, a starved host, or a noisy neighbour all show up as a rate outside
the band, whatever their cause, and none of them can quietly become a
"healthy" number.
"""

from __future__ import annotations

import hashlib
import json
import statistics
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

LOAD_SERVICE = "load-generator"
LOCUST_PORT = 8089
LOCUST_STATS_PATH = "/stats/requests"

# Locust names the roll-up row this. Summing the per-endpoint rows instead
# would double count, because this row is itself in the list.
AGGREGATE_ROW = "Aggregated"

# The band is stored as a fraction of the fitted target. Frozen here so the
# committed file cannot quietly widen it; see `load_band`.
BAND_FRACTION = 0.20

# A window shorter than this makes the rate a sample of one burst rather than
# an average. Locust's counter moves in steps as users complete requests.
MIN_WINDOW_SECONDS = 30.0

# Fitting a band from one or two cycles freezes whatever those cycles happened
# to do.
MIN_FIT_SAMPLES = 5


class OfferedLoadError(RuntimeError):
    """Raised when the offered load cannot be established at all.

    Distinct from a cycle being out of band. Not knowing the rate and knowing
    it was wrong are different states, and only one of them is evidence.
    """


def _probe_source() -> str:
    """The program run inside the load generator to read its own counter.

    Run in the generator rather than from the host, so no port has to be
    published. That matters because the stack moves to an internal network,
    where Docker publishes nothing, and a measurement that depends on a
    published port would have to punch a hole for itself.

    The container's own `CLOCK_MONOTONIC` is read in the same call as the
    counter. Timing the `docker exec` round trip from the host instead would
    fold process startup, which is tens of milliseconds and varies with host
    load, into the elapsed time and therefore into the rate.
    """
    return (
        "import json,time,urllib.request;"
        f"d=json.load(urllib.request.urlopen('http://127.0.0.1:{LOCUST_PORT}"
        f"{LOCUST_STATS_PATH}',timeout=10));"
        "print(json.dumps({'clock':time.monotonic(),'stats':d}))"
    )


@dataclass(frozen=True)
class LoadReading:
    """The generator's cumulative counters at one instant."""

    clock: float
    requests: int
    failures: int
    users: int
    state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "clock": round(self.clock, 3),
            "requests": self.requests,
            "failures": self.failures,
            "users": self.users,
            "state": self.state,
        }


def parse_reading(payload: str) -> LoadReading:
    """Turn one probe's stdout into a reading.

    Every failure here is raised rather than defaulted. A missing aggregate
    row defaulting to zero requests would read as a stopped generator, and a
    stopped generator is exactly the condition this module exists to catch, so
    it must never be produced by a parsing accident.
    """
    text = (payload or "").strip()
    if not text:
        raise OfferedLoadError("offered-load probe produced no output")
    # The image may log to stdout before the probe prints. The payload is the
    # last line, and it is the only one that has to parse.
    line = text.splitlines()[-1]
    try:
        document = json.loads(line)
    except json.JSONDecodeError as error:
        raise OfferedLoadError(
            f"offered-load probe output is not JSON: {line[:200]!r}"
        ) from error

    stats = document.get("stats") or {}
    rows = stats.get("stats")
    if not isinstance(rows, list):
        raise OfferedLoadError("offered-load probe returned no stats rows")

    aggregate = next(
        (row for row in rows if row.get("name") == AGGREGATE_ROW), None
    )
    if aggregate is None:
        raise OfferedLoadError(
            f"offered-load probe found no {AGGREGATE_ROW!r} row; "
            f"saw {[row.get('name') for row in rows][:10]}"
        )

    clock = document.get("clock")
    if not isinstance(clock, (int, float)):
        raise OfferedLoadError("offered-load probe returned no clock")

    return LoadReading(
        clock=float(clock),
        requests=int(aggregate.get("num_requests", 0)),
        failures=int(aggregate.get("num_failures", 0)),
        users=int(stats.get("user_count", 0)),
        state=str(stats.get("state", "")),
    )


def read_offered_load(
    project: str,
    compose_file: str,
    *,
    service: str = LOAD_SERVICE,
    runner=subprocess.run,
    timeout: float = 60.0,
) -> LoadReading:
    """Read the generator's counters once."""
    result = runner(
        [
            "docker", "compose", "-f", compose_file, "-p", project,
            "exec", "-T", service, "python", "-c", _probe_source(),
        ],
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    if result.returncode != 0:
        raise OfferedLoadError(
            f"offered-load probe failed in {service}: "
            f"{(result.stderr or '').strip()[:400]}"
        )
    return parse_reading(result.stdout)


@dataclass(frozen=True)
class AchievedLoad:
    """What the generator actually issued across one window."""

    requests: int
    failures: int
    elapsed_seconds: float
    requests_per_second: float
    failure_ratio: float
    users: int
    state: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "failures": self.failures,
            "elapsedSeconds": round(self.elapsed_seconds, 3),
            "requestsPerSecond": round(self.requests_per_second, 4),
            "failureRatio": round(self.failure_ratio, 6),
            "users": self.users,
            "state": self.state,
        }


def achieved_between(first: LoadReading, second: LoadReading) -> AchievedLoad:
    """Difference two readings into a rate.

    A counter that went backwards means the generator restarted and reset it.
    That is not a negative rate and it is not a small one either, so it is an
    error rather than a clamp to zero: a clamp would report a stalled
    generator as a slow one.
    """
    elapsed = second.clock - first.clock
    if elapsed <= 0:
        raise OfferedLoadError(
            f"offered-load readings are {elapsed:.3f}s apart; a rate needs a "
            "positive interval"
        )
    delta = second.requests - first.requests
    if delta < 0:
        raise OfferedLoadError(
            "the generator's request counter decreased, so it restarted "
            "mid-window and the two readings describe different runs"
        )
    failures = max(0, second.failures - first.failures)
    return AchievedLoad(
        requests=delta,
        failures=failures,
        elapsed_seconds=elapsed,
        requests_per_second=delta / elapsed,
        failure_ratio=(failures / delta) if delta else 0.0,
        users=second.users,
        state=second.state,
    )


@dataclass(frozen=True)
class LoadBand:
    """The frozen band a healthy cycle's achieved rate must fall inside."""

    host_class: str
    target_rps: float
    fraction: float
    expected_users: int

    @property
    def low_rps(self) -> float:
        return self.target_rps * (1.0 - self.fraction)

    @property
    def high_rps(self) -> float:
        return self.target_rps * (1.0 + self.fraction)

    def to_dict(self) -> dict[str, Any]:
        return {
            "hostClass": self.host_class,
            "targetRps": round(self.target_rps, 4),
            "fraction": self.fraction,
            "lowRps": round(self.low_rps, 4),
            "highRps": round(self.high_rps, 4),
            "expectedUsers": self.expected_users,
        }


@dataclass(frozen=True)
class LoadVerdict:
    """Whether a cycle may be scored at all.

    Three states rather than two. `scored` means the measurement stands.
    `no_verdict` means the cycle is counted and discarded, which is neither a
    pass nor a fail. Collapsing those two into a boolean is what lets a bad
    measurement be recorded as a good result.
    """

    achieved: AchievedLoad
    band: LoadBand
    in_band: bool
    scored: bool
    reasons: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "achieved": self.achieved.to_dict(),
            "band": self.band.to_dict(),
            "inBand": self.in_band,
            "scored": self.scored,
            "verdict": "scored" if self.scored else "no-verdict",
            "reasons": list(self.reasons),
        }


def evaluate_offered_load(
    achieved: AchievedLoad,
    band: LoadBand,
    *,
    min_window_seconds: float = MIN_WINDOW_SECONDS,
) -> LoadVerdict:
    """Decide whether this cycle's measurement may be scored."""
    reasons: list[str] = []

    in_band = band.low_rps <= achieved.requests_per_second <= band.high_rps
    if not in_band:
        reasons.append(
            f"achieved {achieved.requests_per_second:.3f} rps is outside the "
            f"band {band.low_rps:.3f} to {band.high_rps:.3f}"
        )

    if achieved.elapsed_seconds < min_window_seconds:
        reasons.append(
            f"window of {achieved.elapsed_seconds:.1f}s is shorter than the "
            f"{min_window_seconds:.0f}s needed for a rate to be an average"
        )
    if achieved.state != "running":
        reasons.append(
            f"generator state is {achieved.state!r} rather than 'running'"
        )
    if achieved.users != band.expected_users:
        reasons.append(
            f"generator ran {achieved.users} users, not the "
            f"{band.expected_users} the band was fitted on"
        )
    if achieved.requests == 0:
        reasons.append("generator issued no requests at all")

    return LoadVerdict(
        achieved=achieved,
        band=band,
        in_band=in_band,
        scored=not reasons,
        reasons=tuple(reasons),
    )


def fit_band(
    samples: Sequence[float],
    *,
    host_class: str,
    expected_users: int,
    fraction: float = BAND_FRACTION,
    min_samples: int = MIN_FIT_SAMPLES,
) -> LoadBand:
    """Fit the band from healthy cycles.

    The target is the median rather than the mean. One cycle that stalled
    drags a mean down and widens the band around a number no cycle produced,
    which is the opposite of what freezing is for.
    """
    values = [float(value) for value in samples]
    if len(values) < min_samples:
        raise OfferedLoadError(
            f"fitting a band needs at least {min_samples} healthy cycles, "
            f"got {len(values)}"
        )
    if any(value <= 0 for value in values):
        raise OfferedLoadError(
            "a healthy cycle with a non-positive rate is a broken measurement "
            "and must not be fitted into the band"
        )
    return LoadBand(
        host_class=host_class,
        target_rps=statistics.median(values),
        fraction=fraction,
        expected_users=expected_users,
    )


def band_covers(band: LoadBand, samples: Iterable[float]) -> list[float]:
    """Return the fitting samples the fitted band would itself reject.

    The acceptance test for a band. A band that excludes the cycles it was fitted
    on is too narrow to be usable, and a band nobody checked against its own
    inputs is a number rather than a measurement.
    """
    return [
        value
        for value in samples
        if not band.low_rps <= float(value) <= band.high_rps
    ]


def offered_load_path(repo_root: Path) -> Path:
    return repo_root / "benchmark/apps/astronomy-shop/offered-load.json"


def band_hash(payload: Mapping[str, Any]) -> str:
    """Hash the parts of the fit that decide a verdict.

    The per-cycle samples that motivated the band are provenance. Re-measuring
    moves them without moving the band, so including them would report a
    fixture change that is not one. The target, the width and the user count
    are what a gate acts on, so those are covered.
    """
    basis = {
        "hostClass": payload.get("hostClass"),
        "targetRps": payload.get("targetRps"),
        "fraction": payload.get("fraction"),
        "expectedUsers": payload.get("expectedUsers"),
    }
    canonical = json.dumps(basis, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def load_band(repo_root: Path, host_class: str) -> LoadBand:
    """Load the frozen band for this host class, or refuse.

    Refusing is the point. A host nobody fitted a band on cannot be handed the
    laptop's band, because the whole reason for the band is that it describes
    one machine's behaviour under one configuration.
    """
    path = offered_load_path(repo_root)
    if not path.exists():
        raise OfferedLoadError(
            f"{path} is missing; fit an offered-load band before scoring, "
            "because an unmeasured load makes every tolerance unfounded"
        )
    payload = json.loads(path.read_text())

    stored_class = payload.get("hostClass")
    if stored_class != host_class:
        raise OfferedLoadError(
            f"{path} was fitted on host class {stored_class!r} but this host "
            f"is {host_class!r}; fit a band here rather than borrowing one"
        )

    stored_fraction = payload.get("fraction")
    if stored_fraction != BAND_FRACTION:
        raise OfferedLoadError(
            f"{path} records a band fraction of {stored_fraction!r} but the "
            f"frozen fraction is {BAND_FRACTION}; a file that widens its own "
            "band can pass anything"
        )

    stored_hash = payload.get("manifestHash")
    actual_hash = band_hash(payload)
    if stored_hash != actual_hash:
        raise OfferedLoadError(
            f"{path} records manifestHash {stored_hash!r} but its contents "
            f"hash to {actual_hash!r}; the file was edited without refitting"
        )

    return LoadBand(
        host_class=stored_class,
        target_rps=float(payload["targetRps"]),
        fraction=float(stored_fraction),
        expected_users=int(payload["expectedUsers"]),
    )
