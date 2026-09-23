"""Repeated-cycle determinism harness.

The Phase 2 exit criterion is not "the driver works once". It is that ten
consecutive create -> inject -> load -> measure -> destroy cycles reproduce the
same baseline measurements inside a declared tolerance and leave nothing behind.
This module runs those cycles, computes the run-to-run variance, and checks it
against tolerances that are written down rather than assumed.
"""

from __future__ import annotations

import json
import math
import platform
import re
import statistics
import subprocess
import time
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Sequence

from .docker_cli import daemon_info
from .environment import EnvironmentSpec, TrialEnvironment
from .incidents import MYSQL_POOL_DELAY_V1, IncidentVariant
from .load import LoadProfile

HEALTHY_PROFILE = LoadProfile(
    name="healthy-baseline",
    concurrency=8,
    duration_seconds=20.0,
    warmup_seconds=5.0,
    seed=1842,
    # Pre-registered, frozen before the holdout run. Warm steady state is a
    # 29ms median with a 44ms maximum; 100ms is ~3.4x the median and well clear
    # of normal jitter. Cold-start cycles breach it heavily (max 536ms), which
    # is the point -- it is what caught cycle 1 not being a repetition.
    absolute_excursion_seconds=0.100,
)

INCIDENT_PROFILE = LoadProfile(
    name="incident",
    # Concurrency is bounded by the app's DEPENDENCY_TIMEOUT (3s), not by a desire
    # for load. Under the incident the pool holds a connection across the read
    # delay, so the service rate is pinned at pool/delay = 8 rps and closed-loop
    # latency grows as concurrency/8 seconds. At concurrency 8 the p99 reached
    # 4.6s and requests began timing out into 500s (measured: 1.7% error rate),
    # which is nondeterministic run to run. Concurrency 4 keeps the pool
    # saturated (any value >= pool size does) so throughput still pins at 8 rps,
    # while holding p50 near 0.5s with roughly 6x headroom under the timeout.
    concurrency=4,
    duration_seconds=80.0,
    # A 22s window at 8 rps yields only ~176 samples, which makes the throughput
    # estimator coarse: a ten-cycle run showed one cycle at 7.27 rps against a
    # 7.91 rps mode, traced to a single 1.85s stall (normal max latency is
    # 0.51s). With pool=2 one slow query halves capacity while it lasts, so a
    # single stall costs ~14 requests -- 8% of a 176-sample window, and a 2.536%
    # coefficient of variation on its own.
    #
    # Sizing the window against that model: 22s/176 samples gives 2.536% for one
    # stall; 50s/400 samples gives 1.111% for one but 1.52% for two, which
    # breaches the 1.5% bound; 80s/640 samples gives 0.693% for one and 0.95%
    # for two. So 80s, and the stall itself is gated separately rather than
    # being left to show up as throughput variance.
    warmup_seconds=10.0,
    seed=1842,
    # Pre-registered, frozen before the holdout run. Warm steady state is a
    # 506ms median with a 513ms maximum, so 1.0s is roughly 2x the median and
    # ~1.95x the observed maximum. The cold-start cycle reached 1.010s and
    # trips it; every warm cycle sits far below.
    absolute_excursion_seconds=1.000,
)

# Analytic expectations, used to distinguish "this metric is structurally
# pinned" from "this metric was empirically stable". A near-zero CV on a pinned
# metric is not evidence the harness is deterministic; it is evidence the
# arithmetic holds. Recording the expectation next to the measurement makes the
# residual gap visible, and a change in that gap is itself a signal.
STRUCTURAL_EXPECTATIONS: dict[str, dict[str, Any]] = {
    "incident.throughputRps": {
        "expected": 8.0,
        "derivation": "pool size 2 / read delay 0.25s = 8.0 rps",
        "note": (
            "Structurally pinned by the injected delay, not by host capacity. The "
            "residual gap to the measured value is per-request overhead; if it moves, "
            "something about the request path changed."
        ),
    },
    "incident.latencyP50Seconds": {
        "expected": 0.5,
        "derivation": "concurrency 4 / 8.0 rps = 0.5s",
        "note": "Closed-loop queueing identity; pinned for the same reason.",
    },
}

# Metrics that can actually detect environment drift, because they measure
# contended system behaviour rather than an injected constant. Gate sensitivity
# is set from these; the structurally pinned metrics must not be used to argue
# that the harness is stable.
DRIFT_SENSITIVE_METRICS: tuple[str, ...] = (
    "healthy.throughputRps",
    "healthy.latencyP50Seconds",
    "healthy.latencyP95Seconds",
)

