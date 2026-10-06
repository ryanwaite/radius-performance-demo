"""Offline runtime-policy controls. Every subprocess is replaced before use."""

from copy import deepcopy
import importlib
import json
from pathlib import Path
import subprocess
import sys

import pytest

from radius_perf_eval import cli, podman_runtime as runtime


INFO = {
    "version": {"Version": "6.0.2"},
    "host": {
        "arch": "arm64", "os": "linux", "kernel": "test-kernel",
        "cgroupVersion": "v2", "cgroupManager": "systemd",
        "networkBackend": "netavark", "cpus": 5, "memTotal": 3783753728,
        "security": {"rootless": False},
        "ociRuntime": {"name": "crun", "version": "crun version test"},
    },
}


@pytest.fixture(autouse=True)
def no_processes(monkeypatch):
    def denied(*args, **kwargs):
        raise AssertionError("offline test attempted an unmocked process")
    monkeypatch.setattr(subprocess, "run", denied)


@pytest.fixture
def probe(tmp_path, monkeypatch):
    provider = tmp_path / "podman-compose"
    provider.write_text("synthetic provider bytes, never executed\n")
    responses = {
        "client": "podman version 6.0.2\n",
        "connections": [{"Name": "test-machine", "URI": "ssh://root@localhost/run/podman.sock"}],
        "info": deepcopy(INFO), "provider": "podman-compose version 1.5.0\n",
    }
    calls = []
    output = tmp_path / "inventory"

    def which(name):
        return name if Path(name).is_absolute() else "/test/bin/podman"

    def run(args, **kwargs):
        calls.append(args)
        saved = json.loads((output / "inventory.json").read_text())
        assert saved["commands"][-1] == {"args": args, "status": "started"}
        assert kwargs["timeout"] == 30 and kwargs["capture_output"] and kwargs["text"]
        assert kwargs["env"]["CONTAINER_CONNECTION"] == "test-machine"
        assert not any(key.startswith(("DOCKER_", "COMPOSE_", "PODMAN_"))
                       for key in kwargs["env"])
        assert "CONTAINER_HOST" not in kwargs["env"]
        if args == ["/test/bin/podman", "--version"]:
            text = responses["client"]
        elif args == ["/test/bin/podman", "system", "connection", "list", "--format", "json"]:
            text = json.dumps(responses["connections"])
        elif args == ["/test/bin/podman", "--connection", "test-machine", "info", "--format", "json"]:
            text = json.dumps(responses["info"])
        elif args == [str(provider), "--version"]:
            text = responses["provider"]
        else:
            raise AssertionError(f"unexpected runtime command: {args}")
        return subprocess.CompletedProcess(args, responses.get("returncode", 0), text, "synthetic stderr")

    monkeypatch.setattr(runtime.shutil, "which", which)
    monkeypatch.setattr(subprocess, "run", run)
    monkeypatch.setenv("DOCKER_HOST", "unix:///nonexistent/docker.sock")
    monkeypatch.setenv("CONTAINER_HOST", "unix:///nonexistent/podman.sock")
    monkeypatch.setenv("PODMAN_COMPOSE_PROVIDER", "/unapproved/provider")

    def collect(**kwargs):
        return runtime.inventory(output, connection="test-machine",
                                 compose_provider=kwargs.pop("compose_provider", provider), **kwargs)
    return collect, responses, calls, output, provider


def test_complete_inventory_is_not_qualification(probe):
    collect, _, calls, output, _ = probe
    record = collect()
    assert record["status"] == "inventoried-not-qualified"
    assert record["error"] is None and record["eligibleForTrials"] is False
    assert record["inventoryFingerprint"].startswith("podman-inventory-v1:")
    assert record["identity"]["rootless"] is False
    assert record["identity"]["composeProviderSha256"]
    assert record["unverifiedCapabilities"]
    assert len(calls) == 4
    assert json.loads((output / "inventory.json").read_text()) == record
    assert set(path.name for path in output.iterdir()) == {"inventory.json"}
    assert all(entry["status"] == "finished" and entry["stdout"] for entry in record["commands"])
    assert [entry["args"] for entry in record["commands"]] == calls


