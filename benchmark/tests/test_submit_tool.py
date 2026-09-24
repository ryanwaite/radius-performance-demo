"""Tests for the submit tool: validation, component mapping, and recording."""

from __future__ import annotations

import pytest

from radius_perf_eval.submit_tool import (
    CAUSAL_CATEGORIES,
    ComponentMap,
    SubmissionError,
    SubmissionRecorder,
    submit_tool_schema,
    validate_submission,
)

FIXTURE_MAP = {
    "cartservice": "cart",
    "cart-service": "cart",
    "/planes/radius/local/resourceGroups/demo/providers/Applications.Core/containers/cart": "cart",
    "frontend": "frontend",
    "redis": "cache",
}


def _map() -> ComponentMap:
    return ComponentMap.from_mapping(FIXTURE_MAP)


def _valid(**overrides):
    payload = {
        "faultPresent": True,
        "causalCategory": "cpu_saturation",
        "component": "cartservice",
        "evidence": [{"signal": "container_cpu", "observation": "pegged at 99%"}],
        "confidence": 0.8,
        "remediation": "raise the CPU limit",
    }
    payload.update(overrides)
    return payload


# --- the causal category list -------------------------------------------------


def test_causal_categories_are_a_closed_list():
    assert isinstance(CAUSAL_CATEGORIES, tuple)
    assert len(CAUSAL_CATEGORIES) == len(set(CAUSAL_CATEGORIES))


def test_unknown_category_is_rejected():
    with pytest.raises(SubmissionError):
        validate_submission(_valid(causalCategory="gremlins"), component_map=_map())


def test_every_category_validates():
    for category in CAUSAL_CATEGORIES:
        validate_submission(_valid(causalCategory=category), component_map=_map())


# --- component mapping --------------------------------------------------------


def test_compose_name_and_radius_id_map_to_one_canonical_name():
    """Both arms must name the same component identically after mapping.

    Without this, an identical diagnosis would score differently depending on
    which vocabulary the arm happened to use -- an artefact that would look
    exactly like a treatment effect.
    """
    mapping = _map()
    radius_id = (
        "/planes/radius/local/resourceGroups/demo/providers"
        "/Applications.Core/containers/cart"
    )
    assert mapping.canonical("cartservice") == "cart"
    assert mapping.canonical(radius_id) == "cart"


def test_canonical_names_resolve_to_themselves():
    assert _map().canonical("cart") == "cart"


def test_lookup_is_case_and_whitespace_insensitive():
    assert _map().canonical("  CartService  ") == "cart"


def test_unknown_component_is_rejected():
    with pytest.raises(SubmissionError):
        validate_submission(_valid(component="not-a-service"), component_map=_map())


def test_empty_map_is_an_error_not_an_empty_pass():
    """An absent fixture table must fail loudly.

    If an empty map were allowed, every component would be rejected and the
    whole arm would score zero for a harness reason.
    """
    with pytest.raises(SubmissionError):
        ComponentMap.from_mapping({})


def test_map_comes_from_the_fixture_not_the_harness():
    """The harness must expose no default component table.

    A built-in mapping would leak one fixture's component set into every
    scenario, and a scenario the harness had not seen would silently fail to
    map. Scanning the module namespace catches a default that a source-text
    search would miss, and ignores the docstring's illustrative names.
    """
    import radius_perf_eval.submit_tool as module

    for name, value in vars(module).items():
        if name.startswith("__") or not isinstance(value, dict):
            continue
        string_pairs = {
            k: v
            for k, v in value.items()
            if isinstance(k, str) and isinstance(v, str)
        }
        assert not string_pairs, f"{name} looks like a built-in component table"

    # And the constructor has no default: a map must be supplied.
    with pytest.raises(TypeError):
        ComponentMap()


# --- validation ---------------------------------------------------------------


def test_valid_submission_round_trips():
    submission = validate_submission(_valid(), component_map=_map())
    assert submission.fault_present is True
    assert submission.component == "cart"
    assert submission.confidence == 0.8


