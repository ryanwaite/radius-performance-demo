"""Synthetic accounting controls. These captures are not Shop observations."""

from copy import deepcopy
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import json
from pathlib import Path
import sqlite3
import subprocess
import sys

import pytest

from radius_perf_eval import campaign
from radius_perf_eval.campaign import (
    ARMS, Binding, CampaignError, CampaignStore, Verifier, VerifiedAttempt,
    canonical_bytes, digest, main, read_json, validate_preparation,
)
from radius_perf_eval.diagnosis import EvidenceReview, ExpectedDiagnosis, grade_diagnosis
from radius_perf_eval.submit_tool import ComponentMap, SubmissionRecorder
from radius_perf_eval.trial_outcome import ValidationCheck, score_trial


def synthetic_replay(binding, sources):
    """Exact planted values only; deliberately not shipped as a production grader."""
    if set(sources) != {"adapter", "telemetry", "audit", "usage"}:
        raise CampaignError("synthetic capture incomplete")
    data = {}
    for name, raw in sources.items():
        capture = read_json(raw)
        if (set(capture) != {"binding", "data"}
                or canonical_bytes(capture["binding"]) != canonical_bytes(binding.to_dict())):
            raise CampaignError("synthetic source belongs to a different trial")
        data[name] = capture["data"]
    recorder = SubmissionRecorder(ComponentMap({"cartservice": "cart", "db": "db"}))
    for payload in data["adapter"]["calls"]:
        recorder.record(payload)
    fault = binding.assignment["incident"] == "synthetic-fault/v1"
    expected = ExpectedDiagnosis(fault, "cpu_saturation" if fault else None,
                                 "cart" if fault else None)
    grade = None
    if recorder.submission:
        telemetry = data["telemetry"]
        grade = grade_diagnosis(
            recorder.submission, expected,
            mechanism_passed=True, mechanism_evidence=("adapter",),
            review_evidence=lambda citation: EvidenceReview(
                citation, ("telemetry",), citation.signal in telemetry,
                citation.signal == "cpu", telemetry.get(citation.signal) == citation.observation,
            ),
        )
    outcome = score_trial(
        terminal_class=data["adapter"]["terminal"],
        error=data["adapter"]["error"], recorder=recorder, grade=grade,
        **{key: ValidationCheck(data["audit"][key] == "intact", ("audit",))
           for key in ("scope", "safety", "cleanup")},
    )
    return VerifiedAttempt(outcome, data["usage"], ("adapter", "usage"))


def verifier():
    return Verifier("synthetic-only-v1", synthetic_replay, (Path(__file__),), synthetic=True)


def specification(*, healthy=False, phase="pilot"):
    check = verifier()
    return {
        "schemaVersion": "radius-campaign-v1",
        "campaign": {"id": "synthetic-only", "phase": phase, "benchmarkCommit": "a" * 40,
                     "analysisPlan": "b" * 40 if phase == "scored" else "not-preregistered"},
        "assignments": [{
            "runId": f"test-{arm}", "pairId": "pair-1", "blockId": "block-1", "arm": arm,
            "model": "synthetic-model@v1", "incident": "synthetic-healthy/v1" if healthy else "synthetic-fault/v1",
            "seed": "seed-1", "configurationId": "frozen-config", "expectedFault": not healthy,
            "verifierId": check.name, "verifierDigest": check.fingerprint(),
        } for arm in ARMS],
    }


def prepare(tmp_path, **kwargs):
    check = verifier()
    return CampaignStore.prepare(tmp_path / "campaign.sqlite", specification(**kwargs),
                                 verifiers={check.name: check})


def captures(binding, *, fault=None, calls=None, terminal="validated_success", error=None,
             audit=None, metrics=None, telemetry=None):
    if fault is None:
        fault = binding.assignment["expectedFault"]
    answer = {"faultPresent": fault, "confidence": 0.8,
              "evidence": [{"signal": "cpu", "observation": "99%" if fault else "1%"}]}
    if fault:
        answer.update(causalCategory="cpu_saturation", component="cartservice",
                      remediation="synthetic test only")
    data = {
        "adapter": {"calls": [answer] if calls is None else calls, "terminal": terminal, "error": error},
        "telemetry": telemetry if telemetry is not None else {"cpu": "99%" if binding.assignment["expectedFault"] else "1%"},
        "audit": audit if audit is not None else dict.fromkeys(("scope", "safety", "cleanup"), "intact"),
        "usage": metrics if metrics is not None else {"agentSeconds": 2.5, "toolCalls": 3, "aiCredits": None},
    }
    return {name: canonical_bytes({"binding": binding.to_dict(), "data": value})
            for name, value in data.items()}


def finish(store, binding, **kwargs):
    for name, data in captures(binding, **kwargs).items():
        store.capture(binding, name, data)
    return store.finish(binding)