@pytest.mark.parametrize("field,value", [
    ("info", []), ("info", {}),
    ("client", "Docker version test"), ("client", ""), ("returncode", 1),
    ("connections", {}), ("connections", []),
    ("connections", [{"Name": "test-machine"}]),
    ("connections", [{"Name": "test-machine", "URI": "x"}] * 2),
    ("provider", "Docker Compose version test"),
])
def test_invalid_probe_evidence(probe, field, value):
    collect, responses, calls, output, _ = probe
    responses[field] = value
    record = collect()
    assert record["status"] == "incomplete" and record["error"]
    assert record["eligibleForTrials"] is False and record["inventoryFingerprint"] is None
    assert calls and len(record["commands"]) == len(calls)
    assert json.loads((output / "inventory.json").read_text()) == record
    if field == "client" and value == "":
        assert record["error"].startswith("runtime probe returned no evidence")
    if field == "connections" and value == {}:
        assert record["error"] == "connection inventory is not a list"


@pytest.mark.parametrize("path,value", [
    (("host",), None), (("version",), None), (("version", "Version"), ""),
    *[(("host", key), "") for key in
      ("arch", "os", "kernel", "cgroupVersion", "cgroupManager", "networkBackend")],
    (("host", "cpus"), 0), (("host", "cpus"), True),
    (("host", "memTotal"), -1), (("host", "memTotal"), "100"),
    (("host", "security"), None), (("host", "security", "rootless"), None),
    (("host", "security", "rootless"), "false"),
    (("host", "ociRuntime"), None), (("host", "ociRuntime", "name"), ""),
    (("host", "ociRuntime", "version"), ""),
])
def test_engine_identity_requires_evidence(path, value):
    info = deepcopy(INFO)
    target = info
    for key in path[:-1]:
        target = target[key]
    target[path[-1]] = value
    with pytest.raises(runtime.RuntimePolicyError):
        runtime.engine_identity(info)


def test_identity_distinguishes_runtime_context():
    baseline = runtime.engine_identity(INFO)
    for key, value in (("security", {"rootless": True}), ("cgroupVersion", "v1"),
                       ("cgroupManager", "cgroupfs"), ("cpus", 8), ("memTotal", 8000000000)):
        changed = deepcopy(INFO)
        changed["host"][key] = value
        assert runtime.engine_identity(changed) != baseline
    changed = deepcopy(INFO)
    changed["host"]["uptime"] = "later"
    changed["host"]["memFree"] = 1
    assert runtime.engine_identity(changed) == baseline


@pytest.mark.parametrize("changed", ["rootless", "memory", "client", "provider", "provider-bytes", "endpoint", "none"])
def test_fingerprint_binds_observed_identity(probe, changed):
    collect, responses, _, output, provider = probe
    first = collect()
    output.rename(output.with_name("first-inventory"))
    if changed == "rootless":
        responses["info"]["host"]["security"]["rootless"] = True
    elif changed == "memory":
        responses["info"]["host"]["memTotal"] += 1024
    elif changed == "client":
        responses["client"] = "podman version 6.0.3"
    elif changed == "provider":
        responses["provider"] = "podman-compose version 1.5.1"
    elif changed == "provider-bytes":
        provider.write_text("different synthetic provider bytes\n")
    elif changed == "endpoint":
        responses["connections"][0]["URI"] = "ssh://another-engine/run/podman.sock"
    second = collect()
    assert first["status"] == second["status"] == "inventoried-not-qualified"
    assert (first["inventoryFingerprint"] == second["inventoryFingerprint"]) == (changed == "none")


@pytest.mark.parametrize("provider", [None, Path("relative-provider"), Path("/missing/provider")])
def test_missing_provider_is_incomplete(probe, monkeypatch, provider):
    collect, _, calls, _, _ = probe
    original = runtime.shutil.which
    monkeypatch.setattr(runtime.shutil, "which",
                        lambda name: None if name == "/missing/provider" else original(name))
    record = collect(compose_provider=provider)
    assert record["status"] == "incomplete" and record["error"]
    assert record["engineIdentity"] == runtime.engine_identity(INFO)
    assert record["identity"] is None and record["inventoryFingerprint"] is None
    assert len(calls) == 3