# Stall rate is gated separately from the CV tolerances, for the same reason
# as the error budget: its expected value is ~0, so a coefficient of
# variation says nothing useful about it.
#
# This exists because widening the incident window from 22s to 80s made
# throughput robust to a rare stall -- which was the goal -- but in doing so
# it also made a *rising* stall rate invisible: the same event that moved a
# 176-sample window by 8% moves a 640-sample window by 2%. Trading a noisy
# true signal for a quiet blind spot is the mistake that the 0.000% pinned CV
# already taught us once, so stalls are measured directly instead.
#
# Observed baseline: 1 stall across 10 cycles of ~176 requests (~0.057%).
# 0.5% per phase is roughly 9x that, so it tolerates the known rate while
# still failing if stalls become common.
MAX_STALL_RATE = 0.005

# Recorded rather than smoothed away. One cycle of the first ten-cycle suite
# contained a single 1.85s request against a 0.51s normal maximum, and the
# mechanism is still unknown. It is reported with every suite so that a reader
# is not left to infer determinism we have not demonstrated.
#
# What the attribution attempt showed: driving 1816 incident requests -- four
# times a normal cycle -- while independently probing /healthz, which does no
# database work, produced no catalog stall at all (max 0.536s against a 0.506s
# median). The probe's own maximum over 4248 samples was 21ms, so there was no
# sub-application pause anywhere near 1.85s during steady operation. That rules
# out continuous background jitter as the cause, but it does not attribute the
# event, because the event did not recur.
#
# Consequence for the experiment: if this happens during a scored trial, an
# agent could legitimately observe and diagnose a latency spike that we did not
# inject. The stall-rate gate bounds how often that can happen without the
# suite failing; it does not prevent it.
# The stall definition, threshold and budget were chosen after seeing the
# first ten-cycle result, which makes them fitted rather than predictive. They
# are frozen here and recorded in every report so that the holdout run is a
# test of them rather than a continuation of the fitting.
GATE_PRE_REGISTRATION = {
    "frozenBefore": "phase2-holdout",
    "stallDefinition": "latency > stall_factor * phase median latency",
    "stallFactor": 3.0,
    "maxStallRate": 0.005,
    "absoluteExcursionSeconds": {"healthy": 0.100, "incident": 1.000},
    "fittedOn": "phase2-10x (10 cycles) and a contaminated phase2-final (8 cycles)",
    "note": (
        "Fitted on earlier runs, then frozen. The holdout run changed nothing "
        "between freeze and execution. Raw latency distributions are kept per "
        "cycle so any later analysis can re-derive a different threshold "
        "instead of inheriting this one."
    ),
}

UNEXPLAINED_STALL = {
    "status": "unexplained",
    "observedMagnitudeSeconds": 1.85,
    "phaseNormalMaxSeconds": 0.51,
    "frequency": "1 occurrence across 10 cycles (~1760 measured requests)",
    "attribution": "not reproduced in a 1816-request diagnostic window",
    "ruledOut": (
        "continuous sub-application pause: a concurrent database-free /healthz "
        "probe peaked at 21ms over 4248 samples spanning 240s"
    ),
    "openRisk": (
        "a scored trial may contain a latency spike we did not inject, which an "
        "agent could diagnose as a real fault"
    ),
}


@dataclass(frozen=True)
class Tolerance:
    """A declared, documented bound on run-to-run variation.

    ``max_cv_percent`` bounds the coefficient of variation (stdev / mean).
    ``structurally_pinned`` records that the metric is held near-constant by the
    injected incident arithmetic rather than by a stable harness, so a tight
    observed CV on it must not be read as evidence of determinism.
    """

    metric: str
    max_cv_percent: float
    rationale: str
    structurally_pinned: bool = False


DECLARED_TOLERANCES: tuple[Tolerance, ...] = (
    Tolerance(
        metric="incident.throughputRps",
        max_cv_percent=1.5,
        structurally_pinned=True,
        rationale=(
            "Pinned by pool size / read delay (2 / 0.25s = 8.0 rps) rather than by host "
            "capacity. A 3-cycle calibration read 0.000% CV, which flattered the harness; "
            "over 10 cycles it was 2.536%, because a single 1.85s stall in one cycle cost "
            "8% of a 176-sample window. The window is now 80s (~640 samples), which bounds "
            "one such stall to 0.693% and two to 0.95%. 1.5% covers both, and is not "
            "evidence of harness stability -- see DRIFT_SENSITIVE_METRICS for that."
        ),
    ),
    Tolerance(
        metric="incident.latencyP50Seconds",
        max_cv_percent=1.0,
        structurally_pinned=True,
        rationale=(
            "Closed-loop queueing identity: concurrency 4 / 8 rps = 0.5s. Measured 0.506s "
            "at 0.170% CV over 10 cycles. Also structurally pinned."
        ),
    ),
    Tolerance(
        metric="incident.latencyP95Seconds",
        max_cv_percent=3.0,
        rationale=(
            "Tail latency is where a stall actually shows, so unlike the two metrics above "
            "it retains real signal. Measured 0.513s at 0.729% CV over 10 cycles; 3.0% "
            "leaves room for the stall to appear without failing the suite."
        ),
    ),
    Tolerance(
        metric="healthy.throughputRps",
        max_cv_percent=5.0,
        rationale=(
            "Drift-sensitive: 271.3 rps is set by contended system behaviour at a 25ms "
            "read delay, not by an injected constant, so this is one of the metrics that "
            "could actually detect environment drift. Measured 1.960% CV over 10 cycles "
            "(261.3-278.4 rps). 5.0% is roughly 2.5x the observed spread."
        ),
    ),
    Tolerance(
        metric="healthy.latencyP50Seconds",
        max_cv_percent=5.0,
        rationale=(
            "Drift-sensitive, and the reciprocal of healthy throughput at fixed "
            "concurrency. Measured 29.1ms at 1.545% CV over 10 cycles."
        ),
    ),
    Tolerance(
        metric="healthy.latencyP95Seconds",
        max_cv_percent=10.0,
        rationale=(
            "Drift-sensitive and the noisiest metric measured: 33.0ms at 5.133% CV over 10 "
            "cycles (31.6-36.1ms). Gated despite the noise precisely because tail latency "
            "under a healthy system is the most likely place for host contention to show "
            "up first. 10.0% is about 2x the observed CV."
        ),
    ),
)

