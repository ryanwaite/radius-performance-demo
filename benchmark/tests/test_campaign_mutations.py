"""Remove every campaign rejection guard and selected persistence/replay wiring."""

import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest

from radius_perf_eval import campaign


SOURCE = Path(campaign.__file__).read_text()
TREE = ast.parse(SOURCE)


def mutations():
    lines = SOURCE.splitlines(keepends=True)
    for node in ast.walk(TREE):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "require":
            condition = node.args[0]
            start = sum(map(len, lines[:condition.lineno - 1])) + condition.col_offset
            end = sum(map(len, lines[:condition.end_lineno - 1])) + condition.end_col_offset
            yield f"guard-line-{condition.lineno}", SOURCE[:start] + "True" + SOURCE[end:]
    for name, before, after in [
        ("replay-on-read", 'require(canonical_bytes(data["verified"]) == canonical_bytes(replayed),', 'require(True,'),
        ("canonical-outcome-wiring", "_validate_outcome(outcome, binding, sources)", "pass"),
        ("metrics-wiring", "_metrics(metrics)", "pass"),
        ("totals-wiring", "_metrics(totals)", "pass"),
        ("capture-before-finish", 'require(data["sources"] == history[-1]["sources"],', 'require(True,'),
        ("durable-commit", "db.commit()", "db.rollback()"),
        ("persist-event", 'db.execute("INSERT INTO journal VALUES (?, ?)", (len(raw) + 1, event))', "pass"),
        ("private-store", "os.O_WRONLY, 0o600", "os.O_WRONLY, 0o644"),
        ("immutable-binding", 'MappingProxyType(dict(self.assignment))', 'self.assignment'),
        ("immutable-verifier-sources", 'tuple(self.sources)', 'self.sources'),
        ("final-metrics", '**(final["metrics"] if terminal else dict.fromkeys(METRICS))', '**dict.fromkeys(METRICS)'),
        ("agent-denominator", 'item["outcome"]["terminalClass"] in AGENT_CLASSES for item in finished',
         'True for item in finished'),
        ("retry-totals", 'sum(item["metrics"][key] for item in finished',
         'sum(item["metrics"][key] for item in finished[-1:]'),
        ("core-provenance", '"core": {name: digest(Path(__file__).with_name(name).read_bytes()) for name in (',
         '"core": {name: "unverified" for name in ('),
        ("entrypoint-provenance", 'f"{self.replay.__module__}.{self.replay.__qualname__}"', '"unverified"'),
        ("assignment-type-binding", "canonical_bytes(assignment) == canonical_bytes(assignments[run_id])",
         "assignment == assignments[run_id]"),
        ("saved-record-types", 'canonical_bytes(data["verified"]) == canonical_bytes(replayed)',
         'data["verified"] == replayed'),
    ]:
        assert before in SOURCE, name
        yield name, SOURCE.replace(before, after)


MUTATIONS = list(mutations())


@pytest.mark.parametrize("name,source", MUTATIONS, ids=[row[0] for row in MUTATIONS])
def test_campaign_mutations(name, source):
    compile(source, campaign.__file__, "exec")
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; import radius_perf_eval.campaign as m; "
         "exec(compile(sys.stdin.read(), m.__file__, 'exec'), m.__dict__); "
         "import pytest; sys.exit(pytest.main(['-q', '--tb=short', 'tests/test_campaign.py']))"],
        input=source, text=True, capture_output=True, timeout=60,
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "DOCKER_HOST": "unix:///nonexistent/docker.sock"},
    )
    print(f"\nMUTATION {name}\n{result.stdout}{result.stderr}", flush=True)
    assert result.returncode == 1 and "FAILED " in result.stdout, (
        f"mutation {name} survived or broke collection rather than a control"
    )