def complete_block(store, **kwargs):
    for arm in ARMS:
        finish(store, store.start(f"test-{arm}"), **kwargs)


def rewrite_event(store, index, mutate, *, rechain=True):
    """Bypass SQL protections deliberately to test the read-side trust boundary."""
    with sqlite3.connect(store.path) as db:
        events = [read_json(row[0]) for row in db.execute("SELECT payload FROM journal ORDER BY sequence")]
        mutate(events[index])
        db.execute("DROP TRIGGER no_update")
        previous = None
        for number, event in enumerate(events, 1):
            if rechain:
                event["previous"] = previous
            raw = canonical_bytes(event)
            db.execute("UPDATE journal SET payload=? WHERE sequence=?", (raw, number))
            previous = digest(raw)


def test_prepare_resume_and_pending_report(tmp_path):
    spec = specification()
    store = CampaignStore.prepare(tmp_path / "campaign.sqlite", spec)
    assert store.path.stat().st_mode & 0o777 == 0o600
    snapshot = store.export()
    spec["assignments"].reverse()
    reopened = CampaignStore(store.path, store.preparation_digest)
    assert reopened.export() == snapshot
    assert [r["arm"] for r in snapshot["runs"]] == list(ARMS)
    assert all(r["status"] == "pending" and r["attempts"] == r["harnessFailures"] == 0
               and r["recordDigest"] is None and r["reportedFault"] is None
               and r["validators"] == {} and r["agentSeconds"] is None for r in snapshot["runs"])
    assert reopened.accounting()["retryInclusiveMetrics"]["aiCredits"] == {
        "availableSum": None, "availableAttempts": 0, "missingAttempts": 0,
    }
    with pytest.raises(CampaignError, match="unfinished"):
        reopened.export(complete=True)
    with pytest.raises(FileExistsError):
        CampaignStore.prepare(store.path, specification())
    with pytest.raises(CampaignError, match="receipt"):
        CampaignStore(store.path, "sha256:" + "0" * 64).export()


def test_binding_and_verifier_inputs_are_immutable_snapshots():
    row = specification()["assignments"][0]
    binding = Binding("sha256:" + "a" * 64, row, 1)
    row["seed"] = "changed"
    assert binding.assignment["seed"] == "seed-1"
    with pytest.raises(TypeError):
        binding.assignment["seed"] = "changed"
    paths = [Path(__file__)]
    check = Verifier("synthetic", synthetic_replay, paths, True)
    fingerprint = check.fingerprint()
    paths.clear()
    assert check.fingerprint() == fingerprint


@pytest.mark.parametrize("mutation", [
    lambda s: s.update(schemaVersion="v1"),
    lambda s: s.update(assignments=[]),
    lambda s: s["assignments"].pop(),
    lambda s: s["assignments"].append(deepcopy(s["assignments"][0])),
    lambda s: s["assignments"][1].update(arm="native"),
    lambda s: s["assignments"][0].update(arm="graph"),
    lambda s: s["assignments"][0].update(expectedFault=1),
    lambda s: s["assignments"][0].update(verifierDigest="sha256:fake"),
    lambda s: s["campaign"].update(phase="scored"),
    lambda s: s["campaign"].update(phase="unknown"),
    lambda s: s["assignments"][0].update(runId="=SUM(1,2)"),
    lambda s: s["assignments"][0].update(model="<script>"),
    lambda s: s["campaign"].update(id="secret\nAuthorization: password"),
    lambda s: s["campaign"].update(unexpected="secret"),
])
def test_reject_bad_rosters(mutation):
    spec = specification()
    mutation(spec)
    with pytest.raises(CampaignError):
        validate_preparation(spec)


@pytest.mark.parametrize("key", campaign.MATCH_FIELDS)
def test_reject_mismatched_set(key):
    spec = specification()
    spec["assignments"][1][key] = False if key == "expectedFault" else (
        "sha256:" + "0" * 64 if key == "verifierDigest" else "different")
    with pytest.raises(CampaignError, match="matched"):
        validate_preparation(spec)


def test_interruption_retains_capture_and_requires_explicit_completion(tmp_path):
    store = prepare(tmp_path)
    binding = store.start("test-native")
    source = captures(binding)
    store.capture(binding, "adapter", source["adapter"])
    reopened = CampaignStore(store.path, store.preparation_digest, store.verifiers)
    assert reopened.open_attempts() == [binding]
    assert reopened.export()["runs"][0]["status"] == "running"
    with pytest.raises(CampaignError, match="incomplete"):
        reopened.finish(binding)
    assert reopened.open_attempts() == [binding]
    for name in ("telemetry", "audit", "usage"):
        reopened.capture(binding, name, source[name])
    final = reopened.finish(binding)
    assert digest(reopened.record_bytes(final)) == final
    assert reopened.export()["runs"][0]["recordDigest"] == final
    assert reopened.open_attempts() == []
    with pytest.raises(CampaignError, match="duplicate"):
        reopened.finish(binding)
    with pytest.raises(CampaignError, match="only a finished harness"):
        reopened.start("test-native")
    with pytest.raises(CampaignError, match="not found"):
        reopened.record_bytes(store.preparation_digest)


