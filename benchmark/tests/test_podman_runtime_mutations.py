"""Delete inventory guards and dispatch blocks in fresh offline interpreters."""

import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest

from radius_perf_eval import podman_runtime


SOURCE = Path(podman_runtime.__file__).read_text()


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
    yield "live-policy-refusal", SOURCE.replace(
        "def require_podman_driver() -> None:\n", "def require_podman_driver() -> None:\n    return\n")
    yield "eligibility", SOURCE.replace('"eligibleForTrials": False', '"eligibleForTrials": True')
    yield "empty-status-success", SOURCE.replace('"status": "incomplete"', '"status": "inventoried-not-qualified"')
    yield "raw-before-parse", SOURCE.replace('        save()\n        require(result.returncode',
                                            '        require(result.returncode')


MUTATIONS = list(mutations())
CASES = [(name, "radius_perf_eval.podman_runtime", source) for name, source in MUTATIONS]
ROOT = Path(__file__).resolve().parents[1]
for module, path in [
    ("radius_perf_eval.cli", ROOT / "radius_perf_eval/cli.py"),
    *[(f"tools.{name}", ROOT / "tools" / f"{name}.py") for name in
      ("run_holdout", "measure_astronomy_footprint", "pin_astronomy_shop_images")],
]:
    source = path.read_text()
    assert "require_podman_driver()" in source
    CASES.append((f"dispatch-{module}", module, source.replace("require_podman_driver()", "pass")))


@pytest.mark.parametrize("name,module,source", CASES, ids=[row[0] for row in CASES])
def test_runtime_mutations(name, module, source):
    compile(source, podman_runtime.__file__, "exec")
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys, importlib; m = importlib.import_module(sys.argv[1]); "
         "exec(compile(sys.stdin.read(), m.__file__, 'exec'), m.__dict__); "
         "import pytest; sys.exit(pytest.main(['-q', '--tb=short', 'tests/test_podman_runtime.py']))", module],
        input=source, text=True, capture_output=True, timeout=90,
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "DOCKER_HOST": "unix:///nonexistent/docker.sock",
             "CONTAINER_HOST": "unix:///nonexistent/podman.sock"},
    )
    print(f"\nMUTATION {name}\n{result.stdout}{result.stderr}", flush=True)
    assert result.returncode == 1 and "FAILED " in result.stdout, (
        f"{name} survived or failed outside a test"
    )
