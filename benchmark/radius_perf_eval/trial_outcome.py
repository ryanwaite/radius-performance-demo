"""Turn one agent execution into a scored trial result.

Three independent things can end a trial, and the plan scores all three as
failures:

* the budget ran out (30 minutes or 100 tool calls);
* the agent never produced a valid submission;
* the sandbox gate could not confirm confinement.

They are kept as separate reasons rather than collapsed into a single boolean,
because "ran out of time" and "answered wrongly" are different findings about a
harness and would otherwise be indistinguishable in the results.

**Invalid is not absent.** An agent that called the submit tool with malformed
fields scores as a failure, exactly like one that never called it, but the
record keeps the rejected attempts so a reader can tell "could not diagnose"
from "could not express". If a whole arm fails on rejected submissions, that is
a harness artefact, not evidence about the treatment.

**The gate is not a scorer.** A sandbox-gate failure means the trial produced no
trustworthy evidence, so it is reported as invalid rather than as a wrong
answer. Scoring it as a wrong answer would let a broken harness masquerade as a
weak treatment arm. Such a trial takes the terminal class ``harness_failure``
rather than the agent's, so it is counted where it belongs; the class the agent
would have earned is kept in ``agentTerminalClass`` so nothing is lost.

**Shell trials run with the static screen off**, because the screen denies
ordinary ``/proc`` and cgroup reads while missing the assembled paths the
sandbox denies, and because the arms do not depend on it equally. The sandbox is
then the only boundary on shell, so a shell trial is valid only when every tool
execution reports ``sandboxApplied: "true"``. A trial in that configuration with
no gate result is a harness failure, not a pass: omitting the gate is exactly
how the confinement requirement would otherwise be skipped silently.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "FAILURE_CLASSES",
    "TrialOutcome",
    "score_trial",
]

#: Terminal classes that score as a failure rather than as a scored answer.
#: ``harness_failure`` is excluded from the agent's results rather than charged
#: to it, but it is still counted, so a harness that quietly invalidates one
#: arm's trials cannot look like an arm that simply performed badly.
FAILURE_CLASSES = (
    "budget_exhaustion",
    "no_submission",
    "invalid_submission",
    "error",
    "harness_failure",
)


@dataclass
class TrialOutcome:
    """The scored result of one trial."""

    scored: bool
    """True only when the trial produced a submission that can be graded."""

    valid: bool
    """False when the harness itself failed, so the trial yields no evidence."""

    terminal_class: str
    reasons: list[str] = field(default_factory=list)
    submission: dict[str, Any] | None = None
    rejected_attempts: int = 0
    budget_stop_reason: str | None = None
    sandbox_gate: dict[str, Any] | None = None
    static_screen: str = "on"
    shell_enabled: bool = False
    agent_terminal_class: str | None = None
    """What the agent earned, kept when a harness failure overrides the class."""

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "scored": self.scored,
            "valid": self.valid,
            "terminalClass": self.terminal_class,
            "agentTerminalClass": self.agent_terminal_class,
            "scoredAsFailure": not self.scored,
            "harnessFailure": self.terminal_class == "harness_failure",
            "reasons": list(self.reasons),
            "submission": self.submission,
            "rejectedSubmissionAttempts": self.rejected_attempts,
            "budgetStopReason": self.budget_stop_reason,
            "sandboxGate": self.sandbox_gate,
            # Recorded at the top level as well as inside the gate, because a
            # trial with no gate result still has to say which boundary it ran
            # under -- that case is precisely the one worth catching.
            "staticScreen": self.static_screen,
            "shellEnabled": self.shell_enabled,
        }


def score_trial(
    *,
    terminal_class: str,
    recorder: Any,
    gate_result: Any = None,
    budget_stop_reason: str | None = None,
    error: str | None = None,
    shell_enabled: bool = False,
    static_screen: str = "on",
) -> TrialOutcome:
    """Combine budget, submission, and sandbox state into one scored result.

    ``gate_result`` stays optional so a shell-disabled trial, which has no shell
    commands to confine, can be scored. It is not optional in the scored shell
    configuration: with shell enabled and the screen off the sandbox is the only
    boundary, so a missing gate is treated as a harness failure rather than
    waved through. Passing a gate result that did not pass has the same effect.
    """
    if static_screen not in ("on", "off"):
        raise ValueError(
            f"static_screen must be 'on' or 'off', got {static_screen!r}"
        )

    reasons: list[str] = []
    # Reasons the *harness* failed, kept apart from the agent's outcome so the
    # two are never conflated in the count.
    harness_reasons: list[str] = []

    if gate_result is not None and not gate_result.passed:
        harness_reasons.append(f"sandbox gate: {gate_result.reason}")
    elif gate_result is None and shell_enabled and static_screen == "off":
        harness_reasons.append(
            "shell was enabled with the static screen off, but no sandbox gate "
            "result was recorded, so confinement was never confirmed"
        )

    submission = getattr(recorder, "submission", None)
    rejected = int(getattr(recorder, "rejected_attempts", 0) or 0)

    if error:
        agent_class = "error"
        reasons.append(f"session error: {error}")
    elif submission is not None:
        # An accepted submission is the agent's answer even if the budget later
        # expired during teardown: the answer existed before the clock did.
        agent_class = "submitted"
    elif terminal_class == "budget_exhaustion":
        agent_class = "budget_exhaustion"
        reasons.append(budget_stop_reason or "budget exhausted before submission")
    elif rejected:
        agent_class = "invalid_submission"
        reasons.append(
            f"{rejected} submission attempt(s) were rejected and none was valid"
        )
    else:
        agent_class = "no_submission"
        reasons.append("the agent never called the submit tool")

    valid = not harness_reasons and agent_class != "error"
    if harness_reasons:
        # A trial that cannot confirm confinement says nothing about the agent,
        # so it is counted as a harness failure instead of as the agent's class.
        resolved = "harness_failure"
        reasons = harness_reasons + reasons
    else:
        resolved = agent_class

    scored = resolved == "submitted" and valid
    return TrialOutcome(
        scored=scored,
        valid=valid,
        terminal_class=resolved,
        reasons=reasons,
        submission=submission.to_json_dict() if submission is not None else None,
        rejected_attempts=rejected,
        budget_stop_reason=budget_stop_reason,
        sandbox_gate=(
            gate_result.to_json_dict() if gate_result is not None else None
        ),
        static_screen=static_screen,
        shell_enabled=shell_enabled,
        agent_terminal_class=agent_class,
    )
