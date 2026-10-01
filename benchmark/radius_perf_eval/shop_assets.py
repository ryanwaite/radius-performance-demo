"""Pinned startup assets for an offline Astronomy Shop."""

from __future__ import annotations

import ast
import hashlib
import json
import re
import stat
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any

PLUGIN_ID = "grafana-opensearch-datasource"
PLUGIN_VERSION = "2.34.4"
PLATFORMS = ("linux_amd64", "linux_arm64")
DATASOURCE_SOURCE = "upstream/src/grafana/provisioning/datasources/opensearch.yaml"
DATASOURCE_DERIVED = "derived/grafana/opensearch.yaml"


class AssetError(ValueError):
    """An offline startup asset is missing, altered, or unsafe."""


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def derive_load_script(source: str) -> str:
    """Remove only the task calling the service absent from Compose."""
    tree = ast.parse(source)
    classes = [node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == "WebsiteUser"]
    methods = [
        node for cls in classes for node in cls.body
        if isinstance(node, ast.FunctionDef) and node.name == "ask_agent"
    ]
    if len(methods) != 1 or not methods[0].decorator_list:
        raise AssetError("expected exactly one decorated WebsiteUser.ask_agent task")
    method = methods[0]
    start = min(node.lineno for node in method.decorator_list) - 1
    lines = source.splitlines(keepends=True)
    derived = "".join(lines[:start] + lines[method.end_lineno:])
    ast.parse(derived)
    return derived


def asset_root(repo_root: Path) -> Path:
    return repo_root / "benchmark/apps/astronomy-shop"


def derive_opensearch_datasource(source: str) -> str:
    """Use concrete daily indices for plugin health and Lucene queries."""
    original = "      database: otel-logs-*\n"
    if source.count(original) != 1 or re.search(r"^\s*interval:", source, re.M):
        raise AssetError("expected one wildcard database and no existing index interval")
    return source.replace(original, "      database: '[otel-logs-]YYYY-MM-DD'\n      interval: daily\n")


def derive_proxy_template(source: str) -> str:
    """Block public access to hidden flags and mutable harness controls."""
    protected = ("/loadgen", "/opamp", "/flagservice/", "/feature")
    pattern = r"^                        - match:.*?(?=^                        - match:|^                http_filters:)"
    blocks = list(re.finditer(pattern, source, re.M | re.S))
    blocked = set()
    for match in reversed(blocks):
        first = match.group().splitlines()[0]
        for prefix in protected:
            if f'"{prefix}' in first:
                blocked.add(prefix)
                replacement = first + "\n                          direct_response: { status: 404 }\n"
                source = source[:match.start()] + replacement + source[match.end():]
                break
    if blocked != set(protected):
        raise AssetError("proxy template is missing a protected route")
    source, count = re.subn(
        r"^    - name: (?:flagservice|flagd-ui|loadgen|opamp)\n.*?(?=^    - name:|^admin:)",
        "", source, flags=re.M | re.S,
    )
    if count != 4:
        raise AssetError("proxy template is missing a protected cluster")
    return source


