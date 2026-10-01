"""Offline extraction controls. No test claims to classify arbitrary semantics."""

from __future__ import annotations

import ast
import copy
import io
import json
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch

from radius_perf_eval import grafana_review as review
from radius_perf_eval.shop_assets import AssetError, digest

ROOT = Path(__file__).resolve().parents[2]
PREFIX = "upstream/src/grafana/"
DASHBOARD = PREFIX + "provisioning/dashboards/demo/example.json"


class ReviewTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name)
        self.root = self.repo / "benchmark/apps/astronomy-shop"
        self.sources = {
            DASHBOARD: json.dumps({"title": "Neutral", "panels": [{"title": "Healthy"}]}),
            PREFIX + "provisioning/alerting/rules.yaml": "# An incident hint can be in a comment\n",
            PREFIX + "provisioning/datasources/default.yaml": "url: http://prometheus:9090\n",
            PREFIX + "provisioning/dashboards/demo.yaml": "providers: [{name: Demo}]\n",
            PREFIX + "grafana.ini": "; A comment is also visible to the agent\n[server]\nhttp_port = 3000\n",
            "derived/grafana/opensearch.yaml": "database: '[otel-logs-]YYYY-MM-DD'\n",
        }

    def capture(self, *, corrupt_hash=False):
        entries = []
        for name, text in self.sources.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text)
            entries.append({"path": name, "sha256": digest(text.encode()),
                            "lines": len(text.splitlines())})
        if corrupt_hash:
            entries[0]["sha256"] = "sha256:changed"
        inventory = {"files": entries, "matches": [], "searchedTerms": ["flagd"]}
        with patch.object(review, "grafana_inventory", return_value=inventory), \
             patch.object(review.astronomy_shop, "declared_flags", return_value={"fault": {}}):
            return review.review_surfaces(self.repo)

    def test_all_roles_and_raw_text_are_preserved_without_a_verdict(self):
        report = self.capture()
        self.assertEqual(report["semanticVerdict"], "not-established")
        self.assertIs(report["reviewRequired"], True)
        self.assertEqual(report["schemaVersion"], "radius-grafana-review-v1")
        self.assertIn("not a sealed or running fixture", report["scope"])
        self.assertTrue(report["limitations"])
        self.assertEqual({d["path"] for d in report["documents"]}, set(self.sources))
        self.assertEqual({d["role"] for d in report["documents"]},
                         {"dashboard", "alerting", "datasource", "dashboard-provider", "configuration"})
        self.assertEqual([d["role"] for d in report["documents"]],
                         ["dashboard", "alerting", "datasource", "dashboard-provider",
                          "configuration", "datasource"])
        for document in report["documents"]:
            self.assertEqual(document["sha256"], digest(self.sources[document["path"]].encode()))
            if "fields" not in document:
                self.assertEqual(document["lines"], [
                    {"line": n, "text": line}
                    for n, line in enumerate(self.sources[document["path"]].splitlines(), 1)
                ])

    def test_planted_nonliteral_hints_survive_unknown_fields_and_types(self):
        planted = {
            "title": "Cart latency cause",
            "panels": [{
                "description": "Increase the connection pool to fix the bottleneck",
                "targets": [{"expr": "queue_depth{service='checkout'} > 42"}],
                "links": [{"url": "https://example.invalid/incident-answer"}],
                "unknown": {"answer/key~": "database lock contention", "threshold": 0.0001,
                            "enabled": False, "unset": None, "empty": {}, "array": []},
            }],
        }
        self.sources[DASHBOARD] = json.dumps(planted)
        report = self.capture()
        fields = next(d["fields"] for d in report["documents"] if d["path"] == DASHBOARD)
        self.assertEqual(fields, [
            {"pointer": "/title", "value": "Cart latency cause"},
            {"pointer": "/panels/0/description", "value": "Increase the connection pool to fix the bottleneck"},
            {"pointer": "/panels/0/targets/0/expr", "value": "queue_depth{service='checkout'} > 42"},
            {"pointer": "/panels/0/links/0/url", "value": "https://example.invalid/incident-answer"},
            {"pointer": "/panels/0/unknown/answer~1key~0", "value": "database lock contention"},
            {"pointer": "/panels/0/unknown/threshold", "value": 0.0001},
            {"pointer": "/panels/0/unknown/enabled", "value": False},
            {"pointer": "/panels/0/unknown/unset", "value": None},
            {"pointer": "/panels/0/unknown/empty", "value": {}},
            {"pointer": "/panels/0/unknown/array", "value": []},
        ])
        self.assertEqual(report["semanticVerdict"], "not-established")

    def test_malformed_or_empty_dashboard_cannot_supply_review_coverage(self):
        invalid = [
            "", " ", "{", "[]", "null", '{"title": "A", "title": "B", "panels": [{}]}',
            '{"title":"A","panels":[{"hint":"answer","hint":"hidden"}]}',
            '{"title":"A","panels":[{}],"other":NaN}',
            '{"title":"A","panels":[{}],"other":Infinity}',
            '{"title":"A","panels":[{}],"other":-Infinity}',
            '{"title":"A","panels":[{}],"other":1e1000}',
        ]
        for title in (None, "", " ", [], 42):
            invalid.append(json.dumps({"title": title, "panels": [{}]}))
        for panels in (None, [], {}, "not panels"):
            invalid.append(json.dumps({"title": "A", "panels": panels}))
        for text in invalid:
            with self.subTest(text=text):
                self.sources[DASHBOARD] = text
                with self.assertRaises((AssetError, ValueError)):
                    self.capture()

    def test_missing_roles_empty_text_and_changed_bytes_fail(self):
        original = self.sources.copy()
        for removed in (
            [DASHBOARD], [PREFIX + "provisioning/alerting/rules.yaml"],
            [PREFIX + "provisioning/datasources/default.yaml", "derived/grafana/opensearch.yaml"],
            [PREFIX + "provisioning/dashboards/demo.yaml"], [PREFIX + "grafana.ini"],
            list(original),
        ):
            self.sources = {k: v for k, v in original.items() if k not in removed}
            with self.subTest(removed=removed), self.assertRaises(AssetError):
                self.capture()
        self.sources = original.copy()
        self.sources[PREFIX + "grafana.ini"] = "\n "
        with self.assertRaisesRegex(AssetError, "empty"):
            self.capture()
        self.sources = original
        with self.assertRaisesRegex(AssetError, "changed"):
            self.capture(corrupt_hash=True)

    def test_real_inventory_covers_all_files_and_records_known_semantic_hints(self):
        report = review.review_surfaces(ROOT)
        root = ROOT / "benchmark/apps/astronomy-shop"
        expected = {str(p.relative_to(root)) for p in (root / PREFIX).rglob("*") if p.is_file()}
        expected.add("derived/grafana/opensearch.yaml")
        documents = {d["path"]: d for d in report["documents"]}
        self.assertTrue(expected)
        self.assertEqual(set(documents), expected)
        for name, doc in documents.items():
            self.assertEqual(doc["sha256"], digest((root / name).read_bytes()))
        demo = documents[PREFIX + "provisioning/dashboards/demo/demo-dashboard.json"]
        fields = {f["pointer"]: f["value"] for f in demo["fields"]}
        self.assertEqual(fields["/panels/14/targets/0/queryType"], "dependencyGraph")
        self.assertEqual(fields["/panels/11/title"], "Recommendations Rate")
        cart = documents[PREFIX + "provisioning/alerting/cart-service-alerting.yml"]
        self.assertTrue(any("CartAddItemHighLatency" in line["text"] for line in cart["lines"]))
        self.assertTrue(any("0.0001" in line["text"] for line in cart["lines"]))
        self.assertEqual(report["inventory"]["matches"], [])

    def test_numeric_values_must_be_finite(self):
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value), self.assertRaises(AssetError):
                review.json_leaves(value)
        self.assertEqual(review.json_leaves(0.0001), [{"pointer": "", "value": 0.0001}])

    def test_cli_writes_new_artifact_and_never_overwrites(self):
        output = self.repo / "review.json"
        with patch("sys.argv", ["review", "--repo-root", str(ROOT), "--output", str(output)]):
            review.main()
            before = output.read_bytes()
            self.assertTrue(json.loads(before)["documents"])
            with self.assertRaises(FileExistsError):
                review.main()
            self.assertEqual(output.read_bytes(), before)


