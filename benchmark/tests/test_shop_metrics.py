"""Fault and mutation controls for fresh metric-export evidence."""

import ast
import copy
import io
import types
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from radius_perf_eval import shop_metrics as metrics


def direct():
    return '\n'.join([
        'target_info{service_name="otelcol-contrib",service_instance_id="instance"} 1',
        'otelcol_exporter_sent_metric_points{exporter="debug"} 10',
        'otelcol_exporter_sent_metric_points{exporter="otlp_http/prometheus"} 10',
        'otelcol_receiver_failed_metric_points{receiver="otlp",transport="grpc"} 0',
    ])


def backend():
    return {"status": "success", "data": {"resultType": "matrix", "result": [
        {"metric": {"__name__": metrics.SENT + "_total", "exporter": exporter,
                    "service_instance_id": "instance"}, "values": [[100, "10"]]}
        for exporter in sorted(metrics.EXPORTERS)
    ]}}


class MetricTests(unittest.TestCase):
    def test_direct_requires_positive_exports_and_one_identity(self):
        good = direct()
        baseline = metrics.direct_counters(good)
        self.assertEqual(baseline["sent"], {"debug": 10, "otlp_http/prometheus": 10})
        self.assertEqual(baseline["instance"], "instance")
        self.assertEqual(baseline["observedFailureCounters"][0]["value"], 0)
        # Failure-only exporter series can be absent, but are never reported as zero.
        self.assertEqual(metrics.direct_counters(
            '\n'.join(good.split('\n')[:-1]))["observedFailureCounters"], [])
        invalid = [
            "", good + "\n" + good.splitlines()[0],
            good.replace('service_name="otelcol-contrib"', 'service_name="other"'),
            good.replace('service_instance_id="instance"', 'service_instance_id=""'),
            good.replace('exporter="debug"', 'exporter="other"'),
            good + "\n" + good.splitlines()[1], good.replace(" 10", " 0"),
        ]
        invalid.extend(good.replace(line, "") for line in good.splitlines()[:3])
        for text in invalid:
            with self.subTest(text=text), self.assertRaises(metrics.MetricExportError):
                metrics.direct_counters(text)
        for name in metrics.FAILURES:
            with self.subTest(name=name), self.assertRaisesRegex(metrics.MetricExportError, "metric failure"):
                metrics.direct_counters(good + f'\n{name}{{exporter="otlp_http/prometheus"}} 1')

    def test_backend_rejects_untrusted_shapes_and_identities(self):
        baseline = metrics.direct_counters(direct())
        good = backend()
        self.assertTrue(metrics.backend_counters(good, baseline, 100)["ready"])
        invalid = []
        for key, value in (("status", "error"), ("warnings", ["partial"]), ("infos", ["omitted"])):
            changed = copy.deepcopy(good)
            changed[key] = value
            invalid.append(changed)
        changed = copy.deepcopy(good)
        changed["data"]["resultType"] = "vector"
        invalid.append(changed)
        for key, value in (("__name__", "other"), ("service_instance_id", "replaced"), ("exporter", "other")):
            changed = copy.deepcopy(good)
            changed["data"]["result"][0]["metric"][key] = value
            invalid.append(changed)
        changed = copy.deepcopy(good)
        changed["data"]["result"].append(copy.deepcopy(changed["data"]["result"][0]))
        invalid.append(changed)
        for values in ([], [[100, "NaN"]], [[100, "-1"]], [[float("inf"), "10"]]):
            changed = copy.deepcopy(good)
            changed["data"]["result"][0]["values"] = values
            invalid.append(changed)
        for payload in invalid:
            with self.subTest(payload=payload), self.assertRaises(metrics.MetricExportError):
                metrics.backend_counters(payload, baseline, 100)

    def test_empty_partial_stale_and_lagging_backend_are_not_ready(self):
        baseline = metrics.direct_counters(direct())
        for fault in ("empty", "missing", "stale", "behind"):
            changed = backend()
            if fault == "empty":
                changed["data"]["result"] = []
            elif fault == "missing":
                changed["data"]["result"].pop()
            elif fault == "stale":
                changed["data"]["result"][0]["values"] = [[99, "1000"]]
            else:
                changed["data"]["result"][0]["values"] = [[100, "9"]]
            with self.subTest(fault=fault):
                self.assertFalse(metrics.backend_counters(changed, baseline, 100)["ready"])

    def test_poll_waits_for_real_periodic_samples_and_preserves_attempts(self):
        empty = backend()
        empty["data"]["result"] = []
        evidence = Mock()
        with patch.object(metrics, "request", side_effect=[
            direct(), empty, direct(), backend(), direct(),
        ]) as request, patch.object(metrics.time, "time", return_value=100), \
             patch.object(metrics.time, "sleep") as sleep:
            result = metrics.verify("p", evidence)
        self.assertTrue(result["backend"]["ready"])
        self.assertEqual(request.call_count, 5)
        self.assertIn("%5B2m%5D", request.call_args_list[1].args[-1])
        self.assertIn("service_instance_id", request.call_args_list[1].args[-1])
        self.assertEqual([call.args[1]["backend"]["ready"] for call in evidence.call_args_list], [False, True])
        sleep.assert_called_once_with(2)

    def test_timeout_failed_exports_and_restart_cannot_pass(self):
        for fault in ("empty", "replaced", "decreased", "failed-open", "failed-close"):
            after = direct()
            payload = backend()
            first = direct()
            if fault == "empty":
                payload["data"]["result"] = []
            elif fault == "replaced":
                after = after.replace('"instance"', '"replaced"')
            elif fault == "decreased":
                after = after.replace(" 10", " 9")
            else:
                failed = direct() + '\notelcol_exporter_send_failed_metric_points{exporter="otlp_http/prometheus"} 171'
                if fault == "failed-open":
                    first = failed
                else:
                    after = failed
            with self.subTest(fault=fault), patch.object(metrics, "request", side_effect=[first, payload, after]), \
                 patch.object(metrics.time, "time", return_value=100):
                with self.assertRaises(metrics.MetricExportError):
                    metrics.verify("p", Mock(), timeout=0)


