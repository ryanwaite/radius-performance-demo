"""Controlled offline evidence, not production Shop incident validators."""

from dataclasses import replace
import pytest

from radius_perf_eval.diagnosis import (
    DiagnosisGrade, EvidenceReview, ExpectedDiagnosis, grade_diagnosis,
)
from radius_perf_eval.submit_tool import (
    Citation, ComponentMap, Connection, SubmissionRecorder, validate_submission,
)
from radius_perf_eval.trial_outcome import ValidationCheck, score_trial


COMPONENTS = ComponentMap.from_mapping({
    "caller-service": "caller", "db-service": "db", "monitor": "monitor",
    "/radius/caller": "caller", "/radius/db": "db",
})
FAULT = ExpectedDiagnosis(
    True, "slow_database", connection=Connection("caller", "db")
)


def payload(**overrides):
    answer = {
        "faultPresent": True,
        "causalCategory": "slow_database",
        "component": "db-service",
        "connection": {"source": "caller-service", "target": "db-service"},
        "evidence": [{"signal": "db.latency", "observation": "250 ms"}],
        "confidence": 0.8,
        "remediation": "remove the injected read delay",
    }
    answer.update(overrides)
    return answer


def healthy_payload():
    return {
        "faultPresent": False,
        "evidence": [{"signal": "db.latency", "observation": "5 ms"}],
        "confidence": 0.8,
    }


def reviewer(*, healthy=False):
    # Exact strings deliberately represent a toy capture. No production grader
    # is allowed to infer support by finding these strings in arbitrary prose.
    capture = {"db.latency": "5 ms" if healthy else "250 ms", "monitor.latency": "800 ms"}

    def review(citation):
        return EvidenceReview(
            citation, ("offline-control:captured-latencies",),
            citation.signal in capture,
            citation.signal == "db.latency",
            capture.get(citation.signal) == citation.observation,
        )

    return review


def grade(answer=None, expected=FAULT, *, healthy=False):
    submission = validate_submission(
        payload() if answer is None else answer, component_map=COMPONENTS
    )
    return grade_diagnosis(submission, expected, review_evidence=reviewer(healthy=healthy))


def checks():
    return {
        name: ValidationCheck(True, (f"offline-control:{name}",))
        for name in ("scope", "safety", "cleanup")
    }


def outcome(*, answer=None, expected=FAULT, terminal="validated_success", **kwargs):
    recorder = SubmissionRecorder(COMPONENTS)
    recorder.record(payload() if answer is None else answer)
    diagnosis = grade_diagnosis(
        recorder.submission, expected, review_evidence=reviewer()
    ) if recorder.submission is not None else None
    return score_trial(
        terminal_class=terminal, recorder=recorder,
        **{"grade": diagnosis, **checks(), **kwargs},
    )


def test_reference_connection_and_component_answers_pass():
    for component in ("caller-service", "/radius/db"):
        result = grade(payload(component=component))
        assert result.diagnosis_passed and result.evidence_passed
    result = grade(
        payload(connection=None), ExpectedDiagnosis(True, "slow_database", component="db")
    )
    assert result.diagnosis_passed and result.evidence_passed
    result = outcome()
    assert result.terminal_class == "validated_success"
    assert result.valid and result.scored
    assert result.validators == {name: "pass" for name in (
        "diagnosis", "evidence", "scope", "safety", "cleanup"
    )}
    assert set(result.validator_evidence) == set(result.validators)


@pytest.mark.parametrize("overrides", [
    {"component": "monitor"},
    {"causalCategory": "cpu_saturation"},
    {"connection": {"source": "db", "target": "caller"}},
    {"connection": {"source": "caller", "target": "monitor"}},
    {"connection": None},
])
def test_wrong_causal_target_fails(overrides):
    result = outcome(answer=payload(**overrides))
    assert result.terminal_class == "diagnosis_failure"
    assert result.validators["diagnosis"] == "fail"
    assert result.valid and not result.scored


def test_component_incident_cannot_pass_on_a_matching_edge_alone():
    result = outcome(
        answer=payload(component="caller"),
        expected=ExpectedDiagnosis(True, "slow_database", component="db"),
    )
    assert result.terminal_class == "diagnosis_failure"


