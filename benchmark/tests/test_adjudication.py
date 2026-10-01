"""Planted review decisions exercise wiring, not measured human agreement."""

from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import subprocess

import pytest

from radius_perf_eval import adjudication
from radius_perf_eval.adjudication import CPUReference, RUBRIC_DIGEST, main
from radius_perf_eval.campaign import CampaignError, CampaignStore, canonical_bytes, digest, read_json
from radius_perf_eval.diagnosis import ReviewRequired
from tests.test_campaign import specification


def setup(tmp_path, *, healthy=False):
    config_path = tmp_path / "reference.json"
    config_path.write_bytes(canonical_bytes({
        "target": "cart", "componentAliases": {"cartservice": "cart", "/radius/cart": "cart", "db": "db"},
        "baselineCpuMax": "200000 100000", "reviewers": ["reviewer-a", "reviewer-b"],
    }))
    reference = CPUReference(config_path)
    verifier = reference.verifier()
    spec = specification(healthy=healthy)
    for row in spec["assignments"]:
        row.update(incident="cpu-quota-healthy/v1" if healthy else "cpu-quota-fault/v1",
                   configurationId=reference.configuration_id,
                   verifierId=verifier.name, verifierDigest=verifier.fingerprint())
    store = CampaignStore.prepare(tmp_path / "store.sqlite", spec, verifiers={verifier.name: verifier})
    return reference, store


def answer(*, healthy=False, explanation="The CPU limit was lowered. Restore the previous allocation."):
    result = {"faultPresent": not healthy, "confidence": 0.9,
              "evidence": [
                  {"signal": "cart/cpu.max", "observation": "The quota is unchanged." if healthy
                   else "The active limit is a quarter of the baseline."},
                  {"signal": "cart/cpu.stat", "observation": "No added throttling." if healthy
                   else "The target spent more periods throttled."},
              ]}
    if not healthy:
        result.update(causalCategory="cpu_saturation", component="cartservice", remediation=explanation)
    return result


def sources(binding, *, payload=None, healthy=False):
    checksum = "sha256:" + "a" * 64
    data = {
        "adapter": {"terminalClass": "validated_success", "calls": [answer(healthy=healthy) if payload is None else payload],
                    "error": None, "budgetStopReason": None},
        "measurements": {
            "target": "cart", "baselineCpuMax": "200000 100000",
            "activeCpuMax": "200000 100000" if healthy else "50000 100000",
            "beforeCpuStat": "nr_periods 100\nnr_throttled 3\nthrottled_usec 40\n",
            "afterCpuStat": "nr_periods 150\nnr_throttled 3\nthrottled_usec 40\n" if healthy
            else "nr_periods 150\nnr_throttled 15\nthrottled_usec 2040\n",
        },
        "lifecycle": {"workspaceBefore": {"app.py": checksum}, "workspaceAfter": {"app.py": checksum},
                      "actions": [{"id": "tool-1", "kind": "read"}],
                      "resourcesBefore": {"reference-project": True}, "resourcesAfter": {"reference-project": False}},
        "usage": {"startedMonotonicNs": 1_000_000_000, "finishedMonotonicNs": 3_500_000_000,
                  "toolCallIds": ["tool-1"], "aiCredits": None},
    }
    return {name: canonical_bytes({"binding": binding.to_dict(), "data": value}) for name, value in data.items()}


def modify(captures, name, change):
    envelope = read_json(captures[name])
    change(envelope["data"])
    captures[name] = canonical_bytes(envelope)


def review(reference, binding, captures, *, mechanism=True, citation_pass=True, status="decided"):
    packet = reference.packet(binding, captures)
    result = reference.review_template(packet)
    result.update(reviewer="reviewer-a", status=status, mechanismPassed=mechanism,
                  rationale="Planted decision for offline wiring; not an actual human review.")
    for citation in result["citations"]:
        citation.update(relevant=citation_pass, supported=citation_pass,
                        rationale="Planted citation judgment against reference measurements.")
    return result


def capture(store, binding, captured):
    for name, raw in captured.items():
        store.capture(binding, name, raw)


def adjudicate(reference, binding, captured, **kwargs):
    captured["human-review-0001"] = canonical_bytes(review(reference, binding, captured, **kwargs))
    return captured


