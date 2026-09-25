"""The OpenTelemetry Astronomy Shop as a benchmark fixture.

The scored campaign runs against the Astronomy Shop rather than the catalog
application, because a four-service topology gives an application graph
almost nothing to explain. This module pins the upstream release, records
what we changed and why, and produces the Compose file a trial actually runs.

Upstream is vendored verbatim under `apps/astronomy-shop/upstream`, and
nothing in this module edits those files. The trial Compose file is generated
from them by applying a declared list of transforms. That split is deliberate:
"what did we change" is answerable by reading `TRANSFORMS`, re-vendoring a new
release is a clean replacement, and each transform is a testable object rather
than a diff someone has to notice.

Why a transform list rather than a Compose overlay: Compose merges a service's
`volumes` and `ports` by appending. An overlay can add a mount but cannot
remove one, and three of the transforms below are removals.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

# Pinned upstream. Changing either constant is a fixture change and a plan
# amendment, not a routine bump. `tools/vendor_astronomy_shop.py` refuses to
# copy anything if the tag no longer resolves to this commit.
UPSTREAM_REPO = "open-telemetry/opentelemetry-demo"
UPSTREAM_TAG = "3.1.0"
UPSTREAM_COMMIT = "dedc0178918e260823323b8d95005a8cb924b007"

COMPOSE_FILES = (
    "compose.yaml",
    "compose.full.yaml",
    "compose.observability.yaml",
)

# 3.1.0 rather than the newest-looking alternative, and the reason is specific.
# AIOpsLab's Astronomy Shop problems, which the incident increment ports, name
# eleven feature flags. 3.0.0 is missing `loadGeneratorFloodHomepage`: the load
# generator was swapped to k6 in 3.0.0 and reverted in 3.1.0, taking the flag
# out and putting it back. Verified by reading `src/flagd/demo.flagd.json` at
# both tags rather than taken from release notes.
AIOPSLAB_REQUIRED_FLAGS: tuple[str, ...] = (
    "adFailure",
    "adHighCpu",
    "adManualGc",
    "cartFailure",
    "imageSlowLoad",
    "kafkaQueueProblems",
    "loadGeneratorFloodHomepage",
    "paymentFailure",
    "paymentUnreachable",
    "productCatalogFailure",
    "recommendationCacheFailure",
)

# Services the flag service and its user interface run as. The plan puts both
# out of the agent's reach, so neither may be reachable from the host.
FLAG_SERVICES: tuple[str, ...] = ("flagd", "flagd-ui")

# Host paths upstream binds into the collector, both of which we remove. Kept
# as constants so the check that asserts their absence names the same strings
# the transform removes, rather than two lists that can drift apart.
DOCKER_SOCKET_PATHS: tuple[str, ...] = (
    "/var/run/docker.sock",
    "/run/docker.sock",
)
HOST_FILESYSTEM_MOUNT = "/hostfs"

# Where the collector's configs live before and after derivation. The trial
# stack mounts the derived copies; see `_use_derived_collector_config`.
UPSTREAM_COLLECTOR_SEGMENT = "upstream/src/otel-collector/"
DERIVED_COLLECTOR_SEGMENT = "derived/otel-collector/"


class AstronomyShopError(RuntimeError):
    """Raised when the vendored tree cannot produce a usable trial stack."""


@dataclass(frozen=True)
class Transform:
    """One declared modification of the upstream Compose definition.

    `rationale` is not decoration. Every one of these is a deviation from the
    reference application, and a deviation nobody can justify later is how a
    fixture quietly stops being the thing it claims to be.
    """

    name: str
    rationale: str
    apply: Callable[[dict[str, Any]], list[str]]

    def to_dict(self) -> dict[str, str]:
        return {"name": self.name, "rationale": self.rationale}


def _iter_services(config: dict[str, Any]):
    return sorted((config.get("services") or {}).items())


def _remove_docker_socket(config: dict[str, Any]) -> list[str]:
    changed = []
    for name, service in _iter_services(config):
        volumes = service.get("volumes") or []
        kept = [
            volume
            for volume in volumes
            if str(volume.get("source", "")) not in DOCKER_SOCKET_PATHS
        ]
        if len(kept) != len(volumes):
            service["volumes"] = kept
            changed.append(name)
    return changed


def _remove_host_filesystem(config: dict[str, Any]) -> list[str]:
    changed = []
    for name, service in _iter_services(config):
        volumes = service.get("volumes") or []
        kept = [
            volume
            for volume in volumes
            if str(volume.get("target", "")) != HOST_FILESYSTEM_MOUNT
        ]
        if len(kept) != len(volumes):
            service["volumes"] = kept
            changed.append(name)
    return changed


def _use_derived_collector_config(config: dict[str, Any]) -> list[str]:
    """Point the collector at the configs that match its actual mounts.

    `remove-docker-socket` and `remove-host-filesystem` take away mounts that
    the `docker_stats` and `host_metrics` receivers require. The collector
    validates receivers at startup, so leaving them configured turns the
    removal into a crash loop rather than an isolated environment. The
    derived configs are generated from upstream by
    `tools/derive_collector_config.py` and committed.

    A missing derived file is a hard error. Docker would otherwise create an
    empty directory at the mount point and the collector would start against
    a config that is not the one anybody reviewed.
    """
    changed = []
    for name, service in _iter_services(config):
        touched = False
        for volume in service.get("volumes") or []:
            source = str(volume.get("source", ""))
            if UPSTREAM_COLLECTOR_SEGMENT not in source:
                continue
            derived = Path(
                source.replace(UPSTREAM_COLLECTOR_SEGMENT, DERIVED_COLLECTOR_SEGMENT)
            )
            if not derived.is_file():
                raise FileNotFoundError(
                    f"derived collector config missing: {derived}. "
                    "Run tools/derive_collector_config.py."
                )
            volume["source"] = str(derived)
            touched = True
        if touched:
            changed.append(name)
    return changed


def _scope_container_names(config: dict[str, Any]) -> list[str]:
    """Let Compose name containers from the project, as isolation requires.

    Upstream sets an explicit `container_name` on all 28 services, which
    overrides the project-scoped name Compose would otherwise derive. Two
    consequences, both fatal to a per-trial environment. A second trial on
    the same host cannot start, because the names are already taken. And the
    names carry no project prefix, so anything that identifies a trial's
    containers by name cannot tell them apart from another trial's, or from
    an unrelated container that happens to be called `frontend`.
    """
    changed = []
    for name, service in _iter_services(config):
        if service.pop("container_name", None) is not None:
            changed.append(name)
    return changed


def _scope_network_names(config: dict[str, Any]) -> list[str]:
    """Drop the fixed network name so each project gets its own network.

    Upstream pins `networks.default.name` to `opentelemetry-demo`. Compose
    normally derives `<project>_default`, which keeps concurrent projects on
    separate bridges. With the name pinned, two trials share one network and
    each one's services can resolve and reach the other's.
    """
    changed = []
    for name, network in sorted((config.get("networks") or {}).items()):
        if isinstance(network, dict) and network.pop("name", None) is not None:
            changed.append(name)
    return changed


def _unpublish_all_ports(config: dict[str, Any]) -> list[str]:
    """Drop every fixed host port; the driver allocates them dynamically.

    Upstream publishes `frontend-proxy` on 8080 and `prometheus` on 9090.
    Fixed ports collide between concurrent runs and make a trial depend on
    what else happens to hold the port, which is the opposite of an isolated
    environment. Unpublished container ports stay declared, so Compose still
    assigns an ephemeral host port and the driver rediscovers it.
    """
    changed = []
    for name, service in _iter_services(config):
        ports = service.get("ports") or []
        touched = False
        for port in ports:
            if port.get("published"):
                port.pop("published", None)
                touched = True
        if touched:
            changed.append(name)
    return changed


def _hide_flag_services(config: dict[str, Any]) -> list[str]:
    """Remove the flag service and its interface from the host entirely.

    The plan puts the flag service's configuration and user interface out of
    the agent's reach. Unpublishing is the mechanism; the services keep their
    ports on the internal network so the application still reads its flags.
    """
    changed = []
    for name in FLAG_SERVICES:
        service = (config.get("services") or {}).get(name)
        if service is None:
            continue
        if service.get("ports"):
            service["ports"] = []
            changed.append(name)
    return changed


TRANSFORMS: tuple[Transform, ...] = (
    Transform(
        name="remove-docker-socket",
        rationale=(
            "Upstream binds the host Docker socket into the collector for its "
            "docker_stats receiver. Three reasons to remove it, of which the "
            "third is the one that would have corrupted measurements. It "
            "violates the standing rule against exposing the Docker socket to "
            "any container in an agent-facing environment. A read-only bind "
            "does not help: :ro applies to the socket file, not to the API "
            "reachable through it. And a collector holding the socket reports "
            "metrics for every container on the machine, including other "
            "Compose projects, so two concurrent trials would each ingest the "
            "other's containers as if they were their own."
        ),
        apply=_remove_docker_socket,
    ),
    Transform(
        name="remove-host-filesystem",
        rationale=(
            "Upstream binds the entire host filesystem at /hostfs so the "
            "host_metrics receiver can report the machine's CPU, disk and "
            "load. Those are properties of the laptop, not of the trial, and "
            "they move with whatever else is running. Feeding them into the "
            "telemetry an agent diagnoses from injects exactly the "
            "noisy-neighbour signal the isolated environment exists to "
            "exclude."
        ),
        apply=_remove_host_filesystem,
    ),
    Transform(
        name="use-derived-collector-config",
        rationale=(
            "The two mount removals above take away files that the "
            "docker_stats and host_metrics receivers require, and the "
            "collector validates its receivers at startup. Removing the "
            "mount without removing the receiver does not isolate the "
            "collector, it crash-loops it: the first footprint attempt "
            "failed with 'invalid root_path: stat /hostfs: no such file or "
            "directory'. The derived configs are generated from upstream by "
            "tools/derive_collector_config.py and committed, so the driver "
            "needs no YAML parser and the change is reviewable in a diff."
        ),
        apply=_use_derived_collector_config,
    ),
    Transform(
        name="scope-container-names",
        rationale=(
            "Upstream sets an explicit container_name on all 28 services, "
            "overriding the project-scoped name Compose would derive. That "
            "defeats the unique-project-name isolation the driver depends "
            "on: a second trial cannot start because the names are taken, "
            "and no name carries a project prefix, so a trial's containers "
            "cannot be told apart from another trial's or from an unrelated "
            "container that happens to be called 'frontend'."
        ),
        apply=_scope_container_names,
    ),
    Transform(
        name="scope-network-names",
        rationale=(
            "Upstream pins networks.default.name to 'opentelemetry-demo'. "
            "Compose would otherwise derive <project>_default and keep "
            "concurrent projects on separate bridges. With the name pinned, "
            "two trials share one network and each one's services can "
            "resolve and reach the other's."
        ),
        apply=_scope_network_names,
    ),
    Transform(
        name="unpublish-fixed-ports",
        rationale=(
            "Upstream publishes frontend-proxy on 8080 and prometheus on "
            "9090. Fixed host ports collide between concurrent runs and make "
            "a trial depend on what else holds the port. The driver allocates "
            "dynamically and rediscovers after every container recreation."
        ),
        apply=_unpublish_all_ports,
    ),
    Transform(
        name="hide-flag-services",
        rationale=(
            "The flag service and its user interface are out of the agent's "
            "reach by design, because an agent that can read the flag state "
            "can read the answer. They keep their ports on the internal "
            "network so the application still resolves its flags."
        ),
        apply=_hide_flag_services,
    ),
)


@dataclass
class TransformReport:
    """What each transform actually changed, for the run record."""

    applied: dict[str, list[str]] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "transforms": [transform.to_dict() for transform in TRANSFORMS],
            "servicesChanged": {k: sorted(v) for k, v in self.applied.items()},
        }


def apply_transforms(config: dict[str, Any]) -> TransformReport:
    """Apply every declared transform in order, recording what each touched.

    A transform that changes nothing is reported as changing nothing rather
    than skipped silently. An upstream bump that removes the Docker socket
    mount on its own should show up as an empty list here, not as a transform
    that quietly became a no-op while still claiming to protect something.
    """
    report = TransformReport()
    for transform in TRANSFORMS:
        report.applied[transform.name] = transform.apply(config)
    return report


def upstream_dir(repo_root: Path) -> Path:
    """The vendored tree, always absolute.

    Resolved rather than returned as given, because every caller then runs
    `docker compose` with its working directory set to this directory. A
    relative path would be interpreted twice and silently resolve to a
    doubled, nonexistent path.
    """
    path = (Path(repo_root) / "benchmark" / "apps" / "astronomy-shop" / "upstream").resolve()
    if not path.is_dir():
        raise AstronomyShopError(
            f"the Astronomy Shop is not vendored at {path}; run "
            "tools/vendor_astronomy_shop.py"
        )
    return path


def compose_file_paths(repo_root: Path) -> tuple[Path, ...]:
    base = upstream_dir(repo_root)
    paths = tuple(base / name for name in COMPOSE_FILES)
    missing = [str(path) for path in paths if not path.is_file()]
    if missing:
        raise AstronomyShopError(
            "vendored tree is incomplete, missing: " + ", ".join(missing)
        )
    return paths


def declared_flags(repo_root: Path) -> dict[str, Any]:
    """The flag definitions the vendored release ships."""
    path = upstream_dir(repo_root) / "src" / "flagd" / "demo.flagd.json"
    try:
        payload = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise AstronomyShopError(f"cannot read {path}: {exc}") from exc
    flags = payload.get("flags")
    if not isinstance(flags, dict) or not flags:
        raise AstronomyShopError(f"{path} declares no flags")
    return flags


def default_off_flags(flags: dict[str, Any]) -> dict[str, str]:
    """Each flag's off variant, as upstream declares it.

    Read from the shipped definition rather than assumed to be `"off"`. Some
    flags are graded and their neutral variant is a number or a duration, so a
    hard-coded "off" would report a graded flag as active in a healthy
    baseline and fail every clean run.
    """
    resolved: dict[str, str] = {}
    for name, flag in flags.items():
        variant = flag.get("defaultVariant")
        if variant is None:
            raise AstronomyShopError(f"flag {name!r} declares no defaultVariant")
        resolved[name] = str(variant)
    return resolved


_IMAGE_TAG_RE = re.compile(r"^(?P<repo>[^@]+?):(?P<tag>[^:/@]+)$")


def image_references(config: dict[str, Any]) -> dict[str, str]:
    """Every service's image reference, as the merged config states it."""
    return {
        name: str(service.get("image", ""))
        for name, service in _iter_services(config)
        if service.get("image")
    }


def floating_references(config: dict[str, Any]) -> dict[str, str]:
    """Image references that are not already pinned by digest.

    Upstream resolves its own images to `ghcr.io/open-telemetry/demo:latest-*`,
    which is a floating tag by construction: the content behind `latest-cart`
    changes whenever upstream publishes. Every one of these is resolved to a
    digest once and recorded, which is what makes a trial reproducible.
    """
    return {
        name: reference
        for name, reference in image_references(config).items()
        if "@sha256:" not in reference
    }


__all__ = [
    "AIOPSLAB_REQUIRED_FLAGS",
    "COMPOSE_FILES",
    "DOCKER_SOCKET_PATHS",
    "FLAG_SERVICES",
    "HOST_FILESYSTEM_MOUNT",
    "TRANSFORMS",
    "UPSTREAM_COMMIT",
    "UPSTREAM_REPO",
    "UPSTREAM_TAG",
    "AstronomyShopError",
    "Transform",
    "TransformReport",
    "apply_transforms",
    "compose_file_paths",
    "declared_flags",
    "default_off_flags",
    "floating_references",
    "image_references",
    "upstream_dir",
]
