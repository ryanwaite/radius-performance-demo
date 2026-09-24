"""Tests for the submit tool: validation, component mapping, and recording."""

from __future__ import annotations

import pytest

from radius_perf_eval.submit_tool import (
    CATEGORY_DEFINITIONS,
    CATEGORY_DISAMBIGUATION,
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
    schema = submit_tool_schema()
    properties = schema["properties"]
    assert properties["causalCategory"]["enum"] == list(CAUSAL_CATEGORIES)
    assert "enum" not in properties["component"]


def test_schema_requires_the_plan_fields():
    schema = submit_tool_schema()
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


# --- the fixture's component inventory must not leak to the agent -------------
#
# The canonical names are the application's service inventory. The Radius graph
# and the architecture document supply that inventory to their own arms; putting
# it in the tool schema would supply it to the native arm as well, shrinking the
# very difference the experiment measures. A rejection must not leak it either,
# or one throwaway guess buys the list.

#: Deliberately unlovely names. A canonical name like "cart" is a substring of
#: ordinary words, so a scan for it would fire on prose and the test would pass
#: for the wrong reason -- or fail for it. These cannot occur by accident.
LEAK_MAP = {
    "zzcartsvc": "zzcanonicalcart",
    "zzfrontendsvc": "zzcanonicalfrontend",
    "/planes/radius/local/resourceGroups/demo/providers/Applications.Core/containers/zzcartsvc": "zzcanonicalcart",
    "zzredissvc": "zzcanonicalcache",
}

LEAKED_NAMES = tuple(sorted(set(LEAK_MAP.values())))


def _leaks(text: str) -> tuple[str, ...]:
    """Every canonical name that appears in ``text``, case-insensitively."""
    haystack = text.lower()
    return tuple(name for name in LEAKED_NAMES if name.lower() in haystack)


def test_the_leak_detector_itself_detects_a_leak():
    """Positive control for the two tests below.

    Both of those assert an absence. An absence passes just as happily when the
    detector has stopped working, so the detector is shown to fire before it is
    trusted to stay silent.
    """
    assert _leaks("the affected service is zzcanonicalcart") == ("zzcanonicalcart",)
    assert _leaks("ZZCANONICALCACHE") == ("zzcanonicalcache",)
    assert _leaks("nothing to see here") == ()


def test_schema_does_not_contain_any_canonical_component_name():
    import json

    schema_text = json.dumps(submit_tool_schema())
    assert _leaks(schema_text) == ()


def test_schema_cannot_be_given_a_fixture_at_all():
    """The strongest form of the guarantee: the leak is not reachable.

    A schema that merely happens not to list the names today could start doing
    so again. A schema that has no access to them cannot.
    """
    import inspect

    assert inspect.signature(submit_tool_schema).parameters == {}


def _every_rejection_text(component_map: ComponentMap) -> list[str]:
    """Collect the rejection text from every path that can reject a payload."""
    recorder = SubmissionRecorder(component_map=component_map)
    bad_payloads = [
        {},
        {"faultPresent": True},
        _valid(component="not-a-real-service"),
        _valid(component="zzcanonicalcarts"),
        _valid(component=""),
        _valid(component=None),
        _valid(causalCategory="gremlins"),
        _valid(evidence=[]),
        _valid(evidence="a string"),
        _valid(confidence=5),
        _valid(confidence="high"),
        _valid(remediation=""),
        {"faultPresent": False, "causalCategory": "cpu_saturation",
         "evidence": [{"signal": "s", "observation": "o"}], "confidence": 0.5},
        {"faultPresent": False, "component": "zzcartsvc",
         "evidence": [{"signal": "s", "observation": "o"}], "confidence": 0.5},
    ]
    texts: list[str] = []
    for payload in bad_payloads:
        accepted, error = recorder.record(payload)
        assert not accepted, f"expected {payload!r} to be rejected"
        texts.append(str(error))
    return texts


def test_no_rejection_text_names_a_valid_component():
    component_map = ComponentMap.from_mapping(LEAK_MAP)
    texts = _every_rejection_text(component_map)
    # Guard against the collection silently shrinking to nothing.
    assert len(texts) >= 10
    for text in texts:
        assert _leaks(text) == (), f"rejection leaked a component name: {text!r}"


def test_unknown_component_rejection_says_only_what_the_reviewer_asked():
    component_map = ComponentMap.from_mapping(LEAK_MAP)
    with pytest.raises(SubmissionError) as excinfo:
        component_map.canonical("not-a-real-service")
    assert str(excinfo.value) == "unknown component; name a service from the application"


def test_rejection_does_not_echo_the_submitted_name_either():
    """Echoing is harmless on its own but makes the map probeable by bisection.

    A rejection that repeats the guess tells an agent nothing it did not already
    know. A rejection that varies with the guess would, and the cheapest way to
    keep the two apart is for the text to be constant.
    """
    component_map = ComponentMap.from_mapping(LEAK_MAP)
    messages = set()
    for guess in ("alpha", "beta", "zzcanonicalcar", "/planes/radius/nope"):
        with pytest.raises(SubmissionError) as excinfo:
            component_map.canonical(guess)
        messages.add(str(excinfo.value))
    assert len(messages) == 1


# --- category definitions -----------------------------------------------------


def test_every_category_has_a_definition():
    assert tuple(CATEGORY_DEFINITIONS) == CAUSAL_CATEGORIES
    for name, definition in CATEGORY_DEFINITIONS.items():
        assert definition.strip(), f"{name} has no definition"


def test_categories_are_derived_from_the_definitions():
    """A category added without a definition would be an ambiguous label.

    Deriving the tuple from the mapping makes that unrepresentable rather than
    merely discouraged.
    """
    assert CAUSAL_CATEGORIES == tuple(CATEGORY_DEFINITIONS)


def test_definitions_are_distinct():
    definitions = list(CATEGORY_DEFINITIONS.values())
    assert len(set(definitions)) == len(definitions)


def test_schema_states_every_definition_and_tie_break():
    description = submit_tool_schema()["properties"]["causalCategory"]["description"]
    for name, definition in CATEGORY_DEFINITIONS.items():
        assert name in description
        assert definition in description
    for rule in CATEGORY_DISAMBIGUATION:
        assert rule in description


def test_the_overlapping_pairs_are_separated_by_origin():
    """The three pairs the reviewer named must be distinguishable on wording.

    This is a text check, not a semantic one, so it is deliberately narrow: it
    pins the distinguishing clause so a later edit cannot smooth it away.
    """
    definitions = CATEGORY_DEFINITIONS
    assert "non-database" in definitions["dependency_latency"]
    assert "inside the database engine" in definitions["slow_database"]
    assert "its own locks" in definitions["slow_database"]
    assert "in-process" in definitions["lock_contention"]
    assert "while memory remains" in definitions["garbage_collection"]


def test_component_is_defined_by_what_must_change():
    description = submit_tool_schema()["properties"]["component"]["description"]
    assert "whose behaviour must change to fix the fault" in description
