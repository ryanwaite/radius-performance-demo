"""Offline authoring-source controls. These do not qualify a treatment."""

import io
import json
import os
from pathlib import Path
import tarfile
from unittest.mock import patch

import pytest

from radius_perf_eval import shop_fixtures as f


def tar(path, entries):
    with tarfile.open(path, "w") as archive:
        for name, data, mode, kind in entries:
            member = tarfile.TarInfo(name)
            member.mode, member.type = mode, kind
            member.size = len(data) if kind == tarfile.REGTYPE else 0
            member.linkname = "../../outside"
            archive.addfile(member, io.BytesIO(data) if member.isfile() else None)
    return path


@pytest.fixture
def upstream(tmp_path, monkeypatch):
    files = {name: (b"public source\n", 0o644) for name in f.REQUIRED_FILES}
    files.update({
        ".gitignore": (b"ignored.go\n", 0o644),
        ".env": (b"AD_PORT=9555\nVALKEY_ADDR=valkey-cart:6379\nAPI_KEY=not-for-export\n"
                 b"POSTGRES_PASSWORD=not-for-export\nHOST_FILESYSTEM=/\nDOCKER_SOCK=/socket\n", 0o644),
        ".env.override": (b"private override\n", 0o644),
        "src/checkout/.env.local": (b"private\n", 0o644),
        "src/agent/fixtures/vcr_cassettes/capture.yaml": (b"model transcript\n", 0o644),
        "src/checkout/main_test.go": (b"service test\n", 0o644),
        "src/checkout/tool.sh": (b"#!/bin/sh\n", 0o755),
        "src/checkout/ignored.go": (b"tracked upstream despite ignore rule\n", 0o644),
        "src/checkout/benchmark/answers.json": (b"planted answer\n", 0o644),
        "docs/specs/experiment.md": (b"planted plan\n", 0o644),
        "evaluation/inject.py": (b"planted injector\n", 0o644),
        ".git/config": (b"host credential\n", 0o644),
        ".github/workflows/private.yml": (b"credential\n", 0o644),
    })
    path = tmp_path / "upstream.tar"
    f.write_archive({f.SOURCE_PREFIX + "/" + name: value for name, value in files.items()}, path)
    monkeypatch.setattr(f, "ARCHIVE_SHA256", f.hash_file(path).removeprefix("sha256:"))
    return path


@pytest.fixture
def prepared(upstream, tmp_path):
    root = tmp_path / "prepared"
    receipt = f.prepare(upstream, root)
    manifest = json.loads((root / "source.manifest.json").read_bytes())
    return root, receipt, manifest


def test_prepare_same_realistic_source_and_explicit_incomplete_treatments(prepared):
    root, receipt, manifest = prepared
    assert receipt["eligibleForTrials"] is False
    assert receipt["radius"]["actualVersion"] is None
    assert receipt["architecture"]["status"].startswith("awaiting")
    assert receipt["incidentLeakageReview"] == "not-performed-no-incident-selected"
    baselines = list(receipt["workspaces"].values())
    assert len(baselines) == 3
    assert len({(r["tree"], r["commit"]) for r in baselines}) == 1
    for arm in f.ARMS:
        workspace = root / "workspaces" / arm
        assert f.verify_workspace(workspace, manifest)
        assert not (workspace / ".env").exists()
        assert not (workspace / "ARCHITECTURE.md").exists()
        assert not (workspace / ".radius").exists()
        assert (workspace / "src/checkout/main_test.go").read_bytes() == b"service test\n"
        assert (workspace / "src/checkout/tool.sh").stat().st_mode & 0o777 == 0o755
        defaults = (workspace / "compose.defaults").read_text()
        assert "VALKEY_ADDR=valkey-cart:6379" in defaults
        assert "AD_PORT=9555" in defaults
        assert "not-for-export" not in defaults
        assert "HOST_FILESYSTEM" not in defaults
        assert "DOCKER_SOCK" not in defaults
        assert f.git(workspace, "remote", "-v") == ""
        assert "Co-authored-by: Copilot App" in f.git(workspace, "log", "-1", "--format=%B")
    review = json.loads((root / "source.review.json").read_bytes())
    assert {r["path"] for r in review["excluded"]} >= {
        ".env", ".env.override", "src/checkout/.env.local", ".git/config",
        "src/agent/fixtures/vcr_cassettes/capture.yaml", "docs/specs/experiment.md",
        "src/checkout/benchmark/answers.json", "evaluation/inject.py",
    }
    assert all(row["sha256"] and row["reason"] for row in review["excluded"])
    for arm in ("architecture", "radius"):
        diff = json.loads((root / f"native-to-{arm}.json").read_bytes())
        assert diff["status"] == "pending-treatment-not-parity-approval"
        assert diff["differences"] == []


