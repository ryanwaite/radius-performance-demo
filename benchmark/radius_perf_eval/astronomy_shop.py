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
import shutil
import tempfile
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
# The service that only ever reads the flags, so its copy can be read-only.
FLAGD_SERVICE = "flagd"
# The vendored directory both flag services bind, matched by name rather than
# by full path so the transform still fires when the checkout moves.
FLAG_DIR_NAME = "flagd"

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
    apply: Callable[..., list[str]]
    # True when the transform needs the observed host class. Only the CPU
    # limits need it, and it is threaded through explicitly rather than
    # observed inside the transform so that rendering stays testable without
    # a Docker daemon, while the production path still observes rather than
    # accepts a label.
    needs_host_class: bool = False

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



def _bind_source(mount: Any) -> tuple[str | None, str]:
    """Read a bind mount's source and target in either compose syntax.

    `docker compose config` normalises volumes to the long dict form, so the
    rendered stack only ever holds dicts. The upstream compose files that feed
    it use the short `source:target[:mode]` string form. Handling only dicts
    would work today and fail quietly the moment anything parses those files
    without normalising them first, which is the failure this transform exists
    to prevent. Returns `(None, "")` for anything that is not a bind.
    """
    if isinstance(mount, dict):
        if mount.get("type") != "bind":
            return None, ""
        return str(mount.get("source", "")) or None, str(mount.get("target", ""))
    if isinstance(mount, str):
        parts = mount.split(":")
        if len(parts) < 2:
            return None, ""
        source, target = parts[0], parts[1]
        if not source.startswith((".", "/", "~")):
            return None, ""
        return source, target
    return None, ""


def _isolate_flag_file(config: dict[str, Any]) -> list[str]:
    """Give each rendered stack its own copy of the flag definitions.

    Upstream binds the vendored ``src/flagd`` directory into both flagd and
    flagd-ui, and flagd-ui mounts it read-write because writing flags is what
    that interface is for. Three consequences, and the second is the one that
    corrupts a campaign silently. A flag toggled during a trial edits a file
    that is committed to this repository, so the working tree goes dirty and
    the fixture hash no longer describes what is on disk. The edit survives
    teardown, because `down --volumes` removes volumes and this is a bind, so
    the next trial starts from the previous trial's flag state rather than
    from the baseline. And two stacks rendered from the same checkout share
    one file, so they are not isolated from each other at all.

    Each render therefore gets its own copy, and the mount points at that.
    flagd's copy is additionally read-only, since flagd only reads.

    The copy is made eagerly rather than by pointing at a path Docker would
    create on demand: a missing bind source becomes an empty directory, flagd
    would start with no flags at all, and the flag gate would then be reading
    a state nothing had defined.
    """
    changed = []
    vendored = None
    mounted = [
        name
        for name in FLAG_SERVICES
        if ((config.get("services") or {}).get(name) or {}).get("volumes")
    ]
    for name in mounted:
        service = config["services"][name]
        for mount in service.get("volumes") or []:
            source, _ = _bind_source(mount)
            if source is not None and Path(source).name == FLAG_DIR_NAME:
                vendored = Path(source)
    if vendored is None:
        if mounted:
            raise AstronomyShopError(
                f"{', '.join(mounted)} mount volumes but none is a directory "
                f"named {FLAG_DIR_NAME!r}; upstream mounts the flag directory "
                "into these services, so either the mount shape changed or "
                "this transform is now silently leaving the shared writable "
                "bind in place"
            )
        return changed

    if not vendored.is_dir():
        raise AstronomyShopError(
            f"the vendored flag directory {vendored} is missing; a bind to a "
            "path that does not exist would be created as an empty directory "
            "and flagd would start with no flags defined"
        )

    private = Path(tempfile.mkdtemp(prefix="radius-eval-flagd-"))
    shutil.copytree(vendored, private, dirs_exist_ok=True)

    for name in mounted:
        service = config["services"][name]
        volumes = service.get("volumes") or []
        for index, mount in enumerate(volumes):
            source, target = _bind_source(mount)
            if source is None or Path(source).name != FLAG_DIR_NAME:
                continue
            replacement = {
                "type": "bind",
                "source": str(private),
                "target": target,
            }
            if name == FLAGD_SERVICE:
                replacement["read_only"] = True
            volumes[index] = replacement
            changed.append(name)
    return changed


