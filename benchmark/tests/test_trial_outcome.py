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
