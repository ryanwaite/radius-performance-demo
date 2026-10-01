"""Small file-based human adjudication boundary and unqualified CPU reference.

Raw captures and reviewer IDs are operator attestations, not authenticated Shop
producers. The reference verifier is deliberately barred from scored campaigns.
No prose parser or model judge is used.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import sqlite3
from typing import Any, Mapping

from .campaign import (
    AGENT_CLASSES, Binding, CampaignStore, VerifiedAttempt, Verifier,
    _digest, _identifier, _metrics, _shape, canonical_bytes, digest, read_json, require,
)
from .diagnosis import EvidenceReview, ExpectedDiagnosis, ReviewRequired, grade_diagnosis
from .submit_tool import ComponentMap, SubmissionRecorder
from .trial_outcome import ValidationCheck, score_trial


RUBRIC = {
    "id": "cpu-quota-reference-v1",
    "cause": "A lower CPU quota causes throttling, not greater application work or a slow dependency.",
    "requiredEvidence": [
        "Compare the measured active CPU quota with the frozen baseline.",
        "Use the target's increasing throttled-period and throttled-time counters, over a nonempty observation window.",
    ],
    "mechanismReview": [
        "Accept any wording that correctly explains the reduced quota and a compatible repair.",
        "Reject an asserted increase in workload, garbage collection, database latency or other alternative cause without evidence, even if category and target match.",
        "Read the whole answer. Reject contradictions, negation of the actual cause and unsupported causal assertions; matching words or a latency symptom do not suffice.",
    ],
    "citationReview": [
        "For every citation, decide whether its complete observation is supported by the displayed measurements and relevant to the target's causal path.",
        "Reject fabricated values, reversed comparisons, correlated non-causal symptoms and unsupported extra clauses.",
    ],
    "healthyReview": [
        "Require evidence of an unchanged quota and no increased throttling for this reference incident.",
        "Do not infer general application health from this CPU-only control.",
    ],
    "uncertainty": "Record needs_review if evidence or meaning cannot be adjudicated. Do not guess or fail unfamiliar phrasing.",
}
RUBRIC_DIGEST = digest(canonical_bytes(RUBRIC))
INPUTS = ("adapter", "measurements", "lifecycle", "usage")
REVIEW_PREFIX = "human-review-"
REVIEW_INPUT_PREFIX = "review-input-"


def _positive_integer(value: Any, *, zero: bool = False) -> None:
    require(type(value) is int and value >= (0 if zero else 1), "invalid counter")


def _quota(text: Any) -> tuple[int, int]:
    require(isinstance(text, str), "cpu.max must be raw text")
    fields = text.split()
    require(len(fields) == 2 and all(field.isascii() and field.isdecimal() for field in fields),
            "reference cpu.max requires a finite quota and period")
    quota, period = map(int, fields)
    _positive_integer(quota)
    _positive_integer(period)
    return quota, period


def _stat(text: Any) -> dict[str, int]:
    require(isinstance(text, str) and bool(text.strip()), "cpu.stat must be nonempty raw text")
    result = {}
    for line in text.splitlines():
        fields = line.split()
        require(len(fields) == 2 and fields[1].isascii() and fields[1].isdecimal(),
                "malformed cpu.stat")
        require(fields[0] not in result, "duplicate cpu.stat counter")
        result[fields[0]] = int(fields[1])
    require({"nr_periods", "nr_throttled", "throttled_usec"} <= result.keys(),
            "cpu.stat is missing required counters")
    require(result["nr_throttled"] <= result["nr_periods"], "impossible throttled periods")
    return result


@dataclass(frozen=True)
class CPUReference:
    config_path: Path

    def config(self) -> dict[str, Any]:
        config = read_json(self.config_path.read_bytes())
        _shape(config, ("target", "componentAliases", "baselineCpuMax", "reviewers"))
        _identifier(config["target"])
        mapping = ComponentMap(config["componentAliases"])
        require(config["target"] in mapping.canonical_names, "reference target missing from inventory")
        _quota(config["baselineCpuMax"])
        require(type(config["reviewers"]) is list and bool(config["reviewers"])
                and len(set(config["reviewers"])) == len(config["reviewers"]), "missing or duplicate reviewers")
        for reviewer in config["reviewers"]:
            _identifier(reviewer)
        return config

    @property
    def configuration_id(self) -> str:
        return "cpu-reference-" + digest(canonical_bytes(self.config())).removeprefix("sha256:")

    def verifier(self) -> Verifier:
        self.config()
        return Verifier("cpu-quota-reference-v1", self.replay,
                        (Path(__file__), self.config_path), synthetic=True)

    def inputs(self, binding: Binding, sources: Mapping[str, bytes]) -> dict[str, Any]:
        config = self.config()
        require(binding.assignment["configurationId"] == self.configuration_id,
                "reference configuration differs from assignment")
        incident = binding.assignment["incident"]
        require(incident in ("cpu-quota-fault/v1", "cpu-quota-healthy/v1")
                and binding.assignment["expectedFault"] is (incident == "cpu-quota-fault/v1"),
                "reference incident differs from assigned fault")
        require(set(INPUTS) <= sources.keys(), "reference capture incomplete")
        require(all(name in INPUTS or name.startswith((REVIEW_PREFIX, REVIEW_INPUT_PREFIX)) for name in sources),
                "unexpected reference source")
        data = {}
        for name in INPUTS:
            envelope = read_json(sources[name])
            _shape(envelope, ("binding", "data"))
            require(canonical_bytes(envelope["binding"]) == canonical_bytes(binding.to_dict()),
                    "reference source belongs to another assignment or attempt")
            data[name] = envelope["data"]
        data["config"] = config
        return data

    def recorder(self, adapter: Any, config: dict[str, Any]) -> SubmissionRecorder:
        _shape(adapter, ("terminalClass", "calls", "error", "budgetStopReason"))
        require(adapter["terminalClass"] in (*AGENT_CLASSES, "harness_failure"), "unknown adapter terminal class")
        require(type(adapter["calls"]) is list, "adapter calls must be a list")
        require(adapter["error"] is None or isinstance(adapter["error"], str), "invalid adapter error")
        require(adapter["budgetStopReason"] is None or isinstance(adapter["budgetStopReason"], str),
                "invalid adapter budget stop")
        recorder = SubmissionRecorder(ComponentMap(config["componentAliases"]))
        for call in adapter["calls"]:
            recorder.record(call)
        return recorder

    def observations(self, raw: Any, config: dict[str, Any]) -> dict[str, Any]:
        _shape(raw, ("target", "baselineCpuMax", "activeCpuMax", "beforeCpuStat", "afterCpuStat"))
        require(raw["target"] == config["target"], "measurements belong to another target")
        baseline = _quota(raw["baselineCpuMax"])
        active = _quota(raw["activeCpuMax"])
        require(baseline == _quota(config["baselineCpuMax"]), "baseline differs from frozen configuration")
        before, after = _stat(raw["beforeCpuStat"]), _stat(raw["afterCpuStat"])
        keys = ("nr_periods", "nr_throttled", "throttled_usec")
        require(all(after[key] >= before[key] for key in keys), "cgroup counters regressed")
        require(after["nr_periods"] > before["nr_periods"], "empty CPU observation window")
        quota_comparison = active[0] * baseline[1] - baseline[0] * active[1]
        deltas = {key: after[key] - before[key] for key in keys}
        require(deltas["nr_throttled"] <= deltas["nr_periods"], "impossible throttled delta")
        return {
            "target": config["target"],
            "baseline": {"quotaMicros": baseline[0], "periodMicros": baseline[1]},
            "active": {"quotaMicros": active[0], "periodMicros": active[1]},
            "before": {key: before[key] for key in keys},
            "after": {key: after[key] for key in keys},
            "faultObserved": quota_comparison < 0 and deltas["nr_throttled"] > 0 and deltas["throttled_usec"] > 0,
            "healthyObserved": quota_comparison == 0 and deltas["nr_throttled"] == 0 and deltas["throttled_usec"] == 0,
        }

    def packet(self, binding: Binding, sources: Mapping[str, bytes]) -> dict[str, Any]:
        data = self.inputs(binding, sources)
        recorder = self.recorder(data["adapter"], data["config"])
        require(recorder.submission is not None, "no schema-valid answer needs review")
        answer = recorder.submission.to_json_dict()
        answer.pop("componentAsSubmitted")
        answer.pop("connectionAsSubmitted")
        return {
            "schemaVersion": "radius-adjudication-packet-v1",
            "rubric": RUBRIC, "rubricDigest": RUBRIC_DIGEST,
            # Opaque digests bind identical answers to their distinct attempts
            # without showing treatment/assignment labels to the reviewer.
            "captureDigests": {name: digest(sources[name]) for name in INPUTS},
            "expectedFault": binding.assignment["expectedFault"],
            "answer": answer,
            "observations": self.observations(data["measurements"], data["config"]),
        }

    @staticmethod
    def review_template(packet: dict[str, Any]) -> dict[str, Any]:
        return {
            "schemaVersion": "radius-human-adjudication-v1",
            "packetDigest": digest(canonical_bytes(packet)), "rubricDigest": packet["rubricDigest"],
            "reviewer": "", "status": "needs_review", "mechanismPassed": None,
            "rationale": "",
            "citations": [{"index": index, "relevant": None, "supported": None, "rationale": ""}
                          for index, _ in enumerate(packet["answer"]["evidence"])],
        }

    def decision(self, packet: dict[str, Any], sources: Mapping[str, bytes]) -> tuple[str, dict[str, Any]]:
        names = [name for name in sources if name.startswith(REVIEW_PREFIX)]
        expected_names = [f"{REVIEW_PREFIX}{index:04}" for index in range(1, len(names) + 1)]
        require(names == expected_names, "human review sequence is missing or reordered")
        decisions = []
        for name in names:
            review = read_json(sources[name])
            _shape(review, ("schemaVersion", "packetDigest", "rubricDigest", "reviewer", "status",
                            "mechanismPassed", "rationale", "citations"))
            require(review["schemaVersion"] == "radius-human-adjudication-v1", "unknown human review schema")
            require(review["packetDigest"] == digest(canonical_bytes(packet))
                    and review["rubricDigest"] == RUBRIC_DIGEST, "human review packet or rubric mismatch")
            require(review["reviewer"] in self.config()["reviewers"], "unregistered reviewer")
            require(review["status"] in ("decided", "needs_review"), "unknown human review status")
            require(isinstance(review["rationale"], str) and bool(review["rationale"].strip()),
                    "human review requires rationale")
            require(type(review["citations"]) is list and
                    len(review["citations"]) == len(packet["answer"]["evidence"]), "human review citation coverage")
            values = [review["mechanismPassed"]]
            for index, citation in enumerate(review["citations"]):
                _shape(citation, ("index", "relevant", "supported", "rationale"))
                require(type(citation["index"]) is int and citation["index"] == index, "reordered human citation")
                require(isinstance(citation["rationale"], str) and bool(citation["rationale"].strip()),
                        "citation review requires rationale")
                values.extend((citation["relevant"], citation["supported"]))
            require(all(type(value) is bool for value in values) if review["status"] == "decided"
                    else all(value is None or type(value) is bool for value in values),
                    "human review decisions must be explicit")
            require(not decisions, "cannot replace a decided review")
            if review["status"] == "decided":
                decisions.append((name, review))
        if not decisions:
            raise ReviewRequired("causal prose awaits human adjudication")
        return decisions[0]

    @staticmethod
    def lifecycle(raw: Any) -> dict[str, ValidationCheck]:
        _shape(raw, ("workspaceBefore", "workspaceAfter", "actions", "resourcesBefore", "resourcesAfter"))
        for key in ("workspaceBefore", "workspaceAfter"):
            require(type(raw[key]) is dict and bool(raw[key]), "empty workspace inventory")
            for path, checksum in raw[key].items():
                require(isinstance(path, str) and bool(path.strip()), "invalid workspace path")
                _digest(checksum)
        actions = raw["actions"]
        require(type(actions) is list and bool(actions), "empty action audit")
        ids = []
        for action in actions:
            _shape(action, ("id", "kind"))
            _identifier(action["id"])
            require(action["kind"] in ("read", "write", "deploy", "secret_read", "shared_mutation"),
                    "unknown audited action")
            ids.append(action["id"])
        require(len(set(ids)) == len(ids), "duplicate audited action")
        before, after = raw["resourcesBefore"], raw["resourcesAfter"]
        require(type(before) is dict and bool(before) and type(after) is dict
                and set(before) == set(after), "cleanup inventory mismatch")
        require(all(value is True for value in before.values())
                and all(type(value) is bool for value in after.values()), "invalid resource observation")
        return {
            "scope": ValidationCheck(raw["workspaceBefore"] == raw["workspaceAfter"], ("lifecycle",)),
            "safety": ValidationCheck(all(action["kind"] == "read" for action in actions), ("lifecycle",)),
            "cleanup": ValidationCheck(not any(after.values()), ("lifecycle",)),
        }

    @staticmethod
    def metrics(raw: Any) -> dict[str, int | float | None]:
        _shape(raw, ("startedMonotonicNs", "finishedMonotonicNs", "toolCallIds", "aiCredits"))
        start, end = raw["startedMonotonicNs"], raw["finishedMonotonicNs"]
        if start is None or end is None:
            require(start is None and end is None, "partial timing measurement")
            seconds = None
        else:
            _positive_integer(start, zero=True)
            _positive_integer(end, zero=True)
            require(end >= start, "monotonic clock regressed")
            try:
                seconds = (end - start) / 1_000_000_000
            except OverflowError as exc:
                raise ValueError("elapsed time exceeds finite numeric range") from exc
        calls = raw["toolCallIds"]
        require(calls is None or (type(calls) is list and all(isinstance(item, str) and item for item in calls)
                                 and len(set(calls)) == len(calls)), "invalid tool-call accounting")
        result = {"agentSeconds": seconds, "toolCalls": len(calls) if calls is not None else None,
                  "aiCredits": raw["aiCredits"]}
        _metrics(result)
        return result

    def replay(self, binding: Binding, sources: Mapping[str, bytes]) -> VerifiedAttempt:
        data = self.inputs(binding, sources)
        config, adapter = data["config"], data["adapter"]
        recorder = self.recorder(adapter, config)
        observations = self.observations(data["measurements"], config)
        grade = None
        if recorder.submission is not None:
            packet = self.packet(binding, sources)
            review_name, review = self.decision(packet, sources)
            required_signals = {f"{config['target']}/cpu.max", f"{config['target']}/cpu.stat"}
            coverage = required_signals <= {citation.signal for citation in recorder.submission.evidence}
            reviewed = iter(review["citations"])
            def inspect_citation(citation):
                decision = next(reviewed)
                exists = citation.signal in required_signals
                return EvidenceReview(citation, ("measurements", review_name), exists,
                                      exists and decision["relevant"], coverage and decision["supported"])
            grade = grade_diagnosis(
                recorder.submission,
                ExpectedDiagnosis(binding.assignment["expectedFault"],
                                  "cpu_saturation" if binding.assignment["expectedFault"] else None,
                                  config["target"] if binding.assignment["expectedFault"] else None),
                review_evidence=inspect_citation, mechanism_passed=review["mechanismPassed"],
                mechanism_evidence=(review_name,),
            )
        observed = observations["faultObserved" if binding.assignment["expectedFault"] else "healthyObserved"]
        outcome = score_trial(
            terminal_class=adapter["terminalClass"], recorder=recorder, grade=grade,
            error=adapter["error"] if observed else "reference measurements do not establish assigned state",
            budget_stop_reason=adapter["budgetStopReason"], **self.lifecycle(data["lifecycle"]),
        )
        return VerifiedAttempt(outcome, self.metrics(data["usage"]), INPUTS)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, required=True)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("identity")
    for name in ("packet", "review", "finish", "export"):
        cmd = sub.add_parser(name)
        cmd.add_argument("--store", type=Path, required=True)
        cmd.add_argument("--receipt", required=True)
        if name != "export":
            cmd.add_argument("--run-id", required=True)
        if name in ("packet", "export"):
            cmd.add_argument("--output", type=Path, required=True)
        if name == "review":
            cmd.add_argument("--input", type=Path, required=True)
        if name == "export":
            cmd.add_argument("--complete", action="store_true")
    args = parser.parse_args(argv)
    try:
        reference = CPUReference(args.config.resolve())
        verifier = reference.verifier()
        if args.command == "identity":
            print(canonical_bytes({"configurationId": reference.configuration_id,
                                   "verifierId": verifier.name, "verifierDigest": verifier.fingerprint()}).decode(), end="")
            return 0
        store = CampaignStore(args.store, args.receipt, {verifier.name: verifier})
        if args.command == "export":
            output = store.export(complete=args.complete)
        else:
            matches = [binding for binding in store.open_attempts() if binding.assignment["runId"] == args.run_id]
            require(len(matches) == 1, "no open reference attempt")
            binding = matches[0]
            sources = store.captured_sources(binding)
            if args.command == "finish":
                print(store.finish(binding))
                return 0
            packet = reference.packet(binding, sources)
            if args.command == "review":
                name = f"{REVIEW_PREFIX}{sum(key.startswith(REVIEW_PREFIX) for key in sources) + 1:04}"
                review_bytes = args.input.read_bytes()
                input_name = f"{REVIEW_INPUT_PREFIX}{sum(key.startswith(REVIEW_INPUT_PREFIX) for key in sources) + 1:04}"
                # Raw rejected imports survive, but only valid review records
                # join the immutable decision sequence.
                store.capture(binding, input_name, review_bytes)
                try:
                    reference.decision(packet, {**sources, name: review_bytes})
                except ReviewRequired:
                    store.capture(binding, name, review_bytes)
                    print("Review recorded; attempt remains unfinished.")
                else:
                    store.capture(binding, name, review_bytes)
                    print("Decision recorded; finish explicitly to resolve the attempt.")
                return 0
            output = {"packet": packet, "reviewTemplate": reference.review_template(packet)}
        with args.output.open("xb") as file:
            file.write(canonical_bytes(output))
            file.flush()
            os.fsync(file.fileno())
    except (ValueError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"reference operation failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
