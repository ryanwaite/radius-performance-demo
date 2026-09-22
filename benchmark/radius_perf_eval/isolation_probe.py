"""Negative isolation probes against a real temporary workspace.

The experiment plan requires proof that the agent cannot read or write outside
its assigned workspace ("Isolation exit criteria"). These probes exercise the
real filesystem -- including a real symlink -- rather than mocks, so symlink
resolution is genuinely tested.

Each probe builds a permission request shaped like the runtime's and asserts
the policy denies it. A probe that is *approved* is a failure.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .copilot import IsolationPolicy, TemporaryWorkspace
from .events import EventRecorder

__all__ = ["ProbeResult", "build_probes", "run_isolation_probes"]


@dataclass
class _FakeRequest:
    """Minimal stand-in with the same attribute surface the policy reads."""

    kind: str
    path: str | None = None
    resolved_path: str | None = None
    file_name: str | None = None
    full_command_text: str | None = None
    possible_paths: list[str] | None = None
    possible_urls: list[Any] | None = None
    request_sandbox_bypass: bool | None = None
    url: str | None = None


@dataclass
class ProbeResult:
    name: str
    expectation: str
    approved: bool
    reason: str

    @property
    def passed(self) -> bool:
        # Every probe in this suite must be denied.
        return not self.approved

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "expectation": self.expectation,
            "approved": self.approved,
            "reason": self.reason,
            "passed": self.passed,
        }


def build_probes(workspace: TemporaryWorkspace) -> list[tuple[str, str, _FakeRequest]]:
    """Construct the negative probes, creating real on-disk bait."""
    root = workspace.root

    # A real symlink inside the workspace pointing at a real directory outside
    # it. Containment must survive following this link.
    escape_link = root / "escape-link"
    if not escape_link.exists():
        try:
            escape_link.symlink_to(root.parent, target_is_directory=True)
        except OSError:  # pragma: no cover - platform dependent
            pass

    outside_file = Path(os.sep) / "etc" / "hosts"

    probes: list[tuple[str, str, _FakeRequest]] = [
        (
            "parent-directory-read",
            "reading ../ must be denied",
            _FakeRequest(kind="read", path=str(root.parent)),
        ),
        (
            "relative-parent-traversal-read",
            "reading ../../etc via a relative path must be denied",
            _FakeRequest(kind="read", path="../../etc"),
        ),
        (
            "absolute-path-read",
            "reading an absolute path outside the workspace must be denied",
            _FakeRequest(kind="read", path=str(outside_file)),
        ),
        (
            "symlink-escape-read",
            "reading through an in-workspace symlink that points outside must be denied",
            _FakeRequest(kind="read", path=str(escape_link)),
        ),
        (
            "absolute-path-write",
            "writing to an absolute path outside the workspace must be denied",
            _FakeRequest(
                kind="write",
                file_name=str(Path(os.sep) / "tmp" / "radius-perf-escape.txt"),
            ),
        ),
        (
            "symlink-escape-write",
            "writing through an escaping symlink must be denied",
            _FakeRequest(kind="write", file_name=str(escape_link / "escape.txt")),
        ),
        (
            "shell-absolute-path-empty-possible-paths",
            "a shell command naming an absolute path must be denied even though "
            "the runtime leaves possible_paths empty",
            _FakeRequest(
                kind="shell",
                full_command_text="cat /etc/hosts",
                possible_paths=[],
                possible_urls=[],
            ),
        ),
        (
            "shell-parent-traversal-empty-possible-paths",
            "a shell command using ../ must be denied with no structured path hint",
            _FakeRequest(
                kind="shell",
                full_command_text="cat ../escape-probe-parent.txt",
                possible_paths=[],
                possible_urls=[],
            ),
        ),
        (
            "shell-absolute-write-redirection",
            "a shell redirection writing outside the workspace must be denied",
            _FakeRequest(
                kind="shell",
                full_command_text="echo probe > /tmp/radius-perf-escape-probe.txt",
                possible_paths=[],
                possible_urls=[],
            ),
        ),
        (
            "shell-parent-path",
            "a shell command touching a parent path must be denied",
            _FakeRequest(
                kind="shell",
                full_command_text=f"cat {root.parent}/secret",
                possible_paths=[str(root.parent / "secret")],
                possible_urls=[],
            ),
        ),
        (
            "shell-sandbox-bypass",
            "a shell command requesting a sandbox bypass must be denied",
            _FakeRequest(
                kind="shell",
                full_command_text="sudo sh -c 'echo hi'",
                possible_paths=[],
                possible_urls=[],
                request_sandbox_bypass=True,
            ),
        ),
        (
            "network-url",
            "network access must be denied",
            _FakeRequest(kind="url", url="https://example.invalid/answers"),
        ),
        (
            "memory-access",
            "memory must be denied",
            _FakeRequest(kind="memory"),
        ),
        (
            "unknown-permission-kind",
            "an unrecognized permission kind must fail closed",
            _FakeRequest(kind="some-future-kind"),
        ),
    ]
    return probes


def run_isolation_probes(
    workspace: TemporaryWorkspace,
    *,
    recorder: EventRecorder | None = None,
) -> dict[str, Any]:
    """Run every negative probe and summarize. All must fail closed."""
    # allow_shell=True deliberately: it forces the shell probes through the
    # command screen instead of being denied trivially by the default
    # shell-off policy, so the screen itself is what is under test.
    policy = IsolationPolicy(workspace_root=workspace.root, allow_shell=True)
    results: list[ProbeResult] = []

    for name, expectation, request in build_probes(workspace):
        approved, reason = policy.evaluate(request)
        result = ProbeResult(
            name=name, expectation=expectation, approved=approved, reason=reason
        )
        results.append(result)
        if recorder is not None:
            recorder.record("harness", "isolation.probe", result.to_json_dict())

    # A positive control: a legitimate in-workspace read must still be allowed,
    # otherwise "all denied" would be trivially satisfied by denying everything.
    control_request = _FakeRequest(kind="read", path="notes.txt")
    control_approved, control_reason = policy.evaluate(control_request)

    summary = {
        "workspaceRoot": str(workspace.root),
        "shellScreenUnderTest": True,
        "productionDefaultAllowsShell": IsolationPolicy(
            workspace_root=workspace.root
        ).allow_shell,
        "probes": [r.to_json_dict() for r in results],
        "allFailedClosed": all(r.passed for r in results),
        "failedProbes": [r.name for r in results if not r.passed],
        "positiveControl": {
            "name": "in-workspace-read",
            "approved": control_approved,
            "reason": control_reason,
            "passed": control_approved,
        },
    }
    if recorder is not None:
        recorder.record(
            "harness",
            "isolation.summary",
            {
                "allFailedClosed": summary["allFailedClosed"],
                "failedProbes": summary["failedProbes"],
                "positiveControlPassed": control_approved,
            },
        )
    return summary