def test_human_review_drives_causal_verdict_and_export(tmp_path):
    reference, store = setup(tmp_path)
    binding = store.start("test-native")
    raw = sources(binding)
    capture(store, binding, raw)
    with pytest.raises(ReviewRequired, match="awaits human"):
        store.finish(binding)
    assert store.export()["runs"][0]["status"] == "running"
    assert store.accounting()["harnessFailures"] == 0
    packet = reference.packet(binding, store.captured_sources(binding))
    text = json.dumps(packet)
    for private in ("test-native", "pair-1", "synthetic-model", "cartservice", "/radius/cart", "componentAsSubmitted"):
        assert private not in text
    assert packet["observations"]["faultObserved"] is True
    decision = review(reference, binding, raw)
    store.capture(binding, "human-review-0001", canonical_bytes(decision))
    final = store.finish(binding)
    row = store.export()["runs"][0]
    assert row["status"] == "validated_success" and row["recordDigest"] == final
    assert row["agentSeconds"] == 2.5 and row["toolCalls"] == 1 and row["aiCredits"] is None
    canonical = read_json(store.record_bytes(final))
    expected = canonical["data"]["verified"]["outcome"]["validatorEvidence"]["diagnosis"]
    assert expected["mechanismPassed"] is True and expected["mechanismExamined"] == ["human-review-0001"]
    assert "reviewer-a" not in json.dumps(store.export())


@pytest.mark.parametrize("explanation", [
    "CPU demand increased due to added request work; increase the quota.",
    "The quota was NOT reduced. It is a garbage collection storm.",
    "The quota was lowered, but not lowered; replace the database.",
    "The CPU quota was reduced. Unobserved disk errors are the real cause.",
])
def test_wrong_within_category_prose_fails_only_on_explicit_human_judgment(tmp_path, explanation):
    reference, store = setup(tmp_path)
    binding = store.start("test-native")
    raw = sources(binding, payload=answer(explanation=explanation))
    with pytest.raises(ReviewRequired):
        reference.replay(binding, raw)
    adjudicate(reference, binding, raw, mechanism=False)
    capture(store, binding, raw)
    store.finish(binding)
    row = store.export()["runs"][0]
    assert row["status"] == "diagnosis_failure" and row["validators"]["diagnosis"] == "fail"
    assert row["validators"]["evidence"] == "pass"
    with pytest.raises(CampaignError, match="only a finished harness"):
        store.start("test-native")


def test_unfamiliar_correct_paraphrase_is_not_rejected_by_parser(tmp_path):
    reference, store = setup(tmp_path)
    binding = store.start("test-radius")
    payload = answer(explanation="Put back the compute allowance that was taken away; the scheduler is holding this process back.")
    payload["component"] = "/radius/cart"
    raw = sources(binding, payload=payload)
    adjudicate(reference, binding, raw)
    assert reference.replay(binding, raw).outcome.terminal_class == "validated_success"


@pytest.mark.parametrize("mutation", [
    lambda a: a.update(component="db"),
    lambda a: a.update(causalCategory="slow_database"),
])
def test_human_pass_cannot_override_automated_target_or_category(tmp_path, mutation):
    reference, store = setup(tmp_path)
    binding = store.start("test-native")
    payload = answer()
    mutation(payload)
    raw = adjudicate(reference, binding, sources(binding, payload=payload))
    assert reference.replay(binding, raw).outcome.terminal_class == "diagnosis_failure"


@pytest.mark.parametrize("kind", ["missing", "invented", "correlated", "fabricated"])
def test_evidence_coverage_and_human_support(tmp_path, kind):
    reference, store = setup(tmp_path)
    binding = store.start("test-native")
    payload = answer()
    if kind == "missing":
        payload["evidence"].pop()
    elif kind in ("invented", "correlated"):
        payload["evidence"].append({"signal": "db/latency" if kind == "correlated" else "nonexistent",
                                    "observation": "Slow."})
    else:
        payload["evidence"][0]["observation"] = "The quota rose from 1 to 100 cores."
    raw = adjudicate(reference, binding, sources(binding, payload=payload),
                     citation_pass=kind != "fabricated")
    result = reference.replay(binding, raw).outcome
    assert result.terminal_class == "diagnosis_failure" and result.validators["evidence"] == "fail"