@pytest.mark.parametrize("citation", [
    {"signal": "invented.metric", "observation": "250 ms"},
    {"signal": "db.latency", "observation": "5 ms"},
    {"signal": "monitor.latency", "observation": "800 ms"},
])
def test_fabricated_or_correlated_evidence_fails_even_after_a_good_citation(citation):
    answer = payload()
    answer["evidence"].append(citation)
    result = outcome(answer=answer)
    assert result.terminal_class == "diagnosis_failure"
    assert result.validators["diagnosis"] == "pass"
    assert result.validators["evidence"] == "fail"
    assert len(result.validator_evidence["evidence"]) == 2


def test_healthy_controls_require_real_review_and_correct_fault_claim():
    healthy = ExpectedDiagnosis(False)
    correct = grade(healthy_payload(), healthy, healthy=True)
    assert correct.diagnosis_passed and correct.evidence_passed
    false_alarm = grade(payload(), healthy, healthy=True)
    assert not false_alarm.diagnosis_passed
    missed = grade(healthy_payload())
    assert not missed.diagnosis_passed
    unseen = replace(correct.submission, evidence=())
    empty = grade_diagnosis(unseen, healthy, review_evidence=reviewer(healthy=True))
    assert not empty.evidence_passed
    with pytest.raises(ValueError, match="examined"):
        grade_diagnosis(correct.submission, healthy, review_evidence=lambda c: EvidenceReview(
            c, (), True, True, True
        ))


@pytest.mark.parametrize("kwargs", [
    {"fault_present": "yes"},
    {"fault_present": 1, "causal_category": "slow_database", "component": "db"},
    {"fault_present": None},
    {"fault_present": True},
    {"fault_present": True, "causal_category": "gremlins", "component": "db"},
    {"fault_present": True, "causal_category": "slow_database"},
    {"fault_present": True, "causal_category": "slow_database", "component": ""},
    {"fault_present": True, "causal_category": "slow_database",
     "component": "db", "connection": Connection("caller", "db")},
    {"fault_present": True, "causal_category": "slow_database",
     "connection": Connection("", "db")},
    {"fault_present": False, "component": "db"},
    {"fault_present": False, "causal_category": "slow_database"},
    {"fault_present": False, "connection": Connection("caller", "db")},
])
def test_invalid_hidden_expectations_are_harness_errors(kwargs):
    with pytest.raises(ValueError):
        ExpectedDiagnosis(**kwargs)


def test_misdirected_and_crashed_reviewers_cannot_grade():
    submission = grade().submission
    with pytest.raises(ValueError, match="another citation"):
        grade_diagnosis(submission, FAULT, review_evidence=lambda c: EvidenceReview(
            Citation("other", "value"), ("offline-control:other",), True, True, True
        ))

    def broken(citation):
        raise RuntimeError("capture unavailable")

    with pytest.raises(RuntimeError, match="capture unavailable"):
        grade_diagnosis(submission, FAULT, review_evidence=broken)


@pytest.mark.parametrize("name", ["grade", "scope", "safety", "cleanup"])
def test_missing_validator_is_harness_failure_not_success_or_agent_error(name):
    result = outcome(**{name: None})
    assert result.terminal_class == "harness_failure"
    assert not result.valid and not result.scored
    assert "pass" not in result.validators.get(name, "")


def test_schema_acceptance_alone_does_not_score():
    recorder = SubmissionRecorder(COMPONENTS)
    recorder.record(payload())
    assert recorder.outcome()["validationLevel"] == "schema_only"
    result = score_trial(terminal_class="validated_success", recorder=recorder)
    assert result.terminal_class == "harness_failure"
    assert result.agent_terminal_class is None
    assert not result.scored


@pytest.mark.parametrize("name", ["scope", "safety"])
def test_prohibited_actions_override_a_correct_answer(name):
    result = outcome(**{name: ValidationCheck(False, ("offline-control:violation",))})
    assert result.terminal_class == "isolation_violation_attempt"
    assert result.valid and not result.scored
    assert result.validators[name] == "fail"


