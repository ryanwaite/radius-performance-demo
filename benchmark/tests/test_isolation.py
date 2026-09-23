"""Negative isolation tests.

The experiment plan's isolation exit criteria require proof that a session
cannot read or write outside its assigned workspace. These tests exercise the
real filesystem, including a real symlink, so symlink resolution is genuinely
tested rather than mocked.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from radius_perf_eval.copilot import (
    BUILTIN_AGENTS,
    RUNTIME_MIN_AI_CREDITS,
    SUBAGENT_TOOLS,
    IsolationPolicy,
    IsolationViolation,
    SessionBudget,
    TemporaryWorkspace,
    isolated_session_kwargs,
)
from radius_perf_eval.isolation_probe import _FakeRequest, run_isolation_probes


@pytest.fixture
def workspace():
    with TemporaryWorkspace() as ws:
        ws.write("notes.txt", "hello\n")
        yield ws


@pytest.fixture
def policy(workspace):
    return IsolationPolicy(workspace_root=workspace.root)


# ---------------------------------------------------------------------------
# The three required negative cases
# ---------------------------------------------------------------------------


def test_parent_directory_read_is_denied(policy, workspace):
    approved, reason = policy.evaluate(
        _FakeRequest(kind="read", path=str(workspace.root.parent))
    )
    assert not approved
    assert "escapes assigned workspace" in reason


def test_relative_parent_traversal_is_denied(policy):
    approved, _ = policy.evaluate(_FakeRequest(kind="read", path="../../etc/passwd"))
    assert not approved


def test_symlink_escape_read_is_denied(policy, workspace):
    link = workspace.root / "escape"
    link.symlink_to(workspace.root.parent, target_is_directory=True)
    approved, reason = policy.evaluate(_FakeRequest(kind="read", path=str(link)))
    assert not approved
    assert "escapes assigned workspace" in reason


def test_symlink_to_file_outside_is_denied(policy, workspace):
    link = workspace.root / "hosts-link"
    link.symlink_to(Path(os.sep) / "etc" / "hosts")
    approved, _ = policy.evaluate(_FakeRequest(kind="read", path="hosts-link"))
    assert not approved


def test_absolute_path_write_is_denied(policy):
    approved, _ = policy.evaluate(
        _FakeRequest(kind="write", file_name=str(Path(os.sep) / "tmp" / "escape.txt"))
    )
    assert not approved


def test_write_through_escaping_symlink_is_denied(policy, workspace):
    link = workspace.root / "escape"
    link.symlink_to(workspace.root.parent, target_is_directory=True)
    approved, _ = policy.evaluate(
        _FakeRequest(kind="write", file_name=str(link / "escape.txt"))
    )
    assert not approved


# ---------------------------------------------------------------------------
# Positive control: containment must not be achieved by denying everything
# ---------------------------------------------------------------------------


def test_in_workspace_read_is_allowed(policy):
    approved, _ = policy.evaluate(_FakeRequest(kind="read", path="notes.txt"))
    assert approved


def test_nested_in_workspace_write_is_allowed(policy, workspace):
    approved, _ = policy.evaluate(
        _FakeRequest(kind="write", file_name=str(workspace.root / "sub" / "out.txt"))
    )
    assert approved


def test_symlink_that_stays_inside_is_allowed(policy, workspace):
    (workspace.root / "sub").mkdir()
    link = workspace.root / "inner-link"
    link.symlink_to(workspace.root / "sub", target_is_directory=True)
    approved, _ = policy.evaluate(_FakeRequest(kind="read", path="inner-link"))
    assert approved


# ---------------------------------------------------------------------------
# Fail-closed behavior
# ---------------------------------------------------------------------------


def test_unknown_permission_kind_is_denied(policy):
    approved, reason = policy.evaluate(_FakeRequest(kind="a-kind-invented-next-year"))
    assert not approved
    assert "denied by default" in reason


def test_pathless_read_request_is_denied(policy):
    # A request the harness cannot evaluate must never be approved.
    approved, _ = policy.evaluate(_FakeRequest(kind="read"))
    assert not approved


def test_network_is_denied(policy):
    approved, _ = policy.evaluate(_FakeRequest(kind="url", url="https://example.invalid"))
    assert not approved


def test_memory_is_denied(policy):
    approved, reason = policy.evaluate(_FakeRequest(kind="memory"))
    assert not approved
    assert "memory" in reason


def test_shell_is_denied_by_default_because_possible_paths_is_unreliable(policy):
    # Verified against CLI 1.0.83: `cat /etc/hosts` arrives with
    # possible_paths == [], so approving on that signal fails open.
    approved, reason = policy.evaluate(
        _FakeRequest(
            kind="shell",
            full_command_text="cat /etc/hosts",
            possible_paths=[],
            possible_urls=[],
        )
    )
    assert not approved
    assert "shell is disabled" in reason


def test_shell_denied_by_default_even_for_an_innocuous_command(policy):
    approved, _ = policy.evaluate(
        _FakeRequest(
            kind="shell", full_command_text="ls", possible_paths=[], possible_urls=[]
        )
    )
    assert not approved


@pytest.mark.parametrize(
    "command",
    [
        "cat /etc/hosts",
        "cat ../escape.txt",
        "echo hi > /tmp/escape.txt",
        "cat ~/.ssh/id_rsa",
        "cat ../../etc/passwd",
        "ls .. ",
        "cat sub/../../../etc/passwd",
    ],
)
def test_shell_screen_denies_escape_shaped_commands(workspace, command):
    policy = IsolationPolicy(workspace_root=workspace.root, allow_shell=True)
    approved, _ = policy.evaluate(
        _FakeRequest(
            kind="shell", full_command_text=command, possible_paths=[], possible_urls=[]
        )
    )
    assert not approved, command


@pytest.mark.parametrize(
    "command",
    ["ls", "cat notes.txt", "grep -r hello .", "python3 script.py", "ls sub/nested"],
)
def test_shell_screen_allows_workspace_relative_commands(workspace, command):
    policy = IsolationPolicy(workspace_root=workspace.root, allow_shell=True)
    approved, _ = policy.evaluate(
        _FakeRequest(
            kind="shell", full_command_text=command, possible_paths=[], possible_urls=[]
        )
    )
    assert approved, command


def test_shell_screen_still_honours_populated_possible_paths(workspace):
    policy = IsolationPolicy(workspace_root=workspace.root, allow_shell=True)
    approved, _ = policy.evaluate(
        _FakeRequest(
            kind="shell",
            # Command text looks relative, but the runtime resolved an escape.
            full_command_text="cat notes.txt",
            possible_paths=[str(workspace.root.parent / "secret")],
            possible_urls=[],
        )
    )
    assert not approved
    assert policy.violations[-1]["command"] == "cat notes.txt"


def test_shell_sandbox_bypass_is_denied_even_when_shell_is_allowed(workspace):
    policy = IsolationPolicy(workspace_root=workspace.root, allow_shell=True)
    approved, reason = policy.evaluate(
        _FakeRequest(
            kind="shell",
            full_command_text="sudo id",
            possible_paths=[],
            possible_urls=[],
            request_sandbox_bypass=True,
        )
    )
    assert not approved
    assert "sandbox bypass" in reason


def test_shell_network_is_denied_even_when_shell_is_allowed(workspace):
    policy = IsolationPolicy(workspace_root=workspace.root, allow_shell=True)
    approved, _ = policy.evaluate(
        _FakeRequest(
            kind="shell",
            full_command_text="curl https://example.invalid",
            possible_paths=[],
            possible_urls=["https://example.invalid"],
        )
    )
    assert not approved


def test_shell_sandbox_bypass_is_denied(policy):
    approved, _ = policy.evaluate(
        _FakeRequest(
            kind="shell",
            full_command_text="sudo id",
            possible_paths=[],
            possible_urls=[],
            request_sandbox_bypass=True,
        )
    )
    assert not approved


def test_shell_touching_outside_path_is_denied(policy, workspace):
    approved, _ = policy.evaluate(
        _FakeRequest(
            kind="shell",
            full_command_text="cat ../secret",
            possible_paths=[str(workspace.root.parent / "secret")],
            possible_urls=[],
        )
    )
    assert not approved
    assert policy.violations[-1]["command"] == "cat ../secret"


def test_shell_with_network_is_denied(policy):
    approved, _ = policy.evaluate(
        _FakeRequest(
            kind="shell",
            full_command_text="curl https://example.invalid",
            possible_paths=[],
            possible_urls=["https://example.invalid"],
        )
    )
    assert not approved


def test_violations_are_recorded_for_the_artifact(policy, workspace):
    policy.evaluate(_FakeRequest(kind="read", path=str(workspace.root.parent)))
    assert len(policy.violations) == 1
    assert policy.violations[0]["kind"] == "read"
    assert "resolvedPath" in policy.violations[0]


# ---------------------------------------------------------------------------
# Probe suite
# ---------------------------------------------------------------------------


def test_full_probe_suite_fails_closed(workspace):
    report = run_isolation_probes(workspace)
    assert report["allFailedClosed"], report["failedProbes"]
    assert report["positiveControl"]["passed"]
    assert len(report["probes"]) >= 10


# ---------------------------------------------------------------------------
# Workspace seeding
# ---------------------------------------------------------------------------


def test_workspace_refuses_to_seed_outside_itself(workspace):
    with pytest.raises(IsolationViolation):
        workspace.write("../escape.txt", "nope")


def test_workspace_root_is_fully_resolved():
    with TemporaryWorkspace() as ws:
        assert ws.root == ws.root.resolve()
        assert ws.root.is_absolute()


def test_workspace_is_destroyed_on_exit():
    with TemporaryWorkspace() as ws:
        root = ws.root
        assert root.exists()
    assert not root.exists()


# ---------------------------------------------------------------------------
# Session configuration
# ---------------------------------------------------------------------------


def test_auto_routing_is_rejected(workspace):
    with pytest.raises(ValueError, match="auto routing is prohibited"):
        isolated_session_kwargs(workspace_root=workspace.root, model="auto")
    with pytest.raises(ValueError):
        isolated_session_kwargs(workspace_root=workspace.root, model="  AUTO ")


def test_session_kwargs_disable_memory_subagents_and_discovery(workspace):
    kwargs = isolated_session_kwargs(workspace_root=workspace.root, model="gpt-5.4")

    assert kwargs["memory"]["enabled"] is False
    assert kwargs["additional_directories"] == []
    assert kwargs["working_directory"] == str(workspace.root)

    for tool in SUBAGENT_TOOLS:
        assert f"builtin:{tool}" in kwargs["excluded_tools"]
    assert "mcp:*" in kwargs["excluded_tools"]
    assert "custom:*" in kwargs["excluded_tools"]
    assert kwargs["excluded_builtin_agents"] == list(BUILTIN_AGENTS)
    assert "task" in kwargs["excluded_builtin_agents"]

    assert kwargs["enable_skills"] is False
    assert kwargs["skip_custom_instructions"] is True
    assert kwargs["enable_config_discovery"] is False
    assert kwargs["enable_session_store"] is False
    assert kwargs["skip_embedding_retrieval"] is True
    assert kwargs["enable_file_hooks"] is False
    assert kwargs["mcp_servers"] == {}
    assert kwargs["mcp_oauth_token_storage"] == "in-memory"
    assert kwargs["embedding_cache_storage"] == "in-memory"


def test_session_limits_are_raised_to_the_runtime_floor(workspace):
    # The runtime rejects anything below 30 AI credits, so a tight per-trial
    # budget cannot be expressed as a runtime limit.
    kwargs = isolated_session_kwargs(
        workspace_root=workspace.root, model="gpt-5.4", max_ai_credits=1.5
    )
    assert kwargs["session_limits"]["max_ai_credits"] == RUNTIME_MIN_AI_CREDITS


def test_session_limits_above_the_floor_are_passed_through(workspace):
    kwargs = isolated_session_kwargs(
        workspace_root=workspace.root, model="gpt-5.4", max_ai_credits=50.0
    )
    assert kwargs["session_limits"]["max_ai_credits"] == 50.0


def test_harness_budget_enforces_the_real_cap_below_the_runtime_floor():
    # The tight cap the runtime cannot express is enforced locally instead.
    budget = SessionBudget(max_ai_credits=1.5)
    assert budget.check(
        elapsed_ms=0, model_requests=0, tool_calls=0, ai_credits=0.5
    ) is None
    reason = budget.check(
        elapsed_ms=0, model_requests=0, tool_calls=0, ai_credits=1.6
    )
    assert reason is not None and "AI-credit budget exhausted" in reason


def test_reasoning_effort_is_omitted_unless_requested(workspace):
    assert "reasoning_effort" not in isolated_session_kwargs(
        workspace_root=workspace.root, model="gpt-5.4"
    )
    kwargs = isolated_session_kwargs(
        workspace_root=workspace.root, model="gpt-5.4", reasoning_effort="medium"
    )
    assert kwargs["reasoning_effort"] == "medium"


# ---------------------------------------------------------------------------
# Misleading PermissionRequestShell fields (verified against CLI 1.0.83).
#
# Three fields a handler author would reasonably trust are unreliable, and two
# of them actively mislead. These tests pin the screening input so a future
# refactor toward the more "structured"-looking field fails here.
# ---------------------------------------------------------------------------


def _shell_request(**kwargs):
    from radius_perf_eval.isolation_probe import _FakeRequest

    kwargs.setdefault("kind", "shell")
    kwargs.setdefault("possible_paths", [])
    kwargs.setdefault("possible_urls", [])
    return _FakeRequest(**kwargs)


def _screening_policy(tmp_path):
    return IsolationPolicy(workspace_root=tmp_path, allow_shell=True)


def test_screen_denies_when_segments_truncate_the_redirection(tmp_path):
    """The exact shape that would bypass a segment-based screen.

    CLI 1.0.83 truncates `command_segments` at the redirection operator, so a
    handler reading segments sees `echo probe` and approves a write to /tmp.
    The screen must use the top-level command text instead.
    """
    from radius_perf_eval.isolation_probe import _FakeSegment

    request = _shell_request(
        full_command_text="echo probe > /tmp/escape.txt",
        command_segments=[_FakeSegment(full_command_text="echo probe")],
        has_write_file_redirection=False,
    )
    approved, reason = _screening_policy(tmp_path).evaluate(request)
    assert approved is False
    assert "/tmp/escape.txt" in reason


def test_segment_view_alone_would_have_approved_the_escape(tmp_path):
    """Demonstrates the bypass, so the regression test above has teeth."""
    from radius_perf_eval.isolation_probe import _FakeSegment

    segment_only = _shell_request(
        full_command_text=_FakeSegment(full_command_text="echo probe").full_command_text
    )
    approved, _ = _screening_policy(tmp_path).evaluate(segment_only)
    assert approved is True  # the truncated text contains no outside path


def test_has_write_file_redirection_is_not_trusted(tmp_path):
    """The flag is False for a redirection writing outside the workspace."""
    request = _shell_request(
        full_command_text="echo probe > /tmp/escape.txt",
        has_write_file_redirection=False,
    )
    approved, _ = _screening_policy(tmp_path).evaluate(request)
    assert approved is False


def test_empty_possible_paths_does_not_imply_containment(tmp_path):
    request = _shell_request(full_command_text="cat /etc/hosts", possible_paths=[])
    approved, _ = _screening_policy(tmp_path).evaluate(request)
    assert approved is False


def test_shell_is_denied_by_default_regardless_of_command(tmp_path):
    policy = IsolationPolicy(workspace_root=tmp_path)
    assert policy.allow_shell is False
    approved, reason = policy.evaluate(_shell_request(full_command_text="echo hi"))
    assert approved is False
    assert "possible_paths" in reason


# ---------------------------------------------------------------------------
# Known limitations are observations, never assertions.
# ---------------------------------------------------------------------------


def test_limitation_probes_are_excluded_from_the_pass_total(tmp_path):
    """A defeated limitation probe must not flip allFailedClosed."""
    from radius_perf_eval.copilot import TemporaryWorkspace as _TW
    from radius_perf_eval.isolation_probe import run_isolation_probes

    with _TW() as workspace:
        (workspace.root / "notes.txt").write_text("x", encoding="utf-8")
        report = run_isolation_probes(workspace)

    limitations = report["knownLimitations"]
    assert report["allFailedClosed"] is True
    assert limitations["defeatedCount"] > 0, "evidence must show where the screen ends"
    assert all(p["outcomeGuaranteed"] is False for p in limitations["probes"])
    named = {p["name"] for p in limitations["probes"]}
    assert "base64-encoded-path" in named


def test_encoded_path_defeats_the_static_screen(tmp_path):
    """Documents the boundary explicitly: this is expected to be approved."""
    request = _shell_request(
        full_command_text="echo L2V0Yy9ob3N0cw== | base64 -d | xargs cat"
    )
    approved, _ = _screening_policy(tmp_path).evaluate(request)
    assert approved is True, (
        "if this now denies, the screen improved -- update the evidence note, "
        "but do not treat it as confinement"
    )


# ---------------------------------------------------------------------------
# The isolation gate must stop a run, not report a metric.
# ---------------------------------------------------------------------------


def _gate_inputs(**probe_overrides):
    probe = {
        "shellRequestsSeen": 3,
        "shellRequestsApproved": 0,
        "canaryContentLeakedIntoTranscript": False,
        "absolutePathWriteSucceeded": False,
    }
    probe.update(probe_overrides)
    return {"probes": [{"name": "x"}], "allFailedClosed": True}, probe


def test_gate_passes_only_with_a_live_blocked_probe():
    from radius_perf_eval.isolation_probe import evaluate_isolation_gate

    report, probe = _gate_inputs()
    assert evaluate_isolation_gate(isolation_report=report, live_probe=probe)["passed"]


def test_gate_verdict_does_not_claim_confinement():
    """The verdict names what it proves: handler denial, not confinement.

    The agent runs as a host process, so nothing in this runtime confines it.
    A field called ``confinementBasis`` invited exactly the misreading that a
    passing gate means shell is contained, and consumers key off these names.
    """
    from radius_perf_eval.isolation_probe import evaluate_isolation_gate

    report, probe = _gate_inputs()
    verdict = evaluate_isolation_gate(isolation_report=report, live_probe=probe)

    assert "confinementBasis" not in verdict
    basis = verdict["permissionHandlerBasis"]
    assert "not confinement" in basis
    assert "host process" in basis


def test_scored_run_may_not_skip_the_live_probe():
    from radius_perf_eval.isolation_probe import evaluate_isolation_gate

    report, _ = _gate_inputs()
    verdict = evaluate_isolation_gate(isolation_report=report, live_probe=None)
    assert verdict["passed"] is False
    assert "liveProbePresent" in verdict["failedChecks"]


def test_vacuous_probe_fails_the_gate():
    """No attempted escape means nothing was proven."""
    from radius_perf_eval.isolation_probe import evaluate_isolation_gate

    report, probe = _gate_inputs(shellRequestsSeen=0)
    verdict = evaluate_isolation_gate(isolation_report=report, live_probe=probe)
    assert verdict["passed"] is False
    assert "liveProbeNonVacuous" in verdict["failedChecks"]


@pytest.mark.parametrize(
    "override, expected",
    [
        ({"shellRequestsApproved": 1}, "noShellRequestApproved"),
        ({"canaryContentLeakedIntoTranscript": True}, "noCanaryLeak"),
        ({"absolutePathWriteSucceeded": True}, "noAbsolutePathWrite"),
    ],
)
def test_each_escape_signal_fails_the_gate(override, expected):
    from radius_perf_eval.isolation_probe import evaluate_isolation_gate

    report, probe = _gate_inputs(**override)
    verdict = evaluate_isolation_gate(isolation_report=report, live_probe=probe)
    assert verdict["passed"] is False
    assert expected in verdict["failedChecks"]


def test_missing_static_probes_fail_the_gate():
    from radius_perf_eval.isolation_probe import evaluate_isolation_gate

    _, probe = _gate_inputs()
    verdict = evaluate_isolation_gate(isolation_report={}, live_probe=probe)
    assert verdict["passed"] is False
    assert "staticProbesRan" in verdict["failedChecks"]


def test_enforce_raises_rather_than_returning_a_flag():
    from radius_perf_eval.isolation_probe import (
        IsolationGateError,
        enforce_isolation_gate,
    )

    report, probe = _gate_inputs(shellRequestsApproved=1)
    with pytest.raises(IsolationGateError, match="noShellRequestApproved"):
        enforce_isolation_gate(isolation_report=report, live_probe=probe)


def test_unscored_run_may_skip_but_records_that_it_did():
    from radius_perf_eval.isolation_probe import evaluate_isolation_gate

    report, _ = _gate_inputs()
    verdict = evaluate_isolation_gate(
        isolation_report=report, live_probe=None, scored=False
    )
    assert verdict["passed"] is True
    assert verdict["scored"] is False


def test_cli_exits_nonzero_when_the_gate_fails(monkeypatch, capsys):
    """Prove the gate can actually go red, end to end, with no model call."""
    from radius_perf_eval import smoke

    async def _raise(**_kwargs):
        raise smoke.IsolationGateError("permission handler was not proven to deny")

    monkeypatch.setattr(smoke, "run_smoke", _raise)
    assert smoke.main(["--model", "gpt-5.4"]) == 1
    assert "ISOLATION GATE FAILED" in capsys.readouterr().err


def test_gate_artifact_is_cleared_between_runs():
    """A stale gate verdict must never be mistaken for the current run's."""
    from radius_perf_eval.smoke import ARTIFACT_NAMES

    assert "isolation-gate.json" in ARTIFACT_NAMES
