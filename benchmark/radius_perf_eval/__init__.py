"""Benchmark harness for the Radius performance experiment.

This package is *benchmark control-plane* code. It must never be included in a
fixture handed to an evaluated agent (see
``docs/specs/copilot-radius-experiment-plan.md``, "Repository fixture and
workspace isolation").

Two increments live here so far:

- Copilot SDK instrumentation (``copilot``, ``events``, ``usage``,
  ``versions``).
- The deterministic Compose trial driver (``environment``, ``compose``,
  ``incidents``, ``load``, ``telemetry``, ``manifest``, ``trials``), which
  creates and destroys a verified, isolated environment per scored trial. See
  ``benchmark/README.md``.

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
    "telemetry",
    "trials",
    "usage",
    "versions",
]

__version__ = "0.1.0"
