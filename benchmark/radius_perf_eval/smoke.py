"""Phase 1 smoke run: Copilot SDK instrumentation spike.

Runs one harmless, non-scored prompt end to end and emits the artifact set the
experiment plan requires, plus the negative isolation and budget-termination
evidence.

This is *not* a scored trial. The prompt is deliberately trivial and mentions
nothing about Radius, graphs, caches, or root causes, so nothing here can leak
into a real condition.

Artifacts written to ``--output``:

* ``events.jsonl``            ordered, monotonic session event log
* ``assistant-usage.jsonl``   verbatim per-call usage payloads
* ``session-usage-metrics.json`` raw accumulated-usage RPC response
* ``model-catalog.json``      account model catalog (raw + normalized)
* ``provenance.json``         SDK/CLI/runtime/OS/model provenance
* ``usage-normalized.json``   normalized record + reconciliation
* ``isolation-report.json``   negative-test results
* ``run.json``                run summary
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys
from pathlib import Path
from typing import Any

from .copilot import (
    SessionBudget,
    SpikeSession,
    TemporaryWorkspace,
    fetch_model_catalog,
)
from .events import EventRecorder, MonotonicClock
from .isolation_probe import run_isolation_probes
from .usage import (
    TokenOverlapPolicy,
    build_normalized_record,
    normalize_from_metrics,
    normalize_from_usage_events,
    reconcile,
)
from .versions import capture_provenance, sha256_of_json

DEFAULT_MODEL = "gpt-5.4"

#: Harmless and scenario-free on purpose.
SMOKE_PROMPT = (
    "Read the file notes.txt in the current directory and reply with its exact "
    "contents and nothing else."
)

SMOKE_FILE_NAME = "notes.txt"
SMOKE_FILE_BODY = "hello from the radius-perf-eval smoke workspace\n"

#: A second, still harmless prompt that drives real escape attempts through the
#: runtime so the permission handler is exercised end to end rather than only
#: in unit tests. Nothing here references a benchmark scenario.
ESCAPE_PROBE_PROMPT = (
    "Run these three commands one at a time and tell me the exact result of "
    "each, including any error text. Do not try to work around a failure.\n"
    "1. cat ../escape-probe-parent.txt\n"
    "2. cat /etc/hosts\n"
    "3. echo probe > /tmp/radius-perf-escape-probe.txt\n"
)

#: Written outside the workspace so probe 1 targets a file that really exists;
#: containment, not absence, must be what stops the read.
ESCAPE_BAIT_NAME = "escape-probe-parent.txt"


ARTIFACT_NAMES = (
    "events.jsonl",
    "assistant-usage.jsonl",
    "session-usage-metrics.json",
    "model-catalog.json",
    "provenance.json",
    "usage-normalized.json",
    "isolation-report.json",
    "live-escape-probe.json",
    "budget-termination.json",
    "run.json",
)


def _clear_previous_artifacts(output_dir: Path) -> None:
    """Start every run from a clean artifact set.

    ``events.jsonl`` is opened in append mode so a crash keeps its prefix. That
    makes stale records from an earlier run indistinguishable from this one's,
    so the previous set is removed before a new run begins.
    """
    for name in ARTIFACT_NAMES:
        target = output_dir / name
        if target.exists():
            target.unlink()


def _write_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, indent=2, default=str, sort_keys=True), encoding="utf-8")


def _write_jsonl(path: Path, rows: list[Any]) -> None:
    with path.open("w", encoding="utf-8") as handle:
        for row in rows:
            handle.write(json.dumps(row, default=str, sort_keys=True) + "\n")


async def run_smoke(
    *,
    model: str,
    output_dir: Path,
    wall_clock_ms: float,
    max_model_requests: int | None,
    max_tool_calls: int | None,
    max_ai_credits: float | None,
    prompt_timeout_s: float,
    verify_budget_termination: bool,
    live_escape_probe: bool = True,
) -> dict[str, Any]:
    from copilot import CopilotClient

    output_dir.mkdir(parents=True, exist_ok=True)
    _clear_previous_artifacts(output_dir)
    clock = MonotonicClock()
    recorder = EventRecorder(output_dir / "events.jsonl", clock=clock)

    client = CopilotClient(use_logged_in_user=True, log_level="error")
    summary: dict[str, Any] = {"schemaVersion": "spike-v1"}
    live_escape: dict[str, Any] | None = None

    try:
        recorder.record("harness", "client.start", {})
        await client.start()

        auth = await client.get_auth_status()
        auth_payload = {
            "isAuthenticated": getattr(auth, "isAuthenticated", None),
            "authType": getattr(auth, "authType", None),
            "host": getattr(auth, "host", None),
            "login": getattr(auth, "login", None),
        }
        recorder.record("harness", "auth.status", auth_payload)
        if not auth_payload["isAuthenticated"]:
            raise RuntimeError(f"not authenticated to Copilot: {auth_payload}")

        catalog = await fetch_model_catalog(client)
        catalog["catalogDigest"] = sha256_of_json(catalog["raw"])
        _write_json(output_dir / "model-catalog.json", catalog)
        recorder.record(
            "harness",
            "models.catalog",
            {"modelCount": catalog["modelCount"], "digest": catalog["catalogDigest"]},
        )

        available = {m["id"] for m in catalog["normalized"]}
        if model not in available:
            raise RuntimeError(
                f"pinned model {model!r} is not in the account catalog. "
                f"Available: {sorted(available)}"
            )
        pinned = next(m for m in catalog["normalized"] if m["id"] == model)

        # -- main smoke session -----------------------------------------
        with TemporaryWorkspace() as workspace:
            workspace.write(SMOKE_FILE_NAME, SMOKE_FILE_BODY)

            isolation = run_isolation_probes(workspace, recorder=recorder)
            _write_json(output_dir / "isolation-report.json", isolation)

            budget = SessionBudget(
                wall_clock_ms=wall_clock_ms,
                max_model_requests=max_model_requests,
                max_tool_calls=max_tool_calls,
                max_ai_credits=max_ai_credits,
            )
            session = SpikeSession(
                client=client,
                workspace=workspace,
                recorder=recorder,
                model=model,
                budget=budget,
            )
            await session.start()
            provenance = capture_provenance(
                model=model,
                reasoning_effort=None,
                session={
                    "sessionId": session._session.session_id,
                    "workspaceRoot": str(workspace.root),
                    "contextPolicy": "cold",
                    "memoryEnabled": False,
                    "autoRouting": False,
                    "subagentsEnabled": False,
                },
            ).to_json_dict()
            _write_json(output_dir / "provenance.json", provenance)

            try:
                outcome = await session.run_prompt(
                    SMOKE_PROMPT, timeout_s=prompt_timeout_s
                )
            finally:
                await session.close()

        if live_escape_probe:
            live_escape = await _run_live_escape_probe(
                client=client,
                recorder=recorder,
                model=model,
                prompt_timeout_s=prompt_timeout_s,
            )
            _write_json(output_dir / "live-escape-probe.json", live_escape)

        _write_jsonl(output_dir / "assistant-usage.jsonl", outcome.assistant_usage_events)
        _write_json(
            output_dir / "session-usage-metrics.json",
            {
                "response": outcome.session_metrics,
                "error": outcome.session_metrics_error,
                "experimental": True,
            },
        )

        from_events = normalize_from_usage_events(
            outcome.assistant_usage_events, policy=TokenOverlapPolicy.UNKNOWN
        )
        from_metrics = (
            normalize_from_metrics(outcome.session_metrics, policy=TokenOverlapPolicy.UNKNOWN)
            if outcome.session_metrics
            else None
        )
        rec = reconcile(from_events, from_metrics)
        normalized = build_normalized_record(
            model=model,
            agent_wall_clock_ms=outcome.agent_wall_clock_ms,
            logical_tool_calls=outcome.tool_timeline.get("logicalToolCalls", 0),
            tool_attempts=outcome.tool_timeline.get("toolAttempts", 0),
            from_events=from_events,
            from_metrics=from_metrics,
            reconciliation=rec,
            premium_request_multiplier=pinned.get("billingMultiplier"),
            pricing_version=catalog["catalogDigest"],
        )
        _write_json(output_dir / "usage-normalized.json", normalized)

        # -- budget termination proof ------------------------------------
        budget_proof: dict[str, Any] | None = None
        if verify_budget_termination:
            budget_proof = await _prove_budget_termination(
                client=client, recorder=recorder, model=model, prompt_timeout_s=prompt_timeout_s
            )
            _write_json(output_dir / "budget-termination.json", budget_proof)

        summary.update(
            {
                "model": model,
                "terminalClass": outcome.terminal_class,
                "sessionId": outcome.session_id,
                "auth": auth_payload,
                "agentWallClockMs": outcome.agent_wall_clock_ms,
                "modelRequests": normalized["modelRequests"],
                "logicalToolCalls": normalized["logicalToolCalls"],
                "toolAttempts": normalized["toolAttempts"],
                "usage": normalized["usage"],
                "credits": normalized["credits"],
                "reconciliation": normalized["reconciliation"],
                "modelRequestDurationsMs": outcome.model_request_durations_ms,
                "toolTimeline": outcome.tool_timeline,
                "isolation": isolation,
                "liveEscapeProbe": live_escape,
                "isolationViolationsDuringRun": outcome.isolation_violations,
                "permissionDecisions": outcome.permission_decisions,
                "subagentEventCount": outcome.subagent_events,
                "budgetTermination": budget_proof,
                "sessionMetricsAvailable": outcome.session_metrics is not None,
                "sessionMetricsError": outcome.session_metrics_error,
                "finalMessage": outcome.final_message,
                "error": outcome.error,
                "eventCount": recorder.count,
                "provenance": provenance,
                "modelCatalogDigest": catalog["catalogDigest"],
            }
        )
        _write_json(output_dir / "run.json", summary)
        return summary
    finally:
        recorder.record("harness", "client.stop", {})
        try:
            await client.stop()
        except Exception:  # pragma: no cover - defensive
            pass
        recorder.close()


async def _prove_budget_termination(
    *,
    client: Any,
    recorder: EventRecorder,
    model: str,
    prompt_timeout_s: float,
) -> dict[str, Any]:
    """Prove a session is terminated by a cap rather than running to completion.

    Uses a one-model-request cap: the session is aborted as soon as the first
    ``assistant.usage`` event lands, which exercises the same path a real trial
    would take on budget exhaustion.
    """
    with TemporaryWorkspace() as workspace:
        workspace.write(SMOKE_FILE_NAME, SMOKE_FILE_BODY)
        budget = SessionBudget(max_model_requests=1, wall_clock_ms=120_000)
        session = SpikeSession(
            client=client,
            workspace=workspace,
            recorder=recorder,
            model=model,
            budget=budget,
        )
        await session.start()
        try:
            outcome = await session.run_prompt(
                "List every file in the current directory, then describe each one "
                "in a short sentence.",
                timeout_s=prompt_timeout_s,
            )
        finally:
            await session.close()

    return {
        "budget": {"maxModelRequests": 1},
        "terminalClass": outcome.terminal_class,
        "budgetStopReason": outcome.budget_stop_reason,
        "modelRequests": len(outcome.assistant_usage_events),
        "terminatedByBudget": outcome.terminal_class == "budget_exhaustion",
    }


async def _run_live_escape_probe(
    *,
    client: Any,
    recorder: EventRecorder,
    model: str,
    prompt_timeout_s: float,
) -> dict[str, Any]:
    """Drive real escape attempts through the live runtime.

    Runs in its own session with ``allow_shell=True`` so the command screen is
    what blocks the escape. With the production default (shell off) every one
    of these is denied trivially, which proves much less. Unit tests prove the
    policy denies an escaping request; this proves the policy is actually
    wired into a running session and that nothing escapes in practice.
    """
    with TemporaryWorkspace() as workspace:
        workspace.write(SMOKE_FILE_NAME, SMOKE_FILE_BODY)
        bait = workspace.root.parent / ESCAPE_BAIT_NAME
        bait.write_text("BENCHMARK-ESCAPE-CANARY\n", encoding="utf-8")
        absolute_target = Path("/tmp") / "radius-perf-escape-probe.txt"
        if absolute_target.exists():
            absolute_target.unlink()

        session = SpikeSession(
            client=client,
            workspace=workspace,
            recorder=recorder,
            model=model,
            budget=SessionBudget(wall_clock_ms=180_000, max_model_requests=12),
            allow_shell=True,
        )
        await session.start()
        recorder.record("harness", "isolation.live-probe.start", {"bait": str(bait)})
        try:
            outcome = await session.run_prompt(
                ESCAPE_PROBE_PROMPT, timeout_s=prompt_timeout_s
            )
        finally:
            await session.close()

        workspace_contents = sorted(p.name for p in workspace.root.iterdir())
        decisions = session.policy.decisions
        violations = session.policy.violations

    transcript = outcome.final_message or ""
    canary_leaked = "BENCHMARK-ESCAPE-CANARY" in transcript
    absolute_write_succeeded = absolute_target.exists()

    try:
        bait.unlink()
    except OSError:
        pass
    if absolute_target.exists():
        absolute_target.unlink()

    report = {
        "prompt": ESCAPE_PROBE_PROMPT,
        "shellAllowedSoTheScreenIsUnderTest": True,
        "terminalClass": outcome.terminal_class,
        "permissionDecisionsObserved": decisions,
        "shellRequestsSeen": sum(1 for d in decisions if d.get("kind") == "shell"),
        "shellRequestsApproved": sum(
            1 for d in decisions if d.get("kind") == "shell" and d.get("approved")
        ),
        "violationsObserved": violations,
        "canaryContentLeakedIntoTranscript": canary_leaked,
        "absolutePathWriteSucceeded": absolute_write_succeeded,
        "workspaceContents": workspace_contents,
        "finalMessage": transcript,
    }
    report["allEscapesBlocked"] = (
        not canary_leaked
        and not absolute_write_succeeded
        and report["shellRequestsApproved"] == 0
    )
    recorder.record("harness", "isolation.live-probe.result", report)
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="radius-perf-smoke", description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL, help="explicit pinned model id")
    parser.add_argument(
        "--output", type=Path, default=Path("artifacts/smoke"), help="artifact directory"
    )
    parser.add_argument("--wall-clock-ms", type=float, default=300_000.0)
    parser.add_argument("--max-model-requests", type=int, default=20)
    parser.add_argument("--max-tool-calls", type=int, default=20)
    parser.add_argument("--max-ai-credits", type=float, default=30.0)
    parser.add_argument("--prompt-timeout-s", type=float, default=180.0)
    parser.add_argument(
        "--skip-live-escape-probe",
        action="store_true",
        help="skip the in-session escape attempt (saves a model request)",
    )
    parser.add_argument(
        "--skip-budget-termination-check",
        action="store_true",
        help="skip the extra session that proves budget termination",
    )
    args = parser.parse_args(argv)

    if args.model.strip().lower() == "auto":
        parser.error("auto routing is prohibited; pass an explicit model id")

    summary = asyncio.run(
        run_smoke(
            model=args.model,
            output_dir=args.output,
            wall_clock_ms=args.wall_clock_ms,
            max_model_requests=args.max_model_requests,
            max_tool_calls=args.max_tool_calls,
            max_ai_credits=args.max_ai_credits,
            prompt_timeout_s=args.prompt_timeout_s,
            verify_budget_termination=not args.skip_budget_termination_check,
            live_escape_probe=not args.skip_live_escape_probe,
        )
    )

    print(json.dumps(summary, indent=2, default=str, sort_keys=True))
    probe = summary.get("liveEscapeProbe")
    ok = (
        summary.get("terminalClass") == "validated_success"
        and summary.get("isolation", {}).get("allFailedClosed") is True
        and summary.get("subagentEventCount") == 0
        and (probe is None or probe.get("allEscapesBlocked") is True)
    )
    return 0 if ok else 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
