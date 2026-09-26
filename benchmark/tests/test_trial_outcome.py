"""Tests for trial scoring and the budget positive control."""

from __future__ import annotations

import asyncio
import time

import pytest

from radius_perf_eval.copilot import (
    PLAN_MAX_TOOL_CALLS,
    PLAN_REASONING_EFFORT,
    PLAN_WALL_CLOCK_MS,
    SessionBudget,
)
from radius_perf_eval.sandbox import SandboxApplication, SandboxGate, SandboxSettings
from radius_perf_eval.submit_tool import ComponentMap, SubmissionRecorder
from radius_perf_eval.trial_outcome import score_trial

MAP = ComponentMap.from_mapping({"cartservice": "cart"})


def _valid():
    return {
        "faultPresent": True,
        "causalCategory": "cpu_saturation",
        "component": "cartservice",
        "evidence": [{"signal": "cpu", "observation": "99%"}],
        "confidence": 0.8,
        "remediation": "raise the limit",
    }


class _Payload:
    def __init__(self, data):
        self._data = data

    def to_dict(self):
        return self._data


class _Event:
    """The attributes ``SpikeSession._on_event`` reads off an SDK event."""

    def __init__(self, event_type, data):
        self.type = event_type
        self.data = _Payload(data)
        self.id = "e"
        self.agent_id = None
        self.parent_id = None
        self.timestamp = None


def _event(event_type, data):
    return _Event(event_type, data)


def _recorder(*payloads):
    recorder = SubmissionRecorder(component_map=MAP)
    for payload in payloads:
        recorder.record(payload)
    return recorder


def _passing_gate():
    gate = SandboxGate()
    gate.record_application(
        SandboxApplication(
            requested={},
            succeeded=True,
            applied_before_first_prompt=True,
        )
    )
    gate.observe_tool_execution(
        {"toolCallId": "t", "toolTelemetry": {"properties": {"sandboxApplied": "true"}}}
    )
    return gate.evaluate()


# --- the plan's budget --------------------------------------------------------


def test_plan_budget_matches_the_plan_defaults():
    budget = SessionBudget.plan_default()
    assert budget.wall_clock_ms == 30 * 60 * 1000
    assert budget.max_tool_calls == 100
    assert PLAN_WALL_CLOCK_MS == 1_800_000
    assert PLAN_MAX_TOOL_CALLS == 100
    assert PLAN_REASONING_EFFORT == "high"


def test_plan_budget_declares_no_model_request_cap():
    """An undeclared third cap would end trials for a reason no arm agreed to.

    That would surface as a between-arm difference in exhaustion rate caused by
    the harness rather than the treatment.
    """
    assert SessionBudget.plan_default().max_model_requests is None


def test_wall_clock_cap_fires():
    budget = SessionBudget.plan_default()
    reason = budget.check(
        elapsed_ms=PLAN_WALL_CLOCK_MS + 1,
        model_requests=1,
        tool_calls=1,
        ai_credits=None,
    )
    assert reason is not None
    assert "wall-clock" in reason


def test_tool_call_cap_fires_at_the_limit_not_past_it():
    budget = SessionBudget.plan_default()
    assert (
        budget.check(
            elapsed_ms=0, model_requests=0, tool_calls=99, ai_credits=None
        )
        is None
    )
    reason = budget.check(
        elapsed_ms=0, model_requests=0, tool_calls=100, ai_credits=None
    )
    assert reason is not None
    assert "tool-call" in reason


def test_whichever_comes_first():
    """Either cap alone ends the trial."""
    budget = SessionBudget.plan_default()
    assert budget.check(
        elapsed_ms=PLAN_WALL_CLOCK_MS, model_requests=0, tool_calls=0, ai_credits=None
    )
    assert budget.check(
        elapsed_ms=0, model_requests=0, tool_calls=PLAN_MAX_TOOL_CALLS, ai_credits=None
    )


