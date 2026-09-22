# `radius_perf_eval` — benchmark control plane

Two increments share this package:

- **Copilot SDK instrumentation** (Increment 1) — documented below.
- **The deterministic Compose trial driver** (Increment 2) — documented in
  [Compose trial driver](#compose-trial-driver-increment-2) at the end of this file.

## Copilot SDK instrumentation (Increment 1)

Control-plane code for the Radius performance benchmark. It starts an
instrumented, isolated GitHub Copilot SDK session and records everything
needed to account for a trial in time, tokens, tool calls, and AI credits.

> **This package must never appear inside a fixture given to the agent.**
> It contains benchmark control logic and would be answer leakage. See
> "Repository fixture and workspace isolation" in
> `docs/specs/copilot-radius-experiment-plan.md`.

Increment 1 deliberately excludes fixtures, Inspect tasks, scenarios, and
validators. The Compose trial driver referenced above as a security control
landed separately as Increment 2.

## Layout

| Module | Responsibility |
|---|---|
| `copilot.py` | Session lifecycle, isolation policy, budgets, model catalog |
| `events.py` | Append-only JSONL event log, monotonic clock, tool-call timeline |
| `usage.py` | Raw + normalized usage, reconciliation (SDK-free, unit-tested) |
| `versions.py` | Provenance and CFS package-supply-chain verification |
| `smoke.py` | Exit-criterion driver (`radius-perf-smoke`) |
| `isolation_probe.py` | Deterministic fail-closed workspace isolation probes |
| `environment.py` | Trial lifecycle, verification gates, signed-off manifest |
| `compose.py` | Compose project control and teardown verification |
| `incidents.py` | Reversible incident injection + independent verification |
| `load.py` | Stdlib closed-loop load generator |
| `telemetry.py` | Prometheus baseline capture (canonical PromQL) |
| `manifest.py` | Environment manifest, fixture hashing, sign-off |
| `trials.py` | Repeated-cycle determinism harness and declared tolerances |
| `images.py` / `docker_cli.py` | Digest pinning and Docker CLI plumbing |
| `cli.py` | `radius-perf-eval-env` (`doctor`/`trial`/`determinism`/`cleanup`) |

## Packages come from CFS only

Per "Package supply chain compliance" in the experiment plan, all packages are
acquired through Microsoft Central Feed Services. `pyproject.toml` commits a
**single** default index so CI and container builds do not depend on developer
machine settings:

```toml
[[tool.uv.index]]
name = "cfs"
url = "https://packagefeedproxy.microsoft.io/pypi/simple"
default = true
```

Never add a second index or an `--extra-index-url`; multiple indexes are
treated as a dependency-confusion risk. `versions.package_supply_chain()`
re-reads this configuration and scans the lockfile for public registries, so
compliance is a *verified property* recorded in every run's provenance rather
than an assumption. `tests/test_versions.py` fails if it regresses.

CFS quarantine lag means pins must be chosen from what CFS actually serves,
not from a public registry listing.

## Running

```bash
cd benchmark
uv sync
uv run pytest                 # 133 tests, no model calls
uv run radius-perf-smoke --model gpt-5.4 --output ../artifacts/smoke
```

The smoke driver exits non-zero unless every exit-criterion condition holds.
`artifacts/` is gitignored; a curated subset is committed under
`evidence/smoke-run/` (the full `events.jsonl` is omitted for size — see
`evidence/smoke-run/events-summary.json`).

## Accounting rules

These are the invariants the unit tests exist to defend:

- **Null, never zero.** A field the provider does not report is `null`. A
  reported zero stays `0`.
- **Never sum overlapping fields.** `reasoningTokens` is a subset of
  `outputTokens`; `inputTokens` includes `cacheReadTokens`. Totals are derived
  by partition, never addition.
- **Never double count.** `session.usage.getMetrics` `agentMetrics` is a
  breakdown of the same spend as `modelMetrics`, not an addition. Only
  user-initiated calls are charged as premium requests.
- **Reconcile, never merge.** Token totals come from exactly one source,
  recorded in `usageSource`. The other source is retained verbatim and
  compared; disagreement is reported, not resolved.
- **No invented money.** Copilot reports AI credits and premium-request
  multipliers, not USD, so `cost.actualUSD` and `cost.estimatedUSD` stay
  `null`.

The `assistant.usage` event stream is preferred for token totals because it
carries the provider's billed token partition (`copilotUsage.tokenDetails`),
which yields a real uncached-input figure under prompt caching.
`session.usage.getMetrics` reports no such breakdown and is experimental, so
it serves as the independent cross-check.

`assistant.usage` is **ephemeral and never replayed** — the recorder is the
only durable copy.

## Isolation: what actually holds

File reads and writes are confined to the assigned temporary workspace, and
symlink escapes are defeated by resolving paths before checking them.

**Shell commands cannot be confined through the permission API.** Three
fields of `PermissionRequestShell` are unreliable on CLI 1.0.83, and two of
them actively mislead:

| Field | For `echo probe > /tmp/x` | Consequence |
|---|---|---|
| `possible_paths` | `[]` (also `[]` for `cat /etc/hosts`) | no path to check |
| `has_write_file_redirection` | `False` | a check named for this case never fires |
| `command_segments[*].full_command_text` | `"echo probe"` | truncated at `>`, so no outside path is visible |

The screen therefore reads the **top-level `full_command_text`** and nothing
else. The segment list is the more natural-looking choice precisely because it
appears tokenized and structured — do not switch to it. A regression test
plants a command whose segments differ from the full text and proves the
screen still denies it.

Shell is **disabled by default**; when enabled, commands are screened
statically for absolute, `~`, and `..` path tokens.

**That screen is defence in depth, not confinement.** It matches escape-shaped
*tokens*, so any construction that removes the literal token defeats it.
`isolation-report.json` records this boundary explicitly under
`knownLimitations`: of six limitation probes, three defeat the screen
(base64-encoded, interpreter-constructed, and variable-assembled paths). Those
probes are observations, never assertions, and are excluded from
`allFailedClosed` so a lucky denial cannot read as proof.

Real filesystem confinement requires a sandbox that the agent's own processes run
inside. **The Compose environment in Increment 2 is not that sandbox**, and an
earlier version of this file said it was. The Copilot CLI and its tools run as
host processes against a host temporary directory; the containers hold the
application under test, not the agent. Their mounts bound what the *application*
can reach, which is worth having for the data plane, but they say nothing about
what an agent shell can reach.

No agent sandbox exists yet. Shell-enabled scored runs stay blocked until one
does.

### Confinement is a run gate, not a metric

Because the gap is invisible to tests written against the API, confinement is
established by a **live escape probe in every scored run**, and the verdict
**stops the run**. `evaluate_isolation_gate` requires every check to be
*affirmatively* satisfied:

- static probes ran and all failed closed;
- a live probe is present — a scored run may not skip it;
- the live probe is **non-vacuous**: the agent actually attempted an escape;
- zero shell requests approved, no canary leak, no absolute-path write.

Missing evidence fails. A run whose agent never attempted an escape fails as
vacuous rather than passing trivially. The gate is enforced inside `run_smoke`,
so a programmatic caller cannot bypass it by not checking an exit code, and it
raises only *after* the run record is written so a failed run stays auditable.
`--skip-live-escape-probe` explicitly downgrades the run to `scored: false`,
recorded in the artifact, so the degradation is visible rather than silent.

---

## Compose trial driver (Increment 2)

This package builds and destroys the environment that a scored benchmark trial runs
in. Every trial gets its own Compose project, its own volumes, its own host ports and
its own throwaway credentials, and every environmental property the experiment depends
on is verified against reality before any measurement is taken.

The point is narrow: if trial A and trial B differ, the difference must come from the
treatment (Radius-enabled repo vs native repo), not from environment drift. So the
driver refuses to hand back an environment it could not prove.

See `docs/specs/copilot-radius-experiment-plan.md` for the experiment this serves and
`docs/specs/telemetry-contract.md` for the canonical PromQL reproduced in
`telemetry.py`.

### Why not just use `docker-compose.yml` / `make local-up`?

The repo-root Compose file is a developer convenience and is disqualified as a trial
harness on four counts:

| Property | Root `docker-compose.yml` | Trial driver |
| --- | --- | --- |
| Host ports | fixed 8080/3306/6379/9090 | Docker-assigned ephemeral, loopback-bound |
| Volumes | one shared `mysql-data` | per-run, per-project, destroyed after |
| Images | floating tags (`mysql:8.4`) | digest-pinned (`mysql@sha256:...`) |
| Credentials | hardcoded in the file | generated per run, never committed |

Fixed ports alone make concurrent trials impossible. A shared volume alone makes trial
N observable from trial N+1. The driver is a separate path on purpose; the root
Compose file is left alone for humans.

### Lifecycle

```
pin images -> create -> verify start state -> measure (healthy)
           -> inject incident -> verify incident -> measure (incident)
           -> [revert -> verify reverted] -> destroy -> verify cleanup
```

Each stage appends gates to an environment manifest. The manifest is signed off
(`"signedOff": true`) only if **every** gate passed. A failed gate does not degrade the
run to a warning — it un-signs the manifest, and `trials.py` refuses to count that
cycle.

### Verified properties

Gates recorded per run, all checked against the live system rather than assumed:

- `image-pinned:<service>` — the container's running image ID equals the pinned digest.
- `resource-limits:<service>` — `HostConfig.NanoCpus` / `HostConfig.Memory` match the
  declared spec. The incident depends on constrained resources, so unconstrained
  containers would silently invalidate the scenario.
- `environment-variables:catalog-api` — read back from the daemon, not from the app.
- `application-readiness` — HTTP readiness against the app and Prometheus, plus
  `up{job="catalog-api"}`. `up --wait` only proves containers are healthy; it does not
  prove the app can serve or that Prometheus is scraping it.
- `mysql-seed-rows` — exact row count queried from MySQL.
- `valkey-empty` — `DBSIZE` is 0.
- `egress-blocked:mysql`, `egress-blocked:valkey` — negative test; outbound DNS from
  the data tier must fail.
- `catalog-api-image-hermetic` — no shell and no package manager in the runtime image.
- `incident-active-verified` / `incident-inactive-verified` — see below.
- `cleanup-verified` — see below.

### Incident injection and independent verification

`mysql-pool-delay` is applied as a Compose overlay
(`compose/incident-mysql-pool-delay.yml`) that overrides three environment variables on
`catalog-api` and nothing else, which makes reversion exact rather than approximate.

The brief required that activation be verifiable from outside the application. Trusting
`/healthz` would be circular — a misconfigured or misreporting app is precisely the
failure this benchmark is trying to detect. So three independent authorities are
consulted, none of which is the app's own self-report:

1. **`docker-daemon`** — `Config.Env` read back via `docker inspect`.
2. **`mysql-server`** — peak concurrent sessions for the app user sampled from
   `information_schema.PROCESSLIST` during a saturating probe. With `pool=2`, MySQL can
   never observe a third session. This is the strongest check: the app cannot fake
   MySQL's own view of its connections.
3. **`external-probe`** — wall-clock single-request latency measured by the driver over
   the network.

Deactivation inverts all three. A unit test asserts that `"application"` never appears
as a verification source.

### Cleanup verification

`down --volumes --remove-orphans` returning 0 is not evidence. `residual_resources()`
checks both Compose labels **and** a name-prefix scan, so a resource whose labels were
stripped or which was created out of band is still caught. `destroy()` retries with
escalating force-removal, and the manifest records `cleanupVerified` only after a clean
scan.

### Image and package policy

All dependencies are baked in at build time. A scored trial fetches nothing, so trial
timing cannot be polluted by a slow or failing package mirror, and a mirror change
cannot alter the fixture mid-experiment.

Microsoft Container Registry is used where an image exists:

- `mcr.microsoft.com/oss/go/microsoft/golang:1.23-bookworm` (builder)
- `mcr.microsoft.com/azurelinux/distroless/base:3.0` (runtime)
- `mcr.microsoft.com/oss/v2/prometheus/prometheus:v3.5.0`

**MCR has no MySQL or Valkey equivalent** (8 path variants probed, all absent), so those
two come from Docker Hub and are digest-pinned. This is a known gap, not an oversight.

The Python package itself has **zero runtime dependencies** — stdlib only — so it needs
no package index at all. Tests use stdlib `unittest` for the same reason.

The Go build is hermetic in both halves: `go mod download` primes the module cache in a
layer keyed only on `go.mod`/`go.sum`, and the compile step then runs with `GOPROXY=off`
so an incomplete cache fails the build instead of quietly reaching the network.
`GOTOOLCHAIN` is pinned exactly rather than left at `auto`, which would otherwise fetch
a different toolchain on demand.

`scripts/verify-hermetic-build.sh` proves this rather than asserting it, and it is worth
reading as an example of how the check can lie. Negative tests pass for the wrong reason
very easily, and this one did — three times:

1. The eviction step wrote to `/root/go/pkg/mod`, but `GOMODCACHE` in the MCR image is
   `/go/pkg/mod`. The `rm` was a no-op and the cache stayed intact.
2. Asserting the cache directory was *empty after* the `rm` did not catch that, because
   `mkdir -p` produces an empty directory at any path, correct or not.
3. Grepping the build log for the control's marker matched the `RUN` command text that
   `--progress=plain` echoes, not the command's output — so the control reported itself
   armed on a step that never ran.

What holds now: the control asserts the cache was **populated before** it was removed
(the only version of the check that a wrong path cannot satisfy), the marker literals
are split across a string concatenation so the grep can only match real output, and the
build failure must carry the specific `module lookup disabled by GOPROXY=off` error —
any other failure is reported as inconclusive rather than as a pass. Re-running the
script with `GOMODCACHE` deliberately pointed at the wrong path makes it fail with
"positive control not armed", which is how the above was confirmed.

Two consequences of the distroless choice are worth knowing before editing
`compose/base.yml`: the Prometheus image has no shell and no `wget`, so its healthcheck
uses `promtool check ready`; and the catalog-api image has no shell at all, so its
readiness must be probed over HTTP from the driver rather than with a container
healthcheck.

### Network posture

Two networks, and the split is a real constraint rather than a preference:

- `data` — `internal: true`. MySQL and Valkey live here with no egress.
- `edge` — catalog-api and Prometheus additionally join this, because **Docker cannot
  publish host ports from an `internal` network** (`docker compose port` returns
  `invalid IP:0`).

So the honest claim is per-service, and the manifest records it that way: the data tier
is verifiably egress-free; the two services that must publish ports are not. The driver
does not claim a blanket egress block it cannot deliver.

The Docker socket is never mounted into any container.

**None of this confines an agent.** Everything in this section bounds the
*application* containers — what the data tier can reach, what the driver
publishes, what is mounted where. The Copilot CLI and its tools run as host
processes, outside all of it. Read the network posture and mount audit as
properties of the fixture's data plane, not as a security boundary around the
thing being evaluated.

### Credentials

MySQL passwords are generated per run with `secrets.token_hex(16)`, prefixed and
alphanumeric-only so they need no escaping inside the Go DSN. They are passed to the
`mysql` client via `--env MYSQL_PWD` so they never appear in a container command line,
and they are never written to disk or committed. They are throwaway, local-only, and
die with the Compose project.

### Usage

Requires Python 3.12+ and a running Docker daemon. No install step.

```bash
cd benchmark

python3.12 -m radius_perf_eval.cli doctor            # is the daemon reachable?
python3.12 -m radius_perf_eval.cli trial --run-id smoke01 --revert
python3.12 -m radius_perf_eval.cli determinism --cycles 10
python3.12 -m radius_perf_eval.cli cleanup           # remove stray radius-eval-* projects
```

Useful flags: `--no-pull` (use local images, skip registry pulls), `--results-dir`,
`--scenario`, `--suite-id`.

Artifacts land in `benchmark/results/<run-id>/`:
`environment-manifest.json`, `measurements.json`, and for a suite,
`<suite-id>/determinism-report.json`.

Run the Docker-free tests with:

```bash
cd /path/to/repo && python3.12 -m unittest discover -s benchmark/tests -t benchmark
```

### Every measured cycle is the same experiment

A suite runs in three parts, and only the third is measured:

1. **Setup** — pull and build every image. Unmeasured.
2. **Warm-up** — one or more full cycles, identical to the measured ones, results
   discarded. These absorb page-cache and layer-cache effects the setup phase does not.
3. **Measured** — ten identical cycles, pulling disabled on all of them.

This is not ceremony. An earlier version pulled images on cycle 1 only, which made it a
cold start rather than a repetition, and the numbers show it plainly:

| cycle | healthy rps | healthy max | healthy stalls | incident max |
|---|---|---|---|---|
| 1 | 226.3 | 0.536s | 58 (1.709%) | 1.010s |
| 2–8 | 273.6–274.8 | 0.039–0.044s | 0 | 0.513s |

Cycle 1 breached the 0.5% stall budget on its own, and averaging it with nine warm
cycles described a population that does not exist. `determinism-report.json` records the
setup and warm-up cycles under `unmeasured` so the distinction is auditable.

### Exit criterion

Ten consecutive cycles must all sign off, all verify cleanup, all verify the incident,
stay inside `DECLARED_TOLERANCES`, keep both phases inside the error budget, and show
the incident actually degrading performance. `determinism-report.json` reports
`exitCriterionMet` plus the measured coefficient of variation per metric, so the
variance is written down rather than assumed.

Tolerances live in `trials.py::DECLARED_TOLERANCES`, each with a stated rationale.
Measured values from the ten-cycle run are in the pull request description.

### Stalls are gated separately, on purpose

The first ten-cycle suite failed on `incident.throughputRps` variance: one cycle
contained a single 1.85s request against a 0.51s normal maximum. With `pool=2` that
outlier halves capacity while it lasts, costing 14 of 176 requests — 8% of the window,
and a 2.536% coefficient of variation against a declared 1.0%.

The fix was to widen the incident measurement window from 22s to 80s, because the
estimator was too small to be stable, not because the bound was too tight. But a wider
window also *dilutes* the stall: the same event moves a 640-sample window by 2% instead
of 8%. Left there, we would have traded a noisy true signal for a quiet blind spot —
the same mistake as reading a 0.000% coefficient of variation as stability when it was
really insensitivity.

So stalls are now counted and bounded directly. A stall is a request exceeding
`stall_factor` (3x) times its own phase's median latency; the threshold is relative
because the healthy phase runs at ~29ms and the incident phase at ~506ms, and no single
absolute number describes both. `MAX_STALL_RATE` (0.5%) bounds the rate per phase, and
`determinism-report.json` carries a `stallBudget` block with per-cycle occurrences and
their magnitudes, so a rare event stays visible as a discrete occurrence rather than
being averaged into the background.

This matters most for the case throughput variance cannot see at all: a stall rate that
is *uniformly* elevated across every cycle degrades all of them equally, so the
coefficient of variation reads 0.000% while the environment is measurably worse.
`tests/test_driver.py::StallDetectionTests` asserts exactly that scenario.

Two further guards, because the relative rule has its own blind spot. A uniformly slower
environment raises its own threshold along with the median and can report zero stalls
while being obviously worse, so each phase also carries a **frozen absolute bound**
(100ms healthy, 1.0s incident) and the report counts excursions past it. And the full
sorted latency sample is written to every cycle's `measurements.json`, so later analysis
can re-derive any threshold rather than inheriting the one chosen here.

The stall definition, the 3x factor and the 0.5% budget were all fitted **after** seeing
the first ten-cycle result. They are frozen in `GATE_PRE_REGISTRATION` and echoed into
every report, and the holdout run changed nothing between freeze and execution — so the
holdout tests the gate rather than continuing to fit it.

### A note on the cache

`CACHE_ENABLED` defaults to `false`, which keeps MySQL on the hot path — necessary,
because a warm cache over a 10-row fixture would mask the `mysql-pool-delay` incident
almost entirely. Valkey still runs and is still verified empty before load, and
`cacheHitRatio` / `valkeyP95Seconds` are therefore legitimately `null` rather than `0`,
per the telemetry contract. Incident variants that exercise the cache will want to flip
this.
