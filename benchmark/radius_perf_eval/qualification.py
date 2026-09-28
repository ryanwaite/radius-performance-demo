"""Fingerprint drift, re-qualification records, and the scored-start gate.

`hostclass` answers two different questions with two different identifiers,
and this module is what makes the second one mean something.

The class id is the performance envelope. It keys the frozen tolerance sets,
and it is deliberately strict: a machine with a different CPU, core count, or
container-runtime memory ceiling is a different class and gets no verdict
until bounds are fitted on it.

The fingerprint is the class plus every patch-level version -- operating
system release, Docker engine, Python. These move on their own. A Homebrew
update and a Docker Desktop update both landed between the catalog app's
holdout and the check that followed it, and neither moved the measured
numbers, which is why a patch bump does not void a frozen set.

That leaves a hazard. An identifier that is recorded but never acted on is
decoration, and a version bump that *does* move the numbers would be captured
in the report and ignored by everything. So the fingerprint carries three
obligations:

1. Every run compares its fingerprint against the one its tolerance set was
   fitted on, and the report lists each component that differs.
2. A host whose fingerprint has drifted still gets a verdict, but scored
   trials refuse to start on it until a short re-qualification run -- three
   cycles against the unchanged frozen tolerances -- has passed and been
   recorded.
3. During a scored campaign the versions are pinned so the fingerprint cannot
   move underneath a run. See the README for the commands.

Re-qualification records are machine-local state, not source. A record says
"this physical machine, at this fingerprint, passed a short check against
these frozen bounds". That is a fact about one host, so it lives in a state
file on that host rather than in the repository, where it would accumulate
one entry per developer machine and be meaningless to all the others.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .hostclass import HostClassError, HostFacts, derive_class_id, derive_fingerprint

# A re-qualification is a short check, not a re-fit. Three cycles is enough to
# catch a version bump that moved the numbers out of the frozen bounds, and is
# deliberately too few to fit new bounds with -- the point is to confirm the
# existing bounds still hold, not to discover new ones.
MIN_REQUALIFICATION_CYCLES = 3

_STORE_ENV_VAR = "RADIUS_PERF_EVAL_QUALIFICATION_STORE"
_DEFAULT_STORE = Path.home() / ".radius-perf-eval" / "qualifications.json"

# The fingerprint's shape, used to report which component drifted rather than
# just that the strings differ. `derive_fingerprint` joins these with "/".
_FINGERPRINT_COMPONENTS: tuple[str, ...] = (
    "hostClass",
    "osRelease",
    "dockerEngine",
    "pythonVersion",
)


class QualificationError(RuntimeError):
    """Raised when the qualification store cannot be read or written."""


def store_path() -> Path:
    """Where re-qualification records live on this machine.

    Overridable so tests never touch the real store, and so a campaign can
    point several checkouts at one shared record.
    """
    override = os.environ.get(_STORE_ENV_VAR)
    return Path(override).expanduser() if override else _DEFAULT_STORE


@dataclass(frozen=True)
class Requalification:
    """One recorded short check of a fingerprint against frozen bounds."""

    host_class: str
    fingerprint: str
    recorded_at: str
    cycles: int
    suite_id: str
    driver_commit: str
    tolerances_fitted_at_commit: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "hostClass": self.host_class,
            "fingerprint": self.fingerprint,
            "recordedAt": self.recorded_at,
            "cycles": self.cycles,
            "suiteId": self.suite_id,
            "driverCommit": self.driver_commit,
            "tolerancesFittedAtCommit": self.tolerances_fitted_at_commit,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "Requalification":
        return cls(
            host_class=str(payload["hostClass"]),
            fingerprint=str(payload["fingerprint"]),
            recorded_at=str(payload["recordedAt"]),
            cycles=int(payload["cycles"]),
            suite_id=str(payload.get("suiteId", "")),
            driver_commit=str(payload.get("driverCommit", "")),
            tolerances_fitted_at_commit=str(
                payload.get("tolerancesFittedAtCommit", "")
            ),
        )


@dataclass(frozen=True)
class FingerprintComparison:
    """Observed fingerprint against the one the bounds were fitted on."""

    observed: str | None
    fitted: str | None
    matches: bool
    comparable: bool
    drift: tuple[dict[str, str], ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "observed": self.observed,
            "fittedOn": self.fitted,
            "matches": self.matches,
            "comparable": self.comparable,
            "drift": [dict(entry) for entry in self.drift],
        }


def compare_fingerprints(
    observed: str | None, fitted: str | None
) -> FingerprintComparison:
    """Report every component that differs, not just that the strings do.

    An incomparable pair -- either side missing -- is reported as a mismatch,
    never as a match. The catalog app's bounds were fitted before fingerprints
    were captured, so `fitted` is genuinely unknown there, and treating
    unknown as "probably fine" is the failure this module exists to prevent.
    """
    if observed is None or fitted is None:
        return FingerprintComparison(
            observed=observed,
            fitted=fitted,
            matches=False,
            comparable=False,
            drift=(),
        )

    if observed == fitted:
        return FingerprintComparison(
            observed=observed,
            fitted=fitted,
            matches=True,
            comparable=True,
            drift=(),
        )

    observed_parts = observed.split("/")
    fitted_parts = fitted.split("/")
    drift: list[dict[str, str]] = []
    width = max(len(observed_parts), len(fitted_parts))
    for index in range(width):
        left = fitted_parts[index] if index < len(fitted_parts) else ""
        right = observed_parts[index] if index < len(observed_parts) else ""
        if left == right:
            continue
        name = (
            _FINGERPRINT_COMPONENTS[index]
            if index < len(_FINGERPRINT_COMPONENTS)
            else f"component{index}"
        )
        drift.append({"component": name, "fittedOn": left, "observed": right})

    return FingerprintComparison(
        observed=observed,
        fitted=fitted,
        matches=False,
        comparable=True,
        drift=tuple(drift),
    )


def load_requalifications(path: Path | None = None) -> list[Requalification]:
    """Read the machine-local records, tolerating an absent store."""
    target = path or store_path()
    if not target.exists():
        return []
    try:
        payload = json.loads(target.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise QualificationError(
            f"the qualification store at {target} could not be read: {exc}"
        ) from exc
    entries = payload.get("requalifications", []) if isinstance(payload, dict) else []
    records: list[Requalification] = []
    for entry in entries:
        try:
            records.append(Requalification.from_dict(entry))
        except (KeyError, TypeError, ValueError) as exc:
            raise QualificationError(
                f"the qualification store at {target} holds a malformed record: {exc}"
            ) from exc
    return records


def find_requalification(
    records: list[Requalification],
    host_class: str,
    fingerprint: str,
    tolerances_fitted_at_commit: str | None = None,
) -> Requalification | None:
    """The most recent record for exactly this class, fingerprint and bounds.

    The bounds are part of the identity of the check, not metadata about it. A
    re-qualification is a claim that *these* three cycles fell inside *those*
    tolerances. Match on class and fingerprint alone and a record made against
    a superseded set silently clears bounds it never ran against, which is the
    one thing the gate exists to prevent.

    The commit is required rather than optional. Passing ``None`` matches
    nothing, because a caller that cannot say which set is in force cannot be
    told that some record satisfies it.
    """
    if not tolerances_fitted_at_commit:
        return None
    matches = [
        record
        for record in records
        if record.host_class == host_class
        and record.fingerprint == fingerprint
        and record.tolerances_fitted_at_commit == tolerances_fitted_at_commit
        and record.cycles >= MIN_REQUALIFICATION_CYCLES
    ]
    if not matches:
        return None
    return sorted(matches, key=lambda record: record.recorded_at)[-1]


def record_requalification(
    *,
    host_class: str,
    fingerprint: str,
    cycles: int,
    suite_id: str,
    driver_commit: str,
    tolerances_fitted_at_commit: str,
    path: Path | None = None,
) -> Requalification:
    """Append a passing short check to the machine-local store."""
    if cycles < MIN_REQUALIFICATION_CYCLES:
        raise QualificationError(
            f"a re-qualification needs at least {MIN_REQUALIFICATION_CYCLES} "
            f"cycles; this run had {cycles}"
        )
    target = path or store_path()
    existing = load_requalifications(target)
    record = Requalification(
        host_class=host_class,
        fingerprint=fingerprint,
        recorded_at=datetime.now(timezone.utc).isoformat(),
        cycles=cycles,
        suite_id=suite_id,
        driver_commit=driver_commit,
        tolerances_fitted_at_commit=tolerances_fitted_at_commit,
    )
    existing.append(record)
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(
                {"requalifications": [item.to_dict() for item in existing]}, indent=2
            )
            + "\n"
        )
    except OSError as exc:
        raise QualificationError(
            f"the qualification store at {target} could not be written: {exc}"
        ) from exc
    return record


@dataclass(frozen=True)
class ScoredReadiness:
    """Whether scored trials may start on this host, and why or why not."""

    allowed: bool
    reason: str
    fingerprint: FingerprintComparison
    requalification: Requalification | None

    def to_dict(self) -> dict[str, Any]:
        return {
            "allowed": self.allowed,
            "reason": self.reason,
            "fingerprint": self.fingerprint.to_dict(),
            "requalification": (
                self.requalification.to_dict() if self.requalification else None
            ),
            "note": (
                "A drifted fingerprint does not invalidate the verdict; the "
                "frozen bounds still apply to this host class. It blocks "
                "scored trials until a short re-qualification run has passed "
                f"on this fingerprint ({MIN_REQUALIFICATION_CYCLES} cycles "
                "against the unchanged bounds)."
            ),
        }


def evaluate_scored_readiness(
    facts: HostFacts | None,
    *,
    fitted_fingerprint: str | None,
    tolerances_resolved: bool,
    tolerances_fitted_at_commit: str | None = None,
    records: list[Requalification] | None = None,
    path: Path | None = None,
) -> ScoredReadiness:
    """Decide whether a scored campaign may begin on the observed host.

    Evaluated against the records that existed *before* the current run, so a
    run cannot clear its own gate. A drifted run records a re-qualification on
    success and the next run is allowed; this one still says it was blocked.
    """
    observed = derive_fingerprint(facts) if facts is not None else None

    if facts is None:
        return ScoredReadiness(
            allowed=False,
            reason="the host was not observed, so scored trials cannot start",
            fingerprint=compare_fingerprints(None, fitted_fingerprint),
            requalification=None,
        )

    comparison = compare_fingerprints(observed, fitted_fingerprint)

    if not tolerances_resolved:
        return ScoredReadiness(
            allowed=False,
            reason=(
                "this host class has no frozen tolerance set, so there is "
                "nothing for a scored trial to be measured against"
            ),
            fingerprint=comparison,
            requalification=None,
        )

    if comparison.matches:
        return ScoredReadiness(
            allowed=True,
            reason="the fingerprint is unchanged since the bounds were fitted",
            fingerprint=comparison,
            requalification=None,
        )

    try:
        host_class = derive_class_id(facts)
    except HostClassError as exc:
        return ScoredReadiness(
            allowed=False,
            reason=f"the host could not be classified: {exc}",
            fingerprint=comparison,
            requalification=None,
        )

    known = records if records is not None else load_requalifications(path)
    matched = (
        find_requalification(
            known, host_class, observed, tolerances_fitted_at_commit
        )
        if observed
        else None
    )
    if matched is not None:
        return ScoredReadiness(
            allowed=True,
            reason=(
                "the fingerprint changed but was re-qualified on "
                f"{matched.recorded_at} over {matched.cycles} cycles against "
                "the frozen bounds now in force, fitted at "
                f"{matched.tolerances_fitted_at_commit}"
            ),
            fingerprint=comparison,
            requalification=matched,
        )

    detail = (
        "the fingerprint cannot be compared because the bounds were frozen "
        "before fingerprints were recorded"
        if not comparison.comparable
        else "fingerprint changed: "
        + ", ".join(
            f"{entry['component']} {entry['fittedOn'] or '?'} -> "
            f"{entry['observed'] or '?'}"
            for entry in comparison.drift
        )
    )
    return ScoredReadiness(
        allowed=False,
        reason=f"{detail}; not re-qualified",
        fingerprint=comparison,
        requalification=None,
    )


__all__ = [
    "MIN_REQUALIFICATION_CYCLES",
    "FingerprintComparison",
    "QualificationError",
    "Requalification",
    "ScoredReadiness",
    "compare_fingerprints",
    "evaluate_scored_readiness",
    "find_requalification",
    "load_requalifications",
    "record_requalification",
    "store_path",
]
