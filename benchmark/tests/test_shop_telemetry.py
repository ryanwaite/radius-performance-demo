"""Offline positive, fault and mutation controls for the ingestion witness."""

from __future__ import annotations

import ast
import copy
import importlib.util
import io
import json
import subprocess
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from radius_perf_eval import shop_telemetry as telemetry, shop_assets

ROOT = Path(__file__).resolve().parents[2]
SHARDS = {"total": 1, "successful": 1, "failed": 0}


def exposition():
    return "\n".join([
        '# HELP otelcol_receiver_accepted_log_records Records',
        'unrelated_metric{label="untouched"} 1.25',
        *(f'otelcol_receiver_{kind}_log_records{{receiver="otlp",transport="{transport}"}} {value}'
          for kind, value in (("accepted", 5), ("refused", 0), ("failed", 0))
          for transport in ("grpc", "http")),
        *(f'otelcol_exporter_sent_log_records{{exporter="{name}"}} 10' for name in ("debug", "opensearch")),
    ]) + "\n"


def sources():
    return {"_shards": SHARDS, "hits": {"total": {"value": 2, "relation": "eq"}, "hits": [
        {"_source": {"body": "marker", "attributes": {
            "http": "scalar preserved", "http.request.method": "GET", "radius.acceptance.value": value,
        }}} for value in (7, 13)
    ]}}


def grafana():
    return {"results": {"A": {"status": 200, "frames": [{
        "schema": {"fields": [{"name": name} for name in ("@timestamp", "resource.service.name", "body")]},
        "data": {"values": [[1], ["checkout"], ["actual application message"]]},
    }]}}}


