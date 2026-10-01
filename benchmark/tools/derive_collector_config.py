#!/usr/bin/env python3
"""Generate the collector configs the trial stack actually runs.

Two of the declared transforms remove a bind mount that a collector receiver
depends on. Removing the mount alone is not enough: the collector validates
its receivers at startup and refuses to run when `host_metrics` cannot stat
its `root_path`, so the service crash-loops and `up --wait` blocks until it
times out. That is not a hypothetical. It happened on the first footprint
attempt, and the collector logged

    invalid configuration: receivers::host_metrics: invalid root_path:
    stat /hostfs: no such file or directory

The receivers have to go with the mounts. This script derives the configs
once, at vendoring time, and the result is committed. Deriving at run time
would mean parsing YAML inside the driver, and the driver deliberately
depends on nothing outside the standard library so its test suite runs
anywhere. So PyYAML is a tooling dependency, used here and never imported by
`radius_perf_eval`.

Uses PyYAML from the locked CFS environment:

    benchmark/.venv/bin/python benchmark/tools/derive_collector_config.py

Regenerating is a fixture change: the derived files are hashed into the run
record, so a change to them shows up as a changed fixture rather than as a
silent difference in what the collector was told to do.
"""

from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path

try:
    import yaml
except ModuleNotFoundError:  # pragma: no cover - tooling-only path
    # Deferred rather than fatal here. The strip functions below are pure
    # dictionary transforms with no YAML involved, and they carry the rules
    # worth testing. Exiting at import time would make them unreachable from
    # the driver's test suite, which runs on the standard library alone.
    yaml = None

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radius_perf_eval import astronomy_shop as shop  # noqa: E402

# Receivers removed, and the transform each one belongs to.
#
# Both read host-scoped state that is a property of the machine rather than
# of the trial. Leaving either in place while removing its mount turns a
# deliberate isolation decision into a crash loop.
REMOVED_RECEIVERS: dict[str, str] = {
    "docker_stats": "remove-docker-socket",
    "host_metrics": "remove-host-filesystem",
}

# Exporters removed, keyed by their *full* name.
#
# Receivers above are matched on the part before the slash, because a
# receiver's base name identifies its type and every instance of that type is
# going. Exporters must not be matched that way. `otlp_grpc` is the type of
# both `otlp_grpc/firepit` and `otlp_grpc/jaeger`, so a base-name match would
# delete trace export to Jaeger along with the intended target, and the
# resulting config would still be valid YAML and still start. The asymmetry is
# deliberate and the reason is that a type is the right granularity for one
# and the wrong granularity for the other.
#
# `firepit` is a profiling backend that the Compose deployment never declares.
# It is not one of the 28 services, so the endpoint does not resolve, and the
# exporter retries against a host that does not exist for the whole life of
# every trial. That burns CPU and fills the collector's logs during the exact
# window we are measuring.
REMOVED_EXPORTERS: dict[str, str] = {
    "otlp_grpc/firepit": "remove-unresolvable-firepit-exporter",
}

# Any occurrence of these substrings in a derived file means the strip missed
# something. Checked against the rendered text, so a reference hiding in a
# structure this script does not walk is still caught.
FORBIDDEN_SUBSTRINGS: tuple[str, ...] = ("firepit",)

DERIVED_DIR = "derived/otel-collector"


def add_direct_metrics(document: dict) -> bool:
    """Keep OTLP telemetry and add an internal, synchronous audit endpoint."""
    telemetry = (document.get("service") or {}).get("telemetry")
    if telemetry is None:
        return False
    readers = telemetry["metrics"]["readers"]
    if not isinstance(readers, list) or not readers or any("pull" in reader for reader in readers):
        raise ValueError("expected existing periodic telemetry and no pull reader")
    readers.append({"pull": {"exporter": {"prometheus": {"host": "0.0.0.0", "port": 8888}}}})
    return True


def strip_receivers(document: dict) -> tuple[dict, list[str]]:
    """Remove the receivers and every pipeline reference to them.

    Removing the definition but leaving the name in a pipeline's receiver
    list is still a fatal config error, so both have to go. Returns what was
    actually removed so a receiver that upstream drops on its own shows up as
    an empty list rather than as a step that quietly stopped doing anything.
    """
    removed: list[str] = []

    receivers = document.get("receivers")
    if isinstance(receivers, dict):
        for name in list(receivers):
            base = str(name).split("/", 1)[0]
            if base in REMOVED_RECEIVERS:
                receivers.pop(name)
                removed.append(f"receivers::{name}")

    pipelines = ((document.get("service") or {}).get("pipelines") or {})
    if isinstance(pipelines, dict):
        for pipeline_name, pipeline in pipelines.items():
            if not isinstance(pipeline, dict):
                continue
            listed = pipeline.get("receivers")
            if not isinstance(listed, list):
                continue
            kept = [
                entry
                for entry in listed
                if str(entry).split("/", 1)[0] not in REMOVED_RECEIVERS
            ]
            if len(kept) != len(listed):
                dropped = sorted(set(listed) - set(kept))
                pipeline["receivers"] = kept
                removed.extend(
                    f"service::pipelines::{pipeline_name}::{entry}"
                    for entry in dropped
                )

    return document, removed


