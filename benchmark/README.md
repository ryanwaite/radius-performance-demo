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
validators. The Compose trial driver landed separately as Increment 2. It
isolates the application under test, not the agent.

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
not from a public registry listing. `uv` itself is pinned at 0.12.15 for this
reason: 0.12.18 exists publicly but CFS does not serve it yet.

### CI installs from public PyPI, by hash

GitHub-hosted runners cannot reach CFS. It authorises by **network context
rather than by credential** and returns 401 there, so adding a token would not
fix it. CI therefore installs from public PyPI — using hashes exported from the
CFS-resolved lock.

That works because CFS serves byte-identical artifacts, so a hash minted
against CFS validates against pythonhosted. This is checked, not assumed:

```bash
cd benchmark
python3 tools/verify_lock_hashes.py
```

It asserts that every hash in `requirements-ci.txt` is a digest public PyPI
publishes for that exact version, and fails otherwise. At the time of writing
it passed for all 446 hashes across 88 packages. It compares published metadata
and downloads no artifacts, so it runs on networks that cannot reach
`files.pythonhosted.org`.

Regenerate `requirements-ci.txt` whenever `uv.lock` changes, from `benchmark/`:

```bash
uv export --frozen --offline --format requirements.txt --all-groups --no-emit-project --output-file requirements-ci.txt
```

Commit the result. `--frozen` forbids re-resolution and `--offline` forbids
reaching an index, so the export reflects the lock and nothing else. The file
records this command in its own header, so the committed file states how to
reproduce it.

CI runs the identical command and fails if the committed file differs, which
catches a lock change that skipped this step. CI installs with
`--require-hashes --no-deps`, so a file whose hash differs from the lock fails
the install and pip is never allowed to resolve a version of its own.

## Running