class LogAcceptanceTests(unittest.TestCase):
    def test_grafana_outbound_inventory_has_coverage_and_positive_controls(self):
        startup = 'logger=settings msg="Starting Grafana"\n'
        report = telemetry.audit_grafana_logs(startup, {"grafana", "opensearch"})
        self.assertEqual(report["linesExamined"], 1)
        self.assertEqual(report["unexplainedCandidates"], [])
        with self.assertRaises(telemetry.LogIngestionError):
            telemetry.audit_grafana_logs("", {"grafana"})
        with self.assertRaises(telemetry.LogIngestionError):
            telemetry.audit_grafana_logs("truncated log tail", {"grafana"})
        internal = telemetry.audit_grafana_logs(startup + "url=http://opensearch:9200\n", {"opensearch"})
        self.assertEqual(internal["destinations"][0]["host"], "opensearch")
        self.assertEqual(internal["unexplainedCandidates"], [])
        for line in ("url=https://grafana.com/api", "url=https://unknown.example", "download failed",
                     "lookup example.org: no such host", "dial tcp 1.2.3.4:443", "contact grafana.com"):
            report = telemetry.audit_grafana_logs(startup + line, {"grafana"})
            self.assertTrue(report["unexplainedCandidates"], line)

    def test_counters_require_complete_nonempty_typed_evidence(self):
        good = exposition()
        self.assertEqual(telemetry.counters(good)["sent:opensearch"], 10)
        invalid = [
            "", good.replace(' 5', ' 0'), good + good,
            good.replace(' 5', ' NaN'), good.replace(' 5', ' -1'), good.replace(' 5', ' 1.5'),
            good.replace('receiver="otlp"', 'receiver="other"'),
            good.replace('transport="grpc"', 'transport="unknown"'),
            good.replace('exporter="debug"', 'exporter="other"'),
            good.replace('receiver="otlp"', 'receiver="otlp",receiver="otlp"'),
            good.replace('receiver="otlp"', 'not-a-label'),
            good + 'otelcol_exporter_sent_log_records{exporter="other"} 1',
            good + 'otelcol_receiver_accepted_log_records{receiver="otlp",transport="other"} 1',
        ]
        for line in good.splitlines():
            if line.startswith("otelcol"):
                invalid.append(good.replace(line, ""))
        for kind in ("refused", "failed"):
            invalid.append(good.replace(f'{kind}_log_records{{receiver="otlp",transport="grpc"}} 0',
                                       f'{kind}_log_records{{receiver="otlp",transport="grpc"}} 1'))
        for kind in ("send_failed", "enqueue_failed"):
            invalid.append(good + f'otelcol_exporter_{kind}_log_records{{exporter="opensearch"}} 1')
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(telemetry.LogIngestionError):
                telemetry.counters(text)

    def test_count_requires_successful_shards(self):
        good = {"count": 10, "_shards": SHARDS}
        self.assertEqual(telemetry.shard_count(good), 10)
        for count in (None, 0, -1, True, 1.5, "10"):
            with self.subTest(count=count), self.assertRaises(telemetry.LogIngestionError):
                telemetry.shard_count({**good, "count": count})
        for shards in ({}, {"total": 0, "successful": 0, "failed": 0},
                       {"total": 1, "successful": 0, "failed": 0},
                       {"total": 1, "successful": 1, "failed": 1},
                       {"total": True, "successful": 1, "failed": 0},
                       {"total": "1", "successful": "1", "failed": 0}):
            with self.subTest(shards=shards), self.assertRaises(telemetry.LogIngestionError):
                telemetry.shard_count({**good, "_shards": shards})

    def test_refresh_distinguishes_unassigned_replicas_from_failed_queries(self):
        telemetry.verify_refresh({"_shards": SHARDS})
        telemetry.verify_refresh({"_shards": {**SHARDS, "total": 2}})
        for shards in ({}, {"total": 0, "successful": 0, "failed": 0},
                       {"total": 1, "successful": 0, "failed": 0},
                       {"total": 1, "successful": 2, "failed": 0},
                       {"total": 1, "successful": 1, "failed": 1},
                       {"total": "1", "successful": 1, "failed": 0},
                       {"total": 1, "successful": True, "failed": 0}):
            with self.subTest(shards=shards), self.assertRaises(telemetry.LogIngestionError):
                telemetry.verify_refresh({"_shards": shards})

    def test_reconciliation_requires_a_stable_balanced_bracket(self):
        before = telemetry.counters(exposition())
        self.assertTrue(telemetry.reconciled(before, 10, before.copy()))
        self.assertTrue(telemetry.reconciled({**before, "sent:opensearch": 9}, 10, before.copy()))
        self.assertFalse(telemetry.reconciled(before, 9, before.copy()))
        self.assertFalse(telemetry.reconciled(before, 11, before.copy()))
        self.assertFalse(telemetry.reconciled(before, 10, {**before, "accepted:http": 6}))
        self.assertFalse(telemetry.reconciled(before, 10, {**before, "accepted:grpc": 6}))
        for key in ("sent:debug", "sent:opensearch"):
            changed = {**before, key: 9}
            self.assertFalse(telemetry.reconciled(changed, 10, changed))
        zero = {key: 0 for key in before}
        self.assertFalse(telemetry.reconciled(zero, 0, zero))
        with self.assertRaisesRegex(telemetry.LogIngestionError, "decreased"):
            telemetry.reconciled(before, 10, {**before, "accepted:http": 4})
        with self.assertRaisesRegex(telemetry.LogIngestionError, "decreased"):
            telemetry.reconciled({**before, "send-failed:opensearch": 0}, 10, before)

    def test_reconciliation_retries_refresh_lag_but_not_forever(self):
        raw = exposition()
        responses = [(raw, {"_shards": SHARDS}, {"count": 9, "_shards": SHARDS}, raw),
                     (raw, {"_shards": SHARDS}, {"count": 10, "_shards": SHARDS}, raw)]
        evidence = Mock()
        with patch.object(telemetry, "read_bracket", side_effect=responses) as request, \
             patch.object(telemetry.time, "sleep"):
            self.assertTrue(telemetry.reconcile("p", evidence)["reconciled"])
        self.assertEqual(request.call_count, 2)
        self.assertEqual(evidence.call_count, 2)
        with patch.object(telemetry, "read_bracket", side_effect=responses[:1]):
            with self.assertRaisesRegex(telemetry.LogIngestionError, "stable bracket"):
                telemetry.reconcile("p", evidence, timeout=0)
        with patch.object(telemetry, "read_bracket", return_value=(raw, {"_shards": {}}, {}, raw)):
            with self.assertRaisesRegex(telemetry.LogIngestionError, "shard"):
                telemetry.reconcile("p", evidence)

    def test_bracket_runs_ordered_requests_in_one_container_and_rejects_partial_output(self):
        delimiter = "\nRADIUS_LOG_AUDIT_FRAME\n"
        good = delimiter.join([exposition(), json.dumps({"_shards": SHARDS}),
                               json.dumps({"count": 10, "_shards": SHARDS}), exposition()])
        for code, text in ((0, good), (22, good), (0, ""), (0, good.replace('{"count":', '{"broken":['))):
            evidence = Mock()
            with patch.object(telemetry, "docker", return_value=subprocess.CompletedProcess(
                [], code, stdout=text, stderr="stderr",
            )) as runner:
                if code == 0 and text == good:
                    before, refresh, counted, after = telemetry.read_bracket("p", evidence)
                    self.assertEqual(before, after)
                    self.assertEqual(counted["count"], 10)
                    self.assertEqual(refresh["_shards"], SHARDS)
                else:
                    with self.assertRaises(telemetry.LogIngestionError):
                        telemetry.read_bracket("p", evidence)
                runner.assert_called_once()
                self.assertIn("-ec", runner.call_args.args)
                script = runner.call_args.args[-1]
                self.assertLess(script.index("_refresh"), script.index("_count"))
                self.assertEqual(script.count("http://otel-collector:8888/metrics"), 2)
                evidence.assert_called_once()

    def test_http_failures_preserve_raw_evidence_before_judgment(self):
        for code, text, succeeds in ((0, '{"a":1}', True), (0, "not JSON", False), (22, "{}", False)):
            evidence = Mock()
            with patch.object(telemetry, "docker", return_value=subprocess.CompletedProcess(
                [], code, stdout=text, stderr="diagnostic",
            )) as runner:
                if succeeds:
                    self.assertEqual(telemetry.request("p", evidence, "probe", "http://test", payload={}), {"a": 1})
                else:
                    with self.assertRaises(telemetry.LogIngestionError):
                        telemetry.request("p", evidence, "probe", "http://test")
            evidence.assert_called_once_with("probe", {"exitCode": code, "stdout": text, "stderr": "diagnostic"})
            self.assertIn("com.docker.compose.project=p", runner.call_args.args)
        with patch.object(telemetry, "docker", return_value=subprocess.CompletedProcess(
            [], 0, stdout="raw", stderr="",
        )):
            self.assertEqual(telemetry.request("p", Mock(), "probe", "http://test", raw=True), "raw")

    def test_bootstrap_installs_and_verifies_before_starting_producers(self):
        good = {"index_templates": [{"name": "log-attributes", "index_template": telemetry.TEMPLATE}]}
        for response in (good, {}, {"index_templates": []}, {"index_templates": [{}, {}]},
                         {"index_templates": [{"index_template": {}}]}):
            project = Mock(project="p")
            with patch.object(telemetry, "request", side_effect=[{"acknowledged": True}, response]):
                if response == good:
                    telemetry.start_stack(project, Mock())
                    self.assertEqual(project.up.call_count, 2)
                    self.assertEqual(project.up.call_args.kwargs, {"wait_timeout": 900})
                else:
                    with self.assertRaises(telemetry.LogIngestionError):
                        telemetry.start_stack(project, Mock())
                    self.assertEqual(project.up.call_count, 1)
                self.assertEqual(project.up.call_args_list[0].kwargs, {"services": ["opensearch"], "wait_timeout": 180})
        with patch.object(telemetry, "request", side_effect=[{"acknowledged": False}, good]):
            with self.assertRaises(telemetry.LogIngestionError):
                telemetry.start_stack(Mock(project="p"), Mock())

    def test_typed_source_controls(self):
        good = sources()
        telemetry.verify_probe_sources(good, "marker")
        variants = [{}, {**good, "_shards": {}}]
        for total in ({"value": 1, "relation": "eq"}, {"value": 2, "relation": "gte"}):
            changed = copy.deepcopy(good)
            changed["hits"]["total"] = total
            variants.append(changed)
        changed = copy.deepcopy(good)
        changed["hits"]["hits"].pop()
        variants.append(changed)
        for field, value in (("body", "wrong"), ("http", "wrong"), ("http.request.method", "wrong"),
                             ("radius.acceptance.value", "7"), ("radius.acceptance.value", 8),
                             ("radius.acceptance.value", 7.0),
                             ("radius.acceptance.value", True)):
            changed = copy.deepcopy(good)
            source = changed["hits"]["hits"][0]["_source"]
            (source if field == "body" else source["attributes"])[field] = value
            variants.append(changed)
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(telemetry.LogIngestionError):
                telemetry.verify_probe_sources(payload, "marker")

    def test_typed_probe_checks_otlp_then_sources_then_numeric_queries(self):
        ranged = {"_shards": SHARDS, "hits": {"total": {"value": 1, "relation": "eq"}},
                  "aggregations": {"mean": {"value": 13}}}
        for fault in (None, "partial", "missing", "range", "aggregation", "shards"):
            source = sources()
            source["hits"]["hits"][0]["_source"]["body"] = "radius-ingestion-id"
            source["hits"]["hits"][1]["_source"]["body"] = "radius-ingestion-id"
            if fault == "missing":
                source["hits"]["total"]["value"] = 1
            numeric = copy.deepcopy(ranged)
            if fault == "range": numeric["hits"]["total"]["value"] = 2
            if fault == "aggregation": numeric["aggregations"]["mean"]["value"] = 10
            if fault == "shards": numeric["_shards"] = {}
            responses = [{"partialSuccess": {"rejectedLogRecords": "1"}} if fault == "partial" else {},
                         {"_shards": SHARDS}, source, numeric]
            with self.subTest(fault=fault), patch.object(telemetry.uuid, "uuid4", return_value=Mock(hex="id")), \
                 patch.object(telemetry, "request", side_effect=responses) as request, \
                 patch.object(telemetry.time, "monotonic", side_effect=[0, 31]):
                if fault:
                    with self.assertRaises(telemetry.LogIngestionError):
                        telemetry.typed_probe("p", Mock())
                else:
                    self.assertTrue(telemetry.typed_probe("p", Mock())["numericQueryVerified"])
                    self.assertEqual(request.call_count, 4)
                    payload = request.call_args_list[0].kwargs["payload"]
                    records = payload["resourceLogs"][0]["scopeLogs"][0]["logRecords"]
                    self.assertEqual([r["attributes"][-1]["value"]["intValue"] for r in records], ["7", "13"])
                    self.assertEqual(request.call_args.kwargs["payload"]["query"]["bool"]["filter"][1],
                                     {"range": {"attributes.radius.acceptance.value": {"gte": 10}}})

    def test_grafana_requires_application_rows_not_probe_or_collector_rows(self):
        good = grafana()
        self.assertEqual(telemetry.verify_grafana_logs(good, {"checkout"}), {"observedServices": ["checkout"]})
        variants = [{}, {"results": {"A": {"status": 200, "frames": []}}}]
        for field, value in (("error", "bad"), ("status", 500)):
            changed = copy.deepcopy(good)
            changed["results"]["A"][field] = value
            variants.append(changed)
        for values in ([], [[1], ["checkout"]], [[1], [], ["body"]], [[None], ["checkout"], ["body"]],
                       [[1, 2], ["checkout"], ["body"]],
                       [[1], ["radius-ingestion-control"], ["body"]], [[1], ["otelcol-contrib"], ["body"]],
                       [[1], ["checkout"], [""]], [[1], ["checkout"], [None]]):
            changed = copy.deepcopy(good)
            changed["results"]["A"]["frames"][0]["data"]["values"] = values
            variants.append(changed)
        changed = copy.deepcopy(good)
        changed["results"]["A"]["frames"][0]["schema"]["fields"][0]["name"] = "wrong"
        variants.append(changed)
        for payload in variants:
            with self.subTest(payload=payload), self.assertRaises(telemetry.LogIngestionError):
                telemetry.verify_grafana_logs(payload, {"checkout"})
        with patch.object(telemetry, "request", return_value=good) as request:
            telemetry.grafana_logs("p", Mock(), {"checkout"})
            self.assertEqual(request.call_args.args[3], "http://frontend-proxy:8080/grafana/api/ds/query")
            query = request.call_args.kwargs["payload"]["queries"][0]
            self.assertEqual(query["datasource"]["uid"], "webstore-logs")


