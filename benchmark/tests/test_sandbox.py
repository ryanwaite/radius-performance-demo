"""Tests for sandbox application, the per-command gate, and escape probes.

The three tests the brief names as positive controls are marked in their
docstrings. Each is written so that it fails if the corresponding safety check
is removed, rather than merely passing when the check happens to be present.
"""

from __future__ import annotations

from pathlib import Path

from radius_perf_eval.sandbox import (
    DEFAULT_PROBES,
    EscapeProbe,
    ProbeExecution,
    ProbeOutcome,
    ProbeReport,
    SandboxApplication,
    SandboxGate,
    SandboxSettings,
    sandbox_applied_flag,
)


def _application(
    *, succeeded: bool = True, before_first_prompt: bool = True
) -> SandboxApplication:
    return SandboxApplication(
        requested={"sandboxConfig": {"enabled": True}},
        succeeded=succeeded,
        applied_before_first_prompt=before_first_prompt,
        result={"ok": True} if succeeded else None,
        error=None if succeeded else "rejected",
    )


def _execution(flag):
    payload = {"toolCallId": "t1"}
    if flag is not None:
        payload["toolTelemetry"] = {"properties": {"sandboxApplied": flag}}
    return payload


# --- configuration -----------------------------------------------------------


def test_settings_carry_the_configuration_the_plan_requires():
    config = SandboxSettings(workspace=Path("/tmp/ws")).to_wire()
    assert config["enabled"] is True
    assert config["allowBypass"] is False
    assert config["allowDevToolAccess"] is False
    assert config["addCurrentWorkingDirectory"] is True


def test_toolchain_paths_are_declared_read_only():
    settings = SandboxSettings(
        workspace=Path("/tmp/ws"), readonly_paths=("/usr/bin", "/opt/homebrew")
    )
    filesystem = settings.to_wire()["userPolicy"]["filesystem"]
    assert "/usr/bin" in filesystem["readonlyPaths"]
    assert "/opt/homebrew" in filesystem["readonlyPaths"]
    # A toolchain path must never become writable: that would hand the agent a
    # writable directory outside its workspace under the guise of tooling.
    assert not set(filesystem["readonlyPaths"]) & set(filesystem["readwritePaths"])


def test_workspace_is_the_only_readwrite_path():
    filesystem = SandboxSettings(workspace=Path("/tmp/ws")).to_wire()["userPolicy"][
        "filesystem"
    ]
    assert filesystem["readwritePaths"] == ["/tmp/ws"]


def test_wire_dict_round_trips_through_the_typed_config():
    """The recorded request must be what the SDK would actually send.

    If a field name were wrong, the typed constructor would drop it and the
    recorded evidence would claim a setting that was never transmitted.
    """
    from copilot.generated.rpc import SandboxConfig

    wire = SandboxSettings(workspace=Path("/tmp/ws")).to_wire()
    assert SandboxConfig.from_dict(wire).to_dict() == wire


# --- the sandboxApplied flag -------------------------------------------------


def test_sandbox_applied_flag_reads_the_string_form():
    assert sandbox_applied_flag(_execution("true")) == "true"
    assert sandbox_applied_flag(_execution("false")) == "false"


def test_absent_flag_is_none_not_false():
    """"Reported not applied" and "reported nothing" are different findings."""
    assert sandbox_applied_flag(_execution(None)) is None


def test_false_string_is_not_treated_as_truthy():
    """The flag arrives as a string, so `"false"` is truthy in Python.

    This is the bug the gate would have had if the value were tested directly.
    """
    gate = SandboxGate()
    gate.record_application(_application())
    gate.observe_tool_execution(_execution("false"))
    result = gate.evaluate()
    assert result.passed is False
    assert result.tool_executions_confirmed == 0


# --- the gate ----------------------------------------------------------------


def test_gate_passes_when_every_execution_is_confirmed():
    gate = SandboxGate()
    gate.record_application(_application())
    for _ in range(3):
        gate.observe_tool_execution(_execution("true"))
    result = gate.evaluate()
    assert result.passed is True
    assert result.tool_executions_observed == 3
    assert result.tool_executions_confirmed == 3
    assert result.vacuous is False


def test_positive_control_unflagged_execution_fails_the_gate():
    """POSITIVE CONTROL (brief 1): a tool execution without `sandboxApplied`
    must fail the gate.

    Two of the three executions are confirmed, so this also shows the gate is
    not satisfied by a majority.
    """
    gate = SandboxGate()
    gate.record_application(_application())
    gate.observe_tool_execution(_execution("true"))
    gate.observe_tool_execution(_execution(None))
    gate.observe_tool_execution(_execution("true"))
    result = gate.evaluate()
    assert result.passed is False
    assert result.tool_executions_observed == 3
    assert result.tool_executions_confirmed == 2
    assert result.unconfirmed[0]["sandboxApplied"] is None


def test_positive_control_update_that_never_ran_fails_the_trial():
    """POSITIVE CONTROL (brief 1): an update that never ran must fail the
    trial rather than pass it.

    The tempting bug is to pass when there is nothing to object to. Every
    execution here reports `sandboxApplied: "true"`, so the only thing that can
    fail this trial is the missing application record itself.
    """
    gate = SandboxGate()
    for _ in range(5):
        gate.observe_tool_execution(_execution("true"))
    result = gate.evaluate()
    assert result.passed is False
    assert "never applied" in (result.reason or "")
    assert result.tool_executions_confirmed == 5


