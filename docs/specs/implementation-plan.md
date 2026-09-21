# Implementation Plan

The canonical experiment design, treatment definitions, Copilot harness, measures, and research roadmap are in the [Copilot + Radius experiment plan](copilot-radius-experiment-plan.md). This document tracks repository implementation surfaces.

## Phase 1: Runnable performance lab

**Status:** implemented by the initial scaffold.

Implemented choices:

- Original Go 1.23 service using `net/http`, MySQL as source of truth, and optional fail-open Valkey cache-aside.
- Environment-based configuration with bounded list limits, dependency timeouts, graceful shutdown, database pool controls, cache TTL, and cancellable artificial read delay.
- Stable Prometheus HTTP, dependency, and cache metrics.
- Interface-based handler and cache-aside unit tests that need no external services.
- Docker Compose for repeatable local baseline and cache-enabled runs.
- Kustomize base and cache overlay so the topology change is explicit and retains both MySQL and Valkey dependencies.
- Ephemeral demo data in Kubernetes and obvious non-production credentials.

Milestone exit criteria:

- `gofmt`, `go test ./...`, and `go build ./...` pass.
- Docker Compose configuration renders.
- Both Kustomize variants render, and only the cache overlay introduces Valkey and enables caching.
- Documentation distinguishes implemented behavior from planned Radius, Canvas, telemetry, and benchmark capabilities.

## Phase 2: Reproducible environments and Radius model

**Status:** planned.

Work:

- Publish multi-architecture API images to GHCR with immutable tags.
- Author `app.bicep` that models the catalogue API, MySQL, Prometheus, Valkey, and explicit connections.
- Align Radius resource and connection identifiers with the telemetry and agent-evaluation contracts.
- Add a versioned scenario manifest format for seed data, load, incident injection, reset, validators, and expected topology.
- Make Compose and Kubernetes environments resettable to identical known state without manual intervention.
- Implement the first reversible incident injectors: MySQL delay/pool pressure, missing or ineffective cache, and CPU throttling.
- Add the GitHub Actions workflow used by the existing Radius Canvas deployment path.
- Configure a target Radius environment and Kubernetes cluster without committing credentials.
- Report workflow state and `kubectl rollout status` in the deploying view.

Milestone exit criteria:

- Baseline and cache-enabled revisions deploy through Radius to the same environment.
- The planned and deployed graphs match the intended resources and connections.
- A scenario can be provisioned, loaded, validated, and reset repeatedly in Compose and Kubernetes.
- Reset returns seed data, configuration, image versions, cache state, and resource limits to the declared baseline.
- Rollback to the baseline image/model is documented and rehearsed.

## Decisions required before Phase 2

These are recommendations, not recorded decisions:

| Decision | Recommended default |
|---|---|
| Agent execution host and interface | Inspect AI orchestrating fresh GitHub Copilot SDK sessions in an ephemeral runner |
| Initial models | Two or three explicit pinned models available in the user's Copilot account |
| Per-trial budget | 20 minutes, 40 tool calls, and explicit Copilot usage, AI-credit, and cost ceilings |
| Graph payload | Versioned JSON containing Radius topology, stable IDs, code references, and deployment state; telemetry is a separate optional section |
| Applying remediation | Automatically apply only inside an ephemeral sandbox after the agent emits a structured plan and patch |
| Initial target | Docker Compose for fast pilots, followed by an isolated Kubernetes namespace per trial |
| Radius service strategy | In-cluster MySQL and Valkey for deterministic benchmark pilots; evaluate managed recipes later |
| Image tags | Immutable digest or commit-SHA tags; never benchmark with `latest` |

The implementation must keep these values configurable. Final choices depend on runner availability, provider access, cost policy, and the selected Radius environment.

## Phase 3: Agent benchmark MVP

**Status:** planned. Detailed design: [agent-evaluation-spec.md](agent-evaluation-spec.md).

Work:

- Implement the benchmark-facing GitHub Copilot SDK adapter and normalized output schema.
- Implement a headless trial orchestrator that provisions, resets, injects, loads, invokes, records, validates, and rolls back.
- Freeze versioned prompts, repository snapshots, scenario definitions, graph payloads, and hidden validators.
- Run the MVP with one app, three incidents, the native and fully Radius-enabled fixtures, and 2-3 explicit Copilot models.
- Separate diagnosis-only trials from diagnosis-and-remediation trials.
- Produce immutable JSON run records, JSONL event streams, summary CSV, Markdown reports, logs, patches, telemetry windows, and graph snapshots.

Milestone exit criteria:

