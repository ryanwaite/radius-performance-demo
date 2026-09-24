"""Benchmark harness for the Radius performance experiment.

This package is *benchmark control-plane* code. It must never be included in a
fixture handed to an evaluated agent (see
``docs/specs/copilot-radius-experiment-plan.md``, "Repository fixture and
workspace isolation").

Three increments live here so far:

- Copilot SDK instrumentation (``copilot``, ``events``, ``usage``,
  ``versions``).
- The deterministic Compose trial driver (``environment``, ``compose``,
  ``incidents``, ``load``, ``telemetry``, ``manifest``, ``trials``), which
  creates and destroys a verified, isolated environment per scored trial. See
  ``benchmark/README.md``.

- Sandbox wiring, trial budgets, and the submit tool (``sandbox``,
  ``submit_tool``, ``trial_outcome``), which put the runtime sandbox in force,
  cap each trial, and receive the agent's answer.

Fixture building, scenarios, and hidden validators live in later increments.
"""

from __future__ import annotations

__all__ = [
    "__version__",
    "cli",
    "compose",
    "copilot",
    "docker_cli",
    "environment",
    "events",
    "images",
    "incidents",
    "load",
    "manifest",
    "sandbox",
    "submit_tool",
    "telemetry",
    "trial_outcome",
    "trials",
    "usage",
    "versions",
]

__version__ = "0.1.0"
