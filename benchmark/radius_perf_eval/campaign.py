"""Offline, append-only campaign accounting, not a scheduler or a Shop grader.

The operator supplies a prepared roster and a trusted verifier registry. A
verifier replays captured inputs, calls the canonical diagnosis scorer, and
derives metrics. Artifact bytes or saved pass labels alone are not a verifier.
"""

from __future__ import annotations

import argparse
import base64
from contextlib import contextmanager
from dataclasses import dataclass
import hashlib
import inspect
import json
import math
import os
from pathlib import Path
import re
import sqlite3
from types import MappingProxyType
from typing import Any, Callable, Iterator, Mapping

from .diagnosis import DiagnosisGrade, EvidenceReview, ExpectedDiagnosis
from .sandbox import SandboxGateResult
from .submit_tool import Citation, ComponentMap, Connection, validate_submission
from .trial_outcome import TrialOutcome

ARMS = ("native", "architecture", "radius")
AGENT_CLASSES = (
    "validated_success", "diagnosis_failure", "budget_exhaustion", "no_submission",
    "invalid_structured_output", "refusal", "isolation_violation_attempt",
)
METRICS = ("agentSeconds", "toolCalls", "aiCredits")
GATES = ("diagnosis", "evidence", "scope", "safety", "cleanup")
ASSIGNMENT_FIELDS = (
    "runId", "pairId", "blockId", "arm", "model", "incident", "seed",
    "configurationId", "expectedFault", "verifierId", "verifierDigest",
)
REPORT_FIELDS = (
    "runId", "pairId", "arm", "model", "incident", "seed", "configurationId",
    "expectedFault",
)
MATCH_FIELDS = (
    "blockId", "model", "incident", "seed", "configurationId", "expectedFault",
    "verifierId", "verifierDigest",
)