- Every paired trial starts from an identical seeded incident and differs only in graph access.
- Condition order is randomized and all model/configuration/budget fields are captured.
- Deterministic pass/fail validators run without human interpretation.
- The orchestrator can resume after infrastructure failure without reusing a contaminated environment.
- Pilot results are labeled as benchmark-specific and are not generalized from tiny samples.

## Phase 4: Telemetry adapter, graph ablations, and Canvas overlays

**Status:** planned.

Work:

- Implement the diagnostic adapter defined in [telemetry-contract.md](telemetry-contract.md).
- Add explicit Radius resource/connection mapping configuration.
- Add current, baseline, severity, stale, and unavailable rendering states.
- Expose the same structured evidence to Copilot diagnosis tools and the benchmark graph payload.
- Add benchmark conditions for static graph only, graph plus telemetry overlay, and raw Kubernetes manifests.
- Ensure traces and raw metrics are either available to every compared arm or excluded from every arm.
- Persist before/after measurement windows for graph-diff narration and benchmark validation.
- Add adapter contract tests with recorded Prometheus responses.

Milestone exit criteria:

- A degraded baseline marks the API and MySQL connection with correct evidence.
- A cache-enabled run displays Valkey metrics and a recovered API while retaining the MySQL connection.
- Missing or stale data is visible and never converted into a healthy state.
- Ablation payloads are versioned and do not leak graph-derived conclusions into control prompts.
- Benchmark records can attribute gains to static topology versus telemetry overlay.

## Phase 5: Canvas demo hardening and benchmark expansion

**Status:** planned.

Work:

- Integrate the catalogue API into a recognizable storefront flow inspired by OpenTelemetry Demo without copying its implementation.
- Add a rehearsed one-command environment reset and clearly labeled replay mode for the human demo.
- Expand the scenario catalog to dependency timeout/misconfiguration and misleading correlated symptoms.
- Run larger paired comparisons after the MVP establishes stable variance and validators.
- Add confidence intervals, per-scenario breakdowns, unsafe-change review, and reproducibility audits.
- Tune demo thresholds using repeated runs in the selected environment.
- Write the final presenter checklist and benchmark operations guide.

Milestone exit criteria:

- Three consecutive human-demo rehearsals meet demo success thresholds.
- The live and fallback presentation paths complete within the allotted time.
- Each comparison model has enough paired repetitions to report uncertainty rather than anecdotal wins.
- Published results include scenario, prompt, graph, validator, model, and benchmark versions.

## Test strategy

- **Unit:** handler behavior; cache hit, miss, error fallback, and population; configuration parsing; scenario schema; adapter normalization; score calculation.
- **Component:** API with disposable MySQL and Valkey containers; seed data, TTL, fallback, metrics labels, incident injection, and reset.
- **Manifest:** `docker compose config`; `kubectl kustomize` for base and overlay; Radius model validation; schema validation in CI.
- **Performance:** identical k6 scenarios for paired conditions, with fixed latency, seed, images, resource settings, and measurement windows.
- **Adapter contract:** PromQL fixtures, graph mapping, stale data, missing series, partial failure, and control-payload leakage checks.
- **Benchmark end to end:** scenario provision, randomized condition, agent invocation, tool capture, patch application, hidden validators, rollback, and immutable artifacts.
- **Demo end to end:** Radius plan, graph diff, workflow dispatch, rollouts, load, diagnostics, recovery, and fallback replay.

## Risks and mitigations

| Risk | Mitigation |
|---|---|
| Natural environment variance obscures the bottleneck | Use deterministic incidents, paired trials, fixed windows, and repeated runs |
| Cache warm-up makes comparisons misleading | Separate warm-up and measurement windows |
| Cache addition is incorrectly shown as replacing MySQL | Contract, graph, and validator assertions require both edges |
| Graph conclusions leak into the control arm | Generate control prompts independently and test payloads for graph-derived identifiers/summaries |
| Provider differences overwhelm the graph comparison | Pair conditions within model/version/configuration and report per-model effects |
| Agent changes contaminate later trials | Use ephemeral containers or namespaces and reset from immutable scenario state |
| Metrics and Radius topology drift | Use stable IDs, explicit mapping validation, and versioned graph snapshots |
| Prometheus or telemetry adapter is unavailable | Surface unavailable state; retry infrastructure failures rather than score them as agent failures |
| Mutable artifacts make results irreproducible | Pin images, scenarios, prompts, validators, repository commits, and graph payload versions |
| Automated remediation changes unsafe resources | Restrict credentials/network, cap resources, and apply changes only inside isolated sandboxes |
| Tiny samples produce overstated claims | Run a pilot for variance, then larger paired comparisons with confidence intervals |
| Demo credentials are reused outside isolation | SECURITY.md and manifests label them as non-production |