def test_positive_control_over_budget_run_terminates_and_is_classified():
    """POSITIVE CONTROL (brief 2): a run that exceeds its budget must actually
    terminate and be classified as budget-exhausted.

    The budget here is one millisecond, and the loop would otherwise run
    forever. If termination did not work, this test would hang rather than
    fail, so it is wrapped in a timeout: a hang is reported as a failure.
    """

    async def run() -> tuple[int, str | None]:
        budget = SessionBudget(wall_clock_ms=1.0)
        started = time.monotonic()
        iterations = 0
        stop: str | None = None
        while stop is None:
            iterations += 1
            elapsed_ms = (time.monotonic() - started) * 1000
            stop = budget.check(
                elapsed_ms=elapsed_ms,
                model_requests=0,
                tool_calls=0,
                ai_credits=None,
            )
            await asyncio.sleep(0.001)
            if iterations > 10_000:  # pragma: no cover - the cap never fires
                raise AssertionError("budget never terminated the loop")
        return iterations, stop

    iterations, stop = asyncio.run(asyncio.wait_for(run(), timeout=10))
    assert stop is not None
    assert "wall-clock budget exhausted" in stop

    outcome = score_trial(
        terminal_class="budget_exhaustion",
        recorder=_recorder(),
        gate_result=_passing_gate(),
        budget_stop_reason=stop,
    )
    assert outcome.terminal_class == "budget_exhaustion"
    assert outcome.scored is False
    assert outcome.to_json_dict()["scoredAsFailure"] is True
    # Valid means the harness worked; the trial failed on its own terms.
    assert outcome.valid is True


# --- scoring ------------------------------------------------------------------


def test_a_valid_submission_scores():
    outcome = score_trial(
        terminal_class="validated_success",
        recorder=_recorder(_valid()),
        gate_result=_passing_gate(),
    )
    assert outcome.scored is True
    assert outcome.terminal_class == "submitted"
    assert outcome.submission["component"] == "cart"


def test_no_submission_scores_as_failure():
    outcome = score_trial(
        terminal_class="validated_success",
        recorder=_recorder(),
        gate_result=_passing_gate(),
    )
    assert outcome.scored is False
    assert outcome.terminal_class == "no_submission"


def test_invalid_output_scores_as_failure_but_is_distinguishable():
    """Invalid output and no output are both failures, and must stay separable.

    An arm that fails entirely on rejected submissions is a harness artefact,
    not a weak treatment.
    """
    outcome = score_trial(
        terminal_class="validated_success",
        recorder=_recorder({"faultPresent": "yes"}),
        gate_result=_passing_gate(),
    )
    assert outcome.scored is False
    assert outcome.terminal_class == "invalid_submission"
    assert outcome.rejected_attempts == 1


def test_a_failed_sandbox_gate_invalidates_rather_than_fails_the_answer():
    """A broken harness must not look like a weak treatment arm."""
    gate = SandboxGate()  # no application recorded
    outcome = score_trial(
        terminal_class="validated_success",
        recorder=_recorder(_valid()),
        gate_result=gate.evaluate(),
    )
    assert outcome.scored is False
    assert outcome.valid is False
    assert any("sandbox gate" in r for r in outcome.reasons)


def test_a_submission_survives_a_later_budget_stop():
    """An answer that existed before the clock expired is still an answer."""
    outcome = score_trial(
        terminal_class="budget_exhaustion",
        recorder=_recorder(_valid()),
        gate_result=_passing_gate(),
        budget_stop_reason="wall-clock budget exhausted",
    )
    assert outcome.scored is True
    assert outcome.terminal_class == "submitted"


def test_a_session_error_invalidates_the_trial():
    outcome = score_trial(
        terminal_class="error",
        recorder=_recorder(),
        gate_result=_passing_gate(),
        error="transport closed",
    )
    assert outcome.valid is False
    assert outcome.terminal_class == "error"


def test_outcome_serializes_the_gate_and_the_budget_reason():
    payload = score_trial(
        terminal_class="budget_exhaustion",
        recorder=_recorder(),
        gate_result=_passing_gate(),
        budget_stop_reason="tool-call budget exhausted: 100 >= 100",
    ).to_json_dict()
    assert payload["budgetStopReason"].startswith("tool-call")
    assert payload["sandboxGate"]["passed"] is True


# --- the scored shell configuration: screen off, sandbox verified per command --