def strip_exporters(document: dict) -> tuple[dict, list[str]]:
    """Remove the exporters in `REMOVED_EXPORTERS` and their pipeline entries.

    Matching is on the full name rather than the type, for the reason given
    where `REMOVED_EXPORTERS` is defined.

    A pipeline whose exporter list would end up empty is a fatal error rather
    than something to write out. The collector refuses to start on an empty
    exporter list, so emitting one would turn a config change into a crash
    loop discovered at `up --wait` timeout, which is the failure mode this
    whole script exists to prevent.
    """
    removed: list[str] = []

    exporters = document.get("exporters")
    if isinstance(exporters, dict):
        for name in list(exporters):
            if str(name) in REMOVED_EXPORTERS:
                exporters.pop(name)
                removed.append(f"exporters::{name}")

    pipelines = ((document.get("service") or {}).get("pipelines") or {})
    if isinstance(pipelines, dict):
        for pipeline_name, pipeline in pipelines.items():
            if not isinstance(pipeline, dict):
                continue
            listed = pipeline.get("exporters")
            if not isinstance(listed, list):
                continue
            kept = [entry for entry in listed if str(entry) not in REMOVED_EXPORTERS]
            if len(kept) == len(listed):
                continue
            if not kept:
                raise SystemExit(
                    f"service::pipelines::{pipeline_name} would be left with no "
                    "exporters, which the collector rejects at startup"
                )
            dropped = sorted(set(listed) - set(kept))
            pipeline["exporters"] = kept
            removed.extend(
                f"service::pipelines::{pipeline_name}::{entry}"
                for entry in dropped
            )

    return document, removed


def main() -> int:
    if yaml is None:
        sys.exit(
            "PyYAML is required. Run this tool with benchmark/.venv/bin/python "
            "after restoring the frozen CFS environment. Do not use a public package index."
        )
    repo_root = Path(__file__).resolve().parents[2]
    upstream = shop.upstream_dir(repo_root)
    out_dir = repo_root / "benchmark/apps/astronomy-shop" / DERIVED_DIR
    out_dir.mkdir(parents=True, exist_ok=True)

    source_dir = upstream / "src/otel-collector"
    results: dict[str, dict] = {}

    for source in sorted(source_dir.glob("otelcol-config*.yml")):
        original = source.read_text()
        document = yaml.safe_load(original)
        if not isinstance(document, dict):
            # otelcol-config-extras.yml ships empty on purpose.
            document = {}
        document, removed = strip_receivers(document)
        document, removed_exporters = strip_exporters(document)
        removed = removed + removed_exporters
        direct_metrics = add_direct_metrics(document)
        rendered = yaml.safe_dump(document, sort_keys=False, width=1000)
        target = out_dir / source.name
        target.write_text(rendered)

        leftover = sorted(
            name for name in REMOVED_RECEIVERS if name in rendered
        )
        leftover += sorted(
            token for token in FORBIDDEN_SUBSTRINGS if token in rendered
        )
        if leftover:
            raise SystemExit(
                f"{source.name}: {leftover} still present after derivation"
            )

        results[source.name] = {
            "removed": removed,
            "directMetrics": direct_metrics,
            "sourceSha256": hashlib.sha256(original.encode()).hexdigest(),
            "sha256": hashlib.sha256(rendered.encode()).hexdigest(),
        }
        print(f"{source.name}: removed {len(removed)} reference(s)")
        for entry in removed:
            print(f"    - {entry}")

    manifest = {
        "upstreamTag": shop.UPSTREAM_TAG,
        "upstreamCommit": shop.UPSTREAM_COMMIT,
        "removedReceivers": REMOVED_RECEIVERS,
        "removedExporters": REMOVED_EXPORTERS,
        "files": results,
    }
    digest = hashlib.sha256(
        json.dumps(manifest, sort_keys=True).encode()
    ).hexdigest()
    manifest["manifestHash"] = f"sha256:{digest}"

    manifest_path = out_dir.parent / "collector-config-manifest.json"
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"\nmanifest {manifest['manifestHash']}")
    print(f"wrote {manifest_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
