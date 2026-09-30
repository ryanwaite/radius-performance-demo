"""Real-input and fault-injection controls for Shop startup and measurements."""

from __future__ import annotations

import ast
import copy
import json
import os
import subprocess
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest.mock import Mock, patch

from radius_perf_eval import cpu_limits as cpu, offered_load as load, shop_assets as assets
from radius_perf_eval import shop_environment as environment, shop_readiness as readiness
from radius_perf_eval.cli import build_parser
from radius_perf_eval.compose import ResidualResources
from radius_perf_eval.hostclass import HostFacts

ROOT = Path(__file__).resolve().parents[2]
FULL = "a" * 64
HOST_CLASS = json.loads((assets.asset_root(ROOT) / "cpu-limits.json").read_text())["fittedFrom"]["hostClass"]


def completed(text="", code=0):
    return subprocess.CompletedProcess([], code, stdout=text, stderr="planted failure" if code else "")


class StartupAssetTests(unittest.TestCase):
    def test_committed_assets_are_complete_and_derived(self):
        manifest = assets.verify_assets(ROOT)
        self.assertEqual(manifest["upstreamCommit"], environment.shop.UPSTREAM_COMMIT)
        source = (assets.asset_root(ROOT) / "upstream/src/load-generator/locustfile.py").read_text()
        derived = assets.derive_load_script(source)
        self.assertNotIn("def ask_agent", derived)
        original = ast.parse(source)
        changed = ast.parse(derived)
        cls = next(node for node in original.body if isinstance(node, ast.ClassDef) and node.name == "WebsiteUser")
        cls.body = [node for node in cls.body if not (isinstance(node, ast.FunctionDef) and node.name == "ask_agent")]
        self.assertEqual(ast.dump(original), ast.dump(changed))
        with self.assertRaises(assets.AssetError):
            assets.derive_load_script(derived)
        proxy = (assets.asset_root(ROOT) / "upstream/src/frontend-proxy/envoy.tmpl.yaml").read_text()
        blocked = assets.derive_proxy_template(proxy)
        self.assertIn("direct_response: { status: 404 }", blocked)
        for cluster in ("flagservice", "flagd-ui", "loadgen", "opamp"):
            self.assertNotIn(f"cluster: {cluster}", blocked)
            self.assertNotIn(f"- name: {cluster}\n", blocked)
        self.assertIn("route: { cluster: grafana }", blocked)
        with self.assertRaises(assets.AssetError):
            assets.derive_proxy_template(blocked)

    def test_missing_or_modified_asset_is_rejected(self):
        assets.verify_assets(ROOT)
        original = Path.read_bytes
        with patch.object(Path, "read_bytes", lambda path:
                          b"altered" if path.name == ".env" else original(path)):
            with self.assertRaisesRegex(assets.AssetError, "hash mismatch"):
                assets.verify_assets(ROOT)
        with patch.object(Path, "is_file", lambda path: False if path.name == ".env" else True):
            with self.assertRaisesRegex(assets.AssetError, "missing"):
                assets.verify_assets(ROOT)

    def test_real_compose_renders_offline_with_both_plugin_architectures(self):
        # config reads files but does not contact the daemon.
        for architecture in ("arm64", "amd64"):
            with self.subTest(architecture=architecture), tempfile.TemporaryDirectory() as temp:
                runtime = Path(temp) / "runtime"
                runtime.mkdir()
                output = Path(temp) / "stack.json"
                with patch.object(environment, "derive_class_id", return_value=HOST_CLASS), patch.dict(
                    os.environ, {"DOCKER_HOST": "unix:///nonexistent/docker.sock",
                                 "LOCUST_USERS": "999", "GF_INSTALL_PLUGINS": "planted"}
                ):
                    result = environment.render_stack(ROOT, output, HostFacts(docker_arch=architecture), runtime)
                config = json.loads(output.read_text())
                self.assertEqual(result["loadConfiguration"]["LOCUST_USERS"], "5")
                self.assertTrue(config["networks"]["default"]["internal"])
                self.assertEqual(config["services"]["frontend-proxy"]["ports"][0]["host_ip"], "127.0.0.1")
                for name, spec in config["services"].items():
                    if name != "frontend-proxy":
                        self.assertEqual(spec["networks"], {"default": {}})
                        self.assertEqual(spec["ports"], [])
                grafana = config["services"]["grafana"]
                self.assertNotIn("GF_INSTALL_PLUGINS", grafana["environment"])
                self.assertEqual(grafana["environment"]["GF_PLUGINS_PREINSTALL_DISABLED"], "true")
                plugin = runtime / "plugins" / assets.PLUGIN_ID
                self.assertTrue((plugin / f"gpx_opensearch-datasource_linux_{architecture}").stat().st_mode & 0o111)
                for service, target in (
                    ("grafana", f"/var/lib/grafana/plugins/{assets.PLUGIN_ID}"),
                    ("load-generator", "/usr/src/app/locustfile.py"),
                    ("frontend-proxy", "/home/envoy/envoy.tmpl.yaml"),
                ):
                    mount = next(volume for volume in config["services"][service]["volumes"] if volume["target"] == target)
                    self.assertTrue(mount["read_only"])
                    self.assertFalse(mount["bind"]["create_host_path"])
                self.assertTrue((runtime / "flags/demo.flagd.json").is_file())

    def test_wrong_platform_fails_before_extraction(self):
        with tempfile.TemporaryDirectory() as temp:
            with self.assertRaisesRegex(assets.AssetError, "architecture"):
                assets.mount_assets({}, ROOT, Path(temp), "unknown")

    def test_manifest_inventory_and_link_guards(self):
        manifest = assets.verify_assets(ROOT)
        missing = copy.deepcopy(manifest)
        missing["files"].pop("upstream/.env")
        for value in ({}, {"files": {}}, missing):
            original = Path.read_text
            with patch.object(Path, "read_text", lambda path, *a, **kw:
                              json.dumps(value) if path.name == "startup-assets.json" else original(path, *a, **kw)):
                with self.assertRaises(assets.AssetError):
                    assets.verify_assets(ROOT)
        for derivation in ("derive_load_script", "derive_proxy_template"):
            with patch.object(assets, derivation, return_value="different derived source"):
                    with self.assertRaisesRegex(assets.AssetError, "derived"):
                        assets.verify_assets(ROOT)
        source = (assets.asset_root(ROOT) / "upstream/src/frontend-proxy/envoy.tmpl.yaml").read_text()
        with self.assertRaisesRegex(assets.AssetError, "route"):
            assets.derive_proxy_template(source.replace('"/feature"', '"/changed"'))

    def test_missing_services_and_duplicate_mount(self):
        for services in ({}, {"grafana": {}, "load-generator": {}, "frontend-proxy": {}}):
            if services:
                    services["grafana"]["volumes"] = [{"target": f"/var/lib/grafana/plugins/{assets.PLUGIN_ID}"}]
            with tempfile.TemporaryDirectory() as temp:
                    with self.assertRaises(assets.AssetError):
                        assets.mount_assets({"services": services}, ROOT, Path(temp), "arm64")
        original = Path.read_text
        with patch.object(Path, "read_text", lambda path, *a, **kw:
                          "{}" if path.name == "image-digests.json" else original(path, *a, **kw)):
            with self.assertRaisesRegex(assets.AssetError, "image manifest"):
                assets.verify_assets(ROOT)

    def test_archive_controls(self):
        base = {
            f"{assets.PLUGIN_ID}/LICENSE": "Apache License Version 2.0",
            f"{assets.PLUGIN_ID}/MANIFEST.txt": "synthetic test signature",
            f"{assets.PLUGIN_ID}/plugin.json": json.dumps({"id": assets.PLUGIN_ID, "info": {"version": assets.PLUGIN_VERSION}}),
        }
        changes = [
            {f"{assets.PLUGIN_ID}/LICENSE": "Nonredistributable"},
            {f"{assets.PLUGIN_ID}/plugin.json": json.dumps({"id": "wrong", "info": {"version": assets.PLUGIN_VERSION}})},
            {f"{assets.PLUGIN_ID}/plugin.json": json.dumps({"id": assets.PLUGIN_ID, "info": {"version": "wrong"}})},
            {"/absolute": "bad"}, {f"{assets.PLUGIN_ID}/../escape": "bad"},
            {f"{assets.PLUGIN_ID}/a\\b": "bad"}, {f"{assets.PLUGIN_ID}/a:b": "bad"},
        ]
        for extra in changes:
            with tempfile.TemporaryDirectory() as temp:
                archive = Path(temp) / "plugin.zip"
                with zipfile.ZipFile(archive, "w") as output:
                    for name, value in (base | extra).items():
                        output.writestr(name, value)
                with self.assertRaises(assets.AssetError):
                    assets.extract_plugin(archive, Path(temp) / "out")
        with tempfile.TemporaryDirectory() as temp:
            archive = Path(temp) / "plugin.zip"
            with zipfile.ZipFile(archive, "w") as output:
                for name, value in base.items():
                    if not name.endswith("MANIFEST.txt"):
                        output.writestr(name, value)
            with self.assertRaisesRegex(assets.AssetError, "signature"):
                assets.extract_plugin(archive, Path(temp) / "out")

    def test_render_guards(self):
        images = json.loads((assets.asset_root(ROOT) / "image-digests.json").read_text())
        config = {"services": {name: {"image": ref, "environment": {}} for name, ref in images["images"].items()}}
        original = Path.read_text
        for fault in ("compose-error", "hash", "inventory", "floating-image"):
            manifest = copy.deepcopy(images)
            if fault == "hash":
                manifest["manifestHash"] = "wrong"
            elif fault == "inventory":
                manifest["images"].pop("cart")
            elif fault == "floating-image":
                manifest["images"]["cart"] = "cart:latest"
            if fault in ("inventory", "floating-image"):
                manifest["manifestHash"] = assets.digest(json.dumps(manifest["images"], sort_keys=True, separators=(",", ":")).encode())
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                runtime = Path(temp) / "runtime"
                runtime.mkdir()
                with patch.object(environment.subprocess, "run", return_value=completed(json.dumps(config), int(fault == "compose-error"))), \
                     patch.object(environment, "derive_class_id", return_value=HOST_CLASS), \
                     patch.object(Path, "read_text", lambda path, *a, **kw:
                                  json.dumps(manifest) if path.name == "image-digests.json" else original(path, *a, **kw)):
                    with self.assertRaises((environment.ShopEnvironmentError, environment.DockerError)):
                        environment.render_stack(ROOT, Path(temp) / "stack.json", HostFacts(docker_arch="arm64"), runtime)