def test_append_only_and_transaction_rollback(tmp_path):
    store = prepare(tmp_path)
    with sqlite3.connect(store.path) as db:
        for statement in ("UPDATE journal SET payload='bad'", "DELETE FROM journal"):
            with pytest.raises(sqlite3.IntegrityError, match="append-only"):
                db.execute(statement)
    with pytest.raises(RuntimeError):
        with store._transaction() as db:
            db.execute("INSERT INTO journal VALUES(2, ?)", (b"partial",))
            raise RuntimeError("synthetic crash before commit")
    assert store.accounting()["attempts"] == 0
    process = subprocess.run([sys.executable, "-c", """
import os, sqlite3, sys
db = sqlite3.connect(sys.argv[1])
db.execute("BEGIN IMMEDIATE")
db.execute("INSERT INTO journal VALUES (2, ?)", (b"partial",))
os._exit(9)
""", str(store.path)], check=False)
    assert process.returncode == 9
    assert store.export()["runs"][0]["status"] == "pending"


def test_retry_once_at_end_of_block_exclusion_and_metrics(tmp_path):
    store = prepare(tmp_path)
    first = store.start("test-native")
    failed = finish(store, first, error="SECRET backend error; =SUM(1,2)")
    waiting = store.export()["runs"][0]
    assert (waiting["status"], waiting["attempts"], waiting["harnessFailures"]) == ("running", 1, 1)
    assert waiting["recordDigest"] is None and waiting["agentSeconds"] is None
    failed_record = read_json(store.record_bytes(failed))
    assert failed_record["data"]["verified"]["outcome"]["agentTerminalClass"] == "validated_success"
    with pytest.raises(CampaignError, match="end of its block"):
        store.start("test-native")
    finish(store, store.start("test-architecture"), calls=[], terminal="refusal")
    finish(store, store.start("test-radius"), error="second arm synthetic failure")
    retry = store.start("test-native")
    assert retry.attempt == 2 and retry.assignment == first.assignment
    assert store.export()["runs"][0]["harnessFailures"] == 1
    finish(store, retry, metrics={"agentSeconds": 8, "toolCalls": 0, "aiCredits": 1})
    finish(store, store.start("test-radius"), error="second harness failure")
    report = store.export(complete=True)
    assert [r["status"] for r in report["runs"]] == ["validated_success", "refusal", "excluded"]
    assert report["runs"][0]["agentSeconds"] == 8
    assert report["runs"][0]["toolCalls"] == 0
    assert report["runs"][2]["attempts"] == report["runs"][2]["harnessFailures"] == 2
    assert "SECRET" not in json.dumps(report) and "SUM(" not in json.dumps(report)
    account = store.accounting()
    assert (account["planned"], account["scored"], account["excluded"], account["pending"],
            account["running"], account["attempts"], account["harnessFailures"]) == (3, 2, 1, 0, 0, 5, 3)
    assert account["planned"] == account["scored"] + account["excluded"] + account["pending"] + account["running"]
    assert account["retryInclusiveMetrics"]["agentSeconds"]["availableSum"] == 18
    assert account["retryInclusiveMetrics"]["aiCredits"] == {
        "availableSum": 1, "availableAttempts": 1, "missingAttempts": 4,
    }
    with pytest.raises(CampaignError, match="retry limit"):
        store.start("test-radius")


@pytest.mark.parametrize("kwargs,expected", [
    ({}, "validated_success"),
    ({"calls": []}, "no_submission"),
    ({"calls": ["malformed", {}]}, "invalid_structured_output"),
    ({"calls": [], "terminal": "budget_exhaustion"}, "budget_exhaustion"),
    ({"calls": [], "terminal": "refusal"}, "refusal"),
    ({"terminal": "isolation_violation_attempt"}, "isolation_violation_attempt"),
    ({"fault": False}, "diagnosis_failure"),
    ({"telemetry": {"cpu": "different", "symptom": "99%"}}, "diagnosis_failure"),
    ({"audit": {"scope": "changed", "safety": "intact", "cleanup": "intact"}}, "isolation_violation_attempt"),
])
def test_agent_classes_and_no_retry(tmp_path, kwargs, expected):
    store = prepare(tmp_path)
    final = finish(store, store.start("test-native"), **kwargs)
    row = store.export()["runs"][0]
    assert row["status"] == expected and row["recordDigest"] == final
    assert row["attempts"] == 1 and row["harnessFailures"] == 0
    assert store.accounting()["scored"] == 1
    with pytest.raises(CampaignError, match="only a finished harness"):
        store.start("test-native")


