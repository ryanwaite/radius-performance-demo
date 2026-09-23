# `radius_perf_eval` — Copilot SDK instrumentation (Increment 1)

Control-plane code for the Radius performance benchmark. It starts an
instrumented, isolated GitHub Copilot SDK session and records everything
needed to account for a trial in time, tokens, tool calls, and AI credits.

> **This package must never appear inside a fixture given to the agent.**
> It contains benchmark control logic and would be answer leakage. See
> "Repository fixture and workspace isolation" in
> `docs/specs/copilot-radius-experiment-plan.md`.

This increment deliberately excludes the Compose trial driver, fixtures,
Inspect tasks, scenarios, and validators.

## Layout

| Module | Responsibility |
|---|---|
| `copilot.py` | Session lifecycle, isolation policy, budgets, model catalog |
| `events.py` | Append-only JSONL event log, monotonic clock, tool-call timeline |
| `usage.py` | Raw + normalized usage, reconciliation (SDK-free, unit-tested) |
| `versions.py` | Provenance and CFS package-supply-chain verification |
| `smoke.py` | Exit-criterion driver (`radius-perf-smoke`) |
| `isolation_probe.py` | Deterministic fail-closed workspace isolation probes |

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
  user-initiated calls are charged as premium requests: summing every call's
  `cost` overcounts. Measured over a 4-turn session that produced 8
  `assistant.usage` events alternating `user`/`agent`, each `cost: 1.0` —
  filtering on `initiator == "user"` gives **4.0**, matching the runtime's
  `totalPremiumRequestCost` of 4.0, while the unfiltered sum gives 8.0. That
  session also rules out the rival reading that only the *first* call is
  charged, which would have predicted 1.0. Verified for the pinned runtime and
  model family; re-verify on change.
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

**There is no OS or process boundary.** The SDK spawns its pinned CLI as a
host child process, and the workspace is an ordinary host temporary directory.
Everything below rests on an advisory, in-process permission handler: a denial
is this harness declining a request, not the kernel refusing an operation.

Tool-mediated file reads and writes are screened against the assigned
temporary workspace, and symlink escapes are defeated by resolving paths
before checking them. That holds for access the runtime routes through the
permission API, and only while shell is disabled — a shell command can read or
write anywhere the host user can, and the screen below is not a reliable
barrier.

**Shell commands cannot be screened reliably through the permission API.**
Three fields of `PermissionRequestShell` are unreliable on CLI 1.0.83, and two
of them actively mislead:

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

Real confinement would require executing the agent **inside a mount
boundary** — a dedicated agent runner, which is not built. The Compose
increment does not supply it: those containers bound the application under
test, while the agent stays a host process outside them. Until that runner
exists, shell should remain disabled for scored runs.

The capability does exist one layer down, and is only out of reach. The CLI
wire protocol defines an OS-level `SandboxConfig` — an `enabled` flag,
`userPolicy.filesystem` read-only and read-write path lists, a fail-closed
`allowBypass`, and sandboxed MCP/LSP subprocesses. None of it is exposed on
`CopilotClient.create_session` in the pinned SDK 1.0.13, whose ~80 parameters
include nothing sandbox-related. So this is an **SDK surface gap, not a
missing runtime feature**, and that is worth confirming before a runner is
scoped from scratch.

### Handler denial is a run gate, not a metric

Because the gap is invisible to tests written against the API, the handler is
proven to deny by a **live escape probe in every scored run**, and the verdict
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

What a passing gate establishes is narrow and worth stating exactly: the
handler was wired, it saw real attempts, and it denied them. That is a
**wiring check**. It is not evidence of confinement, and `isolation-gate.json`
says so in `permissionHandlerBasis`.