class CollectorAssetTests(unittest.TestCase):
    def test_grafana_inventory_records_real_files_and_does_not_hide_flag_references(self):
        from radius_perf_eval import astronomy_shop
        inventory = shop_assets.grafana_inventory(ROOT, set(astronomy_shop.declared_flags(ROOT)))
        self.assertTrue(inventory["files"])
        self.assertIn("flagd", inventory["searchedTerms"])
        paths = {entry["path"] for entry in inventory["files"]}
        self.assertIn("upstream/src/grafana/provisioning/datasources/opensearch.yaml", paths)
        self.assertIn(shop_assets.DATASOURCE_DERIVED, paths)
        target = shop_assets.asset_root(ROOT) / "upstream/src/grafana/provisioning/datasources/opensearch.yaml"
        original = Path.read_text
        with patch.object(Path, "read_text", lambda p, *a, **kw:
                          "title: plantedFault\nquery: flagd" if p == target else original(p, *a, **kw)):
            planted = shop_assets.grafana_inventory(ROOT, {"plantedFault"})
        self.assertTrue(any(match["terms"] == ["plantedFault"] for match in planted["matches"]))
        self.assertTrue(any(match["terms"] == ["flagd"] for match in planted["matches"]))
        with self.assertRaises(shop_assets.AssetError):
            shop_assets.grafana_inventory(ROOT, set())
        with patch.object(Path, "rglob", return_value=iter(())):
            with self.assertRaises(shop_assets.AssetError):
                shop_assets.grafana_inventory(ROOT, {"flag"})

    def test_source_and_output_hashes_are_verified(self):
        self.assertTrue(shop_assets.verify_collector_assets(ROOT).startswith("sha256:"))
        original = Path.read_bytes
        for parent in ("derived/otel-collector", "upstream/src/otel-collector"):
            path = shop_assets.asset_root(ROOT) / parent / "otelcol-config.yml"
            with patch.object(Path, "read_bytes", lambda p: b"changed" if p == path else original(p)):
                with self.assertRaisesRegex(shop_assets.AssetError, "hash"):
                    shop_assets.verify_collector_assets(ROOT)
        path = shop_assets.asset_root(ROOT) / "derived/collector-config-manifest.json"
        manifest = json.loads(path.read_text())
        original_text = Path.read_text
        for fault in ("hash", "inventory"):
            changed = copy.deepcopy(manifest)
            if fault == "hash":
                changed["manifestHash"] = "wrong"
            else:
                changed["files"].pop("otelcol-config.yml")
                changed.pop("manifestHash")
                changed["manifestHash"] = shop_assets.digest(json.dumps(changed, sort_keys=True).encode())
            with patch.object(Path, "read_text", lambda p, *a, **kw:
                              json.dumps(changed) if p == path else original_text(p, *a, **kw)):
                with self.assertRaises(shop_assets.AssetError):
                    shop_assets.verify_collector_assets(ROOT)

    def test_direct_reader_preserves_the_original_otlp_reader(self):
        spec = importlib.util.spec_from_file_location("derive", ROOT / "benchmark/tools/derive_collector_config.py")
        tool = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(tool)
        original = {"periodic": {"exporter": {"otlp": {"endpoint": "existing"}}}}
        document = {"service": {"telemetry": {"metrics": {"readers": [copy.deepcopy(original)]}}}}
        self.assertTrue(tool.add_direct_metrics(document))
        self.assertEqual(document["service"]["telemetry"]["metrics"]["readers"], [
            original, {"pull": {"exporter": {"prometheus": {"host": "0.0.0.0", "port": 8888}}}},
        ])
        self.assertFalse(tool.add_direct_metrics({}))
        for readers in ([], {}, {"periodic": {}}, [{"pull": {}}]):
            with self.assertRaises(ValueError):
                tool.add_direct_metrics({"service": {"telemetry": {"metrics": {"readers": readers}}}})


