# Telemetry Contract

## Scope

This contract defines the metrics emitted by the Phase 1 catalogue API and the JSON a future Canvas telemetry adapter returns. The adapter overlays telemetry on Radius topology; it does not create or modify Radius resources or infer undeclared connections.

## Stable Prometheus metrics

Metric and label names are compatibility surfaces. New labels require adapter review because label changes can break queries and recorded fixtures.

### `catalog_http_requests_total`

Counter for completed catalogue API requests.

Labels:

- `method`: HTTP method, currently `GET`.
- `route`: stable low-cardinality route ID, `products.list` or `products.get`.
- `status`: three-digit HTTP status string.

### `catalog_http_request_duration_seconds`

Histogram for completed catalogue API request duration.

Labels:

- `method`
- `route`
- `status`

### `catalog_dependency_request_duration_seconds`

Histogram for dependency operation duration, including the configured artificial MySQL delay.

Labels:

- `dependency`: `mysql` or `valkey`.
- `operation`: `list`, `get`, `list_set`, or `get_set`.
- `outcome`: `success`, `error`, `not_found`, `hit`, or `miss` as applicable.

### `catalog_cache_requests_total`

Counter for cache-aside decisions at the service layer.

Labels:

- `operation`: `list`, `get`, `list_set`, or `get_set`.
- `result`: `hit`, `miss`, or `error`.

## Canonical PromQL

Catalogue p95:

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(catalog_http_request_duration_seconds_bucket{route=~"products.list|products.get",status=~"2.."}[2m])
  )
)
```

MySQL p95:

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(catalog_dependency_request_duration_seconds_bucket{dependency="mysql"}[2m])
  )
)
```

Valkey p95:

```promql
histogram_quantile(
  0.95,
  sum by (le) (
    rate(catalog_dependency_request_duration_seconds_bucket{dependency="valkey"}[2m])
  )
)
```

Cache hit ratio:

```promql
sum(rate(catalog_cache_requests_total{result="hit",operation=~"list|get"}[2m]))
/
clamp_min(sum(rate(catalog_cache_requests_total{result=~"hit|miss",operation=~"list|get"}[2m])), 0.000001)
```

HTTP error rate:

```promql
sum(rate(catalog_http_requests_total{status=~"5.."}[2m]))
/
clamp_min(sum(rate(catalog_http_requests_total[2m])), 0.000001)
```

## Radius identifiers

Resource IDs:

- Application: `applications.radius.dev/catalog-demo`
- Catalogue API: `applications.core/catalog-api`
- MySQL: `applications.core/mysql`
- Valkey: `applications.core/valkey`
- Prometheus: `applications.core/prometheus`

Connection IDs:

- API to MySQL: `catalog-api--mysql`
- API to Valkey: `catalog-api--valkey`
- Prometheus to API: `prometheus--catalog-api`

These are logical contract IDs. Phase 2 `app.bicep` authoring must either produce them directly or provide a deterministic mapping.

## Diagnostic JSON response

The adapter returns one document per application/environment/window:

```json
{
  "schemaVersion": "1.0",
  "applicationId": "applications.radius.dev/catalog-demo",
  "environmentId": "demo-kubernetes",
  "generatedAt": "2026-09-15T20:00:00Z",
  "window": {
    "start": "2026-09-15T19:58:00Z",
    "end": "2026-09-15T20:00:00Z",
    "stepSeconds": 5
  },
  "status": "complete",
  "diagnostics": [
    {
      "id": "catalog-http-p95",
      "target": {
        "kind": "resource",
        "radiusResourceId": "applications.core/catalog-api"
      },
      "metric": "http.request.duration.p95",
      "displayName": "Catalogue request p95",
      "unit": "seconds",
      "currentValue": 0.312,
      "baseline": {
        "value": 0.08,
        "start": "2026-09-15T19:50:00Z",
        "end": "2026-09-15T19:52:00Z"
      },
      "severity": "critical",
      "thresholds": {
        "warning": 0.15,
        "critical": 0.25,
        "direction": "above"
      },
      "promql": "histogram_quantile(0.95, sum by (le) (rate(catalog_http_request_duration_seconds_bucket{route=~\"products.list|products.get\",status=~\"2..\"}[2m])))",
      "sampleCount": 8400,
      "stale": false
    },
    {
      "id": "mysql-dependency-p95",
      "target": {
        "kind": "connection",
        "radiusConnectionId": "catalog-api--mysql",
        "sourceRadiusResourceId": "applications.core/catalog-api",
        "destinationRadiusResourceId": "applications.core/mysql"
      },
      "metric": "dependency.duration.p95",
      "displayName": "MySQL dependency p95",
      "unit": "seconds",
      "currentValue": 0.274,
      "baseline": {
        "value": 0.04,
        "start": "2026-09-15T19:50:00Z",
        "end": "2026-09-15T19:52:00Z"
      },
      "severity": "critical",
      "thresholds": {
        "warning": 0.1,
        "critical": 0.2,
        "direction": "above"
      },
      "promql": "histogram_quantile(0.95, sum by (le) (rate(catalog_dependency_request_duration_seconds_bucket{dependency=\"mysql\"}[2m])))",
      "sampleCount": 4200,
      "stale": false
    }
  ],
  "errors": []
}
```

## Field rules

- `schemaVersion`: required semantic contract version.
- `applicationId`: required Radius application ID.
- `environmentId`: required adapter-defined deployment environment ID.
- `generatedAt`: required RFC 3339 timestamp.
- `window`: required inclusive query window and step.
- `status`: `complete`, `partial`, or `unavailable`.
- `diagnostics`: zero or more measurements.
- `diagnostics[].id`: stable diagnostic definition ID.
- `target.kind`: `resource` or `connection`.
- `radiusResourceId`: required for resource targets.
- `radiusConnectionId`, `sourceRadiusResourceId`, and `destinationRadiusResourceId`: required for connection targets.
- `metric`: stable semantic metric ID independent of display text.
- `currentValue`: numeric value for the requested window; omitted when unavailable, never replaced with zero.
- `baseline`: optional comparison value and exact window.
- `severity`: `healthy`, `info`, `warning`, `critical`, or `unknown`.
- `thresholds`: optional values used to derive severity. `direction` is `above` or `below`.
- `promql`: exact query used to obtain the current value.
- `sampleCount`: number of source observations when available.
- `stale`: true when the newest contributing sample exceeds the adapter staleness threshold.
- `errors`: structured adapter/query/mapping errors. Partial data must use `status: partial`.

## Severity and missing-data behavior

- Thresholds are diagnostic configuration, not hard-coded in the service.
- Baseline deviation may be displayed, but severity is determined by explicit thresholds so repeated demos are stable.
- No series, query failure, ambiguous mapping, and stale samples produce `severity: unknown`, not `healthy`.
- An unavailable Valkey series in the baseline is expected and should not create a Valkey target because the baseline Radius graph has no Valkey resource or connection.
- After the cache change, both `catalog-api--valkey` and `catalog-api--mysql` diagnostics may be present. Lower MySQL traffic is evidence of cache effectiveness, not evidence that the MySQL connection disappeared.