def _gate(*, static_screen="on", shell_enabled=False, flags=("true",)):
    """A gate with one execution per entry in ``flags``.

    ``flags`` entries are the ``sandboxApplied`` values the runtime reported;
    ``None`` means the telemetry carried no flag at all, which is a different
    failure from reporting ``"false"`` and is kept distinguishable.
    """
    gate = SandboxGate(static_screen=static_screen, shell_enabled=shell_enabled)
    gate.record_application(
        SandboxApplication(
            requested={}, succeeded=True, applied_before_first_prompt=True
        )
    )
    for index, flag in enumerate(flags):
        properties = {} if flag is None else {"sandboxApplied": flag}
        gate.observe_tool_execution(
            {"toolCallId": f"t{index}", "toolTelemetry": {"properties": properties}},
            tool_name="bash",
        )
    return gate


def test_screen_off_trial_is_valid_when_every_execution_is_confirmed():
    """The permissive configuration must still be able to pass.

    Without this the next test would be satisfied by a gate that fails on
    everything, which would prove nothing about the condition being tested.
    """
    result = _gate(
        static_screen="off", shell_enabled=True, flags=("true", "true", "true")
    ).evaluate()
    assert result.passed is True
    outcome = score_trial(
        terminal_class="validated_success",
        recorder=_recorder(_valid()),
        gate_result=result,
        shell_enabled=True,
        static_screen="off",
    )
    assert outcome.scored is True
    assert outcome.valid is True


@pytest.mark.parametrize("bad_flag", ["false", None])
def test_one_unconfirmed_execution_among_confirmed_ones_invalidates_the_trial(bad_flag):
    """The positive control: a single unconfirmed command spoils the trial.

    The unconfirmed execution is deliberately placed *between* confirmed ones.
    A gate that only inspected the first or last execution would pass this, and
    a majority-style check would too, since two of three are confirmed.
    """
    result = _gate(
        static_screen="off", shell_enabled=True, flags=("true", bad_flag, "true")
    ).evaluate()
    assert result.passed is False
    assert result.tool_executions_observed == 3
    assert result.tool_executions_confirmed == 2

    outcome = score_trial(
        terminal_class="validated_success",
        recorder=_recorder(_valid()),
        gate_result=result,
        shell_enabled=True,
        static_screen="off",
    )
    assert outcome.valid is False
    assert outcome.scored is False
    # Counted as the harness's failure, not charged to the agent, whose own
    # outcome is preserved rather than overwritten.
    assert outcome.terminal_class == "harness_failure"
    assert outcome.agent_terminal_class == "submitted"
    assert outcome.to_json_dict()["harnessFailure"] is True


def test_screen_off_without_a_gate_result_is_a_harness_failure():
    """Omitting the gate must not be the way to skip the requirement.

    This is the hole the module docstring warned about: with shell enabled and
    the screen off, the sandbox is the only boundary, so "no gate result" is
    absence of evidence and cannot score.
    """
    outcome = score_trial(
        terminal_class="validated_success",
        recorder=_recorder(_valid()),
        gate_result=None,
        shell_enabled=True,
        static_screen="off",
    )
    assert outcome.valid is False
    assert outcome.terminal_class == "harness_failure"
    assert any("never confirmed" in r for r in outcome.reasons)


def test_a_shell_disabled_trial_still_scores_without_a_gate():
    """The rule is scoped to the configuration that needs it.

    A trial with no shell has no shell commands to confine, so requiring a gate
    there would fail trials for missing evidence they could not produce.
    """
    outcome = score_trial(
        terminal_class="validated_success",
        recorder=_recorder(_valid()),
        gate_result=None,
    )
    assert outcome.scored is True
    assert outcome.valid is True


def test_the_default_configuration_keeps_the_screen_on():
    """The permissive setting must be chosen, never inherited."""
    assert SandboxGate().static_screen == "on"
    assert SandboxGate().shell_enabled is False
    assert SandboxGate().screen_off_with_shell is False

    outcome = score_trial(
        terminal_class="validated_success", recorder=_recorder(_valid())
    )
    assert outcome.static_screen == "on"
    assert outcome.shell_enabled is False
    assert outcome.to_json_dict()["staticScreen"] == "on"