class CgroupResolutionTests(unittest.TestCase):
    def test_project_inventory_cannot_drop_or_duplicate_services(self):
        other = "b" * 64
        listing = completed(f"{FULL[:12]}\tpayment\n{other[:12]}\tcheckout\n")
        for inspected in (f"{FULL}\tpayment\n", f"{FULL}\tpayment\n{other}\tpayment\n"):
            with self.assertRaisesRegex(cpu.CpuLimitError, "inventory"):
                cpu._full_ids_by_service("p", timeout=5, runner=Mock(side_effect=[listing, completed(inspected)]))
        for responses, message in (([completed(code=1)], "could not list"),
                                   ([listing, completed(code=1)], "could not inspect"),
                                   ([completed("")], "no containers")):
            with self.assertRaisesRegex(cpu.CpuLimitError, message):
                cpu._full_ids_by_service("p", timeout=5, runner=Mock(side_effect=responses))

    def test_resolves_cgroupfs_and_systemd_using_host_processes(self):
        for path in (f"/docker/{FULL}", f"/system.slice/docker-{FULL}.scope"):
            runner = Mock(side_effect=[completed(f"{FULL}\t123\ttrue\n"), completed(f"{FULL}\t{path}\n")])
            self.assertEqual(cpu.resolve_cgroup_paths({FULL: "payment"}, project="p", timeout=5, runner=runner), {FULL: path})
            command = runner.call_args_list[1].args[0]
            self.assertIn("--pid=host", command)
            self.assertIn("--cgroupns=host", command)
            self.assertIn("/proc/123/cgroup", command[-1])
            self.assertNotIn("--privileged", command)

    def test_bad_inventory_and_paths_fail_closed(self):
        for inventory in ({}, {"short": "payment"}, {"a; exit 0": "payment"}):
            runner = Mock()
            with self.assertRaises(cpu.CpuLimitError):
                cpu.resolve_cgroup_paths(inventory, project="p", timeout=5, runner=runner)
            runner.assert_not_called()
        for response in ("", f"{FULL}\t0\ttrue", f"{FULL}\t12\tfalse", f"{FULL}\tx\ttrue",
                         f"{FULL}\t12\ttrue\n{FULL}\t12\ttrue", "unknown\t12\ttrue"):
            runner = Mock(return_value=completed(response))
            with self.subTest(response=response), self.assertRaises(cpu.CpuLimitError):
                cpu.resolve_cgroup_paths({FULL: "payment"}, project="p", timeout=5, runner=runner)
            self.assertEqual(runner.call_count, 1)
        for path in ("", "/", "/../etc", "/a/./b", "/a//b", "/a/$(id)", "/a;id", "relative"):
            runner = Mock(side_effect=[completed(f"{FULL}\t123\ttrue"), completed(f"{FULL}\t{path}")])
            with self.subTest(path=path), self.assertRaises(cpu.CpuLimitError):
                cpu.resolve_cgroup_paths({FULL: "payment"}, project="p", timeout=5, runner=runner)
        for response in ("", "unknown\t/docker/x", f"{FULL}\t/a\n{FULL}\t/b"):
            runner = Mock(side_effect=[completed(f"{FULL}\t123\ttrue"), completed(response)])
            with self.assertRaises(cpu.CpuLimitError):
                cpu.resolve_cgroup_paths({FULL: "payment"}, project="p", timeout=5, runner=runner)

    def test_resolver_command_failures_are_not_missing_data(self):
        for responses, message in (([completed(code=1)], "cannot resolve container processes"),
                                   ([completed(f"{FULL}\t123\ttrue"), completed(code=1)], "cannot read process cgroups")):
            with self.assertRaisesRegex(cpu.CpuLimitError, message):
                cpu.resolve_cgroup_paths({FULL: "payment"}, project="p", timeout=5, runner=Mock(side_effect=responses))

    def test_both_readers_use_resolved_path_and_reject_missing_counters(self):
        path = f"/system.slice/docker-{FULL}.scope"
        with patch.object(cpu, "_full_ids_by_service", return_value={FULL: "payment"}), \
             patch.object(cpu, "resolve_cgroup_paths", return_value={FULL: path}):
            runner = Mock(return_value=completed(
                f"==={FULL}\nnr_periods 2\nnr_throttled 0\nthrottled_usec 0\n---max\n800000 100000\n"
            ))
            self.assertEqual(cpu.read_throttling("project", runner=runner)["payment"].quota_cores, 8)
            self.assertIn(f"/hostcg{path}/cpu.stat", runner.call_args.args[0][-1])
            with self.assertRaisesRegex(cpu.CpuLimitError, "sidecar failed"):
                cpu.read_throttling("project", runner=Mock(return_value=completed(code=1)))
            for text in ("", f"==={FULL}\nnr_periods 1\n---max\n800000 100000",
                         f"==={FULL}\nnr_periods 1\nnr_throttled -1\nthrottled_usec 0\n---max\n800000 100000"):
                with self.assertRaises(cpu.CpuLimitError):
                    cpu.read_throttling("project", runner=Mock(return_value=completed(text)))
            runner = Mock(return_value=completed(f"@0\n{FULL} 0\n@1\n{FULL} 100000\n"))
            samples = cpu.sample_cpu_demand("project", duration_seconds=1, interval_seconds=1, runner=runner)
            self.assertEqual(samples["payment"].peak_cores, 0.1)
            self.assertIn(f"/hostcg{path}/cpu.stat", runner.call_args.args[0][-1])