# Timeout-driven 500s are the main source of nondeterminism in the incident
# phase, so they are bounded directly rather than via a coefficient of variation
# (which is undefined for a metric whose mean should be zero).
#
# The measured value is 0.000% in both phases over 10 cycles. The budget is
# deliberately NOT zero: a single transient connection reset would otherwise
# fail an entire trial on noise rather than signal. 0.5% is tight enough to
# catch the concurrency-8 regression class that was actually observed (1.7%),
# and loose enough not to be brittle. Any nonzero reading is worth investigating
# even though it passes.
MAX_ERROR_RATE = 0.005

# The incident must demonstrably degrade performance, otherwise the environment
# is not exercising the scenario at all.
MIN_THROUGHPUT_DEGRADATION_FACTOR = 5.0
MIN_LATENCY_DEGRADATION_FACTOR = 5.0


@dataclass
class CycleResult:
    index: int
    run_id: str
    ok: bool
    signed_off: bool
    cleanup_verified: bool
    incident_verified: bool
    duration_seconds: float
    metrics: dict[str, float] = field(default_factory=dict)
    failed_gates: list[str] = field(default_factory=list)
    missing_gates: list[str] = field(default_factory=list)
    stalls: list[dict[str, Any]] = field(default_factory=list)
    host_suspension_seconds: float = 0.0
    started_wall_clock: str = ""
    suite_elapsed_seconds: float | None = None
    error: str | None = None
    artifacts_dir: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "runId": self.run_id,
            "ok": self.ok,
            "signedOff": self.signed_off,
            "cleanupVerified": self.cleanup_verified,
            "incidentVerified": self.incident_verified,
            "durationSeconds": round(self.duration_seconds, 3),
            "startedWallClock": self.started_wall_clock,
            "suiteElapsedSeconds": (
                None
                if self.suite_elapsed_seconds is None
                else round(self.suite_elapsed_seconds, 3)
            ),
            "hostSuspensionSeconds": round(self.host_suspension_seconds, 3),
            "metrics": self.metrics,
            "failedGates": self.failed_gates,
            "missingGates": self.missing_gates,
            "stalls": self.stalls,
            "error": self.error,
            "artifactsDir": self.artifacts_dir,
        }


# A cycle is only a measurement if the host was awake for all of it. macOS
# advances time.time() across sleep and does not advance time.monotonic(), so
# their divergence over a cycle is the time the host spent suspended.
#
# This is not hypothetical. An earlier holdout attempt had the lid closed on
# battery; the host entered clamshell sleep 90 seconds into cycle 3 and
# alternated sleep and darkwake for the next 109 minutes. That cycle passed
# every gate and reported a throughput figure computed across a wall-clock
# window the machine had mostly slept through. Nothing in the driver noticed.
#
# The bound is generous because it is discriminating between "scheduling
# jitter" and "the machine was off", not measuring anything.
MAX_HOST_SUSPENSION_SECONDS = 5.0


def summarise(values: Sequence[float]) -> dict[str, float]:
    clean = [v for v in values if v is not None and not math.isnan(v)]
    if not clean:
        return {"n": 0}
    mean = statistics.fmean(clean)
    stdev = statistics.stdev(clean) if len(clean) > 1 else 0.0
    if stdev == 0.0:
        # A constant series has no relative variation. Reporting inf for the
        # all-zero case (error rates, typically) would misread as instability.
        cv = 0.0
    else:
        cv = (stdev / mean * 100.0) if mean else math.inf
    return {
        "n": len(clean),
        "mean": mean,
        "stdev": stdev,
        "cvPercent": cv,
        "min": min(clean),
        "max": max(clean),
        "halfRangePercent": ((max(clean) - min(clean)) / 2 / mean * 100.0) if mean else 0.0,
    }