def test_missing_client_is_recorded(probe, monkeypatch):
    collect, _, calls, output, _ = probe
    monkeypatch.setattr(runtime.shutil, "which", lambda name: None)
    record = collect()
    assert not calls and record["error"].startswith("Podman client not found")
    assert json.loads((output / "inventory.json").read_text()) == record


@pytest.mark.parametrize("error", [
    OSError("missing"),
    subprocess.TimeoutExpired("podman", 30, output=b"partial output", stderr=b"partial error"),
])
def test_probe_failures_are_durable(probe, monkeypatch, error):
    collect, _, _, output, _ = probe
    def fail(*args, **kwargs):
        raise error
    monkeypatch.setattr(subprocess, "run", fail)
    record = collect()
    assert record["status"] == "incomplete" and record["error"]
    assert record["commands"][0]["status"] == "failed"
    if isinstance(error, subprocess.TimeoutExpired):
        assert record["commands"][0]["stdout"] == "partial output"
        assert record["commands"][0]["stderr"] == "partial error"
    assert json.loads((output / "inventory.json").read_text()) == record


@pytest.mark.parametrize("connection", ["", " ", "-unsafe"])
def test_invalid_connection_precedes_output(tmp_path, connection):
    output = tmp_path / "new"
    with pytest.raises(runtime.RuntimePolicyError):
        runtime.inventory(output, connection=connection)
    assert not output.exists()


def test_existing_artifacts_are_preserved(probe):
    collect, _, _, output, _ = probe
    collect()
    before = (output / "inventory.json").read_bytes()
    with pytest.raises(FileExistsError):
        collect()
    assert (output / "inventory.json").read_bytes() == before


def test_raw_info_is_saved_before_interpretation(probe, monkeypatch):
    collect, _, _, output, _ = probe
    original = runtime.engine_identity
    def inspect_saved(info):
        saved = json.loads((output / "inventory.json").read_text())
        last = saved["commands"][-1]
        assert last["status"] == "finished" and json.loads(last["stdout"]) == info
        return original(info)
    monkeypatch.setattr(runtime, "engine_identity", inspect_saved)
    assert collect()["status"] == "inventoried-not-qualified"


@pytest.mark.parametrize("command", ["shop", "trial", "determinism", "cleanup"])
def test_live_cli_refuses_before_dispatch(monkeypatch, capsys, command):
    dispatched = []
    monkeypatch.setattr(cli, f"cmd_{command}", lambda args: dispatched.append(command) or 0)
    assert cli.main([command]) == 2
    assert not dispatched
    assert "Docker fallback is prohibited" in capsys.readouterr().err


def test_doctor_dispatch_remains_available(probe, capsys):
    _, _, calls, output, provider = probe
    assert cli.main(["doctor", "--connection", "test-machine", "--output", str(output),
                     "--compose-provider", str(provider)]) == 0
    assert calls and json.loads(capsys.readouterr().out)["eligibleForTrials"] is False


def test_doctor_incomplete_returns_failure(probe, capsys):
    _, _, calls, output, _ = probe
    assert cli.main(["doctor", "--connection", "test-machine", "--output", str(output)]) == 2
    assert calls and json.loads(capsys.readouterr().out)["status"] == "incomplete"


@pytest.mark.parametrize("tool", ["measure_astronomy_footprint", "pin_astronomy_shop_images", "run_holdout"])
def test_legacy_tools_refuse_before_effects(monkeypatch, tmp_path, tool):
    path = Path(__file__).resolve().parents[1] / "tools" / f"{tool}.py"
    module = importlib.import_module(f"tools.{tool}")
    if tool == "measure_astronomy_footprint":
        monkeypatch.setattr(module, "DEFAULT_ARTIFACTS", tmp_path / "artifacts")
    monkeypatch.setattr(sys, "argv", [str(path)])
    with pytest.raises(runtime.RuntimePolicyError, match="Podman driver migration"):
        if tool == "run_holdout":
            module.main([str(path.parents[2]), str(tmp_path / "artifacts"), "synthetic"])
        else:
            module.main()
    assert not list(tmp_path.iterdir())