def test_screen_off_cannot_waive_its_own_evidence_requirement():
    """`require_evidence=False` must not buy a vacuous pass in this mode.

    A gate that observed nothing has confirmed nothing. Letting the caller opt
    out would make the strictest configuration the easiest one to satisfy.
    """
    vacuous = SandboxGate(
        require_evidence=False, static_screen="off", shell_enabled=True
    )
    vacuous.record_application(
        SandboxApplication(
            requested={}, succeeded=True, applied_before_first_prompt=True
        )
    )
    result = vacuous.evaluate()
    assert result.passed is False
    assert result.vacuous is True

    # The opt-out still works where the sandbox is not the only boundary.
    permitted = SandboxGate(require_evidence=False)
    permitted.record_application(
        SandboxApplication(
            requested={}, succeeded=True, applied_before_first_prompt=True
        )
    )
    assert permitted.evaluate().passed is True


def test_the_trial_record_carries_the_setting_and_every_confirmation():
    """The setting must be readable from the data, not from the config."""
    result = _gate(
        static_screen="off", shell_enabled=True, flags=("true", "false")
    ).evaluate()
    payload = score_trial(
        terminal_class="validated_success",
        recorder=_recorder(_valid()),
        gate_result=result,
        shell_enabled=True,
        static_screen="off",
    ).to_json_dict()

    assert payload["staticScreen"] == "off"
    assert payload["shellEnabled"] is True
    gate_payload = payload["sandboxGate"]
    assert gate_payload["staticScreen"] == "off"

    # Every execution, not only the failures: a reader must be able to tell
    # "all confirmed" from "nothing ran" without trusting a summary count.
    executions = gate_payload["toolExecutions"]
    assert len(executions) == 2
    assert [e["sandboxApplied"] for e in executions] == ["true", "false"]
    assert [e["confirmed"] for e in executions] == [True, False]
    assert executions[0]["toolName"] == "bash"


def test_an_unrecognised_screen_value_is_rejected_rather_than_assumed_safe():
    """A typo must not silently read as the strict setting.

    Defaulting an unknown value to "on" would record a boundary the trial did
    not actually have, which is the more dangerous direction of the two.
    """
    with pytest.raises(ValueError, match="static_screen"):
        SandboxGate(static_screen="offf")
    with pytest.raises(ValueError, match="static_screen"):
        score_trial(
            terminal_class="validated_success",
            recorder=_recorder(_valid()),
            static_screen="OFF",
        )


# --- a command that starts and never completes is not evidence of confinement -


def _started_gate(*, static_screen="off", shell_enabled=True):
    gate = SandboxGate(static_screen=static_screen, shell_enabled=shell_enabled)
    gate.record_application(
        SandboxApplication(
            requested={}, succeeded=True, applied_before_first_prompt=True
        )
    )
    return gate


def _start(gate, call_id, name="bash"):
    gate.observe_tool_execution_start({"toolCallId": call_id, "toolName": name})


def _finish(gate, call_id, flag="true"):
    properties = {} if flag is None else {"sandboxApplied": flag}
    gate.observe_tool_execution(
        {"toolCallId": call_id, "toolTelemetry": {"properties": properties}}
    )


def test_paired_starts_and_completions_still_pass():
    """The positive control for the matching itself.

    Without this, a gate that failed every trial -- or one that treated every
    start as unfinished -- would satisfy the test below while proving nothing.
    """
    gate = _started_gate()
    for call_id in ("t0", "t1", "t2"):
        _start(gate, call_id)
        _finish(gate, call_id)

    result = gate.evaluate()
    assert result.passed is True
    assert result.tool_executions_observed == 3
    assert result.tool_executions_confirmed == 3
    assert all(r["completed"] for r in result.tool_executions)


def test_a_start_with_no_completion_fails_the_permissive_configuration():
    """The hole: the budget kills a command mid-flight and the gate never hears.

    The unfinished start is placed between two confirmed, completed executions,
    so a gate that reasons only over completions sees two of two confirmed and
    passes. That command ran, and whether it was confined is unknown.
    """
    gate = _started_gate()
    _start(gate, "t0")
    _finish(gate, "t0")
    _start(gate, "t1")  # killed mid-command: no completion ever arrives
    _start(gate, "t2")
    _finish(gate, "t2")

    result = gate.evaluate()
    assert result.passed is False
    assert result.tool_executions_observed == 3
    assert result.tool_executions_confirmed == 2
    assert "never completed" in (result.reason or "")

    unfinished = [r for r in result.tool_executions if not r["completed"]]
    assert [r["toolCallId"] for r in unfinished] == ["t1"]
    # "never finished" must stay distinct from "the runtime reported nothing":
    # the first is a harness or budget event, the second is a runtime fault.
    assert unfinished[0]["sandboxApplied"] is None
    assert unfinished[0]["confirmed"] is False

    outcome = score_trial(
        terminal_class="budget_exhaustion",
        recorder=_recorder(),
        gate_result=result,
        shell_enabled=True,
        static_screen="off",
    )
    assert outcome.valid is False
    assert outcome.terminal_class == "harness_failure"


