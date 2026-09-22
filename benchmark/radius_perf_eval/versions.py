"""Version and provenance capture for a trial.

The experiment plan requires every run to record the pinned Copilot SDK
version, Copilot CLI/runtime version, runtime/OS, and model id so results stay
attributable and reproducible (``docs/specs/copilot-radius-experiment-plan.md``,
"Reproducibility record" and "Artifacts and run record").

Two CLI versions matter and are recorded separately:

* ``pinnedCliVersion`` -- the runtime version the installed SDK pins and
  downloads. This is the version that actually serves the session.
* ``hostCliVersion`` -- whatever ``copilot`` happens to be on ``PATH``. It is
  recorded for provenance only and is *not* what drives the session unless
  ``COPILOT_CLI_PATH`` is set.

Conflating the two would silently misattribute a result to the wrong runtime.
"""

from __future__ import annotations

import hashlib
import json
import platform
import shutil
import subprocess
import sys
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "PROHIBITED_REGISTRY_HOSTS",
    "ProvenanceRecord",
    "capture_provenance",
    "host_cli_version",
    "package_supply_chain",
    "pinned_distributions",
    "sdk_versions",
    "sha256_of_json",
]

#: Public registries that must not appear in committed configuration or
#: lockfiles. See the plan's "Package supply chain compliance" section,
#: "Prohibited patterns".
PROHIBITED_REGISTRY_HOSTS: tuple[str, ...] = (
    "pypi.org",
    "files.pythonhosted.org",
    "registry.npmjs.org",
)

#: Distributions whose CFS-resolved versions are recorded at freeze time.
PINNED_DISTRIBUTIONS: tuple[str, ...] = (
    "github-copilot-sdk",
    "inspect-ai",
    "pytest",
)


def sha256_of_json(payload: Any) -> str:
    """Stable content digest for a JSON-serializable artifact."""
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _package_version(name: str) -> str | None:
    try:
        from importlib.metadata import PackageNotFoundError, version
    except ImportError:  # pragma: no cover - Python < 3.8 only
        return None
    try:
        return version(name)
    except PackageNotFoundError:
        return None


def sdk_versions() -> dict[str, Any]:
    """Versions of the installed SDK and the runtime it pins."""
    pinned_cli: str | None = None
    protocol: int | None = None
    try:
        from copilot._cli_version import CLI_VERSION

        pinned_cli = CLI_VERSION
    except Exception:  # pragma: no cover - defensive
        pinned_cli = None
    try:
        from copilot._sdk_protocol_version import SDK_PROTOCOL_VERSION

        protocol = SDK_PROTOCOL_VERSION
    except Exception:  # pragma: no cover - defensive
        protocol = None

    return {
        "sdkPackage": "github-copilot-sdk",
        "sdkVersion": _package_version("github-copilot-sdk"),
        "sdkProtocolVersion": protocol,
        "pinnedCliVersion": pinned_cli,
        "inspectAiVersion": _package_version("inspect-ai"),
    }


def host_cli_version() -> dict[str, Any]:
    """Version of any ``copilot`` binary on ``PATH``.

    Recorded for provenance only. Unless ``COPILOT_CLI_PATH`` points at it,
    this binary does not serve the session.
    """
    path = shutil.which("copilot")
    if path is None:
        return {"hostCliPath": None, "hostCliVersion": None}
    try:
        completed = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        raw = (completed.stdout or completed.stderr or "").strip()
    except Exception as exc:  # pragma: no cover - defensive
        return {"hostCliPath": path, "hostCliVersion": None, "hostCliError": repr(exc)}

    version_token: str | None = None
    for token in raw.replace("\n", " ").split():
        stripped = token.strip().rstrip(".")
        if stripped and stripped[0].isdigit():
            version_token = stripped
            break
    return {"hostCliPath": path, "hostCliVersion": version_token, "hostCliRaw": raw}


def pinned_distributions(
    names: Sequence[str] = PINNED_DISTRIBUTIONS,
) -> dict[str, str | None]:
    """Resolved versions of the pinned distributions, as actually installed.

    The plan requires recording CFS-resolved versions at freeze time, because
    quarantine lag makes a public-registry listing an unreliable pin.
    """
    return {name: _package_version(name) for name in names}


