"""Remove each smoke guard in a fresh offline interpreter."""

import ast
import os
from pathlib import Path
import subprocess
import sys
import unittest

from radius_perf_eval import kind_smoke


class GuardMutations(unittest.TestCase):
    def test_guards(self):
        source = Path(kind_smoke.__file__).read_text()
        lines = source.splitlines(keepends=True)
        guards = [node for node in ast.walk(ast.parse(source))
                  if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                  and node.func.id == "require"]
        self.assertTrue(guards)
        cases = []
        for node in guards:
            condition = node.args[0]
            start = sum(map(len, lines[:condition.lineno - 1])) + condition.col_offset
            end = sum(map(len, lines[:condition.end_lineno - 1])) + condition.end_col_offset
            cases.append((f"guard-{node.lineno}", source[:start] + "True" + source[end:]))
        cases.extend([
            ("eligibility", source.replace('"eligibleForTrials": False', '"eligibleForTrials": True')),
            ("premature-pass", source.replace("checks_passed = True",
                                             'self.record["status"] = "passed"; checks_passed = True')),
            ("reader-order", source.replace(
                '"for i in $(seq 1 60); do "', '"cat /data/index.html; for i in $(seq 1 60); do "')),
        ])
        for name, mutated in cases:
            with self.subTest(name=name):
                result = subprocess.run(
                    [sys.executable, "-c",
                     "import sys, unittest; from radius_perf_eval import kind_smoke as m; "
                     "exec(compile(sys.stdin.read(), m.__file__, 'exec'), m.__dict__); "
                     "suite=unittest.defaultTestLoader.discover('benchmark/tests', pattern='test_kind_smoke.py'); "
                     "r=unittest.TextTestRunner().run(suite); sys.exit(not r.wasSuccessful())"],
                    input=mutated, text=True, capture_output=True, timeout=30,
                    cwd=Path(__file__).resolve().parents[2],
                    env={**os.environ, "DOCKER_HOST": "unix:///nonexistent/docker.sock",
                         "CONTAINER_HOST": "unix:///nonexistent/podman.sock",
                         "PYTHONPATH": str(Path(__file__).resolve().parents[1])},
                )
                print(f"{name}: exit={result.returncode}", flush=True)
                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                self.assertIn("FAIL:", result.stderr, result.stderr)


if __name__ == "__main__":
    unittest.main()