@pytest.mark.parametrize("fault,expected", [(False, "validated_success"), (True, "diagnosis_failure")])
def test_healthy_distinction(tmp_path, fault, expected):
    store = prepare(tmp_path, healthy=True)
    finish(store, store.start("test-native"), fault=fault)
    assert store.export()["runs"][0]["status"] == expected


@pytest.mark.parametrize("metric", campaign.METRICS)
@pytest.mark.parametrize("bad", [-1, True, "3"])
def test_bad_metrics_are_not_outcomes(tmp_path, metric, bad):
    store = prepare(tmp_path)
    binding = store.start("test-native")
    values = dict.fromkeys(campaign.METRICS)
    values[metric] = bad
    for name, data in captures(binding, metrics=values).items():
        store.capture(binding, name, data)
    with pytest.raises(CampaignError, match="metrics"):
        store.finish(binding)
    assert store.open_attempts() == [binding]
    assert store.export()["runs"][0]["recordDigest"] is None


def test_nonfinite_fractional_and_missing_metrics():
    campaign._metrics({"agentSeconds": 1e20, "toolCalls": 2**54, "aiCredits": 1e20})
    for values in (
        {"agentSeconds": float("inf"), "toolCalls": None, "aiCredits": None},
        {"agentSeconds": float("nan"), "toolCalls": None, "aiCredits": None},
        {"agentSeconds": None, "toolCalls": 1.5, "aiCredits": None},
        {"agentSeconds": None, "toolCalls": None},
    ):
        with pytest.raises(CampaignError):
            campaign._metrics(values)
    with pytest.raises(CampaignError, match="finite numeric range"):
        campaign._metrics({"agentSeconds": 10**400, "toolCalls": None, "aiCredits": None})


def test_retry_inclusive_totals_refuse_overflow(tmp_path):
    store = prepare(tmp_path)
    complete_block(store, metrics={"agentSeconds": 1e308, "toolCalls": 0, "aiCredits": None})
    assert all(row["agentSeconds"] == 1e308 for row in store.export(complete=True)["runs"])
    with pytest.raises(CampaignError, match="finite"):
        store.accounting()


def test_binding_duplicate_and_foreign_sources(tmp_path):
    store = prepare(tmp_path)
    binding = store.start("test-native")
    with pytest.raises(CampaignError, match="only a finished"):
        store.start("test-native")
    with pytest.raises(CampaignError, match="unknown logical"):
        store.start("foreign")
    with pytest.raises(CampaignError, match="not started"):
        store.finish(replace(binding, assignment={"runId": "foreign"}))
    with pytest.raises(CampaignError, match="mismatched"):
        store.capture(replace(binding, attempt=2), "audit", b"wrong")
    for name, raw in captures(replace(binding, attempt=2)).items():
        store.capture(binding, name, raw)
    with pytest.raises(CampaignError, match="different trial"):
        store.finish(binding)
    with pytest.raises(CampaignError, match="duplicate source"):
        store.capture(binding, "audit", b"replacement")
    with pytest.raises(CampaignError, match="empty"):
        store.capture(binding, "empty", b"")
    with pytest.raises(CampaignError, match="public identifier"):
        store.capture(binding, "../escape", b"bytes")
    assert store.export()["runs"][0]["status"] == "running"


@pytest.mark.parametrize("bad", [
    b'{"x":1,"x":2}', b'{"x":NaN}', b'{"x":Infinity}', b'{"unfinished":',
])
def test_strict_json(bad):
    with pytest.raises(CampaignError):
        read_json(bad)


def test_unregistered_drifted_or_synthetic_verifier(tmp_path):
    store = prepare(tmp_path)
    finish(store, store.start("test-native"))
    with pytest.raises(CampaignError, match="no trusted verifier"):
        CampaignStore(store.path, store.preparation_digest).export()
    drifted = replace(verifier(), synthetic=False)
    with pytest.raises(CampaignError, match="provenance"):
        CampaignStore(store.path, store.preparation_digest, {drifted.name: drifted}).export()
    scored_path = tmp_path / "scored"
    scored_path.mkdir()
    scored = prepare(scored_path, phase="scored")
    binding = scored.start("test-native")
    for name, data in captures(binding).items():
        scored.capture(binding, name, data)
    with pytest.raises(CampaignError, match="synthetic"):
        scored.finish(binding)
    assert scored.export()["campaign"]["phase"] == "scored"


