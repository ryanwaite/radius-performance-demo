"""What kind of machine is this, and may we give a verdict on it?

Tolerances are fitted by measuring one host and are only meaningful on hosts
of that kind. A bound fitted on a laptop says nothing about a cloud virtual
machine with different core counts, a different memory envelope and no
hypervisor in the middle. Applying one to the other produces a verdict that
looks identical to a real one and means nothing.

So the driver observes the machine it is running on, derives a host class from
what it observed, and looks the tolerances up by that class. A host class with
no frozen tolerances gets no verdict at all, and that refusal is a failure
rather than a skip -- see ``resolve_tolerances``.

Nothing here accepts a caller-supplied label. A caller that could name its own
host class could name one that has tolerances, which would defeat the whole
mechanism: the check would pass because it was told the right answer rather
than because the machine was the right machine.

Two identifiers are derived, and the distinction matters:

``class_id``
    The performance envelope: operating system family, architecture, CPU
    model, core count, memory, and the container runtime's own CPU and memory
    envelope. This is what tolerances are keyed on, because these are the
    facts that determine how fast the thing under test can possibly run.

``fingerprint``
    Everything in the class plus the patch-level versions: operating system
    release, Docker engine version, Python version. This is recorded and
    compared but does not select tolerances.

The split exists because the two failure modes are not symmetric. Applying
laptop bounds to a virtual machine is a silent wrong answer, so the class must
be strict about the envelope. Refusing a verdict because Docker Desktop
auto-updated its patch version is a false alarm, and a mechanism that cries
wolf on every background update gets routed around within a week. Patch drift
is therefore reported, loudly, rather than treated as a different machine.

That choice is backed by measurement rather than convenience. Between the
merged ten-cycle holdout and a later three-cycle check the Docker engine moved
29.7.2 to 29.8.0 and Python moved 3.12.13 to 3.12.14, while incident
throughput stayed at 7.886 rps against the holdout's 7.914-7.829 and healthy
throughput at 274.5 rps against 254.9-286.9. The envelope did not move, and
neither did the numbers.
"""

from __future__ import annotations

import platform
import re
import subprocess
from dataclasses import asdict, dataclass
from typing import Any

__all__ = [
    "HostFacts",
    "HostClassError",
    "observe_host",
    "derive_class_id",
    "derive_fingerprint",
    "gibibytes",
]


class HostClassError(RuntimeError):
    """Raised when the host cannot be observed well enough to be classified."""


