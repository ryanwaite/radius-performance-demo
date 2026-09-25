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

Requires PyYAML 6.0.2:

    python3.12 -m venv .tools-venv
    .tools-venv/bin/pip install "PyYAML==6.0.2"
    .tools-venv/bin/python benchmark/tools/derive_collector_config.py

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
    sys.exit(
        "PyYAML is required. This is a tooling dependency, not a driver "
        "dependency:\n"
        "    python3.12 -m venv .tools-venv\n"
        '    .tools-venv/bin/pip install "PyYAML==6.0.2"'
    )

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

DERIVED_DIR = "derived/otel-collector"


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


def main() -> int:
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
        rendered = yaml.safe_dump(document, sort_keys=False, width=1000)
        target = out_dir / source.name
        target.write_text(rendered)

        leftover = sorted(
            name for name in REMOVED_RECEIVERS if name in rendered
        )
        if leftover:
            raise SystemExit(
                f"{source.name}: {leftover} still present after derivation"
            )

        results[source.name] = {
            "removed": removed,
            "sha256": hashlib.sha256(rendered.encode()).hexdigest(),
        }
        print(f"{source.name}: removed {len(removed)} reference(s)")
        for entry in removed:
            print(f"    - {entry}")

    manifest = {
        "upstreamTag": shop.UPSTREAM_TAG,
        "upstreamCommit": shop.UPSTREAM_COMMIT,
        "removedReceivers": REMOVED_RECEIVERS,
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
