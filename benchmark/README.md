# `radius_perf_eval` — benchmark control plane

The benchmark compares the **combined Radius repository experience** against
native and architecture-document repositories. It does not test graphs and
skills separately. Follow the [completion plan](../docs/specs/benchmark-completion-plan.md)
for the remaining work; the Astronomy Shop campaign is not yet runnable end to end.

## Local results dashboard

Open [`dashboard.html`](dashboard.html) directly in a local browser, then choose
**Import campaign JSON**. The page starts empty. It accepts only the versioned
[comparison report contract](../docs/specs/benchmark-completion-plan.md#results-dashboard-and-download-contract),
not the historical smoke evidence or catalogue determinism reports. The
[offline campaign store and exporter](#m2-campaign-bookkeeping-and-export)
produce this contract from a prepared roster and replay-verified attempts.
Production Shop reviewers and the live campaign runner remain unbuilt.

Model and incident filters affect descriptive comparisons, trial rows, and
**Download filtered CSV**. **Download full JSON** always preserves the complete
imported report and its provenance. JSON is lossless; CSV flattens declared
fields and neutralizes spreadsheet formulas. Neither includes raw transcripts
or telemetry files referenced by a digest. Those need the future redacted
campaign artifact export.

The dashboard reports per-model pass rates and matched differences without
claiming significance. It labels pilot/smoke data as non-findings and hides
outcome comparisons while a scored campaign is running. Exclusions, missing
metrics, and missing healthy answers stay visible rather than becoming zeros
or successes. Source evidence and hashes are not authenticated in the browser.

The dashboard needs no dependencies or server. Its automated checks need Python
and Node.js, use the standard libraries only, and run from the repository root:

```bash
DOCKER_HOST=unix:///nonexistent/docker.sock python3 -m unittest discover -s benchmark/tests -p test_dashboard.py -v
```

The checks run the actual embedded JavaScript, import positive and negative
controls, exercise UI events and downloads through DOM doubles, and mutate
validation and denominator guards in memory. The same test module is discovered
by CI. Synthetic inputs are test fixtures, never published benchmark findings.

## Harness increments

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
| `campaign.py` | Prepared three-arm roster, append-only attempt journal, retry reduction, source-replaying report export |

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
uv run pytest                 # no model calls
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

This section describes the smoke harness, `run_smoke`, which runs without the
runtime sandbox. Scored trials run inside the sandbox with the static screen
off; see [Sandbox, budgets, and the submit tool](#sandbox-budgets-and-the-submit-tool-increment-3).

**Without the sandbox there is no OS or process boundary.** The SDK spawns its pinned CLI as a
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

Real confinement requires an OS boundary around the agent process. The Compose
increment does not supply it: those containers bound the application under
test, while the agent stays a host process outside them.

The runtime sandbox supplies it. It is **reachable only after the session
exists, via the experimental `session.options.update`**, so the session applies
it between creation and the first prompt, when it is given `SandboxSettings`.
The smoke harness passes none, so shell stays disabled in smoke runs. A live
wiring check on SDK 1.0.14 / CLI 1.0.87 found every escape probe denied by the
operating system; the locked pair is re-verified before the pilot. See the
runtime-sandbox section of `docs/specs/copilot-radius-experiment-plan.md`.

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
proven to deny by a **live escape probe in every smoke run marked scored**,
and the verdict
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

This design is **settled for scored trials**: the sandbox spike found that the
runtime's denial text asks the agent not to attempt workarounds, and the agent
complies, so a probe issued through the model cannot tell "blocked" from "never
tried". The smoke harness keeps this probe. Scored trials run no probe; each is
gated on every tool execution reporting `sandboxApplied: "true"`, and
harness-driven escape probes run in separate sessions on the same host and pins
before each campaign batch.

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
only thing that can deny. Scored shell trials use the same setting, gated on the
sandbox; see the next section.

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

The gate is fed `tool.execution_start` as well as `tool.execution_complete`. A
command that starts and never completes has still run, and may have run
unconfined — the budget killing a trial mid-command, a session error, and a
dropped event all produce that shape. Feeding the gate only completions would
make exactly that execution invisible, so the trial could pass on "every
execution confirmed" while the one execution worth objecting to never reached
the check. A start with no matching completion is therefore unconfirmed, and
three states stay distinct in the record, because they have different causes:

| `sandboxApplied` | `completed` | meaning |
|---|---|---|
| `"false"` | `true` | the runtime said the sandbox was not applied |
| `null` | `true` | the execution finished and reported no flag |
| `null` | `false` | the execution never finished; confinement unknown |

Completions with no matching start are recorded with `startObserved: false`
rather than normalised away, since they are also a sign the event stream is not
what we think it is.

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

Fixed fields: `faultPresent`, `causalCategory`, `component`, optional `connection`, `evidence`,
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

### M2 answer and outcome contract increment

`connection` is an object with `source` and `target`. Both endpoints use the
fixture's `ComponentMap`, just as `component` does. Direction is preserved.
The answer stores both canonical and submitted endpoint names. Healthy answers
omit the connection. The map rejects conflicting aliases or canonical names
before execution and cannot be changed after construction. Its inventory is
not added to the tool schema or rejection text.

Submission acceptance means **schema-valid**, not causally correct.
`SubmissionRecorder.outcome()` labels this `validationLevel: schema_only`.
Its existing `scoredAsFailure` field still means no accepted submission, not a
causal verdict. Malformed JSON and non-object tool calls remain in submission
history, as do rejected corrections; returned history is a snapshot. This is
submission-call accounting, not durable campaign-attempt storage.

`diagnosis.grade_diagnosis` compares a hidden `ExpectedDiagnosis` with the
submitted fault claim, category and causal target. A component incident requires
that component. A connection incident requires the directed edge and a component
at one of its ends. Every citation goes through a required, incident-owned
`EvidenceReviewer`, including healthy claims. The reviewer reports whether the
signal exists, belongs to the declared causal path or healthy detection coverage,
and supports the observation. It must identify nonempty examined references.
An absent signal, unsupported observation, or correlated non-causal signal fails.
A crashed reviewer is a harness error, not a wrong diagnosis.

This is an **integration contract, not an implemented Shop evidence grader**.
The offline tests use a labelled, controlled latency dictionary and exact
observations. That toy reviewer is test code only. No keyword grader, general
free-text mechanism grader, or production telemetry adapter is supplied. Real
incident activation, healthy detection coverage, within-category mechanisms,
captured-artifact authentication and reviewer versioning still need incident
validators and campaign wiring. The caller must bind the hidden expectation,
reviewer, submission and captured artifacts to the same trial.

`trial_outcome.score_trial` now requires independent diagnosis/evidence,
scope, safety and cleanup results before reporting `validated_success`.
Checks retain what they examined. Missing checks, failed cleanup, adapter errors
and sandbox-gate failures produce `harness_failure`; a known agent result stays
in `agentTerminalClass`. A wrong diagnosis or citation produces
`diagnosis_failure`. Prohibited actions produce `isolation_violation_attempt`
even when an accepted answer is correct. No answer, malformed output, an
explicit refusal and budget exhaustion retain their distinct canonical classes.
Refusal must come from a trusted adapter decision, not a keyword in a transcript.
The existing rule that an accepted answer precedes a later teardown budget stop
is unchanged.

For compatibility, `TrialOutcome.scored` still means success; `valid` identifies
agent results, including failures, rather than harness failures. Neither field
defines a campaign denominator. Historical smoke/session completion labels and
catalogue environment/determinism records retain their development-fixture
meaning and are not comparison reports. In particular, the SDK adapter's
historical `validated_success` completion label is not trusted as a diagnosis.
There is no remediation scorer in this increment.

The next increment below implements campaign bookkeeping and the report
boundary. M2 remains open for production incident reviewers and their independent
mechanism, activation and healthy-detection controls. Neither increment changes
M1 internals.

With the locked benchmark environment restored, run the offline contracts and
guard mutations from the repository root:

```bash
DOCKER_HOST=unix:///nonexistent/docker.sock benchmark/.venv/bin/python -m pytest benchmark/tests/test_submit_tool.py benchmark/tests/test_trial_outcome.py benchmark/tests/test_diagnosis.py benchmark/tests/test_m2_mutations.py -q
```

Mutation controls remove guards and call wiring in fresh local Python processes.
They do not start agents or Docker, and a collection/import failure does not
count as a killed mutation.

### M2 campaign bookkeeping and export

`campaign.CampaignStore` prepares the entire nonempty assignment roster before
execution. It requires exactly one `native`, `architecture` and `radius` row per
matched set and unique logical `runId` values. Preparation returns a SHA-256
receipt. Every subsequent open requires that receipt and verifies the exact
prepared bytes, including roster order. Preparation cannot replace an existing
database. Keep the receipt outside the database.

The preparation object has `schemaVersion: radius-campaign-v1`, `campaign` and
`assignments`. Campaign fields are `id`, `phase`, `benchmarkCommit` and
`analysisPlan`, as in the report contract. Each assignment has the report's
`runId`, `pairId`, `arm`, `model`, `incident`, `seed`, `configurationId` and
`expectedFault`, plus `blockId`, `verifierId` and `verifierDigest`.
Members of a matched set must agree on every non-treatment field, including
block and verifier identity. These are operator-curated **public identifiers**,
not log excerpts, credentials or free-text labels. The store rejects extra
fields, whitespace/control characters, markup and formula prefixes in these
identifiers. An identifier-shaped secret still requires operator review.
`configurationId` identifies frozen non-treatment inputs; checking the actual
runtime against those inputs remains integration work.

The store is one operator-only SQLite database, outside the repository. Its
canonical JSON event bytes form a hash chain. Transactions use full synchronous
commits; SQL triggers prohibit updates and deletion. `start(run_id)` appends an
attempt and returns its `Binding`. `capture(binding, name, bytes)` appends each
raw source with a checksum **before** judgment. Captured source names cannot be
replaced. `finish(binding)` replays the trusted verifier and appends a canonical
terminal record, including the PR #18 `TrialOutcome.to_json_dict()` output,
metrics, validator provenance and the captured sources. The terminal record's
digest hashes the actual persisted bytes; `record_bytes(digest)` retrieves and
verifies them. These raw records may contain sensitive data and are not the
redacted download.

Restart uses `open_attempts()` to recover the same unfinished bindings. A
missing capture, crashed verifier or interrupted transaction does not fabricate
an outcome. Existing captures survive a rejected finish. The caller must
complete the evidence for that attempt, including any independently verified
interruption/cleanup result, before it can retry. Duplicate starts, captures,
finishes, foreign bindings, reordered events, malformed JSON, mismatched source
checksums and saved outcomes that disagree with replay fail closed. Concurrent
writers serialize through SQLite. This is not tamper-proof storage: someone
who controls the database can bypass SQL triggers or remove a history suffix.
Signed external checkpoints and authenticated capture producers remain
integration work; the hash chain is not a signature.

Only a canonical `harness_failure` permits a retry. All first attempts in its
declared block must finish before that retry starts. The second harness failure
excludes the logical assignment; any agent outcome ends it without retry.
Every attempt remains stored, including its separate `agentTerminalClass`.
An assignment waiting for its retry is `running` with one attempt and one
harness failure, null final digest and null final metrics. It is not silently
counted as a started second attempt. The dashboard accepts this waiting state.
Scheduling randomized arm order, performing the retry and stopping the campaign
at the canonical failure-rate thresholds remain M5 work.

`export(complete=False)` includes the entire roster, including pending work.
It derives statuses and denominators rather than accepting caller-supplied
report rows. `complete=True` refuses unfinished assignments. Only allowlisted
assignment fields, classes, validator decisions, Boolean fault claims, finite
metrics and digests leave the store. Exclusions use fixed public reason text;
the actual failure reasons remain in their canonical records. Agent text,
submission observations, logs, capture bytes and filesystem paths are not
exported. Missing metrics stay null; measured zero stays zero. Report metrics
describe the final attempt. `accounting()` separately reconciles logical
assignments, all attempts and retry-inclusive metric sums with available and
missing attempt counts. An available sum is not a complete campaign total when
measurements are missing. It provides no treatment effects or interim analysis.

**Verifier trust boundary:** an operator installs a `Verifier` in an explicit
Python registry. Campaign JSON cannot import code or nominate a callable.
Its fingerprint pins the callable entry point, its declared incident-specific
source/configuration files, and the core scorer/store source files. The verifier
receives the immutable assignment/attempt binding and captured bytes. It must
validate their trial origin, reconstruct the submission and independent checks,
call the canonical scorer, and derive usage metrics from captured accounting
inputs. It returns `VerifiedAttempt`, containing a `TrialOutcome`, metrics and
nonempty examined source IDs. All validator references must resolve to actual,
nonempty captured bytes. The exporter reruns that same registered implementation,
recomputes the shared diagnosis gate and compares the entire saved result,
including types, with replay. A self-consistent checksum or a saved `pass` label
is not sufficient.

No production Shop verifier is registered by this increment. The test verifier
is a labelled exact-observation toy, and the store forbids it in scored attempts.
The source hash does not prove a reviewer's scientific validity or the origin
of caller-supplied telemetry. Production evidence authentication, within-category
mechanism validation, fixture/runtime binding, hidden incident provenance and
independent reviewer controls remain M5/M6 and incident work. Retain pinned
verifier sources with the artifacts; changing them refuses replay rather than
silently regrading history. Historical smoke/catalogue records are not accepted.

The offline module CLI exposes `prepare`, `export` and `accounting`. `prepare`
takes `--spec` and `--store` and prints the receipt. The read commands take
`--store`, `--receipt` and a new `--output` path; export also accepts `--complete`.
Existing outputs are never overwritten. The default CLI has no production
verifier registry, so it can export prepared/pending rosters but refuses
terminal records. A future trusted driver can call `main(..., verifiers=...)`
or the same `CampaignStore` API. This is deliberately not a live run/resume CLI.

From the repository root with the locked environment restored:

```bash
DOCKER_HOST=unix:///nonexistent/docker.sock benchmark/.venv/bin/python -m radius_perf_eval.campaign --help
DOCKER_HOST=unix:///nonexistent/docker.sock benchmark/.venv/bin/python -m pytest benchmark/tests/test_campaign.py benchmark/tests/test_campaign_mutations.py benchmark/tests/test_dashboard.py -q
```

The controls exercise restart, process interruption before commit, concurrent
starts, retry/exclusion reconciliation, source/assignment forgeries, redaction,
healthy/fault distinctions and verifier provenance. Generated reports pass
through the shipped dashboard JavaScript and its existing UI/download/embargo
checks. Guard and call-wiring mutations run offline in fresh Python processes.
Synthetic captures stay in test temporary directories, not published results.
Raw verification logs, including failed mutation and collection attempts, are
retained in `../radius-perf-eval-artifacts/m2-attempts-20261001-07393aba/`.

### M2 exit-criterion accounting

The bookkeeping increment does **not** close M2. The owner has asked for the
entire milestone before declaring it ready. This table separates a tested
interface from an implemented incident grader.

| Criterion | Implementation and evidence | Remaining M2 work |
|---|---|---|
| Directed answers and canonical names | `submit_tool.py`; reference, reversed-edge, alias-conflict and malformed-call controls in `test_submit_tool.py` and `test_diagnosis.py` | None at the schema/mapping boundary; sealed fixture inventories are M3. |
| Correct component/edge and healthy/fault claim | `DiagnosisGrade` derives these verdicts; reference and planted wrong answers in `test_diagnosis.py`, with canonical outcomes exercised through `test_campaign.py` | Connect the shared gate to the reference incident reviewer below, not just the exact-observation test double. |
| Wrong mechanism fails | The shared gate checks `causalCategory`. The submit schema has no separate mechanism claim; the toy reviewer matches complete observation strings. Neither establishes discrimination between mechanisms within a category. | An owner-approved mechanism-claim/evidence interpretation contract and an implemented deterministic reference reviewer, with correct and wrong within-category controls. Do not count category mismatch as this criterion. |
| Correlated symptom, fabricated evidence and empty evidence fail | Every citation must be reviewed; the test reviewer rejects non-causal signals, missing signals and wrong exact observations. Actual captured byte references, checksums and trial binding are exercised in `test_campaign.py`. | A reference reviewer that checks incident-owned causal evidence rather than test dictionary equality. Actual Shop capture/authentication and incident activation integrate in M5; the expanded library and calibration are M6. |
| Canonical outcome classes and all success gates | `trial_outcome.py` and `campaign.py`; missing grade/lifecycle checks fail closed, agent class survives harness override, and unsupported canonical shapes cannot export | Wire the reference reviewer into the canonical attempt path. Real sandbox/environment adapters are M4/M5, not implied by offline pass labels. |
| Prepared full roster; interruption and retry do not inflate denominators | `CampaignStore`; durable start/capture/finish events, process-crash and concurrent-start controls, retry-once/exclusion tests and guard mutations | None in the offline bookkeeping contract. Live randomized scheduling and campaign-stop enforcement are M5. |
| Source-backed redacted report and shipped dashboard | Export replays registered code against stored bytes, checks the canonical grade and saved result, and emits the full roster. `test_campaign.py` passes generated exports through `dashboard_checks.cjs`, including healthy false alarms and the scored embargo. | Install the reference verifier through a trusted operator entry point. The default CLI currently refuses terminal export. Real telemetry-origin authentication cannot be inferred from hashes. |

The unresolved mechanism contract changes how answers are judged, so it is an
owner decision under the canonical plan, not an implementation convenience.
M2 must remain incomplete until that decision is recorded and the corresponding
reference grading and integration controls pass. No live call is needed to make
or exercise that contract; real Shop measurements remain subject to the existing
approval gates.

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
(`"signedOff": true`) only if every **required** gate was **recorded** and
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

### The required gates are generated, not listed

The per-service half of the requirement is derived from the Compose file by
`checks.py`, not written down in a tuple. The earlier version enumerated four
services by hand in three places, which was correct for this stack and was a
latent complete-inventory failure: a fifth service acquired no checks, and the
manifest still signed off with every gate it knew about passing. The check meant
to prove the environment matched its declaration could not see the part of the
declaration nobody had told it about.

`docker compose config --format json` is the authority. It resolves
interpolation, merges overlays, and normalises units, so the model describes
what will actually run rather than what the template appears to say. Each
service gets one check of each of five kinds — `image-pinned`,
`resource-limits`, `environment-variables`, `egress`, `readiness` — and
reconciliation fails in both directions: a service in the file with no check,
and a check naming a service not in the file.

Two gates hold this closed, and both are needed:

- `check-plan-generated` — without it, a run that died before generating a plan
  would have an empty derived requirement and could sign off having verified
  nothing.
- `check-plan-covers-compose-services` — without it, a plan that omitted a
  service would omit that service's gates from the requirement too, so the
  omission would erase its own evidence. This gate is the only thing that
  notices.

Measured on the real file: adding a fifth service to `compose/base.yml` takes
the plan from 20 checks to 25 and fails sign-off three separate ways — the
coverage gate names the problems, `resource-limits:sidecar` fails because the
service declares none, and `readiness:sidecar` is required but never recorded.
Undeclared limits parse to `None` rather than `0` precisely so that case fails:
an unlimited container reports `NanoCpus: 0`, so a `0` default would have
compared equal and passed.

### Verified properties

Gates recorded per run, all checked against the live system rather than assumed.
The first five kinds are generated once per service in the Compose file:

- `image-pinned:<service>` — the container's running image ID equals the pinned digest.
- `resource-limits:<service>` — `HostConfig.NanoCpus` / `HostConfig.Memory` match the
  limits the Compose file declares. The incident depends on constrained resources, so
  unconstrained containers would silently invalidate the scenario.
- `environment-variables:<service>` — every variable the Compose file declares is
  present in the container with the same value, read back from the daemon rather than
  from the app. Values are compared in full and recorded redacted, because generating
  this check over every service brought the per-run MySQL credentials into scope and
  manifests outlive the trial. A service that declares no environment records
  `coverage 0` rather than an unqualified pass.
- `egress:<service>` — the networks the daemon reports match the networks the file
  declares; for services on `internal: true` networks only, outbound DNS must also
  fail. A service that can reach the network must declare why in `EGRESS_EXCEPTIONS`,
  so a new service cannot quietly acquire egress.
- `readiness:<service>` — one application-level probe per service: MySQL answers
  `SELECT 1`, Valkey answers `PING`, catalog-api serves `/healthz`, `/readyz`, seeded
  products and metrics, Prometheus is ready and scraping. A service with no registered
  probe fails reconciliation. `up --wait` only proves containers are healthy.

The rest are declared per application or per lifecycle stage:

- `application-readiness` — every per-service probe passed.
- `mysql-seed-rows` — exact row count queried from MySQL.
- `valkey-empty` — `DBSIZE` is 0.
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
cd /path/to/repo/benchmark && uv run python -m pytest tests/
```

Use `pytest`, not `unittest discover`. Seven of the eight test modules are
written as bare `def test_*` functions rather than `unittest.TestCase`
subclasses, and `unittest discover` collects **zero** tests from a module in
that style. It does not error: it imports the module, finds no `TestCase`,
collects nothing, and reports `OK` on whatever remains. On a machine with the
SDK dependencies installed, the `unittest` command therefore ran one module of
eight and printed a green result, which is the silent-omission failure this
README warns about two paragraphs below. CI has always used `pytest`, so the
defect was in this instruction rather than in the gate.

### What CI does and does not cover

The `python` CI job discovers every test module under `benchmark/tests` from
disk, rather than from a list. For each one it reports how many tests were
collected and how many ran, and it fails if either is zero, if collection
fails, or if the two numbers differ. Discovering no modules at all is also a
hard failure. **The CI log carries the current per-module counts**; they are
deliberately not repeated here, because a number in prose is not checked by
anything and goes stale silently — this paragraph has claimed 120, then 254,
then 284, each true when written.

It previously ran only `test_driver`, which at the time was 120 tests. The other
modules import `github-copilot-sdk` and `inspect-ai`, which were reachable only
through CFS, and CFS authorizes by **network context rather than by
credential**: it resolves from a managed machine and returns 401 to a
GitHub-hosted runner, so no token would have fixed it.

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

### The host class decides which tolerances apply

Tolerances are numbers fitted by measurement on one machine. Applying them to a
different machine is not a small approximation; it is a measurement of one thing
reported as a measurement of another. So the driver reads the machine's own facts and
refuses to give a verdict on a machine it has no frozen bounds for.

`hostclass.observe_host` reads the operating system and release, architecture, CPU
model and logical core count, physical memory, the Docker engine version, the
container runtime's kernel, core count and memory ceiling, whether that runtime is
virtualised, and the Python patch version. Nothing is passed in by a caller. A label
supplied from outside would be the one fact nobody measured, and it is the fact
everything else keys on.

Those facts produce two identifiers, and the distinction between them matters.

The **class id** is the performance envelope: operating system, architecture, CPU
model, core count, memory, and the container runtime's own core count and memory
ceiling. It keys the frozen tolerance sets. On this laptop it resolves to
`darwin-arm64-apple-m5-10c-32.0gib-docker-vm-10c-7.7gib`.

That last part is not a typo. The laptop has 32 GiB, but containers run inside the
Docker Desktop virtual machine, which was given 7.7 GiB. The workload cannot reach the
laptop's memory, so classifying by it would describe a resource that does not exist
from the container's point of view. Both numbers are in the class id because both
constrain something.

The **fingerprint** is the class id plus every patch-level version: operating system
release, Docker engine, Python. These move on their own and are not part of the
envelope. Between the merged holdout and the check that followed it, the Docker engine
went from 29.7.2 to 29.8.0 and Python from 3.12.13 to 3.12.14, and the measured
numbers did not move. Voiding a frozen set on a patch bump would mean refitting
tolerances every time Homebrew runs, which in practice means nobody refits them and
the refusal gets switched off.

If no frozen set exists for the observed class, `exitCriterionMet` is false and
`hostQualification.refusal` names the class that was seen and the classes that would
have been accepted. It does not warn and continue. Borrowed bounds fail silently and
look exactly like success.

### A changed fingerprint blocks scored trials until it is re-checked

Recording an identifier and never acting on it is decoration. The fingerprint carries
a specific consequence, and it is deliberately not the same consequence as an unknown
class:

- An unknown **class** means there is nothing to measure against, so there is no
  verdict.
- A changed **fingerprint** means the bounds still apply, so there is still a verdict,
  but nobody has checked that the version bump left the numbers where they were. So
  scored trials refuse to start until someone checks.

The check is short: at least three cycles against the unchanged frozen bounds. Passing
it writes a re-qualification record, and the next run on that fingerprint is allowed to
start scored trials. It is deliberately too short to fit new bounds with. The question
it answers is "do the existing bounds still hold", not "what should the bounds be".

The gate is evaluated against the records that existed before the suite began, so a run
cannot clear its own gate. A blocked run that then passes reports both facts: that it
started blocked, and that the next one will not be.

Records are machine-local, in `~/.radius-perf-eval/qualifications.json` by default and
overridable with `RADIUS_PERF_EVAL_QUALIFICATION_STORE`. They are not committed. A
record is a statement about one physical machine; in the repository it would accumulate
one entry per developer laptop and mean nothing on any of them.

The catalog app's frozen set declares its fitted fingerprint as unknown, and that is
not an oversight. The holdout at `0407638` ran before the driver recorded host facts,
so its report contains no fingerprint and there is nothing to reconstruct one from.
An unknown fitted fingerprint is treated as a mismatch, never as a match, so scored
trials on the catalog app stay blocked until a three-cycle re-qualification records the
real one. Writing today's fingerprint into the source to make the gate pass would
assert something no artifact supports, and a test fails if anyone does.

### Freezing the versions a fingerprint is made of

Detection is the guarantee that actually holds, and it is tested. Prevention is worth
attempting anyway, so a campaign is not interrupted by an update it could have
declined. What follows is what is available on each host, including where nothing is.

**On this laptop, there is no supported way to freeze the Docker Desktop version.**
Docker's documented mechanism is an administrator settings file at
`/Library/Application Support/com.docker.docker/admin-settings.json`:

```json
{
  "configurationFileVersion": 2,
  "disableUpdate": { "value": true, "locked": true }
}
```

Settings management is a Docker Business feature and requires enforced sign-in. The
account on this machine reports `PlanName: personal` with no organisations, so the file
would be written and ignored. It is documented here rather than run, because a command
in a README that silently does nothing is worse than an absent one: it converts an
unsolved problem into an apparently solved one. Docker Desktop on a personal plan does
not install an update without someone clicking through it, so during the pilot the
control is a human one, declining the prompt. The enforcement is the fingerprint gate
above, which does not depend on anyone remembering.

**On the Linux virtual machines,** where the scored campaign runs, the versions can
actually be pinned. These have not been run, because no virtual machine exists yet;
they are recorded now so the qualification run performs them rather than inventing them
under time pressure:

```sh
# Pin the Docker engine at its qualified version.
sudo apt-mark hold docker-ce docker-ce-cli containerd.io \
  docker-buildx-plugin docker-compose-plugin
apt-mark showhold            # positive control: the five packages must be listed

# Stop unattended upgrades from moving anything underneath a run.
sudo systemctl disable --now unattended-upgrades.service
systemctl is-enabled unattended-upgrades.service   # must print "disabled"
```

Each has a read-back command alongside it, because "I ran the disable command" and "it
is disabled" are different claims, and only the second one is the one that matters.

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

## The Astronomy Shop

Scored trials move from the catalog app to the OpenTelemetry Astronomy Shop.
Upstream is vendored at release `3.1.0`, pinned to a commit, with every image
resolved to a digest. The trial stack is *generated* from the vendored files by
applying a declared list of transforms, rather than being layered with a Compose
overlay, because Compose merges `volumes` and `ports` by appending: an overlay
can add a mount but can never remove one, and most of what the shop needs is
removal.

### Status: what is built, and what is not

The shop is **not** ready to run scored trials. The list below is the handover.
It is deliberately specific about the unbuilt items, because each one has
requirements that were settled in discussion and would otherwise survive only
in a chat log.

**Built and verified on this laptop**

Read this list with the next paragraph, which says which of these the trial
driver actually calls. Several are libraries with tests and no caller yet.

  - Host class derived from observed facts, with tolerances frozen per class,
    and a refusal to give a verdict on an unknown class.
  - Fingerprint recorded beside the qualification fingerprint, with a drift
    gate: a changed fingerprint yields a verdict marked "not re-qualified" and
    blocks a scored start until a 3-cycle re-check passes. A missing
    fingerprint counts as a mismatch.
  - Vendored upstream `3.1.0` at a pinned commit, all images resolved to
    digests, with the manifest hash carried in provenance.
  - Generated per-service checks, derived from the Compose file rather than
    enumerated, covering image pinning, limits, environment, egress and
    readiness. A service with no checks fails sign-off.
  - The three upstream isolation defects fixed by transform, and a static check
    on the rendered config for `container_name`, named networks, fixed host
    ports, and socket or `/hostfs` mounts.
  - A per-render private copy of the flag directory, so a flag toggled during a
    trial cannot rewrite the vendored file or persist into the next trial.
  - Application-level readiness per service, including Kafka and the databases,
    with ports rediscovered after every container recreation.
  - Flag services unpublished, and a gate that reads every flag's resolved
    variant from flagd itself and fails if any is not at its baseline variant
    or if the state cannot be read.
  - CPU limits on all 28 services, fitted from kernel `usage_usec` with limits
    removed, applied as a uniform generous floor, and accepted only on zero
    lifetime throttling across every service.
  - The offered-load gate: achieved request rate measured per cycle from the
    generator's own counter, scored only inside a frozen band.
  - The `firepit` exporter stripped from the derived collector config.

**Merged in PR #14:** `shop_environment.py` calls the deployment,
readiness, flag, lifetime-throttling and phase-specific load checks. The
catalogue runner in `trials.py` is unchanged. The Shop command is an
environment check, not an agent trial or a qualified campaign.

The startup assets include the public upstream `.env`, a derived load script
without `ask_agent`, a derived proxy template blocking flag and mutable control
routes, a derived daily-index Grafana datasource, and Linux ARM64/AMD64
archives of OpenSearch plugin `2.34.4`. The archives
contain the Apache-2.0 license and Grafana signature. Their declared Grafana
dependency includes the pinned Grafana version. `startup-assets.json` records
source URLs and hashes, and `image-digests.json` links those pins. Rendering
verifies the assets and mounts them read-only; Grafana startup downloads are
disabled. Only ingress publishes a loopback port. Backend readiness runs on
the internal network.

With the locked environment installed, the environment-only check is:

```bash
benchmark/.venv/bin/radius-perf-eval-env shop --calibrate --seconds 60
```

Earlier executions failed at the OpenSearch datasource-health gate with
`Index not found: otel-logs-*`. A follow-up inspection found a nonempty dated
index: the pinned plugin was looking up the literal wildcard in a response
keyed by concrete index names. The derived datasource now uses
`[otel-logs-]YYYY-MM-DD` and `interval: daily`. An isolated live control passed
health and a Grafana PPL query over synthetic records; missing-index and
missing-plugin controls failed. The driver requires the timestamp field to
exist with date type, not merely a plugin status of `OK`.

**PR #15 added typed ingestion acceptance.** Both the environment
driver and footprint producer start OpenSearch first, install and read back
the same `attributes: {type: object, disable_objects: true}` index template,
then start the remaining services. The render record hashes the template and
verified collector source/derived configuration. Image and plugin pins are
unchanged. No attributes are discarded or converted to `flat_object`.

The collector retains upstream OTLP self-telemetry and exposes direct counters
on internal port 8888, without a host port. Before and after the healthy
measurement, one probe container reads counters, refreshes/counts the dated
indices, and reads counters again. Acceptance requires stable receiver-accepted
counts across both transports, matching debug/OpenSearch exports and indexed
documents, successful query shards, and the same non-restarted collector.
Failure counters reject any observed loss. Missing required counters and a
bracket that never stabilizes fail; absent failure-only series are not treated
as reported zeros. The witness covers collector-accepted logs through each
boundary, not logs an application SDK never emitted or delivered.

An OTLP positive control verifies scalar and dotted keys in `_source` and a
numeric range/average query. An additional Grafana PPL query must return actual
Shop service logs through the existing `/grafana` ingress, not just synthetic
probe rows. Live controls rejected an incompatible nested object and a removed
plugin. `disable_objects` is not blanket arbitrary-map support: a nested object
reusing a scalar field's name still fails. The driver must reject that loss if
future application data introduces it.

The exact environment command above passed in
`radius-eval-shop-72c84caeafb3`. The independent planted-failure run is
`radius-eval-shop-93bde23ed9e4`. These are bounded healthy samples, not host
qualification. The earlier checkout throttling remains unexplained; this
change neither refits limits nor relaxes lifetime-throttling checks.

**Metric-export acceptance:** before and after the measured load window, the
driver captures direct metric counters, then waits up to a 90-second polling
deadline for Prometheus's stored self-telemetry samples to reach those lower
bounds. Individual probe requests have their own timeouts. Both debug and
Prometheus exporters need positive counters from the same collector instance.
The backend samples must have been recorded after the direct-read boundary.
A range-vector query preserves stored timestamps; an instant-vector timestamp
would only prove when the query ran. Empty, partial, stale, and lagging results
cannot pass. A changed collector, regressed direct counters, or any observed
metric receiver refusal/failure or exporter send/enqueue failure rejects the
attempt. Failure-only counters that were not emitted are not reported as zeros.
Raw queries and direct expositions are saved before the verdict. On a failed
environment attempt, complete collector and Prometheus logs are also saved
in the measurement journal.

This witnesses fresh self-telemetry delivery and detects observed export
failures. It does not account for every application metric, prove SDK
completeness, or establish that every scraper succeeded. It does not replace
the direct log-accounting witness. Collector configuration and periodic
export cadence are unchanged.

The exact environment command passed with both metric boundaries in
`radius-eval-shop-bc65f8391e84`. Live controls in
`radius-eval-shop-a01cc2841397` rejected an empty query and a deliberately
conflicting OTLP sample that produced a permanent HTTP 400 and a nonzero
export-failure counter. The earlier startup HTTP 500 was not reproduced in
`radius-eval-shop-757f8b7374b2`; its cause remains unknown. The HTTP 400 control
proves detection, not a diagnosis of that earlier failure. All attempts
preserved evidence and verified cleanup.

The driver saves complete Grafana stdout/stderr and inventories logged URLs
and outbound-error candidates, rejecting unexplained destinations and missing
startup coverage. This does not detect unlogged network attempts. Render
provenance also lists and hashes Grafana source/provisioning files and the
derived datasource, with every literal fault-flag/`flagd` match and line.
The measured inventory had no such matches. Its planted-reference control
detects both flag names and `flagd`. The source-level semantic assessment below
records additional diagnostic hints; final sealed-fixture leakage enforcement
remains M3 work. Dashboards and provisioning are not rewritten.

The shared footprint/demand producer completed a bounded live run recorded as
`astro-footprint-20260930T165306`. Its raw cgroup stream, load boundaries,
host-labelled demand report and cleanup record are beside this checkout.
This run retained quotas and cannot be used as an unlimited-demand refit.
See the canonical plan's current-state section for investigation history.

`--calibrate` records a fitting sample without claiming a frozen load-band
verdict. Without it, the driver refuses a missing or wrong-host band.
Artifacts go to the checkout's sibling `radius-perf-eval-artifacts` directory.
Each run writes an incremental measurement journal, an environment record,
rendered Compose, raw load readings when reached, and failure logs. The schema
is `radius-shop-environment-v1`, not a dashboard comparison report. Runtime
bind copies are removed only after verified container cleanup.

**Remaining acceptance work**

  - **Qualification and campaign integration.** There is still no frozen
    `offered-load.json`, Shop determinism campaign, agent trial, or report
    exporter. A successful environment sample will not qualify a host.
  - **Telemetry and outbound acceptance.** Retain the typed ingestion and
    missing-plugin controls. Extend outbound-attempt coverage beyond Grafana's
    logs; network isolation alone does not prove services never tried to leave.
    Investigate the unreproduced startup Prometheus HTTP 500 using the new
    complete backend logs. Metric gates now prevent observed export failures
    or missing periodic counter evidence from passing, but are not a repair
    for that unexplained rejection. The Grafana source assessment below feeds
    M3's incident-specific review; it does not clear the sealed fixtures.
  - **The incident-phase load gate integration.** The implemented gate checks
    generator activity, both window-boundary states and user counts, and its
    lifetime throttling. Wire it to actual incident activation in M5/M6.
    The rate band is valid for healthy cycles
    only, because Locust is closed-loop and a working incident legitimately
    lowers the rate. Incident phases must instead gate on the cause: the load
    generator's own lifetime throttled-period count must be zero, and it must
    be in the running state with its configured user count. Load-surge
    incidents declare their own expected rate.
  - **Repeatable healthy acceptance.** Bounded windows passed with active
    configured users, no new endpoint failures, and zero lifetime throttling.
    Do not extrapolate them to repeatability or dismiss the earlier checkout
    throttle failure. Keep the existing gates.
  - **The determinism fit and holdout.** Roughly three hours at about eight
    minutes a cycle, fitting on one set of cycles and validating on a separate
    holdout, following the catalog app's method. It needs the user's go-ahead
    and a laptop kept awake and on power. Until it runs, the shop has no frozen
    tolerance set and therefore cannot produce a verdict.
  - **Host coverage.** Everything fitted here covers **this laptop only**. The
    Linux VM host class has no frozen tolerances, and by the rule above the
    suite will refuse to give a verdict there until it is qualified on that
    class. Nothing measured on this laptop transfers.
  - **Live cgroup and demand verification.** Both CPU readers now resolve
    `/proc/<pid>/cgroup` in the daemon's PID namespace rather than constructing
    a cgroupfs-only path. Missing process/path/counter evidence fails closed.
    The footprint tool streams raw kernel readings and emits a demand report
    containing the observed host class, sample coverage, load and services with quotas.
    The bounded laptop producer ran successfully. Confirm the resolver on the
    Linux VM before fitting that host class.

### Grafana semantic review evidence

The offline extractor records source hashes, every parsed dashboard JSON leaf
at its JSON pointer (including unknown fields, links, defaults and numeric
thresholds), and every YAML/INI line including comments. It covers the same
source and derived datasource inventory as the render record. It refuses empty
files, malformed or duplicate-key dashboard JSON, dashboards without titles or
panels, missing file roles, and a file that changed during capture. It does not
use a keyword heuristic to label arbitrary prose safe. A successful extraction
always reports `semanticVerdict: not-established` and `reviewRequired: true`.
It is not a live Grafana export, a frozen file allowlist, or M3's sealed-fixture
scanner. Removing one file while others still cover its role is not a semantic
approval; the generated path/hash inventory is what a reviewer must compare.

From the repository root, after installing the locked environment:

```bash
mkdir -p ../radius-perf-eval-artifacts/m1-audits-20261001
DOCKER_HOST=unix:///nonexistent/docker.sock benchmark/.venv/bin/python -m radius_perf_eval.grafana_review --output ../radius-perf-eval-artifacts/m1-audits-20261001/grafana-review-final.json
```

The output path must be new. Choose another filename for a later capture;
the command never overwrites earlier evidence. No Docker, model call or
network request is needed. The generated document inventory and offline test
log carry coverage details, rather than counts copied into this README.
Planted controls preserve a remediation sentence, a causal category, an
incident URL, a service selector and a numeric threshold outside literal
flag names, including inside unknown nested fields. They prove extraction,
not automatic recognition of those meanings. Guard and parser-wiring
mutations exercise the fail-closed checks.

**Source measurement, October 1, 2026:** the capture at
`m1-audits-20261001/grafana-review-final.json` hashes the unchanged vendored
Grafana files and derived datasource. The literal scan found no declared
fault-flag or `flagd` match. The following are observed source contents, not
measurements of loaded dashboards, firing alerts or network attempts.
Dashboard paths below are relative to
`upstream/src/grafana/provisioning/dashboards/demo/`; other paths are relative
to `upstream/src/grafana/`.

| Surface | Observed evidence | Assessment and retained risk |
|---|---|---|
| `demo-dashboard.json` `/panels/14/targets/0/queryType` | `dependencyGraph`, under Service Dependency, uses the Jaeger datasource. | All arms can receive topology information independently of Radius. This can narrow the treatment contrast, as the canonical plan already anticipates. |
| `demo-dashboard.json` `/panels/9` through `/panels/12` | Python CPU/memory, Recommendations Rate with `recommendation_type="catalog"`, and Quote Service batch span processor panels. | Uneven diagnostic emphasis may direct attention to services or mechanisms that overlap future incidents. It is not a literal answer flag. |
| `exemplars-dashboard.json` `/title` and `/panels` | Cart Service Exemplars with GetCart and AddItem latency/exemplar panels. | Cart has dedicated diagnostic guidance not shared uniformly by every service. Review against the final incident set. |
| `provisioning/alerting/cart-service-alerting.yml` | `CartAddItemHighLatency`, cart/AddItem selectors, a `0.0001` seconds threshold, and `isPaused: false`. Its description still says `xxx seconds for 2 minutes` while `for` is `1m`. | The source preselects a component, operation and symptom. The inconsistent prose is not an incident specification or a verified firing threshold in a running stack. Preserve it now; any neutralization needs M3 review and identical treatment across arms. |
| `apm-dashboard.json` `/templating/list/5/current`; `demo-dashboard.json` `/templating/list/0/current` | Default service selections are `checkout` and `frontend`. APM also groups outbound services/databases and links logs and traces. | Defaults can steer an agent before it examines evidence. Useful navigation does not establish a cause, but may overlap a future answer. |
| `self-observability.json` `/panels/7/description`, `/panels/13/description`, `/panels/15/description`; `opentelemetry-collector.json`; `provisioning/alerting/opentelemetry-collector-rules.yaml` | Text distinguishes processor queue drops from failed exports and names `queue_full`, shutdown and connection errors. Collector rules label refused/export/enqueue failures, queue utilization and runbook links. | These are explicit mechanism hints. They are ordinary diagnostic telemetry, not grounds to remove it, but an incident about telemetry loss could become easier through prewritten explanations. |
| `events-by-name.json`, `spanmetrics-dashboard.json`, `NGINX-metrics.json`, `linux-dashboard.json`, `postgresql-dashboard.json` | Event-name grouping, slow/error span rankings, connections, host resources, PostgreSQL cache/conflict/deadlock/connection views. Linux defaults to `docker-desktop`. | These reveal symptom categories and some environment context, not a demonstrated hidden diagnosis. Event names and live labels may carry additional hints absent from static files. Host panels do not prove that the removed host receiver supplies data. |
| `provisioning/datasources/`, `provisioning/dashboards/demo.yaml`, `grafana.ini`, derived OpenSearch datasource | Internal backend URLs, trace/log/exemplar joins, dashboard file provider and Demo home folder. Source dashboards/datasources are editable; anonymous access is configured as Admin. | Navigation and runtime edits are separate review surfaces. This offline capture cannot attest to the state later visible to an agent. Source and derived datasource retain their existing query capabilities. |
| `apm-dashboard.json` `/panels/0/options/content`; datasource and alert provisioning | An external OpenTelemetry image URL, external documentation/runbook links, localhost exemplar links, and the default email contact `admin@example.com`. | The HTML image could cause a browser-side fetch, not necessarily a Grafana-server call. A configured email recipient or link is not proof of an attempted connection. External contents were not fetched or reviewed. |

**Judgment:** this agent-authored assessment finds meaningful diagnostic hints
beyond literal flags. It does not establish that any is a hidden incident
answer, nor certify their absence. The final incident set and sealed fixtures
are not available for that comparison. The owner must review incident overlap,
live state and common-arm parity in M3. Preserve these capabilities identically
across all three arms; this increment changes no dashboard, provisioned alert,
telemetry semantics, startup asset, image pin or quota.

### Outbound-attempt observation remains open

**Owner decision, October 1, 2026:** finish the semantic audit now and defer
tracing approval. No packet capture, syscall tracer or new instrumentation
permission is authorized by this increment.

The existing ingress-positive/backend-negative curl controls demonstrate a
connection boundary. They generate harness traffic, not evidence that each
application refrained from trying an external call. Grafana's complete logs
cover only logged destinations. Neither provides an application-attempt
observation interval for every service. A socket-table poll can miss short-lived
or immediately rejected calls; packet capture misses calls rejected before a
packet exists. Empty traces, absent logs and blocked routes cannot clear M1.

A future approved design must name the exact observation start/end for each
service and container incarnation, include startup or explicitly leave it
uncovered, distinguish application traffic from readiness/load/control probes,
and inventory expected internal destinations rather than exempt all traffic.
It must define coverage of IPv4, IPv6, DNS (including Docker's embedded resolver),
TCP and UDP/send operations, connection reuse and observer loss. Plant a real
external attempt and an internal control in every claimed observation scope;
prove detection even when the external call is blocked. Record raw observations
before judging, reject missing coverage, and measure observer perturbation.

Possible owner decisions are a separately bounded, service-scoped syscall
diagnostic with explicitly approved tracing permissions and pinned tooling, or
packet-level evidence that leaves pre-packet failures unresolved. Neither is
implemented or silently substituted for the required absence-of-attempt
evidence. M1 stays open pending that decision and its controls.

### It is 28 services, not 17

The plan said about 17. The real count is 28: `compose.yaml` declares 20,
`compose.full.yaml` adds 3, `compose.observability.yaml` adds 5.

### Three isolation defects in upstream, all the same family

Upstream is built to run once, on a developer's laptop, so it hard-codes
identity in three places. Each would stop a second trial from starting or make
two trials interfere:

- an explicit `container_name` on all 28 services, so names carry no project
  prefix and a second stack collides;
- `networks.default.name: opentelemetry-demo`, so two trials share one bridge;
- fixed published ports on `frontend-proxy` and `prometheus`.

All three are removed by transforms. Because upstream sets `container_name`,
anything that identifies containers by name prefix silently matches nothing;
the footprint sampler selects on
`label=com.docker.compose.project` instead and asserts it saw every expected
service.

### Removing a mount is not the same as removing what needs it

The first transform pass removed the Docker socket and the `/hostfs` bind but
left the `docker_stats` and `host_metrics` receivers that read them. The
collector validates receivers at startup, so it crash-looped:
`invalid root_path: stat /hostfs`. `up --wait` correctly refused to proceed,
which is the gate doing its job.

The collector configs are therefore *derived* at tooling time and committed, so
the change is reviewable in a diff and the driver stays dependency-free. A
missing derived config is a hard error rather than a fallback, because Docker
would otherwise create an empty directory at the mount point and the collector
would start against a config nobody reviewed.

### Readiness is per service, at application level

`up --wait` reports container health, and several shop services report healthy
before they serve anything. All 28 have an application-level probe. Every probe
below was corrected against a live stack rather than inferred from the Compose
file:

- `telemetry-docs` serves on 8000 and `quote` on 8090, not 8080;
- `opamp-server` and `kafka` declare no ports, so they are probed by exec on
  the internal listener; Kafka's CLI lives at `/opt/kafka/bin` and the broker
  listens on `kafka:9092`, not `localhost`;
- `image-provider` returns 403 on `/` because nginx denies directory listing;
- `jaeger` is base-pathed, so `/api/services` is a 404 and the UI is under
  `/jaeger/ui/`;
- `flagd` and `flagd-ui` are distroless, so exec is impossible, and they are
  unpublished, so the host cannot reach them. Both are probed from a throwaway
  curl container on the project network, pinned by digest;
- `accounting` and `fraud-detection` expose nothing at all, so readiness is
  read from the broker's consumer-group list, which is an external fact rather
  than a self-report.

A probe that cannot be evaluated is recorded as not-ready with a reason. It is
never skipped and never aborts the sweep, so "could not tell" stays distinct
from "not ready".

### The fault flags are read from flagd, not assumed

The healthy baseline requires every fault flag off. The gate reads resolved
state from flagd's OFREP endpoint and compares *variant* rather than value,
because several flags carry numeric or duration payloads whose neutral setting
is not boolean false. It fails closed: state it cannot read is not a clean
baseline, and a flag missing from the response is unknown rather than off.

The expected baseline is recorded from a verified-clean stack rather than
derived from `defaultVariant`. `productCatalogFailure` is the only flag with
targeting rules, and targeting bypasses the default, so a baseline derived from
defaults would disagree with reality for that flag. On 3.1.0 the recorded state
and the shipped defaults agree exactly, and a test pins that agreement so a
newly targeted flag surfaces rather than quietly weakening the gate.

### CPU limits are fitted, applied to every service, and verified by the kernel

Upstream declares `deploy.resources.limits.memory` on all 28 services and
`cpus` on none, so 28 services contend freely for the host's cores underneath
every measurement.

Limits are fitted as `max(2 x measured healthy peak, 8.0 cores)` and committed
in `cpu-limits.json`. The rule is frozen in code and the loader refuses a
manifest fitted under a different rule, so the committed numbers cannot drift
away from a fitting anyone can reproduce. `tools/refit_cpu_limits.py` applies
the rule to a recorded measurement, so the file is reproducible rather than
hand-edited.

On this host class no measured peak reaches half the floor, so every service
receives the same 8.0 cores and the multiplier does not bind. The uniformity
is wanted: the agent under test reads this Compose file, and limits fitted
per service would tell it which service we expect to strain before it read any
telemetry.

**These limits are guard rails, not constraints, and that is deliberate.** The
first two fits tried to be tight, and both were wrong in instructive ways.

The first fitted `max(2 x peak, 0.25)` from `docker stats` peaks. The kernel
then throttled 19 of 28 services. A `docker stats` percentage averages over a
multi-second interval while a CPU quota binds within a 100ms scheduling
period, so the peaks were understated by up to seventeenfold: `email` measured
0.05 cores against a true peak of 0.87 and throttled 11% of its periods. The
three worst-throttled services were exactly the three most understated.

The failure of that fit's positive control mattered more than the fit. A
service deliberately starved to a quarter of its measured peak throttled
*zero* times. The quota had reached the daemon and the service was being
measured, so the instrument was working.

The first published explanation for that was wrong, and the correction is more
useful than the original claim. It said the load generator had been throttled,
so it offered less load, so downstream services saw lighter traffic. The load
generator was throttled, but only for 2.3% of its periods, and that cannot take
`product-catalog` from a 1.49-core peak to under 0.25 cores in every one of 906
periods. A container capped at 0.25 cores cannot reach 1.49, so the only
question is whether the demand existed and was suppressed, which shows as
throttling, or never arrived, which does not. Zero throttled periods means it
never arrived.

What actually suppressed it was **`frontend`**, the direct caller of
`product-catalog`, capped at 0.26 cores against a 0.52-core measured peak and
throttled 9% of its periods. The mechanism is not a few percent of lost
throughput, it is burst smoothing. During a burst `frontend` wants 0.52 cores
and can have 0.26, so the burst is served at half rate over twice the time.
`product-catalog` then sees a flattened arrival stream. It has a 35x
peak-to-mean ratio, 1.49 cores against 0.042, and a service shaped like that is
taken under quota by flattening alone while its total work barely moves.

Two consequences follow, and both are larger than the corrected sentence.
**A binding CPU limit does not merely add noise to a measurement; it suppresses
the load that would have revealed the noise**, and it does so anywhere in the
request path rather than only at the load generator. And because `frontend` was
throttled in both arms of that experiment, the whole of the first fit's
verification measured a stack that was already degraded. "This service is fine
at its fitted quota" was true only of suppressed load. That is the real reason
the first fit was discarded, and it is a stronger argument for a uniform floor
than the original one: the only configuration that can be trusted is one where
nothing binds anywhere in the path.

The request path is load-generator, then `frontend-proxy`, then `frontend`,
then the rest, so the zero-throttle check has to hold on every service in that
chain on every cycle, not only at fit time. It covers all 28.

Refitting from kernel counters at a 1.0 floor cleared the steady-state window
but not the kernel's lifetime counters: kafka had spent 131 throttled periods
starting up, ad 60, fraud-detection 39. A probe at uniform quotas then
bracketed the floor. At 4.0 cores kafka still throttled one period during
startup; at 8.0 every service was clean for its whole life. One period out of
thousands means 4.0 sits on the edge, and a limit on the edge binds on some
runs and not others, which is the run-to-run variance this driver exists to
remove.

The arithmetic is not the evidence. A limit is accepted only if the kernel
reports **zero throttled periods over each container's whole life**, read from
each container's cgroup `cpu.stat`:

- **Lifetime, not a sampled window.** An earlier version subtracted window
  open from window close on the reasoning that only throttling during
  measurement can corrupt a measurement. Measurement disproved the premise:
  throttling lengthens bring-up, so readiness timing becomes a function of
  host contention, which is variance in the environment itself. The window
  figures are still reported, because they localise *when* throttling
  happened, which a lifetime total cannot.
- **No tunable threshold.** The window criterion came with a budget
  percentage. Periods throttled inside a window are a subset of those counted
  since container start, so the lifetime criterion subsumes it entirely and
  the budget could never decide a verdict. It was removed rather than left as
  a knob that invites being turned until the gate passes.
- **Every service, including the two we cannot exec into.** Readings come from
  the host cgroup hierarchy through a sidecar run with `--cgroupns=host` and a
  read-only bind of `/sys/fs/cgroup`. It needs no `--privileged` (verified) and
  never receives the Docker socket. An exempt service is exactly where an
  unnoticed throttle would hide.

A window with no scheduling periods is recorded as unmeasured, not as clean,
and a service absent from a reading fails the verdict rather than passing by
omission.

Two limits of this approach are worth stating plainly. A quota of 8.0 cores on
a ten-core host does not meaningfully constrain a single container, and the
limits sum to 224 cores on a ten-core machine. That sum is not a statement of
demand and should never be read as one. A CPU limit is a ceiling, not a
reservation, so nothing is set aside and the total is free to exceed the host.
Actual demand is the measured column in the fit, and it totals well under one
core at the mean. The limits do not protect against several services bursting
at once and saturating the host; what covers that is the plan's rule of one
trial at a time per machine, together with the offered-load gate, which
refuses a verdict on any cycle whose achieved request rate left the frozen
band whatever the cause. And the floor is fitted for this host class. A VM with
fewer cores needs its own fit, and the loader will reject the committed file
there rather than apply a number nobody measured on it.

### Egress: `internal: true` works, and costs all port publishing

Measured rather than assumed, with a control. On a routing bridge a probe
container reached the internet (HTTP 301); on an `internal` network the same
probe failed to connect (`curl` exit 7) while container-to-container DNS still
resolved.

Running the whole 28-service stack on an internal network seals egress and
breaks the harness, and the split is total:

| probe kind | ready | failed |
| --- | --- | --- |
| consumer-group | 2 | 0 |
| exec | 5 | 0 |
| internal-http | 2 | 0 |
| http | 0 | 8 |
| tcp | 0 | 11 |

All 19 failures report `container port N is not published`. This is the same
constraint the catalog app hit: **Docker publishes no host ports for a
container that is only on an `internal` network.** The shop will need the same
shape the catalog app uses, a routed network for the ingress container and an
internal one for the rest.

### Only one service reaches outside at startup, and it is a determinism problem

Checked by reading all 28 service logs, not by watching for healthcheck
failures, because a service that reaches out and then degrades quietly would
never fail a healthcheck. Four services logged connection or DNS errors and
three are false positives worth naming, since each would have been easy to
misreport:

- `frontend-proxy` to `www.envoyproxy.io` is a documentation link inside a
  deprecation warning;
- `otel-collector` to `github.com` is a README link inside a feature-gate
  warning;
- `jaeger` and `otel-collector` failing to resolve `prometheus` and
  `otel-collector` are *internal* names during startup ordering. On an internal
  network Docker's embedded DNS cannot forward, so a not-yet-registered
  container returns "server misbehaving" rather than NXDOMAIN. Both recovered.

The one real egress is **grafana to `grafana.com`**. `compose.observability.yaml`
sets `GF_INSTALL_PLUGINS=grafana-opensearch-datasource`, so grafana downloads an
unpinned plugin from a third party on every startup. That defeats the
byte-identical environment premise on the routed network we use today, not only
on an internal one, and the digest manifest does not cover it because the plugin
arrives after the image.

### A dangling exporter, now stripped

The collector's observability config exports to `firepit:4317`. No Compose file
in our set declares `firepit` and it is not among the 28, so the exporter
retries against a host that will never exist for the whole of every measurement
window. That burns CPU and fills the collector's logs during the exact window we
are measuring, which is the reason it had to go rather than be tolerated.

`derive_collector_config.py` now removes the exporter definition and its one
pipeline entry, alongside the two receivers it already removed. The derived
files are committed and hashed into the run record, so the removal is a recorded
fixture change rather than a silent difference in what the collector was told to
do.

One asymmetry in that tool is worth knowing about, because getting it wrong
would have been quiet and expensive. Receivers are matched on the part of the
name before the slash, because that part names the receiver's type and every
instance of the type is going. Exporters are matched on the **full** name. The
exporter being removed is `otlp_grpc/firepit`, and `otlp_grpc` is also the type
of `otlp_grpc/jaeger`, which carries every trace an agent under test would
diagnose from. A type-level match would have deleted both, and the result would
still have been valid YAML that started cleanly, so nothing would have failed
until someone noticed the traces were missing. There is a test for the removal
and a separate test asserting Jaeger survives it.

A pipeline that would be left with no exporters at all is a hard error rather
than something written out, since the collector rejects an empty exporter list
at startup and the symptom would be an `up --wait` timeout well away from the
cause.

### The independent variable is measured, not assumed

Every tolerance in this suite describes how the stack behaves *under a stated
load*. That load is an assumption until someone measures it. If the load
generator itself is starved, it offers fewer requests, the stack answers them
comfortably, and the cycle records low, stable latencies. That cycle does not
merely pass. It looks like the best result in the set, and it is the one a
careless reader would hold up as the target. The failure is silent and it
flatters itself, which is the combination worth engineering against.

So each cycle records the generator's achieved request rate and is scored only
if that rate falls inside a frozen band. Four details each change the number:

  - **Attempts, not successes.** A fault that breaks responses must not void
    the measurement built to observe it. Counting successes would make a
    working incident indistinguishable from a broken measurement.
  - **Read inside the container.** The counter is fetched by `exec`, so no port
    has to be published. The stack runs on an internal network where Docker
    publishes nothing, and a measurement that needed a published port would
    have to punch a hole for itself.
  - **The container's own clock, in the same call.** Timing the `docker exec`
    round trip from the host would fold process startup, tens of milliseconds
    and varying with host load, into the elapsed time and so into the rate.
  - **Fitted on the median.** One bad cycle during fitting cannot widen the
    band far enough to admit its own kind.

The verdict is tri-state, not a boolean. Out of band is not a failed trial, it
is a **failed measurement**: it yields no verdict and is counted rather than
scored. Collapsing that into pass/fail is precisely what lets a bad measurement
be read as a good result.

The control was run live against the 28-service shop, one stack, one variable
changed. The quota was applied with `docker update` rather than by editing the
Compose file, because recreating the container would restart Locust and reset
the counter the gate differences.

    healthy                1.189 rps   scored
    generator at 0.1 core  0.698 rps   no verdict   (41.3% drop)
    band                   0.951 to 1.427 rps

The starved arm was caught on rate alone. User count stayed at 5 and state
stayed `running` in both arms, so no side channel did the work. Had it tripped
on `state` instead, the band would still be untested and the control would have
proved nothing about the thing it was built to check.

Two limits are worth stating plainly, because neither is a property of the code
and neither can be fixed by tightening a constant.

**Counting noise sets a floor on the band.** At 1.189 rps over 300 s a cycle
sees about 357 requests, so Poisson counting alone contributes about 5.3%
relative noise. The band is ±20%, roughly 3.8 sigma, which is sound. But it
cannot be tightened much further without longer windows or heavier load.

**Locust is closed-loop, so offered load is not fully independent of health.**
Each of the fixed five users waits for a response before issuing the next
request, so a slow stack lowers the offered rate by construction. The band
therefore describes the **healthy baseline only**. Applied to an incident
phase it would refuse a verdict exactly when an incident worked. Incident
phases need their own expectation, and this gate is not it.

### Every healthy baseline failure is one absent host

The healthy baseline failed about one request in fifteen. A floor that size
cannot be fitted over, because an incident's signal has to clear it before it
is visible, and an unexplained floor might have been our own derivation
breaking something.

It was measured per endpoint, with pristine upstream `3.1.0` as the control:
same host, same load, same window, the vendored Compose files run unmodified
from their own directory.

    derived    21 failures / 313 requests    6.71%
    upstream   24 failures / 340 requests    7.06%

Two things settle it. The rates match, with the derived stack marginally
*lower*, and in both arms every single failure is the same endpoint, `POST
/prompt`, which fails **100%** of the time. No other endpoint failed once in
either arm. The set of endpoints failing only in the derived stack is empty, so
nothing in the derivation, not the stripped collector receivers, not the
unpublished flag services, not the renamed networks, and not the CPU limits,
broke anything.

The cause is exact. Locust reports `gaierror(-2, 'Name or service not known')`,
a DNS failure rather than an application error, and the load generator's
`ask_agent` task posts to `http://agent:8010/prompt`. No `agent` service is
declared in `compose.yaml`, `compose.full.yaml` or `compose.observability.yaml`.
The service exists in the release's Helm chart but not in its Compose
deployment, while the load generator baked into the Compose image calls it
regardless. The task carries `@task(3)`, which is about the right share of the
weighted total to produce the observed rate.

This is the same family as the `firepit` exporter: a reference to a host that
never exists in this deployment, retried for the whole of every measurement
window. It is upstream behaviour, not ours, and the decision it needs is a
fixture decision rather than a bug hunt. Leaving it costs a constant DNS
failure on a known endpoint and a floor under every latency distribution that
includes it. Removing it means setting `AGENT_ENDPOINT` at a host that exists
or dropping the task, either of which is a fixture change that must be declared
and re-fitted. **Nothing should be fitted until this is decided**, because the
floor moves when it is.

Two limits on the evidence, both recorded rather than worked around. Locust's
own `Aggregated` row is returned alongside the per-endpoint rows, so the raw
totals in the artifact double-count; the ratios are unaffected because
numerator and denominator double together, and the per-endpoint figures above
are the real ones. And the flag state could not be read in the upstream arm,
because upstream hard-codes its network name, which is one of the three
isolation defects the derivation exists to fix. The derived arm read all 18
flags directly from flagd and every one was off. Both arms load the identical
vendored flag file, so the upstream arm's flags are the same by construction,
but that is an inference and the derived arm's reading is the measurement.

### The load the tolerances assume

On record, from the vendored release and the image, so the number behind every
tolerance is not folklore:

    users                 5          (LOCUST_USERS)
    spawn rate            1/s        Locust's default, not set by the release
    think time            between(1, 10) seconds per user
    user mix              9 HTTP to 1 browser
    autostart             true, web UI enabled (not headless)
    locust                2.44.4
    achieved rate         about 1.0 to 1.2 requests per second

At roughly 350 requests in a 300 second window, Poisson counting alone
contributes about 5% relative noise, which is the floor under how tight the
offered-load band can be set. Raising the load is a pilot-calibration decision
and a fixture change, so the release default stands until someone makes it.
