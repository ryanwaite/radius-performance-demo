"""Remove contract guards and wiring; the ordinary controls must reject each edit."""

import os
from pathlib import Path
import subprocess
import sys

import pytest


# Exact anchors also make a refactor update its mutation controls, rather than
# silently exercising an old, unreachable condition.
MUTATIONS = [
    ("submit_tool", "alias-conflict", 'if key in aliases and aliases[key] != value:', 'if False:'),
    ("submit_tool", "canonical-conflict", 'if key in aliases and aliases[key] != canonical:', 'if False:'),
    ("submit_tool", "immutable-map", 'MappingProxyType(aliases)', 'aliases'),
    ("submit_tool", "connection-presence", 'if connection_raw is not None:', 'if False:'),
    ("submit_tool", "connection-shape",
     'not isinstance(connection_raw, Mapping) or set(connection_raw) != {',
     'False and set(connection_raw) != {'),
    ("submit_tool", "connection-source", 'component_map.canonical(connection_as_submitted.source)',
     'connection_as_submitted.source'),
    ("submit_tool", "connection-target", 'component_map.canonical(connection_as_submitted.target)',
     'connection_as_submitted.target'),
    ("submit_tool", "connection-output", 'connection=connection,', 'connection=None,'),
    ("submit_tool", "connection-original", 'connection_as_submitted=connection_as_submitted,',
     'connection_as_submitted=None,'),
    ("submit_tool", "healthy-connection",
     'if connection_raw is not None:\n            raise SubmissionError("connection must be omitted',
     'if False:\n            raise SubmissionError("connection must be omitted'),
    ("submit_tool", "malformed-json-history",
     'recorder.attempts.append(\n                    {"payload": arguments, "accepted": False, "error": "invalid JSON"}\n                )',
     'pass'),
    ("submit_tool", "payload-snapshot", '"payload": deepcopy(payload)', '"payload": payload'),
    ("submit_tool", "export-snapshot", '"attemptLog": deepcopy(self.attempts)', '"attemptLog": self.attempts'),
    ("diagnosis", "fault-type", 'if type(self.fault_present) is not bool:', 'if False:'),
    ("diagnosis", "expected-category", 'if self.causal_category not in CAUSAL_CATEGORIES:', 'if False:'),
    ("diagnosis", "target-kind", 'if (self.component is None) == (self.connection is None):', 'if False:'),
    ("diagnosis", "target-names", 'if any(not isinstance(name, str) or not name.strip() for name in names):', 'if False:'),
    ("diagnosis", "healthy-cause", 'elif any(value is not None for value in (', 'elif False and any(value is not None for value in ('),
    ("diagnosis", "examined-presence", 'if not self.examined or any(', 'if any('),
    ("diagnosis", "immutable-references", 'if not isinstance(self.examined, tuple):', 'if False:'),
    ("diagnosis", "immutable-reviews", 'if not isinstance(self.reviews, tuple):', 'if False:'),
    ("diagnosis", "examined-content", 'not isinstance(ref, str) or not ref.strip()', 'False'),
    ("diagnosis", "explicit-verdict", 'if any(type(value) is not bool for value in (', 'if False and any(type(value) is not bool for value in ('),
    ("diagnosis", "signal-exists", 'return self.exists and self.relevant and self.supported',
     'return self.relevant and self.supported'),
    ("diagnosis", "signal-causal", 'return self.exists and self.relevant and self.supported',
     'return self.exists and self.supported'),
    ("diagnosis", "observation-supported", 'return self.exists and self.relevant and self.supported',
     'return self.exists and self.relevant'),
    ("diagnosis", "nonempty-evidence", 'bool(self.reviews) and all', 'all'),
    ("diagnosis", "every-citation", 'all(review.passed for review in self.reviews)',
     'any(review.passed for review in self.reviews)'),
    ("diagnosis", "fault-claim", 'submission.fault_present == expected.fault_present', 'True'),
    ("diagnosis", "mechanism", 'submission.causal_category == expected.causal_category', 'True'),
    ("diagnosis", "directed-edge", 'submission.connection == expected.connection', 'True'),
    ("diagnosis", "endpoint", 'submission.component in (', 'True or submission.component in ('),
    ("diagnosis", "component", 'submission.component == expected.component', 'True'),
    ("diagnosis", "review-wiring", 'review = review_evidence(citation)',
     'review = EvidenceReview(citation, ("fabricated",), True, True, True)'),
    ("diagnosis", "citation-binding", 'if review.citation != citation:', 'if False:'),
    ("diagnosis", "review-coverage",
     'if tuple(review.citation for review in self.reviews) != self.submission.evidence:', 'if False:'),
    ("trial_outcome", "verdict-type", 'if type(self.passed) is not bool:', 'if False:'),
    ("trial_outcome", "check-liveness", 'if not self.examined or any(', 'if any('),
    ("trial_outcome", "immutable-check", 'if not isinstance(self.examined, tuple):', 'if False:'),
    ("trial_outcome", "check-content", 'not isinstance(ref, str) or not ref.strip()', 'False'),
    ("trial_outcome", "terminal-vocabulary",
     'if terminal_class not in TERMINAL_CLASSES + ("error", "copilot_sdk_or_adapter_failure"):',
     'if False:'),
    ("trial_outcome", "diagnosis-only", 'if terminal_class == "remediation_failure":', 'if False:'),
    ("trial_outcome", "missing-check", 'if check is None:', 'if False:'),
    ("trial_outcome", "cleanup", 'if cleanup is not None and not cleanup.passed:', 'if False:'),
    ("trial_outcome", "isolation", 'terminal_class == "isolation_violation_attempt" or any(', 'False or any('),
    ("trial_outcome", "prohibited-actions",
     'check is not None and not check.passed for check in (scope, safety)',
     'False for check in (scope, safety)'),
    ("trial_outcome", "missing-grade", 'if grade is None:', 'if False:'),
    ("trial_outcome", "grade-binding", 'elif grade.submission != submission:', 'elif False:'),
    ("trial_outcome", "causal-gate", 'if grade.diagnosis_passed and grade.evidence_passed',
     'if grade.evidence_passed'),
    ("trial_outcome", "evidence-gate", 'if grade.diagnosis_passed and grade.evidence_passed',
     'if grade.diagnosis_passed'),
    ("trial_outcome", "harness-overrides", '"harness_failure" if harness_reasons else agent_class', 'agent_class'),
]