class MeasurementControlsTests(unittest.TestCase):
    def test_load_readings_require_real_counters_and_a_finite_clock(self):
        document = {"clock": 10, "stats": {"state": "running", "user_count": 5,
                    "stats": [{"name": "Aggregated", "num_requests": 100, "num_failures": 0}]}}
        self.assertEqual(load.parse_reading(json.dumps(document)).requests, 100)
        for key in ("num_requests", "num_failures", "user_count", "clock"):
            for bad in (None, True, -1, "wrong", float("nan"), float("inf")):
                if key == "clock" and bad == -1:
                    continue
                changed = copy.deepcopy(document)
                if key == "clock": changed[key] = bad
                elif key == "user_count": changed["stats"][key] = bad
                else: changed["stats"]["stats"][0][key] = bad
                with self.assertRaises(load.OfferedLoadError):
                    load.parse_reading(json.dumps(changed))
        for text in ("", "not-json", "{}", '{"clock":10,"stats":{"stats":[]}}'):
            with self.assertRaises(load.OfferedLoadError):
                load.parse_reading(text)

    def generator(self, *, quota=8.0, periods=200, throttled=0):
        first = cpu.ThrottleReading("load-generator", 100, 0, 0, 8.0)
        last = cpu.ThrottleReading("load-generator", periods, throttled, 0, quota)
        return cpu.verdict_from_readings({"load-generator": first}, {"load-generator": last}, {"load-generator": 8})

    def test_throttle_guards_have_clean_and_faulted_controls(self):
        self.assertTrue(self.generator().accepted)
        self.assertFalse(self.generator(quota=None).accepted)
        self.assertFalse(self.generator(quota=4).accepted)
        self.assertFalse(self.generator(periods=100).accepted)
        self.assertFalse(self.generator(throttled=1).accepted)
        self.assertFalse(self.generator(throttled=-1).accepted)
        self.assertFalse(cpu.verdict_from_readings({}, {}, {}).accepted)

    def test_incident_throughput_drop_is_not_an_infrastructure_failure(self):
        first = load.LoadReading(0, 1000, 0, 5, "running")
        second = load.LoadReading(60, 1001, 1, 5, "running")
        achieved, problems = load.evaluate_incident_load(first, second, expected_users=5,
                                                        generator_throttle=self.generator())
        self.assertEqual(problems, ())
        self.assertFalse(load.evaluate_offered_load(achieved, load.LoadBand("host", 100, .2, 5)).scored)
        for changed in (
            load.LoadReading(60, 1000, 0, 5, "running"),
            load.LoadReading(60, 1001, 0, 4, "running"),
            load.LoadReading(60, 1001, 0, 5, "stopped"),
            load.LoadReading(1, 1001, 0, 5, "running"),
        ):
            self.assertTrue(load.evaluate_incident_load(first, changed, expected_users=5,
                                                        generator_throttle=self.generator())[1])
        self.assertTrue(load.evaluate_incident_load(first, second, expected_users=5,
                                                    generator_throttle=self.generator(throttled=1))[1])
        self.assertTrue(load.evaluate_incident_load(first, second, expected_users=5,
                                                    generator_throttle=cpu.ThrottleVerdict(()))[1])
        with self.assertRaises(load.OfferedLoadError):
            load.achieved_between(load.LoadReading(0, 100, 5, 5, "running"),
                                  load.LoadReading(60, 101, 0, 5, "running"))

    def test_empty_readiness_and_unexpected_flags_cannot_pass(self):
        with patch.object(readiness, "build_probes", return_value=()):
            self.assertFalse(readiness.evaluate_all(readiness.ProbeContext("p", "file", {}))[1])
        with patch.object(readiness, "read_flag_state", return_value=(True, {"known": "off"}, "")):
            self.assertTrue(readiness.evaluate_flag_gate("p", {"known": "off"}).baseline_clean)
            self.assertFalse(readiness.evaluate_flag_gate("p", {}).baseline_clean)
        with patch.object(readiness, "read_flag_state", return_value=(True, {"known": "off", "extra": "on"}, "")):
            self.assertFalse(readiness.evaluate_flag_gate("p", {"known": "off"}).baseline_clean)


