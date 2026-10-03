"""Delete import guards and weaken capture wiring in separate offline processes."""

import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest

from radius_perf_eval import radius_overlay


SOURCE = Path(radius_overlay.__file__).read_text()


def mutations():
    lines = SOURCE.splitlines(keepends=True)
    for node in ast.walk(ast.parse(SOURCE)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "require"):
            continue
        condition = node.args[0]
        start = sum(map(len, lines[:condition.lineno - 1])) + condition.col_offset
        end = sum(map(len, lines[:condition.end_lineno - 1])) + condition.end_col_offset
        yield f"guard-{node.lineno}", SOURCE[:start] + "True" + SOURCE[end:]
    for name, before, after in [
        ("common-bytes", "radius = native | overlay", "radius = application"),
        ("missing-overlay", "radius = native | overlay", "radius = native"),
        ("source-normalization", 'data.replace(b"\\r\\n", b"\\n")', "data"),
        ("wrong-origin-comparison", 'origin["appBicepHash"] == origin_model_digest(\n'
         '            overlay[".radius/app.bicep"][0])', 'origin["appBicepHash"] == MODEL_DIGEST'),
        ("skip-origin-normalization", 're.sub(r"[ \\t]+$", "", text, flags=re.MULTILINE).rstrip()',
         "text"),
        ("fake-cli-version", '"installedRadiusCliVersion": None', '"installedRadiusCliVersion": "0.61"'),
        ("false-seal", '"eligibleForTrials": False', '"eligibleForTrials": True'),
        ("skip-materialization", "source.write_workspace(files, manifest, workspace)",
         '{"tree": BASE_TREE, "commit": BASE_COMMIT}'),
    ]:
        assert before in SOURCE
        yield name, SOURCE.replace(before, after)


MUTATIONS = list(mutations())


@pytest.mark.parametrize("name,source", MUTATIONS, ids=[row[0] for row in MUTATIONS])
def test_overlay_mutations(name, source):
    compile(source, radius_overlay.__file__, "exec")
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; import radius_perf_eval.radius_overlay as m; "
         "exec(compile(sys.stdin.read(), m.__file__, 'exec'), m.__dict__); "
         "import pytest; sys.exit(pytest.main(['-q', '--tb=short', 'tests/test_radius_overlay.py']))"],
        input=source, text=True, capture_output=True, timeout=120,
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "DOCKER_HOST": "unix:///nonexistent/docker.sock"},
    )
    print(f"\nMUTATION {name}\n{result.stdout}{result.stderr}", flush=True)
    assert result.returncode == 1 and "FAILED " in result.stdout, (
        f"{name} survived or failed outside a test"
    )