def verify_assets(repo_root: Path) -> dict[str, Any]:
    root = asset_root(repo_root)
    manifest = json.loads((root / "startup-assets.json").read_text())
    files = manifest.get("files")
    if not isinstance(files, dict) or not files:
        raise AssetError("startup manifest has no files")
    required = {
        "upstream/.env", "upstream/src/load-generator/locustfile.py",
        "upstream/src/load-generator/Dockerfile", "derived/load-generator/locustfile.py",
        "upstream/src/frontend-proxy/Dockerfile", "upstream/src/frontend-proxy/envoy.tmpl.yaml",
        "derived/frontend-proxy/envoy.tmpl.yaml",
        DATASOURCE_SOURCE, DATASOURCE_DERIVED,
        *(f"plugins/{PLUGIN_ID}-{PLUGIN_VERSION}.{platform}.zip" for platform in PLATFORMS),
    }
    if set(files) != required:
        raise AssetError("startup manifest does not cover the required asset inventory")
    image_manifest = json.loads((root / "image-digests.json").read_text())
    expected_link = {
        "manifest": "startup-assets.json",
        "sha256": digest((root / "startup-assets.json").read_bytes()),
        "plugins": {name: entry for name, entry in files.items() if name.startswith("plugins/")},
    }
    if image_manifest.get("startupAssets") != expected_link:
        raise AssetError("image manifest does not match the pinned startup assets")
    for name, entry in files.items():
        path = root / name
        if not path.is_file() or digest(path.read_bytes()) != entry.get("sha256"):
            raise AssetError(f"startup asset missing or hash mismatch: {name}")
    upstream = (root / "upstream/src/load-generator/locustfile.py").read_text()
    if (root / "derived/load-generator/locustfile.py").read_text() != derive_load_script(upstream):
        raise AssetError("derived load script differs from the declared task removal")
    proxy = (root / "upstream/src/frontend-proxy/envoy.tmpl.yaml").read_text()
    if (root / "derived/frontend-proxy/envoy.tmpl.yaml").read_text() != derive_proxy_template(proxy):
        raise AssetError("derived proxy template differs from the declared route restrictions")
    datasource = (root / DATASOURCE_SOURCE).read_text()
    if (root / DATASOURCE_DERIVED).read_text() != derive_opensearch_datasource(datasource):
        raise AssetError("derived OpenSearch datasource differs from the declared daily index pattern")
    return manifest


def verify_collector_assets(repo_root: Path) -> str:
    root = asset_root(repo_root)
    path = root / "derived/collector-config-manifest.json"
    manifest = json.loads(path.read_text())
    recorded_hash = manifest.pop("manifestHash", None)
    if digest(json.dumps(manifest, sort_keys=True).encode()) != recorded_hash:
        raise AssetError("collector manifest hash mismatch")
    expected = {"otelcol-config.yml", "otelcol-config-full.yml",
                "otelcol-config-observability.yml", "otelcol-config-extras.yml"}
    if set(manifest.get("files", {})) != expected:
        raise AssetError("collector manifest inventory is incomplete")
    for name, entry in manifest["files"].items():
        for directory, key in (("upstream/src/otel-collector", "sourceSha256"),
                               ("derived/otel-collector", "sha256")):
            if digest((root / directory / name).read_bytes()) != "sha256:" + entry[key]:
                raise AssetError(f"collector asset hash mismatch: {directory}/{name}")
    return recorded_hash


def grafana_inventory(repo_root: Path, flags: set[str]) -> dict[str, Any]:
    """Inventory possible answer leakage without modifying upstream dashboards."""
    root = asset_root(repo_root)
    source = root / "upstream/src/grafana"
    paths = sorted(path for path in source.rglob("*") if path.is_file())
    paths.append(root / DATASOURCE_DERIVED)
    if not flags or not any(path.suffix == ".json" for path in paths):
        raise AssetError("Grafana leakage inventory requires flags and dashboard files")
    terms = sorted(flags | {"flagd"})
    files = []
    matches = []
    for path in paths:
        text = path.read_text()
        name = str(path.relative_to(root))
        files.append({"path": name, "sha256": digest(path.read_bytes()), "lines": len(text.splitlines())})
        for number, line in enumerate(text.splitlines(), 1):
            found = [term for term in terms if term.lower() in line.lower()]
            if found:
                matches.append({"path": name, "line": number, "terms": found, "text": line.strip()})
    return {"files": files, "searchedTerms": terms, "matches": matches,
            "scope": "literal flag names and flagd in Grafana source and derived datasource; not a sealed-fixture verdict"}