def host_suspension_seconds(
    wall_start: float, wall_end: float, mono_start: float, mono_end: float
) -> float:
    """Seconds the host spent suspended between two paired clock readings.

    ``time.monotonic()`` does not advance while a macOS host is asleep and
    ``time.time()`` does, so the gap between them is time the process was not
    running. Clamped at zero: ordinary clock skew is not suspension.
    """
    return max(0.0, (wall_end - wall_start) - (mono_end - mono_start))


def host_power_state() -> dict[str, Any]:
    """Whether the host is on AC or battery, and any thermal limit in force.

    Not a gate, and deliberately not one: this records a fact about the host
    rather than judging it. The first holdout's last cycles drifted in one
    direction -- incident throughput 7.914 -> 7.886 -> 7.857 -> 7.829, incident
    p50 rising 0.5058 -> 0.5112, and the suite's lowest healthy throughput in
    the final cycle -- on a machine that happened to be on battery. Power and
    thermal state are the first thing to suspect there, and they cannot be
    reconstructed after the run, so they are captured rather than remembered.
    """
    state: dict[str, Any] = {"platform": platform.system(), "source": "unknown"}
    if platform.system() != "Darwin":
        state["note"] = "power state capture is implemented for macOS only"
        return state
    state.update(parse_power_state(_run_text(["pmset", "-g", "batt"]), _run_text(["pmset", "-g", "therm"])))
    return state


def parse_power_state(batt: str | None, therm: str | None) -> dict[str, Any]:
    """Parse ``pmset`` output. Split out so it can be tested off a Mac."""
    state: dict[str, Any] = {}
    if batt is not None:
        state["raw"] = batt.strip()
        match = re.search(r"Now drawing from '([^']+)'", batt)
        if match:
            drawing = match.group(1)
            state["drawingFrom"] = drawing
            state["source"] = "ac" if "AC" in drawing else "battery"
        percent = re.search(r"(\d+)%", batt)
        if percent:
            state["batteryPercent"] = int(percent.group(1))
    if therm is not None:
        limit = re.search(r"CPU_Speed_Limit\s*=\s*(\d+)", therm)
        # Absent on a healthy Apple Silicon host; present and below 100 when
        # the OS is actively throttling, which is the case worth seeing.
        state["cpuSpeedLimitPercent"] = int(limit.group(1)) if limit else None
        state["thermalWarningsRecorded"] = "No thermal warning level" not in therm
    return state


def _run_text(command: list[str]) -> str | None:
    """Best-effort capture of a host probe. Never fails the suite."""
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=15, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return completed.stdout if completed.returncode == 0 else None


def _daemon_versions() -> dict[str, str]:
    """Daemon identity for the report, or a recorded failure.

    ``build_report`` runs after every cycle has finished, so raising here
    would discard a completed suite over a daemon that has since gone away --
    losing the measurements rather than the version string. It also made the
    "Docker-free" unit tests silently daemon-dependent: they passed on a
    machine with Docker running and errored on one without.
    """
    try:
        return daemon_info()
    except Exception as exc:  # noqa: BLE001 - provenance must not fail a suite
        return {"error": str(exc).splitlines()[0]}


def suite_provenance(repo_root: Path, suite_id: str, artifact_dir: Path) -> dict[str, Any]:
    """Which commit produced a suite, and whether the tree was modified.

    A holdout tests gates that were frozen beforehand, so a report that cannot
    name the commit it ran against is not a holdout -- it is a measurement of
    something unknown. ``worktreeClean`` is recorded rather than enforced here;
    the caller decides whether a dirty tree is disqualifying.
    """
    head = _run_text(["git", "-C", str(repo_root), "rev-parse", "HEAD"])
    dirty = _run_text(["git", "-C", str(repo_root), "status", "--porcelain"])
    return {
        "suiteId": suite_id,
        "commit": head.strip() if head else None,
        "worktreeClean": (dirty is not None and not dirty.strip()),
        "worktreeDiff": dirty.strip() if dirty else "",
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "artifactDir": str(artifact_dir),
    }