class ShopLifecycleTests(unittest.TestCase):
    def test_deployment_checks_compare_observed_state(self):
        config = {
            "services": {
                name: {"image": "image@sha256:" + "a" * 64,
                       "environment": {"SET": "yes", "UNSET": None},
                       "networks": {"default": {}},
                       "deploy": {"resources": {"limits": {"cpus": 8, "memory": 1024}}}}
                for name in ("frontend-proxy", "backend")
            },
        }
        for fault in (None, "image", "cpu", "memory", "set-env", "unset-env", "network", "internal",
                      "egress-open", "egress-probe-broken", "positive-control"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                env = environment.ShopEnvironment(ROOT, Path(temp))
                env.project = Mock()
                env.project.container_id.side_effect = lambda name: name
                env.model = environment.parse_compose_config(json.dumps(config))
                env.declared_environment = {name: spec["environment"] for name, spec in config["services"].items()}
                def docker(*args, **kwargs):
                    if args[0] == "inspect":
                        name = args[1]
                        value = {
                            "Image": "actual", "HostConfig": {"NanoCpus": 8_000_000_000, "Memory": 1024},
                            "Config": {"Env": ["SET=yes"]},
                            "NetworkSettings": {"Networks": {f"{env.run_id}_default": {}}},
                        }
                        if name == "frontend-proxy":
                            value["NetworkSettings"]["Networks"][f"{env.run_id}_ingress"] = {}
                        if fault == "image": value["Image"] = "wrong"
                        if fault == "cpu": value["HostConfig"]["NanoCpus"] = 0
                        if fault == "memory": value["HostConfig"]["Memory"] = 0
                        if fault == "set-env": value["Config"]["Env"] = []
                        if fault == "unset-env": value["Config"]["Env"].append("UNSET=")
                        if fault == "network": value["NetworkSettings"]["Networks"]["extra"] = {}
                        return completed(json.dumps([value]))
                    if args[0] == "image":
                        return completed('[{"Id":"actual"}]')
                    if args[0] == "network":
                        return completed(json.dumps([{"Name": "default", "Internal": fault != "internal"}]))
                    self.assertEqual(args[0], "run")
                    ingress = "container:frontend-proxy" in args
                    code = 0 if ingress else 7
                    if fault == "egress-open": code = 0
                    if fault == "egress-probe-broken": code = 125
                    if fault == "positive-control": code = 7
                    return completed(code=code)
                with patch.object(environment, "docker", side_effect=docker):
                    if fault:
                        with self.assertRaises(environment.ShopEnvironmentError):
                            env.verify_deployment()
                    else:
                        env.verify_deployment()
                env.destroy()

    def test_cli_is_explicit_about_calibration(self):
        parsed = build_parser().parse_args(["shop", "--calibrate", "--seconds", "30"])
        self.assertTrue(parsed.calibrate)
        self.assertEqual(parsed.seconds, 30)

    def test_failed_start_and_interrupt_always_clean_up_and_persist(self):
        for failure in (RuntimeError("startup failed"), KeyboardInterrupt()):
            with tempfile.TemporaryDirectory() as temp:
                env = environment.ShopEnvironment(ROOT, Path(temp))
                project = Mock()
                project.destroy.return_value = ResidualResources()
                project._run.return_value = completed()
                def fail():
                    env.project = project
                    env.owns_project = True
                    raise failure
                with patch.object(env, "create", side_effect=fail):
                    with self.assertRaises(type(failure)):
                        env.run_healthy(30, calibrate=True)
                project.destroy.assert_called_once()
                result = json.loads((env.run_dir / "environment.json").read_text())
                self.assertEqual(result["status"], "failed")
                self.assertTrue(result["gates"]["cleanup"]["passed"])
                self.assertFalse(env.runtime_dir.exists())

    def test_cleanup_failure_is_not_saved_as_success(self):
        with tempfile.TemporaryDirectory() as temp:
            env = environment.ShopEnvironment(ROOT, Path(temp))
            env.project = Mock()
            env.owns_project = True
            env.project.destroy.return_value = ResidualResources(containers=("leftover",))
            env.record["status"] = "healthy-measured"
            with self.assertRaises(environment.ShopEnvironmentError):
                env.destroy()
            result = json.loads((env.run_dir / "environment.json").read_text())
            self.assertEqual(result["status"], "failed")
            self.assertTrue(env.runtime_dir.exists())

    def test_unowned_project_is_never_destroyed(self):
        with tempfile.TemporaryDirectory() as temp:
            env = environment.ShopEnvironment(ROOT, Path(temp))
            env.project = Mock()
            env.destroy()
            env.project.destroy.assert_not_called()

    def test_plugin_health_requires_actual_success(self):
        for text, code, passing in (
            ('{"status":"OK"}', 0, True), ('{"status":"ERROR"}', 0, False),
            ('{"status":"OK"}', 1, False), ('{"message":"Plugin not found"}', 0, False),
            ("", 0, False), ("[]", 0, False),
        ):
            with tempfile.TemporaryDirectory() as temp:
                env = environment.ShopEnvironment(ROOT, Path(temp))
                with patch.object(environment, "docker", return_value=completed(text, code)):
                    if passing:
                        env.check_plugin()
                    else:
                        with self.assertRaises(environment.ShopEnvironmentError):
                            env.check_plugin()
                env.destroy()

    def test_measurement_positive_and_negative_controls(self):
        for fault in (None, "no-requests", "stopped", "users", "endpoint-failures", "throttle"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                env = environment.ShopEnvironment(ROOT, Path(temp))
                env.host_class = "test-host"
                env.record["render"] = {"loadConfiguration": {"LOCUST_USERS": "5"}}
                first = load.LoadReading(0, 100, 0, 5, "running")
                second = load.LoadReading(60, 100 if fault == "no-requests" else 200,
                                          1 if fault == "endpoint-failures" else 0,
                                          4 if fault == "users" else 5,
                                          "stopped" if fault == "stopped" else "running")
                readings = [
                    {"load-generator": cpu.ThrottleReading("load-generator", 100, 0, 0, 8)},
                    {"load-generator": cpu.ThrottleReading("load-generator", 200, 0, 0, 8)},
                ]
                readings[0]["backend"] = cpu.ThrottleReading("backend", 100, 0, 0, 8)
                readings[1]["backend"] = cpu.ThrottleReading("backend", 200, int(fault == "throttle"), 0, 8)
                with patch.object(environment.time, "sleep"), \
                     patch.object(load, "read_offered_load", side_effect=[first, second]), \
                     patch.object(cpu, "load_fitted_limits", return_value={"limitCores": {"load-generator": 8, "backend": 8}}), \
                     patch.object(cpu, "read_throttling", side_effect=readings):
                    if fault:
                        with self.assertRaises(environment.ShopEnvironmentError):
                            env.measure(60, calibrate=True)
                    else:
                        env.measure(60, calibrate=True)
                        self.assertEqual(env.record["measurement"]["requests"], 100)
                        with patch.object(load, "load_band", return_value=load.LoadBand("host", 100, .2, 5)), \
                             patch.object(load, "read_offered_load", side_effect=[first, second]), \
                             patch.object(cpu, "read_throttling", side_effect=readings):
                            with self.assertRaisesRegex(environment.ShopEnvironmentError, "offered-load-band"):
                                env.measure(60)
                records = [json.loads(line) for line in (env.run_dir / "measurements.jsonl").read_text().splitlines()]
                self.assertEqual(records[0]["name"], "cpu-open")
                env.destroy()

    def test_invalid_duration_cannot_start_a_stack(self):
        for seconds in (0, 29, float("nan"), float("inf")):
            with tempfile.TemporaryDirectory() as temp:
                env = environment.ShopEnvironment(ROOT, Path(temp))
                with patch.object(env, "create") as create:
                    with self.assertRaises(environment.ShopEnvironmentError):
                        env.run_healthy(seconds, calibrate=True)
                    create.assert_not_called()
            with tempfile.TemporaryDirectory() as temp:
                env = environment.ShopEnvironment(ROOT, Path(temp))
                with self.assertRaisesRegex(environment.ShopEnvironmentError, "window"):
                    env.measure(seconds, calibrate=True)
                env.destroy()

    def test_readiness_flags_and_hidden_routes(self):
        for fault in (None, "inventory", "service", "flag", "hidden-route"):
            with self.subTest(fault=fault), tempfile.TemporaryDirectory() as temp:
                env = environment.ShopEnvironment(ROOT, Path(temp))
                env.project = Mock()
                env.model = Mock(services={"backend": {}})
                env.project.endpoint.return_value = "http://127.0.0.1:12345"
                results = [] if fault == "inventory" else [readiness.ProbeResult("backend", "tcp", fault != "service", "control")]
                flags = readiness.FlagState(True, {"flag": "off"}, {"flag": "off"},
                                             ("flag",) if fault == "flag" else (), ())
                with patch.object(readiness, "discover_ports", return_value={}), \
                     patch.object(readiness, "evaluate_all", return_value=(results, fault is None)), \
                     patch.object(readiness, "evaluate_flag_gate", return_value=flags), \
                     patch.object(env, "check_plugin"), \
                     patch.object(environment, "docker", return_value=completed("200" if fault == "hidden-route" else "404")):
                    if fault:
                        with self.assertRaises(environment.ShopEnvironmentError):
                            env.ready(timeout=0)
                    else:
                        env.ready(timeout=0)
                env.destroy()

    def test_incomplete_check_plan_stops_before_deployment_checks(self):
        for checks, complete in (((), True), ((1,), False)):
            with tempfile.TemporaryDirectory() as temp:
                env = environment.ShopEnvironment(ROOT, Path(temp))
                project = Mock()
                project.up.return_value = completed()
                project.assert_absent = Mock()
                project.config_json.return_value = '{"services":{"backend":{"image":"x"}}}'
                project.destroy.return_value = ResidualResources()
                plan = Mock(checks=checks, complete=complete)
                plan.to_dict.return_value = {}
                with patch.object(environment, "observe_host", return_value=HostFacts()), \
                     patch.object(environment, "derive_class_id", return_value="test"), \
                     patch.object(environment, "derive_fingerprint", return_value={}), \
                     patch.object(environment, "render_stack", return_value={}), \
                     patch.object(environment, "ComposeProject", return_value=project), \
                     patch.object(environment, "generate_check_plan", return_value=plan), \
                     patch.object(env, "verify_deployment"):
                    with self.assertRaisesRegex(environment.ShopEnvironmentError, "check-plan"):
                        env.create()
                env.destroy()


if __name__ == "__main__":
    unittest.main()