@pytest.mark.parametrize("change", [
    lambda event: event.update(sequence=99),
    lambda event: event.update(previous="sha256:" + "0" * 64),
    lambda event: event.update(kind="not-an-event"),
    lambda event: event["data"]["binding"].update(attempt=2),
    lambda event: event["data"]["binding"]["assignment"].update(seed="foreign"),
    lambda event: event["data"]["binding"].update(preparationDigest="sha256:" + "0" * 64),
])
def test_reject_reordered_foreign_events(tmp_path, change):
    store = prepare(tmp_path)
    store.start("test-native")
    rewrite_event(store, 1, change, rechain=False)
    with pytest.raises(CampaignError):
        store.export()


def test_corrupt_bytes_partial_and_noncanonical(tmp_path):
    store = prepare(tmp_path)
    store.start("test-native")
    with sqlite3.connect(store.path) as db:
        db.execute("DROP TRIGGER no_update")
        original = db.execute("SELECT payload FROM journal WHERE sequence=2").fetchone()[0]
        db.execute("UPDATE journal SET payload=? WHERE sequence=2", (b'{"partial":',))
    with pytest.raises(CampaignError, match="malformed"):
        store.export()
    with sqlite3.connect(store.path) as db:
        db.execute("UPDATE journal SET payload=? WHERE sequence=2", (original + b" ",))
    with pytest.raises(CampaignError, match="noncanonical"):
        store.export()


def test_digest_match_does_not_validate_forged_claim(tmp_path):
    store = prepare(tmp_path)
    finish(store, store.start("test-native"), fault=False)
    def forge(event):
        verified = event["data"]["verified"]
        verified["outcome"].update(terminalClass="validated_success", agentTerminalClass="validated_success",
                                   scored=True, scoredAsFailure=False)
        verified["metrics"]["agentSeconds"] = 0
    rewrite_event(store, -1, forge)
    with pytest.raises(CampaignError, match="verifier replay"):
        store.export()


def test_source_checksum_and_capture_binding(tmp_path):
    store = prepare(tmp_path)
    finish(store, store.start("test-native"))
    rewrite_event(store, 2, lambda event: event["data"]["source"].update(digest="sha256:" + "0" * 64))
    with pytest.raises(CampaignError, match="checksum"):
        store.export()


def test_finished_sources_cannot_replace_saved_capture(tmp_path):
    store = prepare(tmp_path)
    finish(store, store.start("test-native"))
    rewrite_event(store, -1, lambda event: event["data"]["sources"].pop("audit"))
    with pytest.raises(CampaignError, match="differ from captures"):
        store.export()


def test_cli_uses_same_store_and_refuses_terminal_without_registry(tmp_path, capsys):
    spec = tmp_path / "spec.json"
    spec.write_bytes(canonical_bytes(specification()))
    db = tmp_path / "campaign.sqlite"
    assert main(["prepare", "--spec", str(spec), "--store", str(db)]) == 0
    receipt = capsys.readouterr().out.strip()
    report = tmp_path / "report.json"
    assert main(["export", "--store", str(db), "--receipt", receipt, "--output", str(report)]) == 0
    assert read_json(report.read_bytes())["runs"][0]["status"] == "pending"
    with pytest.raises(SystemExit):
        main(["export", "--store", str(db), "--receipt", receipt, "--output", str(report)])
    store = CampaignStore(db, receipt, {verifier().name: verifier()})
    complete_block(store)
    final_path = tmp_path / "final.json"
    args = ["export", "--store", str(db), "--receipt", receipt, "--output", str(final_path), "--complete"]
    with pytest.raises(SystemExit):
        main(args)
    assert not final_path.exists()
    assert main(args, verifiers=store.verifiers) == 0
    assert read_json(final_path.read_bytes()) == store.export(complete=True)
    totals = tmp_path / "totals.json"
    assert main(["accounting", "--store", str(db), "--receipt", receipt, "--output", str(totals)],
                verifiers=store.verifiers) == 0
    assert read_json(totals.read_bytes()) == store.accounting()