class MutationTests(unittest.TestCase):
    def test_each_rejection_guard_is_exercised(self):
        # Mutate only rejection guards here. Branches governing parsing, retry,
        # and successful return are exercised by the controls above.
        global telemetry
        original = telemetry
        tree = ast.parse(Path(original.__file__).read_text())
        guards = [node for node in ast.walk(tree)
                  if isinstance(node, ast.If) and node.body and isinstance(node.body[0], ast.Raise)]
        self.assertTrue(guards)
        mutations = [(guard.lineno, None, None) for guard in guards]
        for guard in guards:
            mutations.extend(
                (guard.lineno, expression.lineno, index)
                for expression in ast.walk(guard.test) if isinstance(expression, ast.BoolOp)
                for index in range(len(expression.values))
            )
        reconciliation = next(node for node in tree.body
                              if isinstance(node, ast.FunctionDef) and node.name == "reconciled")
        returned = next(node for node in ast.walk(reconciliation) if isinstance(node, ast.Return))
        mutations.extend((returned.lineno, returned.value.lineno, index)
                         for index in range(len(returned.value.values)))
        survivors = []
        try:
            for line, expression_line, operand in mutations:
                mutated = copy.deepcopy(tree)
                target = next(node for node in ast.walk(mutated) if isinstance(node, (ast.If, ast.Return))
                              and node.lineno == line)
                if operand is None:
                    target.test = ast.Constant(False)
                else:
                    expression = next(node for node in ast.walk(target)
                                      if isinstance(node, ast.BoolOp) and node.lineno == expression_line)
                    expression.values[operand] = ast.Constant(isinstance(expression.op, ast.And))
                module = types.ModuleType("radius_perf_eval._mutated_telemetry")
                module.__package__ = "radius_perf_eval"
                exec(compile(ast.fix_missing_locations(mutated), original.__file__, "exec"), module.__dict__)
                telemetry = module
                suite = unittest.defaultTestLoader.loadTestsFromTestCase(LogAcceptanceTests)
                result = unittest.TextTestRunner(stream=io.StringIO()).run(suite)
                if result.wasSuccessful():
                    survivors.append((line, expression_line, operand))
        finally:
            telemetry = original
        self.assertEqual(survivors, [], f"unexercised log guards at lines {survivors}")