@pytest.mark.parametrize("healthy,claim,expected", [
    (True, True, "validated_success"), (True, False, "diagnosis_failure"),
    (False, True, "diagnosis_failure"),
])
def test_reference_healthy_fault_distinction(tmp_path, healthy, claim, expected):
    reference, store = setup(tmp_path, healthy=healthy)
    binding = store.start("test-native")
    raw = sources(binding, payload=answer(healthy=claim), healthy=healthy)
    raw = adjudicate(reference, binding, raw)
    assert reference.replay(binding, raw).outcome.terminal_class == expected


def test_needs_review_is_durable_and_can_be_resolved_without_retry(tmp_path):
    reference, store = setup(tmp_path)
    binding = store.start("test-native")
    raw = sources(binding)
    pending = review(reference, binding, raw, status="needs_review", mechanism=None, citation_pass=None)
    raw["human-review-0001"] = canonical_bytes(pending)
    capture(store, binding, raw)
    with pytest.raises(ReviewRequired):
        store.finish(binding)
    reopened = CampaignStore(store.path, store.preparation_digest, store.verifiers)
    assert reopened.open_attempts() == [binding]
    assert reopened.accounting()["attempts"] == 1 and reopened.accounting()["harnessFailures"] == 0
    final = review(reference, binding, raw)
    reopened.capture(binding, "human-review-0002", canonical_bytes(final))
    reopened.finish(binding)
    assert reopened.export()["runs"][0]["status"] == "validated_success"


@pytest.mark.parametrize("change,message", [
    (lambda r: r.update(schemaVersion="unknown"), "unknown human review schema"),
    (lambda r: r.update(packetDigest="sha256:" + "0" * 64), "packet or rubric"),
    (lambda r: r.update(rubricDigest="sha256:" + "0" * 64), "packet or rubric"),
    (lambda r: r.update(reviewer="intruder"), "unregistered reviewer"),
    (lambda r: r.update(status="approved"), "unknown human review status"),
    (lambda r: r.update(rationale=""), "requires rationale"),
    (lambda r: r.update(citations=[]), "citation coverage"),
    (lambda r: r["citations"][0].update(index=1), "reordered human citation"),
    (lambda r: r["citations"][0].update(index=False), "reordered human citation"),
    (lambda r: r["citations"][0].update(rationale=""), "citation review requires rationale"),
    (lambda r: r.update(mechanismPassed=None), "must be explicit"),
    (lambda r: r.update(mechanismPassed=1), "must be explicit"),
    (lambda r: r["citations"][0].update(supported="yes"), "must be explicit"),
])
def test_human_review_integrity_controls(tmp_path, change, message):
    reference, store = setup(tmp_path)
    binding = store.start("test-native")
    raw = sources(binding)
    decision = review(reference, binding, raw)
    change(decision)
    raw["human-review-0001"] = canonical_bytes(decision)
    with pytest.raises(CampaignError, match=message):
        reference.replay(binding, raw)


def test_packet_binding_prevents_review_reuse_and_prose_edits(tmp_path):
    reference, store = setup(tmp_path)
    binding = store.start("test-native")
    raw = adjudicate(reference, binding, sources(binding))
    other = store.start("test-radius")
    other_raw = sources(other)
    other_raw["human-review-0001"] = raw["human-review-0001"]
    with pytest.raises(CampaignError, match="packet or rubric"):
        reference.replay(other, other_raw)
    modify(raw, "adapter", lambda a: a["calls"][0].update(remediation="A different cause."))
    with pytest.raises(CampaignError, match="packet or rubric"):
        reference.replay(binding, raw)


def test_review_order_and_no_replacement(tmp_path):
    reference, store = setup(tmp_path)
    binding = store.start("test-native")
    raw = sources(binding)
    decision = canonical_bytes(review(reference, binding, raw))
    with pytest.raises(CampaignError, match="sequence"):
        reference.replay(binding, {**raw, "human-review-0002": decision})
    with pytest.raises(CampaignError, match="replace"):
        reference.replay(binding, {**raw, "human-review-0001": decision, "human-review-0002": decision})


@pytest.mark.parametrize("field", ["relevant", "supported"])
def test_each_human_citation_gate_is_required(tmp_path, field):
    reference, store = setup(tmp_path)
    binding = store.start("test-native")
    raw = sources(binding)
    decision = review(reference, binding, raw)
    decision["citations"][0][field] = False
    raw["human-review-0001"] = canonical_bytes(decision)
    assert reference.replay(binding, raw).outcome.terminal_class == "diagnosis_failure"