def test_prepare_wiring(upstream, tmp_path):
    root = tmp_path / "direct"
    receipt = f.prepare(upstream, root)
    manifest = json.loads((root / "source.manifest.json").read_bytes())
    assert len(receipt["workspaces"]) == 3
    for row in receipt["workspaces"].values():
        assert row["tree"] and row["commit"]
        assert f.verify_workspace(Path(row["path"]), manifest)


def test_reproducible_export_and_ten_baselines(prepared, upstream, tmp_path):
    root, receipt, manifest = prepared
    again = tmp_path / "again"
    f.prepare(upstream, again)
    assert (root / "source.tar").read_bytes() == (again / "source.tar").read_bytes()
    assert (root / "source.manifest.json").read_bytes() == (again / "source.manifest.json").read_bytes()
    expected = receipt["workspaces"]["native"]
    for index in range(10):
        observed = f.materialize(root / "source.tar", manifest, tmp_path / f"repeat-{index}")
        assert observed == {key: expected[key] for key in ("tree", "commit")}


@pytest.mark.parametrize("name", ["/escape", "../escape", "a/../escape", "a//b", "./file",
                                 "a\\b", "C:escape", "a\nb", ""])
def test_unsafe_paths(name):
    with pytest.raises(ValueError, match="unsafe"):
        f.safe_path(name)


@pytest.mark.parametrize("name", [
    "benchmark/foo.py", "docs/specs/plan.md", ".github/config.json",
    "src/checkout/.git/config", "src/checkout/.env", "src/checkout/.env.local",
    "src/checkout/answers/answer.json", "src/checkout/credentials/key",
    "src/checkout/.aws/config", "src/checkout/node_modules/package/index.js",
    "src/checkout/.gitmodules", "src/checkout/.lfsconfig",
    "src/checkout/injectors/fault.py", "src/unknown/main.py",
])
def test_allowlist_exclusions(name):
    assert f.exclusion(name) is not None


@pytest.mark.parametrize("kind", [tarfile.SYMTYPE, tarfile.LNKTYPE, tarfile.CHRTYPE,
                                  tarfile.BLKTYPE, tarfile.FIFOTYPE])
def test_nonregular_archive_members(tmp_path, kind):
    path = tar(tmp_path / "bad.tar", [("src/checkout/main.go", b"", 0o644, kind)])
    with pytest.raises(ValueError, match="nonregular"):
        f.read_archive(path)


def test_archive_positive_and_negative_controls(tmp_path):
    path = tar(tmp_path / "positive.tar", [
        ("root", b"", 0o755, tarfile.DIRTYPE),
        ("root/source.go", b"source", 0o644, tarfile.REGTYPE),
    ])
    assert f.read_archive(path, "root") == {"source.go": (b"source", 0o644)}
    with pytest.raises(ValueError, match="prefix"):
        f.read_archive(path, "different")
    duplicate = tar(tmp_path / "duplicate.tar", [
        ("file", b"a", 0o644, tarfile.REGTYPE), ("file", b"b", 0o644, tarfile.REGTYPE)])
    with pytest.raises(ValueError, match="duplicate"):
        f.read_archive(duplicate)
    mode = tar(tmp_path / "mode.tar", [("file", b"source", 0o4755, tarfile.REGTYPE)])
    with pytest.raises(ValueError, match="mode"):
        f.read_archive(mode)
    empty = tar(tmp_path / "empty.tar", [])
    with pytest.raises(ValueError, match="empty"):
        f.read_archive(empty)
    no_name = tar(tmp_path / "name.tar", [("root", b"x", 0o644, tarfile.REGTYPE)])
    with pytest.raises(ValueError, match="no relative path"):
        f.read_archive(no_name, "root")
    traversal = tar(tmp_path / "traversal.tar", [("../outside", b"x", 0o644, tarfile.REGTYPE)])
    with pytest.raises(ValueError, match="unsafe"):
        f.read_archive(traversal)


