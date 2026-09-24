"""The submit tool: the only channel through which a trial produces an answer.

The agent answers by calling one tool with fixed fields. Free-text answers are
not scored, so a trial that ends without a valid submission is a failure, and so
is a submission the schema rejects.

Two design points are worth stating because they are easy to get backwards.

*The tool is terminal on success only.* ``Tool.is_terminal`` halts the agent
turn when the call succeeds, so a valid submission ends the trial immediately
and nothing the model says afterwards can change the recorded answer. A
*failed* call leaves the loop running, so a malformed submission is returned to
the model as an error it can correct rather than silently ending the trial. That
asymmetry is what lets validation be strict without making schema fluency the
thing being measured.

*The component vocabulary belongs to the fixture, not to this module.* The
Compose arm names a service ``cartservice`` while the Radius arm names the same
thing through a resource id. If the harness carried that mapping, the harness
would be encoding one arm's naming into the scorer and an arm could fail for
saying the right thing in its own vocabulary. The table is supplied per fixture
and this module refuses to score a component without one.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any

__all__ = [
    "CAUSAL_CATEGORIES",
    "CATEGORY_DEFINITIONS",
    "CATEGORY_DISAMBIGUATION",
    "ComponentMap",
    "Citation",
    "Submission",
    "SubmissionError",
    "SubmissionRecorder",
    "validate_submission",
    "submit_tool_schema",
    "build_submit_tool",
    "SUBMIT_TOOL_NAME",
    "SUBMIT_TOOL_DESCRIPTION",
]


#: The fixed causal vocabulary, identical in every arm, with the definition that
#: makes each category disjoint from its neighbours.
#:
#: The definitions are the point. Without them a slow database satisfies both
#: ``dependency_latency`` and ``slow_database``, and a database lock satisfies
#: both ``slow_database`` and ``lock_contention`` -- so an arm could be marked
#: wrong for choosing the other true label, and the measured difference between
#: arms would partly be a difference in guessing the scorer's taste. Each
#: definition therefore carves on *where the delay or failure originates*, which
#: is a property of the incident rather than of the wording.
#:
#: The list is closed. An open vocabulary would make agreement a judgement call
#: and let a scorer be generous to one arm's phrasing.
CATEGORY_DEFINITIONS: Mapping[str, str] = MappingProxyType(
    {
        "cpu_saturation": (
            "compute demand exceeds the CPU available to a service, including a "
            "CPU limit set too low, so requests wait for the processor"
        ),
        "memory_exhaustion": (
            "the component runs short of memory -- limit reached, out-of-memory "
            "kill, swapping, or unbounded growth"
        ),
        "garbage_collection": (
            "runtime GC pauses or GC CPU overhead dominate, caused by collector "
            "behaviour or explicit collection rather than by a leak; a leak is "
            "memory_exhaustion"
        ),
        "dependency_latency": (
            "a call to a non-database downstream service, or the network path "
            "between components, is slow while the calling component is healthy"
        ),
        "slow_database": (
            "latency originating inside the database engine, including its own "
            "locks, query plans, and storage I/O"
        ),
        "queue_backlog": (
            "work is produced faster than consumers drain it, so the delay is "
            "time spent waiting in the queue rather than time spent serving"
        ),
        "cache_failure": (
            "the cache stops serving hits -- unavailable, cold, evicting, or "
            "misconfigured -- pushing load onto the origin"
        ),
        "lock_contention": (
            "in-process contention inside an application service, such as mutex "
            "waits or exhaustion of a thread or connection pool"
        ),
        "partial_errors": (
            "a fraction of requests fail or are retried while the rest succeed, "
            "so the fault appears as an error rate rather than uniform slowness"
        ),
        "load_surge": (
            "offered load rises beyond what the deployment is provisioned for, "
            "and every component behaves correctly for the load it receives"
        ),
    }
)

#: Tie-breaks for the cases where two definitions could still both be read as
#: fitting. These are stated to the agent verbatim and are identical in every
#: arm, so no arm has to infer the scorer's convention.
CATEGORY_DISAMBIGUATION: tuple[str, ...] = (
    "Choose where the delay or failure originates, not where it is observed: a "
    "service that is slow only because it is waiting on another names the other "
    "one's category.",
    "If every component would be healthy at normal load and the only change "
    "needed is capacity or admission control, choose load_surge; otherwise "
    "choose the category of the component that must change.",
)

#: Derived from the definitions so a category cannot be added without one.
CAUSAL_CATEGORIES: tuple[str, ...] = tuple(CATEGORY_DEFINITIONS)


class SubmissionError(ValueError):
    """A submission that cannot be scored."""


@dataclass(frozen=True)
class ComponentMap:
    """Fixture-supplied mapping from arm-local component names to canonical ones.

    Keys are matched case-insensitively and with surrounding whitespace removed,
    because that much normalization cannot favour an arm. Anything beyond it --
    substring matching, stripping a resource-id prefix -- would be the harness
    guessing at one arm's naming scheme, so an unrecognized name is rejected
    instead.
    """

    aliases: Mapping[str, str]

    @staticmethod
    def from_mapping(raw: Mapping[str, str]) -> ComponentMap:
        if not raw:
            raise SubmissionError("component map is empty")
        aliases: dict[str, str] = {}
        for alias, canonical in raw.items():
            if not isinstance(alias, str) or not isinstance(canonical, str):
                raise SubmissionError("component map entries must be strings")
            key = alias.strip().lower()
            if not key or not canonical.strip():
                raise SubmissionError("component map contains an empty name")
            aliases[key] = canonical.strip()
        # Canonical names must themselves resolve, so a submission may name the
        # canonical form directly.
        for canonical in list(aliases.values()):
            aliases.setdefault(canonical.strip().lower(), canonical)
        return ComponentMap(aliases)

    @staticmethod
    def from_json_file(path: Path) -> ComponentMap:
        data = json.loads(Path(path).read_text())
        if not isinstance(data, Mapping):
            raise SubmissionError(f"{path} does not contain a JSON object")
        return ComponentMap.from_mapping(data)

    def canonical(self, name: str) -> str:
        key = name.strip().lower()
        if key not in self.aliases:
            # The rejection must not name the valid components either: an agent
            # that guessed wrong would otherwise be handed the inventory it
            # failed to discover, and a single throwaway guess would buy it.
            raise SubmissionError(
                "unknown component; name a service from the application"
            )
        return self.aliases[key]

    @property
    def canonical_names(self) -> tuple[str, ...]:
        return tuple(sorted(set(self.aliases.values())))


@dataclass(frozen=True)
class Citation:
    signal: str
    observation: str

    def to_json_dict(self) -> dict[str, str]:
        return {"signal": self.signal, "observation": self.observation}


@dataclass(frozen=True)
class Submission:
    """A validated answer."""

    fault_present: bool
    causal_category: str | None
    component: str | None
    component_as_submitted: str | None
    evidence: tuple[Citation, ...]
    confidence: float
    remediation: str | None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "faultPresent": self.fault_present,
            "causalCategory": self.causal_category,
            "component": self.component,
            "componentAsSubmitted": self.component_as_submitted,
            "evidence": [c.to_json_dict() for c in self.evidence],
            "confidence": self.confidence,
            "remediation": self.remediation,
        }


def _require_str(value: Any, field_name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise SubmissionError(f"{field_name} must be a non-empty string")
    return value.strip()


def validate_submission(
    payload: Mapping[str, Any], *, component_map: ComponentMap
) -> Submission:
    """Validate one submit call, or raise :class:`SubmissionError`.

    The no-fault case is validated as its own shape rather than as a degenerate
    fault. About one trial in eight is a no-fault control whose correct answer is
    that nothing is wrong, and a schema that demanded a causal category would
    force those trials to assert a cause that does not exist.
    """
    if not isinstance(payload, Mapping):
        raise SubmissionError("submission must be an object")

    fault_present = payload.get("faultPresent")
    if not isinstance(fault_present, bool):
        raise SubmissionError("faultPresent must be a boolean")

    confidence = payload.get("confidence")
    if isinstance(confidence, bool) or not isinstance(confidence, (int, float)):
        raise SubmissionError("confidence must be a number between 0 and 1")
    confidence = float(confidence)
    if not 0.0 <= confidence <= 1.0:
        raise SubmissionError(f"confidence {confidence} is outside 0..1")

    raw_evidence = payload.get("evidence")
    if not isinstance(raw_evidence, Sequence) or isinstance(raw_evidence, (str, bytes)):
        raise SubmissionError("evidence must be a list of citations")
    if not raw_evidence:
        # Required in both directions: "there is no fault" is a claim that needs
        # support just as much as a diagnosis does.
        raise SubmissionError("evidence must contain at least one citation")
    citations: list[Citation] = []
    for index, item in enumerate(raw_evidence):
        if not isinstance(item, Mapping):
            raise SubmissionError(
                f"evidence[{index}] must be an object with 'signal' and 'observation'"
            )
        citations.append(
            Citation(
                signal=_require_str(item.get("signal"), f"evidence[{index}].signal"),
                observation=_require_str(
                    item.get("observation"), f"evidence[{index}].observation"
                ),
            )
        )

    category = payload.get("causalCategory")
    component_raw = payload.get("component")
    remediation = payload.get("remediation")

    if fault_present:
        if not isinstance(category, str) or category not in CAUSAL_CATEGORIES:
            raise SubmissionError(
                f"causalCategory must be one of {', '.join(CAUSAL_CATEGORIES)}"
            )
        component_as_submitted = _require_str(component_raw, "component")
        component = component_map.canonical(component_as_submitted)
        remediation_text = _require_str(remediation, "remediation")
    else:
        if category is not None:
            raise SubmissionError(
                "causalCategory must be omitted when faultPresent is false"
            )
        if component_raw is not None:
            raise SubmissionError(
                "component must be omitted when faultPresent is false"
            )
        if remediation is not None and str(remediation).strip():
            raise SubmissionError(
                "remediation must be omitted when faultPresent is false"
            )
        component = None
        component_as_submitted = None
        remediation_text = None

    return Submission(
        fault_present=fault_present,
        causal_category=category if fault_present else None,
        component=component,
        component_as_submitted=component_as_submitted,
        evidence=tuple(citations),
        confidence=confidence,
        remediation=remediation_text,
    )


def submit_tool_schema() -> dict[str, Any]:
    """JSON schema advertised to the model.

    This function takes no fixture input, and that is deliberate. The canonical
    component names are the application's service inventory, which is part of
    what the Radius graph and the architecture document supply to their arms.
    Listing them here would hand that inventory to the native arm too, shrinking
    the difference the experiment exists to measure. Because the schema is built
    from module constants alone, it is byte-identical in every arm and cannot
    leak a fixture's vocabulary even by accident.

    ``component`` is therefore unconstrained: an arm answers in its own
    vocabulary -- Compose service name, container name, or Radius resource id --
    and the fixture's mapping resolves it after the fact.
    """
    category_lines = "\n".join(
        f"- {name}: {definition}" for name, definition in CATEGORY_DEFINITIONS.items()
    )
    disambiguation = " ".join(CATEGORY_DISAMBIGUATION)
    return {
        "type": "object",
        "properties": {
            "faultPresent": {
                "type": "boolean",
                "description": "Whether the application has a real problem.",
            },
            "causalCategory": {
                "type": "string",
                "enum": list(CAUSAL_CATEGORIES),
                "description": (
                    "The kind of fault, chosen from the list below. Exactly one "
                    "applies.\n"
                    f"{category_lines}\n"
                    f"{disambiguation}\n"
                    "Omit entirely when faultPresent is false."
                ),
            },
            "component": {
                "type": "string",
                "description": (
                    "The component whose behaviour must change to fix the "
                    "fault. Name it however the application names it: a "
                    "service name, a container name, or a resource id are all "
                    "accepted. Omit when faultPresent is false."
                ),
            },
            "evidence": {
                "type": "array",
                "minItems": 1,
                "items": {
                    "type": "object",
                    "properties": {
                        "signal": {
                            "type": "string",
                            "description": "The metric, trace, or log consulted.",
                        },
                        "observation": {
                            "type": "string",
                            "description": "What that signal showed.",
                        },
                    },
                    "required": ["signal", "observation"],
                },
            },
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "remediation": {
                "type": "string",
                "description": (
                    "The smallest safe change that restores the objective. "
                    "Omit when faultPresent is false."
                ),
            },
        },
        "required": ["faultPresent", "evidence", "confidence"],
    }


@dataclass
class SubmissionRecorder:
    """Holds every submit attempt and the first valid one.

    Every attempt is kept, valid or not. The count of rejected attempts is a
    measure in its own right -- it separates an agent that could not diagnose
    the fault from one that diagnosed it and could not express it -- and a
    recorder that kept only the accepted answer would hide that difference.
    """

    component_map: ComponentMap
    attempts: list[dict[str, Any]] = field(default_factory=list)
    submission: Submission | None = None

    def record(self, payload: Mapping[str, Any]) -> tuple[bool, str | None]:
        """Validate and record one attempt. Returns ``(accepted, error)``."""
        if self.submission is not None:
            # First valid submission wins. The tool is terminal on success, so a
            # second one should not be reachable; if it is, the answer is not
            # revised after the fact.
            self.attempts.append(
                {"payload": dict(payload), "accepted": False, "error": "already submitted"}
            )
            return False, "a submission has already been accepted"
        try:
            submission = validate_submission(payload, component_map=self.component_map)
        except SubmissionError as exc:
            self.attempts.append(
                {"payload": dict(payload), "accepted": False, "error": str(exc)}
            )
            return False, str(exc)
        self.submission = submission
        self.attempts.append({"payload": dict(payload), "accepted": True, "error": None})
        return True, None

    @property
    def rejected_attempts(self) -> int:
        return sum(1 for a in self.attempts if not a["accepted"])

    @property
    def scored_as_failure(self) -> bool:
        """No valid submission scores as a failure, per the plan.

        Invalid output and no output are both failures, but the attempt log
        keeps them distinguishable.
        """
        return self.submission is None

    def outcome(self) -> dict[str, Any]:
        """The scoring view of what the agent submitted."""
        return {
            "submitted": self.submission is not None,
            "submission": self.submission.to_json_dict() if self.submission else None,
            "attempts": len(self.attempts),
            "rejectedAttempts": self.rejected_attempts,
            "attemptLog": list(self.attempts),
            # A trial that never submits, or only ever submitted invalid output,
            # is a failure rather than an absence of result.
            "scoredAsFailure": self.submission is None,
        }


SUBMIT_TOOL_NAME = "submit_diagnosis"

SUBMIT_TOOL_DESCRIPTION = (
    "Submit your final answer. Call this exactly once, when you are done "
    "investigating. A successful call ends the task, so do not call it until "
    "your answer is complete. If the call is rejected, read the error, correct "
    "the fields, and call it again."
)


def build_submit_tool(recorder: SubmissionRecorder, *, event_recorder: Any = None) -> Any:
    """Build the SDK ``Tool`` that receives the agent's answer.

    ``is_terminal`` is set, so an accepted submission halts the agent turn and
    the trial ends on the agent's own answer rather than on a budget. A rejected
    submission returns ``result_type="failure"``, which leaves the loop running
    so the model can correct it.
    """
    from copilot.tools import Tool, ToolResult

    def handler(invocation: Any) -> Any:
        arguments = getattr(invocation, "arguments", None)
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError as exc:
                if event_recorder is not None:
                    event_recorder.record(
                        "harness",
                        "submit.rejected",
                        {"error": f"arguments were not valid JSON: {exc}"},
                    )
                return ToolResult(
                    text_result_for_llm=f"Submission rejected: invalid JSON ({exc}).",
                    result_type="failure",
                    error="invalid JSON",
                )
        if not isinstance(arguments, Mapping):
            arguments = {}

        accepted, error = recorder.record(arguments)
        if event_recorder is not None:
            event_recorder.record(
                "harness",
                "submit.accepted" if accepted else "submit.rejected",
                {"arguments": dict(arguments), "error": error},
            )
        if accepted:
            return ToolResult(
                text_result_for_llm="Submission accepted. The task is complete.",
                result_type="success",
            )
        return ToolResult(
            text_result_for_llm=(
                f"Submission rejected: {error}. Correct the fields and submit again."
            ),
            result_type="failure",
            error=error,
        )

    return Tool(
        name=SUBMIT_TOOL_NAME,
        description=SUBMIT_TOOL_DESCRIPTION,
        handler=handler,
        parameters=submit_tool_schema(),
        is_terminal=True,
        # The submission is the measured output, so it must never be blocked on
        # a permission prompt the harness would have to answer.
        skip_permission=True,
    )