class MutationTests(unittest.TestCase):
    def test_guards_and_readiness_operands_are_live(self):
        global metrics
        original = metrics
        tree = ast.parse(Path(original.__file__).read_text())
        guards = [node for node in ast.walk(tree) if isinstance(node, ast.If)
                  and node.body and isinstance(node.body[0], ast.Raise)]
        targets = [(node.lineno, None) for node in guards]
        for node in ast.walk(tree):
            if isinstance(node, ast.BoolOp):
                targets.extend((node.lineno, index) for index in range(len(node.values)))
        self.assertTrue(targets)
        survivors = []
        try:
            for line, operand in targets:
                changed = copy.deepcopy(tree)
                if operand is None:
                    target = next(node for node in ast.walk(changed)
                                  if isinstance(node, ast.If) and node.lineno == line)
                    target.test = ast.Constant(False)
                else:
                    target = next(node for node in ast.walk(changed)
                                  if isinstance(node, ast.BoolOp) and node.lineno == line)
                    target.values[operand] = ast.Constant(isinstance(target.op, ast.And))
                module = types.ModuleType("radius_perf_eval._mutated_metrics")
                module.__package__ = "radius_perf_eval"
                exec(compile(ast.fix_missing_locations(changed), original.__file__, "exec"), module.__dict__)
                metrics = module
                result = unittest.TextTestRunner(stream=io.StringIO()).run(
                    unittest.defaultTestLoader.loadTestsFromTestCase(MetricTests))
                if result.wasSuccessful():
                    survivors.append((line, operand))
        finally:
            metrics = original
        self.assertEqual(survivors, [])
