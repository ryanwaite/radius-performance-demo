"""Prepare pinned Shop source for treatment authoring, never trial admission."""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat
import subprocess
import tarfile
import urllib.request

from .astronomy_shop import UPSTREAM_COMMIT, UPSTREAM_REPO, UPSTREAM_TAG
from .manifest import hash_file


POLICY_VERSION = "shop-authoring-v1"
ARCHIVE_SHA256 = "60bc2420ede659375b54302799fc312443345457a6894b26ba5c2d7acc6895f7"
ARCHIVE_URL = f"https://codeload.github.com/{UPSTREAM_REPO}/tar.gz/{UPSTREAM_COMMIT}"
SOURCE_PREFIX = f"opentelemetry-demo-{UPSTREAM_COMMIT}"
ROOT_FILES = frozenset({
    ".dockerignore", ".gitignore", ".gitattributes", ".licenserc.json",
    ".markdownlint.yaml", ".yamlignore", ".yamllint",
    "AGENTS.md", "CLAUDE.md", "CONTRIBUTING.md", "LICENSE", "Makefile",
    "README.md", "buildkitd.toml", "compose.agent.yaml", "compose.extras.yaml",
    "compose.full.yaml", "compose.observability.yaml", "compose.profiling.yaml",
    "compose.tests.yaml", "compose.yaml", "docker-gen-proto.sh",
    "ide-gen-proto.sh", "otel-config.yml", "package.json", "package-lock.json",
})
SOURCE_DIRS = frozenset({
    "accounting", "ad", "agent", "cart", "chatbot", "checkout", "currency",
    "email", "flagd", "flagd-ui", "fraud-detection", "frontend", "frontend-proxy",
    "grafana", "image-provider", "jaeger", "kafka", "load-generator", "mcp",
    "opamp-server", "opensearch", "otel-collector", "payment", "postgresql",
    "product-catalog", "prometheus", "quote", "react-native-app",
    "recommendation", "shared", "shipping", "telemetry-docs",
})
SUPPORT_DIRS = ("test/telemetry/", "telemetry-schema/", "internal/tools/")
FORBIDDEN_PARTS = frozenset({
    ".git", ".github", ".radius", ".copilot", ".ssh", ".aws", ".azure",
    ".vscode", ".idea", ".DS_Store", ".gitmodules", ".lfsconfig",
    "benchmark", "evaluation", "results", "node_modules", "__pycache__",
    ".venv", "coverage", "profiles", "logs", "credentials", "secrets",
    "incidents", "injectors", "answers", "ground-truth", "vcr_cassettes",
})
REQUIRED_FILES = frozenset({
    "LICENSE", "AGENTS.md", "README.md", "Makefile", "compose.yaml",
    "compose.full.yaml", "compose.observability.yaml", "otel-config.yml",
    "src/cart/src/Dockerfile", "src/checkout/main.go",
    "src/load-generator/locustfile.py", "src/grafana/grafana.ini",
    "src/prometheus/prometheus-config.yaml", "test/telemetry/test_traces.py",
})
ARMS = ("native", "architecture", "radius")
README = """# OpenTelemetry Astronomy Shop

The Astronomy Shop is a microservice application that demonstrates OpenTelemetry
instrumentation. Application source and service tests are under `src/`; shared
telemetry definitions are under `telemetry-schema/`, and integration tests are
under `test/telemetry/`. Service Dockerfiles and Compose manifests describe build
inputs and runtime configuration. See `CONTRIBUTING.md` and service documentation
for development guidance. Upstream copyright notices and `LICENSE` are retained.

This source export comes from open-telemetry/opentelemetry-demo release 3.1.0,
commit dedc0178918e260823323b8d95005a8cb924b007. It has a fresh local Git baseline,
not upstream history. No dependencies have been installed or application services
started. Existing source, tests and telemetry configuration are unchanged.

`compose.defaults` records public non-secret configuration defaults from that
revision for source inspection. It is not a complete runtime environment:
credentials, machine-specific mounts and local `.env` files are not distributed.
The upstream Makefile still expects operator-supplied environment files.
Do not run its default startup on a shared host without reviewing mounts,
published ports, dependencies and local configuration.

The optional agent service's recorded model conversations are not distributed.
Its replay mode is consequently incomplete. Ordinary source references to those
files remain unchanged. This export is for source inspection and authoring, not
a claim that every optional startup profile is runnable.
"""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise ValueError(message)


