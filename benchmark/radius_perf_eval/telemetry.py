"""Baseline telemetry capture from Prometheus.

Queries are the canonical PromQL from ``docs/specs/telemetry-contract.md``. The
exact query string is stored alongside every value so a later remediation
comparison is provably measuring the same thing as the baseline.
"""

from __future__ import annotations

import json
import math
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any

HTTP_LATENCY_QUANTILE = (
    'histogram_quantile({q}, sum by (le) ('
    'rate(catalog_http_request_duration_seconds_bucket'
    '{{route=~"products.list|products.get",status=~"2.."}}[{window}])))'
)

DEPENDENCY_LATENCY_QUANTILE = (
    'histogram_quantile({q}, sum by (le) ('
    'rate(catalog_dependency_request_duration_seconds_bucket'
    '{{dependency="{dependency}"}}[{window}])))'
)

HTTP_THROUGHPUT = 'sum(rate(catalog_http_requests_total[{window}]))'

HTTP_ERROR_RATE = (
    'sum(rate(catalog_http_requests_total{{status=~"5.."}}[{window}])) / '
    'clamp_min(sum(rate(catalog_http_requests_total[{window}])), 0.000001)'
)

CACHE_HIT_RATIO = (
    'sum(rate(catalog_cache_requests_total{{result="hit",operation=~"list|get"}}[{window}])) / '
    'clamp_min(sum(rate(catalog_cache_requests_total'
    '{{result=~"hit|miss",operation=~"list|get"}}[{window}])), 0.000001)'
)

SCRAPE_UP = 'up{job="catalog-api"}'


class PrometheusError(RuntimeError):
    pass


@dataclass(frozen=True)
class Measurement:
    name: str
    value: float | None
    promql: str
    unit: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "value": self.value,
            "promql": self.promql,
            "unit": self.unit,
        }


@dataclass(frozen=True)
class TelemetryWindow:
    phase: str
    start_epoch: float
    end_epoch: float
    window_seconds: int
    measurements: dict[str, Measurement] = field(default_factory=dict)

    def value(self, name: str) -> float | None:
        measurement = self.measurements.get(name)
        return measurement.value if measurement else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "phase": self.phase,
            "window": {
                "startEpoch": self.start_epoch,
                "endEpoch": self.end_epoch,
                "rangeSeconds": self.window_seconds,
            },
            "measurements": {
                name: measurement.to_dict() for name, measurement in sorted(self.measurements.items())
            },
        }


class PrometheusClient:
    def __init__(self, base_url: str, *, timeout: float = 20.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def _get(self, path: str, params: dict[str, str]) -> Any:
        url = f"{self.base_url}{path}?{urllib.parse.urlencode(params)}"
        try:
            with urllib.request.urlopen(url, timeout=self.timeout) as response:
                payload = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            raise PrometheusError(f"prometheus request failed: {url}: {exc}") from exc
        if payload.get("status") != "success":
            raise PrometheusError(f"prometheus error for {url}: {payload}")
        return payload["data"]

    def ready(self, *, attempts: int = 40, interval: float = 1.0) -> bool:
        for _ in range(attempts):
            try:
                with urllib.request.urlopen(f"{self.base_url}/-/ready", timeout=5) as response:
                    if response.status == 200:
                        return True
            except Exception:
                pass
            time.sleep(interval)
        return False

    def instant(self, promql: str, at_epoch: float | None = None) -> float | None:
        params = {"query": promql}
        if at_epoch is not None:
            params["time"] = f"{at_epoch:.3f}"
        data = self._get("/api/v1/query", params)
        result = data.get("result") or []
        if not result:
            return None
        raw = result[0].get("value", [None, None])[1]
        if raw is None:
            return None
        value = float(raw)
        return None if math.isnan(value) else value

    def scrape_target_up(self) -> bool:
        """Confirm Prometheus is actually scraping the application."""
        try:
            return (self.instant(SCRAPE_UP) or 0.0) >= 1.0
        except PrometheusError:
            return False


def capture_window(
    client: PrometheusClient,
    *,
    phase: str,
    start_epoch: float,
    end_epoch: float,
    settle_seconds: float = 7.0,
) -> TelemetryWindow:
    """Capture the canonical metric set over one load phase.

    ``settle_seconds`` allows at least one further scrape to land after the load
    stops, so the final samples of the phase are included in the range vector.
    """
    if settle_seconds > 0:
        time.sleep(settle_seconds)

    span = max(int(math.ceil(end_epoch - start_epoch)), 15)
    window = f"{span}s"
    at = end_epoch

    specs: list[tuple[str, str, str]] = [
        ("httpP50Seconds", HTTP_LATENCY_QUANTILE.format(q="0.50", window=window), "seconds"),
        ("httpP95Seconds", HTTP_LATENCY_QUANTILE.format(q="0.95", window=window), "seconds"),
        ("httpP99Seconds", HTTP_LATENCY_QUANTILE.format(q="0.99", window=window), "seconds"),
        ("httpThroughputRps", HTTP_THROUGHPUT.format(window=window), "requests/second"),
        ("httpErrorRate", HTTP_ERROR_RATE.format(window=window), "ratio"),
        (
            "mysqlP95Seconds",
            DEPENDENCY_LATENCY_QUANTILE.format(q="0.95", dependency="mysql", window=window),
            "seconds",
        ),
        (
            "valkeyP95Seconds",
            DEPENDENCY_LATENCY_QUANTILE.format(q="0.95", dependency="valkey", window=window),
            "seconds",
        ),
        ("cacheHitRatio", CACHE_HIT_RATIO.format(window=window), "ratio"),
    ]

    measurements: dict[str, Measurement] = {}
    for name, promql, unit in specs:
        try:
            value = client.instant(promql, at)
        except PrometheusError:
            value = None
        measurements[name] = Measurement(name=name, value=value, promql=promql, unit=unit)

    return TelemetryWindow(
        phase=phase,
        start_epoch=start_epoch,
        end_epoch=end_epoch,
        window_seconds=span,
        measurements=measurements,
    )
