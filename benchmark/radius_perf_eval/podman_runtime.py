"""Read-only Podman inventory, deliberately separate from host qualification."""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
from typing import Any


class RuntimePolicyError(RuntimeError):
    """The requested operation lacks a migrated, verified implementation."""


def require_podman_driver() -> None:
    raise RuntimePolicyError(
        "Live benchmark execution is blocked pending the Podman driver migration. "
        "Docker fallback is prohibited. Use doctor for read-only Podman inventory; "
        "inventory does not qualify limits, networks, cleanup, telemetry or sandboxing. "
        "See benchmark/README.md#podman-migration."
    )


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimePolicyError(message)


def engine_identity(info: Any) -> dict[str, Any]:
    """Extract stable identity, not changing counters or a qualification class."""
    require(isinstance(info, dict), "Podman info must be an object")
    host = info.get("host")
    version = info.get("version")
    require(isinstance(host, dict) and isinstance(version, dict),
            "Podman info lacks host/version objects; Docker info is not compatible evidence")
    identity = {"engine": "podman", "serverVersion": version.get("Version")}
    for key in ("arch", "os", "kernel", "cgroupVersion", "cgroupManager", "networkBackend"):
        identity[key] = host.get(key)
    for key, value in identity.items():
        require(isinstance(value, str) and bool(value.strip()), f"missing engine identity: {key}")
    for key in ("cpus", "memTotal"):
        value = host.get(key)
        require(type(value) is int and value > 0, f"invalid engine envelope: {key}")
        identity[key] = value
    security = host.get("security")
    require(isinstance(security, dict), "missing engine security context")
    rootless = security.get("rootless")
    require(type(rootless) is bool, "missing rootless/rootful context")
    identity["rootless"] = rootless
    runtime = host.get("ociRuntime")
    require(isinstance(runtime, dict), "missing OCI runtime identity")
    for key in ("name", "version"):
        value = runtime.get(key)
        require(isinstance(value, str) and bool(value.strip()), f"missing OCI runtime {key}")
        identity[f"ociRuntime{key.title()}"] = value
    return identity


def inventory(
    output: Path, *, connection: str, podman: str = "podman",
    compose_provider: Path | None = None,
) -> dict[str, Any]:
    """Capture only version/connection/info reads; never render or launch a stack.

    A new output directory retains each command result before interpreting it.
    Missing providers and unreachable engines produce incomplete inventories,
    not empty successful probes. No qualification store is read or written.
    """
    require(bool(connection.strip()) and not connection.startswith("-"),
            "select an explicit Podman connection name")
    output.mkdir(parents=True, exist_ok=False)
    record: dict[str, Any] = {
        "schemaVersion": "radius-podman-inventory-v1", "status": "incomplete",
        "eligibleForTrials": False, "connection": connection, "commands": [],
        "identity": None, "inventoryFingerprint": None, "error": None,
        "unverifiedCapabilities": [
            "compose-render-and-labels", "cgroup-probes-and-applied-limits",
            "networks-ports-and-egress", "owned-resource-cleanup",
            "telemetry-and-delivered-load", "sandbox-socket-isolation",
            "host-fit-and-qualification",
        ],
    }

    def save() -> None:
        temporary = output / "inventory.tmp"
        temporary.write_text(json.dumps(record, indent=2) + "\n")
        temporary.replace(output / "inventory.json")

    def capture(args: list[str]) -> str:
        entry: dict[str, Any] = {"args": args, "status": "started"}
        record["commands"].append(entry)
        save()
        # Do not inherit Docker endpoints or an unrelated Podman connection.
        env = {key: value for key, value in os.environ.items()
               if not key.startswith(("DOCKER_", "COMPOSE_", "CONTAINER_", "PODMAN_"))}
        env["CONTAINER_CONNECTION"] = connection
        try:
            result = subprocess.run(args, capture_output=True, text=True, timeout=30, env=env)
        except (OSError, subprocess.TimeoutExpired) as error:
            entry.update(status="failed", error=str(error))
            for key in ("stdout", "stderr"):
                value = getattr(error, key, None)
                entry[key] = value.decode("utf-8", errors="replace") if isinstance(value, bytes) else value
            save()
            raise RuntimePolicyError(f"read-only runtime probe failed: {error}") from error
        entry.update(status="finished", returncode=result.returncode,
                     stdout=result.stdout, stderr=result.stderr)
        save()
        require(result.returncode == 0, f"runtime probe failed: {' '.join(args)}")
        require(bool(result.stdout.strip()), f"runtime probe returned no evidence: {' '.join(args)}")
        return result.stdout

    save()
    try:
        executable = shutil.which(podman)
        require(executable is not None, f"Podman client not found: {podman}; owner installation required")
        client = capture([executable, "--version"]).strip()
        require(re.fullmatch(r"podman version \S+", client) is not None,
                "client did not identify itself as Podman")
        record["clientVersion"] = client
        connections = json.loads(capture([executable, "system", "connection", "list", "--format", "json"]))
        require(isinstance(connections, list), "connection inventory is not a list")
        selected = [entry for entry in connections
                    if isinstance(entry, dict) and entry.get("Name") == connection]
        require(len(selected) == 1, "selected connection must occur exactly once in the inventory")
        uri = selected[0].get("URI")
        require(isinstance(uri, str) and bool(uri.strip()), "selected connection has no endpoint URI")
        record["connectionUri"] = uri
        info = json.loads(capture([executable, "--connection", connection, "info", "--format", "json"]))
        record["engineIdentity"] = engine_identity(info)
        save()
        require(compose_provider is not None,
                "select an installed podman-compose provider explicitly; automatic provider discovery is disabled")
        require(compose_provider.is_absolute(), "Compose provider must be an absolute executable path")
        provider = shutil.which(str(compose_provider))
        require(provider is not None, f"Compose provider not executable: {compose_provider}")
        provider_version = capture([provider, "--version"]).strip()
        require(re.search(r"(?m)^podman-compose version:?\s+\S+", provider_version) is not None,
                "only an explicitly identified podman-compose provider is supported by this inventory")
        identity = {
            **record["engineIdentity"], "clientVersion": client,
            "clientPath": executable, "connection": connection, "connectionUri": uri,
            "composeProviderPath": provider, "composeProviderVersion": provider_version,
            "composeProviderSha256": hashlib.sha256(Path(provider).read_bytes()).hexdigest(),
        }
        record["identity"] = identity
        record["inventoryFingerprint"] = "podman-inventory-v1:" + hashlib.sha256(
            json.dumps(identity, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        record["status"] = "inventoried-not-qualified"
    except (RuntimePolicyError, OSError, ValueError) as error:
        record["error"] = str(error)
    finally:
        save()
    return record
