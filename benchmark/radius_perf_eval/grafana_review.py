"""Complete review surfaces, not an automated semantic-leakage verdict."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

from . import astronomy_shop
from .shop_assets import AssetError, asset_root, digest, grafana_inventory


def json_leaves(value: Any, pointer: str = "") -> list[dict[str, Any]]:
    """Preserve unknown fields, thresholds and empty containers too."""
    if isinstance(value, float) and not math.isfinite(value):
        raise AssetError(f"nonfinite Grafana JSON number at {pointer}")
    if isinstance(value, dict) and value:
        return [
            leaf
            for key, child in value.items()
            for leaf in json_leaves(child, pointer + "/" + key.replace("~", "~0").replace("/", "~1"))
        ]
    if isinstance(value, list) and value:
        return [
            leaf for index, child in enumerate(value)
            for leaf in json_leaves(child, pointer + "/" + str(index))
        ]
    return [{"pointer": pointer, "value": value}]


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise AssetError(f"duplicate Grafana JSON key: {key}")
        result[key] = value
    return result


def review_surfaces(repo_root: Path) -> dict[str, Any]:
    """Export every inventoried file without interpreting arbitrary prose."""
    inventory = grafana_inventory(repo_root, set(astronomy_shop.declared_flags(repo_root)))
    root = asset_root(repo_root)
    documents = []
    roles = set()
    for entry in inventory["files"]:
        name = entry["path"]
        data = (root / name).read_bytes()
        if digest(data) != entry["sha256"]:
            raise AssetError(f"Grafana file changed during review capture: {name}")
        text = data.decode("utf-8")
        if not text.strip():
            raise AssetError(f"empty Grafana review source: {name}")
        if name.endswith(".json"):
            dashboard = json.loads(text, object_pairs_hook=_unique_object)
            if not isinstance(dashboard, dict):
                raise AssetError(f"Grafana dashboard is not an object: {name}")
            if not isinstance(dashboard.get("title"), str) or not dashboard["title"].strip():
                raise AssetError(f"Grafana dashboard has no title: {name}")
            if not isinstance(dashboard.get("panels"), list) or not dashboard["panels"]:
                raise AssetError(f"Grafana dashboard has no panels: {name}")
            role = "dashboard"
            surface = {"fields": json_leaves(dashboard)}
        else:
            role = (
                "alerting" if "/alerting/" in name else
                "datasource" if "/datasources/" in name or name.startswith("derived/") else
                "dashboard-provider" if "/dashboards/" in name else "configuration"
            )
            # YAML/INI remain text: parsing would erase comments and may
            # interpret template expressions or silently overwrite duplicate keys.
            surface = {"lines": [
                {"line": number, "text": line}
                for number, line in enumerate(text.splitlines(), 1)
            ]}
        roles.add(role)
        documents.append({**entry, "role": role, **surface})
    required = {"dashboard", "alerting", "datasource", "dashboard-provider", "configuration"}
    if roles != required:
        raise AssetError(f"incomplete Grafana review roles: expected {sorted(required)}, got {sorted(roles)}")
    return {
        "schemaVersion": "radius-grafana-review-v1",
        "scope": "Vendored Grafana source and derived datasource, not a sealed or running fixture",
        "semanticVerdict": "not-established",
        "reviewRequired": True,
        "limitations": [
            "Complete extraction does not decide whether text reveals an incident answer.",
            "No hidden incident set, final sealed fixture, live dashboard state, or external link contents reviewed.",
            "Literal matches and diagnostic hints are evidence for review, not automatic leakage classifications.",
        ],
        "inventory": inventory,
        "documents": documents,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[2])
    parser.add_argument("--output", type=Path, required=True, help="New JSON artifact path; never overwritten")
    args = parser.parse_args()
    report = review_surfaces(args.repo_root)
    with args.output.open("x") as stream:
        json.dump(report, stream, indent=2)
        stream.write("\n")


if __name__ == "__main__":
    main()
