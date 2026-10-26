"""Offline fixtures: no subprocess, container engine, cluster or model calls."""

import copy
import json
import os
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from radius_perf_eval import kind_smoke as smoke


NODE = {"items": [{"status": {"conditions": [{"type": "Ready", "status": "True"}],
                              "nodeInfo": {"kubeletVersion": "v1.37.0"}}}]}
IMAGE = {"spec": {"containers": [{"image": smoke.WORK_IMAGE}]},
         "status": {"containerStatuses": [{"ready": True, "imageID": smoke.WORK_IMAGE}]}}
OOM = {"status": {"containerStatuses": [
    {"state": {"terminated": {"reason": "OOMKilled", "exitCode": 137}}}]}}
PORTS = {"NetworkSettings": {"Ports": {"6443/tcp": [{"HostIp": "127.0.0.1"}]}}}
NODE_CONTAINER = {**PORTS, "Image": "node-image",
                  "Mounts": [{"Type": "volume", "Name": "node-volume"}]}
STAT = "usage_usec 100\nnr_periods 10\nnr_throttled 5\nthrottled_usec 100\n"
AFTER = "usage_usec 200\nnr_periods 20\nnr_throttled 8\nthrottled_usec 200\n"
INFO = {"host": {"arch": "arm64", "os": "linux", "kernel": "test",
                "cgroupVersion": "v2", "cgroupManager": "systemd",
                "networkBackend": "netavark", "cpus": 5,
                "memTotal": 3783753728, "memFree": 2811912192,
                "security": {"rootless": False},
                "ociRuntime": {"name": "crun", "version": "test"}},
        "version": {"Version": "6.0.2"}}


class OfflineSmoke(smoke.Smoke):
    """Small command fixture with real orchestration and ownership decisions."""

    def __init__(self, output):
        super().__init__(output)
        self.responses = {}
        self.calls = []
        self.created = False
        self.deleted = False
        self.network_deleted = False
        self.stat_calls = 0

    def run(self, args, **kwargs):
        self.calls.append(args)
        key = " ".join(args)
        for match, value in self.responses.items():
            if match in key:
                return value(self) if callable(value) else value
        if args[:2] == [smoke.PODMAN, "--version"]:
            return "podman version 6.0.2"
        if args[:2] == [smoke.KIND, "version"]:
            return "kind v0.33.0 go1.27.1 darwin/arm64"
        if "version --client" in key:
            return json.dumps({"clientVersion": {"gitVersion": "v1.37.1"}})
        if "connection list" in key:
            return json.dumps([{"Name": smoke.CONNECTION, "Default": True,
                                "URI": "ssh://root@127.0.0.1:52746/run/podman/podman.sock"}])
        if "info --format" in key:
            return json.dumps(INFO)
        if "/proc/meminfo" in key:
            return "MemAvailable: 2746008 kB\n"
        if "get clusters" in key:
            return ""
        if "network create" in key:
            return self.name
        if "network inspect" in key:
            return json.dumps([{"id": "net-id", "labels": {"radius-smoke": self.name}}])
        if "network rm" in key:
            self.network_deleted = True
            return ""
        if "network ls" in key:
            return "[]"
        if "create cluster" in key:
            self.created = True
            return ""
        if "delete cluster" in key:
            self.deleted = True
            return ""
        if "ps -a" in key:
            return json.dumps([{"Id": "node-id", "Names": [self.name + "-control-plane"]}]
                              if self.created and not self.deleted else [])
        if " inspect node-id" in key:
            return json.dumps([NODE_CONTAINER])
        if "image inspect" in key:
            return '[{"Id":"node-image","Architecture":"arm64"}]'
        if "get nodes" in key:
            return json.dumps(NODE)
        if "get pod/web" in key:
            return json.dumps(IMAGE)
        if "get pvc/data" in key:
            return json.dumps({"status": {"phase": "Bound"}, "spec": {"volumeName": "pv-id"}})
        if "get pod/memory" in key:
            return json.dumps(OOM)
        if "logs memory" in key:
            return "33554432\nKilled\n"
        if "logs reader" in key or "wget -qO-" in key:
            return self.name
        if "nslookup" in key:
            return "Name: web.smoke.svc.cluster.local\nAddress: 10.0.0.1\n"
        if "cpu.max" in key:
            return "10000 100000"
        if "memory.max" in key:
            return "67108864"
        if "cpu.stat" in key:
            self.stat_calls += 1
            return STAT if self.stat_calls == 1 else AFTER
        return "[]"

    def forward(self):
        self.record["portForward"] = {"status": "fixture-not-live"}