def run_cycle(
    index: int,
    *,
    repo_root: Path,
    suite_id: str,
    spec: EnvironmentSpec,
    incident: IncidentVariant,
    results_dir: Path,
    pull: bool,
    suite_start_epoch: float | None = None,
) -> CycleResult:
    run_id = f"{suite_id}-c{index:02d}"
    started = time.monotonic()
    started_wall = time.time()
    environment = TrialEnvironment(
        run_id,
        repo_root=repo_root,
        spec=spec,
        incident=incident,
        results_dir=results_dir,
        pull=pull,
        suite_start_epoch=suite_start_epoch,
    )
    error: str | None = None
    metrics: dict[str, float] = {}
    stalls: list[dict[str, Any]] = []
    incident_verified = False

    try:
        environment.create()
        environment.verify_start_state()

        healthy = environment.measure(HEALTHY_PROFILE.name, HEALTHY_PROFILE)
        verification = environment.inject_incident()
        incident_verified = verification.ok
        incident_phase = environment.measure(INCIDENT_PROFILE.name, INCIDENT_PROFILE)

        metrics = {
            "healthy.throughputRps": healthy.load.throughput_rps,
            "healthy.latencyP50Seconds": healthy.load.latency_p50_seconds,
            "healthy.latencyP95Seconds": healthy.load.latency_p95_seconds,
            "healthy.errorRate": healthy.load.error_rate,
            "healthy.promHttpP95Seconds": healthy.telemetry.value("httpP95Seconds") or math.nan,
            "healthy.promMysqlP95Seconds": healthy.telemetry.value("mysqlP95Seconds") or math.nan,
            "incident.throughputRps": incident_phase.load.throughput_rps,
            "incident.latencyP50Seconds": incident_phase.load.latency_p50_seconds,
            "incident.latencyP95Seconds": incident_phase.load.latency_p95_seconds,
            "incident.errorRate": incident_phase.load.error_rate,
            "healthy.stallRate": healthy.load.stall_rate,
            "incident.stallRate": incident_phase.load.stall_rate,
            "healthy.stallCount": float(healthy.load.stall_count),
            "incident.stallCount": float(incident_phase.load.stall_count),
            "healthy.latencyMaxSeconds": healthy.load.latency_max_seconds,
            "healthy.excursionRate": healthy.load.excursion_rate,
            "incident.excursionRate": incident_phase.load.excursion_rate,
            "healthy.excursionCount": float(healthy.load.excursion_count),
            "incident.excursionCount": float(incident_phase.load.excursion_count),
            "incident.latencyMaxSeconds": incident_phase.load.latency_max_seconds,
            "incident.promHttpP95Seconds": incident_phase.telemetry.value("httpP95Seconds")
            or math.nan,
            "incident.promMysqlP95Seconds": incident_phase.telemetry.value("mysqlP95Seconds")
            or math.nan,
        }

        # Timestamped per stall rather than reduced to a count, so a
        # recurrence can be tested against a periodic host or database
        # event instead of being left as an anecdote.
        stalls = [
            {**event.to_dict(), "phase": phase, "runId": run_id}
            for phase, measurement in (
                ("healthy", healthy),
                ("incident", incident_phase),
            )
            for event in measurement.load.stall_events
        ]
    except Exception:
        error = traceback.format_exc()
    finally:
        try:
            environment.destroy()
        except Exception:
            error = (error or "") + "\n" + traceback.format_exc()
        artifacts = environment.write_artifacts()

    manifest = environment.manifest
    duration = time.monotonic() - started
    suspended = host_suspension_seconds(
        started_wall, time.time(), started, time.monotonic()
    )
    if suspended > MAX_HOST_SUSPENSION_SECONDS:
        error = (error or "") + (
            f"\nhost suspended for {suspended:.1f}s during this cycle "
            f"(bound {MAX_HOST_SUSPENSION_SECONDS}s); the measurement window "
            f"does not describe a running system"
        )
    return CycleResult(
        index=index,
        run_id=run_id,
        ok=error is None and manifest.signed_off,
        signed_off=manifest.signed_off,
        cleanup_verified=manifest.cleanup_verified,
        incident_verified=incident_verified,
        duration_seconds=duration,
        started_wall_clock=datetime.fromtimestamp(started_wall, tz=timezone.utc).isoformat(),
        suite_elapsed_seconds=(
            None if suite_start_epoch is None else started_wall - suite_start_epoch
        ),
        host_suspension_seconds=suspended,
        metrics=metrics,
        stalls=stalls,
        failed_gates=[gate.name for gate in manifest.failed_gates],
        missing_gates=manifest.missing_gates,
        error=error,
        artifacts_dir=str(artifacts),
    )


