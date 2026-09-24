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
weak treatment arm.
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
FAILURE_CLASSES = (
    "budget_exhaustion",
    "no_submission",
    "invalid_submission",
    "error",
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

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "scored": self.scored,
            "valid": self.valid,
            "terminalClass": self.terminal_class,
            "scoredAsFailure": not self.scored,
            "reasons": list(self.reasons),
            "submission": self.submission,
            "rejectedSubmissionAttempts": self.rejected_attempts,
            "budgetStopReason": self.budget_stop_reason,
            "sandboxGate": self.sandbox_gate,
        }


def score_trial(
    *,
    terminal_class: str,
    recorder: Any,
    gate_result: Any = None,
    budget_stop_reason: str | None = None,
    error: str | None = None,
) -> TrialOutcome:
    """Combine budget, submission, and sandbox state into one scored result.

    ``gate_result`` is optional only so a shell-disabled trial, which has no
    tool executions to confirm, can be scored. When shell is enabled the caller
    must pass it; omitting it would silently skip the confinement requirement,
    which is the failure mode the plan's vacuity rule exists to prevent.
    """
    reasons: list[str] = []
    valid = True

    if gate_result is not None and not gate_result.passed:
        valid = False
        reasons.append(f"sandbox gate: {gate_result.reason}")

    submission = getattr(recorder, "submission", None)
    rejected = int(getattr(recorder, "rejected_attempts", 0) or 0)

    if error:
        resolved = "error"
        reasons.append(f"session error: {error}")
        valid = False
    elif submission is not None:
        # An accepted submission is the agent's answer even if the budget later
        # expired during teardown: the answer existed before the clock did.
        resolved = "submitted"
    elif terminal_class == "budget_exhaustion":
        resolved = "budget_exhaustion"
        reasons.append(budget_stop_reason or "budget exhausted before submission")
    elif rejected:
        resolved = "invalid_submission"
        reasons.append(
            f"{rejected} submission attempt(s) were rejected and none was valid"
        )
    else:
        resolved = "no_submission"
        reasons.append("the agent never called the submit tool")

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
    )
