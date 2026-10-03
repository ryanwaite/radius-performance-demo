"""Offline capture controls, not graph, deployment or semantic acceptance."""

import json
import sys

import pytest

from radius_perf_eval import radius_overlay as r, shop_fixtures as s
from tests.test_shop_fixtures import upstream  # Reuse the source policy's synthetic archive.


@pytest.fixture
def inputs(upstream, tmp_path, monkeypatch):
    files = s.read_archive(upstream)
    for name in r.GIT_NORMALIZED_PATHS:
        files[f"{s.SOURCE_PREFIX}/{name}"] = (b"windows script\r\n", 0o644)
    source_archive = tmp_path / "source-input.tar"
    s.write_archive(files, source_archive)
    monkeypatch.setattr(s, "ARCHIVE_SHA256", s.hash_file(source_archive).removeprefix("sha256:"))
    native, _ = s.source_files(source_archive)
    baseline = s.write_workspace(native, {"files": s.inventory(native)}, tmp_path / "baseline")
    monkeypatch.setattr(r, "BASE_TREE", baseline["tree"])
    monkeypatch.setattr(r, "BASE_COMMIT", baseline["commit"])
    model = b"extension radius\nparam environment string\n"
    monkeypatch.setattr(r, "ORIGIN", {**r.ORIGIN, "sourceCommit": baseline["commit"],
                                    "appBicepHash": s.digest(model.rstrip())})
    monkeypatch.setattr(r, "MODEL_DIGEST", s.digest(model))
    overlay = {
        ".radius/app.bicep": (model, 0o644),
        ".radius/app.origin.json": (s.canonical(r.ORIGIN), 0o644),
        ".radius/.gitignore": (b"app-graph.json\n", 0o644),
        ".radius/bicepconfig.json": (s.canonical({
            "extensions": {"radius": "br:biceptypes.azurecr.io/radius:0.61"},
        }), 0o644),
    }
    application = dict(native)
    for name in r.GIT_NORMALIZED_PATHS:
        application[name] = (native[name][0].replace(b"\r\n", b"\n"), native[name][1])
    application.update(overlay)
    return source_archive, native, application


def archive(tmp_path, monkeypatch, files, name="application.tar"):
    path = tmp_path / name
    s.write_archive({r.ARCHIVE_PREFIX + "/" + p: value for p, value in files.items()}, path)
    monkeypatch.setattr(r, "ARCHIVE_SHA256", s.hash_file(path).removeprefix("sha256:"))
    return path


def test_capture_recreation_and_complete_common_bytes(inputs, tmp_path, monkeypatch):
    upstream, native, application = inputs
    app = archive(tmp_path, monkeypatch, application)
    outputs = [tmp_path / "first", tmp_path / "second"]
    for output in outputs:
        receipt = r.import_overlay(upstream, app, output)
        assert receipt["eligibleForTrials"] is False
        assert set(receipt["workspaces"]) == {"native", "radius"}
        for arm in ("native", "radius"):
            manifest = json.loads((output / f"{'source' if arm == 'native' else arm}.manifest.json").read_bytes())
            files = s.workspace_files(output / "workspaces" / arm)
            assert {p: value for p, value in files.items() if p in native} == native
            assert s.verify_workspace(output / "workspaces" / arm, manifest)
            assert s.inventory(s.read_archive(output / f"{'source' if arm == 'native' else arm}.tar")) == manifest["files"]
        actual = s.workspace_files(output / "workspaces/radius")
        assert set(actual) - native.keys() == r.OVERLAY_PATHS
        assert {p: actual[p] for p in r.OVERLAY_PATHS} == {p: application[p] for p in r.OVERLAY_PATHS}
        assert s.read_archive(output / "radius-overlay.tar") == {p: application[p] for p in r.OVERLAY_PATHS}
        diff = json.loads((output / "native-to-radius.json").read_bytes())
        assert diff["unchangedFiles"] == s.inventory(native)
        assert {row["path"] for row in diff["differences"]} == r.OVERLAY_PATHS
        assert all(row["reason"] == "unchanged committed Radius authoring addition"
                   for row in diff["differences"])
        assert all(row["before"] is None and row["after"] in s.inventory(application)
                   for row in diff["differences"])
        assert diff["sourceGitNormalizations"]
        for row in diff["sourceGitNormalizations"]:
            assert row["workspace"] != row["committed"]
            assert actual[row["path"]][0] == b"windows script\r\n"
        setup = json.loads((output / "setup-inventory.json").read_bytes())
        assert setup["examinedFiles"] == s.inventory(application)
        assert setup["originHashMatchesModel"] is True
        assert setup["normalizedModelDigest"] == r.ORIGIN["appBicepHash"]
        assert setup["modelDigest"] == s.digest(application[".radius/app.bicep"][0])
        assert setup["eligibleForTrials"] is False
        for field in ("installedRadiusCliVersion", "installedExtensionVersion", "diagnosticTools",
                      "diagnosticSkills", "graphPayload"):
            assert setup[field] is None
        assert setup["repositoryToolManifestsAdded"] == []
        assert setup["repositorySkillFilesAdded"] == []
        assert setup["githubAccess"]["scoredPolicy"] == "unchanged-no-general-github-access"
        assert setup["incidentLeakageReview"] == "not-performed-no-incident-selected"
    for name in ("source.tar", "radius.tar", "radius-overlay.tar", "source.manifest.json",
                 "radius.manifest.json", "native-to-radius.json", "setup-inventory.json"):
        assert (outputs[0] / name).read_bytes() == (outputs[1] / name).read_bytes()
    first = json.loads((outputs[0] / "import.json").read_bytes())
    second = json.loads((outputs[1] / "import.json").read_bytes())
    for arm in first["workspaces"]:
        assert first["workspaces"][arm]["tree"] == second["workspaces"][arm]["tree"]
        assert first["workspaces"][arm]["commit"] == second["workspaces"][arm]["commit"]
    with pytest.raises(FileExistsError):
        r.import_overlay(upstream, app, outputs[0])