def run_suite(
    *,
    repo_root: Path,
    cycles: int = 10,
    suite_id: str | None = None,
    spec: EnvironmentSpec | None = None,
    incident: IncidentVariant = MYSQL_POOL_DELAY_V1,
    results_dir: Path | None = None,
    pull: bool = True,
    warmup_cycles: int = 1,
) -> dict[str, Any]:
    """Run N identical measured cycles and report variance against tolerances.

    Cycles are only comparable if they are actually repetitions of each other.
    An earlier version pulled images on cycle 1 only, which made that cycle a
    cold start rather than a repeat: it measured 226 rps against the 274 rps
    of its nine siblings, with 58 stalls where they had none, and its incident
    phase peaked at 1.010s against their 0.513s. Averaging that with the rest
    described a population that does not exist.

    So the run is now in three explicit parts: a setup phase that pulls and
    builds every image and is never measured, one or more warm-up cycles whose
    results are discarded, and only then the measured cycles -- all identical,
    all with pulling disabled.
    """
    repo_root = Path(repo_root).resolve()
    spec = spec or EnvironmentSpec()
    results_dir = Path(results_dir) if results_dir else repo_root / "benchmark" / "results"
    suite_id = suite_id or f"suite-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"
    suite_dir = results_dir / suite_id
    suite_dir.mkdir(parents=True, exist_ok=True)
    # Anchors every stall timestamp, so an anomaly can be placed against the
    # other cycles in this suite and against suites that ran at other times.
    suite_start_epoch = time.time()
    started_mono = time.monotonic()
    provenance = suite_provenance(repo_root, suite_id, suite_dir)
    power_before = host_power_state()
    _write_json(suite_dir / "provenance.json", provenance)

    # Setup phase: acquire every image once, measured by nobody. Done through a
    # throwaway cycle so the build path exercised here is the same one the
    # measured cycles use, rather than a separate code path that could drift.
    setup_cycles: list[dict[str, Any]] = []
    if pull:
        print(f"[setup] pulling and building images (not measured) ...", flush=True)
        setup = run_cycle(
            0,
            repo_root=repo_root,
            suite_id=f"{suite_id}-setup",
            spec=spec,
            incident=incident,
            results_dir=results_dir,
            pull=True,
            suite_start_epoch=suite_start_epoch,
        )
        setup_cycles.append(
            {"runId": setup.run_id, "ok": setup.ok, "role": "image-acquisition"}
        )
        if not setup.ok:
            raise RuntimeError(
                f"setup cycle failed, refusing to measure: "
                f"{setup.failed_gates or setup.error}"
            )

    # Warm-up cycles: identical to the measured ones, results discarded. These
    # absorb page-cache and layer-cache effects that the setup phase does not.
    warmups: list[dict[str, Any]] = []
    for index in range(1, warmup_cycles + 1):
        print(f"[warmup {index}/{warmup_cycles}] discarded cycle ...", flush=True)
        warm = run_cycle(
            index,
            repo_root=repo_root,
            suite_id=f"{suite_id}-warmup",
            spec=spec,
            incident=incident,
            results_dir=results_dir,
            pull=False,
            suite_start_epoch=suite_start_epoch,
        )
        warmups.append(
            {
                "runId": warm.run_id,
                "ok": warm.ok,
                "role": "discarded-warmup",
                "metrics": warm.metrics,
            }
        )

    cycle_results: list[CycleResult] = []
    for index in range(1, cycles + 1):
        print(f"[{index}/{cycles}] running cycle {suite_id}-c{index:02d} ...", flush=True)
        result = run_cycle(
            index,
            repo_root=repo_root,
            suite_id=suite_id,
            spec=spec,
            incident=incident,
            results_dir=results_dir,
            # Never conditional on the index: every measured cycle must be the
            # same experiment as every other measured cycle.
            pull=False,
            suite_start_epoch=suite_start_epoch,
        )
        cycle_results.append(result)
        status = "ok" if result.ok else f"FAILED ({result.failed_gates or result.error})"
        print(
            f"    -> {status}; incident {result.metrics.get('incident.throughputRps', float('nan')):.3f} rps "
            f"p50 {result.metrics.get('incident.latencyP50Seconds', float('nan')):.3f}s "
            f"in {result.duration_seconds:.1f}s",
            flush=True,
        )

    report = build_report(
        cycle_results,
        suite_id=suite_id,
        cycles=cycles,
        setup_cycles=setup_cycles,
        warmups=warmups,
        suite_start_epoch=suite_start_epoch,
    )
    report["provenance"] = provenance
    report["wallClockSeconds"] = round(time.time() - suite_start_epoch, 1)
    # Per-cycle suspension cannot see a host that slept *between* cycles, in
    # the teardown-to-setup gap. Measuring across the whole suite closes that
    # window. Reported, not gated: the per-cycle bound is the gate.
    report["hostSuspension"]["suiteLevelSeconds"] = round(
        host_suspension_seconds(
            suite_start_epoch, time.time(), started_mono, time.monotonic()
        ),
        3,
    )
    annotate_power(report, power_before, host_power_state())
    # The README documents this path, and for a while the code did not
    # produce it: run_suite returned the report and only the CLI printed it,
    # mixed into progress output. The holdout's report survived because an
    # external harness wrote it, which is not a property to depend on.
    _write_json(suite_dir / "determinism-report.json", report)
    return report


def annotate_power(
    report: dict[str, Any], before: dict[str, Any], after: dict[str, Any]
) -> dict[str, Any]:
    """Attach the power readings taken at both ends of a suite.

    Captured twice rather than once so a change during the run is visible
    rather than inferred. A suite that starts on AC and ends on battery is not
    the same experiment throughout, and the report should say so.
    """
    report["hostPower"] = {
        "atStart": before,
        "atEnd": after,
        "changedDuringSuite": before.get("source") != after.get("source"),
        "note": (
            "Recorded, never gated. A monotonic drift across the last cycles of "
            "a suite is what this is for: power and thermal state are the first "
            "thing to suspect and cannot be reconstructed afterwards."
        ),
    }
    return report


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=str) + "\n",
        encoding="utf-8",
    )


