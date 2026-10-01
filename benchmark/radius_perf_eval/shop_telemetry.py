"""Direct evidence for the pinned Shop's collector-to-OpenSearch log path.

Reconciliation covers records accepted by the collector, not logs that an
application SDK never delivered. A stable bracket avoids mistaking an async
export queue or OpenSearch refresh lag for lost records.
"""

from __future__ import annotations

import json
import re
import time
import uuid
from collections.abc import Callable
from typing import Any
from urllib.parse import urlsplit

from .compose import ComposeProject
from .docker_cli import CommandResult, docker
from .shop_readiness import PROBE_IMAGE

Evidence = Callable[[str, Any], None]
TEMPLATE = {
    "index_patterns": ["otel-logs-*"],
    "composed_of": [],
    "template": {"mappings": {"properties": {
        "attributes": {"type": "object", "disable_objects": True},
    }}},
}


class LogIngestionError(RuntimeError):
    """Log-ingestion evidence is missing, inconsistent, or reports a loss."""


def start_stack(project: ComposeProject, evidence: Evidence) -> CommandResult:
    started = project.up(services=["opensearch"], wait_timeout=180)
    evidence("opensearch-up", {"stdout": started.stdout, "stderr": started.stderr})
    evidence("log-template-request", TEMPLATE)
    response = request(project.project, evidence, "log-template-install",
                       "http://opensearch:9200/_index_template/log-attributes",
                       method="PUT", payload=TEMPLATE)
    if response.get("acknowledged") is not True:
        raise LogIngestionError("OpenSearch did not acknowledge the log template")
    response = request(project.project, evidence, "log-template-readback",
                       "http://opensearch:9200/_index_template/log-attributes")
    templates = response.get("index_templates", [])
    if len(templates) != 1 or templates[0].get("index_template") != TEMPLATE:
        raise LogIngestionError("OpenSearch log template differs from the requested mapping")
    return project.up(wait_timeout=900)


