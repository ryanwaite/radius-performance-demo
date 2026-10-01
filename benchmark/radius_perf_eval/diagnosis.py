"""Diagnosis gate shared by arms, with incident-owned evidence verification.

This module compares the declared causal target. It does not interpret telemetry
or grade free-text observations. An incident supplies an independent reviewer
that checks each citation against that trial's captured evidence. No production
Shop reviewer is implemented here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

from .submit_tool import CAUSAL_CATEGORIES, Citation, Connection, Submission


@dataclass(frozen=True)
class ExpectedDiagnosis:
    fault_present: bool
    causal_category: str | None = None
    component: str | None = None
    connection: Connection | None = None

    def __post_init__(self) -> None:
        if type(self.fault_present) is not bool:
            raise ValueError("expected fault must be boolean")
        if self.fault_present:
            if self.causal_category not in CAUSAL_CATEGORIES:
                raise ValueError("expected causal category is required")
            if (self.component is None) == (self.connection is None):
                raise ValueError("declare exactly one causal component or connection")
            names = (
                (self.connection.source, self.connection.target)
                if self.connection is not None else (self.component,)
            )
            if any(not isinstance(name, str) or not name.strip() for name in names):
                raise ValueError("causal target names must be nonempty canonical names")
        elif any(value is not None for value in (
            self.causal_category, self.component, self.connection
        )):
            raise ValueError("healthy expectations cannot declare a cause")


@dataclass(frozen=True)
class EvidenceReview:
    """One citation's result from a hidden, trial-specific evidence reviewer.

    ``relevant`` means on the declared causal path for a fault, or part of the
    incident's fault-detection telemetry for a healthy control. ``supported``
    checks the observation, not merely whether the signal's name exists.
    Artifact references identify what was actually examined, including on a
    rejection. They are not authenticated by this gate.
    """

    citation: Citation
    examined: tuple[str, ...]
    exists: bool
    relevant: bool
    supported: bool

    def __post_init__(self) -> None:
        if not isinstance(self.examined, tuple):
            raise ValueError("examined references must be an immutable tuple")
        if not self.examined or any(
            not isinstance(ref, str) or not ref.strip() for ref in self.examined
        ):
            raise ValueError("evidence review requires nonempty examined references")
        if any(type(value) is not bool for value in (
            self.exists, self.relevant, self.supported
        )):
            raise ValueError("evidence decisions must be explicit booleans")

    @property
    def passed(self) -> bool:
        return self.exists and self.relevant and self.supported


class EvidenceReviewer(Protocol):
    def __call__(self, citation: Citation) -> EvidenceReview: ...


@dataclass(frozen=True)
class DiagnosisGrade:
    submission: Submission
    expected: ExpectedDiagnosis
    reviews: tuple[EvidenceReview, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.reviews, tuple):
            raise ValueError("evidence reviews must be an immutable tuple")
        if tuple(review.citation for review in self.reviews) != self.submission.evidence:
            raise ValueError("grade must review every submitted citation in order")

    @property
    def diagnosis_passed(self) -> bool:
        submission, expected = self.submission, self.expected
        diagnosis_passed = submission.fault_present == expected.fault_present
        if expected.fault_present:
            diagnosis_passed = diagnosis_passed and (
                submission.causal_category == expected.causal_category
            )
            if expected.connection is not None:
                diagnosis_passed = diagnosis_passed and (
                    submission.connection == expected.connection
                    and submission.component in (
                        expected.connection.source, expected.connection.target
                    )
                )
            else:
                diagnosis_passed = diagnosis_passed and submission.component == expected.component
        return diagnosis_passed

    @property
    def evidence_passed(self) -> bool:
        return bool(self.reviews) and all(review.passed for review in self.reviews)


def grade_diagnosis(
    submission: Submission,
    expected: ExpectedDiagnosis,
    *,
    review_evidence: EvidenceReviewer,
) -> DiagnosisGrade:
    """Compare cause and inspect every citation, including for healthy claims.

    Reviewer failures propagate as harness errors; they are not wrong answers.
    The caller must bind the reviewer and expected answer to the same trial.
    """
    reviews = []
    for citation in submission.evidence:
        review = review_evidence(citation)
        if review.citation != citation:
            raise ValueError("evidence reviewer returned a result for another citation")
        reviews.append(review)
    return DiagnosisGrade(submission, expected, tuple(reviews))
