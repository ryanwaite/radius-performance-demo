# Working in this repository

This repository holds a benchmark. It asks whether a Radius-enabled repository, with `app.bicep`, an application graph, and Radius skills, makes the GitHub Copilot agent better at diagnosing performance incidents in the OpenTelemetry Astronomy Shop. The agent is compared across three arms: the native repository, the native repository with an architecture document, and the Radius-enabled repository.

## Where to start

1. Read [Current state and next steps](docs/specs/copilot-radius-experiment-plan.md#current-state-and-next-steps) in the experiment plan. It lists what is built and the next steps in order, each with an exit criterion.
2. The [experiment plan](docs/specs/copilot-radius-experiment-plan.md) is the canonical design. Where another document disagrees with it, the plan wins.
3. [`benchmark/README.md`](benchmark/README.md) documents the harness, driver, sandbox, and submit tool, how to run the tests, and the detailed requirements for work that is not yet built.

## Rules

### Changes and approval

- Every change, including a change to the plan, goes through a pull request. Never commit to `main`.
- The repository owner merges. Don't merge your own pull request.
- A decision the plan does not already settle goes to the owner. Record it in the plan, in a pull request, before building on it.
- Update [Current state and next steps](docs/specs/copilot-radius-experiment-plan.md#current-state-and-next-steps) in the same pull request as the work it describes.
- Ask the owner before any of these:
  - a run expected to take more than one hour; give an estimate first;
  - any live model call or premium request, including smoke runs; say how many requests you expect;
  - provisioning cloud resources;
  - changing the Docker Desktop allocation, or the Docker, Python, SDK, or CLI version, during a campaign, since each changes the host class or fingerprint;
  - deleting Docker images, volumes, or build cache that you didn't create.

### Packages

- On the developer machine, Python and npm packages come only from Microsoft Central Feed Services (CFS). The plan's [Package supply chain compliance](docs/specs/copilot-radius-experiment-plan.md#package-supply-chain-compliance) section gives the configuration.
- Never add a second package index or fall back to a public registry. If CFS lacks a package or version, stop and ask. A CFS exception is a human decision.
- GitHub-hosted CI is the one exception. It installs from PyPI with every hash required, from a file exported from `uv.lock`, and never resolves versions.

### Evidence

These rules come from defects that this project has already shipped and then caught.

- **A check must not pass on no evidence.** A negative check ("nothing bad happened") must show what it examined. An empty collection, an unreachable branch, or a default that reads as success is a failure, not a pass.
- **Give every check a positive control.** Show that it fails when the fault it guards against is present. A test that asserts absence also passes when its detector is broken.
- **Mutation-test new guards.** Delete or weaken each condition and confirm a test fails. A condition whose removal breaks nothing is dead code.
- **Measure the independent variable,** not only the outcome. For example, check that the load generator actually delivered its load before trusting healthy latency.
- **A documented command is a claim.** Run it exactly as written before merge, and check that it does what the text says.
- **Don't put counts in prose.** Test counts and similar figures go stale unchecked. Point to the CI log or to a generated file instead.
- **A test that claims not to need Docker** runs with `DOCKER_HOST=unix:///nonexistent/docker.sock`, as CI does.
- **Write records as you go.** Save raw measurements before summarising them, so a crash late in a run doesn't lose the data.
- **Report measurement and inference separately,** and correct an earlier claim openly in a later commit rather than by rewriting history.

### Evidence locations

- Run artifacts go in `../radius-perf-eval-artifacts/`, beside this checkout, not in the repository. Don't delete existing artifacts.
- Host qualification records are machine-local, in `~/.radius-perf-eval/qualifications.json`, because each describes one physical machine.

## Writing

Write documentation, commit messages, and pull request descriptions in plain, direct prose. Name who did what, state what was measured, and say what remains unknown. Avoid em-dashes.
