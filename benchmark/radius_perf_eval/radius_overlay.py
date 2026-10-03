"""Import the reviewed Radius commit as an unsealed, source-identical draft.

This offline importer does not compile Bicep, operate a graph, qualify semantics,
or capture tools from the host. Fetch the exact archives separately.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import re

from . import shop_fixtures as source
from .manifest import hash_file
from .shop_fixtures import canonical, digest, inventory, require


REPOSITORY = "ryanwaite/astronomy-shop-radius"
COMMIT = "dce2f8f596e2eda9d7d07c114cb44749dc27acb0"
BASE_COMMIT = "12dbef5dfde1df1b902ca74b7d2b03ada89eceef"
BASE_TREE = "7d9c5587e18627943c8237229b1fea0293c958b5"
ARCHIVE_SHA256 = "e8a15244c19f8896de4e9699da50e3b0c6fec4096f953c0042b69d811cbeaaf0"
ARCHIVE_PREFIX = f"ryanwaite-astronomy-shop-radius-{COMMIT}"
MODEL_DIGEST = "sha256:c6bfefa7fd5d2577bcb764e7a4f0dc2d6bddd416541623312b09ecff24df74e1"
GIT_NORMALIZED_PATHS = ("src/ad/gradlew.bat", "src/fraud-detection/gradlew.bat")
OVERLAY_PATHS = frozenset({
    ".radius/.gitignore", ".radius/app.bicep",
    ".radius/app.origin.json", ".radius/bicepconfig.json",
})
ORIGIN = {
    "sourceCommit": BASE_COMMIT, "skillVersion": "0.2.0",
    "generatedAt": "2026-10-03T03:20:19.777Z",
    "appBicepHash": "sha256:81cf697706f4584d4ef93f66d7e75a573b9b7e6a418601eca0f54d0039e65f59",
}


def origin_model_digest(data: bytes) -> str:
    """Radius 0.2.0 origin hashes normalized text, unlike archive byte hashes."""
    text = data.decode().replace("\r\n", "\n")
    return digest(re.sub(r"[ \t]+$", "", text, flags=re.MULTILINE).rstrip().encode())


def import_overlay(upstream_archive: Path, application_archive: Path, output: Path) -> dict:
    require(hash_file(application_archive) == "sha256:" + ARCHIVE_SHA256,
            "application archive digest mismatch")
    application = source.read_archive(application_archive, ARCHIVE_PREFIX)
    native, review = source.source_files(upstream_archive)
    # The original source workspace retains CRLF; Git's text=auto cleans these
    # blobs at baseline commit. Compare committed bytes, but export neither
    # normalization as a treatment change.
    committed_native = dict(native)
    for name in GIT_NORMALIZED_PATHS:
        data, mode = native[name]
        committed_native[name] = (data.replace(b"\r\n", b"\n"), mode)
    unchanged = {name: value for name, value in application.items()
                 if not name.startswith(".radius/")}
    require(unchanged == committed_native,
            "application differs from the complete committed native source inventory")
    overlay = {name: value for name, value in application.items()
               if name.startswith(".radius/")}
    require(overlay.keys() == OVERLAY_PATHS, "unexpected or missing Radius overlay paths")
    require(all(mode == 0o644 for _, mode in overlay.values()), "unexpected Radius file mode")
    origin = json.loads(overlay[".radius/app.origin.json"][0])
    require(origin == ORIGIN, "unexpected Radius origin")
    require(digest(overlay[".radius/app.bicep"][0]) == MODEL_DIGEST, "Radius model digest mismatch")
    config = json.loads(overlay[".radius/bicepconfig.json"][0])
    radius = native | overlay

    output.mkdir(parents=True, exist_ok=False)
    source.write_archive(native, output / "source.tar")
    source.write_archive(overlay, output / "radius-overlay.tar")
    source.write_archive(radius, output / "radius.tar")
    source_manifest = {
        "policy": source.POLICY_VERSION, "status": "draft-source-only", "eligibleForTrials": False,
        "upstream": {"repository": source.UPSTREAM_REPO, "tag": source.UPSTREAM_TAG,
                     "commit": source.UPSTREAM_COMMIT,
                     "archiveDigest": hash_file(upstream_archive)},
        "artifactDigest": hash_file(output / "source.tar"), "files": inventory(native),
    }
    radius_manifest = {
        "policy": "shop-radius-import-v1", "status": "draft-unsealed", "eligibleForTrials": False,
        "application": {"repository": REPOSITORY, "commit": COMMIT, "sourceCommit": BASE_COMMIT,
                        "archiveDigest": hash_file(application_archive)},
        "sourceManifestDigest": digest(canonical(source_manifest)),
        "overlayDigest": hash_file(output / "radius-overlay.tar"),
        "artifactDigest": hash_file(output / "radius.tar"), "files": inventory(radius),
    }
    difference = {
        "status": "captured-not-parity-approval", "from": "native", "to": "radius",
        "sourceManifestDigest": digest(canonical(source_manifest)),
        "radiusManifestDigest": digest(canonical(radius_manifest)),
        "unchangedFiles": inventory(native),
        "committedSourceFiles": inventory(committed_native),
        "sourceGitNormalizations": [
            {"path": name, "workspace": inventory({name: native[name]})[0],
             "committed": inventory({name: committed_native[name]})[0],
             "reason": "baseline Git text=auto CRLF-to-LF; not applied to either draft"}
            for name in GIT_NORMALIZED_PATHS
        ],
        "differences": [{"path": row["path"], "before": None, "after": row,
                         "reason": "unchanged committed Radius authoring addition"}
                        for row in inventory(overlay)],
    }
    setup = {
        "status": "incomplete", "eligibleForTrials": False,
        "examinedFiles": inventory(application), "radiusAdditions": inventory(overlay),
        "origin": origin, "bicepConfiguration": config,
        "modelDigest": MODEL_DIGEST,
        "normalizedModelDigest": origin_model_digest(overlay[".radius/app.bicep"][0]),
        "originHashMatchesModel": origin["appBicepHash"] == origin_model_digest(
            overlay[".radius/app.bicep"][0]),
        "installedRadiusCliVersion": None, "installedExtensionVersion": None,
        "diagnosticTools": None, "diagnosticSkills": None,
        "repositoryToolManifestsAdded": [], "repositorySkillFilesAdded": [],
        "graphPayload": None, "graphBuildVerification": "owner-extension-check-required",
        "graphLiveAccess": "not-verified",
        "semanticParity": "not-established-see-static-review",
        "runtimeValidation": "not-performed",
        "incidentLeakageReview": "not-performed-no-incident-selected",
        "architecture": "blocked-on-validated-model-and-approved-isolated-authoring",
        "githubAccess": {
            "authoring": "required-by-owner-handoff; build.source-pins-private-repository",
            "diagnosis": "unknown-app-hosted-exposure-needs-owner-check",
            "localSource": "available-in-all-arms",
            "scoredPolicy": "unchanged-no-general-github-access",
        },
    }
    for name, value in {
        "source.manifest.json": source_manifest, "source.review.json": review,
        "radius.manifest.json": radius_manifest, "native-to-radius.json": difference,
        "setup-inventory.json": setup,
    }.items():
        (output / name).write_bytes(canonical(value))
    receipt = {
        "status": "draft-unsealed", "eligibleForTrials": False,
        "sourceManifestDigest": digest(canonical(source_manifest)),
        "radiusManifestDigest": digest(canonical(radius_manifest)), "workspaces": {},
    }
    for arm, files, manifest in (
        ("native", native, source_manifest), ("radius", radius, radius_manifest),
    ):
        workspace = output / "workspaces" / arm
        baseline = source.write_workspace(files, manifest, workspace)
        if arm == "native":
            require(baseline["tree"] == BASE_TREE and baseline["commit"] == BASE_COMMIT,
                    "reconstructed native Git baseline differs from authoring provenance")
        receipt["workspaces"][arm] = {"path": str(workspace.resolve()), **baseline}
        (output / "import.json").write_bytes(canonical(receipt))
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-archive", required=True, type=Path)
    parser.add_argument("--application-archive", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    print(canonical(import_overlay(args.source_archive, args.application_archive, args.output)).decode(),
          end="")


if __name__ == "__main__":
    main()