def test_no_fault_submission_omits_cause_fields():
    submission = validate_submission(
        {
            "faultPresent": False,
            "evidence": [{"signal": "latency", "observation": "flat at baseline"}],
            "confidence": 0.7,
        },
        component_map=_map(),
    )
    assert submission.fault_present is False
    assert submission.causal_category is None
    assert submission.component is None


def test_no_fault_submission_may_not_assert_a_cause():
    """A control trial that reports no fault cannot also name its cause."""
    with pytest.raises(SubmissionError):
        validate_submission(
            {
                "faultPresent": False,
                "causalCategory": "cpu_saturation",
                "evidence": [{"signal": "s", "observation": "o"}],
                "confidence": 0.5,
            },
            component_map=_map(),
        )


def test_fault_submission_requires_a_component():
    with pytest.raises(SubmissionError):
        payload = _valid()
        del payload["component"]
        validate_submission(payload, component_map=_map())


def test_evidence_is_required_in_both_directions():
    for payload in (_valid(evidence=[]), {"faultPresent": False, "confidence": 0.5}):
        with pytest.raises(SubmissionError):
            validate_submission(payload, component_map=_map())


@pytest.mark.parametrize("value", [-0.1, 1.1, "high", None])
def test_confidence_must_be_a_number_in_range(value):
    with pytest.raises(SubmissionError):
        validate_submission(_valid(confidence=value), component_map=_map())


def test_boolean_confidence_is_rejected():
    """`True` is an int in Python, so a naive range check would accept it."""
    with pytest.raises(SubmissionError):
        validate_submission(_valid(confidence=True), component_map=_map())


def test_fault_present_must_be_a_real_boolean():
    with pytest.raises(SubmissionError):
        validate_submission(_valid(faultPresent="yes"), component_map=_map())


# --- the schema ---------------------------------------------------------------


def test_schema_constrains_category_but_not_component():
    """The model must be free to name a component in its own arm's vocabulary.

    Enumerating canonical names in the schema would hand every arm the answer
    key's naming, and would leak the fixture's component list into the prompt.
    """
    schema = submit_tool_schema(_map())
    properties = schema["properties"]
    assert properties["causalCategory"]["enum"] == list(CAUSAL_CATEGORIES)
    assert "enum" not in properties["component"]


def test_schema_requires_the_plan_fields():
    schema = submit_tool_schema(_map())
    for field in ("faultPresent", "evidence", "confidence"):
        assert field in schema["required"]


# --- recording ----------------------------------------------------------------


def test_recorder_accepts_the_first_valid_submission():
    recorder = SubmissionRecorder(component_map=_map())
    accepted, error = recorder.record(_valid())
    assert accepted is True
    assert error is None
    assert recorder.submission is not None


def test_recorder_rejects_a_second_submission():
    recorder = SubmissionRecorder(component_map=_map())
    recorder.record(_valid())
    accepted, error = recorder.record(_valid(confidence=0.1))
    assert accepted is False
    assert recorder.submission.confidence == 0.8


def test_rejected_attempts_are_kept():
    """"Could not diagnose" and "could not express" are different findings.

    If a whole arm failed on rejected submissions, that would be a harness
    artefact rather than evidence about the treatment, and it must be visible.
    """
    recorder = SubmissionRecorder(component_map=_map())
    recorder.record(_valid(causalCategory="gremlins"))
    recorder.record(_valid(component="not-a-service"))
    assert recorder.submission is None
    assert recorder.rejected_attempts == 2


def test_a_rejected_attempt_then_a_valid_one_is_accepted():
    """A failed submit call leaves the turn running so the model can retry."""
    recorder = SubmissionRecorder(component_map=_map())
    assert recorder.record(_valid(confidence=5))[0] is False
    assert recorder.record(_valid())[0] is True
    assert recorder.submission is not None
    assert recorder.rejected_attempts == 1


def test_no_submission_scores_as_failure():
    recorder = SubmissionRecorder(component_map=_map())
    assert recorder.scored_as_failure is True


def test_valid_submission_does_not_score_as_failure():
    recorder = SubmissionRecorder(component_map=_map())
    recorder.record(_valid())
    assert recorder.scored_as_failure is False