def request(project: str, evidence: Evidence, name: str, url: str, *,
            method: str = "GET", payload: Any = None, raw: bool = False) -> Any:
    args = ["run", "--rm", "--network", f"{project}_default",
            "--label", f"com.docker.compose.project={project}", PROBE_IMAGE,
            "--silent", "--show-error", "--fail-with-body", "--max-time", "15", "-X", method]
    if payload is not None:
        args += ["-H", "Content-Type: application/json", "--data-binary", json.dumps(payload)]
    result = docker(*args, url, check=False, timeout=30)
    evidence(name, {"exitCode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
    if result.returncode:
        raise LogIngestionError(f"{name}: HTTP request failed ({result.returncode})")
    if raw:
        return result.stdout
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError as error:
        raise LogIngestionError(f"{name}: invalid JSON response") from error


def counter_samples(text: str, names: set[str]) -> list[tuple[str, dict[str, str], int]]:
    """Read selected integer counters without silently skipping malformed samples."""
    samples = []
    for line in text.splitlines():
        if line.startswith("#") or not line.strip():
            continue
        name = re.split(r"[\s{]", line, maxsplit=1)[0]
        if name not in names:
            continue
        match = re.fullmatch(r'(\w+)\{(.*)\} ([0-9]+)(?:\.0+)?', line)
        if match is None:
            raise LogIngestionError(f"malformed telemetry counter: {line!r}")
        labels = {}
        remaining = match[2]
        while remaining:
            label = re.match(r'(\w+)=("(?:[^"\\]|\\.)*")(?:,|$)', remaining)
            if label is None or label[1] in labels:
                raise LogIngestionError(f"malformed telemetry counter labels: {line!r}")
            labels[label[1]] = json.loads(label[2])
            remaining = remaining[label.end():]
        samples.append((name, labels, int(match[3].split(".")[0])))
    return samples


def counters(text: str) -> dict[str, int]:
    """Parse the pinned collector's direct exposition, never missing-as-zero."""
    names = {
        "otelcol_receiver_accepted_log_records": "accepted",
        "otelcol_receiver_refused_log_records": "refused",
        "otelcol_receiver_failed_log_records": "failed",
        "otelcol_exporter_sent_log_records": "sent",
        "otelcol_exporter_send_failed_log_records": "send-failed",
        "otelcol_exporter_enqueue_failed_log_records": "enqueue-failed",
    }
    values = {}
    for name, labels, value in counter_samples(text, set(names)):
        if name.startswith("otelcol_receiver_"):
            if labels.get("receiver") != "otlp" or labels.get("transport") not in ("grpc", "http"):
                raise LogIngestionError(f"unexpected log receiver: {labels}")
            key = f"{names[name]}:{labels['transport']}"
        else:
            if labels.get("exporter") not in ("debug", "opensearch"):
                raise LogIngestionError(f"unexpected log exporter: {labels}")
            key = f"{names[name]}:{labels['exporter']}"
        if key in values:
            raise LogIngestionError(f"duplicate log counter: {key}")
        values[key] = value
    required = {f"{kind}:{transport}" for kind in ("accepted", "refused", "failed")
                for transport in ("grpc", "http")} | {"sent:debug", "sent:opensearch"}
    if not required <= values.keys():
        raise LogIngestionError(f"missing log counters: {sorted(required - values.keys())}")
    if any(values[f"accepted:{transport}"] <= 0 for transport in ("grpc", "http")):
        raise LogIngestionError("both Shop log transports must have delivered records")
    errors = {name: value for name, value in values.items()
              if name.split(":")[0] in ("refused", "failed", "send-failed", "enqueue-failed") and value}
    if errors:
        raise LogIngestionError(f"collector reports log failures: {errors}")
    return values


def shard_count(payload: dict) -> int:
    shards = payload.get("_shards", {})
    count = payload.get("count")
    if (type(count) is not int or count <= 0 or type(shards.get("total")) is not int
            or shards["total"] <= 0 or shards.get("successful") != shards["total"]
            or shards.get("failed") != 0):
        raise LogIngestionError("index count is empty or shard evidence is incomplete")
    return count


def verify_refresh(payload: dict) -> None:
    shards = payload.get("_shards", {})
    # Refresh totals include unassigned replicas on this single-node cluster;
    # _count/_search below must still cover every queried primary shard.
    if (type(shards.get("total")) is not int or type(shards.get("successful")) is not int
            or not 0 < shards["successful"] <= shards["total"] or shards.get("failed") != 0):
        raise LogIngestionError("index refresh has no successful shard evidence")


def reconciled(before: dict[str, int], count: int, after: dict[str, int]) -> bool:
    accepted = before["accepted:grpc"] + before["accepted:http"]
    if not before.keys() <= after.keys() or any(after[key] < value for key, value in before.items()):
        raise LogIngestionError("collector counters decreased during reconciliation")
    return (
        accepted > 0
        and before["accepted:grpc"] == after["accepted:grpc"]
        and before["accepted:http"] == after["accepted:http"]
        and accepted == count == after["sent:opensearch"] == after["sent:debug"]
    )


def read_bracket(project: str, evidence: Evidence) -> tuple[str, dict, dict, str]:
    delimiter = "\nRADIUS_LOG_AUDIT_FRAME\n"
    commands = [
        "curl -fsS --max-time 15 http://otel-collector:8888/metrics",
        "curl -fsS --max-time 15 -X POST 'http://opensearch:9200/otel-logs-*/_refresh'",
        "curl -fsS --max-time 15 'http://opensearch:9200/otel-logs-*/_count'",
        "curl -fsS --max-time 15 http://otel-collector:8888/metrics",
    ]
    script = ("; printf '\\nRADIUS_LOG_AUDIT_FRAME\\n'; ").join(commands)
    result = docker(
        "run", "--rm", "--network", f"{project}_default",
        "--label", f"com.docker.compose.project={project}",
        "--entrypoint", "sh", PROBE_IMAGE, "-ec", script, check=False, timeout=75,
    )
    evidence("log-bracket-raw", {"exitCode": result.returncode, "stdout": result.stdout, "stderr": result.stderr})
    parts = result.stdout.split(delimiter)
    if result.returncode or len(parts) != 4:
        raise LogIngestionError("log bracket HTTP requests failed or returned incomplete evidence")
    try:
        return parts[0], json.loads(parts[1]), json.loads(parts[2]), parts[3]
    except json.JSONDecodeError as error:
        raise LogIngestionError("log bracket contains invalid JSON") from error


def reconcile(project: str, evidence: Evidence, *, timeout: float = 60) -> dict:
    deadline = time.monotonic() + timeout
    while True:
        raw_before, refresh, counted, raw_after = read_bracket(project, evidence)
        before = counters(raw_before)
        verify_refresh(refresh)
        count = shard_count(counted)
        after = counters(raw_after)
        result = {"before": before, "indexed": count, "after": after,
                  "reconciled": reconciled(before, count, after)}
        evidence("log-reconciliation", result)
        if result["reconciled"]:
            return result
        if time.monotonic() >= deadline:
            raise LogIngestionError("log counts did not reconcile inside a stable bracket")
        time.sleep(1)


def probe_payload(marker: str) -> dict:
    now = str(time.time_ns())
    return {"resourceLogs": [{"resource": {"attributes": [
        {"key": "service.name", "value": {"stringValue": "radius-ingestion-control"}},
    ]}, "scopeLogs": [{"scope": {"name": "radius-ingestion-control"}, "logRecords": [
        {"timeUnixNano": now, "observedTimeUnixNano": now, "body": {"stringValue": marker},
         "attributes": [
             {"key": "http", "value": {"stringValue": "scalar preserved"}},
             {"key": "http.request.method", "value": {"stringValue": "GET"}},
             {"key": "radius.acceptance.value", "value": {"intValue": str(value)}},
         ]} for value in (7, 13)
    ]}]}]}


def verify_probe_sources(payload: dict, marker: str) -> None:
    shard_count({**payload, "count": 1})
    hits = payload.get("hits", {})
    if hits.get("total") != {"value": 2, "relation": "eq"}:
        raise LogIngestionError("typed probe records are missing or duplicated")
    sources = [hit["_source"] for hit in hits.get("hits", [])]
    observed = []
    for source in sources:
        attributes = source.get("attributes", {})
        if (source.get("body") != marker or attributes.get("http") != "scalar preserved"
                or attributes.get("http.request.method") != "GET"
                or type(attributes.get("radius.acceptance.value")) is not int):
            raise LogIngestionError("typed probe source fields changed or disappeared")
        observed.append(attributes["radius.acceptance.value"])
    if sorted(observed) != [7, 13]:
        raise LogIngestionError("typed probe numeric values changed")


def typed_probe(project: str, evidence: Evidence) -> dict:
    marker = "radius-ingestion-" + uuid.uuid4().hex
    response = request(project, evidence, "log-probe-submit", "http://otel-collector:4318/v1/logs",
                       method="POST", payload=probe_payload(marker))
    if response not in ({}, {"partialSuccess": {}}):
        raise LogIngestionError(f"OTLP probe was not fully accepted: {response}")
    # OTLP acknowledgment can precede asynchronous export.
    deadline = time.monotonic() + 30
    while True:
        refresh = request(project, evidence, "log-probe-refresh",
                          "http://opensearch:9200/otel-logs-*/_refresh", method="POST")
        verify_refresh(refresh)
        response = request(project, evidence, "log-probe-sources",
                           "http://opensearch:9200/otel-logs-*/_search", method="POST",
                           payload={"size": 3, "track_total_hits": True,
                                    "query": {"match_phrase": {"body": marker}}})
        total = response.get("hits", {}).get("total", {}).get("value")
        if total == 2 or time.monotonic() >= deadline:
            break
        time.sleep(1)
    verify_probe_sources(response, marker)
    ranged = request(project, evidence, "log-probe-numeric-query",
                     "http://opensearch:9200/otel-logs-*/_search", method="POST",
                     payload={"size": 0, "track_total_hits": True, "query": {"bool": {"filter": [
                         {"match_phrase": {"body": marker}},
                         {"range": {"attributes.radius.acceptance.value": {"gte": 10}}},
                     ]}}, "aggs": {"mean": {"avg": {"field": "attributes.radius.acceptance.value"}}}})
    shard_count({**ranged, "count": 1})
    if (ranged.get("hits", {}).get("total") != {"value": 1, "relation": "eq"}
            or ranged.get("aggregations", {}).get("mean", {}).get("value") != 13):
        raise LogIngestionError("typed probe numeric range/aggregation failed")
    return {"marker": marker, "sourcesPreserved": True, "numericQueryVerified": True}


def grafana_logs(project: str, evidence: Evidence, services: set[str]) -> dict:
    now = int(time.time() * 1000)
    payload = request(project, evidence, "grafana-application-logs",
                      "http://frontend-proxy:8080/grafana/api/ds/query", method="POST", payload={
                          "from": str(now - 3600000), "to": str(now), "queries": [{
                              "refId": "A", "datasource": {
                                  "type": "grafana-opensearch-datasource", "uid": "webstore-logs"},
                              "format": "table", "queryType": "PPL", "luceneQueryType": "Logs",
                              "metrics": [{"id": "1", "type": "logs"}], "timeField": "observedTimestamp",
                              "query": "search source=otel-logs-* | fields @timestamp, resource.service.name, body",
                              "intervalMs": 1000, "maxDataPoints": 100,
                          }],
                      })
    return verify_grafana_logs(payload, services)


def verify_grafana_logs(payload: dict, services: set[str]) -> dict:
    result = payload.get("results", {}).get("A", {})
    if result.get("error") or result.get("status") != 200:
        raise LogIngestionError("Grafana application-log query failed")
    observed = set()
    for frame in result.get("frames", []):
        fields = [field["name"] for field in frame.get("schema", {}).get("fields", [])]
        values = frame.get("data", {}).get("values", [])
        if len(fields) != len(values) or not {"@timestamp", "resource.service.name", "body"} <= set(fields):
            raise LogIngestionError("Grafana log frame lacks diagnostic fields")
        times, names, bodies = (values[fields.index(name)] for name in ("@timestamp", "resource.service.name", "body"))
        if not len(times) == len(names) == len(bodies):
            raise LogIngestionError("Grafana log frame has unequal column lengths")
        for timestamp, name, body in zip(times, names, bodies):
            if name in services and timestamp and isinstance(body, str) and body:
                observed.add(name)
    if not observed:
        raise LogIngestionError("Grafana returned no actual Shop application logs")
    return {"observedServices": sorted(observed)}


def audit_grafana_logs(text: str, services: set[str]) -> dict:
    """Inventory logged destinations, not a packet-level absence claim."""
    lines = text.splitlines()
    if not any('msg="Starting Grafana"' in line for line in lines):
        raise LogIngestionError("Grafana audit lacks complete startup log evidence")
    allowed = services | {"localhost", "127.0.0.1", "::1"}
    destinations = []
    candidates = []
    for number, line in enumerate(lines, 1):
        urls = re.findall(r'https?://[^\s"<>]+', line)
        for url in urls:
            hostname = urlsplit(url).hostname
            entry = {"line": number, "url": url, "host": hostname, "text": line}
            destinations.append(entry)
            if hostname not in allowed:
                candidates.append(entry)
        if not urls and re.search(r'grafana\.com|download|outbound|dial tcp|no such host', line, re.I):
            candidates.append({"line": number, "text": line})
    return {"linesExamined": len(lines), "destinations": destinations,
            "unexplainedCandidates": candidates,
            "scope": "complete Grafana stdout/stderr since startup; unlogged attempts are not observable"}
