"""An evidence-producing Astronomy Shop environment, without model calls."""

from __future__ import annotations

import json
import math
import os
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

from . import astronomy_shop as shop, cpu_limits, offered_load, shop_readiness
from .checks import generate_check_plan, parse_compose_config
from .compose import ComposeProject
from .docker_cli import DockerError, docker
from .hostclass import HostFacts, derive_class_id, derive_fingerprint, observe_host
from .shop_assets import digest, mount_assets, verify_assets


class ShopEnvironmentError(RuntimeError):
    """A required environment gate has not passed."""


def render_stack(
    repo_root: Path, out_path: Path, facts: HostFacts, runtime_dir: Path,
) -> dict[str, Any]:
    verify_assets(repo_root)
    paths = shop.compose_file_paths(repo_root)
    keys = {
        line.split("=", 1)[0] for line in (paths[0].parent / ".env").read_text().splitlines()
        if line and not line.startswith("#") and "=" in line
    }
    env = {key: value for key, value in os.environ.items()
           if key not in keys and not key.startswith("COMPOSE_")}
    env.update(DOCKER_SOCK="/var/run/docker.sock", HOST_FILESYSTEM="/")
    args = ["docker", "compose", "--env-file", str(paths[0].parent / ".env")]
    for path in paths:
        args += ["--file", str(path)]
    rendered = subprocess.run(args + ["config", "--format", "json"], env=env, timeout=120,
                              capture_output=True, text=True)
    if rendered.returncode != 0:
        raise DockerError(f"Shop Compose rendering failed: {rendered.stderr}")
    config = json.loads(rendered.stdout)
    report = shop.apply_transforms(config, derive_class_id(facts), runtime_dir=runtime_dir)
    manifest = json.loads((repo_root / "benchmark/apps/astronomy-shop/image-digests.json").read_text())
    if manifest["manifestHash"] != digest(json.dumps(manifest["images"], sort_keys=True, separators=(",", ":")).encode()):
        raise ShopEnvironmentError("image manifest hash differs from its contents")
    if set(config["services"]) != set(manifest["images"]):
        raise ShopEnvironmentError("pinned images do not match the Compose service inventory")
    for service, reference in manifest["images"].items():
        if "@sha256:" not in reference:
            raise ShopEnvironmentError(f"image for {service} is not digest-pinned")
        config["services"][service]["image"] = reference
        config["services"][service].pop("build", None)
    assets = mount_assets(config, repo_root, runtime_dir, facts.docker_arch or "")
    config.pop("name", None)
    out_path.write_text(json.dumps(config, indent=2) + "\n")
    return {
        "transforms": report.to_dict(), "startupAssets": assets,
        "manifestHash": manifest["manifestHash"], "services": sorted(config["services"]),
        "loadConfiguration": config["services"]["load-generator"]["environment"],
    }