def test_generated_reports_through_shipped_dashboard(tmp_path):
    store = prepare(tmp_path)
    reports = [store.export()]
    first = store.start("test-native")
    reports.append(store.export())
    finish(store, first, error="synthetic harness fault")
    reports.append(store.export())
    finish(store, store.start("test-architecture"), calls=[])
    finish(store, store.start("test-radius"))
    retry = store.start("test-native")
    reports.append(store.export())
    finish(store, retry, error="synthetic exclusion")
    reports.append(store.export(complete=True))
    healthy_path = tmp_path / "healthy"
    healthy_path.mkdir()
    healthy = prepare(healthy_path, healthy=True)
    finish(healthy, healthy.start("test-native"), fault=True)
    finish(healthy, healthy.start("test-architecture"), calls=[])
    finish(healthy, healthy.start("test-radius"))
    reports.append(healthy.export(complete=True))
    scored_path = tmp_path / "scored"
    scored_path.mkdir()
    reports.append(prepare(scored_path, phase="scored").export())
    files = []
    for index, report in enumerate(reports):
        path = tmp_path / f"report-{index}.json"
        path.write_bytes(canonical_bytes(report))
        files.append(str(path))
    result = subprocess.run(["node", str(Path(__file__).with_name("dashboard_checks.cjs")), *files],
                            capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    summaries = [json.loads(line.split(":", 1)[1]) for line in result.stdout.splitlines()
                 if line.startswith("EXPORTED_SUMMARY:")]
    assert len(summaries) == len(reports)
    for report, summary in zip(reports, summaries):
        assert summary["progress"]["planned"] == 3
        assert summary["progress"]["scored"] == sum(row["status"] in campaign.AGENT_CLASSES for row in report["runs"])
        assert summary["progress"]["harnessFailures"] == sum(row["harnessFailures"] for row in report["runs"])
    assert summaries[4]["models"][0]["contrasts"][0]["n"] == 0
    assert summaries[4]["models"][0]["contrasts"][0]["omitted"] == 1
    assert summaries[5]["models"][0]["arms"][0]["falseAlarms"] == 1
    assert summaries[5]["models"][0]["arms"][1]["missingHealthyAnswers"] == 1
    assert summaries[6]["embargoed"] and summaries[6]["models"] == summaries[6]["rows"] == []


@pytest.mark.parametrize("change,message", [
    (lambda o: o.update(terminalClass="submitted"), "unknown diagnosis outcome"),
    (lambda o: o.update(agentTerminalClass="submitted"), "unknown agent outcome"),
    (lambda o: o.update(scored=1), "outcome flags"),
    (lambda o: o.update(valid=False), "outcome flags"),
    (lambda o: o.update(agentTerminalClass=None), "agent outcome mismatch"),
    (lambda o: o.update(reasons=[""]), "invalid outcome reasons"),
    (lambda o: o.update(rejectedSubmissionAttempts=-1), "rejected-call count"),
    (lambda o: o.update(staticScreen="maybe"), "confinement"),
    (lambda o: o.update(shellEnabled=1), "confinement"),
    (lambda o: o.update(budgetStopReason=7), "budget reason"),
    (lambda o: o["validators"].update(scope="unknown"), "validator results"),
    (lambda o: o["validatorEvidence"].pop("scope"), "evidence coverage"),
    (lambda o: o["validators"].update(cleanup="fail"), "lifecycle evidence"),
    (lambda o: o["validators"].update(safety="fail"), "prohibited action"),
    (lambda o: o.update(submission=[]), "submission must be an object"),
    (lambda o: o["submission"].update(remediation=" test "), "noncanonical submission"),
    (lambda o: o.update(submission=None), "incomplete diagnosis grade"),
    (lambda o: o["validatorEvidence"].update(evidence=[]), "empty citation reviews"),
    (lambda o: o["validators"].update(diagnosis="fail"), "saved grade contradicts"),
    (lambda o: o["validators"].update(evidence="fail"), "saved grade contradicts"),
    (lambda o: o["validatorEvidence"]["diagnosis"].update(mechanismPassed=None), "explicit mechanism"),
])
def test_outcome_structure_controls(change, message):
    row = specification()["assignments"][0]
    binding = Binding("sha256:" + "a" * 64, row, 1)
    sources = captures(binding)
    outcome = synthetic_replay(binding, sources).outcome.to_json_dict()
    campaign._validate_outcome(outcome, binding, sources)
    change(outcome)
    with pytest.raises(CampaignError, match=message):
        campaign._validate_outcome(outcome, binding, sources)


def test_outcome_agent_claims_are_not_pass_labels():
    binding = Binding("sha256:" + "a" * 64, specification()["assignments"][0], 1)
    sources = captures(binding)
    def valid():
        return synthetic_replay(binding, sources).outcome.to_json_dict()
    outcome = valid()
    with pytest.raises(CampaignError, match="expectedFault differs"):
        campaign._validate_outcome(outcome, replace(binding, assignment={
            **binding.assignment, "expectedFault": False}), sources)
    outcome["validators"].pop("diagnosis")
    outcome["validators"].pop("evidence")
    outcome["validatorEvidence"].pop("diagnosis")
    outcome["validatorEvidence"].pop("evidence")
    with pytest.raises(CampaignError, match="agent diagnosis outcome lacks grade"):
        campaign._validate_outcome(outcome, binding, sources)
    outcome = valid()
    outcome.update(terminalClass="diagnosis_failure", agentTerminalClass="diagnosis_failure",
                   scored=False, scoredAsFailure=True, reasons=["synthetic"])
    with pytest.raises(CampaignError, match="agent diagnosis contradicts"):
        campaign._validate_outcome(outcome, binding, sources)
    outcome.update(terminalClass="refusal", agentTerminalClass="refusal")
    with pytest.raises(CampaignError, match="no-answer class contains"):
        campaign._validate_outcome(outcome, binding, sources)
    no_answer_sources = captures(binding, calls=[])
    outcome = synthetic_replay(binding, no_answer_sources).outcome.to_json_dict()
    outcome["reasons"] = []
    with pytest.raises(CampaignError, match="missing failure reason"):
        campaign._validate_outcome(outcome, binding, no_answer_sources)
    outcome["reasons"] = ["synthetic"]
    outcome["rejectedSubmissionAttempts"] = 1
    with pytest.raises(CampaignError, match="contradicts rejected"):
        campaign._validate_outcome(outcome, binding, no_answer_sources)


@pytest.mark.parametrize("refs,sources,message", [
    ([], {"audit": b"x"}, "missing examined"),
    ("audit", {"audit": b"x"}, "missing examined"),
    (["missing"], {"audit": b"x"}, "source missing"),
    (["audit"], {"audit": b""}, "source missing"),
    ([1], {"audit": b"x"}, "source missing"),
])
def test_examined_source_controls(refs, sources, message):
    campaign._references(["audit"], {"audit": b"x"})
    with pytest.raises(CampaignError, match=message):
        campaign._references(refs, sources)


def broken_replay(binding, sources):
    result = synthetic_replay(binding, sources)
    result.outcome.validators["cleanup"] = "fail"
    return result


def null_replay(binding, sources):
    return None


@pytest.mark.parametrize("callback,message", [
    (broken_replay, "lifecycle evidence"), (null_replay, "canonical TrialOutcome"),
])
def test_verifier_output_contract_is_wired(tmp_path, callback, message):
    check = replace(verifier(), replay=callback)
    spec = specification()
    for row in spec["assignments"]:
        row["verifierDigest"] = check.fingerprint()
    store = CampaignStore.prepare(tmp_path / "store.sqlite", spec, verifiers={check.name: check})
    binding = store.start("test-native")
    for name, raw in captures(binding).items():
        store.capture(binding, name, raw)
    with pytest.raises(CampaignError, match=message):
        store.finish(binding)
    assert store.export()["runs"][0]["status"] == "running"


def test_verifier_provenance_controls(monkeypatch):
    check = verifier()
    for bad, message in [
        (replace(check, synthetic="yes"), "synthetic must be boolean"),
        (replace(check, sources=()), "empty or duplicate"),
        (replace(check, sources=(Path(__file__), Path(__file__))), "empty or duplicate"),
        (replace(check, sources=(Path(campaign.__file__),)), "absent from provenance"),
    ]:
        with pytest.raises(CampaignError, match=message):
            bad.fingerprint()
    binding = Binding("sha256:" + "a" * 64, specification()["assignments"][0], 1)
    with pytest.raises(CampaignError, match="empty captured evidence"):
        campaign._verify(binding, {}, {check.name: check}, "pilot")
    with pytest.raises(CampaignError, match="invalid captured artifact"):
        campaign._verify(binding, {"audit": ""}, {check.name: check}, "pilot")
    calls = iter([binding.assignment["verifierDigest"], "sha256:" + "0" * 64])
    monkeypatch.setattr(Verifier, "fingerprint", lambda self: next(calls))
    with pytest.raises(CampaignError, match="changed during replay"):
        campaign._verify(binding, captures(binding), {check.name: check}, "pilot")


def test_digest_and_duplicate_id_controls():
    campaign._digest("sha256:" + "0" * 64)
    for bad in (None, "sha256:x", "0" * 64):
        with pytest.raises(CampaignError, match="SHA-256"):
            campaign._digest(bad)
    spec = specification()
    spec["assignments"][1]["runId"] = spec["assignments"][0]["runId"]
    with pytest.raises(CampaignError, match="duplicate logical"):
        validate_preparation(spec)


@pytest.mark.parametrize("encoded,message", [
    ({}, "missing captured sources"),
    ({"audit": {"digest": digest(b"x"), "bytes": 1}}, "invalid captured bytes"),
    ({"audit": {"digest": digest(b""), "bytes": ""}}, "empty captured source"),
    ({"audit": {"digest": digest(b"x"), "bytes": "%%%"}}, "malformed captured bytes"),
])
def test_decode_sources_controls(encoded, message):
    assert CampaignStore._decode_sources({"audit": {"digest": digest(b"x"), "bytes": "eA=="}}) == {"audit": b"x"}
    with pytest.raises(CampaignError, match=message):
        CampaignStore._decode_sources(encoded)


def test_missing_empty_and_nonbinary_database(tmp_path):
    store = prepare(tmp_path)
    link = tmp_path / "link.sqlite"
    link.symlink_to(store.path)
    with pytest.raises(CampaignError, match="database missing"):
        CampaignStore(link, store.preparation_digest).export()
    empty = tmp_path / "empty.sqlite"
    with sqlite3.connect(empty) as db:
        db.execute("CREATE TABLE journal (sequence INTEGER PRIMARY KEY, payload BLOB NOT NULL)")
    with pytest.raises(CampaignError, match="missing preparation"):
        CampaignStore(empty, store.preparation_digest).export()
    with sqlite3.connect(empty) as db:
        db.execute("INSERT INTO journal VALUES (1, 'text')")
    with pytest.raises(CampaignError, match="canonical bytes"):
        CampaignStore(empty, store.preparation_digest).export()


@pytest.mark.parametrize("change,message", [
    (lambda e: e["data"]["binding"]["assignment"].update(runId="foreign"), "foreign assignment"),
    (lambda e: e["data"]["binding"].update(attempt=3), "invalid attempt number"),
    (lambda e: e["data"]["binding"].update(attempt=True), "invalid attempt number"),
])
def test_journal_binding_type_controls(tmp_path, change, message):
    store = prepare(tmp_path)
    store.start("test-native")
    rewrite_event(store, 1, change)
    with pytest.raises(CampaignError, match=message):
        store.export()


def test_boolean_assignment_binding_is_not_numeric_equality(tmp_path):
    store = prepare(tmp_path)
    binding = store.start("test-native")
    numeric = replace(binding, assignment={**binding.assignment, "expectedFault": 1})
    with pytest.raises(CampaignError, match="mismatched finish"):
        store.capture(numeric, "audit", b"wrong binding")
    rewrite_event(store, 1, lambda e: e["data"]["binding"]["assignment"].update(expectedFault=1))
    with pytest.raises(CampaignError, match="assignment binding"):
        store.export()


def test_boolean_outcome_flags_are_not_numeric_equality(tmp_path):
    store = prepare(tmp_path)
    finish(store, store.start("test-native"))
    rewrite_event(store, -1, lambda e: e["data"]["verified"]["outcome"].update(scored=1))
    with pytest.raises(CampaignError, match="verifier replay"):
        store.export()


def test_duplicate_and_unstarted_finish_journal_controls(tmp_path):
    store = prepare(tmp_path)
    finish(store, store.start("test-native"))
    def duplicate(events_index):
        with store._transaction() as db:
            raw = [row[0] for row in db.execute("SELECT payload FROM journal ORDER BY sequence")]
            old = read_json(raw[events_index])
            store._append(db, raw, old["kind"], old["data"])
    duplicate(-1)
    with pytest.raises(CampaignError, match="matching open attempt"):
        store.export()


def test_duplicate_capture_journal_control(tmp_path):
    store = prepare(tmp_path)
    binding = store.start("test-native")
    source = captures(binding)["adapter"]
    store.capture(binding, "adapter", source)
    with store._transaction() as db:
        raw = [row[0] for row in db.execute("SELECT payload FROM journal ORDER BY sequence")]
        capture = read_json(raw[-1])
        store._append(db, raw, "captured", capture["data"])
    with pytest.raises(CampaignError, match="duplicate source capture"):
        store.export()


def test_concurrent_start_does_not_duplicate_assignment(tmp_path):
    store = prepare(tmp_path)
    def start():
        try:
            return store.start("test-native")
        except CampaignError as exc:
            return exc
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: start(), range(2)))
    assert sum(isinstance(result, Binding) for result in results) == 1
    assert sum(isinstance(result, CampaignError) for result in results) == 1
    assert store.accounting()["attempts"] == 1


