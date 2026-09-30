"""Refit ``cpu-limits.json`` from a recorded demand measurement.

The fitted numbers must be reproducible from the measurement that produced
them, or the file is a set of magic constants. This applies the frozen rule
in ``cpu_limits`` to recorded per-service peaks and rewrites the file,
including the hash that goes into provenance.

It deliberately does not measure anything itself. Measurement is a separate,
slow, Docker-dependent step whose output is kept outside the repository;
this tool is the pure function from those numbers to the committed file, so
it can be rerun and diffed without a Docker daemon.

    python3 tools/refit_cpu_limits.py --demand-report <path-to-demand-report>

Omit ``--demand-report`` to refit from the peaks already recorded in the
file, which is what you want when only the rule changed.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "benchmark"))

from radius_perf_eval import cpu_limits  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--demand-report", type=Path, default=None)
    parser.add_argument("--note", default=None)
    options = parser.parse_args()

    path = cpu_limits.cpu_limits_path(REPO)
    payload = json.loads(path.read_text())

    if options.demand_report is not None:
        report = json.loads(options.demand_report.read_text())
        peaks = {
            name: round(entry["peakCores"], 4)
            for name, entry in report["services"].items()
        }
        payload["peakCores"] = dict(sorted(peaks.items()))
        payload["meanCores"] = dict(sorted(
            (name, round(entry["meanCores"], 4))
            for name, entry in report["services"].items()
        ))
        # The class must come from the measurement, never from the machine
        # running this tool and never from a flag. Carrying the previous
        # fittedFrom forward was the actual defect: limits refitted from a
        # report measured anywhere kept whatever class the file already
        # claimed, and the loader's class check then agreed with the label
        # rather than with the host the numbers describe.
        host_class = report.get("hostClass")
        if not host_class:
            raise SystemExit(
                f"{options.demand_report} records no hostClass. Refusing to "
                "guess it: observing this machine would label the fit with "
                "whichever host happens to run the tool, and copying the "
                "previous label would let limits from one host inherit "
                "another's name. Re-measure with a tool that records the "
                "class it measured."
            )
        payload["fittedFrom"] = {
            "run": options.demand_report.parent.name,
            "basis": report.get("basis", "cgroup usage_usec deltas"),
            "intervalSeconds": report.get("intervalSeconds"),
            "intervalsPerService": report.get("minIntervals"),
            "gapIntervals": report.get("totalGapIntervals"),
            "windowSeconds": report.get("windowSeconds"),
            "cpuLimitsInPlaceDuringFitting": bool(
                report.get("cpuLimitedServices")
            ),
            "load": report.get("load", "upstream default load generator"),
            "hostClass": host_class,
        }

    before = dict(payload.get("limitCores", {}))
    payload["rule"] = {
        "multiplier": cpu_limits.MULTIPLIER,
        "floorCores": cpu_limits.FLOOR_CORES,
        "rounding": "ceil to hundredths",
    }
    payload["limitCores"] = {
        name: cpu_limits.fit_limit(peak)
        for name, peak in sorted(payload["peakCores"].items())
    }
    if options.note:
        payload["_comment"] = options.note
    payload["manifestHash"] = cpu_limits.fitted_limits_hash(payload)

    path.write_text(json.dumps(payload, indent=2) + "\n")

    changed = {
        name: (before.get(name), limit)
        for name, limit in payload["limitCores"].items()
        if before.get(name) != limit
    }
    for name, (old, new) in sorted(changed.items()):
        print(f"  {name}: {old} -> {new}")
    distinct = sorted(set(payload["limitCores"].values()))
    print(f"changed {len(changed)} of {len(payload['limitCores'])} services")
    print(f"distinct limits: {distinct}")
    print(f"sum: {round(sum(payload['limitCores'].values()), 2)} cores")
    print(f"manifestHash {payload['manifestHash']}")

    # Reading it back exercises every guard in load_fitted_limits, so the
    # tool cannot leave behind a file the driver would reject at run time.
    cpu_limits.load_fitted_limits(REPO, payload["fittedFrom"]["hostClass"])
    print("load_fitted_limits: accepted")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