class ShopEnvironment:
    def __init__(self, repo_root: Path, results_dir: Path):
        self.repo_root = repo_root.resolve()
        self.run_id = "radius-eval-shop-" + uuid.uuid4().hex[:12]
        self.run_dir = results_dir.resolve() / self.run_id
        self.run_dir.mkdir(parents=True, exist_ok=False)
        self.runtime_dir = self.run_dir / "runtime"
        self.runtime_dir.mkdir()
        self.stack = self.run_dir / "stack.json"
        self.project: ComposeProject | None = None
        self.owns_project = False
        self.record: dict[str, Any] = {
            "schemaVersion": "radius-shop-environment-v1", "runId": self.run_id,
            "status": "running", "gates": {}, "cleanup": None,
        }
        self.save()

    def save(self) -> None:
        temporary = self.run_dir / "environment.tmp"
        temporary.write_text(json.dumps(self.record, indent=2) + "\n")
        temporary.replace(self.run_dir / "environment.json")

    def evidence(self, name: str, value: Any) -> None:
        with (self.run_dir / "measurements.jsonl").open("a") as stream:
            stream.write(json.dumps({"at": time.time(), "name": name, "value": value}) + "\n")
            stream.flush()
            os.fsync(stream.fileno())

    def gate(self, name: str, passed: bool, detail: Any) -> None:
        self.evidence(name, detail)
        self.record["gates"][name] = {"passed": passed, "detail": detail}
        self.save()
        if not passed:
            raise ShopEnvironmentError(f"{name} failed; see {self.run_dir}")

    def create(self) -> None:
        facts = observe_host()
        self.host_class = derive_class_id(facts)
        self.record.update(hostClass=self.host_class, hostFingerprint=derive_fingerprint(facts),
                           host=facts.to_dict())
        self.record["render"] = render_stack(self.repo_root, self.stack, facts, self.runtime_dir)
        self.project = ComposeProject(self.run_id, env={}, files=[self.stack])
        self.project.assert_absent()
        self.owns_project = True
        self.save()
        started = self.project.up(wait_timeout=900)
        self.evidence("compose-up", {"stdout": started.stdout, "stderr": started.stderr})
        normalized = self.project.config_json()
        self.declared_environment = {
            name: spec.get("environment") or {}
            for name, spec in json.loads(normalized)["services"].items()
        }
        self.model = parse_compose_config(normalized)
        self.plan = generate_check_plan(
            self.model, readiness_probes={probe.service for probe in shop_readiness.build_probes()},
            egress_exceptions={"frontend-proxy": "Only ingress is routed; application backends are internal."},
        )
        self.gate("check-plan", bool(self.plan.checks) and self.plan.complete, self.plan.to_dict())
        self.verify_deployment()

    def verify_deployment(self) -> None:
        assert self.project is not None
        containers = {name: self.project.container_id(name) for name in self.model.services}
        for name, container in containers.items():
            declared = self.model.services[name]
            observed = json.loads(docker("inspect", container).stdout)[0]
            image = json.loads(docker("image", "inspect", declared.image).stdout)[0]["Id"]
            self.gate(f"image-pinned:{name}", observed["Image"] == image,
                      {"expected": image, "observed": observed["Image"]})
            limits = observed["HostConfig"]
            self.gate(f"resource-limits:{name}",
                      limits["NanoCpus"] == declared.nano_cpus and limits["Memory"] == declared.memory_bytes,
                      {"nanoCpus": limits["NanoCpus"], "memory": limits["Memory"]})
            actual_env = dict(item.split("=", 1) for item in observed["Config"]["Env"] or [] if "=" in item)
            expected_env = self.declared_environment[name]
            mismatched = [
                key for key, value in expected_env.items()
                if (key in actual_env if value is None else actual_env.get(key) != str(value))
            ]
            self.gate(f"environment-variables:{name}", not mismatched,
                      {"examined": sorted(expected_env), "mismatched": mismatched,
                       "unset": sorted(key for key, value in expected_env.items() if value is None)})
            networks = observed["NetworkSettings"]["Networks"]
            expected = {f"{self.run_id}_default"}
            if name == "frontend-proxy":
                expected.add(f"{self.run_id}_ingress")
            self.gate(f"network-attachment:{name}", set(networks) == expected,
                      {"expected": sorted(expected), "observed": sorted(networks)})
        internal = json.loads(docker("network", "inspect", f"{self.run_id}_default").stdout)[0]
        self.gate("internal-network", internal["Internal"] is True,
                  {"name": internal["Name"], "internal": internal["Internal"]})
        # The same image and destination must work on ingress before a failure
        # in a backend namespace can count as an egress block.
        for name in ["frontend-proxy", *sorted(set(containers) - {"frontend-proxy"}), "frontend-proxy"]:
            result = docker(
                "run", "--rm", "--network", f"container:{containers[name]}",
                "--label", f"com.docker.compose.project={self.run_id}",
                shop_readiness.PROBE_IMAGE, "--silent", "--show-error", "--output", "/dev/null",
                "--connect-timeout", "3", "--max-time", "5", "http://1.1.1.1",
                check=False, timeout=30,
            )
            expected = result.returncode == 0 if name == "frontend-proxy" else result.returncode in (7, 28)
            self.gate(f"egress:{name}", expected,
                      {"exitCode": result.returncode, "stderr": result.stderr,
                       "destination": "http://1.1.1.1", "positiveControl": name == "frontend-proxy"})

    def ready(self, timeout: float = 180) -> None:
        assert self.project is not None
        deadline = time.monotonic() + timeout
        while True:
            context = shop_readiness.ProbeContext(
                self.run_id, str(self.stack), shop_readiness.discover_ports(self.run_id, str(self.stack)),
            )
            results, passed = shop_readiness.evaluate_all(context)
            self.evidence("readiness-sweep", [result.to_dict() for result in results])
            if passed or time.monotonic() >= deadline:
                break
            time.sleep(5)
        self.gate("readiness-inventory",
                  {result.service for result in results} == set(self.model.services),
                  [result.to_dict() for result in results])
        for result in results:
            self.gate(f"readiness:{result.service}", result.ready, result.to_dict())
        expected = shop.default_off_flags(shop.declared_flags(self.repo_root))
        flags = shop_readiness.evaluate_flag_gate(self.run_id, expected)
        self.gate("flags-baseline", flags.baseline_clean, flags.to_dict())
        url = self.project.endpoint("frontend-proxy", 8080)
        for path in ("/feature", "/flagservice/", "/loadgen/", "/opamp/"):
            result = docker("run", "--rm", "--network", f"{self.run_id}_default",
                            "--label", f"com.docker.compose.project={self.run_id}",
                            shop_readiness.PROBE_IMAGE, "-s", "-o", "/dev/null", "-w", "%{http_code}",
                            "--max-time", "5", f"http://frontend-proxy:8080{path}", check=False)
            self.gate(f"hidden-route:{path}", result.returncode == 0 and result.stdout == "404",
                      {"status": result.stdout, "stderr": result.stderr})
        self.record["ingress"] = url
        self.check_plugin()

    def check_plugin(self) -> None:
        result = docker(
            "run", "--rm", "--network", f"{self.run_id}_default",
            "--label", f"com.docker.compose.project={self.run_id}", shop_readiness.PROBE_IMAGE,
            "--fail-with-body", "--silent", "--show-error", "--max-time", "10",
            "http://grafana:3000/api/datasources/uid/webstore-logs/health", check=False,
        )
        try:
            payload = json.loads(result.stdout)
        except json.JSONDecodeError:
            payload = {"error": result.stderr or result.stdout}
        healthy = (
            result.returncode == 0 and isinstance(payload, dict)
            and payload.get("status") == "OK"
            and payload.get("message") == "Index OK. Time field name OK."
        )
        if not healthy:
            for name, path in (
                ("indices", "_cat/indices?format=json"),
                ("mapping", "otel*/_mapping"),
                ("templates", "_index_template"),
            ):
                diagnostic = docker(
                    "run", "--rm", "--network", f"{self.run_id}_default",
                    "--label", f"com.docker.compose.project={self.run_id}",
                    shop_readiness.PROBE_IMAGE, "--fail-with-body", "--silent",
                    "--show-error", "--max-time", "10", f"http://opensearch:9200/{path}",
                    check=False,
                )
                self.evidence(f"opensearch-{name}", {
                    "exitCode": diagnostic.returncode, "stdout": diagnostic.stdout, "stderr": diagnostic.stderr,
                })
        self.gate("opensearch-plugin", healthy, payload)

    def measure(self, seconds: float, *, calibrate: bool = False) -> dict[str, Any]:
        if not math.isfinite(seconds) or seconds < offered_load.MIN_WINDOW_SECONDS:
            raise ShopEnvironmentError("measurement window must be at least 30 seconds")
        band = None if calibrate else offered_load.load_band(self.repo_root, self.host_class)
        expected = cpu_limits.load_fitted_limits(self.repo_root, self.host_class)["limitCores"]
        opened_cpu = cpu_limits.read_throttling(self.run_id)
        self.evidence("cpu-open", {name: value.to_dict() for name, value in opened_cpu.items()})
        first = offered_load.read_offered_load(self.run_id, str(self.stack), raw_path=self.run_dir / "load-open.raw")
        self.evidence("load-open", first.to_dict())
        time.sleep(seconds)
        second = offered_load.read_offered_load(self.run_id, str(self.stack), raw_path=self.run_dir / "load-close.raw")
        self.evidence("load-close", second.to_dict())
        closed_cpu = cpu_limits.read_throttling(self.run_id)
        self.evidence("cpu-close", {name: value.to_dict() for name, value in closed_cpu.items()})
        throttle = cpu_limits.verdict_from_readings(opened_cpu, closed_cpu, expected)
        self.gate("healthy-throttling", throttle.accepted, throttle.to_dict())
        achieved = offered_load.achieved_between(first, second)
        users = int(self.record["render"]["loadConfiguration"]["LOCUST_USERS"])
        generator = cpu_limits.verdict_from_readings(
            {offered_load.LOAD_SERVICE: opened_cpu[offered_load.LOAD_SERVICE]},
            {offered_load.LOAD_SERVICE: closed_cpu[offered_load.LOAD_SERVICE]},
            {offered_load.LOAD_SERVICE: expected[offered_load.LOAD_SERVICE]},
        )
        _, problems = offered_load.evaluate_incident_load(first, second, expected_users=users,
                                                         generator_throttle=generator)
        self.gate("load-production", not problems, {"achieved": achieved.to_dict(), "problems": problems})
        self.gate("healthy-endpoints", achieved.failures == 0, achieved.to_dict())
        if band is not None:
            verdict = offered_load.evaluate_offered_load(achieved, band)
            self.gate("offered-load-band", verdict.scored, verdict.to_dict())
        self.record["measurement"] = achieved.to_dict()
        self.record["calibrationOnly"] = calibrate
        self.save()
        return achieved.to_dict()

    def destroy(self) -> None:
        try:
            if self.project is not None and self.owns_project:
                residue = self.project.destroy()
                self.record["cleanup"] = residue.to_dict()
                self.gate("cleanup", residue.clean, residue.to_dict())
            else:
                self.record["cleanup"] = {"notStarted": True}
            shutil.rmtree(self.runtime_dir)
        except BaseException as error:
            self.record["status"] = "failed"
            self.record["cleanupError"] = f"{type(error).__name__}: {error}"
            raise
        finally:
            self.save()

    def run_healthy(self, seconds: float, *, calibrate: bool = False) -> dict[str, Any]:
        try:
            if not math.isfinite(seconds) or seconds < offered_load.MIN_WINDOW_SECONDS:
                raise ShopEnvironmentError("measurement window must be finite and at least 30 seconds")
            if not calibrate:
                offered_load.load_band(self.repo_root, derive_class_id(observe_host()))
            self.create()
            self.ready()
            self.measure(seconds, calibrate=calibrate)
            self.record["status"] = "calibration-measured" if calibrate else "healthy-measured"
        except BaseException as error:
            self.record["status"] = "failed"
            self.record["error"] = f"{type(error).__name__}: {error}"
            if self.project is not None and self.owns_project:
                try:
                    result = self.project._run("logs", "--no-color", "--tail", "150", check=False)
                    (self.run_dir / "failure-logs.txt").write_text(result.stdout + result.stderr)
                    if result.returncode:
                        self.record["logError"] = result.stderr
                except (DockerError, OSError) as log_error:
                    self.record["logError"] = str(log_error)
            raise
        finally:
            self.save()
            self.destroy()
        return self.record
