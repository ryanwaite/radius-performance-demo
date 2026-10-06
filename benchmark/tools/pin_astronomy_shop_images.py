#!/usr/bin/env python3
"""Resolve every Astronomy Shop image to a digest and write the manifest.

Run from the repository root:

    python3 benchmark/tools/pin_astronomy_shop_images.py

Upstream resolves its own services to `ghcr.io/open-telemetry/demo:latest-<x>`.
That is a floating tag by construction: the content behind `latest-cart`
changes whenever upstream publishes, so two trials a week apart can run
different code while claiming the same fixture. Every reference is resolved
once and recorded here, and the manifest's hash goes into each run's
provenance. Re-running this and getting different digests is a fixture change
and a plan amendment, not a refresh.

The script refuses to overwrite an existing manifest unless `--allow-change`
is passed, and prints the differences it would have made. Silently rewriting
the pins would defeat the point of having them.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radius_perf_eval import astronomy_shop as shop  # noqa: E402
from radius_perf_eval.podman_runtime import RuntimePolicyError, require_podman_driver  # noqa: E402

MANIFEST_PATH = Path("benchmark/apps/astronomy-shop/image-digests.json")


def merged_config(repo_root: Path) -> dict:
    paths = shop.compose_file_paths(repo_root)
    args = ["docker", "compose"]
    for path in paths:
        args += ["-f", str(path)]
    args += ["config", "--format", "json"]
    # Upstream reads these two for the mounts we strip in `apply_transforms`.
    # They still have to resolve for `config` to parse, so they are supplied
    # here and never reach a running container.
    env = dict(os.environ, DOCKER_SOCK="/var/run/docker.sock", HOST_FILESYSTEM="/")
    result = subprocess.run(
        args, capture_output=True, text=True, env=env, cwd=str(paths[0].parent)
    )
    if result.returncode != 0:
        raise SystemExit(f"docker compose config failed: {result.stderr.strip()}")
    return json.loads(result.stdout)


def resolve(references: list[str], *, pull: bool) -> dict[str, str]:
    if pull:
        for reference in references:
            subprocess.run(
                ["docker", "pull", "--quiet", reference],
                capture_output=True,
                text=True,
                check=False,
            )
    resolved: dict[str, str] = {}
    for reference in references:
        result = subprocess.run(
            ["docker", "image", "inspect", reference, "--format", "{{json .RepoDigests}}"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            raise SystemExit(f"cannot inspect {reference}: {result.stderr.strip()}")
        digests = json.loads(result.stdout)
        if not digests:
            raise SystemExit(
                f"{reference} has no repository digest. An image built locally "
                "or loaded from a tarball cannot be pinned, because there is no "
                "registry content to pin to."
            )
        resolved[reference] = str(digests[0])
    return resolved


def manifest_hash(payload: dict) -> str:
    canonical = json.dumps(payload["images"], sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode()).hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--allow-change", action="store_true")
    parser.add_argument("--no-pull", action="store_true")
    options = parser.parse_args()
    require_podman_driver()

    repo_root = Path(__file__).resolve().parents[2]
    config = merged_config(repo_root)
    references = shop.image_references(config)
    resolved = resolve(sorted(set(references.values())), pull=not options.no_pull)

    payload = {
        "upstream": {
            "repo": shop.UPSTREAM_REPO,
            "tag": shop.UPSTREAM_TAG,
            "commit": shop.UPSTREAM_COMMIT,
        },
        "note": (
            "Upstream publishes floating tags. These digests are the fixture. "
            "Changing one is a fixture change and a plan amendment."
        ),
        "images": {service: resolved[ref] for service, ref in sorted(references.items())},
        "taggedReferences": {
            service: ref for service, ref in sorted(references.items())
        },
    }
    payload["manifestHash"] = manifest_hash(payload)

    target = repo_root / MANIFEST_PATH
    if target.exists():
        existing = json.loads(target.read_text())
        if "startupAssets" in existing:
            payload["startupAssets"] = existing["startupAssets"]
        if existing.get("manifestHash") == payload["manifestHash"]:
            print(f"unchanged: {payload['manifestHash']}")
            return 0
        moved = {
            service: (existing["images"].get(service), digest)
            for service, digest in payload["images"].items()
            if existing["images"].get(service) != digest
        }
        print(f"{len(moved)} image(s) differ from the recorded manifest:")
        for service, (was, now) in sorted(moved.items()):
            print(f"  {service}: {was} -> {now}")
        if not options.allow_change:
            print(
                "\nrefusing to rewrite the manifest. Re-pinning is a fixture "
                "change; pass --allow-change once that is the intent.",
                file=sys.stderr,
            )
            return 1

    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(payload, indent=2, sort_keys=False) + "\n")
    print(f"wrote {MANIFEST_PATH} with {len(payload['images'])} services")
    print(f"manifestHash {payload['manifestHash']}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except RuntimePolicyError as error:
        print(f"runtime policy: {error}", file=sys.stderr)
        raise SystemExit(2)