def test_archive_size_control(tmp_path):
    path = tmp_path / "oversized.tar"
    with tarfile.open(path, "w") as archive:
        member = tarfile.TarInfo("large")
        member.mode, member.size = 0o644, 33 * 1024 * 1024
        archive.addfile(member, io.BytesIO(b"x" * member.size))
    with pytest.raises(ValueError, match="size limit"):
        f.read_archive(path)


def test_upstream_digest_and_required_source_controls(upstream, monkeypatch, tmp_path):
    monkeypatch.setattr(f, "ARCHIVE_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="upstream archive digest"):
        f.source_files(upstream)
    empty = tmp_path / "missing.tar"
    f.write_archive({f.SOURCE_PREFIX + "/LICENSE": (b"license", 0o644)}, empty)
    monkeypatch.setattr(f, "ARCHIVE_SHA256", f.hash_file(empty).removeprefix("sha256:"))
    with pytest.raises(ValueError, match="required application"):
        f.source_files(empty)


@pytest.mark.parametrize("field,value", [("policy", "unknown"), ("eligibleForTrials", True),
                                        ("artifactDigest", "sha256:" + "0" * 64),
                                        ("files", [])])
def test_materialize_manifest_controls(prepared, tmp_path, field, value):
    root, _, manifest = prepared
    manifest[field] = value
    with pytest.raises(ValueError):
        f.materialize(root / "source.tar", manifest, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


def test_rehashed_forbidden_and_incomplete_artifacts(prepared, tmp_path):
    root, _, manifest = prepared
    files = f.read_archive(root / "source.tar")
    files["src/checkout/.git/config"] = (b"private", 0o644)
    bad = tmp_path / "forbidden.tar"
    f.write_archive(files, bad)
    manifest.update(artifactDigest=f.hash_file(bad), files=f.inventory(files))
    with pytest.raises(ValueError, match="forbidden source"):
        f.materialize(bad, manifest, tmp_path / "bad")
    assert not (tmp_path / "bad").exists()
    incomplete = tmp_path / "incomplete.tar"
    files = {"README.md": (b"tiny tree", 0o644)}
    f.write_archive(files, incomplete)
    manifest.update(artifactDigest=f.hash_file(incomplete), files=f.inventory(files))
    with pytest.raises(ValueError, match="incomplete"):
        f.materialize(incomplete, manifest, tmp_path / "missing")
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("change", ["bytes", "mode", "extra", "missing", "link", "fifo"])
def test_workspace_content_controls(prepared, tmp_path, change):
    root, _, manifest = prepared
    workspace = root / "workspaces/native"
    path = workspace / "src/checkout/main.go"
    if change == "bytes":
        path.write_bytes(b"modified")
    elif change == "mode":
        path.chmod(0o755)
    elif change == "extra":
        (workspace / "src/checkout/extra.go").write_bytes(b"extra")
    else:
        path.unlink()
        if change == "link":
            path.symlink_to(tmp_path)
        elif change == "fifo":
            os.mkfifo(path)
    expected = {"link": "workspace symlink", "fifo": "workspace nonregular"}.get(
        change, "workspace source differs")
    read_bytes = Path.read_bytes

    def no_fifo_read(candidate):
        if candidate.is_fifo():
            raise AssertionError("verifier attempted to read a FIFO")
        return read_bytes(candidate)

    with patch.object(Path, "read_bytes", no_fifo_read):
        with pytest.raises(ValueError, match=expected):
            f.verify_workspace(workspace, manifest)


@pytest.mark.parametrize("change", ["remote", "hook", "alternate", "config", "refs",
                                   "history", "linked-git", "linked-object", "index", "staged"])
def test_git_isolation_controls(prepared, tmp_path, change):
    root, _, manifest = prepared
    workspace = root / "workspaces/native"
    metadata = workspace / ".git"
    if change == "remote":
        f.git(workspace, "remote", "add", "origin", "https://example.invalid/repo")
    elif change == "hook":
        (metadata / "hooks").mkdir()
        (metadata / "hooks/pre-commit").write_text("#!/bin/sh\nexit 0\n")
    elif change == "alternate":
        (metadata / "objects/info/alternates").write_text("/somewhere/objects")
    elif change == "config":
        f.git(workspace, "config", "core.fsmonitor", "/somewhere/program")
    elif change == "refs":
        f.git(workspace, "tag", "source-history")
    elif change == "history":
        f.git(workspace, "commit", "--allow-empty", "-m", "More history")
    elif change == "linked-git":
        metadata.rename(tmp_path / "foreign-git")
        metadata.symlink_to(tmp_path / "foreign-git", target_is_directory=True)
    elif change == "linked-object":
        (metadata / "objects/foreign").symlink_to(tmp_path)
    elif change == "staged":
        path = workspace / "README.md"
        data = path.read_bytes()
        path.write_bytes(b"staged replacement\n")
        f.git(workspace, "add", "README.md")
        path.write_bytes(data)
    else:
        f.git(workspace, "rm", "--cached", "README.md")
    with pytest.raises(ValueError):
        f.verify_workspace(workspace, manifest)


def test_index_cannot_omit_ignored_source(prepared):
    root, _, manifest = prepared
    workspace = root / "workspaces/native"
    f.git(workspace, "rm", "--cached", "README.md")
    f.git(workspace, "commit", "--amend", "--no-edit")
    (workspace / ".git/info").mkdir(exist_ok=True)
    (workspace / ".git/info/exclude").write_text("README.md\n")
    with pytest.raises(ValueError, match="omits source"):
        f.verify_workspace(workspace, manifest)


def test_missing_and_symlink_workspace(prepared, tmp_path):
    _, _, manifest = prepared
    with pytest.raises(ValueError, match="real directory"):
        f.verify_workspace(tmp_path / "missing", manifest)
    linked = tmp_path / "linked"
    linked.symlink_to(prepared[0] / "workspaces/native", target_is_directory=True)
    with pytest.raises(ValueError, match="real directory"):
        f.verify_workspace(linked, manifest)


def test_empty_workspace_cannot_pass(tmp_path):
    workspace = tmp_path / "empty"
    workspace.mkdir()
    f.git(workspace, "init", "--template=", "--initial-branch=main")
    f.git(workspace, "commit", "--allow-empty", "-m", "Empty baseline")
    with pytest.raises(ValueError, match="incomplete source manifest"):
        f.verify_workspace(workspace, {"files": []})


def test_ambient_git_state_and_existing_destinations(prepared, tmp_path, monkeypatch):
    root, _, manifest = prepared
    monkeypatch.setenv("GIT_DIR", str(root / "workspaces/native/.git"))
    monkeypatch.setenv("GIT_CONFIG_COUNT", "1")
    monkeypatch.setenv("GIT_CONFIG_KEY_0", "core.hooksPath")
    monkeypatch.setenv("GIT_CONFIG_VALUE_0", "/foreign/hooks")
    workspace = tmp_path / "isolated"
    f.materialize(root / "source.tar", manifest, workspace)
    assert f.verify_workspace(workspace, manifest)
    before = (workspace / "README.md").read_bytes()
    with pytest.raises(FileExistsError):
        f.materialize(root / "source.tar", manifest, workspace)
    assert (workspace / "README.md").read_bytes() == before


def test_cli_commands(prepared, upstream, tmp_path, monkeypatch, capsys):
    root, _, manifest = prepared
    monkeypatch.setattr("sys.argv", ["fixtures", "verify", "--workspace",
                                    str(root / "workspaces/native"), "--manifest",
                                    str(root / "source.manifest.json")])
    f.main()
    assert json.loads(capsys.readouterr().out)["tree"]
    monkeypatch.setattr("sys.argv", ["fixtures", "materialize", "--artifact",
                                    str(root / "source.tar"), "--manifest",
                                    str(root / "source.manifest.json"), "--output",
                                    str(tmp_path / "cli-copy")])
    f.main()
    assert json.loads(capsys.readouterr().out) == f.verify_workspace(tmp_path / "cli-copy", manifest)
    monkeypatch.setattr("sys.argv", ["fixtures", "prepare", "--source-archive",
                                    str(upstream), "--output", str(tmp_path / "cli-prepare")])
    f.main()
    assert json.loads(capsys.readouterr().out)["eligibleForTrials"] is False


def test_fetch_digest_control(upstream, tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(f.urllib.request, "urlopen", lambda *a, **kw: upstream.open("rb"))
    monkeypatch.setattr("sys.argv", ["fixtures", "fetch", "--output", str(tmp_path / "fetched.tar")])
    f.main()
    assert json.loads(capsys.readouterr().out)["sha256"] == f.hash_file(upstream)
    monkeypatch.setattr(f, "ARCHIVE_SHA256", "0" * 64)
    monkeypatch.setattr("sys.argv", ["fixtures", "fetch", "--output", str(tmp_path / "bad-fetch.tar")])
    with pytest.raises(ValueError, match="download digest"):
        f.main()