def canonical(value: object) -> bytes:
    return (json.dumps(value, sort_keys=True, indent=2) + "\n").encode()


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def safe_path(name: str) -> None:
    parts = PurePosixPath(name).parts
    require(bool(parts) and not name.startswith("/") and "\\" not in name
            and all(p not in ("", ".", "..") for p in name.split("/"))
            and not any(ord(c) < 32 for c in name) and ":" not in name,
            f"unsafe archive path: {name!r}")


def exclusion(name: str) -> str | None:
    safe_path(name)
    parts = PurePosixPath(name).parts
    if any(p in FORBIDDEN_PARTS or p.startswith(".env") for p in parts):
        return "private, control, recorded-model, generated or environment path"
    if name in ROOT_FILES:
        return None
    if len(parts) > 2 and parts[0] == "src" and parts[1] in SOURCE_DIRS:
        return None
    if name.startswith(SUPPORT_DIRS):
        return None
    return "outside the versioned application-source allowlist"


def read_archive(path: Path, prefix: str = "") -> dict[str, tuple[bytes, int]]:
    """Read regular files only; never ask tarfile to extract paths."""
    files: dict[str, tuple[bytes, int]] = {}
    seen: set[str] = set()
    total = 0
    with tarfile.open(path, "r:*") as archive:
        for member in archive:
            raw = member.name.rstrip("/") if member.isdir() else member.name
            safe_path(raw)
            require(raw not in seen, f"duplicate archive path: {raw}")
            seen.add(raw)
            require(member.isfile() or member.isdir(), f"nonregular archive member: {raw}")
            require(member.mode in (0o644, 0o755, 0o664, 0o775),
                    f"unsafe archive mode: {raw}")
            if prefix:
                require(raw == prefix or raw.startswith(prefix + "/"),
                        f"unexpected source prefix: {raw}")
                raw = raw[len(prefix):].lstrip("/")
            if member.isdir():
                continue
            require(bool(raw), "archive file has no relative path")
            total += member.size
            require(member.size <= 32 * 1024 * 1024 and total <= 256 * 1024 * 1024,
                    "archive size limit exceeded")
            stream = archive.extractfile(member)
            assert stream is not None
            files[raw] = (stream.read(), 0o755 if member.mode & 0o111 else 0o644)
    require(bool(files), "empty archive")
    return files


def inventory(files: dict[str, tuple[bytes, int]]) -> list[dict]:
    return [{"path": name, "sha256": digest(data), "mode": mode}
            for name, (data, mode) in sorted(files.items())]


def source_files(archive: Path) -> tuple[dict[str, tuple[bytes, int]], dict]:
    require(hash_file(archive) == "sha256:" + ARCHIVE_SHA256, "upstream archive digest mismatch")
    upstream = read_archive(archive, SOURCE_PREFIX)
    files = {name: value for name, value in upstream.items() if exclusion(name) is None}
    require(REQUIRED_FILES <= files.keys(), "required application source missing")
    excluded = [{**row, "reason": exclusion(row["path"])}
                for row in inventory(upstream) if exclusion(row["path"]) is not None]
    # Keep source configuration facts, not credentials or machine mounts.
    defaults = []
    omitted = []
    for line in upstream[".env"][0].decode().splitlines():
        if not line or line.startswith("#"):
            continue
        key, value = line.split("=", 1)
        if re.search(r"(?:^|_)(?:PASSWORD|TOKEN|SECRET|KEY)(?:_|$)|^(?:HOST_FILESYSTEM|DOCKER_SOCK)$", key):
            omitted.append(key)
        else:
            defaults.append(f"{key}={value}")
    files["README.md"] = (README.encode(), 0o644)
    files["compose.defaults"] = (
        ("# Public source defaults; not a complete runtime environment.\n"
         + "\n".join(defaults) + "\n").encode(), 0o644,
    )
    changes = [
        {"path": name, "before": next((r for r in inventory(upstream) if r["path"] == name), None),
         "after": next(r for r in inventory(files) if r["path"] == name), "reason": reason}
        for name, reason in (
            ("README.md", "neutral source overview and explicit export limitations"),
            ("compose.defaults", "non-secret source defaults without copying environment files"),
        )
    ]
    return files, {"excluded": excluded, "commonChanges": changes, "omittedDefaultKeys": omitted}