def _run(command: list[str], timeout: float = 15.0) -> str | None:
    """Run a command and return stdout, or None if it failed in any way.

    Every individual fact is allowed to be unavailable. A fact that is missing
    becomes a missing field, and a missing field required by the class makes
    the host unclassifiable -- which is a refusal to give a verdict, not a
    silent default. Defaulting here would be the dangerous option: an unknown
    core count quietly becoming 0 would produce a stable, wrong class id.
    """
    try:
        completed = subprocess.run(
            command, capture_output=True, text=True, timeout=timeout, check=False
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if completed.returncode != 0:
        return None
    return completed.stdout.strip()


def _int_or_none(text: str | None) -> int | None:
    if text is None:
        return None
    try:
        return int(text.strip())
    except ValueError:
        return None


def gibibytes(byte_count: int | None) -> float | None:
    """Bytes as GiB, rounded to one decimal, or None."""
    if byte_count is None:
        return None
    return round(byte_count / (1024**3), 1)


@dataclass(frozen=True)
class HostFacts:
    """Facts the driver read off the machine it is running on.

    Every field is populated by observation. None means the fact could not be
    read, which is deliberately distinct from a zero or an empty string: an
    unread core count must not be confusable with a machine that reported zero
    cores, because the first is a reason to refuse and the second is a reason
    to disbelieve the reading.
    """

    os_name: str | None = None
    os_release: str | None = None
    arch: str | None = None
    cpu_model: str | None = None
    cpu_cores: int | None = None
    memory_bytes: int | None = None

    docker_engine_version: str | None = None
    docker_operating_system: str | None = None
    docker_kernel: str | None = None
    docker_arch: str | None = None
    docker_cpus: int | None = None
    docker_memory_bytes: int | None = None
    docker_virtualized: bool | None = None
    docker_virtualization_evidence: str | None = None

    python_version: str | None = None

    unreadable: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["unreadable"] = list(self.unreadable)
        payload["memoryGib"] = gibibytes(self.memory_bytes)
        payload["dockerMemoryGib"] = gibibytes(self.docker_memory_bytes)
        return payload


# Facts without which a host cannot be classified at all. The container
# runtime's envelope is in here because on macOS the containers do not get the
# laptop's 32 GiB; they get whatever the Docker Desktop virtual machine was
# given, which was 7.7 GiB on the machine this was written on. Classifying by
# the laptop's memory would describe a resource the workload cannot reach.
CLASS_REQUIRED_FIELDS: tuple[str, ...] = (
    "os_name",
    "arch",
    "cpu_model",
    "cpu_cores",
    "memory_bytes",
    "docker_cpus",
    "docker_memory_bytes",
    "docker_virtualized",
)


def _slug(text: str) -> str:
    """Lowercase, collapse anything that is not alphanumeric to a hyphen."""
    return re.sub(r"-+", "-", re.sub(r"[^a-z0-9]+", "-", text.lower())).strip("-")


def _observe_macos(facts: dict[str, Any]) -> None:
    facts["cpu_model"] = _run(["sysctl", "-n", "machdep.cpu.brand_string"])
    facts["cpu_cores"] = _int_or_none(_run(["sysctl", "-n", "hw.logicalcpu"]))
    facts["memory_bytes"] = _int_or_none(_run(["sysctl", "-n", "hw.memsize"]))


def _observe_linux(facts: dict[str, Any]) -> None:
    model = None
    cores = 0
    try:
        with open("/proc/cpuinfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("processor"):
                    cores += 1
                elif model is None and line.split(":")[0].strip() in {
                    "model name",
                    "Model",
                    "Hardware",
                }:
                    model = line.split(":", 1)[1].strip()
    except OSError:
        pass
    facts["cpu_model"] = model
    facts["cpu_cores"] = cores or None
    try:
        with open("/proc/meminfo", encoding="utf-8") as handle:
            for line in handle:
                if line.startswith("MemTotal:"):
                    kib = _int_or_none(line.split()[1])
                    facts["memory_bytes"] = kib * 1024 if kib is not None else None
                    break
    except OSError:
        pass


# Substrings in the container runtime's reported kernel or operating system
# that identify a hypervisor between the engine and the metal. `linuxkit` is
# the Docker Desktop virtual machine on macOS and Windows; the others cover
# the common cloud and desktop hypervisors.
_VIRTUALIZATION_MARKERS: tuple[str, ...] = (
    "linuxkit",
    "docker desktop",
    "microsoft",
    "wsl",
    "hyperv",
    "hypervisor",
)


def _detect_virtualization(
    docker_os: str | None, docker_kernel: str | None, host_os: str | None
) -> tuple[bool | None, str | None]:
    """Is there a virtual machine between the Docker engine and the hardware?

    This matters because it is the single largest structural difference
    between the laptop this was fitted on and the Linux cloud instances the
    scored campaign will run on. On macOS every container runs inside a
    virtual machine with its own CPU and memory allocation and its own disk
    layer; on a Linux virtual machine the engine runs on the guest kernel
    directly. Those are not the same performance envelope and must not share
    a frozen tolerance set.

    Evidence is returned alongside the verdict so the report can say why,
    rather than asserting a boolean nobody can check.
    """
    haystack = " ".join(part for part in (docker_os, docker_kernel) if part).lower()
    if not haystack:
        return None, None
    for marker in _VIRTUALIZATION_MARKERS:
        if marker in haystack:
            return True, f"container runtime reports {marker!r}"
    # A Linux engine on a Linux host with no hypervisor marker is the
    # not-virtualized case. On macOS or Windows a virtual machine is always
    # present even if it did not advertise itself, so an absent marker there
    # is unknown rather than false.
    if host_os == "Linux":
        return False, "engine runs on the host kernel with no hypervisor marker"
    return None, "no hypervisor marker found, but a non-Linux host implies one"


def observe_host(docker_info: dict[str, str] | None = None) -> HostFacts:
    """Read the machine. ``docker_info`` is injectable so tests need no daemon.

    A daemon that cannot be reached leaves the container-runtime fields unread
    rather than raising. The consequence lands in ``derive_class_id``, which
    refuses to classify, which refuses the verdict. That path is worth keeping
    open precisely so it can be tested.
    """
    facts: dict[str, Any] = {}
    unreadable: list[str] = []

    facts["os_name"] = platform.system() or None
    facts["os_release"] = platform.release() or None
    facts["arch"] = platform.machine() or None
    facts["python_version"] = platform.python_version()

    if facts["os_name"] == "Darwin":
        _observe_macos(facts)
    elif facts["os_name"] == "Linux":
        _observe_linux(facts)

    if docker_info is None:
        docker_info = _docker_info()

    if docker_info:
        facts["docker_engine_version"] = docker_info.get("ServerVersion") or None
        facts["docker_operating_system"] = docker_info.get("OperatingSystem") or None
        facts["docker_kernel"] = docker_info.get("KernelVersion") or None
        facts["docker_arch"] = docker_info.get("Architecture") or None
        facts["docker_cpus"] = _int_or_none(docker_info.get("NCPU"))
        facts["docker_memory_bytes"] = _int_or_none(docker_info.get("MemTotal"))
    else:
        unreadable.append("docker")

    virtualized, evidence = _detect_virtualization(
        facts.get("docker_operating_system"),
        facts.get("docker_kernel"),
        facts.get("os_name"),
    )
    facts["docker_virtualized"] = virtualized
    facts["docker_virtualization_evidence"] = evidence

    for field_name in CLASS_REQUIRED_FIELDS:
        if facts.get(field_name) is None and field_name != "docker_virtualized":
            if field_name not in unreadable:
                unreadable.append(field_name)
    if facts.get("docker_virtualized") is None and "docker_virtualized" not in unreadable:
        unreadable.append("docker_virtualized")

    facts["unreadable"] = tuple(unreadable)
    return HostFacts(**facts)


def _docker_info() -> dict[str, str] | None:
    """Ask the daemon for its own view of the machine.

    Deliberately a plain `docker info` rather than the driver's own helper:
    this runs before any project exists and must not depend on the rest of the
    driver being wired up.
    """
    fields = (
        "ServerVersion",
        "OperatingSystem",
        "KernelVersion",
        "Architecture",
        "NCPU",
        "MemTotal",
    )
    template = "|".join("{{." + name + "}}" for name in fields)
    output = _run(["docker", "info", "--format", template])
    if output is None:
        return None
    parts = output.split("|")
    if len(parts) != len(fields):
        return None
    return dict(zip(fields, (part.strip() for part in parts)))


def derive_class_id(facts: HostFacts) -> str:
    """The performance envelope, as a stable identifier.

    Raises rather than returning a placeholder when a required fact is
    missing. A placeholder class id would be stable and meaningless, and would
    match a frozen tolerance set if anyone ever froze one against it.
    """
    missing = [
        name for name in CLASS_REQUIRED_FIELDS if getattr(facts, name, None) is None
    ]
    if missing:
        raise HostClassError(
            "cannot classify this host; unreadable facts: " + ", ".join(sorted(missing))
        )

    runtime = "vm" if facts.docker_virtualized else "native"
    return "-".join(
        (
            _slug(facts.os_name or ""),
            _slug(facts.arch or ""),
            _slug(facts.cpu_model or ""),
            f"{facts.cpu_cores}c",
            f"{gibibytes(facts.memory_bytes)}gib",
            f"docker-{runtime}",
            f"{facts.docker_cpus}c",
            f"{gibibytes(facts.docker_memory_bytes)}gib",
        )
    )


def derive_fingerprint(facts: HostFacts) -> str:
    """The class plus every patch-level version, for drift reporting.

    Unlike the class this never raises: a fingerprint is only ever compared
    and displayed, so a partial one is still useful, whereas a partial class
    would silently select the wrong tolerances.
    """
    try:
        base = derive_class_id(facts)
    except HostClassError:
        base = "unclassified"
    return "/".join(
        (
            base,
            f"os{facts.os_release or '?'}",
            f"docker{facts.docker_engine_version or '?'}",
            f"py{facts.python_version or '?'}",
        )
    )
