#!/usr/bin/env python3
"""Vendor the OpenTelemetry Astronomy Shop's Compose setup at a pinned release.

Run from the repository root:

    python3 benchmark/tools/vendor_astronomy_shop.py

The tag and commit are constants below, not arguments. Re-vendoring a
different release is a fixture change and a plan amendment, so it should be a
reviewed edit to this file rather than something a caller can do in passing.

The script verifies that the tag still resolves to the pinned commit before it
copies anything. A moved tag means the upstream history was rewritten, which
is exactly the case where silently taking the new content would be wrong.

Only the files the Compose setup actually reads are copied. The list is
derived from the three Compose files with environment variables expanded,
because several bind mounts are written as `${OTEL_COLLECTOR_CONFIG}` rather
than a literal path and a scan for `./` misses them. `tests.test_driver`
asserts the copied tree still covers every bind mount, so a future release
that adds one fails rather than producing a stack that cannot start.
"""

from __future__ import annotations

import io
import json
import re
import shutil
import sys
import tarfile
import urllib.request
from pathlib import Path

UPSTREAM_REPO = "open-telemetry/opentelemetry-demo"
UPSTREAM_TAG = "3.1.0"
UPSTREAM_COMMIT = "dedc0178918e260823323b8d95005a8cb924b007"

# The three layers we run. `compose.yaml` is the core shop, `compose.full.yaml`
# adds Kafka with the accounting and fraud-detection consumers, and
# `compose.observability.yaml` adds Jaeger, Prometheus, OpenSearch and Grafana.
# The benchmark needs all three: the plan's incident set includes queue
# backlog, and an agent that cannot read telemetry cannot diagnose anything.
COMPOSE_FILES = (
    "compose.yaml",
    "compose.full.yaml",
    "compose.observability.yaml",
)

SUPPORTING_PATHS = (
    ".env",
    "LICENSE",
    "otel-config.yml",
    "src/flagd",
    "src/grafana",
    "src/jaeger",
    "src/otel-collector",
    "src/postgresql",
    "src/prometheus",
)

VENDOR_DIR = Path("benchmark/apps/astronomy-shop/upstream")


def resolve_tag(repo: str, tag: str) -> str:
    url = f"https://api.github.com/repos/{repo}/git/ref/tags/{tag}"
    with urllib.request.urlopen(url, timeout=30) as response:
        payload = json.load(response)
    return str(payload["object"]["sha"])


def fetch_tree(repo: str, tag: str) -> Path:
    url = f"https://github.com/{repo}/archive/refs/tags/{tag}.tar.gz"
    with urllib.request.urlopen(url, timeout=120) as response:
        blob = response.read()
    destination = Path("/tmp") / f"astronomy-shop-{tag}"
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    with tarfile.open(fileobj=io.BytesIO(blob), mode="r:gz") as archive:
        members = []
        for member in archive.getmembers():
            parts = Path(member.name).parts
            if len(parts) < 2:
                continue
            member.name = str(Path(*parts[1:]))
            members.append(member)
        archive.extractall(destination, members=members, filter="data")
    return destination


def bind_mount_sources(root: Path) -> set[str]:
    """Every host path the Compose files bind, with variables expanded."""
    env: dict[str, str] = {}
    for line in (root / ".env").read_text().splitlines():
        if line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip()

    def expand(text: str) -> str:
        for _ in range(5):
            text = re.sub(
                r"\$\{([A-Za-z0-9_]+)(?::-[^}]*)?\}",
                lambda match: env.get(match.group(1), match.group(0)),
                text,
            )
        return text

    sources: set[str] = set()
    for name in COMPOSE_FILES:
        for line in (root / name).read_text().splitlines():
            stripped = line.strip()
            if not stripped.startswith("- "):
                continue
            value = expand(stripped[2:].strip().strip('"').strip("'"))
            if ":" not in value:
                continue
            host = value.split(":")[0]
            if host.startswith("./") or host.startswith("/"):
                sources.add(host)
    return sources


def main() -> int:
    repo_root = Path(__file__).resolve().parents[2]
    observed = resolve_tag(UPSTREAM_REPO, UPSTREAM_TAG)
    if observed != UPSTREAM_COMMIT:
        print(
            f"tag {UPSTREAM_TAG} now resolves to {observed}, not the pinned "
            f"{UPSTREAM_COMMIT}. The upstream history moved underneath the pin; "
            "nothing was copied.",
            file=sys.stderr,
        )
        return 1

    source = fetch_tree(UPSTREAM_REPO, UPSTREAM_TAG)
    target = repo_root / VENDOR_DIR
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True)

    for name in COMPOSE_FILES + SUPPORTING_PATHS:
        origin = source / name
        destination = target / name
        destination.parent.mkdir(parents=True, exist_ok=True)
        if origin.is_dir():
            shutil.copytree(origin, destination)
        else:
            shutil.copy2(origin, destination)

    missing = [
        path
        for path in sorted(bind_mount_sources(source))
        if path.startswith("./") and not (target / path[2:]).exists()
    ]
    if missing:
        print(
            "these bind mounts have no vendored file: " + ", ".join(missing),
            file=sys.stderr,
        )
        return 1

    print(f"vendored {UPSTREAM_REPO}@{UPSTREAM_TAG} ({UPSTREAM_COMMIT}) into {VENDOR_DIR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