def test_reference_is_barred_from_scored_campaigns(tmp_path):
    reference, store = setup(tmp_path)
    assert reference.verifier().synthetic is True
    binding = store.start("test-native")
    raw = adjudicate(reference, binding, sources(binding))
    from radius_perf_eval.campaign import _verify
    with pytest.raises(CampaignError, match="synthetic"):
        _verify(binding, raw, store.verifiers, "scored")


@pytest.mark.parametrize("change,message", [
    (lambda d: d.update(target="db"), "another target"),
    (lambda d: d.update(baselineCpuMax="100000 100000"), "baseline differs"),
    (lambda d: d.update(afterCpuStat="nr_periods 100\nnr_throttled 3\nthrottled_usec 40"), "empty CPU"),
    (lambda d: d.update(afterCpuStat="nr_periods 101\nnr_throttled 5\nthrottled_usec 50"), "impossible throttled delta"),
    (lambda d: d.update(afterCpuStat="nr_periods 150\nnr_throttled 1\nthrottled_usec 50"), "regressed"),
])
def test_measurement_guards(tmp_path, change, message):
    reference, store = setup(tmp_path)
    binding = store.start("test-native")
    raw = sources(binding)
    modify(raw, "measurements", change)
    with pytest.raises(CampaignError, match=message):
        reference.packet(binding, raw)


@pytest.mark.parametrize("raw", [None, "", "max 100000", "0 100000", "100 0", "1 2 3", "a 1"])
def test_quota_guards(raw):
    assert adjudication._quota("50000 100000\n") == (50000, 100000)
    with pytest.raises(CampaignError):
        adjudication._quota(raw)


@pytest.mark.parametrize("raw", [None, "", "nr_periods nope", "nr_periods 1\nnr_periods 2",
                                "nr_periods 1", "nr_periods 1\nnr_throttled 2\nthrottled_usec 3"])
def test_stat_guards(raw):
    with pytest.raises(CampaignError):
        adjudication._stat(raw)


def test_duplicate_stat_cannot_hide_an_overwritten_measurement():
    with pytest.raises(CampaignError, match="duplicate cpu.stat"):
        adjudication._stat("nr_periods 100\nnr_throttled 3\nthrottled_usec 40\nnr_throttled 0")


@pytest.mark.parametrize("healthy,change", [
    (False, lambda m: m.update(activeCpuMax="200000 100000")),
    (False, lambda m: m.update(afterCpuStat="nr_periods 150\nnr_throttled 3\nthrottled_usec 40")),
    (False, lambda m: m.update(afterCpuStat="nr_periods 150\nnr_throttled 3\nthrottled_usec 41")),
    (False, lambda m: m.update(afterCpuStat="nr_periods 150\nnr_throttled 15\nthrottled_usec 40")),
    (True, lambda m: m.update(activeCpuMax="50000 100000")),
    (True, lambda m: m.update(afterCpuStat="nr_periods 150\nnr_throttled 4\nthrottled_usec 41")),
    (True, lambda m: m.update(afterCpuStat="nr_periods 150\nnr_throttled 4\nthrottled_usec 40")),
    (True, lambda m: m.update(afterCpuStat="nr_periods 150\nnr_throttled 3\nthrottled_usec 41")),
])
def test_measure_assigned_independent_variable_not_just_a_symptom(tmp_path, healthy, change):
    reference, store = setup(tmp_path, healthy=healthy)
    binding = store.start("test-native")
    raw = sources(binding, healthy=healthy)
    modify(raw, "measurements", change)
    raw = adjudicate(reference, binding, raw)
    result = reference.replay(binding, raw).outcome
    assert result.terminal_class == "harness_failure"
    assert result.agent_terminal_class == "validated_success"


def test_assigned_state_failure_preserves_agent_class(tmp_path):
    reference, store = setup(tmp_path)
    binding = store.start("test-native")
    raw = sources(binding, healthy=True)
    raw = adjudicate(reference, binding, raw)
    result = reference.replay(binding, raw).outcome
    assert result.terminal_class == "harness_failure" and result.agent_terminal_class == "diagnosis_failure"


