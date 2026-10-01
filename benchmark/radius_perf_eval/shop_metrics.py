"""Witness metric export and periodic self-telemetry, not SDK completeness."""

from __future__ import annotations

import json
import math
import time
from urllib.parse import urlencode

from .shop_telemetry import Evidence, counter_samples, request

SENT = "otelcol_exporter_sent_metric_points"
FAILURES = {
    "otelcol_exporter_send_failed_metric_points",
    "otelcol_exporter_enqueue_failed_metric_points",
    "otelcol_receiver_refused_metric_points",
    "otelcol_receiver_failed_metric_points",
}
EXPORTERS = {"debug", "otlp_http/prometheus"}


class MetricExportError(RuntimeError):
    """Metric export failed or the backend supplied no fresh counter evidence."""


def direct_counters(text: str) -> dict:
    samples = counter_samples(text, {SENT, "target_info"} | FAILURES)
    identities = [labels for name, labels, _ in samples if name == "target_info"]
    if (len(identities) != 1 or identities[0].get("service_name") != "otelcol-contrib"
            or not identities[0].get("service_instance_id")):
        raise MetricExportError("missing or ambiguous collector telemetry identity")
    sent = {}
    failures = []
    for name, labels, value in samples:
        if name in FAILURES:
            failures.append({"name": name, "labels": labels, "value": value})
            if value != 0:
                raise MetricExportError(f"collector reports metric failure: {name} {labels} = {value}")
        if name == SENT:
            exporter = labels.get("exporter")
            if exporter in sent or value <= 0:
                raise MetricExportError("duplicate or empty metric export counter")
            sent[exporter] = value
    if set(sent) != EXPORTERS:
        raise MetricExportError("missing or unexpected metric export counters")
    return {"instance": identities[0]["service_instance_id"], "sent": sent,
            "observedFailureCounters": failures}


def backend_counters(payload: dict, baseline: dict, since: float) -> dict:
    """Require actual stored samples newer than the direct-read boundary.

    Instant-vector timestamps are query evaluation times, not sample times.
    A range vector retains the timestamps of the stored periodic exports.
    """
    data = payload.get("data", {})
    if (payload.get("status") != "success" or data.get("resultType") != "matrix"
            or payload.get("warnings") or payload.get("infos")):
        raise MetricExportError("Prometheus query failed or returned qualified evidence")
    observed = {}
    for series in data.get("result", []):
        labels = series.get("metric", {})
        exporter = labels.get("exporter")
        if (labels.get("__name__") != SENT + "_total"
                or labels.get("service_instance_id") != baseline["instance"]
                or exporter not in EXPORTERS or exporter in observed):
            raise MetricExportError("unexpected or duplicate Prometheus metric series")
        values = series.get("values", [])
        if not values:
            raise MetricExportError("Prometheus series has no stored samples")
        timestamp, raw_value = values[-1]
        value = float(raw_value)
        if not math.isfinite(value) or value < 0 or not math.isfinite(timestamp):
            raise MetricExportError("Prometheus sample is not a finite nonnegative counter")
        observed[exporter] = {"timestamp": timestamp, "value": value}
    ready = set(observed) == EXPORTERS and all(
        sample["timestamp"] >= since and sample["value"] >= baseline["sent"][exporter]
        for exporter, sample in observed.items()
    )
    return {"ready": ready, "observed": observed}


def verify(project: str, evidence: Evidence, *, timeout: float = 90) -> dict:
    since = time.time()
    deadline = time.monotonic() + timeout
    baseline = direct_counters(request(
        project, evidence, "metric-direct-open", "http://otel-collector:8888/metrics", raw=True,
    ))
    # Query both exporter counters, scoped to the live collector instance.
    selector = SENT + "_total{service_instance_id=" + json.dumps(baseline["instance"]) + "}[2m]"
    url = "http://prometheus:9090/api/v1/query?" + urlencode({"query": selector})
    while True:
        payload = request(project, evidence, "metric-prometheus-raw", url)
        backend = backend_counters(payload, baseline, since)
        current = direct_counters(request(
            project, evidence, "metric-direct-close", "http://otel-collector:8888/metrics", raw=True,
        ))
        if (current["instance"] != baseline["instance"]
                or any(current["sent"][name] < baseline["sent"][name] for name in EXPORTERS)):
            raise MetricExportError("collector identity changed or metric counters decreased")
        result = {"baseline": baseline, "current": current, "since": since, "backend": backend}
        evidence("metric-export-witness", result)
        if backend["ready"]:
            return result
        if time.monotonic() >= deadline:
            raise MetricExportError("fresh Prometheus export counters did not arrive before the deadline")
        time.sleep(2)
