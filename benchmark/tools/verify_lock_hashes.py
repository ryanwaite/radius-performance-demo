#!/usr/bin/env python3
"""Check that every hash in the exported requirements file is a digest that
public PyPI publishes for that exact package version.

Why this exists
---------------
The lock is resolved through the Microsoft CFS proxy, but CI installs from
public PyPI. That only works if CFS serves byte-identical artifacts, so the
hashes minted against CFS validate against pythonhosted. If CFS ever served a
rebuilt or re-signed artifact, the hash would differ and every CI install would
fail with a hash mismatch -- loudly, but only after the change had landed.

This script checks the premise directly and in advance: it asks PyPI which
sha256 digests it publishes for each pinned version, and asserts that every
hash we export is in that published set. It is the generalisation of a single
spot check on one package to the whole dependency set.

It deliberately does NOT download artifacts. PyPI's JSON API serves the
digests as metadata, which is enough to compare, and artifact download from
files.pythonhosted.org is not reachable from every managed network. Comparing
metadata means this check runs in places the full install cannot.

Direction of the check
----------------------
We assert `exported hashes` is a SUBSET of `PyPI published digests`. It is not
equality: the resolver picks the subset of wheels relevant to our
requires-python and platforms, so PyPI legitimately publishes digests we do
not reference. A hash we carry that PyPI does not publish is the finding.

Usage:
    python tools/verify_lock_hashes.py [--requirements PATH] [--jobs N]

Exits non-zero if any exported hash is not published by PyPI, or if any pinned
version cannot be looked up.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import json
import re
import sys
import urllib.error
import urllib.request
from pathlib import Path

PYPI_JSON = "https://pypi.org/pypi/{name}/{version}/json"

DEFAULT_REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements-ci.txt"

# "name==1.2.3", optionally followed by " ; marker", optionally " \"
_PIN_RE = re.compile(r"^(?P<name>[A-Za-z0-9._-]+)==(?P<version>[^\s;\\]+)")
_HASH_RE = re.compile(r"--hash=sha256:(?P<digest>[0-9a-f]{64})")


def parse_requirements(path: Path) -> dict[tuple[str, str], set[str]]:
    """Map (name, version) -> set of sha256 hashes referenced for it."""
    pins: dict[tuple[str, str], set[str]] = {}
    current: tuple[str, str] | None = None

    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue

        pin = _PIN_RE.match(line)
        if pin:
            current = (pin.group("name"), pin.group("version"))
            pins.setdefault(current, set())

        if current is not None:
            for match in _HASH_RE.finditer(line):
                pins[current].add(match.group("digest"))

    return pins


def published_digests(name: str, version: str, timeout: float) -> set[str]:
    """sha256 digests PyPI publishes for this exact version."""
    url = PYPI_JSON.format(name=name, version=version)
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.load(response)
    return {
        entry["digests"]["sha256"]
        for entry in payload.get("urls", [])
        if "sha256" in entry.get("digests", {})
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--requirements", type=Path, default=DEFAULT_REQUIREMENTS)
    parser.add_argument("--jobs", type=int, default=8)
    parser.add_argument("--timeout", type=float, default=30.0)
    args = parser.parse_args()

    if not args.requirements.is_file():
        print(f"error: no requirements file at {args.requirements}", file=sys.stderr)
        return 2

    pins = parse_requirements(args.requirements)
    if not pins:
        # An empty file would otherwise "pass" by verifying nothing, which is
        # the vacuous-success failure mode this project treats as a defect.
        print("error: parsed zero pinned packages; refusing to report success", file=sys.stderr)
        return 2

    total_hashes = sum(len(h) for h in pins.values())
    print(f"Checking {len(pins)} packages / {total_hashes} hashes against public PyPI\n")

    unpublished: list[str] = []
    lookup_failures: list[str] = []

    def check(item):
        (name, version), hashes = item
        try:
            return name, version, hashes, published_digests(name, version, args.timeout), None
        except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError, OSError) as exc:
            return name, version, hashes, set(), str(exc)

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for name, version, hashes, published, error in pool.map(check, sorted(pins.items())):
            if error is not None:
                lookup_failures.append(f"{name}=={version}: {error}")
                print(f"  ??  {name}=={version}: lookup failed: {error}")
                continue

            missing = hashes - published
            if missing:
                unpublished.append(f"{name}=={version}: {sorted(missing)}")
                print(f"  !!  {name}=={version}: {len(missing)} hash(es) NOT published by PyPI")
            else:
                print(f"  ok  {name}=={version}: {len(hashes)}/{len(published)} hashes published")

    print()
    if lookup_failures:
        print(f"FAILED: {len(lookup_failures)} package(s) could not be looked up:")
        for entry in lookup_failures:
            print(f"  {entry}")
    if unpublished:
        print(f"FAILED: {len(unpublished)} package(s) carry hashes PyPI does not publish:")
        for entry in unpublished:
            print(f"  {entry}")

    if lookup_failures or unpublished:
        return 1

    print(
        f"OK: all {total_hashes} exported hashes across {len(pins)} packages are "
        "digests published by public PyPI."
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
