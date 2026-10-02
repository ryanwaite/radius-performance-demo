"""Weaken each source-preparation rejection guard in a fresh offline process."""

import ast
import os
from pathlib import Path
import subprocess
import sys

import pytest

from radius_perf_eval import shop_fixtures


SOURCE = Path(shop_fixtures.__file__).read_text()
SELECTORS = {
    "safe_path": "unsafe_paths",
    "read_archive": "archive_positive or nonregular_archive or archive_size",
    "source_files": "upstream_digest or prepare_same",
    "workspace_files": "workspace_content or missing_and_symlink",
    "verify_workspace": "workspace_content or git_isolation or index_cannot or empty_workspace",
    "materialize": "materialize_manifest or rehashed",
    "main": "fetch_digest",
    "archive-path-wiring": "archive_positive",
    "source-filter-wiring": "prepare_wiring",
    "workspace-verification-wiring": "prepare_wiring",
    "forced-source-add": "prepare_wiring",
    "private-defaults": "prepare_same",
}


def mutations():
    lines = SOURCE.splitlines(keepends=True)
    for function in ast.parse(SOURCE).body:
        if not isinstance(function, ast.FunctionDef):
            continue
        for node in ast.walk(function):
            if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                    and node.func.id == "require"):
                continue
            condition = node.args[0]
            start = sum(map(len, lines[:condition.lineno - 1])) + condition.col_offset
            end = sum(map(len, lines[:condition.end_lineno - 1])) + condition.end_col_offset
            yield (f"{function.name}-guard-{condition.lineno}",
                   SOURCE[:start] + "True" + SOURCE[end:], SELECTORS[function.name])
    for name, before, after in [
        ("archive-path-wiring", "safe_path(raw)", "pass"),
        ("source-filter-wiring", "if exclusion(name) is None}", "if True}"),
        ("workspace-verification-wiring", "return verify_workspace(output, manifest)", "return {}"),
        ("forced-source-add", '"add", "--force", "--all"', '"add", "--all"'),
        ("private-defaults", 'omitted.append(key)', 'defaults.append(f"{key}={value}")'),
    ]:
        assert before in SOURCE
        yield name, SOURCE.replace(before, after), SELECTORS[name]


MUTATIONS = list(mutations())


@pytest.mark.parametrize("name,source,selector", MUTATIONS, ids=[row[0] for row in MUTATIONS])
def test_fixture_mutations(name, source, selector):
    compile(source, shop_fixtures.__file__, "exec")
    result = subprocess.run(
        [sys.executable, "-c",
         "import sys; import radius_perf_eval.shop_fixtures as m; "
         "exec(compile(sys.stdin.read(), m.__file__, 'exec'), m.__dict__); "
         "import pytest; sys.exit(pytest.main(['-q', '--tb=short', 'tests/test_shop_fixtures.py', "
         "'-k', sys.argv[1]]))", selector],
        input=source, text=True, capture_output=True, timeout=90,
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "DOCKER_HOST": "unix:///nonexistent/docker.sock"},
    )
    print(f"\nMUTATION {name}\n{result.stdout}{result.stderr}", flush=True)
    assert result.returncode == 1 and "FAILED " in result.stdout, (
        f"{name} survived or failed outside a test"
    )
