# Pinned Radius overlay: capture and static review

The merged model can be imported reproducibly, but it is **not a trial-ready
treatment**. Its source and generation provenance check out. Static inspection
finds missing runtime configuration in the authored model and a deployment-profile
mismatch with the benchmark. No application or Radius command was run in this
review. Compose rendering used an unreachable Docker socket, the committed CPU
class, and the arm64 plugin asset, not observed host facts or a live deployment.

## Exact capture

| Input | Revision or digest |
|---|---|
| Upstream Shop 3.1.0 | `dedc0178918e260823323b8d95005a8cb924b007` |
| Application repository | [`ryanwaite/astronomy-shop-radius`](https://github.com/ryanwaite/astronomy-shop-radius) |
| Authoring source commit / tree | `12dbef5dfde1df1b902ca74b7d2b03ada89eceef` / `7d9c5587e18627943c8237229b1fea0293c958b5` |
| Model commit in [ryanwaite/astronomy-shop-radius#1](https://github.com/ryanwaite/astronomy-shop-radius/pull/1) | `a6b16a0cc281fc595c5e10f37549daff20568213` |
| Merged application commit / tree | `dce2f8f596e2eda9d7d07c114cb44749dc27acb0` / `8f62374c9924fee5182b87358cd35c02b7d1b1ea` |
| GitHub application archive SHA-256 | `e8a15244c19f8896de4e9699da50e3b0c6fec4096f953c0042b69d811cbeaaf0` |
| Raw `app.bicep` SHA-256 | `c6bfefa7fd5d2577bcb764e7a4f0dc2d6bddd416541623312b09ecff24df74e1` |
| Radius normalized-text SHA-256 | `81cf697706f4584d4ef93f66d7e75a573b9b7e6a418601eca0f54d0039e65f59` |
| Model Git blob | `178a3fb455ba82f38b13548e4166d28109ed0416` |
| Origin generation time / reported generator version | `2026-10-03T03:20:19.777Z` / `0.2.0` |
| Native source tar SHA-256 | `0d4e4b4a8ccf4fdbb3f4a53f6014a3d316cc7fe1c036813ee9c7aba6e5b194ec` |
| Overlay tar SHA-256 | `24a5ddb2a95d87c04b6393974c96821af76abe7220a5a34e6d6a5bd74583b92a` |
| Native-plus-overlay tar SHA-256 | `ab0933a8b113be0d9660942cf6c7413b673a5f0617a12bcd0502f4a7614f1ffb` |

**Correction to the initial inspection:** the different raw and origin hashes
are not stale provenance. The Radius
[writer contract](https://github.com/radius-project/ai-extensions/blob/24ad63f740a199e5769b8589668c35d525faa13f/extensions/radius/skills/radius-app-bicep/scripts/write-app-origin.mjs#L56-L67)
normalizes CRLF, strips trailing spaces/tabs on each line, and trims trailing
whitespace before hashing. That calculation matches `app.origin.json` exactly.
Archive manifests deliberately hash raw bytes. Both measurements are retained.
There is no evidence of a provenance-update defect or reason to regenerate on
account of this hash difference.

The complete baseline-to-merged Git archive comparison adds only
`.radius/.gitignore`, `.radius/app.bicep`, `.radius/app.origin.json`, and
`.radius/bicepconfig.json`. The importer compares every other path, byte and
mode against the independently reconstructed upstream source export.

The source tar and Git tree differ at `src/ad/gradlew.bat` and
`src/fraud-detection/gradlew.bat`: the tar preserves CRLF, while the baseline
commit stores LF under `* text=auto`. Their raw SHA-256 values are respectively
`d539676c48b596afda64c963ec8f7ee56c7b3fe7e3b81d1dbe2d1a1e3dd9e9f8`
and `59328c7a17f673b1a63040bfb380a0c749e5d6df3406f7f18641060314cd9aa1`.
The separately captured baseline archive confirms this predates model authoring.
The importer accounts for precisely these paths when comparing committed source;
it does **not** normalize either materialized draft. Native and Radius copies
retain identical source-tar bytes and modes. Original receipts remain unchanged.
Their synthetic Git trees reproduce the recorded baseline and merged trees;
the Radius draft's synthetic commit is not the application's historical commit.

## What the static model describes

The literal resource and source-reference inventory is in `static-review.json`.
Every captured `codeReference` and build `dockerfile` resolves to a file in the
merged archive; every explicit line fragment is in range. These checks establish
resolvable locations, not that a link supplies a runtime configuration or is the
best process entrypoint. For example, the database link to `init.sql` does not
mount or execute that file.

The model represents the synchronous shop services, Flagd, Valkey and PostgreSQL.
Its host expressions preserve connections such as frontend to checkout/cart,
checkout to payment/product-catalog/shipping, recommendation to product-catalog,
shipping to quote, and cart to Valkey. The represented listeners agree with the
source port defaults. These are static source facts, not observed reachability.

The benchmark renderer inspected at `609cc27` combines `compose.yaml`, `compose.full.yaml`,
and `compose.observability.yaml` before applying its committed transformations.
It uses digest-pinned images and derived load, proxy, collector and Grafana
configuration. The model instead builds source images for `linux/amd64` and
describes unmodified source settings. A build-source hash is not equivalence
with these runtime images, mounted assets, CPU limits or network policy.

| Difference | Evidence and consequence | Minimal owner-assisted follow-up |
|---|---|---|
| Full application profile absent | `accounting`, `fraud-detection` and `kafka` are active in rendered Compose and declared in `compose.full.yaml`. The model's checkout block, `.radius/app.bicep:788-834`, lacks `KAFKA_ADDR`; `src/checkout/main.go:248-255` enables its producer only when that setting exists. This removes an application dependency, not merely a visual infrastructure node. | Select the ordinary full Shop profile for the benchmark-target model and preserve producer/consumer and database relationships. |
| Shared diagnostic topology absent | `otel-collector`, `grafana`, `jaeger`, `prometheus`, `opensearch` and `opamp-server` are active in the benchmark. Collector is also in core `compose.yaml`. The model has neither those resources nor explicit external telemetry endpoints. Native `OTEL_*` configuration is absent across the represented workloads. | Represent the shared diagnostic dependencies, whether as modeled services or explicitly supplied external endpoints under the chosen profile. Do not remove shared diagnostics from the controls to match the incomplete model. |
| Required Flagd UI settings missing | Model `.radius/app.bicep:890-915` supplies only `FLAGD_UI_PORT` and `PHX_HOST`. `src/flagd-ui/Dockerfile:50-54,108-120` builds/runs production; `src/flagd-ui/config/runtime.exs:26-44` raises when either `SECRET_KEY_BASE` or `OTEL_EXPORTER_OTLP_ENDPOINT` is absent. | Bind a secure session-key input and the actual collector endpoint. This is a confirmed source-contract omission in the authored model, not a measured deployment failure. |
| Database bootstrap and relationship missing | Model `.radius/app.bicep:437-468` starts stock PostgreSQL with a password but does not mount `init.sql`; core `compose.yaml:812` does. `src/postgresql/init.sql` creates the application user, database, schema and seed data. Product-catalog's model at `654-690` reads a caller-supplied connection-string secret, with no reference tying it to the modeled database. | Preserve the bootstrap path and explicitly establish the product-catalog/database relationship. A source link and a separately supplied connection string do not prove either. |
| Proxy configuration incomplete | Model `.radius/app.bicep:1013-1080` omits the collector, Grafana, Jaeger and OpAMP settings used by `src/frontend-proxy/envoy.tmpl.yaml:182-198,273-303`. Its Dockerfile performs `envsubst` at startup and supplies no defaults for them. Benchmark-rendered environment and derived template differ from this source-built path. | Supply required native values and the selected-profile template, or a proven profile-specific alternative. Do not assume blank substitutions or a successful Bicep compile establish a usable proxy. |

The load-generator's literal `http://frontend-proxy-frontendProxy:8080` at model
line 991 is not by itself a generator defect. Proxy references load-generator in
the opposite direction; the skill explicitly permits a literal DNS cycle break.
Its namespace/recipe resolution and the graph's representation of that reverse
relationship still need extension evidence. Likewise, absence of optional
`agent`, `chatbot`, `mcp`, profiling or test workloads is not a missing active
service: those profiles are not selected by this benchmark. No optional-profile
failure is relabelled as an incident.

## Did graph generation lose the resources?

The retained authoring-session log identifies where selection occurred:

- At `2026-10-03T03:17:10.113Z`, the generation handoff listed source candidates
  including accounting, fraud-detection, Kafka, OpAMP and OpenSearch. Discovery
  had not lost those directories.
- At `03:19:41.379Z`, GPT-5.6 Sol explicitly announced the **core Compose
  profile**, then wrote the initial Bicep. The original user had requested the
  app graph, not the benchmark's full/observability deployment. The source
  archive contained all Compose layers and `compose.defaults`; it contained no
  benchmark plan or active renderer configuration.
- Reconstructing that initial write and the subsequent host-access syntax
  repair reproduces the merged Bicep byte-for-byte. The absent workloads and
  native settings were already absent at authoring. Validation returned exit
  zero at `03:20:15.280Z`; origin writing and promotion succeeded afterward.
- The only retained graph inspection used `missingOnly: true` and returned
  `ready: true, resources: []`. This supports the reported missing-reference
  check, not a positive inventory of the complete rendered graph. The later PR
  commit excluded the local `app-graph.json`; no graph payload was committed.

**Classification:** full-profile omissions are a confirmed profile mismatch
with the benchmark, not evidence of a renderer defect or a later benchmark
topology change. The
[authoring contract](https://github.com/radius-project/ai-extensions/blob/24ad63f740a199e5769b8589668c35d525faa13f/extensions/radius/skills/radius-app-bicep/SKILL.md#L80-L94)
allows selecting one runnable profile, but requires its native runtime contract,
bootstrap and dependencies. The Flagd UI and database omissions above therefore
identify confirmed defects in this generated model and missed semantic
validation. They are not proof that every generation reproduces the defect.
The proxy/telemetry findings also originate in authoring, not graph assembly.

The public checker at the same pinned revision checks compiled types, security
rules and source-reference shape; it is not a Compose/runtime-contract
equivalence checker. Its successful exit does not contradict these findings.
The pinned
[graph adapter](https://github.com/radius-project/ai-extensions/blob/24ad63f740a199e5769b8589668c35d525faa13f/packages/adapter-shared/src/rad.ts#L1604-L1657)
consumes supplied Bicep and intentionally filters image-build resources and their
registry credential secret from visualization. Those implementation-detail
exclusions are not lost application services. No graph assembly operation was
replayed here, so loss of any *present* application resource in the renderer
remains untested.

The inspected ai-extensions source revision is
`24ad63f740a199e5769b8589668c35d525faa13f`. Prior authoring-session diagnosis
reported mixed instruction/script installations; the handoff reported generator
`0.2.0` and the successful tool records identify `gpt-5.6-sol`. Those records do
not freeze the complete installed extension or CLI. The earlier environment-gate
refusal was already filed as
[radius-project/ai-extensions#962](https://github.com/radius-project/ai-extensions/issues/962);
it is separate from these generated-model omissions.

## Setup inventory and next access check

The repository carries Bicep, its origin, an extensibility flag and the
`br:biceptypes.azurecr.io/radius:0.61` alias. The alias is **not an installed CLI
version**. No tool manifest, generic Radius skill, instruction addition, graph
payload, environment definition or recipe pack was added. The complete examined
tree is retained, so these absences are not inferred from an empty search.
The Apache-2.0 source license is unchanged. No personal configuration,
credentials or skill folder was imported; licensing/provenance for any future
redistributed skill still requires review.

GitHub backing was required for the authoring handoff. Model image builds also
name the private Git repository. Neither fact proves that diagnosis needs
GitHub: every arm already has local source. The graph adapter can consume local
Bicep, but application-hosted tool exposure, source navigation, freshness checks
and registry resolution may have separate dependencies. Absence of repository
tool manifests does not mean those capabilities are unavailable in the app.
Installed CLI/extension versions, diagnostic tool schemas/versions and generic
skill exposure remain unknown.

The next owner check is to capture, **in the application session through the
Radius extension**, the installed versions, nonempty graph resource/relationship
inventory and exact diagnostic tools/skills exposed to a fresh scored agent.
For each capability, record whether it reads local files or needs GitHub/registry
access and which repository/ref or endpoint it reads. No benchmark graph view,
direct Radius executable, model call or deployment is authorized by this review.
The scored network policy remains unchanged pending an explicit owner decision.

Repair the selected application profile in the app repository before extracting
architecture facts. No architecture document or mechanical author fact list was
created from this unvalidated model. Isolated Claude Opus 5/high authoring,
retained prompt/transcript and +/-20% token parity against **all** Radius
additions remain required. If a fresh generation reproduction is desired,
propose one application-session attempt with an initial allowance of **20 model
requests**, stop before exceeding it, and obtain approval first; it is not
equivalent to one request because it is one session. Current static evidence
already identifies the source-contract omissions without such a run.

## Retained evidence

Raw captures are under
`../radius-perf-eval-artifacts/radius-import-20261003/`: exact archives,
`compare.json`, `tree.json`, `commit.json`, `pr1*.json`,
`baseline-to-merged.json`, `archive-source-diff.json`, `stack.json`,
`render.json`, `static-review.json`, generation-history excerpts and reconstructed
pre-promotion models, plus pinned public writer/checker/adapter source.
`generation-to-merged.diff` is empty **with both compared model hashes recorded
above**, not an independent pass-on-empty check. Early raw-hash interpretation
and failed test records are preserved. The executed README command produced
corrected captures under
`../radius-perf-eval-artifacts/radius-overlay-20261003T174517Z/`;
`verified-recreation.json` records both complete common-file inventories, raw
and normalized model hashes, artifact digests and matching Git trees.
The final difference-reason metadata was also recreated by the exact README
command under `../radius-perf-eval-artifacts/radius-overlay-20261003T174801Z/`;
the source, overlay and combined tar digests are unchanged.
The final focused and renderer logs are `pytest-import-final.log` and
`pytest-render.log` in the original capture directory; the final metadata checks
are in `pytest-import-metadata.log`.
Tests and guard/capture mutations run with
Docker unreachable. These records establish capture and static findings, not
incident-specific leakage clearance, graph/live parity, confinement or efficacy.