def write_archive(files: dict[str, tuple[bytes, int]], output: Path) -> None:
    with output.open("xb") as handle, tarfile.open(fileobj=handle, mode="w", format=tarfile.USTAR_FORMAT) as archive:
        for name, (data, mode) in sorted(files.items()):
            info = tarfile.TarInfo(name)
            info.size, info.mode = len(data), mode
            info.mtime = 0
            archive.addfile(info, io.BytesIO(data))


def git(workspace: Path, *args: str) -> str:
    env = {
        "PATH": os.environ["PATH"], "HOME": str(workspace),
        "LANG": "C", "LC_ALL": "C", "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_CONFIG_GLOBAL": os.devnull, "GIT_CONFIG_SYSTEM": os.devnull,
        "GIT_ATTR_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0",
        "GIT_AUTHOR_NAME": "Shop source", "GIT_COMMITTER_NAME": "Shop source",
        "GIT_AUTHOR_EMAIL": "source@example.invalid",
        "GIT_COMMITTER_EMAIL": "source@example.invalid",
        "GIT_AUTHOR_DATE": "2000-01-01T00:00:00Z",
        "GIT_COMMITTER_DATE": "2000-01-01T00:00:00Z",
    }
    return subprocess.run(
        ["git", "-C", str(workspace), *args], env=env,
        capture_output=True, text=True, check=True,
    ).stdout.strip()


def workspace_files(workspace: Path) -> dict[str, tuple[bytes, int]]:
    require(workspace.is_dir() and not workspace.is_symlink(), "workspace is not a real directory")
    files = {}
    for path in sorted(workspace.rglob("*")):
        name = path.relative_to(workspace).as_posix()
        if name == ".git" or name.startswith(".git/"):
            continue
        require(not path.is_symlink(), f"workspace symlink: {name}")
        if path.is_dir():
            continue
        mode = path.stat().st_mode
        require(stat.S_ISREG(mode), f"workspace nonregular file: {name}")
        files[name] = (path.read_bytes(), stat.S_IMODE(mode))
    return files


def verify_workspace(workspace: Path, manifest: dict) -> dict:
    require(REQUIRED_FILES <= {row["path"] for row in manifest["files"]},
            "incomplete source manifest")
    require(inventory(workspace_files(workspace)) == manifest["files"], "workspace source differs")
    metadata = workspace / ".git"
    require(metadata.is_dir() and not metadata.is_symlink(), "not a standalone Git repository")
    allowed = {
        "core.repositoryformatversion": "0", "core.filemode": "true",
        "core.bare": "false", "core.logallrefupdates": "true",
        "core.ignorecase": "true", "core.precomposeunicode": "true",
        "core.autocrlf": "false",
    }
    config = dict(line.split("\n", 1) for line in
                  git(workspace, "config", "--local", "--null", "--list").split("\0") if line)
    require(all(allowed.get(key) == value for key, value in config.items()), "unexpected Git configuration")
    forbidden = ("hooks", "objects/info/alternates", "objects/info/http-alternates",
                 "commondir", "modules", "worktrees", "shallow", "info/grafts")
    require(not any((metadata / p).exists() or (metadata / p).is_symlink() for p in forbidden),
            "forbidden Git metadata")
    require(not any(p.is_symlink() for p in metadata.rglob("*")), "linked Git metadata")
    require(git(workspace, "rev-list", "--all", "--count") == "1", "source history present")
    require(git(workspace, "for-each-ref", "--format=%(refname)") == "refs/heads/main",
            "unexpected Git refs")
    require(git(workspace, "status", "--porcelain", "--untracked-files=all") == "", "dirty workspace")
    require(git(workspace, "ls-files", "-z").split("\0")[:-1] ==
            sorted(row["path"] for row in manifest["files"]), "Git baseline omits source files")
    return {"tree": git(workspace, "rev-parse", "HEAD^{tree}"),
            "commit": git(workspace, "rev-parse", "HEAD")}


def materialize(artifact: Path, manifest: dict, output: Path) -> dict:
    require(manifest["policy"] == POLICY_VERSION and manifest["eligibleForTrials"] is False,
            "only draft source artifacts are supported")
    require(hash_file(artifact) == manifest["artifactDigest"], "source artifact digest mismatch")
    files = read_archive(artifact)
    require(inventory(files) == manifest["files"], "source artifact inventory mismatch")
    require(REQUIRED_FILES <= files.keys(), "empty or incomplete application fixture")
    require(all(exclusion(n) is None or n == "compose.defaults" for n in files),
            "forbidden source file")
    return write_workspace(files, manifest, output)