def test_sandbox_result_cannot_be_overridden_by_success():
    binding = Binding("sha256:" + "a" * 64, specification()["assignments"][0], 1)
    sources = captures(binding)
    outcome = synthetic_replay(binding, sources).outcome.to_json_dict()
    outcome.update(shellEnabled=True, staticScreen="off")
    with pytest.raises(CampaignError, match="confinement failure"):
        campaign._validate_outcome(outcome, binding, sources)
    gate = campaign.SandboxGateResult(False, "synthetic failure", 1, 0).to_json_dict()
    outcome["sandboxGate"] = gate
    with pytest.raises(CampaignError, match="confinement failure"):
        campaign._validate_outcome(outcome, binding, sources)
    gate["passed"] = "yes"
    with pytest.raises(CampaignError, match="invalid sandbox verdict"):
        campaign._validate_outcome(outcome, binding, sources)
    gate["passed"] = True
    campaign._validate_outcome(outcome, binding, sources)


def test_fingerprint_pins_core_sources_and_entrypoint(monkeypatch):
    check = verifier()
    fingerprint = check.fingerprint()
    assert replace(check, replay=broken_replay).fingerprint() != fingerprint
    read_bytes = Path.read_bytes
    monkeypatch.setattr(Path, "read_bytes", lambda path: read_bytes(path) + (
        b"\n# changed" if path.name == "sandbox.py" else b""
    ))
    assert check.fingerprint() != fingerprint