@pytest.mark.parametrize("module,name,before,after", MUTATIONS, ids=[m[1] for m in MUTATIONS])
def test_contract_mutation(module, name, before, after):
    root = Path(__file__).resolve().parents[2]
    source_path = root / "benchmark" / "radius_perf_eval" / f"{module}.py"
    source = source_path.read_text()
    assert before in source, f"mutation anchor disappeared: {name}"
    # The connection-presence mutation targets the first (fault) branch only.
    mutated = source.replace(before, after, 1 if name == "connection-presence" else -1)
    compile(mutated, str(source_path), "exec")
    command = (
        "import importlib, sys; "
        "module = importlib.import_module(sys.argv[1]); "
        "exec(compile(sys.stdin.read(), module.__file__, 'exec'), module.__dict__); "
        "import pytest; sys.exit(pytest.main(sys.argv[2:]))"
    )
    result = subprocess.run(
        [sys.executable, "-c", command, f"radius_perf_eval.{module}",
         "-q", "--tb=short",
         "tests/test_submit_tool.py", "tests/test_diagnosis.py",
         "tests/test_trial_outcome.py"],
        input=mutated, text=True, capture_output=True, cwd=root / "benchmark", timeout=30,
        env={**os.environ, "DOCKER_HOST": "unix:///nonexistent/docker.sock"},
    )
    print(f"\nMUTATION {module}:{name}\n{result.stdout}{result.stderr}", flush=True)
    assert result.returncode == 1 and "FAILED " in result.stdout, (
        f"mutation {name} survived or broke collection instead of a control"
    )