class KindSmokeTests(unittest.TestCase):
    def test_validators_accept_positive_evidence(self):
        smoke.ready_nodes(NODE)
        smoke.image_ready(IMAGE)
        smoke.oom(OOM)
        smoke.loopback_ports(PORTS)
        self.assertEqual(smoke.limits("10000 100000", "67108864", STAT, AFTER)["nr_throttled"], 3)
        smoke.cleaned([{"Id": "old"}], [{"Id": "owned"}], [{"Id": "old"}])

    def test_invalid_evidence(self):
        bad_node = copy.deepcopy(NODE)
        bad_node["items"][0]["status"]["conditions"][0]["status"] = "False"
        bad_version = copy.deepcopy(NODE)
        bad_version["items"][0]["status"]["nodeInfo"]["kubeletVersion"] = "v1.36.0"
        cases = [
            (smoke.ready_nodes, [{"items": []}]),
            (smoke.ready_nodes, [bad_node]),
            (smoke.ready_nodes, [bad_version]),
            (smoke.limits, ["20000 100000", "67108864", STAT, AFTER]),
            (smoke.limits, ["10000 100000", "134217728", STAT, AFTER]),
            (smoke.limits, ["10000 100000", "67108864", STAT, STAT]),
            (smoke.oom, [{"status": {"containerStatuses": []}}]),
            (smoke.oom, [{"status": {"containerStatuses": OOM["status"]["containerStatuses"] * 2}}]),
            (smoke.oom, [{"status": {"containerStatuses": [
                {"state": {"terminated": {"reason": "Error", "exitCode": 1}}}]}}]),
            (smoke.image_ready, [{"status": {"containerStatuses": []}}]),
            (smoke.image_ready, [{**IMAGE, "status": {"containerStatuses": IMAGE["status"]["containerStatuses"] * 2}}]),
            (smoke.image_ready, [{**IMAGE, "status": {"containerStatuses": [{"ready": True, "imageID": ""}]}}]),
            (smoke.image_ready, [{**IMAGE, "spec": {"containers": [{"image": "busybox:latest"}]}}]),
            (smoke.loopback_ports, [{"NetworkSettings": {"Ports": {}}}]),
            (smoke.loopback_ports, [{"NetworkSettings": {"Ports": {"6443/tcp": [{"HostIp": "0.0.0.0"}]}}}]),
            (smoke.cleaned, [[], [], []]),
            (smoke.cleaned, [[{"Id": "old"}], [{"Id": "owned"}], []]),
            (smoke.cleaned, [[{"Id": "owned"}], [{"Id": "owned"}], [{"Id": "owned"}]]),
        ]
        for function, args in cases:
            with self.subTest(function=function.__name__, args=args):
                with self.assertRaises(smoke.RuntimePolicyError):
                    function(*args)

    def test_lifecycle_and_isolation(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {
            "DOCKER_HOST": "unix:///nonexistent/docker.sock", "CONTAINER_HOST": "unrelated",
            "KUBECONFIG": "/unrelated", "KIND_EXPERIMENTAL_PROVIDER": "docker"
        }):
            runner = OfflineSmoke(Path(tmp) / "run")
            snapshots = []
            save = runner.save
            def capture():
                snapshots.append(copy.deepcopy(runner.record))
                save()
            with patch.object(runner, "save", side_effect=capture):
                result = runner.execute()
            self.assertEqual(result["status"], "passed", result)
            self.assertEqual(result["cleanup"], "verified")
            self.assertFalse(result["eligibleForTrials"])
            self.assertTrue(runner.network_deleted)
            self.assertNotIn("DOCKER_HOST", runner.env)
            self.assertNotIn("CONTAINER_HOST", runner.env)
            self.assertEqual(runner.env["KIND_EXPERIMENTAL_PROVIDER"], "podman")
            self.assertEqual(runner.env["CONTAINER_CONNECTION"], smoke.CONNECTION)
            self.assertEqual(runner.env["KUBECONFIG"], str(runner.output / "kubeconfig"))
            self.assertEqual(json.loads((runner.output / "result.json").read_text()), result)
            self.assertTrue(snapshots)
            self.assertTrue(all(s["status"] != "passed" for s in snapshots[:-1]))
            with self.assertRaises(FileExistsError):
                OfflineSmoke(runner.output)

    def test_corrupt_lifecycle_evidence(self):
        cases = [
            ("podman --version", "podman version 7.0"),
            ("kind version", "kind v0.34.0 test"),
            ("version --client", '{"clientVersion":{"gitVersion":"v1.36.1"}}'),
            ("connection list", "[]"),
            ("info --format", json.dumps({**INFO, "host": {**INFO["host"], "arch": "amd64"}})),
            ("/proc/meminfo", "MemAvailable: 1024 kB\n"),
            ("get clusters", lambda s: s.name),
            ("network create", ""),
            ("--filter label=io.x-k8s.kind.cluster=", "[]"),
            ("--filter label=io.x-k8s.kind.cluster=", '[{"Id":"node-id"},{"Id":"second-node"}]'),
            (" inspect node-id", json.dumps([{**NODE_CONTAINER, "Mounts": []}])),
            ("image inspect", '[{"Id":"wrong-image","Architecture":"arm64"}]'),
            ("volume ls", lambda s: '[{"Name":"node-volume"}]' if s.deleted else "[]"),
            ("get pvc/data", '{"status":{"phase":"Pending"},"spec":{"volumeName":""}}'),
            ("logs reader", "wrong-volume"),
            ("nslookup", "wrong-name"),
            ("wget -qO-", "wrong-service"),
            ("logs memory", "wrong-limit"),
            ("network inspect", '{"bad":"shape"}'),
            ("network inspect", lambda s: json.dumps([{"id": "foreign-id" if s.deleted else "net-id",
                                                       "labels": {"radius-smoke": s.name}}])),
            ("network ls", lambda s: json.dumps([{"name": "foreign", "id": "net-id"}])
             if s.network_deleted else "[]"),
        ]
        for match, value in cases:
            with self.subTest(match=match), tempfile.TemporaryDirectory() as tmp:
                runner = OfflineSmoke(Path(tmp) / "run")
                runner.responses[match] = value
                result = runner.execute()
                self.assertEqual(result["status"], "failed", result)
                self.assertTrue(result["errors"])

    def test_diagnostic_failure_does_not_skip_cleanup(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = OfflineSmoke(Path(tmp) / "run")
            def failed(_):
                raise smoke.RuntimePolicyError("planted diagnostic timeout")
            runner.responses["get pods -A"] = failed
            result = runner.execute()
            self.assertEqual(result["status"], "failed")
            self.assertIn("planted diagnostic timeout", result["errors"][0])
            self.assertEqual(result["cleanup"], "verified")
            self.assertTrue(runner.deleted)
            self.assertTrue(runner.network_deleted)

    def test_raw_command_before_failure(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner = smoke.Smoke(Path(tmp) / "run")
            with patch.object(smoke.subprocess, "run") as run:
                run.return_value.returncode = 17
                with self.assertRaises(smoke.RuntimePolicyError):
                    runner.run(["nonexistent-fixture-command"])
            record = json.loads((runner.output / "result.json").read_text())
            self.assertEqual(record["commands"][0]["returncode"], 17)
            self.assertTrue((runner.output / "000.stderr").exists())

    def test_forward_positive_and_negative_controls(self):
        for failure in (None, "payload", "listener", "process"):
            with self.subTest(failure=failure), tempfile.TemporaryDirectory() as tmp:
                runner = smoke.Smoke(Path(tmp) / "run")
                with patch.object(smoke.socket, "socket") as sock, \
                        patch.object(smoke.subprocess, "Popen") as popen, \
                        patch.object(smoke.urllib.request, "urlopen") as request, \
                        patch.object(smoke.time, "sleep"):
                    socket_instance = sock.return_value.__enter__.return_value
                    socket_instance.getsockname.return_value = ("127.0.0.1", 12345)
                    socket_instance.connect_ex.return_value = 0 if failure == "listener" else 61
                    process = popen.return_value
                    process.poll.return_value = 1 if failure == "process" else None
                    process.returncode = 0
                    def started(*args, **kwargs):
                        if failure != "process":
                            kwargs["stdout"].write("Forwarding from 127.0.0.1:12345 -> 8080\n")
                            kwargs["stdout"].flush()
                        return process
                    popen.side_effect = started
                    request.return_value.__enter__.return_value.read.return_value = (
                        b"wrong" if failure == "payload" else runner.name.encode())
                    if failure:
                        with self.assertRaises(smoke.RuntimePolicyError):
                            runner.forward()
                    else:
                        runner.forward()
                        self.assertIn("--address=127.0.0.1", popen.call_args.args[0])
                        self.assertEqual(runner.record["portForward"]["status"], "observed")
                    process.terminate.assert_called_once()
                    process.wait.assert_called_once()

    def test_workloads_bounded_and_socket_free(self):
        text = json.dumps(smoke.manifests("owned-token"))
        self.assertNotIn("hostPath", text)
        self.assertNotIn("privileged", text)
        self.assertNotIn("docker.sock", text)
        self.assertNotIn("podman.sock", text)
        pods = [p for p in smoke.manifests("owned-token")["items"] if p["kind"] == "Pod"]
        self.assertTrue(pods)
        for pod in pods:
            self.assertFalse(pod["spec"]["automountServiceAccountToken"])
            self.assertEqual(pod["spec"]["containers"][0]["resources"]["limits"]["cpu"], "100m")
            self.assertEqual(pod["spec"]["containers"][0]["image"], smoke.WORK_IMAGE)

    def test_reader_waits_for_writer(self):
        pods = smoke.manifests("owned-token")["items"]
        reader = next(p for p in pods if p["metadata"]["name"] == "reader")
        command = reader["spec"]["containers"][0]["command"][-1]
        with tempfile.TemporaryDirectory() as tmp:
            command = command.replace("/data/", tmp + "/")
            command = command.replace("sleep 1", "sleep 0.02").replace("seq 1 60", "seq 1 10")
            writer = threading.Timer(0.05, lambda: (Path(tmp) / "index.html").write_text("owned-token"))
            writer.start()
            try:
                result = smoke.subprocess.run(["/bin/sh", "-ec", command],
                                              text=True, capture_output=True, timeout=5)
            finally:
                writer.join()
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "owned-token")
            (Path(tmp) / "index.html").unlink()
            result = smoke.subprocess.run(["/bin/sh", "-ec", command],
                                          text=True, capture_output=True, timeout=5)
            self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
