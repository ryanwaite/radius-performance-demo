"""Fetch reviewed, pinned upstream startup assets. Never runs during a trial."""

from __future__ import annotations

import json
import sys
import tempfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO / "benchmark"))

from radius_perf_eval.astronomy_shop import UPSTREAM_COMMIT, UPSTREAM_REPO
from radius_perf_eval.shop_assets import (
    PLATFORMS, PLUGIN_ID, PLUGIN_VERSION, asset_root, derive_load_script, derive_proxy_template, digest,
    extract_plugin, verify_assets,
)


def fetch(url: str) -> bytes:
    with urllib.request.urlopen(url, timeout=120) as response:
        return response.read()


def main() -> None:
    root = asset_root(REPO)
    files = {}
    for name in (
        ".env", "src/load-generator/locustfile.py", "src/load-generator/Dockerfile",
        "src/frontend-proxy/Dockerfile", "src/frontend-proxy/envoy.tmpl.yaml",
    ):
        url = f"https://raw.githubusercontent.com/{UPSTREAM_REPO}/{UPSTREAM_COMMIT}/{name}"
        data = fetch(url)
        target = root / "upstream" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files[str(target.relative_to(root))] = {"source": url, "sha256": digest(data)}
    source = (root / "upstream/src/load-generator/locustfile.py").read_text()
    derived = derive_load_script(source).encode()
    target = root / "derived/load-generator/locustfile.py"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(derived)
    files[str(target.relative_to(root))] = {
        "source": "upstream/src/load-generator/locustfile.py minus WebsiteUser.ask_agent",
        "sha256": digest(derived),
    }
    source = (root / "upstream/src/frontend-proxy/envoy.tmpl.yaml").read_text()
    derived = derive_proxy_template(source).encode()
    target = root / "derived/frontend-proxy/envoy.tmpl.yaml"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(derived)
    files[str(target.relative_to(root))] = {
        "source": "upstream/src/frontend-proxy/envoy.tmpl.yaml with flag and control routes blocked",
        "sha256": digest(derived),
    }
    for platform in PLATFORMS:
        name = f"{PLUGIN_ID}-{PLUGIN_VERSION}.{platform}.zip"
        url = f"https://github.com/grafana/opensearch-datasource/releases/download/v{PLUGIN_VERSION}/{name}"
        data = fetch(url)
        with tempfile.TemporaryDirectory(prefix="radius-plugin-license-") as temp:
            candidate = Path(temp) / name
            candidate.write_bytes(data)
            extract_plugin(candidate, Path(temp) / "reviewed")
        target = root / "plugins" / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        files[str(target.relative_to(root))] = {
            "source": url, "sha256": digest(data), "license": "Apache-2.0",
        }
    manifest = {
        "upstreamCommit": UPSTREAM_COMMIT,
        "pluginVersion": PLUGIN_VERSION,
        "compatibility": {
            "grafana": "13.1.0", "declaredRequirement": ">=10.4.0-0",
            "basis": "plugin.json in the signed release archive; live datasource check still required",
        },
        "files": files,
    }
    (root / "startup-assets.json").write_text(json.dumps(manifest, indent=2) + "\n")
    image_path = root / "image-digests.json"
    images = json.loads(image_path.read_text())
    images["startupAssets"] = {
        "manifest": "startup-assets.json",
        "sha256": digest((root / "startup-assets.json").read_bytes()),
        "plugins": {name: entry for name, entry in files.items() if name.startswith("plugins/")},
    }
    image_path.write_text(json.dumps(images, indent=2) + "\n")
    verify_assets(REPO)
    print("Pinned startup assets written and verified")


if __name__ == "__main__":
    main()