def write_workspace(files: dict[str, tuple[bytes, int]], manifest: dict, output: Path) -> dict:
    """Write already validated files and verify the complete standalone baseline."""
    output.mkdir(parents=True, exist_ok=False)
    for name, (data, mode) in files.items():
        path = output / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        path.chmod(mode)
    git(output, "init", "--template=", "--initial-branch=main")
    git(output, "config", "core.autocrlf", "false")
    git(output, "config", "core.filemode", "true")
    git(output, "add", "--force", "--all")
    git(output, "-c", "commit.gpgsign=false", "commit", "-m",
        "Import Shop source\n\nCo-authored-by: Copilot App "
        "<223556219+Copilot@users.noreply.github.com>")
    return verify_workspace(output, manifest)


def prepare(archive: Path, output: Path) -> dict:
    files, review = source_files(archive)
    output.mkdir(parents=True, exist_ok=False)
    artifact = output / "source.tar"
    write_archive(files, artifact)
    manifest = {
        "policy": POLICY_VERSION, "status": "draft-source-only", "eligibleForTrials": False,
        "upstream": {"repository": UPSTREAM_REPO, "tag": UPSTREAM_TAG,
                     "commit": UPSTREAM_COMMIT, "archiveDigest": "sha256:" + ARCHIVE_SHA256},
        "artifactDigest": hash_file(artifact), "files": inventory(files),
    }
    (output / "source.manifest.json").write_bytes(canonical(manifest))
    (output / "source.review.json").write_bytes(canonical(review))
    receipt = {
        "status": "draft-source-only", "eligibleForTrials": False,
        "manifestDigest": digest(canonical(manifest)), "workspaces": {},
        "radius": {"status": "awaiting-owner-setup", "requestedVersion": "latest-at-setup",
                   "actualVersion": None, "tools": None, "skills": None, "graph": None},
        "architecture": {"status": "awaiting-validated-radius-facts-and-approved-authoring"},
        "incidentLeakageReview": "not-performed-no-incident-selected",
        "runtimeValidation": "not-performed",
    }
    for arm in ARMS:
        workspace = output / "workspaces" / arm
        baseline = materialize(artifact, manifest, workspace)
        receipt["workspaces"][arm] = {"path": str(workspace.resolve()), **baseline}
        (output / "preparation.json").write_bytes(canonical(receipt))
    for arm in ("architecture", "radius"):
        difference = {"status": "pending-treatment-not-parity-approval", "from": "native",
                      "to": arm, "differences": [], "sourceManifestDigest": digest(canonical(manifest))}
        (output / f"native-to-{arm}.json").write_bytes(canonical(difference))
    return receipt


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    fetch = commands.add_parser("fetch", help="download the pinned public source, without extracting it")
    fetch.add_argument("--output", required=True, type=Path)
    build = commands.add_parser("prepare", help="create source-only draft authoring workspaces")
    build.add_argument("--source-archive", required=True, type=Path)
    build.add_argument("--output", required=True, type=Path)
    create = commands.add_parser("materialize", help="recreate a standalone source baseline")
    create.add_argument("--artifact", required=True, type=Path)
    create.add_argument("--manifest", required=True, type=Path)
    create.add_argument("--output", required=True, type=Path)
    verify = commands.add_parser("verify", help="verify an unchanged source-only baseline")
    verify.add_argument("--workspace", required=True, type=Path)
    verify.add_argument("--manifest", required=True, type=Path)
    args = parser.parse_args()
    if args.command == "fetch":
        with urllib.request.urlopen(ARCHIVE_URL, timeout=120) as response, args.output.open("xb") as target:
            while chunk := response.read(1024 * 1024):
                target.write(chunk)
        require(hash_file(args.output) == "sha256:" + ARCHIVE_SHA256, "download digest mismatch")
        result = {"archive": str(args.output), "sha256": hash_file(args.output)}
    elif args.command == "prepare":
        result = prepare(args.source_archive, args.output)
    elif args.command == "materialize":
        result = materialize(args.artifact, json.loads(args.manifest.read_bytes()), args.output)
    else:
        result = verify_workspace(args.workspace, json.loads(args.manifest.read_bytes()))
    print(canonical(result).decode(), end="")


if __name__ == "__main__":
    main()
