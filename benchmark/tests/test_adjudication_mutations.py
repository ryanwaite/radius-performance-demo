"""Mutation controls for the approved adjudication and reference boundaries."""

import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest

from radius_perf_eval import adjudication


SOURCE = Path(adjudication.__file__).read_text()


def mutations():
    lines = SOURCE.splitlines(keepends=True)
    for node in ast.walk(ast.parse(SOURCE)):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "require":
            condition = node.args[0]
            start = sum(map(len, lines[:condition.lineno - 1])) + condition.col_offset
            end = sum(map(len, lines[:condition.end_lineno - 1])) + condition.end_col_offset
            yield f"guard-line-{condition.lineno}", SOURCE[:start] + "True" + SOURCE[end:]
    for name, before, after in [
        ("missing-human-review", "if not decisions:", "if False:"),
        ("mechanism-wiring", 'mechanism_passed=review["mechanismPassed"]', "mechanism_passed=True"),
        ("citation-support", 'coverage and decision["supported"]', "coverage"),
        ("citation-relevance", 'exists and decision["relevant"]', "exists"),
        ("citation-existence", 'exists = citation.signal in required_signals', "exists = True"),
        ("evidence-coverage", 'coverage = required_signals <=', 'coverage = True or required_signals <='),
        ("human-capture-before-finish", "store.capture(binding, name, review_bytes)", "pass"),
        ("retain-rejected-review", "store.capture(binding, input_name, review_bytes)", "pass"),
        ("fault-measurement-wiring", 'if observed else "reference measurements do not establish assigned state"',
         'if True else "reference measurements do not establish assigned state"'),
        ("scope-wiring", 'raw["workspaceBefore"] == raw["workspaceAfter"]', "True"),
        ("safety-wiring", 'all(action["kind"] == "read" for action in actions)', "True"),
        ("cleanup-wiring", "not any(after.values())", "True"),
        ("blinded-alias", 'answer.pop("componentAsSubmitted")', "pass"),
        ("no-scored-reference", 'synthetic=True', "synthetic=False"),
        ("quota-fault", "quota_comparison < 0 and", "True and"),
        ("quota-healthy", "quota_comparison == 0 and", "True and"),
        ("throttled-period-fault", 'deltas["nr_throttled"] > 0 and', "True and"),
        ("throttled-time-fault", 'deltas["throttled_usec"] > 0', "True"),
        ("throttled-period-healthy", 'deltas["nr_throttled"] == 0 and', "True and"),
        ("throttled-time-healthy", 'deltas["throttled_usec"] == 0', "True"),
    ]:
        assert before in SOURCE, name
        yield name, SOURCE.replace(before, after)


MUTATIONS = list(mutations())


@pytest.mark.parametrize("name,source", MUTATIONS, ids=[m[0] for m in MUTATIONS])
def test_adjudication_mutations(name, source):
    compile(source, adjudication.__file__, "exec")
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; import radius_perf_eval.adjudication as m; "
         "exec(compile(sys.stdin.read(), m.__file__, 'exec'), m.__dict__); "
         "import pytest; sys.exit(pytest.main(['-q', '--tb=short', 'tests/test_adjudication.py']))"],
        input=source, text=True, capture_output=True, timeout=60,
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "DOCKER_HOST": "unix:///nonexistent/docker.sock"},
    )
    print(f"\nMUTATION {name}\n{result.stdout}{result.stderr}", flush=True)
    assert result.returncode == 1 and "FAILED " in result.stdout, (
        f"mutation {name} survived or failed collection"
    )