@pytest.mark.parametrize("change", ["modify", "delete", "add", "mode", "empty"])
def test_source_inventory_rejection(inputs, tmp_path, monkeypatch, change):
    upstream, _, files = inputs
    if change == "modify":
        files["src/checkout/main.go"] = (b"changed source", 0o644)
    elif change == "delete":
        del files["src/checkout/main.go"]
    elif change == "add":
        files[".github/private.json"] = (b"host config", 0o644)
    elif change == "mode":
        data, _ = files["src/checkout/main.go"]
        files["src/checkout/main.go"] = (data, 0o755)
    else:
        files = {p: v for p, v in files.items() if p in r.OVERLAY_PATHS}
    with pytest.raises(ValueError, match="complete committed native"):
        r.import_overlay(upstream, archive(tmp_path, monkeypatch, files), tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


@pytest.mark.parametrize("change", ["missing", "extra", "empty", "mode", "model", "origin",
                                    "sourceCommit", "skillVersion", "generatedAt"])
def test_overlay_rejection(inputs, tmp_path, monkeypatch, change):
    upstream, _, files = inputs
    message = "overlay paths"
    if change == "missing":
        del files[".radius/.gitignore"]
    elif change == "empty":
        files = {p: v for p, v in files.items() if p not in r.OVERLAY_PATHS}
    elif change == "extra":
        files[".radius/host-token"] = (b"private", 0o644)
    elif change == "mode":
        files[".radius/.gitignore"] = (b"app-graph.json\n", 0o755)
        message = "file mode"
    elif change == "model":
        files[".radius/app.bicep"] = (b"changed model\n", 0o644)
        message = "model digest"
    else:
        origin = dict(r.ORIGIN)
        origin["appBicepHash" if change == "origin" else change] = "unexpected"
        files[".radius/app.origin.json"] = (s.canonical(origin), 0o644)
        message = "unexpected Radius origin"
    with pytest.raises(ValueError, match=message):
        r.import_overlay(upstream, archive(tmp_path, monkeypatch, files), tmp_path / "bad")
    assert not (tmp_path / "bad").exists()


def test_archive_pin_rejection(inputs, tmp_path, monkeypatch):
    upstream, _, files = inputs
    app = archive(tmp_path, monkeypatch, files)
    monkeypatch.setattr(r, "ARCHIVE_SHA256", "0" * 64)
    with pytest.raises(ValueError, match="application archive digest"):
        r.import_overlay(upstream, app, tmp_path / "bad")


@pytest.mark.parametrize("constant", ["BASE_TREE", "BASE_COMMIT"])
def test_baseline_pin_rejection(inputs, tmp_path, monkeypatch, constant):
    upstream, _, files = inputs
    monkeypatch.setattr(r, constant, "0" * 40)
    with pytest.raises(ValueError, match="native Git baseline"):
        r.import_overlay(upstream, archive(tmp_path, monkeypatch, files), tmp_path / "bad")
    assert not (tmp_path / "bad/import.json").exists()


def test_cli_wiring(inputs, tmp_path, monkeypatch, capsys):
    upstream, _, files = inputs
    app = archive(tmp_path, monkeypatch, files)
    monkeypatch.setattr(sys, "argv", ["radius_overlay", "--source-archive", str(upstream),
                                    "--application-archive", str(app), "--output", str(tmp_path / "cli")])
    r.main()
    receipt = json.loads(capsys.readouterr().out)
    assert receipt == json.loads((tmp_path / "cli/import.json").read_bytes())
    assert receipt["workspaces"]["radius"]["commit"]


def test_origin_normalization_contract():
    expected = s.digest(b"  extension radius\nparam environment string")
    assert r.origin_model_digest(b"  extension radius \t\r\nparam environment string\t \r\n\n") == expected
    assert r.origin_model_digest(b"  extension radius\nparam environment string\n") == expected
    assert r.origin_model_digest(b"extension radius\nparam environment string\n") != expected
    assert r.origin_model_digest(b"  extension radius\nparam environment different\n") != expected
