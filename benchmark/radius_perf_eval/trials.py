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
import statistics
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
    # A 22s window at 8 rps yields only ~176 samples, which makes the throughput
    # estimator coarse: a ten-cycle run showed one cycle at 7.27 rps against a
    # 7.91 rps mode, traced to a single 1.85s stall (normal max latency is
    # 0.51s). With pool=2 one slow query halves capacity while it lasts, so that
    # single event cost 14 requests, i.e. 8% of the window, and a modelled CV of
    # 2.536% -- matching the 2.536% actually measured.
    #
    # Widening the window is the honest fix: it reduces the estimator's exposure
    # to a rare stall without hiding the stall, which still shows in p95/p99.
    # At an 80s window (~640 samples) the same stall costs 2.2%, giving a
    # modelled CV of 0.693% for one stall and 0.95% for two -- both inside the
    # declared 1.5%. A 50s window was rejected because two stalls in ten cycles
    # would model at 1.52% and breach the bound.
    duration_seconds=90.0,
    warmup_seconds=10.0,
    seed=1842,
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
            "8% of a 174-sample window. The window is now 50s (~400 samples), which bounds "
            "one such stall to under 1%. 1.5% covers that plus a second stall, and is not "
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
            "metrics": self.metrics,
            "failedGates": self.failed_gates,
            "error": self.error,
            "artifactsDir": self.artifacts_dir,
        }


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


def run_cycle(
    index: int,
    *,
    repo_root: Path,
    suite_id: str,
    spec: EnvironmentSpec,
    incident: IncidentVariant,
    results_dir: Path,
    pull: bool,
) -> CycleResult:
    run_id = f"{suite_id}-c{index:02d}"
    started = time.monotonic()
    environment = TrialEnvironment(
        run_id,
        repo_root=repo_root,
        spec=spec,
        incident=incident,
        results_dir=results_dir,
        pull=pull,
    )
    error: str | None = None
    metrics: dict[str, float] = {}
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
            "incident.latencyMaxSeconds": incident_phase.load.latency_max_seconds,
            "incident.promHttpP95Seconds": incident_phase.telemetry.value("httpP95Seconds")
            or math.nan,
            "incident.promMysqlP95Seconds": incident_phase.telemetry.value("mysqlP95Seconds")
            or math.nan,
        }
    except Exception:
        error = traceback.format_exc()
    finally:
        try:
            environment.destroy()
        except Exception:
            error = (error or "") + "\n" + traceback.format_exc()
        artifacts = environment.write_artifacts()

    manifest = environment.manifest
    return CycleResult(
        index=index,
        run_id=run_id,
        ok=error is None and manifest.signed_off,
        signed_off=manifest.signed_off,
        cleanup_verified=manifest.cleanup_verified,
        incident_verified=incident_verified,
        duration_seconds=time.monotonic() - started,
        metrics=metrics,
        failed_gates=[gate.name for gate in manifest.failed_gates],
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
) -> dict[str, Any]:
    """Run N cycles and report variance against the declared tolerances."""
    repo_root = Path(repo_root).resolve()
    spec = spec or EnvironmentSpec()
    results_dir = Path(results_dir) if results_dir else repo_root / "benchmark" / "results"
    suite_id = suite_id or f"suite-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"

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
            pull=pull and index == 1,
        )
        cycle_results.append(result)
        status = "ok" if result.ok else f"FAILED ({result.failed_gates or result.error})"
        print(
            f"    -> {status}; incident {result.metrics.get('incident.throughputRps', float('nan')):.3f} rps "
            f"p50 {result.metrics.get('incident.latencyP50Seconds', float('nan')):.3f}s "
            f"in {result.duration_seconds:.1f}s",
            flush=True,
        )

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
            "cycleId": c.cycle_id,
            "phase": phase,
            "count": int(c.metrics.get(f"{phase}.stallCount", 0) or 0),
            "maxLatencySeconds": c.metrics.get(f"{phase}.latencyMaxSeconds"),
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
        "unexplainedObservation": UNEXPLAINED_STALL,
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

    exit_criterion_met = bool(
        len(successful) == cycles
        and all(c.cleanup_verified for c in cycle_results)
        and all(c.incident_verified for c in successful)
        and all(entry["withinTolerance"] for entry in tolerance_report)
        and degradation["incidentDegradedPerformance"]
        and error_budget["withinBudget"]
        and stall_budget["withinBudget"]
    )

    report = {
        "suiteId": suite_id,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "cycles": cycles,
        "successfulCycles": len(successful),
        "exitCriterionMet": exit_criterion_met,
        "dockerVersions": daemon_info(),
        "degradation": degradation,
        "errorBudget": error_budget,
        "stallBudget": stall_budget,
        "structuralExpectations": structural_report,
        "driftSensitivity": drift_report,
        "tolerances": tolerance_report,
        "variance": variance,
        "results": [c.to_dict() for c in cycle_results],
    }

    suite_dir = results_dir / suite_id
    suite_dir.mkdir(parents=True, exist_ok=True)
    (suite_dir / "determinism-report.json").write_text(
        json.dumps(report, indent=2, sort_keys=True, default=str) + "\n", encoding="utf-8"
    )
    return report
