# Implementation Plan

## Phase 1: Runnable performance lab

**Status:** implemented by the initial scaffold.

Choices made:

- Original Go 1.23 service using `net/http`, MySQL as source of truth, and optional Valkey cache-aside.
- Environment-based configuration with bounded list limits, dependency timeouts, graceful shutdown, database pool controls, cache TTL, and cancellable artificial read delay.
- Stable Prometheus HTTP, dependency, and cache metrics.
- Interface-based handler and cache-aside unit tests that need no external services.
- Docker Compose for repeatable local baseline and cache-enabled runs.
- Kustomize base and cache overlay so the topology change is explicit.
- Ephemeral demo data in Kubernetes and obvious non-production credentials.

Milestone exit criteria:

- `gofmt`, `go test ./...`, and `go build ./...` pass.
- Docker Compose configuration renders.
- Both Kustomize variants render, and only the cache overlay introduces Valkey and enables caching.
- Documentation distinguishes implemented behavior from planned Radius/Canvas integration.

## Phase 2: Publishable Kubernetes and Radius deployment

Work:

- Publish multi-architecture API images to GHCR with immutable tags.
- Author `app.bicep` that models the catalogue API, MySQL, Prometheus, Valkey, and explicit connections.
- Align Radius resource identifiers with the telemetry contract.
- Add the GitHub Actions workflow used by the existing Radius Canvas deployment path.
- Configure a target Radius environment and Kubernetes cluster without committing credentials.
- Report workflow state and `kubectl rollout status` in the deploying view.

Milestone exit criteria:

- Baseline and cache-enabled revisions deploy through Radius to the same environment.
- The planned and deployed graphs match the intended resources and connections.
- Rollback to the baseline image/model is documented and rehearsed.

## Decisions required before Phase 2

- Select the demonstration Kubernetes/Radius environment and its non-secret GitHub environment name.
- Decide whether MySQL and Valkey remain in-cluster demo containers or use Radius recipes for managed services.
- Select the immutable GHCR tag strategy the deployment workflow will consume.

These choices affect deployable resource types or external configuration. They do not block Phase 1.

## Phase 3: Telemetry adapter and Canvas overlays

Work:

- Implement the diagnostic adapter defined in [telemetry-contract.md](telemetry-contract.md).
- Add explicit Radius resource/connection mapping configuration.
- Add current, baseline, severity, stale, and unavailable rendering states.
- Expose the same structured evidence to Copilot diagnosis tools.
- Persist or select before/after windows for graph-diff comparisons.
- Add adapter contract tests with recorded Prometheus responses.

Milestone exit criteria:

- A degraded baseline marks the API and MySQL connection with correct evidence.
- A cache-enabled run displays Valkey metrics and a recovered API while retaining the MySQL connection.
- Missing or stale data is visible and never converted into a healthy state.

## Phase 4: Storefront integration and demo hardening

Work:

- Integrate the catalogue API into a recognizable storefront flow inspired by OpenTelemetry Demo without copying its implementation.
- Add a rehearsed one-command environment reset.
- Add saved diagnostic fixtures and a clearly labeled replay mode.
- Tune thresholds using repeated runs in the selected demo environment.
- Write the final presenter checklist and rollback procedure.

Milestone exit criteria:

- Three consecutive rehearsals meet success thresholds.
- The live path and fallback path both complete within the allotted demo time.

## Test strategy

- **Unit:** handler status/body/validation behavior; cache hit, miss, error fallback, and population behavior; configuration parsing.
- **Component:** API with disposable MySQL and Valkey containers; verify seed data, TTL behavior, fallback, and metrics labels.
- **Manifest:** `docker compose config`; `kubectl kustomize` for base and overlay; schema validation in CI when a suitable pinned tool is selected.
- **Performance:** identical k6 scenarios for baseline and warmed cache, with fixed latency and resource settings.
- **Adapter contract:** JSON schema fixtures, PromQL response fixtures, mapping validation, stale data, missing series, and partial dependency failure.
- **End to end:** Radius plan, graph diff, workflow dispatch, rollouts, load, diagnostics, and recovery.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Natural environment variance obscures the bottleneck | Use cancellable `DB_READ_DELAY` and identical load windows |
| Cache warm-up makes comparisons misleading | Separate warm-up and measurement windows |
| Cache addition is incorrectly shown as replacing MySQL | Contract and graph assertions require both edges |
| Metrics and Radius topology drift | Use stable IDs and explicit mapping validation |
| Prometheus or telemetry adapter is unavailable | Surface unavailable state and use labeled rehearsal fixtures |
| Mutable image tags make rollback unreliable | Use immutable tags in Phase 2; `latest` is only a placeholder |
| Demo credentials are reused outside isolation | SECURITY.md and manifests label them as non-production |
| MySQL startup races API startup | Readiness probes and restart behavior; later add deployment orchestration gates |