class CampaignError(ValueError):
    """Invalid, incomplete, untrusted or inconsistent campaign evidence."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise CampaignError(message)


def canonical_bytes(value: Any) -> bytes:
    return (json.dumps(value, sort_keys=True, separators=(",", ":"),
                       allow_nan=False, ensure_ascii=True) + "\n").encode("ascii")


def digest(data: bytes) -> str:
    return "sha256:" + hashlib.sha256(data).hexdigest()


def _pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        require(key not in result, "duplicate JSON key")
        result[key] = value
    return result


def read_json(data: bytes) -> Any:
    try:
        return json.loads(data, object_pairs_hook=_pairs,
                          parse_constant=lambda _: require(False, "nonfinite JSON"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise CampaignError("malformed JSON") from exc


def _shape(value: Any, keys: tuple[str, ...] | set[str]) -> None:
    require(type(value) is dict and set(value) == set(keys), "unexpected record fields")


def _identifier(value: Any) -> None:
    require(isinstance(value, str) and re.fullmatch(
        r"[A-Za-z0-9][A-Za-z0-9._/@:+-]*", value
    ) is not None, "expected a public identifier, not free text")


def _digest(value: Any) -> None:
    require(isinstance(value, str) and re.fullmatch(r"sha256:[a-f0-9]{64}", value)
            is not None, "invalid SHA-256 reference")


def _metrics(value: Any) -> None:
    _shape(value, METRICS)
    for name, number in value.items():
        try:
            valid = number is None or (
                type(number) in (int, float) and math.isfinite(number)
                and number >= 0
                and (name != "toolCalls" or type(number) is int)
            )
        except OverflowError as exc:
            raise CampaignError("metric exceeds the finite numeric range") from exc
        require(valid, "metrics must be finite nonnegative numbers or null; toolCalls is an integer")


def validate_preparation(spec: Any) -> None:
    _shape(spec, ("schemaVersion", "campaign", "assignments"))
    require(spec["schemaVersion"] == "radius-campaign-v1", "unknown preparation schema")
    campaign = spec["campaign"]
    _shape(campaign, ("id", "phase", "benchmarkCommit", "analysisPlan"))
    for key in ("id", "benchmarkCommit", "analysisPlan"):
        _identifier(campaign[key])
    require(campaign["phase"] in ("smoke", "pilot", "scored"), "unknown phase")
    require(campaign["phase"] != "scored" or
            re.fullmatch(r"[a-fA-F0-9]{40}", campaign["analysisPlan"]) is not None,
            "scored campaigns require an analysis-plan commit")
    assignments = spec["assignments"]
    require(type(assignments) is list and bool(assignments), "empty assignment roster")
    ids: set[str] = set()
    pairs: dict[str, list[dict[str, Any]]] = {}
    for row in assignments:
        _shape(row, ASSIGNMENT_FIELDS)
        for key in ASSIGNMENT_FIELDS:
            if key not in ("expectedFault", "verifierDigest"):
                _identifier(row[key])
        _digest(row["verifierDigest"])
        require(type(row["expectedFault"]) is bool, "expectedFault must be boolean")
        require(row["arm"] in ARMS, "unknown arm")
        require(row["runId"] not in ids, "duplicate logical assignment")
        ids.add(row["runId"])
        peers = pairs.setdefault(row["pairId"], [])
        require(all(peer["arm"] != row["arm"] for peer in peers), "duplicate arm")
        require(not peers or all(row[key] == peers[0][key] for key in MATCH_FIELDS),
                "matched inputs disagree")
        peers.append(row)
    require(all(len(rows) == len(ARMS) for rows in pairs.values()),
            "each pair must declare all three arms")


@dataclass(frozen=True)
class Binding:
    preparation_digest: str
    assignment: Mapping[str, Any]
    attempt: int

    def __post_init__(self) -> None:
        object.__setattr__(self, "assignment", MappingProxyType(dict(self.assignment)))

    def to_dict(self) -> dict[str, Any]:
        return {"preparationDigest": self.preparation_digest,
                "assignment": dict(self.assignment), "attempt": self.attempt}


@dataclass(frozen=True)
class VerifiedAttempt:
    outcome: TrialOutcome
    metrics: Mapping[str, int | float | None]
    examined: tuple[str, ...]
    """Captured source IDs used for adapter classification and accounting."""


Replay = Callable[[Binding, Mapping[str, bytes]], VerifiedAttempt]


@dataclass(frozen=True)
class Verifier:
    """Trusted operator code, never loaded from a campaign's JSON or artifacts.

    Include the reviewer's configuration and incident-specific dependencies in
    sources. Core scorer/store sources are included automatically. The digest
    pins provenance, not scientific validity or a signature.
    Synthetic verifiers cannot produce scored reports.
    """

    name: str
    replay: Replay
    sources: tuple[Path, ...]
    synthetic: bool = False

    def __post_init__(self) -> None:
        object.__setattr__(self, "sources", tuple(self.sources))

    def fingerprint(self) -> str:
        _identifier(self.name)
        require(type(self.synthetic) is bool, "synthetic must be boolean")
        paths = tuple(path.resolve() for path in self.sources)
        require(bool(paths) and len(set(paths)) == len(paths), "empty or duplicate verifier sources")
        implementation = inspect.getsourcefile(self.replay)
        require(implementation is not None and Path(implementation).resolve() in paths,
                "verifier implementation is absent from provenance")
        return digest(canonical_bytes({
            "id": self.name, "synthetic": self.synthetic,
            "entrypoint": f"{self.replay.__module__}.{self.replay.__qualname__}",
            "sources": [digest(path.read_bytes()) for path in paths],
            "core": {name: digest(Path(__file__).with_name(name).read_bytes()) for name in (
                "campaign.py", "diagnosis.py", "submit_tool.py", "trial_outcome.py", "sandbox.py",
            )},
        }))


def _references(refs: Any, sources: Mapping[str, bytes]) -> None:
    require(type(refs) in (list, tuple) and bool(refs), "missing examined sources")
    require(all(isinstance(ref, str) and ref in sources and bool(sources[ref])
                for ref in refs), "examined source missing or empty")


def _validate_outcome(outcome: dict[str, Any], binding: Binding,
                      sources: Mapping[str, bytes]) -> None:
    _shape(outcome, TrialOutcome(False, False, "harness_failure").to_json_dict().keys())
    terminal, agent = outcome["terminalClass"], outcome["agentTerminalClass"]
    require(terminal in (*AGENT_CLASSES, "harness_failure"), "unknown diagnosis outcome")
    require(agent is None or agent in AGENT_CLASSES, "unknown agent outcome")
    for key, expected in (
        ("scored", terminal == "validated_success"),
        ("valid", terminal != "harness_failure"),
        ("scoredAsFailure", terminal != "validated_success"),
        ("harnessFailure", terminal == "harness_failure"),
    ):
        require(type(outcome[key]) is bool and outcome[key] == expected,
                "inconsistent canonical outcome flags")
    require(terminal == "harness_failure" or agent == terminal, "agent outcome mismatch")
    require(type(outcome["reasons"]) is list and all(
        isinstance(reason, str) and reason.strip() for reason in outcome["reasons"]
    ), "invalid outcome reasons")
    require(terminal == "validated_success" or bool(outcome["reasons"]), "missing failure reason")
    require(type(outcome["rejectedSubmissionAttempts"]) is int
            and outcome["rejectedSubmissionAttempts"] >= 0, "invalid rejected-call count")
    require(outcome["staticScreen"] in ("on", "off")
            and type(outcome["shellEnabled"]) is bool, "invalid confinement settings")
    require(outcome["budgetStopReason"] is None or
            isinstance(outcome["budgetStopReason"], str), "invalid budget reason")
    sandbox = outcome["sandboxGate"]
    if sandbox is not None:
        _shape(sandbox, SandboxGateResult(False, None, 0, 0).to_json_dict().keys())
        require(type(sandbox["passed"]) is bool, "invalid sandbox verdict")
    require(terminal == "harness_failure" or (
        (sandbox is None or sandbox["passed"])
        and (not outcome["shellEnabled"] or outcome["staticScreen"] == "on" or sandbox is not None)
    ), "confinement failure must be a harness failure")
    gates, evidence = outcome["validators"], outcome["validatorEvidence"]
    require(type(gates) is dict and set(gates) <= set(GATES)
            and all(value in ("pass", "fail") for value in gates.values()),
            "invalid validator results")
    require(type(evidence) is dict and set(evidence) == set(gates), "validator evidence coverage")
    for key in ("scope", "safety", "cleanup"):
        if key in gates:
            _references(evidence[key], sources)
    if terminal != "harness_failure":
        require(all(key in gates for key in ("scope", "safety", "cleanup"))
                and gates["cleanup"] == "pass", "agent result lacks lifecycle evidence")
        require(terminal == "isolation_violation_attempt" or
                gates["scope"] == gates["safety"] == "pass", "prohibited action was not classified")
    submission = outcome["submission"]
    if submission is not None:
        require(type(submission) is dict, "submission must be an object or null")
        # Re-parse the canonical answer. Alias correctness is replayed by the
        # trusted fixture-aware verifier, not inferred from a saved alias.
        names = [submission.get("component")]
        connection = submission.get("connection")
        if isinstance(connection, dict):
            names.extend(connection.values())
        component_map = ComponentMap({name: name for name in names if isinstance(name, str)}
                                     or {"unused": "unused"})
        parsed = validate_submission(submission, component_map=component_map)
        _shape(submission, parsed.to_json_dict().keys())
        canonical = parsed.to_json_dict()
        for key in canonical:
            if key not in ("componentAsSubmitted", "connectionAsSubmitted"):
                require(submission[key] == canonical[key], "noncanonical submission")
    else:
        parsed = None
    if "diagnosis" in gates or "evidence" in gates:
        require(parsed is not None and "diagnosis" in gates and "evidence" in gates,
                "incomplete diagnosis grade")
        expected = evidence["diagnosis"]
        _shape(expected, ("expectedFault", "causalCategory", "component", "connection",
                          "mechanismPassed", "mechanismExamined"))
        require(type(expected["mechanismPassed"]) is bool, "missing explicit mechanism review")
        _references(expected["mechanismExamined"], sources)
        edge = expected["connection"]
        if edge is not None:
            _shape(edge, ("source", "target"))
        target = ExpectedDiagnosis(
            expected["expectedFault"], expected["causalCategory"], expected["component"],
            Connection(**edge) if edge is not None else None,
        )
        require(target.fault_present == binding.assignment["expectedFault"],
                "grade expectedFault differs from assignment")
        reviews = evidence["evidence"]
        require(type(reviews) is list and bool(reviews), "empty citation reviews")
        results = []
        for review in reviews:
            _shape(review, ("citation", "examined", "exists", "relevant", "supported"))
            _shape(review["citation"], ("signal", "observation"))
            _references(review["examined"], sources)
            results.append(EvidenceReview(Citation(**review["citation"]),
                           tuple(review["examined"]), review["exists"],
                           review["relevant"], review["supported"]))
        grade = DiagnosisGrade(parsed, target, tuple(results), expected["mechanismPassed"],
                               tuple(expected["mechanismExamined"]))
        require(gates["diagnosis"] == ("pass" if grade.diagnosis_passed else "fail")
                and gates["evidence"] == ("pass" if grade.evidence_passed else "fail"),
                "saved grade contradicts canonical diagnosis gate")
    if agent in ("validated_success", "diagnosis_failure"):
        require(parsed is not None and "diagnosis" in gates and "evidence" in gates,
                "agent diagnosis outcome lacks grade")
        require((agent == "validated_success") ==
                (gates["diagnosis"] == gates["evidence"] == "pass"),
                "agent diagnosis contradicts validators")
    if agent in ("no_submission", "invalid_structured_output", "refusal", "budget_exhaustion"):
        require(parsed is None, "no-answer class contains a submission")
    if agent in ("no_submission", "invalid_structured_output"):
        require((agent == "invalid_structured_output") == (outcome["rejectedSubmissionAttempts"] > 0),
                "no-answer class contradicts rejected calls")


def _verify(binding: Binding, sources: Mapping[str, bytes],
            registry: Mapping[str, Verifier], phase: str) -> dict[str, Any]:
    row = binding.assignment
    verifier = registry.get(row["verifierId"])
    require(verifier is not None and verifier.name == row["verifierId"],
            "no trusted verifier registered for assignment")
    require(verifier.fingerprint() == row["verifierDigest"], "verifier provenance mismatch")
    require(phase != "scored" or not verifier.synthetic, "synthetic evidence is not scored evidence")
    require(bool(sources), "empty captured evidence")
    for name, data in sources.items():
        _identifier(name)
        require(type(data) is bytes and bool(data), "empty or invalid captured artifact")
    verified = verifier.replay(binding, MappingProxyType(dict(sources)))
    require(verifier.fingerprint() == row["verifierDigest"], "verifier changed during replay")
    require(isinstance(verified, VerifiedAttempt) and isinstance(verified.outcome, TrialOutcome),
            "verifier must return a canonical TrialOutcome")
    _references(verified.examined, sources)
    metrics = dict(verified.metrics)
    _metrics(metrics)
    outcome = verified.outcome.to_json_dict()
    _validate_outcome(outcome, binding, sources)
    return {"outcome": outcome, "metrics": metrics, "examined": list(verified.examined),
            "verifier": {"id": verifier.name, "digest": row["verifierDigest"],
                         "synthetic": verifier.synthetic}}


class CampaignStore:
    """Transactional append-only journal. Opening requires the preparation receipt.

    SQLite commits a complete event or nothing. SQL triggers reject updates and
    deletion. Hash chaining detects malformed/reordered/foreign records, not a
    hostile database owner rewriting the whole history. Retain external receipts.
    """

    def __init__(self, path: Path, preparation_digest: str,
                 verifiers: Mapping[str, Verifier] | None = None):
        self.path = Path(path)
        _digest(preparation_digest)
        self.preparation_digest = preparation_digest
        self.verifiers = MappingProxyType(dict(verifiers or {}))

    @classmethod
    def prepare(cls, path: Path, spec: dict[str, Any], *,
                verifiers: Mapping[str, Verifier] | None = None) -> CampaignStore:
        validate_preparation(spec)
        path = Path(path)
        # Exclusive creation prevents prepare from replacing an existing campaign.
        descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        os.close(descriptor)
        event = canonical_bytes({"sequence": 1, "previous": None,
                                 "kind": "prepared", "data": spec})
        with sqlite3.connect(path) as db:
            db.execute("PRAGMA synchronous=FULL")
            db.executescript("""
                CREATE TABLE journal (sequence INTEGER PRIMARY KEY, payload BLOB NOT NULL);
                CREATE TRIGGER no_update BEFORE UPDATE ON journal BEGIN
                    SELECT RAISE(ABORT, 'append-only journal'); END;
                CREATE TRIGGER no_delete BEFORE DELETE ON journal BEGIN
                    SELECT RAISE(ABORT, 'append-only journal'); END;
            """)
            db.execute("INSERT INTO journal VALUES (1, ?)", (event,))
        return cls(path, digest(event), verifiers)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        require(self.path.is_file() and not self.path.is_symlink(), "campaign database missing")
        db = sqlite3.connect(f"{self.path.resolve().as_uri()}?mode=rw", uri=True, timeout=30)
        try:
            db.execute("PRAGMA synchronous=FULL")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def _read(self, db: sqlite3.Connection) -> tuple[dict[str, Any], dict[str, list[dict[str, Any]]], list[bytes]]:
        rows = db.execute("SELECT sequence, payload FROM journal ORDER BY sequence").fetchall()
        require(bool(rows), "missing preparation")
        previous = None
        spec: dict[str, Any] = {}
        attempts: dict[str, list[dict[str, Any]]] = {}
        assignments: dict[str, dict[str, Any]] = {}
        raw = []
        for ordinal, (sequence, payload) in enumerate(rows, 1):
            require(type(payload) is bytes, "journal payload must contain canonical bytes")
            event = read_json(payload)
            _shape(event, ("sequence", "previous", "kind", "data"))
            require(sequence == ordinal and type(event["sequence"]) is int
                    and event["sequence"] == ordinal and event["previous"] == previous,
                    "journal sequence or chain mismatch")
            require(canonical_bytes(event) == payload, "noncanonical journal bytes")
            event_digest = digest(payload)
            if ordinal == 1:
                require(event["kind"] == "prepared" and event_digest == self.preparation_digest,
                        "preparation receipt mismatch")
                spec = event["data"]
                validate_preparation(spec)
                assignments = {row["runId"]: row for row in spec["assignments"]}
                attempts = {run_id: [] for run_id in assignments}
            else:
                kind, data = event["kind"], event["data"]
                require(kind in ("started", "captured", "finished"), "unknown journal event")
                _shape(data, {"started": ("binding",), "captured": ("binding", "name", "source"),
                              "finished": ("binding", "sources", "verified")}[kind])
                binding = data["binding"]
                _shape(binding, ("preparationDigest", "assignment", "attempt"))
                assignment = binding["assignment"]
                require(type(assignment) is dict and assignment.get("runId") in assignments,
                        "foreign assignment")
                run_id = assignment["runId"]
                require(canonical_bytes(assignment) == canonical_bytes(assignments[run_id]) and
                        binding["preparationDigest"] == self.preparation_digest,
                        "assignment binding mismatch")
                number = binding["attempt"]
                require(type(number) is int and number in (1, 2), "invalid attempt number")
                history = attempts[run_id]
                if kind == "started":
                    self._can_start(run_id, attempts, assignments)
                    require(number == len(history) + 1, "duplicate or reordered attempt")
                    history.append({"binding": binding, "sources": {}, "finished": None})
                else:
                    require(len(history) == number and history[-1]["finished"] is None,
                            "finish has no matching open attempt")
                    if kind == "captured":
                        name = data["name"]
                        _identifier(name)
                        require(name not in history[-1]["sources"], "duplicate source capture")
                        self._decode_sources({name: data["source"]})
                        history[-1]["sources"][name] = data["source"]
                        raw.append(payload)
                        previous = event_digest
                        continue
                    require(data["sources"] == history[-1]["sources"], "finish sources differ from captures")
                    sources = self._decode_sources(data["sources"])
                    replayed = _verify(Binding(self.preparation_digest,
                                       MappingProxyType(assignment), number),
                                       sources, self.verifiers, spec["campaign"]["phase"])
                    require(canonical_bytes(data["verified"]) == canonical_bytes(replayed),
                            "record differs from verifier replay")
                    history[-1]["finished"] = {**replayed, "recordDigest": event_digest}
            raw.append(payload)
            previous = event_digest
        return spec, attempts, raw

    @staticmethod
    def _decode_sources(encoded: Any) -> dict[str, bytes]:
        require(type(encoded) is dict and bool(encoded), "missing captured sources")
        result = {}
        for name, source in encoded.items():
            _identifier(name)
            _shape(source, ("digest", "bytes"))
            _digest(source["digest"])
            require(isinstance(source["bytes"], str), "invalid captured bytes")
            try:
                data = base64.b64decode(source["bytes"], validate=True)
            except ValueError as exc:
                raise CampaignError("malformed captured bytes") from exc
            require(digest(data) == source["digest"], "captured source checksum mismatch")
            require(bool(data), "empty captured source")
            result[name] = data
        return result

    @staticmethod
    def _can_start(run_id: str, attempts: dict[str, list[dict[str, Any]]],
                   assignments: dict[str, dict[str, Any]]) -> None:
        require(run_id in attempts, "unknown logical assignment")
        history = attempts[run_id]
        require(len(history) < 2, "retry limit reached")
        if history:
            require(history[-1]["finished"] is not None and
                    history[-1]["finished"]["outcome"]["terminalClass"] == "harness_failure",
                    "only a finished harness failure may retry")
            block = assignments[run_id]["blockId"]
            require(all(attempts[key] and attempts[key][0]["finished"] is not None
                        for key, assignment in assignments.items() if assignment["blockId"] == block),
                    "retry must wait until the end of its block")

    @staticmethod
    def _append(db: sqlite3.Connection, raw: list[bytes], kind: str, data: dict[str, Any]) -> str:
        event = canonical_bytes({"sequence": len(raw) + 1, "previous": digest(raw[-1]),
                                 "kind": kind, "data": data})
        db.execute("INSERT INTO journal VALUES (?, ?)", (len(raw) + 1, event))
        return digest(event)

    def start(self, run_id: str) -> Binding:
        with self._transaction() as db:
            spec, attempts, raw = self._read(db)
            assignments = {row["runId"]: row for row in spec["assignments"]}
            self._can_start(run_id, attempts, assignments)
            binding = Binding(self.preparation_digest, MappingProxyType(assignments[run_id]),
                              len(attempts[run_id]) + 1)
            self._append(db, raw, "started", {"binding": binding.to_dict()})
            return binding

    @staticmethod
    def _open(binding: Binding, attempts: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
        run_id = binding.assignment["runId"]
        require(run_id in attempts and bool(attempts[run_id]), "attempt was not started")
        current = attempts[run_id][-1]
        require(canonical_bytes(current["binding"]) == canonical_bytes(binding.to_dict())
                and current["finished"] is None,
                "duplicate or mismatched finish")
        return current

    def capture(self, binding: Binding, name: str, data: bytes) -> str:
        """Persist raw evidence before verification, including rejected evidence."""
        _identifier(name)
        require(type(data) is bytes and bool(data), "empty or invalid captured artifact")
        with self._transaction() as db:
            _, attempts, raw = self._read(db)
            current = self._open(binding, attempts)
            require(name not in current["sources"], "duplicate source capture")
            return self._append(db, raw, "captured", {
                "binding": binding.to_dict(), "name": name,
                "source": {"digest": digest(data), "bytes": base64.b64encode(data).decode("ascii")},
            })

    def finish(self, binding: Binding) -> str:
        with self._transaction() as db:
            spec, attempts, raw = self._read(db)
            current = self._open(binding, attempts)
            sources = self._decode_sources(current["sources"])
            verified = _verify(binding, sources, self.verifiers, spec["campaign"]["phase"])
            return self._append(db, raw, "finished", {
                "binding": binding.to_dict(), "sources": current["sources"], "verified": verified,
            })

    def record_bytes(self, record_digest: str) -> bytes:
        """Read the verified canonical terminal bytes named by a report digest."""
        _digest(record_digest)
        with self._transaction() as db:
            _, _, raw = self._read(db)
        matches = [data for data in raw if digest(data) == record_digest
                   and read_json(data)["kind"] == "finished"]
        require(len(matches) == 1, "terminal record digest not found")
        return matches[0]

    def captured_sources(self, binding: Binding) -> dict[str, bytes]:
        """Read durable raw inputs for preparing a review packet or resuming work."""
        with self._transaction() as db:
            _, attempts, _ = self._read(db)
            current = self._open(binding, attempts)
            return self._decode_sources(current["sources"])

    def open_attempts(self) -> list[Binding]:
        """Recover unfinished handles without creating or classifying new attempts."""
        with self._transaction() as db:
            _, attempts, _ = self._read(db)
            return [Binding(self.preparation_digest,
                            MappingProxyType(history[-1]["binding"]["assignment"]),
                            history[-1]["binding"]["attempt"])
                    for history in attempts.values() if history and history[-1]["finished"] is None]

    def export(self, *, complete: bool = False) -> dict[str, Any]:
        with self._transaction() as db:
            spec, attempts, _ = self._read(db)
        runs = []
        for assignment in spec["assignments"]:
            history = attempts[assignment["runId"]]
            finished = [attempt["finished"] for attempt in history if attempt["finished"] is not None]
            failures = sum(item["outcome"]["terminalClass"] == "harness_failure" for item in finished)
            final = finished[-1] if finished and len(finished) == len(history) else None
            if not history:
                status = "pending"
            elif final is None or (failures == 1 and len(history) == 1):
                status = "running"
            elif failures == 2:
                status = "excluded"
            else:
                status = final["outcome"]["terminalClass"]
            terminal = status in (*AGENT_CLASSES, "excluded")
            outcome = final["outcome"] if terminal else None
            runs.append({
                **{key: assignment[key] for key in REPORT_FIELDS},
                "status": status, "attempts": len(history), "harnessFailures": failures,
                "recordDigest": final["recordDigest"] if terminal else None,
                "reason": "Harness failed twice; inspect canonical records." if status == "excluded" else "",
                "validators": dict(outcome["validators"]) if outcome else {},
                "reportedFault": (outcome["submission"]["faultPresent"]
                                  if outcome and outcome["submission"] else None),
                **(final["metrics"] if terminal else dict.fromkeys(METRICS)),
            })
        require(not complete or all(row["status"] in (*AGENT_CLASSES, "excluded") for row in runs),
                "complete campaign contains unfinished assignments")
        return {"schemaVersion": "radius-comparison-v1",
                "campaign": {**spec["campaign"], "status": "complete" if complete else "running"},
                "runs": runs}

    def accounting(self) -> dict[str, Any]:
        """Retry-inclusive captured totals, separate from report final-attempt metrics."""
        with self._transaction() as db:
            spec, attempts, _ = self._read(db)
        finished = [a["finished"] for history in attempts.values()
                    for a in history if a["finished"] is not None]
        total_attempts = sum(len(history) for history in attempts.values())
        excluded = sum(len(history) == 2 and all(
            a["finished"] is not None and a["finished"]["outcome"]["terminalClass"] == "harness_failure"
            for a in history) for history in attempts.values())
        scored = sum(item["outcome"]["terminalClass"] in AGENT_CLASSES for item in finished)
        pending = sum(not history for history in attempts.values())
        running = len(attempts) - scored - excluded - pending
        totals = {
            key: sum(item["metrics"][key] for item in finished if item["metrics"][key] is not None)
            if any(item["metrics"][key] is not None for item in finished) else None
            for key in METRICS
        }
        _metrics(totals)
        return {
            "preparationDigest": self.preparation_digest,
            "planned": len(spec["assignments"]), "pending": pending, "running": running,
            "scored": scored, "excluded": excluded, "completed": scored + excluded,
            "attempts": total_attempts, "finishedAttempts": len(finished),
            "harnessFailures": sum(item["outcome"]["terminalClass"] == "harness_failure" for item in finished),
            "retryInclusiveMetrics": {
                key: {
                    "availableSum": totals[key],
                    "availableAttempts": sum(item["metrics"][key] is not None for item in finished),
                    "missingAttempts": total_attempts - sum(item["metrics"][key] is not None for item in finished),
                } for key in METRICS
            },
        }


def main(argv: list[str] | None = None, *, verifiers: Mapping[str, Verifier] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    prepare = commands.add_parser("prepare")
    prepare.add_argument("--spec", type=Path, required=True)
    prepare.add_argument("--store", type=Path, required=True)
    for name in ("export", "accounting"):
        command = commands.add_parser(name)
        command.add_argument("--store", type=Path, required=True)
        command.add_argument("--receipt", required=True)
        command.add_argument("--output", type=Path, required=True)
        if name == "export":
            command.add_argument("--complete", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "prepare":
            store = CampaignStore.prepare(args.store, read_json(args.spec.read_bytes()), verifiers=verifiers)
            print(store.preparation_digest)
        else:
            store = CampaignStore(args.store, args.receipt, verifiers)
            output = store.export(complete=args.complete) if args.command == "export" else store.accounting()
            with args.output.open("xb") as file:
                file.write(canonical_bytes(output))
                file.flush()
                os.fsync(file.fileno())
    except (CampaignError, OSError, sqlite3.Error) as exc:
        parser.exit(1, f"campaign operation failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
