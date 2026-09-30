"""Run the shipped dashboard's JavaScript checks without Docker or npm packages."""

import os
from pathlib import Path
import shutil
import subprocess
import unittest


class DashboardTests(unittest.TestCase):
    def test_dashboard_contract_rendering_downloads_and_mutations(self):
        node = shutil.which("node")
        self.assertIsNotNone(node, "Node.js is required to exercise the dashboard, not skip it")
        checks = Path(__file__).with_name("dashboard_checks.cjs")
        result = subprocess.run(
            [node, str(checks)],
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "DOCKER_HOST": "unix:///nonexistent/docker.sock"},
        )
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("Dashboard checks and mutation controls passed", result.stdout)


if __name__ == "__main__":
    unittest.main()