def test_an_unfinished_start_is_unconfirmed_in_every_configuration():
    """Recorded the same way whether or not the screen was the boundary."""
    gate = _started_gate(static_screen="on", shell_enabled=False)
    _start(gate, "t0")
    _finish(gate, "t0")
    _start(gate, "t1")

    result = gate.evaluate()
    assert result.passed is False
    assert result.tool_executions_confirmed == 1
    assert [r["completed"] for r in result.tool_executions] == [True, False]


def test_a_completion_with_no_start_is_recorded_rather_than_dropped():
    """An orphan completion means the stream is not what we think it is.

    It is not treated as a failure on its own -- the execution did report its
    flag -- but it is marked, because silently normalising it would hide the
    fact that we are missing events.
    """
    gate = _started_gate()
    _finish(gate, "t0")

    result = gate.evaluate()
    assert result.tool_executions_observed == 1
    assert result.tool_executions[0]["startObserved"] is False
    assert result.tool_executions[0]["confirmed"] is True
    assert result.passed is True


def test_an_id_less_start_cannot_be_matched_and_so_is_unconfirmed():
    """A start with no toolCallId can never pair with a completion.

    Dropping it would be the same hole in a different shape: the execution ran
    and the gate would have nothing to object to.
    """
    gate = _started_gate()
    _start(gate, "t0")
    _finish(gate, "t0")
    gate.observe_tool_execution_start({"toolName": "bash"})

    result = gate.evaluate()
    assert result.passed is False
    assert result.tool_executions_observed == 2
    assert result.tool_executions_confirmed == 1


def test_a_repeated_start_for_one_call_id_counts_once():
    """A retried start must not inflate the execution count.

    Attempts are the timeline's business. The gate asks a different question --
    was every execution confined -- and double-counting one call would make the
    confirmed-versus-observed ratio unreadable.
    """
    gate = _started_gate()
    _start(gate, "t0")
    _start(gate, "t0")
    _finish(gate, "t0")

    result = gate.evaluate()
    assert result.passed is True
    assert result.tool_executions_observed == 1


def test_the_session_records_start_payloads_for_the_gate(tmp_path):
    """The evidence has to reach the gate, not merely exist in the timeline.

    The timeline already knew about unfinished calls; the gate could not see
    them, because only completions were kept. This drives the real event
    handler and then feeds what it captured into a real gate, so it fails if
    the wiring is removed -- which a check on the timeline alone would not.
    """
    from radius_perf_eval.copilot import SpikeSession, TemporaryWorkspace
    from radius_perf_eval.events import EventRecorder

    workspace = TemporaryWorkspace()
    recorder = EventRecorder(tmp_path / "events.jsonl")
    try:
        session = SpikeSession(
            client=object(),
            workspace=workspace,
            recorder=recorder,
            model="test-model",
        )
        session._on_event(_event("tool.execution_start", {"toolCallId": "t0"}))
        session._on_event(
            _event(
                "tool.execution_complete",
                {
                    "toolCallId": "t0",
                    "toolTelemetry": {"properties": {"sandboxApplied": "true"}},
                },
            )
        )
        # Starts and never completes: the case the gate must be able to see.
        session._on_event(_event("tool.execution_start", {"toolCallId": "t1"}))
    finally:
        recorder.close()
        workspace.destroy()

    assert [p["toolCallId"] for p in session.tool_execution_starts] == ["t0", "t1"]

    gate = _started_gate()
    for payload in session.tool_execution_starts:
        gate.observe_tool_execution_start(payload)
    for payload in session.tool_executions:
        gate.observe_tool_execution(payload)

    result = gate.evaluate()
    assert result.passed is False
    assert result.tool_executions_observed == 2
    assert result.tool_executions_confirmed == 1