@pytest.mark.parametrize("change,message", [
    (lambda d: d.update(workspaceBefore={}), "empty workspace"),
    (lambda d: d["workspaceBefore"].update({"": "sha256:" + "a" * 64}), "workspace path"),
    (lambda d: d.update(actions=[]), "empty action audit"),
    (lambda d: d["actions"][0].update(kind="unknown"), "unknown audited action"),
    (lambda d: d["actions"].append(deepcopy(d["actions"][0])), "duplicate audited action"),
    (lambda d: d.update(resourcesAfter={}), "cleanup inventory"),
    (lambda d: d.update(resourcesBefore={"reference-project": False}), "resource observation"),
])
def test_lifecycle_guards(tmp_path, change, message):
    _, store = setup(tmp_path)
    raw = read_json(sources(store.start("test-native"))["lifecycle"])["data"]
    change(raw)
    with pytest.raises(CampaignError, match=message):
        CPUReference.lifecycle(raw)


@pytest.mark.parametrize("field,value,status", [
    ("actions", [{"id": "x", "kind": "write"}], "isolation_violation_attempt"),
    ("workspaceAfter", {"app.py": "sha256:" + "b" * 64}, "isolation_violation_attempt"),
    ("resourcesAfter", {"reference-project": True}, "harness_failure"),
])
def test_real_reference_lifecycle_predicates(tmp_path, field, value, status):
    reference, store = setup(tmp_path)
    binding = store.start("test-native")
    raw = sources(binding)
    modify(raw, "lifecycle", lambda d: d.update({field: value}))
    raw = adjudicate(reference, binding, raw)
    assert reference.replay(binding, raw).outcome.terminal_class == status


@pytest.mark.parametrize("change,message", [
    (lambda d: d.update(startedMonotonicNs=None), "partial timing"),
    (lambda d: d.update(finishedMonotonicNs=0), "clock regressed"),
    (lambda d: d.update(startedMonotonicNs=True), "invalid counter"),
    (lambda d: d.update(startedMonotonicNs=-1), "invalid counter"),
    (lambda d: d.update(startedMonotonicNs=0.5), "invalid counter"),
    (lambda d: d.update(toolCallIds=["x", "x"]), "tool-call"),
    (lambda d: d.update(toolCallIds=[1]), "tool-call"),
    (lambda d: d.update(aiCredits=-1), "metrics"),
])
def test_reference_accounting_controls(tmp_path, change, message):
    _, store = setup(tmp_path)
    data = read_json(sources(store.start("test-native"))["usage"])["data"]
    change(data)
    with pytest.raises(CampaignError, match=message):
        CPUReference.metrics(data)


def test_missing_and_zero_usage():
    assert CPUReference.metrics({"startedMonotonicNs": None, "finishedMonotonicNs": None,
                                 "toolCallIds": None, "aiCredits": None}) == {
        "agentSeconds": None, "toolCalls": None, "aiCredits": None}
    assert CPUReference.metrics({"startedMonotonicNs": 0, "finishedMonotonicNs": 0,
                                 "toolCallIds": [], "aiCredits": 0}) == {
        "agentSeconds": 0, "toolCalls": 0, "aiCredits": 0}


@pytest.mark.parametrize("change,message", [
    (lambda c: c.update(target="missing"), "target missing"),
    (lambda c: c.update(reviewers=[]), "missing or duplicate reviewers"),
    (lambda c: c.update(reviewers=["r", "r"]), "missing or duplicate reviewers"),
])
def test_reference_configuration_guards(tmp_path, change, message):
    reference, _ = setup(tmp_path)
    config = reference.config()
    change(config)
    reference.config_path.write_bytes(canonical_bytes(config))
    with pytest.raises(CampaignError, match=message):
        reference.config()


def test_reference_binding_guards(tmp_path):
    reference, store = setup(tmp_path)
    binding = store.start("test-native")
    raw = sources(binding)
    for field, value, message in (
        ("configurationId", "other", "configuration differs"),
        ("incident", "other", "incident differs"),
        ("expectedFault", False, "incident differs"),
    ):
        with pytest.raises(CampaignError, match=message):
            reference.inputs(replace(binding, assignment={**binding.assignment, field: value}), raw)
    with pytest.raises(CampaignError, match="another assignment"):
        reference.inputs(replace(binding, attempt=2), raw)
    with pytest.raises(CampaignError, match="incomplete"):
        reference.inputs(binding, {})
    with pytest.raises(CampaignError, match="unexpected reference source"):
        reference.inputs(binding, {**raw, "extra": b"x"})


