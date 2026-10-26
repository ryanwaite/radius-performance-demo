"""Bounded local Kind capability probe, not a benchmark runtime or qualification."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

from .podman_runtime import RuntimePolicyError, engine_identity, require


NODE_IMAGE = "docker.io/kindest/node:v1.37.0@sha256:a1ed56cfb0e7b93589bdf97c8cd566405a265939e3620fc4f5de89adff580ae5"
WORK_IMAGE = "docker.io/library/busybox@sha256:bdf57e528e45e4433820e045b29b4597825a1c9e38353532d90a01445013f82e"
PODMAN = "/opt/podman/bin/podman"
KIND = "/opt/homebrew/bin/kind"
KUBECTL = "/opt/homebrew/bin/kubectl"
CONNECTION = "podman-machine-default-root"


def ready_nodes(document: dict) -> None:
    items = document["items"]
    require(len(items) == 1, "expected one observed node")
    require(all(any(c["type"] == "Ready" and c["status"] == "True"
                    for c in node["status"]["conditions"]) for node in items),
            "node readiness evidence missing")
    require(all(node["status"]["nodeInfo"]["kubeletVersion"] == "v1.37.0"
                for node in items), "unexpected Kubernetes version")


def limits(cpu: str, memory: str, before: str, after: str) -> dict:
    quota, period = map(int, cpu.split())
    require(quota > 0 and period > 0 and quota / period == 0.1,
            "observed CPU quota is not 100m")
    require(int(memory) == 64 * 1024 * 1024, "observed memory limit is not 64Mi")
    first = dict(line.split() for line in before.splitlines())
    last = dict(line.split() for line in after.splitlines())
    delta = {key: int(last[key]) - int(first[key])
             for key in ("usage_usec", "nr_periods", "nr_throttled", "throttled_usec")}
    require(all(value > 0 for value in delta.values()), "CPU load/throttling evidence missing")
    return delta


def oom(document: dict) -> None:
    statuses = document["status"]["containerStatuses"]
    require(len(statuses) == 1, "missing memory-control container")
    state = statuses[0]["state"]["terminated"]
    require(state["reason"] == "OOMKilled" and state["exitCode"] == 137,
            "memory positive control was not killed by its cgroup")


def image_ready(document: dict) -> None:
    statuses = document["status"]["containerStatuses"]
    require(len(statuses) == 1 and statuses[0]["ready"] is True,
            "missing ready diagnostic image")
    require(statuses[0]["imageID"] == WORK_IMAGE, "runtime image digest differs from pin")
    require(document["spec"]["containers"][0]["image"] == WORK_IMAGE,
            "workload image differs from pin")


def loopback_ports(document: dict) -> None:
    ports = document["NetworkSettings"]["Ports"]
    bindings = [binding for entries in ports.values() for binding in (entries or [])]
    require(bool(bindings), "missing published-port evidence")
    require(all(b["HostIp"] == "127.0.0.1" for b in bindings),
            "cluster publishes a non-loopback port")


def inventory_ids(items: list) -> set[str]:
    return {item["Id"] for item in items}


def cleaned(before: list, owned: list, after: list) -> None:
    require(bool(owned), "cleanup lacks positive owned-resource inventory")
    require(inventory_ids(after) == inventory_ids(before),
            "container inventory changed beyond owned cleanup")
    require(inventory_ids(owned).isdisjoint(inventory_ids(after)),
            "owned containers remain")


def manifests(token: str) -> dict:
    def pod(name: str, command: list[str], memory: str = "64Mi") -> dict:
        return {
            "apiVersion": "v1", "kind": "Pod",
            "metadata": {"name": name, "namespace": "smoke", "labels": {"app": name}},
            "spec": {"restartPolicy": "Never", "automountServiceAccountToken": False,
                     "containers": [{"name": name, "image": WORK_IMAGE,
                                     "command": command,
                                     "resources": {"requests": {"cpu": "50m", "memory": "16Mi"},
                                                   "limits": {"cpu": "100m", "memory": memory}},
                                     "volumeMounts": [{"name": "data", "mountPath": "/data"}]}],
                     "volumes": [{"name": "data", "emptyDir": {}}]},
        }
    server = pod("web", ["sh", "-ec", f"echo {token} > /data/index.html; httpd -f -p 8080 -h /data"])
    # A disk-backed PVC is exercised across separate pods, not only in one filesystem.
    server["spec"]["volumes"] = [{"name": "data", "persistentVolumeClaim": {"claimName": "data"}}]
    reader = pod("reader", ["sh", "-ec",
                           "for i in $(seq 1 60); do "
                           "if test -s /data/index.html; then cat /data/index.html; exit 0; fi; "
                           "sleep 1; done; exit 1"])
    reader["spec"]["volumes"] = server["spec"]["volumes"]
    memory = pod("memory", ["sh", "-ec",
                           "cat /sys/fs/cgroup/memory.max; "
                           "exec dd if=/dev/zero of=/data/fill bs=1M count=96"], "32Mi")
    memory["spec"]["volumes"] = [{"name": "data", "emptyDir": {"medium": "Memory"}}]
    return {"apiVersion": "v1", "kind": "List", "items": [
        {"apiVersion": "v1", "kind": "Namespace", "metadata": {"name": "smoke"}},
        {"apiVersion": "v1", "kind": "PersistentVolumeClaim",
         "metadata": {"name": "data", "namespace": "smoke"},
         "spec": {"accessModes": ["ReadWriteOnce"], "resources": {"requests": {"storage": "64Mi"}}}},
        server,
        {"apiVersion": "v1", "kind": "Service", "metadata": {"name": "web", "namespace": "smoke"},
         "spec": {"selector": {"app": "web"}, "ports": [{"port": 8080, "targetPort": 8080}]}},
        reader, memory,
    ]}


class Smoke:
    def __init__(self, output: Path):
        output.mkdir(parents=True, exist_ok=False)
        self.output = output.resolve()
        self.name = "radius-smoke-" + uuid.uuid4().hex[:12]
        self.env = {k: v for k, v in os.environ.items()
                    if not k.startswith(("DOCKER_", "COMPOSE_", "CONTAINER_", "PODMAN_", "KIND_", "KUBE"))}
        self.env.update(PATH="/opt/podman/bin:/opt/homebrew/bin:/usr/bin:/bin",
                        KIND_EXPERIMENTAL_PROVIDER="podman",
                        KIND_EXPERIMENTAL_PODMAN_NETWORK=self.name,
                        CONTAINER_CONNECTION=CONNECTION,
                        KUBECONFIG=str(self.output / "kubeconfig"))
        self.record = {"schemaVersion": "radius-kind-smoke-v1", "name": self.name,
                       "connection": CONNECTION, "status": "incomplete", "eligibleForTrials": False,
                       "smokeSourceSha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                       "pythonVersion": sys.version,
                       "nodeImage": NODE_IMAGE, "workImage": WORK_IMAGE,
                       "cleanup": "not-created", "commands": [], "errors": [],
                       "unverified": ["benchmark-egress", "sandbox", "Shop-Radius-capacity",
                                      "Shop-telemetry-parity", "host-qualification"]}
        self.save()

    def save(self):
        temporary = self.output / "result.tmp"
        temporary.write_text(json.dumps(self.record, indent=2) + "\n")
        temporary.replace(self.output / "result.json")

    def run(self, args: list[str], *, timeout: int = 45, check: bool = True) -> str:
        index = len(self.record["commands"])
        entry = {"args": args, "started": time.time(), "status": "started"}
        self.record["commands"].append(entry)
        self.save()
        stdout = self.output / f"{index:03d}.stdout"
        stderr = self.output / f"{index:03d}.stderr"
        try:
            with stdout.open("w") as out, stderr.open("w") as err:
                result = subprocess.run(args, env=self.env, stdout=out, stderr=err, timeout=timeout)
            entry.update(status="finished", returncode=result.returncode)
        except (OSError, subprocess.TimeoutExpired) as error:
            entry.update(status="failed", error=str(error))
            raise RuntimePolicyError(f"command failed: {args}: {error}") from error
        finally:
            self.save()
        require(not check or result.returncode == 0, f"command failed: {args}; see {stderr.name}")
        return stdout.read_text()

    def p(self, *args: str, **kwargs) -> str:
        return self.run([PODMAN, "--connection", CONNECTION, *args], **kwargs)

    def k(self, *args: str, **kwargs) -> str:
        return self.run([KUBECTL, "--kubeconfig", self.env["KUBECONFIG"],
                         "--context", f"kind-{self.name}", "--request-timeout=30s", *args], **kwargs)

    def execute(self) -> dict:
        network_id = None
        attempted = False
        before = []
        owned = []
        volumes_before = []
        owned_volumes = []
        checks_passed = False
        try:
            require(self.run([PODMAN, "--version"]).strip() == "podman version 6.0.2",
                    "unreviewed Podman version")
            require(self.run([KIND, "version"]).startswith("kind v0.33.0 "),
                    "unreviewed Kind version")
            client = json.loads(self.run([KUBECTL, "version", "--client", "-o", "json"]))
            require(client["clientVersion"]["gitVersion"] == "v1.37.1", "unreviewed kubectl version")
            connections = json.loads(self.run([PODMAN, "system", "connection", "list", "--format", "json"]))
            selected = [c for c in connections if c["Name"] == CONNECTION]
            require(len(selected) == 1 and selected[0]["Default"] is True
                    and selected[0]["URI"].startswith("ssh://root@127.0.0.1:"),
                    "observed rootful default changed; owner review required")
            info = json.loads(self.p("info", "--format", "json"))
            self.record["engine"] = engine_identity(info)
            require(info["host"]["security"]["rootless"] is False
                    and info["host"]["cgroupVersion"] == "v2"
                    and info["host"]["arch"] == "arm64", "unsupported smoke host")
            meminfo = self.run([PODMAN, "machine", "ssh", "podman-machine-default",
                                "cat", "/proc/meminfo"])
            available = int(next(line.split()[1] for line in meminfo.splitlines()
                                 if line.startswith("MemAvailable:"))) * 1024
            self.record["memoryAvailableBytes"] = available
            require(available >= 2 * 1024**3 and info["host"]["cpus"] >= 2,
                    "insufficient free capacity for bounded smoke")
            self.run([PODMAN, "machine", "inspect"])
            self.p("images", "--format", "json")
            volumes_before = json.loads(self.p("volume", "ls", "--format", "json"))
            before = json.loads(self.p("ps", "-a", "--format", "json"))
            networks = json.loads(self.p("network", "ls", "--format", "json"))
            clusters = self.run([KIND, "get", "clusters"])
            require(not any(f"{self.name}-control-plane" in c["Names"] for c in before)
                    and not any(n["name"] == self.name for n in networks)
                    and self.name not in clusters.splitlines(), "owned name collision")
            self.record["ownership"] = {"preexistingContainerIds": sorted(inventory_ids(before)),
                                        "cluster": self.name, "network": self.name}
            self.save()
            network_id = self.p("network", "create", "--label", f"radius-smoke={self.name}",
                                self.name).strip()
            require(network_id == self.name, "missing created network name receipt")
            network_id = json.loads(self.p("network", "inspect", self.name))[0]["id"]
            self.record["ownership"]["networkId"] = network_id
            self.save()
            config = {"kind": "Cluster", "apiVersion": "kind.x-k8s.io/v1alpha4",
                      "networking": {"apiServerAddress": "127.0.0.1"},
                      "nodes": [{"role": "control-plane"}]}
            config_path = self.output / "kind.json"
            config_path.write_text(json.dumps(config))
            attempted = True
            self.run([KIND, "create", "cluster", "--name", self.name, "--image", NODE_IMAGE,
                      "--config", str(config_path), "--kubeconfig", self.env["KUBECONFIG"],
                      "--wait", "180s", "--retain"], timeout=600)
            owned = json.loads(self.p("ps", "-a", "--filter",
                                     f"label=io.x-k8s.kind.cluster={self.name}", "--format", "json"))
            self.record["ownership"]["containers"] = owned
            self.save()
            require(len(owned) == 1, "missing owned node")
            node = json.loads(self.p("inspect", owned[0]["Id"]))[0]
            owned_volumes = [mount["Name"] for mount in node["Mounts"] if mount["Type"] == "volume"]
            require(bool(owned_volumes), "missing positive node volume inventory")
            self.record["ownership"]["volumes"] = owned_volumes
            self.save()
            loopback_ports(node)
            self.k("wait", "--for=condition=Ready", "nodes", "--all", "--timeout=120s", timeout=150)
            ready_nodes(json.loads(self.k("get", "nodes", "-o", "json")))
            image = json.loads(self.p("image", "inspect", NODE_IMAGE))
            require(len(image) == 1 and image[0]["Id"] == node["Image"]
                    and image[0]["Architecture"] == "arm64",
                    "node image does not match pinned local image/architecture")
            manifest = self.output / "workloads.json"
            manifest.write_text(json.dumps(manifests(self.name), indent=2))
            self.k("apply", "-f", str(manifest))
            self.k("-n", "smoke", "wait", "--for=condition=Ready", "pod/web",
                   "--timeout=180s", timeout=210)
            image_ready(json.loads(self.k("-n", "smoke", "get", "pod/web", "-o", "json")))
            pvc = json.loads(self.k("-n", "smoke", "get", "pvc/data", "-o", "json"))
            require(pvc["status"]["phase"] == "Bound" and bool(pvc["spec"]["volumeName"]),
                    "persistent volume did not bind")
            self.k("-n", "smoke", "wait", "--for=jsonpath={.status.phase}=Succeeded",
                   "pod/reader", "--timeout=90s", timeout=120)
            require(self.k("-n", "smoke", "logs", "reader").strip() == self.name,
                    "volume read from independent pod failed")
            def shell(command: str) -> str:
                return self.k("-n", "smoke", "exec", "web", "--", "sh", "-ec", command)
            dns = shell("nslookup web.smoke.svc.cluster.local")
            require("web.smoke.svc.cluster.local" in dns and "Address:" in dns, "DNS evidence missing")
            require(shell("wget -qO- http://web.smoke.svc.cluster.local:8080").strip() == self.name,
                    "service path did not deliver owned payload")
            # A wrong service and a wrong payload must not masquerade as success.
            shell("if wget -T 3 -qO- http://absent.smoke.svc.cluster.local:8080; "
                  "then exit 1; else echo EXPECTED_DNS_FAILURE; fi")
            cpu = shell("cat /sys/fs/cgroup/cpu.max")
            memory = shell("cat /sys/fs/cgroup/memory.max")
            first = shell("cat /sys/fs/cgroup/cpu.stat")
            shell("timeout 4 sh -c 'while :; do :; done' || test $? -eq 143")
            last = shell("cat /sys/fs/cgroup/cpu.stat")
            self.record["cpuDelta"] = limits(cpu, memory, first, last)
            self.k("-n", "smoke", "wait", "--for=jsonpath={.status.phase}=Failed",
                   "pod/memory", "--timeout=90s", timeout=120)
            oom(json.loads(self.k("-n", "smoke", "get", "pod/memory", "-o", "json")))
            require(self.k("-n", "smoke", "logs", "memory").splitlines()[0] == "33554432",
                    "memory positive control lacks measured limit")
            self.forward()
            checks_passed = True
        except (RuntimePolicyError, OSError, ValueError, KeyError, IndexError, StopIteration) as error:
            self.record["errors"].append(str(error))
        finally:
            self.save()
            if attempted:
                for args in [
                    [KUBECTL, "--kubeconfig", self.env["KUBECONFIG"], "--context", f"kind-{self.name}",
                     "--request-timeout=30s", "get", "pods", "-A", "-o", "json"],
                    [KUBECTL, "--kubeconfig", self.env["KUBECONFIG"], "--context", f"kind-{self.name}",
                     "--request-timeout=30s", "get", "events", "-A", "-o", "json"],
                    [KIND, "export", "logs", "--name", self.name, str(self.output / "cluster-logs")],
                ]:
                    try:
                        self.run(args, timeout=90)
                    except RuntimePolicyError as error:
                        self.record["errors"].append(f"diagnostics: {error}")
                        self.save()
                try:
                    owned = json.loads(self.p("ps", "-a", "--filter",
                                             f"label=io.x-k8s.kind.cluster={self.name}", "--format", "json"))
                    self.record["ownership"]["containersBeforeCleanup"] = owned
                    self.save()
                    self.run([KIND, "delete", "cluster", "--name", self.name,
                              "--kubeconfig", self.env["KUBECONFIG"]], timeout=120)
                    after = json.loads(self.p("ps", "-a", "--format", "json"))
                    cleaned(before, owned, after)
                    volumes_after = json.loads(self.p("volume", "ls", "--format", "json"))
                    require({v["Name"] for v in volumes_after} == {v["Name"] for v in volumes_before}
                            and set(owned_volumes).isdisjoint(v["Name"] for v in volumes_after),
                            "volume cleanup differs from preflight inventory")
                    self.record["cleanup"] = "verified"
                except (RuntimePolicyError, OSError, ValueError, KeyError) as error:
                    self.record["errors"].append(f"cluster cleanup: {error}")
                    self.record["cleanup"] = "unverified"
            if network_id:
                try:
                    observed = json.loads(self.p("network", "inspect", self.name))[0]
                    require(observed["id"] == network_id
                            and observed["labels"]["radius-smoke"] == self.name,
                            "network ownership changed")
                    self.p("network", "rm", network_id)
                    remaining = json.loads(self.p("network", "ls", "--format", "json"))
                    require(all(n["id"] != network_id for n in remaining), "owned network remains")
                except (RuntimePolicyError, OSError, ValueError, KeyError) as error:
                    self.record["errors"].append(f"network cleanup: {error}")
                    self.record["cleanup"] = "unverified"
            if self.record["errors"]:
                self.record["status"] = "failed"
            elif checks_passed and self.record["cleanup"] == "verified":
                self.record["status"] = "passed"
            self.save()
        return self.record

    def forward(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        args = [KUBECTL, "--kubeconfig", self.env["KUBECONFIG"], "--context", f"kind-{self.name}",
                "-n", "smoke", "port-forward", "--address=127.0.0.1", "service/web", f"{port}:8080"]
        self.record["portForward"] = {"args": args, "port": port, "status": "started"}
        self.save()
        with (self.output / "forward.log").open("w") as log:
            process = subprocess.Popen(args, env=self.env, stdout=log, stderr=log)
            try:
                for _ in range(30):
                    if "Forwarding from 127.0.0.1:" in (self.output / "forward.log").read_text():
                        break
                    require(process.poll() is None, "loopback forward exited")
                    time.sleep(0.2)
                with urllib.request.urlopen(f"http://127.0.0.1:{port}", timeout=5) as response:
                    payload = response.read().decode()
                (self.output / "loopback-response.txt").write_text(payload)
                require(payload.strip() == self.name, "loopback response mismatch")
                self.record["portForward"]["status"] = "observed"
            finally:
                process.terminate()
                process.wait(timeout=10)
                self.record["portForward"]["returncode"] = process.returncode
                self.save()
        with socket.socket() as probe:
            probe.settimeout(2)
            require(probe.connect_ex(("127.0.0.1", port)) != 0, "forward listener remains after cleanup")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    result = Smoke(args.output).execute()
    print(json.dumps(result, indent=2))
    return 0 if result["status"] == "passed" else 2


if __name__ == "__main__":
    raise SystemExit(main())
