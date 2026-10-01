#!/usr/bin/env python3
"""Measure the Astronomy Shop's footprint under its own load generator.

Run from the repository root:

    python3 benchmark/tools/measure_astronomy_footprint.py --minutes 10

Brings the stack up with the declared transforms applied and every image
pinned by digest, lets the shop's load generator drive it at a fixed recorded
configuration, and samples `docker stats` throughout. Reports per-service CPU,
memory and block I/O with peaks, and the container runtime's memory ceiling
beside them, because a peak means nothing without the limit it is approaching.

`docker stats` is read from the host, not from a collector holding the Docker
socket. The socket is not mounted into any container in this stack; see
`astronomy_shop.TRANSFORMS`.

Writes its artifacts outside the repository by default, so an interrupted run
still leaves evidence behind.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radius_perf_eval import astronomy_shop as shop  # noqa: E402
from radius_perf_eval import cpu_limits, offered_load, shop_telemetry  # noqa: E402
from radius_perf_eval.compose import ComposeProject  # noqa: E402
from radius_perf_eval.shop_environment import render_stack as render_shop_stack  # noqa: E402
from radius_perf_eval.hostclass import (  # noqa: E402
    derive_class_id,
    derive_fingerprint,
    gibibytes,
    observe_host,
)

DEFAULT_ARTIFACTS = Path(__file__).resolve().parents[2].parent / "radius-perf-eval-artifacts"


def _to_bytes(text: str) -> float:
    text = text.strip()
    units = {"B": 1, "KB": 1e3, "MB": 1e6, "GB": 1e9, "TB": 1e12,
             "KIB": 1024, "MIB": 1024 ** 2, "GIB": 1024 ** 3, "TIB": 1024 ** 4}
    for suffix in sorted(units, key=len, reverse=True):
        if text.upper().endswith(suffix):
            try:
                return float(text[: -len(suffix)]) * units[suffix]
            except ValueError:
                return 0.0
    try:
        return float(text)
    except ValueError:
        return 0.0


def render_stack(repo_root: Path, out_path: Path, facts=None) -> dict:
    """Merge upstream, apply the transforms, pin digests, write the file."""
    runtime = out_path.parent / "runtime"
    runtime.mkdir()
    return render_shop_stack(repo_root, out_path, facts or observe_host(), runtime)


def sample_stats(project: str) -> dict[str, dict[str, float]]:
    """Sample only this project's containers, selected by Compose label.

    Selecting by name prefix would be wrong twice over. Upstream sets an
    explicit container_name on every service, so before the
    `scope-container-names` transform the names carry no project prefix at
    all and the filter silently matches nothing. And a prefix match can
    capture an unrelated container whose name happens to start the same way.
    The Compose project label is set by Compose itself and means exactly
    "belongs to this project".
    """
    listed = subprocess.run(
        ["docker", "ps", "--filter", f"label=com.docker.compose.project={project}",
         "--format", "{{.ID}}\t{{.Label \"com.docker.compose.service\"}}"],
        capture_output=True, text=True,
    )
    if listed.returncode != 0 or not listed.stdout.strip():
        return {}
    by_id = {}
    for line in listed.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) == 2:
            by_id[parts[0]] = parts[1]
    if not by_id:
        return {}

    result = subprocess.run(
        ["docker", "stats", "--no-stream", "--format",
         "{{.ID}}\t{{.CPUPerc}}\t{{.MemUsage}}\t{{.BlockIO}}", *by_id],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        return {}
    samples: dict[str, dict[str, float]] = {}
    for line in result.stdout.strip().splitlines():
        parts = line.split("\t")
        if len(parts) != 4:
            continue
        container_id, cpu, mem, blockio = parts
        service = by_id.get(container_id)
        if service is None:
            continue
        used = mem.split("/")[0]
        read = blockio.split("/")[0] if "/" in blockio else "0B"
        samples[service] = {
            "cpuPercent": _to_bytes(cpu.rstrip("%")),
            "memoryBytes": _to_bytes(used),
            "blockReadBytes": _to_bytes(read),
        }
    return samples


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--minutes", type=float, default=10.0)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--demand-seconds", type=float, default=60.0)
    parser.add_argument("--project", default="astro-footprint")
    parser.add_argument("--artifacts", type=Path, default=DEFAULT_ARTIFACTS)
    options = parser.parse_args()
    if options.minutes <= 0 or options.interval <= 0 or options.demand_seconds < 30:
        parser.error("minutes and interval must be positive; demand-seconds must be at least 30")

    repo_root = Path(__file__).resolve().parents[2]
    run_dir = options.artifacts / f"astro-footprint-{datetime.now():%Y%m%dT%H%M%S}"
    run_dir.mkdir(parents=True, exist_ok=False)
    stack_path = run_dir / "trial-stack.json"

    facts = observe_host()
    rendered = render_stack(repo_root, stack_path, facts)
    ceiling = facts.docker_memory_bytes or 0

    provenance = {
        "startedAt": datetime.now(timezone.utc).isoformat(),
        "hostClass": derive_class_id(facts),
        "hostFingerprint": derive_fingerprint(facts),
        "host": facts.to_dict(),
        "dockerMemoryCeilingBytes": ceiling,
        "upstream": {"tag": shop.UPSTREAM_TAG, "commit": shop.UPSTREAM_COMMIT},
        "loadConfiguration": {
            "generator": "upstream load-generator service, default configuration",
            "note": "Recorded verbatim from the vendored .env below.",
        },
        **rendered,
    }
    env_text = (shop.upstream_dir(repo_root) / ".env").read_text()
    provenance["loadConfiguration"]["env"] = {
        line.split("=", 1)[0]: line.split("=", 1)[1]
        for line in env_text.splitlines()
        if line and not line.startswith("#") and "=" in line
        and line.split("=", 1)[0].startswith("LOCUST")
    }
    (run_dir / "provenance.json").write_text(json.dumps(provenance, indent=2) + "\n")

    compose = ["docker", "compose", "-f", str(stack_path), "-p", options.project]
    project = ComposeProject(options.project, {}, [stack_path])
    project.assert_absent()
    print(f"bringing up {len(rendered['services'])} services as {options.project}", flush=True)
    def evidence(name, value):
        with (run_dir / "startup.jsonl").open("a") as stream:
            stream.write(json.dumps({"at": time.time(), "name": name, "value": value}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    try:
        up = shop_telemetry.start_stack(project, evidence)
        if up.returncode != 0:
            raise RuntimeError(f"Compose startup failed: {up.stderr}")
        peaks: dict[str, dict[str, float]] = {}
        series: list[dict] = []
        deadline = time.monotonic() + options.minutes * 60
        while time.monotonic() < deadline:
            taken = sample_stats(options.project)
            with (run_dir / "series.jsonl").open("a") as stream:
                stream.write(json.dumps({"t": time.monotonic(), "samples": taken}) + "\n")
                stream.flush()
                os.fsync(stream.fileno())
            if taken:
                series.append({"t": round(time.monotonic(), 1), "samples": taken})
                for name, values in taken.items():
                    slot = peaks.setdefault(name, dict(values))
                    for key, value in values.items():
                        slot[key] = max(slot[key], value)
            time.sleep(options.interval)

        # Positive control on the sampler itself. A measurement that silently
        # collected nothing, or collected only some services, must not be
        # reported as a footprint. The first attempt at this run sampled zero
        # containers because the name filter could not match, and the only
        # signal was an empty result that still looked like a successful run.
        expected = set(rendered["services"])
        observed = set(peaks)
        sampling = {
            "expectedServices": len(expected),
            "sampledServices": len(observed),
            "neverSampled": sorted(expected - observed),
            "unexpected": sorted(observed - expected),
            "samples": len(series),
        }
        sampling["trustworthy"] = (
            len(series) > 0 and not sampling["neverSampled"] and not sampling["unexpected"]
        )

        states = subprocess.run(
            compose + ["ps", "-a", "--format",
                       "{{.Service}}\t{{.State}}\t{{.ExitCode}}\t{{.Status}}"],
            capture_output=True, text=True,
        ).stdout
        oom_killed = []
        for line in states.strip().splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[2].strip() == "137":
                oom_killed.append(parts[0])
        for service in sorted(expected):
            inspected = subprocess.run(
                ["docker", "inspect", "--format", "{{.State.OOMKilled}}",
                 f"{options.project}-{service}-1"],
                capture_output=True, text=True,
            )
            if inspected.stdout.strip() == "true" and service not in oom_killed:
                oom_killed.append(service)

        total_peak = sum(v["memoryBytes"] for v in peaks.values())
        result = {
            "sampling": sampling,
            "oomKilled": sorted(oom_killed),
            "dockerMemoryCeilingBytes": ceiling,
            "dockerMemoryCeilingGib": gibibytes(ceiling),
            "peakTotalMemoryBytes": total_peak,
            "peakTotalMemoryGib": round(total_peak / 1024 ** 3, 3),
            "percentOfCeiling": round(100 * total_peak / ceiling, 1) if ceiling else None,
            "perService": {
                name: {
                    "peakCpuPercent": round(values["cpuPercent"], 1),
                    "peakMemoryBytes": int(values["memoryBytes"]),
                    "peakMemoryMib": round(values["memoryBytes"] / 1024 ** 2, 1),
                    "peakBlockReadBytes": int(values["blockReadBytes"]),
                }
                for name, values in sorted(peaks.items())
            },
            "containerStates": states,
            "upSucceeded": up.returncode == 0,
        }
        (run_dir / "footprint.json").write_text(json.dumps(result, indent=2) + "\n")
        (run_dir / "series.json").write_text(json.dumps(series, indent=2) + "\n")
        if not sampling["trustworthy"] or oom_killed:
            raise RuntimeError("footprint sample is incomplete or contains OOM failures")

        first = offered_load.read_offered_load(options.project, str(stack_path))
        (run_dir / "demand-load-open.json").write_text(json.dumps(first.to_dict()) + "\n")
        samples = cpu_limits.sample_cpu_demand(
            options.project, duration_seconds=options.demand_seconds,
            raw_path=run_dir / "demand.raw",
        )
        second = offered_load.read_offered_load(options.project, str(stack_path))
        (run_dir / "demand-load-close.json").write_text(json.dumps(second.to_dict()) + "\n")
        demand = {
            "hostClass": derive_class_id(facts), "hostFingerprint": derive_fingerprint(facts),
            "basis": "cgroup usage_usec deltas", "intervalSeconds": 0.25,
            "windowSeconds": options.demand_seconds,
            "minIntervals": min(sample.intervals for sample in samples.values()),
            "totalGapIntervals": sum(sample.gap_intervals for sample in samples.values()),
            "cpuLimitedServices": rendered["services"],
            "load": offered_load.achieved_between(first, second).to_dict(),
            "services": {
                name: {"peakCores": sample.peak_cores, "meanCores": sample.mean_cores,
                       "intervals": sample.intervals, "gapIntervals": sample.gap_intervals}
                for name, sample in samples.items()
            },
        }
        (run_dir / "demand-report.json").write_text(json.dumps(demand, indent=2) + "\n")

        print(f"\npeak total memory {result['peakTotalMemoryGib']} GiB "
              f"of {result['dockerMemoryCeilingGib']} GiB ceiling "
              f"({result['percentOfCeiling']}%)", flush=True)
        print(f"sampling trustworthy: {sampling['trustworthy']} "
              f"({sampling['sampledServices']}/{sampling['expectedServices']} services, "
              f"{sampling['samples']} samples)", flush=True)
        if sampling["neverSampled"]:
            print(f"NEVER SAMPLED: {sampling['neverSampled']}", flush=True)
        if oom_killed:
            print(f"OOM KILLED: {sorted(oom_killed)}", flush=True)
        print(f"artifacts: {run_dir}", flush=True)

    finally:
        # Teardown belongs in a finally rather than at the end of the happy
        # path. Between `up` and here the tool samples for as long as the
        # caller asked, and a Ctrl-C during that window, or any exception
        # from the sampler, used to leave all 28 containers and their
        # networks running. The next run would then measure a host with a
        # whole shop already on it and report the result as a footprint.
        down = subprocess.run(
            compose + ["down", "--volumes", "--remove-orphans", "--timeout", "60"],
            capture_output=True, text=True, timeout=180,
        )
        residue = project.residual_resources()
        (run_dir / "cleanup.json").write_text(json.dumps(residue.to_dict(), indent=2) + "\n")
        if down.returncode != 0 or not residue.clean:
            raise RuntimeError(f"footprint cleanup failed: {down.stderr}; {residue.describe()}")
        shutil.rmtree(run_dir / "runtime")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