def _package_root() -> Path:
    return Path(__file__).resolve().parent.parent


def _scan_for_prohibited_registries(path: Path) -> dict[str, Any]:
    """Check one file for references to a public package registry."""
    if not path.is_file():
        return {"path": str(path), "present": False, "hits": None}
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:  # pragma: no cover - defensive
        return {"path": str(path), "present": True, "error": repr(exc), "hits": None}
    hits = sorted(host for host in PROHIBITED_REGISTRY_HOSTS if host in text)
    return {"path": str(path), "present": True, "hits": hits}


def _configured_uv_indexes(pyproject: Path) -> list[dict[str, Any]] | None:
    if not pyproject.is_file():
        return None
    try:
        import tomllib

        data = tomllib.loads(pyproject.read_text(encoding="utf-8"))
    except Exception:  # pragma: no cover - defensive
        return None
    indexes = data.get("tool", {}).get("uv", {}).get("index")
    if not isinstance(indexes, list):
        return []
    return [dict(entry) for entry in indexes if isinstance(entry, dict)]


def package_supply_chain(root: Path | None = None) -> dict[str, Any]:
    """Record and verify the committed package-source configuration.

    Compliance is captured as a *verified property* of the committed files
    rather than an assertion: the configured indexes are read back from
    ``pyproject.toml`` and the lockfile is scanned for the prohibited public
    registries listed in the plan.
    """
    base = root if root is not None else _package_root()
    pyproject = base / "pyproject.toml"
    lockfile = base / "uv.lock"

    indexes = _configured_uv_indexes(pyproject)
    scans = [
        _scan_for_prohibited_registries(pyproject),
        _scan_for_prohibited_registries(lockfile),
    ]
    scanned = [s for s in scans if s.get("hits") is not None]
    violations = sorted({host for s in scanned for host in s["hits"]})

    index_urls = [i.get("url") for i in indexes] if indexes is not None else []
    return {
        "policy": "microsoft-central-feed-services",
        "planSection": "Package supply chain compliance",
        "configuredIndexes": indexes,
        "indexUrls": index_urls,
        "singleIndex": len(index_urls) == 1,
        "resolvedVersions": pinned_distributions(),
        "prohibitedRegistryHosts": list(PROHIBITED_REGISTRY_HOSTS),
        "scannedFiles": scans,
        "prohibitedRegistryReferences": violations,
        "compliant": (
            len(index_urls) == 1 and not violations and bool(scanned)
        ),
    }


@dataclass
class ProvenanceRecord:
    """Immutable description of what produced a run."""

    captured_at: str
    model: str | None
    reasoning_effort: str | None
    versions: dict[str, Any] = field(default_factory=dict)
    runtime: dict[str, Any] = field(default_factory=dict)
    session: dict[str, Any] = field(default_factory=dict)
    supply_chain: dict[str, Any] = field(default_factory=dict)

    def to_json_dict(self) -> dict[str, Any]:
        payload = {
            "capturedAt": self.captured_at,
            "model": self.model,
            "reasoningEffort": self.reasoning_effort,
            "versions": self.versions,
            "runtime": self.runtime,
            "session": self.session,
            "supplyChain": self.supply_chain,
        }
        payload["provenanceDigest"] = sha256_of_json(payload)
        return payload


def capture_provenance(
    *,
    model: str | None = None,
    reasoning_effort: str | None = None,
    session: dict[str, Any] | None = None,
) -> ProvenanceRecord:
    """Capture SDK, CLI, runtime, OS, and model provenance for a trial."""
    versions = sdk_versions()
    versions.update(host_cli_version())

    runtime = {
        "pythonVersion": sys.version.split()[0],
        "pythonImplementation": platform.python_implementation(),
        "platform": platform.platform(),
        "system": platform.system(),
        "release": platform.release(),
        "machine": platform.machine(),
        "processor": platform.processor(),
    }

    return ProvenanceRecord(
        captured_at=datetime.now(timezone.utc).isoformat(),
        model=model,
        reasoning_effort=reasoning_effort,
        versions=versions,
        runtime=runtime,
        session=session or {},
        supply_chain=package_supply_chain(),
    )