```bash
cd benchmark
uv sync
uv run pytest                 # 254 tests, no model calls
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

Real confinement requires an OS boundary around the agent process, which this
harness does not yet establish. The Compose increment does not supply it:
those containers bound the application under test, while the agent stays a
host process outside them.

The runtime sandbox that would supply it is **reachable only after the session
exists, via the experimental `session.options.update`** — so there is a window
between session start and that call in which no policy is in force, which a
runner must close or account for. A spike on SDK 1.0.13 / CLI 1.0.83 with one
model denied every escape that executed. The harness does not enable it yet,
so shell stays disabled for scored runs until it does. See the runtime-sandbox
section of `docs/specs/copilot-radius-experiment-plan.md` for the conditions
that adoption is gated on.

The same capability is absent from the session-creation API. The CLI wire
protocol defines an OS-level `SandboxConfig` — an `enabled` flag,
`userPolicy.filesystem` read-only and read-write path lists, a fail-closed
`allowBypass`, and sandboxed MCP/LSP subprocesses. None of it is exposed on
`CopilotClient.create_session` in the pinned SDK 1.0.13, whose ~80 parameters
include nothing sandbox-related. So this is an **SDK surface gap, not a
missing runtime feature** — confirmed by the spike, which drove the sandbox
successfully through the update call.

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

This design is **under review**: the sandbox spike found that the runtime's
denial text asks the agent not to attempt workarounds, and the agent complies,
so a per-run probe may stop attempting escapes for reasons unrelated to the
harness. That behaviour is unchanged here and is being scoped separately.

What a passing gate establishes is narrow and worth stating exactly: the
handler was wired, it saw real attempts, and it denied them. That is a
**wiring check**. It is not evidence of confinement, and `isolation-gate.json`
says so in `permissionHandlerBasis`.

---

## Sandbox, budgets, and the submit tool (Increment 3)

### The runtime sandbox is applied, and the trial fails if it is not

`create_session` takes no `sandbox_config` parameter, so the configuration is
applied through the experimental `session.rpc.options.update` immediately after
the session exists and **before the first prompt**. That ordering is recorded
as an observation — `apply_sandbox` is handed the harness's own `prompts_sent`
counter rather than asserting the ordering it hopes for.

The window between create and update is real and is not closed by this design.
Nothing is prompted into the session during it, but the session does exist
unconfined for that moment. Closing it needs a `sandbox_config` parameter on
session creation, which the pinned SDK does not expose.

The [plan's five conditions](../docs/specs/copilot-radius-experiment-plan.md)
are enforced as follows.

| Condition | Where |
| --- | --- |
| Applied before the first prompt | `SpikeSession.start`, recorded with `appliedBeforeFirstPrompt` |
| Every tool execution carries `sandboxApplied` | `SandboxGate.observe_tool_execution` |
| Write confinement claimed, read confinement not | `SandboxSettings` docstring; `/etc/hosts` stayed readable in the spike |
| Probes the model cannot decline | `run_escape_probes` drives `session.rpc.tools.execute` directly |
| Toolchain paths declared | `SandboxSettings.readonly_paths` |

Three properties of the gate are worth stating, because each one is a way the
check could have quietly stopped testing anything:

* **`sandboxApplied` arrives as a string.** `"false"` is truthy in Python, so a
  direct truth test would have confirmed every unconfined execution. The gate
  compares against `"true"` and treats anything else as unconfirmed.
* **Absent is not false.** A payload that reports nothing is recorded as `null`,
  not as a denial, because "the runtime said no" and "the runtime said nothing"
  are different findings.
* **A gate with no executions does not pass.** "No execution was unconfined" is
  vacuously true when nothing ran, so the result carries the observed and
  confirmed counts and marks the no-evidence case `vacuous`.

Escape probes run through the tool pipeline rather than through the model. This
matters because the model declines probes after a denial, which would make the
check decay to nothing. It is close to free but not free: the probes themselves
consume no model calls, though one turn is needed first to register the tool
surface, because the runtime reports no tools until a turn has run. Ground truth is the filesystem — whether the file appeared, or whether
the canary token reached the transcript — never the exit status. A `perl` write
exited 0 during the spike while writing nothing.

The probe set spans two classes, and the distinction is narrower than it looks.
The `write-outside-variable` probe assembles the full path at runtime, so the
complete target never appears as one token, but **the parent directory is still
spelled out literally**. Only the base64 probes contain no path substring at
all. `conceals_path` records which is which, and a test pins it so a partially
literal probe cannot be relabelled as a concealed one.

### What the live run established, and the false pass it caught first

Run against SDK 1.0.14 / CLI 1.0.87 on `gpt-5.6-sol`. Evidence is outside the
repo, in `radius-perf-eval-artifacts/sandbox-wiring-check/`.

The sandbox **is applied and is enforced**. `options.update` returned
`{"success": true}` before the first prompt, every escape probe failed with
`Operation not permitted` — an OS-level EPERM, not a harness message — and the
outside directory was still empty afterwards. Every probe reported
`sandboxApplied=true`, and the in-workspace control write succeeded, so the
denials are the sandbox refusing rather than the harness failing to run. Both
base64 probes were denied too, which closes the concealed-path class the
earlier spike could not test because the model declined to run it.

**The first run reported five clean denials while nothing reached the sandbox.**
The static screen rejects any absolute path, and a harness probe must name its
target absolutely, so the screen denied all five probes *and* the in-workspace
control. A harness denial and a sandbox denial both leave the file absent, so
ground truth agreed every time and the result was indistinguishable from perfect
confinement. The positive control is the only reason this was visible: it failed
in exactly the same way, and a control that cannot write inside its own
workspace is obviously broken. `ProbeOutcome.SCREENED` now records that class
separately, it is excluded from the evidence that supports a confinement claim,
and `screen_shell_paths=False` puts the sandbox in the position of being the
only thing that can deny. Scored runs leave the screen on.

A denial is also not counted unless that execution reported
`sandboxApplied=true`. If the sandbox was not in force, something else refused
the command, and that something else is not the control being claimed.

Three findings changed the code:

* **`tools.execute` returns `{textResultForLlm, resultType, sessionLog, error,
  toolTelemetry}`.** There is no `exitCode`, `stdout`, or `stderr`. The first
  implementation read those three, got `None` and two empty strings every time,
  and reported the silence as denials.
* **Harness-driven executions emit no `tool.execution_complete` event.** Six ran
  and one event was recorded — the model-issued one. A gate fed only from the
  event stream would never see a probe, so the flag is read from the result
  object instead.
* **Tools register lazily.** Before the first turn the runtime reports
  `{"tools": []}` and `tools.execute` answers `Tool 'bash' does not exist.`
  Probes therefore cost one turn to register the tool surface, which is not the
  zero-model-call claim made earlier in this document's history.

Read confinement also held — the parent canary was unreadable — but the plan's
claim stays limited to **write** confinement, since one model on one OS is not
the basis for a broader one.

### The static screen is off in shell trials, and the sandbox is the only boundary

The benchmark's static screen matches command *text*. Tested against 38
realistic diagnosis commands with no model calls, it denied 11. Five of those
were `/proc` and cgroup reads such as `cpu.max` — the most direct evidence for
the CPU, memory, and garbage-collection faults we inject — and it also denied
`df -h /`, on the bare `/` argument. Meanwhile it allowed three of five
escape-shaped commands, including `cat $HOME/.ssh/config` and a path assembled
from a shell variable, which the sandbox denies at the OS level. Its error
profile is close to inverted.

It also biases the comparison. The screen is identical in every arm, but the
arms do not depend on it equally: the Radius and document arms can learn some
resource facts from the graph or the document, while the native arm's route to
them is mostly the files the screen blocks. Removing the native arm's substitute
would inflate the measured Radius advantage.

So shell trials run with the screen **off**, and the trial is valid only if
every tool execution reports `sandboxApplied: "true"`. A trial that cannot
confirm that is recorded as `harness_failure` — excluded from scoring and
counted as such, never charged to the agent. There is no allowlist: each entry
would be an unexamined decision about what the agent may look at. The screen
stays **on by default** (`SandboxGate().static_screen == "on"`) everywhere the
sandbox is not confirmed, so the permissive setting has to be chosen rather than
inherited, and `require_evidence=False` cannot buy a vacuous pass in it.

`staticScreen`, `shellEnabled`, and the per-execution confirmations are written
into the trial record, so which boundary was in force is readable from the data
rather than from the config that produced it.

Two caveats:

* **The sandbox result is one model on macOS.** Linux hosts are unverified, and
  so are other model pins. The per-execution `sandboxApplied` requirement is
  what keeps this a per-trial claim rather than a general one; re-verify
  whenever the SDK, CLI, or model pin changes.
* **The setting is fixed before the pilot and does not change during it.** It
  changes what an agent can reach, so pilot and scored runs must not straddle
  the change.

### Budgets

30 minutes of wall clock or 100 tool calls, whichever comes first, at high
reasoning effort, identical in every arm (`SessionBudget.plan_default`).
Exhaustion terminates the trial and scores as a failure.

`plan_default` deliberately leaves the model-request cap unset. The plan caps
tool calls, not model calls, and an undeclared third cap would let trials end
for a reason no arm agreed to — which would appear in the results as a
between-arm difference in exhaustion rate caused by the harness rather than by
the treatment.

### The submit tool

Fixed fields: `faultPresent`, `causalCategory`, `component`, `evidence`,
`confidence`, `remediation`. The tool is **terminal on success**, so an accepted
submission ends the trial on the agent's own answer. A rejected submission
returns a failure, which leaves the loop running so the model can correct it.

`causalCategory` is a closed list of ten, each with a one-line definition
carried in the tool description and **identical in every arm**. The definitions
are the point: without them a slow database satisfies both `dependency_latency`
and `slow_database`, and a database lock satisfies both `slow_database` and
`lock_contention`, so an arm could be marked wrong for choosing the other true
label and part of the measured difference between arms would be a difference in
guessing the scorer's taste. Each definition carves on **where the delay or
failure originates**, and two tie-breaks are stated to the agent verbatim. The
tuple is derived from the definitions mapping, so a category cannot be added
without one.

`component` is defined as **the component whose behaviour must change to fix
the fault**, and is deliberately unconstrained. The canonical names are the
application's service inventory, which is part of what the Radius graph and the
architecture document supply to *their* arms; listing them in the schema would
supply the inventory to the native arm too, shrinking the difference the
experiment exists to measure. An arm answers in its own vocabulary — service
name, container name, or Radius resource ID — and **a table the fixture
supplies** resolves it afterwards. The harness carries no built-in mapping, and
a test scans the module namespace to prove it.

The rejection for an unknown component says only `unknown component; name a
service from the application`. It names no valid component and does not vary
with the guess, so a throwaway guess cannot buy the inventory and the map cannot
be probed by bisection. `submit_tool_schema()` takes **no fixture argument at
all**, which makes the leak unreachable rather than merely absent; a test pins
the signature. The detector used by the no-leak tests is itself given a positive
control, because an absence passes just as happily when the detector has stopped
working.

Two decisions the brief did not specify:

* **A no-fault submission must omit `causalCategory`, `component`, and
  `remediation`.** A control trial that reports no fault cannot also name its
  cause.
* **Rejected attempts are retained and counted.** "Could not diagnose" and
  "could not express" are different findings, and an arm that failed entirely on
  rejected submissions would be a harness artefact rather than a weak treatment.

A sandbox-gate failure marks the trial **invalid** rather than scoring it as a
wrong answer, so a broken harness cannot masquerade as a weak arm.

### Context window and compaction

`maxPromptTokens` is normalized into `usage-normalized.json`, along with peak
prompt tokens, `toolTokenCount`, and the headroom ratio.

The ratio is reported twice. Under the `UNKNOWN` overlap policy it is not
established whether `inputTokens` already includes `cacheReadTokens`, and that
changes the real prompt size, so the harness records the runtime's own figure
and the upper bound instead of guessing one. If a run reports several context
limits, the smallest is used: a mid-run switch to a narrower window should not
be reported at its most flattering.

Compaction and truncation are captured as flags from `session.compaction_start`,
`session.compaction_complete`, and `session.truncation`, with **emission
unverified**. No trial has yet filled a 272k context window, so a zero count
here says nothing about the SDK — it says our prompts were small. This is the
opposite of the `requestSandboxBypass` case, where the harness created the
condition and the field still stayed silent.

`compaction_tokens_used` is recorded as **its own usage line** and is not folded
into the trial totals. The SDK describes it as "aligned with assistant.usage
format", meaning the compaction summary is itself a model call; whether that
call *also* appears as an `assistant.usage` event is unknown. Folding it in
would double count if it does, and ignoring it would undercount if it does not.

#### The forced-compaction control (designed, not run)

Before any report states compaction rates, one compaction must be forced on
each scored model pin, so that a zero is a measurement rather than a silence.

`session.rpc.history.compact` triggers compaction directly, which makes the
control far cheaper than filling a context window to 200k tokens. Its `Trigger`
enum carries `MANUAL`, and the SDK persists an organically triggered compaction
**without trigger attribution**, so a control-induced compaction is
distinguishable from a real one by `trigger == "manual"` against an absent
trigger. `collect_compaction` counts the two separately.

The limit of this control must be stated with its result: it proves the harness
**captures** compaction events. It does not prove that organic compaction fires
at any particular threshold. Establishing that still needs a filled context
window, which is why the live run waits on the user.

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
(`"signedOff": true`) only if every gate in `REQUIRED_GATES` was **recorded** and
**passed**. A failed gate does not degrade the run to a warning — it un-signs the
manifest, and `trials.py` refuses to count that cycle.

Requiring presence, not just absence of failure, is the load-bearing half. Sign-off
used to mean "some gate ran and none failed", which made a nearly empty manifest a
passing one: when a suite was interrupted mid-run, seven cycles that never reached
`compose up` still reached teardown, recorded `cleanup-verified`, and signed off on
that single gate while reporting `readinessVerified: false`, `seedCount: 0` and no
images at all. That is the same vacuity as a negative test that passes because its
setup was a no-op — the check reported success because almost nothing it checks had
run. Absence is now failure, and `missingGates` names what was never recorded.

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
`environment-manifest.json` and `measurements.json`. A suite additionally
writes `benchmark/results/<suite-id>/determinism-report.json` and
`provenance.json` — the commit it ran against and whether the worktree was
modified.

For a holdout — a suite run against gates frozen beforehand — use the harness,
which refuses a dirty tree and exits non-zero when the criterion is not met:

```bash
python3.12 benchmark/tools/run_holdout.py "$PWD" /path/outside/the/repo holdout-01
```

Point the artifact directory outside the worktree. Results are gitignored, and
an earlier holdout's evidence was lost to a routine clean-up.

Run the Docker-free tests with:

```bash
cd /path/to/repo && python3.12 -m unittest discover -s benchmark/tests -t benchmark
```

### What CI does and does not cover

The `python` CI job collects and runs **all five** test modules — `test_driver`,
`test_events`, `test_isolation`, `test_usage`, `test_versions` — for **254 tests**.

It previously ran only `test_driver` (120 tests). The other four import
`github-copilot-sdk` and `inspect-ai`, which were reachable only through CFS, and CFS
authorizes by **network context rather than by credential**: it resolves from a managed
machine and returns 401 to a GitHub-hosted runner, so no token would have fixed it.

CI now installs those packages from **public PyPI**, using hashes exported from the
CFS-resolved `uv.lock` — see [CI installs from public PyPI, by hash](#ci-installs-from-public-pypi-by-hash).

A job that quietly omits a test set reads as coverage it does not have, so the job
still reports per-module counts in the step summary and fails if any module collects
zero tests or if fewer tests ran than were collected. Collection is checked separately
from execution, because an import error reports nothing for the module that broke while
the run stays green on the rest.

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

### Stalls are timestamped

Each stall carries `wallClock` and `suiteElapsedSeconds` alongside its latency. Two
stalls at a similar elapsed time would point at periodic work — a Docker Desktop VM
task, a macOS background job, a MySQL purge or checkpoint — and two at unrelated times
would not. With n=2 across two suites there is no pattern worth acting on; recording the
timestamps only makes the claim testable later, at no cost. It changes no gate and no
threshold. The wall clock is what lets a stall be lined up against a host log; the
suite-elapsed clock is what makes suites that started at different times comparable.

### A cycle only counts if the host was awake

`time.time()` advances across macOS sleep and `time.monotonic()` does not, so their
divergence over a cycle is time the process was not running. Cycles exceeding
`MAX_HOST_SUSPENSION_SECONDS` (5s) fail, and `hostSuspension` in the report states the
observed maximum whether or not anything tripped.

This exists because an earlier holdout attempt ran with the lid closed on battery. The
host entered clamshell sleep 90 seconds into cycle 3 and alternated sleep and darkwake
for the next 109 minutes. That cycle passed all 17 gates and reported a throughput
figure computed over a wall-clock window the machine had mostly slept through, and
nothing in the driver noticed. Note that AC power sets `sleep 0` while battery sets
`sleep 1`, and clamshell sleep on battery is unconditional — so run suites on AC.
`caffeinate` does not help: it holds `PreventUserIdleSystemSleep`, not
`PreventSystemSleep`. A suite-level suspension figure is also reported, since the
per-cycle measurement cannot see a host that slept in the gap between cycles.

### Power state is recorded, not gated

`hostPower` records AC or battery, battery percentage, and any CPU speed limit, at
both the start and the end of a suite, with `changedDuringSuite` when the two differ.
Nothing fails on it. It is there because the holdout's last four cycles drifted in one
direction — incident throughput 7.914 → 7.886 → 7.857 → 7.829, incident p50 rising
0.5058 → 0.5112, and the suite's lowest healthy throughput in the final cycle — on a
host that happened to be on battery. Power and thermal state are the first thing to
suspect for a monotonic drift, and they are unreconstructable once the run is over.
Whether that drift is throttling or coincidence is unresolved; recording the state is
what makes the next suite able to answer it.

### A note on the cache

`CACHE_ENABLED` defaults to `false`, which keeps MySQL on the hot path — necessary,
because a warm cache over a 10-row fixture would mask the `mysql-pool-delay` incident
almost entirely. Valkey still runs and is still verified empty before load, and
`cacheHitRatio` / `valkeyP95Seconds` are therefore legitimately `null` rather than `0`,
per the telemetry contract. Incident variants that exercise the cache will want to flip
this.
