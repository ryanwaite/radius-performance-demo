"""Benchmark harness for the Radius performance experiment.

This package is *benchmark control-plane* code. It must never be included in a
fixture handed to an evaluated agent (see
``docs/specs/copilot-radius-experiment-plan.md``, "Repository fixture and
workspace isolation").

Increment 1 scope: GitHub Copilot SDK instrumentation only. Fixture building,
the Compose trial driver, scenarios, and validators live in later increments.
"""

from __future__ import annotations

__all__ = [
    "__version__",
    "copilot",
    "events",
    "usage",
    "versions",
]

__version__ = "0.1.0"