@pytest.mark.parametrize("change,message", [
    (lambda a: a.update(terminalClass="bad"), "terminal class"),
    (lambda a: a.update(calls=None), "calls must"),
    (lambda a: a.update(error=1), "adapter error"),
    (lambda a: a.update(budgetStopReason=1), "budget stop"),
])
def test_adapter_shape_controls(tmp_path, change, message):
    reference, store = setup(tmp_path)
    data = read_json(sources(store.start("test-native"))["adapter"])["data"]
    change(data)
    with pytest.raises(CampaignError, match=message):
        reference.recorder(data, reference.config())


@pytest.mark.parametrize("calls,terminal,status", [
    ([], "validated_success", "no_submission"),
    (["not-json"], "validated_success", "invalid_structured_output"),
    ([], "refusal", "refusal"),
    ([], "budget_exhaustion", "budget_exhaustion"),
])
def test_no_answer_needs_no_prose_adjudication(tmp_path, calls, terminal, status):
    reference, store = setup(tmp_path)
    binding = store.start("test-native")
    raw = sources(binding)
    modify(raw, "adapter", lambda d: d.update(calls=calls, terminalClass=terminal))
    with pytest.raises(CampaignError, match="no schema-valid"):
        reference.packet(binding, raw)
    assert reference.replay(binding, raw).outcome.terminal_class == status


def test_operator_cli_packet_review_finish_export_and_dashboard(tmp_path, capsys):
    reference, store = setup(tmp_path)
    prefix = ["--config", str(reference.config_path)]
    assert main([*prefix, "identity"]) == 0
    identity = json.loads(capsys.readouterr().out)
    assert identity["verifierDigest"] == reference.verifier().fingerprint()
    assert identity["configurationId"] == reference.configuration_id
    shared = ["--store", str(store.path), "--receipt", store.preparation_digest]
    for arm in ("native", "architecture", "radius"):
        binding = store.start(f"test-{arm}")
        raw = sources(binding)
        capture(store, binding, raw)
        packet_file = tmp_path / f"{arm}-packet.json"
        command = [*shared, "--run-id", f"test-{arm}"]
        assert main([*prefix, "packet", *command, "--output", str(packet_file)]) == 0
        output = read_json(packet_file.read_bytes())
        assert output["packet"] == reference.packet(binding, raw)
        assert output["reviewTemplate"]["status"] == "needs_review"
        review_file = tmp_path / f"{arm}-review.json"
        if arm == "native":
            review_file.write_bytes(b"{malformed")
            with pytest.raises(SystemExit):
                main([*prefix, "review", *command, "--input", str(review_file)])
            retained = store.captured_sources(binding)
            assert retained["review-input-0001"] == b"{malformed"
            assert "human-review-0001" not in retained
            review_file.write_bytes(canonical_bytes(review(reference, binding, raw, status="needs_review",
                                                         mechanism=None, citation_pass=None)))
            assert main([*prefix, "review", *command, "--input", str(review_file)]) == 0
            with pytest.raises(SystemExit):
                main([*prefix, "finish", *command])
        review_file.write_bytes(canonical_bytes(review(reference, binding, raw, mechanism=arm != "architecture")))
        assert main([*prefix, "review", *command, "--input", str(review_file)]) == 0
        assert main([*prefix, "finish", *command]) == 0
    destination = tmp_path / "comparison.json"
    assert main([*prefix, "export", *shared, "--output", str(destination), "--complete"]) == 0
    report = read_json(destination.read_bytes())
    assert [row["status"] for row in report["runs"]] == ["validated_success", "diagnosis_failure", "validated_success"]
    assert report == store.export(complete=True)
    result = subprocess.run(["node", str(Path(__file__).with_name("dashboard_checks.cjs")), str(destination)],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    with pytest.raises(SystemExit):
        main([*prefix, "finish", *shared, "--run-id", "test-native"])


def test_elapsed_time_overflow_is_explicit():
    with pytest.raises(ValueError, match="finite numeric range"):
        CPUReference.metrics({"startedMonotonicNs": 0, "finishedMonotonicNs": 10**400,
                              "toolCallIds": [], "aiCredits": None})
