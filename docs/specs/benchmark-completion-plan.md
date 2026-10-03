# Completing the Radius repository comparison

## Scope and finish line

Owner decision, September 30, 2026: compare the **combined Radius repository
experience** against native and architecture-document repositories. Keep the
three arms. Do not build graph-only, skills-only, factorial, or telemetry-overlay
experiments. The [canonical plan](copilot-radius-experiment-plan.md) governs
protocol, budgets, grading, and analysis.

**Owner decision, October 2, 2026:** separate exploratory learning from
confirmatory publication. The immediate deliverable is one real externally
injected Shop fault and a healthy counterpart across all three arms, followed
by a small fixed development batch with one model. Keep M2, its SQLite journal,
verified exporter and dashboard. The canonical
[exploratory policy](copilot-radius-experiment-plan.md#exploratory-learning-path)
governs eligibility, retained safeguards and unresolved run settings.

The confirmatory project is complete when an operator can build sealed fixtures,
qualify a host, run a resumable diagnosis campaign, reproduce its analysis, inspect the
comparison in a local dashboard, and download the results. A working pilot is
not evidence of uplift. Remediation, Kubernetes, and the human Canvas demo are
not required for this finish line.

## Delivery sequence

**Immediate path:** the exploratory policy merged in PR #20. M2 merged in
[PR #19](https://github.com/ryanwaite/radius-performance-demo/pull/19) on October 1
at `ed56c60861e07b20440e242d7a58b4dc10c0c90f`; no live three-arm comparison exists.
Formal M1/M4 acceptance remains incomplete. Exploratory eligibility is separate,
not a waiver or an assertion that the host is qualified.

| Increment | Work | Exit criterion |
|---|---|---|
| Real incident integration | After the policy lands and the owner settles the case and eligibility criteria, connect one external Shop fault and healthy counterpart, sealed three-arm fixtures, SDK/sandbox capture and human review to M2. Reuse existing boundaries rather than build a new orchestrator, store or review UI. | Real trial-bound activation, delivered-load, telemetry and lifecycle evidence enters M2; positive/negative controls reject broken evidence, hidden answers stay isolated, and treatment parity and usable Radius access are checked. |
| Approved integration run | Request model/premium allowance and runtime before calls, including authoring/probes. Run fresh matched sessions in randomized arm order. | Complete roster and failed attempts persist; human decisions and independent audit support replay/export into the dashboard; missing reviews remain unfinished. Usage, runtime, setup and review cost are measured. |
| Fixed development batch | Select cases, one model and matched budgets using integration measurements; obtain approval. Version Radius improvements on development cases and periodically rerun controls. | Descriptive outcomes against both controls and investigation traces identify benefits, regressions or uncertainty. Freeze a candidate before untouched holdout; tuning retires holdout cases to development. |

The assessment's sample batch is illustrative, not a request budget or powered
design. Concrete fault targets, stability/window criteria, run settings and
allowances still need owner approval. Recovered startup disturbances must be
distinguished from baseline/diagnosis-window problems without erasing failures;
accidental faults during diagnosis invalidate the comparison. Intentional CPU
faults need independent activation evidence, not a general pressure exception.
Existing zero-lifetime guards remain unchanged until a separately reviewed
exploratory implementation. Comprehensive outbound tracing stays deferred with
its observation limits recorded; no new tooling or permissions are authorized.

Keep effective confinement, cleanup, matched evidence access, human
mechanism/citation review and independent audit, the full roster and
once-after-block harness-only retry. Review actual incident-specific leakage while retaining
ordinary shared dashboard hints. Synthetic CPU reference captures are not Shop
findings. Broad libraries, cloud scale, inference/publication infrastructure and
exhaustive negative proofs wait until exploratory learning justifies them.

**Retained milestone accounting:** the history and M1-M8 criteria below describe
formal completion, not prerequisites that must all precede exploratory learning.

**Implementation progress:** PR #14 merged the M1 environment driver and
offline assets. PR #15 added shared typed log bootstrap, direct
collector-to-index accounting, actual Grafana log queries, and source inventories.
Bounded live controls passed a healthy window and rejected planted mapping/plugin
failures. The footprint/demand producer also completed. The mapping preserves
typed fields, but does not support a nested object reusing a scalar field's name.
Outbound evidence currently covers Grafana's logged destinations, not every
unlogged attempt. PR #16 requires fresh periodic metric-export
counters in Prometheus at both load boundaries and rejects observed direct
metric failures. A healthy calibration and planted permanent-export failure
exercised that gate. The earlier startup HTTP 500 was not reproduced and is
not explained by the planted HTTP 400. Complete collector/Prometheus failure
logs are now preserved. That startup rejection and intermittent checkout
throttling need further investigation; quotas remain unchanged. Failed attempts
preserve evidence and verify cleanup. M1 is not complete.
The offline semantic audit now extracts all dashboard JSON leaves and
provisioning/configuration lines with source hashes. Its source assessment
records cart latency guidance, service defaults, topology and telemetry-loss
mechanism hints, not a semantic clearance. M3 retains owner review against the
actual incidents and sealed artifacts. No diagnostic capabilities were removed.
On October 1, 2026, the owner chose to finish this audit and defer tracing
approval. Broader outbound-attempt observation remains an M1 gap; neither blocked
connections nor logged destinations proves absence of application attempts.
No new tracing tooling or permissions are authorized.
The dashboard and M2's offline attempt/report boundary are implemented.
M2 also implements the owner-approved file-based human-adjudication reference.
Qualified production incidents and the later campaign milestones remain unbuilt.

**M3 source-authoring increment:** `radius_perf_eval.shop_fixtures` prepares
the allowlisted pinned Shop application source as a hashed tar and materializes
independent Git baselines for native, architecture and Radius authoring.
It retains source/tests, manifests, ordinary instructions, licenses and
telemetry, with an explicit exclusion/common-change inventory. It does not
export the benchmark checkout or inherit host Git state. The concrete copies
are under `../radius-perf-eval-artifacts/shop-authoring-20261002T192813Z/prepared/workspaces/`.
See the [commands and limitations](../../benchmark/README.md#shop-source-authoring-workspaces).

**GitHub-backed authoring handoff, October 2, 2026:** the owner approved private
[`ryanwaite/astronomy-shop-radius`](https://github.com/ryanwaite/astronomy-shop-radius)
to supply the GitHub backing required by the Radius authoring tools. The
coordinator verified the tracked source against `source.manifest.json` and the
clean synthetic baseline before pushing `main` at
`12dbef5dfde1df1b902ca74b7d2b03ada89eceef`, tree
`7d9c5587e18627943c8237229b1fea0293c958b5`. The existing `radius/` checkout now
has that repository as `origin` and tracks `origin/main`. No local setup
changes, benchmark files or credentials were uploaded; native and architecture
siblings remain unchanged and unpublished. No Radius installation, generation
or model call ran.

This is an authoring checkout, not a scored trial workspace. Preserve the
previous source receipt as immutable historical evidence, not proof of the
checkout's current remote-free state. No remote-free trial seal exists, and
the source-only draft remains ineligible. The owner subsequently generated the
model and merged
[ryanwaite/astronomy-shop-radius#1](https://github.com/ryanwaite/astronomy-shop-radius/pull/1)
at `dce2f8f596e2eda9d7d07c114cb44749dc27acb0`.
The [offline import](../../benchmark/README.md#pinned-radius-overlay-import)
now reproduces native source plus that exact overlay and publishes complete
difference/setup inventories. The
[static review and generation trace](radius-overlay-review.md) verifies source
references and normalized provenance, while identifying the selected core
profile's mismatch with the benchmark's full/observability stack and missing
native runtime configuration. The omissions precede graph assembly; the origin
hash is valid, not stale.

Exact installed CLI/extension/tool/skill versions and actual diagnostic exposure
still need owner/extension verification, not inference from `radius:0.61`.
No installation, generation call or usability probe was authorized by source
preparation or this import. Radius remains draft/unsealed; model/runtime gaps
must be resolved in the application repository, not patched inside the fixture.
`architecture/` has no architecture document until validated graph facts and
approved isolated authoring exist. Source and import receipts explicitly
deny trial eligibility. Incident-specific leakage review, final treatment
differences, architecture token parity, actual tool/skill isolation and live
agent usability are still open; **M3 is not complete**.

During assisted setup, inventory GitHub-dependent Radius capabilities and
distinguish authoring needs from runtime diagnosis needs. Local application
source already exists in every trial copy. If runtime GitHub is necessary,
bring the owner a proposal for assigned-repository read access with comparable
source browsing for controls, no benchmark/hidden-answer/other-arm exposure,
and frozen remote state and tool/network policy before runs. The canonical
[access boundary](copilot-radius-experiment-plan.md#authoring-and-diagnostic-github-access)
retains the current denial of general GitHub access in scored trials until an
explicit revision is approved. Radius tools remain part of the treatment, not
a promise of identical tools. This assessment does not block independent
fixture progress.

**M2 first increment:** directed answers and canonical endpoint mapping are
implemented. The shared diagnosis gate checks the hidden causal target and
requires incident-owned evidence review. The outcome resolver no longer treats
schema acceptance as success and requires scope, safety and cleanup evidence.
Offline planted controls use explicitly synthetic captures; they are not
production Shop incident graders. A subsequent offline increment implements the
immutable full roster, append-only SQLite attempts and captures, retry/exclusion
reduction, denominator reconciliation and source-replaying redacted exporter.
It verifies canonical outcomes against an explicitly registered, source-pinned
verifier and actual captured bytes; it does not authenticate Shop evidence.
The owner then approved human causal-prose adjudication with an incident rubric,
keeping automated target, measurement and integrity checks. The reference path
now implements that policy and completes M2's contract milestone: wrong
within-category mechanisms require an explicit negative human decision, while
unknown or ambiguous reviews remain unfinished without retry. Planted decisions
exercise the interface; they are not measured human agreement. See the
[answer/outcome contract](../../benchmark/README.md#m2-answer-and-outcome-contract-increment)
for the implemented boundary and its limitations.
The [bookkeeping and export contract](../../benchmark/README.md#m2-campaign-bookkeeping-and-export)
describes recovery, provenance, redaction and the remaining integration work.
The [criterion accounting](../../benchmark/README.md#m2-exit-criterion-accounting)
maps M2's full exit criteria to implemented controls and separates the later
live capture, operational review and incident-qualification responsibilities.

Each milestone is a reviewable PR or small sequence of PRs. Formal dependencies below
are explicit; environment work and answer/report contracts can proceed
independently. No date or campaign cost is promised before the first integrated
trial measures runtime and usage.

| Milestone | Work and dependencies | Exit criterion |
|---|---|---|
| M1. Reliable Shop environment | Derive the load script without the absent `ask_agent` endpoint; pin and vendor the Grafana plugin; split ingress/internal networks; resolve cgroups portably; include observed host class in footprint records; connect Shop readiness, flag state, throttling, and phase-specific load checks to the driver. | Healthy control has no unexplained endpoint failure or outbound request. Planted failures stop the trial. Records preserve measurements and verified cleanup. |
| M2. Answer and result contracts | Add directed `connection` answers, canonical naming, exact terminal classes, causal/evidence grading, and retry/exclusion rules. Define campaign assignments before execution and preserve every attempt. Build the report export boundary below. Independent of M1. | Correct answers pass; wrong component, edge, mechanism, correlated symptom, fabricated evidence, and wrong healthy/fault assertions fail. Empty evidence cannot pass. Duplicate attempts cannot inflate denominators. |
| M3. Sealed repository treatments | Build all three fixtures from one source snapshot; remove visible fault-flag code for diagnosis-only flag incidents; neutralize documentation; validate/freeze `app.bicep`, graph, source references, config, tools, and generic skills; generate the architecture document. Depends on the M1 deployment topology. | Difference manifests explain every treatment change. Leakage controls reject planted answers. Repeated standalone workspace creation is identical. Radius tools and skills are usable only in their assigned fixture, with no ambient host configuration. |
| M4. Qualification and confinement | Fit and hold out Shop tolerances; re-verify the locked SDK/CLI sandbox, diagnosis-only write prohibition, telemetry access, and compaction accounting. Depends on M1 and M3. | Frozen tolerances pass an independent holdout. Escape probes fail at the real boundary, a legitimate workspace control succeeds, and ordinary diagnosis tools work in every arm. The campaign refuses an unqualified host or unverified sandbox. |
| M5. First complete comparison | Wire Inspect scheduling, the SDK adapter, fixtures, environment, hidden validators, and append-only attempts. Run one incident and a healthy control across randomized three-arm sets. Depends on M2-M4. | One operator invocation produces validated terminal records, verified cleanup, report JSON/CSV, and a dashboard. Interrupt/resume and injected infrastructure failures preserve assignments and retry once without double counting. Label these runs smoke, not findings. |
| M6. Incident library and pilot | Expand to the canonical incident target with external faults, hidden variants, misleading symptoms, and healthy controls. Run pilot and scored-model calibration on disjoint seeds. Depends on M5. | Each retained incident passes independent activation and grader controls, native difficulty lies in the planned band, and blinded reviewer agreement meets the plan. Record measured runtime and cost per run. |
| M7. Freeze campaign and hosts | Use pilot variance to power the specified effect; commit sample size, seeds, analysis and failure policy. Build a pinned harness image, then provision and qualify approved Linux VM hosts. Depends on M6. | Pre-registration and all input hashes are frozen. Each host passes its own holdout; model availability, budget allowance, redaction, and recovery procedures are confirmed. |
| M8. Execute and publish | Run the scheduled campaign without interim arm comparisons. Resolve or explicitly exclude every assignment, reproduce incident-clustered inference, and publish redacted artifacts. Depends on M7. | Dashboard and downloads reconcile with canonical records. Report both Radius contrasts, clustered uncertainty, multiplicity correction, per-model results, healthy false alarms, exclusions, review agreement, and exploratory efficiency measures. |

### Operator workflow to implement

The future command surface must cover fixture build/verification, host
qualification, campaign preparation, run/resume, analysis, and export. These
are requirements for the integrated workflow. The offline preparation, accounting
and export module is implemented; the live run/resume workflow is not.
Configuration must pin model,
runtime, fixtures, incident versions, seeds, host class, tool policy and budget.
A prepared campaign contains its entire assignment roster, including runs not
yet started. Resume uses that roster and never silently creates new trials.

The first exploratory end-to-end run is the integration checkpoint. Do not expand the
incident library or provision scored hosts until it establishes that the agent
can actually use the Radius additions and that the same graders work for all
arms. Do not count synthetic dashboard fixtures or catalogue determinism runs
as Astronomy Shop comparison evidence.

There is no integrated exploratory command yet. `ShopEnvironment.run_healthy`
currently finishes and tears down an environment sample; `incidents.py` targets
the catalogue application. The next slice supplies a real Shop fault/healthy
lifecycle and capture binding to `CampaignStore`, an incident-owned verifier
and rubric, and the sealed fixtures required for the SDK session. It must not
promote `CPUReference` snapshots into authenticated Shop evidence. Keep the
existing report schema and dashboard rather than adding a reporting architecture.

### Approval and cost gates

Ask the owner before any model call, naming the expected requests and allowance.
This includes Radius/document authoring, sandbox registration, smoke runs,
calibration, and the pilot. Model calls and billable requests are different;
report both measured quantities rather than assuming one request per tool call.
The integration run measures cost; the development batch allowance follows that
measurement, including setup and human review. Approval of this policy is not
approval of any live request budget.
Before M4's laptop fit/holdout, request approval for approximately three hours.
Other runs expected to exceed an hour need a separate estimate and approval.
Before M7, request VM and storage cost approval using measured Shop/harness
demand. This plan and dashboard work authorize none of those live operations.

## Results dashboard and download contract

**Approved delivery:** a self-contained `benchmark/dashboard.html`, opened
locally without a server or dependencies. Import a JSON report through a file
picker. No data leaves the browser. No model calls, registry requests, telemetry,
or cloud resources are required. The initial screen says no campaign is loaded;
it never displays fabricated benchmark results.

**Implemented on main:** the dashboard, import consistency checks,
descriptive summaries, model/incident filters, trial details, and JSON/CSV
downloads, with an offline source-replaying campaign exporter. Open the HTML
file directly in a browser. Automated checks exercise
the shipped JavaScript, UI event wiring, exports, and guard mutations without
third-party JavaScript packages. CI runs those checks through the Python suite
with Node.js available. Export requires a trusted registered verifier for
terminal records; the built-in reference CLI installs its unqualified CPU
reviewer explicitly. Qualified production Shop reviewers are not installed. Historical run
JSON cannot be imported as comparison data.

This is a reporting boundary, not a replacement for Inspect, canonical run
records, or the pre-registered analysis. The first dashboard shows descriptive
results only. M8 must add the reviewed analysis export and clustered intervals;
the initial UI must not invent confidence intervals or significance.

### Versioned input

`schemaVersion` is `radius-comparison-v1`, distinct from historical smoke and
environment records. The root contains `campaign` and `runs`.

`campaign` contains nonempty `id`, `benchmarkCommit`, and `analysisPlan` strings,
`phase` (`smoke`, `pilot`, or `scored`), and `status` (`running` or `complete`).
For smoke/pilot work, `analysisPlan` may explicitly say `not-preregistered`.
A scored campaign requires a Git commit hash for `analysisPlan`.

`runs` is the complete, nonempty assignment roster. Each row contains:

| Field | Contract |
|---|---|
| `runId`, `pairId` | Unique logical assignment ID; matched-set ID shared across arms. |
| `arm` | `native`, `architecture`, or `radius`. |
| `model`, `incident`, `seed`, `configurationId` | Nonempty strings. Model includes its pinned version. Configuration identifies the immutable non-treatment configuration. Rows in a matched set must agree on these fields. |
| `expectedFault` | Boolean, including false for healthy controls; constant within a matched set. |
| `status` | `pending`, `running`, `excluded`, or one of `validated_success`, `diagnosis_failure`, `budget_exhaustion`, `no_submission`, `invalid_structured_output`, `refusal`, `isolation_violation_attempt`. |
| `attempts`, `harnessFailures` | Integers. Pending has zero of both. Running has one attempt and zero failures while active, one attempt and one failure while waiting for retry, or two attempts and one failure while the retry is active. An agent result has one or two attempts and exactly `attempts - 1` harness failures. Excluded has two attempts and two harness failures, per retry policy. |
| `recordDigest` | SHA-256 reference to the canonical terminal record; null while pending/running. |
| `reason` | Nonempty for exclusions; string otherwise. No secret-bearing logs. |
| `validators` | Object. A validated success requires `diagnosis`, `evidence`, `scope`, `safety`, and `cleanup` all equal to `pass`. Other rows may carry their actual results or an empty object, not assumed passes. |
| `reportedFault` | Boolean or null if no valid answer exists. Success must agree with `expectedFault`. |
| `agentSeconds`, `toolCalls`, `aiCredits` | Nonnegative finite numbers or null when unavailable; tool calls are integers. Final-attempt metrics, not retry-inclusive campaign cost. Pending metrics are null. |

Every matched set declares each arm exactly once, including pending assignments.
The dashboard rejects unknown schemas/statuses, duplicate assignments,
inconsistent matching, malformed numeric values, and a complete campaign with
unfinished runs. It validates report consistency, not the truth of a claimed
digest or grade. The offline exporter verifies these against captured artifacts
and registered verifier replay, redacts before export, and retains every
underlying attempt separately. Source authentication and production incident
grading still require the later integration milestones.

### Views and denominators

Show campaign identity, phase, benchmark revision, analysis-plan reference,
planned/completed/scored/excluded runs and failed-attempt counts. Filter by
model and incident. Never silently pool model-specific treatment effects.

For each arm, show successful/scored runs, a pass-rate bar, exclusions, pending
work, and medians with available-sample counts for final-attempt time, tools and
AI credits. Missing data renders unavailable, not zero. Healthy controls show
false alarms over scored healthy runs, with missing valid answers separately
counted, not silently treated as correct healthy diagnoses.

For each model, show Radius-minus-native and Radius-minus-architecture
pass-rate differences using only complete, scored matched pairs for that
contrast, with the matched denominator and omitted-pair count. These are
descriptive percentage-point differences, not confidence intervals, significance,
or a graph/skill attribution. Unpaired arm summaries are labeled descriptive.
Trial rows expose statuses, validator results, record digest, and exclusion
reason as text, never executable HTML.

For an in-progress scored campaign, show only overall progress. Hide arm
comparisons and outcome rows until completion, consistent with no interim
looks. Smoke and pilot screens are prominently labeled non-findings.

### Downloads and checks

Download the full validated campaign JSON, preserving the roster and provenance,
regardless of filters. Download a filtered trial-level CSV with explicit
headers, arm/model/incident/status, attempt counts, metrics, validator fields,
digest, and reasons. Label download scope; JSON is the lossless format.
CSV must quote values and neutralize spreadsheet formula prefixes. A future
M8 exporter also packages redacted raw records, telemetry, transcripts, checksums,
and the analysis report; the initial dashboard does not claim to download
artifacts it has not received.

Use synthetic fixtures only inside automated tests. Demonstrate that empty
data, a corrupted schema, duplicate/retried assignments, a forged success
without passing gates, unmatched pairs, missing metrics, hostile text and
spreadsheet formulas cannot silently produce a valid result. Mutation checks
must show the schema, roster, success, and denominator guards are exercised.
Exercise import, filtering, download contents, and the scored-campaign embargo.
Test offline with `DOCKER_HOST=unix:///nonexistent/docker.sock`; no live
benchmark is needed to verify reporting logic.