def build_report(
    cycle_results: list[CycleResult],
    *,
    suite_id: str,
    cycles: int,
    setup_cycles: list[dict[str, Any]] | None = None,
    warmups: list[dict[str, Any]] | None = None,
    suite_start_epoch: float | None = None,
) -> dict[str, Any]:
    """Assemble the determinism report from finished cycles.

    Split out of run_suite so it can be exercised without a Docker daemon.
    It previously could not be, and that is exactly how a crash reached the
    report path: the stall block read a field name that does not exist on
    CycleResult, so any suite containing a stall raised AttributeError
    before writing anything. The unit tests all passed, because none of
    them ran this function with a nonzero stall count.
    """
    setup_cycles = setup_cycles or []
    warmups = warmups or []

    successful = [c for c in cycle_results if c.ok]
    metric_names = sorted({name for c in successful for name in c.metrics})
    variance = {
        name: summarise([c.metrics[name] for c in successful if name in c.metrics])
        for name in metric_names
    }

    tolerance_report = []
    for tolerance in DECLARED_TOLERANCES:
        stats = variance.get(tolerance.metric, {"n": 0})
        observed_cv = stats.get("cvPercent", math.inf)
        tolerance_report.append(
            {
                "metric": tolerance.metric,
                "maxCvPercent": tolerance.max_cv_percent,
                "observedCvPercent": observed_cv,
                "structurallyPinned": tolerance.structurally_pinned,
                "withinTolerance": bool(stats.get("n", 0) > 1 and observed_cv <= tolerance.max_cv_percent),
                "rationale": tolerance.rationale,
            }
        )

    # Analytic expectation beside the measurement, so a pinned metric cannot be
    # mistaken for evidence that the harness itself is stable.
    structural_report = {}
    for metric, expectation in STRUCTURAL_EXPECTATIONS.items():
        stats = variance.get(metric, {})
        measured = stats.get("mean", math.nan)
        expected = expectation["expected"]
        structural_report[metric] = {
            "expected": expected,
            "measured": measured,
            "gapPercent": (
                abs(measured - expected) / expected * 100.0
                if expected and not math.isnan(measured)
                else math.nan
            ),
            "derivation": expectation["derivation"],
            "note": expectation["note"],
        }

    # The determinism claim rests on these, not on the pinned metrics.
    drift_report = {
        "metrics": list(DRIFT_SENSITIVE_METRICS),
        "observedCvPercent": {
            name: variance.get(name, {}).get("cvPercent", math.nan)
            for name in DRIFT_SENSITIVE_METRICS
        },
        "worstCvPercent": max(
            (
                variance.get(name, {}).get("cvPercent", 0.0)
                for name in DRIFT_SENSITIVE_METRICS
            ),
            default=math.nan,
        ),
        "note": (
            "These measure contended system behaviour rather than an injected constant, "
            "so they are the metrics capable of detecting environment drift. Gate "
            "sensitivity is set from these."
        ),
    }

    healthy_throughput = variance.get("healthy.throughputRps", {}).get("mean", math.nan)
    incident_throughput = variance.get("incident.throughputRps", {}).get("mean", math.nan)
    healthy_p95 = variance.get("healthy.latencyP95Seconds", {}).get("mean", math.nan)
    incident_p95 = variance.get("incident.latencyP95Seconds", {}).get("mean", math.nan)

    throughput_factor = (
        healthy_throughput / incident_throughput
        if incident_throughput and not math.isnan(incident_throughput)
        else math.nan
    )
    latency_factor = (
        incident_p95 / healthy_p95 if healthy_p95 and not math.isnan(healthy_p95) else math.nan
    )

    degradation = {
        "throughputFactor": throughput_factor,
        "latencyFactor": latency_factor,
        "minThroughputFactor": MIN_THROUGHPUT_DEGRADATION_FACTOR,
        "minLatencyFactor": MIN_LATENCY_DEGRADATION_FACTOR,
        "incidentDegradedPerformance": bool(
            throughput_factor >= MIN_THROUGHPUT_DEGRADATION_FACTOR
            and latency_factor >= MIN_LATENCY_DEGRADATION_FACTOR
        ),
    }

    # Per-cycle stall detail is kept rather than averaged, so that a rare event
    # stays visible as a discrete occurrence with a magnitude and a frequency.
    stall_observations = [
        {
            "runId": c.run_id,
            "phase": phase,
            "count": int(c.metrics.get(f"{phase}.stallCount", 0) or 0),
            "maxLatencySeconds": c.metrics.get(f"{phase}.latencyMaxSeconds"),
            # Every stall in this phase, timestamped. n=2 across two suites was
            # not a pattern worth acting on, but it was cheap to make testable:
            # if a later suite puts an anomaly at a similar suiteElapsedSeconds,
            # look at periodic host or database work -- a Docker Desktop VM
            # task, a macOS background job, a MySQL purge or checkpoint -- before
            # anything else. If it does not recur, the pattern is not real.
            "events": [e for e in c.stalls if e.get("phase") == phase],
        }
        for c in successful
        for phase in ("healthy", "incident")
        if int(c.metrics.get(f"{phase}.stallCount", 0) or 0) > 0
    ]

    stall_rates = {
        name: variance.get(name, {}).get("max", math.nan)
        for name in ("healthy.stallRate", "incident.stallRate")
    }
    stall_budget = {
        "maxStallRate": MAX_STALL_RATE,
        "observedMaxRate": stall_rates,
        "totalStalls": {
            name: sum(
                int(c.metrics.get(name, 0) or 0)
                for c in successful
            )
            for name in ("healthy.stallCount", "incident.stallCount")
        },
        "observations": stall_observations,
        "withinBudget": bool(
            all(
                not math.isnan(value) and value <= MAX_STALL_RATE
                for value in stall_rates.values()
            )
        ),
        "note": (
            "A stall is a request exceeding 3x its phase's median latency. Gated "
            "directly because the widened measurement window deliberately dilutes "
            "individual stalls out of the throughput estimate."
        ),
        "absoluteExcursions": {
            "thresholdsSeconds": {
                "healthy": HEALTHY_PROFILE.absolute_excursion_seconds,
                "incident": INCIDENT_PROFILE.absolute_excursion_seconds,
            },
            "totals": {
                name: sum(int(c.metrics.get(name, 0) or 0) for c in successful)
                for name in ("healthy.excursionCount", "incident.excursionCount")
            },
            "note": (
                "Absolute counts are reported next to the phase-relative stall "
                "rate because the relative threshold moves with the median: a "
                "uniformly slower environment raises its own bar and can report "
                "zero stalls while being plainly worse."
            ),
        },
        "preRegistration": GATE_PRE_REGISTRATION,
        "unexplainedObservation": UNEXPLAINED_STALL,
        "timingNote": (
            "Each stall carries wallClock and suiteElapsedSeconds so a "
            "recurrence can be checked against periodic host or database work "
            "rather than inferred. Recording them changes no gate."
        ),
    }

    error_rates = {
        name: variance.get(name, {}).get("max", math.nan)
        for name in ("healthy.errorRate", "incident.errorRate")
    }
    error_budget = {
        "maxErrorRate": MAX_ERROR_RATE,
        "observedMax": error_rates,
        "withinBudget": bool(
            all(
                not math.isnan(value) and value <= MAX_ERROR_RATE
                for value in error_rates.values()
            )
        ),
    }

    # A suite is only a measurement if the host was awake throughout. Reported
    # unconditionally, so a clean run states the fact rather than leaving a
    # reader to assume it.
    suspension_audit = {
        "maxSuspensionSeconds": MAX_HOST_SUSPENSION_SECONDS,
        "observedMaxSeconds": max(
            (c.host_suspension_seconds for c in cycle_results), default=0.0
        ),
        "suspendedCycles": [
            {
                "runId": c.run_id,
                "seconds": round(c.host_suspension_seconds, 3),
                "startedWallClock": c.started_wall_clock,
            }
            for c in cycle_results
            if c.host_suspension_seconds > MAX_HOST_SUSPENSION_SECONDS
        ],
        "note": (
            "time.time() advances across macOS sleep and time.monotonic() does "
            "not, so their divergence over a cycle is time the host spent "
            "suspended. An earlier holdout attempt slept 109 minutes mid-suite "
            "and every gate still passed."
        ),
    }
    suspension_audit["hostAwakeThroughout"] = not suspension_audit["suspendedCycles"]

    exit_criterion_met = bool(
        len(successful) == cycles
        and all(c.cleanup_verified for c in cycle_results)
        and all(c.incident_verified for c in successful)
        and all(entry["withinTolerance"] for entry in tolerance_report)
        and degradation["incidentDegradedPerformance"]
        and error_budget["withinBudget"]
        and stall_budget["withinBudget"]
        and suspension_audit["hostAwakeThroughout"]
    )

    report = {
        "suiteId": suite_id,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "cycles": cycles,
        "successfulCycles": len(successful),
        "exitCriterionMet": exit_criterion_met,
        "dockerVersions": _daemon_versions(),
        "suiteStartedAt": (
            None
            if suite_start_epoch is None
            else datetime.fromtimestamp(suite_start_epoch, tz=timezone.utc).isoformat()
        ),
        "hostSuspension": suspension_audit,
        "degradation": degradation,
        "errorBudget": error_budget,
        "stallBudget": stall_budget,
        "unmeasured": {
            "setupCycles": setup_cycles,
            "warmupCycles": warmups,
            "note": (
                "Image acquisition and warm-up are run as separate unmeasured "
                "cycles so that every measured cycle is a repetition of every "
                "other one. Pulling on cycle 1 only made it a cold start: 226 rps "
                "and 58 stalls against 274 rps and zero for its siblings."
            ),
        },
        "structuralExpectations": structural_report,
        "driftSensitivity": drift_report,
        "tolerances": tolerance_report,
        "variance": variance,
        "results": [c.to_dict() for c in cycle_results],
    }

    return report