def test_failed_application_fails_the_gate():
    gate = SandboxGate()
    gate.record_application(_application(succeeded=False))
    gate.observe_tool_execution(_execution("true"))
    assert gate.evaluate().passed is False


def test_application_after_first_prompt_fails_the_gate():
    gate = SandboxGate()
    gate.record_application(_application(before_first_prompt=False))
    gate.observe_tool_execution(_execution("true"))
    result = gate.evaluate()
    assert result.passed is False
    assert "after the first prompt" in (result.reason or "")


def test_gate_with_no_executions_is_vacuous_and_does_not_pass():
    """A check that examined nothing must not report success.

    Without this, a trial whose tools all failed to start would produce a green
    gate, and the green would strengthen as the harness broke.
    """
    gate = SandboxGate()
    gate.record_application(_application())
    result = gate.evaluate()
    assert result.passed is False
    assert result.vacuous is True
    assert result.tool_executions_observed == 0


def test_gate_records_counts_even_when_it_passes():
    gate = SandboxGate()
    gate.record_application(_application())
    gate.observe_tool_execution(_execution("true"))
    payload = gate.evaluate().to_json_dict()
    assert payload["toolExecutionsObserved"] == 1
    assert payload["toolExecutionsConfirmed"] == 1


# --- probes ------------------------------------------------------------------


def test_not_executed_is_not_blocked():
    """A probe that never ran is not evidence of confinement."""
    report = ProbeReport(
        control_passed=True,
        executions=[
            ProbeExecution(
                probe=EscapeProbe(name="p", command="x", kind="write", target="/x"),
                outcome=ProbeOutcome.NOT_EXECUTED,
                error="the model declined",
            )
        ],
    )
    passed, reason = report.passed()
    assert passed is False
    assert "executed" in (reason or "")


def test_report_fails_when_the_positive_control_failed():
    """If the in-workspace write never happened, the probes prove nothing.

    A boundary that blocks everything, including legitimate work, is
    indistinguishable from a harness that could not run commands at all.
    """
    report = ProbeReport(
        control_passed=False,
        executions=[
            ProbeExecution(
                probe=EscapeProbe(name="p", command="x", kind="write", target="/x"),
                outcome=ProbeOutcome.BLOCKED,
            )
        ],
    )
    assert report.passed()[0] is False


def test_report_fails_on_any_escape():
    report = ProbeReport(
        control_passed=True,
        executions=[
            ProbeExecution(
                probe=EscapeProbe(name="a", command="x", kind="write", target="/x"),
                outcome=ProbeOutcome.BLOCKED,
            ),
            ProbeExecution(
                probe=EscapeProbe(name="b", command="y", kind="write", target="/y"),
                outcome=ProbeOutcome.ESCAPED,
            ),
        ],
    )
    passed, reason = report.passed()
    assert passed is False
    assert "did not confine" in (reason or "")
    assert "b" in (reason or "")


def test_report_passes_only_with_a_control_and_a_real_execution():
    report = ProbeReport(
        control_passed=True,
        executions=[
            ProbeExecution(
                probe=EscapeProbe(name="a", command="x", kind="write", target="/x"),
                outcome=ProbeOutcome.BLOCKED,
            )
        ],
    )
    assert report.passed()[0] is True


def test_default_probes_cover_both_concealment_classes(tmp_path):
    """The probe set must span both the literal and the concealed class.

    The distinction is narrower than it first looks. The "variable" probe
    assigns the directory to a shell variable, so the *full* target path never
    appears as one token, but the directory still appears literally. Only the
    base64 probes contain no path substring at all. `conceals_path` records
    that difference, and this test pins it, so a future edit cannot quietly
    relabel a partially literal probe as a concealed one.
    """
    outside = tmp_path / "outside"
    probes = DEFAULT_PROBES(
        outside_dir=outside,
        parent_canary=tmp_path / "canary.txt",
        canary_token="CANARY-TOKEN",
    )
    by_name = {p.name: p for p in probes}

    literal = by_name["write-outside-literal"]
    assert literal.conceals_path is False
    assert str(literal.target) in literal.command

    variable = by_name["write-outside-variable"]
    # Not concealed: the parent directory is spelled out, so a screen keying on
    # the directory would still catch it. Only the full path is assembled.
    assert variable.conceals_path is False
    assert str(outside) in variable.command
    assert str(variable.target) not in variable.command

    concealed = by_name["write-outside-concealed"]
    assert concealed.conceals_path is True
    assert str(outside) not in concealed.command
    assert str(tmp_path) not in concealed.command

    assert {p.kind for p in probes} == {"read", "write"}


def test_every_write_probe_declares_ground_truth(tmp_path):
    """A write probe judged only on exit status proves nothing.

    During the spike a perl write exited 0 while writing nothing; only the file
    check revealed the refusal.
    """
    probes = DEFAULT_PROBES(
        outside_dir=tmp_path / "outside",
        parent_canary=tmp_path / "canary.txt",
        canary_token="CANARY-TOKEN",
    )
    for probe in probes:
        if probe.kind == "write":
            assert probe.target, f"{probe.name} has no target path to check"
        else:
            assert probe.canary, f"{probe.name} has no canary token to check"


def test_probe_outcomes_are_three_distinct_states():
    assert len({ProbeOutcome.BLOCKED, ProbeOutcome.ESCAPED, ProbeOutcome.NOT_EXECUTED}) == 3