def _apply_cpu_limits(config: dict, host_class: str) -> list[str]:
    """Give every service a fitted CPU limit.

    Upstream sets ``deploy.resources.limits.memory`` on all 28 services and
    ``cpus`` on none, so the services compete freely for the host's cores and
    every measurement sits on top of that contention.

    Every service gets one, not just the heavy ones. A CPU-limit incident
    changes a service's limit, and if only the faulted service carried a limit
    its mere presence would announce which service was faulted. Uniform limits
    keep the fault where it belongs, in the value rather than the shape.

    The numbers come from ``cpu-limits.json``, fitted from measurement. A
    service missing from that manifest is an error rather than a service left
    unlimited, because an unlimited service is precisely the variance this
    removes.

    The host class is observed here rather than accepted from a caller, and
    the loader refuses limits fitted on a different one. Without that, a stack
    rendered on a two-core VM would silently receive a ten-core laptop's
    quotas, and every service would look unthrottled because no quota bound.
    """
    from .cpu_limits import CpuLimitError, load_fitted_limits

    repo_root = _repo_root_from_config(config)
    limits = load_fitted_limits(repo_root, host_class)["limitCores"]
    changed: list[str] = []
    unfitted: list[str] = []
    for name, spec in (config.get("services") or {}).items():
        if name not in limits:
            unfitted.append(name)
            continue
        deploy = spec.setdefault("deploy", {})
        resources = deploy.setdefault("resources", {})
        service_limits = resources.setdefault("limits", {})
        service_limits["cpus"] = str(limits[name])
        changed.append(name)
    if unfitted:
        raise CpuLimitError(
            "no fitted CPU limit for "
            + ", ".join(sorted(unfitted))
            + "; refit rather than leaving a service unlimited"
        )
    return changed


def _repo_root_from_config(config: dict) -> "Path":
    """Locate the repository from this module, not from the caller.

    The config is rendered in a temporary directory, so deriving the root from
    it would be unreliable.
    """
    return Path(__file__).resolve().parents[2]


TRANSFORMS: tuple[Transform, ...] = (
    Transform(
        name="isolate-flag-file",
        rationale=(
            "Upstream binds the vendored src/flagd directory into flagd and "
            "into flagd-ui, and flagd-ui mounts it read-write because editing "
            "flags is what that interface does. A flag toggled during a trial "
            "therefore rewrites a file committed to this repository: the "
            "working tree goes dirty, the fixture hash stops describing what "
            "is on disk, and because a bind is not a volume the edit survives "
            "`down --volumes` and seeds the next trial with the previous "
            "trial's flag state. Two stacks rendered from one checkout would "
            "also share the single file. Each render gets its own copy "
            "instead, and flagd's is read-only since flagd only reads."
        ),
        apply=_isolate_flag_file,
    ),
    Transform(
        name="apply-cpu-limits",
        rationale=(
            "Upstream declares a memory limit on every service and a CPU "
            "limit on none, so 28 services contend for the host's cores "
            "underneath every measurement. Limits are fitted from a measured "
            "healthy peak and applied to all 28, including the ones far below "
            "the floor: a CPU-limit incident changes a value, and if only the "
            "faulted service carried a limit, the shape of the file would "
            "give away which service was faulted before the agent looked at "
            "any telemetry. The fitted numbers are a starting point; the "
            "acceptance test is that the kernel reports no throttling during "
            "the measurement window."
        ),
        apply=_apply_cpu_limits,
        needs_host_class=True,
    ),
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


def apply_transforms(
    config: dict[str, Any], host_class: str | None = None
) -> TransformReport:
    """Apply every declared transform in order, recording what each touched.

    A transform that changes nothing is reported as changing nothing rather
    than skipped silently. An upstream bump that removes the Docker socket
    mount on its own should show up as an empty list here, not as a transform
    that quietly became a no-op while still claiming to protect something.

    ``host_class`` decides which fitted CPU limits may be applied. Left as
    ``None`` it is *observed* from this machine, which is what every
    production caller does; it is a parameter only so the transforms can be
    exercised without a Docker daemon. Passing one does not weaken the check,
    because the loader still refuses any class the committed limits were not
    fitted on.
    """
    if host_class is None:
        from .hostclass import derive_class_id, observe_host

        host_class = derive_class_id(observe_host())
    report = TransformReport()
    for transform in TRANSFORMS:
        report.applied[transform.name] = (
            transform.apply(config, host_class)
            if transform.needs_host_class
            else transform.apply(config)
        )
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



# ---------------------------------------------------------------------------
# Environment checks for the shop
# ---------------------------------------------------------------------------

#: Services whose egress we would have to justify if the shop ran on an
#: ``internal: true`` network. Recorded, deliberately unused: see
#: ``shop_check_plan`` for why nothing here suppresses a check.
KNOWN_EGRESS_GAP = "all services share the routing default bridge network"


def shop_check_plan(config_text: str, readiness_probes=()):
    """Generate the shop's environment checks from its own compose file.

    Nothing is enumerated here and nothing is suppressed. Two families of
    problem are expected on the vendored 3.1.0 stack and are left failing on
    purpose, because a suppressed check is indistinguishable from a passing
    one at sign-off:

    * every service declares ``deploy.resources.limits.memory`` and no
      ``cpus``, so no service has a complete resource limit;
    * every service sits on the routing default bridge, so no service can be
      shown to have no egress.

    Both are upstream properties, not driver defects, and both are decisions
    about the fixture rather than about this module. Passing an
    ``egress_exceptions`` mapping here would make the second family disappear
    from sign-off while changing nothing about the environment, which is the
    failure mode these generated checks exist to prevent.
    """
    from . import checks as _checks

    model = _checks.parse_compose_config(config_text)
    return _checks.generate_check_plan(model, readiness_probes=readiness_probes)


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
    "KNOWN_EGRESS_GAP",
    "apply_transforms",
    "shop_check_plan",
    "compose_file_paths",
    "declared_flags",
    "default_off_flags",
    "floating_references",
    "image_references",
    "upstream_dir",
]