class MutationTests(unittest.TestCase):
    def test_all_rejection_guards_and_boolean_operands_are_exercised(self):
        global review
        original = review
        baseline = unittest.TextTestRunner(stream=io.StringIO()).run(
            unittest.defaultTestLoader.loadTestsFromTestCase(ReviewTests))
        self.assertTrue(baseline.wasSuccessful(), "mutation controls require a passing unmutated suite")
        tree = ast.parse(Path(original.__file__).read_text())
        targets = [(n.lineno, None) for n in ast.walk(tree) if isinstance(n, ast.If)
                   and n.body and isinstance(n.body[0], ast.Raise)]
        targets += [(n.lineno, i) for n in ast.walk(tree) if isinstance(n, ast.BoolOp)
                    for i in range(len(n.values))]
        targets += [(0, "object_pairs_hook"), (0, "exclusive-output")]
        self.assertTrue(targets)
        survivors = []
        try:
            for line, operand in targets:
                changed = copy.deepcopy(tree)
                if isinstance(operand, str):
                    if operand == "exclusive-output":
                        node = next(n for n in ast.walk(changed) if isinstance(n, ast.Call)
                                    and isinstance(n.func, ast.Attribute) and n.func.attr == "open")
                        node.args[0] = ast.Constant("w")
                    else:
                        node = next(n for n in ast.walk(changed) if isinstance(n, ast.Call)
                                    and isinstance(n.func, ast.Attribute) and n.func.attr == "loads")
                        node.keywords = [k for k in node.keywords if k.arg != operand]
                elif operand is None:
                    node = next(n for n in ast.walk(changed) if isinstance(n, ast.If) and n.lineno == line)
                    node.test = ast.Constant(False)
                else:
                    node = next(n for n in ast.walk(changed) if isinstance(n, ast.BoolOp) and n.lineno == line)
                    node.values[operand] = ast.Constant(isinstance(node.op, ast.And))
                module = types.ModuleType("radius_perf_eval._mutated_review")
                module.__package__ = "radius_perf_eval"
                module.__file__ = original.__file__
                exec(compile(ast.fix_missing_locations(changed), original.__file__, "exec"), module.__dict__)
                review = module
                result = unittest.TextTestRunner(stream=io.StringIO()).run(
                    unittest.defaultTestLoader.loadTestsFromTestCase(ReviewTests))
                if result.wasSuccessful():
                    survivors.append((line, operand))
        finally:
            review = original
        self.assertEqual(survivors, [])