def test_failed_cleanup_or_adapter_overrides_but_preserves_agent_result():
    for kwargs in (
        {"cleanup": ValidationCheck(False, ("offline-control:remaining-resource",))},
        {"error": "transport closed"},
        {"terminal": "copilot_sdk_or_adapter_failure"},
        {"terminal": "harness_failure"},
    ):
        result = outcome(**kwargs)
        assert result.terminal_class == "harness_failure"
        assert result.agent_terminal_class == "validated_success"
        assert not result.valid


def test_grade_cannot_be_reused_for_another_submission():
    result = outcome(grade=grade(payload(confidence=0.1)))
    assert result.terminal_class == "harness_failure"
    assert any("different submission" in reason for reason in result.reasons)


def test_grade_cannot_drop_or_substitute_reviews():
    reviewed = grade()
    with pytest.raises(ValueError, match="every submitted citation"):
        DiagnosisGrade(reviewed.submission, FAULT, ())
    with pytest.raises(ValueError, match="every submitted citation"):
        replace(reviewed, reviews=(replace(
            reviewed.reviews[0], citation=Citation("other", "value")
        ),))


def test_causal_verdict_is_derived_not_a_caller_assertion():
    reviewed = grade()
    changed = replace(reviewed, expected=ExpectedDiagnosis(False))
    assert not changed.diagnosis_passed


def test_grade_cannot_be_changed_through_a_mutable_review_list():
    reviewed = grade()
    with pytest.raises(ValueError, match="immutable tuple"):
        replace(reviewed, reviews=list(reviewed.reviews))


@pytest.mark.parametrize("terminal", ["refusal", "budget_exhaustion", "isolation_violation_attempt"])
def test_agent_terminal_classes_are_preserved_without_submission(terminal):
    result = score_trial(
        terminal_class=terminal, recorder=SubmissionRecorder(COMPONENTS), **checks()
    )
    assert result.terminal_class == terminal
    assert result.valid and not result.scored


def test_isolation_attempt_overrides_correct_submission():
    assert outcome(terminal="isolation_violation_attempt").terminal_class == "isolation_violation_attempt"


def test_invalid_and_absent_outputs_are_distinct():
    invalid = outcome(answer={})
    assert invalid.terminal_class == "invalid_structured_output"
    absent = score_trial(
        terminal_class="validated_success", recorder=SubmissionRecorder(COMPONENTS),
        **checks(),
    )
    assert absent.terminal_class == "no_submission"


@pytest.mark.parametrize("terminal", ["submitted", "invalid_submission", "garbage", "remediation_failure"])
def test_unrecognized_or_remediation_input_is_not_a_diagnosis_result(terminal):
    with pytest.raises(ValueError):
        outcome(terminal=terminal)


@pytest.mark.parametrize("refs", [(), ("",), (" ",), (None,), "ref", ["ref"], {"ref": "x"}])
def test_empty_validator_evidence_is_not_a_pass(refs):
    with pytest.raises(ValueError):
        ValidationCheck(True, refs)
    with pytest.raises(ValueError):
        EvidenceReview(Citation("s", "o"), refs, True, True, True)


@pytest.mark.parametrize("value", [None, 1, "pass"])
def test_validator_values_are_not_truthy_defaults(value):
    with pytest.raises(ValueError):
        ValidationCheck(value, ("ref",))
    for name in ("exists", "relevant", "supported"):
        with pytest.raises(ValueError):
            EvidenceReview(Citation("s", "o"), ("ref",), **{
                "exists": True, "relevant": True, "supported": True, name: value,
            })


@pytest.mark.parametrize("name", ["exists", "relevant", "supported"])
def test_each_independent_evidence_requirement_is_required(name):
    good = EvidenceReview(Citation("s", "o"), ("captured:signal",), True, True, True)
    assert good.passed
    assert not replace(good, **{name: False}).passed


def test_missing_grade_is_not_overridden_by_a_passing_sandbox():
    result = outcome(grade=None, gate_result=type("Gate", (), {
        "passed": True, "to_json_dict": lambda self: {"passed": True},
    })())
    assert result.terminal_class == "harness_failure"