def extract_plugin(archive_path: Path, destination: Path) -> Path:
    """Extract a verified archive, preserving its signed files and backend mode."""
    with zipfile.ZipFile(archive_path) as archive:
        members = archive.infolist()
        names = {entry.filename for entry in members}
        required = {f"{PLUGIN_ID}/{name}" for name in ("LICENSE", "MANIFEST.txt", "plugin.json")}
        if len(names) != len(members) or not required <= names:
            raise AssetError("plugin archive has duplicate entries or lacks license/signature/metadata")
        license_text = archive.read(f"{PLUGIN_ID}/LICENSE").decode()
        if "Apache License" not in license_text or "Version 2.0" not in license_text:
            raise AssetError("plugin archive does not carry the reviewed Apache-2.0 license")
        metadata = json.loads(archive.read(f"{PLUGIN_ID}/plugin.json"))
        if metadata.get("id") != PLUGIN_ID or metadata.get("info", {}).get("version") != PLUGIN_VERSION:
            raise AssetError("plugin metadata does not match the declared version")
        for entry in members:
            parts = PurePosixPath(entry.filename).parts
            if (
                not parts or parts[0] != PLUGIN_ID or ".." in parts
                or "\\" in entry.filename or ":" in entry.filename
                or stat.S_ISLNK(entry.external_attr >> 16)
            ):
                raise AssetError(f"unsafe plugin archive path: {entry.filename}")
        destination.mkdir(parents=True, exist_ok=False)
        for entry in members:
            target = destination / entry.filename
            if entry.is_dir():
                target.mkdir(parents=True, exist_ok=True)
            else:
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(archive.read(entry))
                target.chmod(0o755 if target.name.startswith("gpx_") else 0o644)
    return destination / PLUGIN_ID


def mount_assets(config: dict[str, Any], repo_root: Path, runtime_dir: Path, architecture: str) -> dict[str, Any]:
    manifest = verify_assets(repo_root)
    platform = {"amd64": "linux_amd64", "x86_64": "linux_amd64", "arm64": "linux_arm64", "aarch64": "linux_arm64"}.get(architecture)
    if platform is None:
        raise AssetError(f"no pinned plugin for Docker architecture {architecture!r}")
    services = config["services"]
    if not {"grafana", "load-generator", "frontend-proxy"} <= services.keys():
        raise AssetError("offline assets require Grafana, the load generator, and the proxy")
    root = asset_root(repo_root)
    archive = root / f"plugins/{PLUGIN_ID}-{PLUGIN_VERSION}.{platform}.zip"
    plugin = extract_plugin(archive, runtime_dir / "plugins")
    for service, source, target in (
        ("grafana", plugin, f"/var/lib/grafana/plugins/{PLUGIN_ID}"),
        ("grafana", root / DATASOURCE_DERIVED, "/etc/grafana/provisioning/datasources/opensearch.yaml"),
        ("load-generator", root / "derived/load-generator/locustfile.py", "/usr/src/app/locustfile.py"),
        ("frontend-proxy", root / "derived/frontend-proxy/envoy.tmpl.yaml", "/home/envoy/envoy.tmpl.yaml"),
    ):
        volumes = services[service].setdefault("volumes", [])
        if any(volume.get("target") == target for volume in volumes):
            raise AssetError(f"duplicate startup asset mount: {target}")
        volumes.append({"type": "bind", "source": str(source.resolve()), "target": target,
                        "read_only": True, "bind": {"create_host_path": False}})
    env = services["grafana"].setdefault("environment", {})
    env.pop("GF_INSTALL_PLUGINS", None)
    env.update({
        "GF_PLUGINS_PREINSTALL_DISABLED": "true",
        "GF_ANALYTICS_CHECK_FOR_UPDATES": "false",
        "GF_ANALYTICS_CHECK_FOR_PLUGIN_UPDATES": "false",
        "GF_ANALYTICS_REPORTING_ENABLED": "false",
    })
    return {"manifestHash": digest((root / "startup-assets.json").read_bytes()),
            "plugin": manifest["files"][str(archive.relative_to(root))],
            "platform": platform}
