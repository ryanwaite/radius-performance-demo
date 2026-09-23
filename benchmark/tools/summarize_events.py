"""Derive ``events-summary.json`` from a run's ``events.jsonl``.

The smoke run writes ``events.jsonl`` but not this summary, so re-running the
smoke leaves the summary stale. That is easy to miss, because every other file
in the evidence directory does get rewritten. Keeping the generator here means
the summary is reproducible and its wording lives in code rather than only in
the artifact it describes.

Usage::

    python tools/summarize_events.py [run-dir]

``run-dir`` defaults to the repository's ``artifacts/smoke``, the documented
``--output`` location for ``radius-perf-smoke``. The default is anchored to
this file rather than the working directory, so it resolves to the same place
regardless of where the command is invoked from.
"""

from __future__ import annotations

import collections
import json
import sys
from pathlib import Path

DEFAULT_RUN_DIR = Path(__file__).resolve().parents[2] / "artifacts" / "smoke"

NOTE = (
    "Summary of the full events.jsonl. The full log is generated output, written "
    "under artifacts/, which the root .gitignore ignores. Regenerate the log "
    "with: uv run radius-perf-smoke --model gpt-5.4 --output ../artifacts/smoke; "
    "then regenerate this summary with: python tools/summarize_events.py "
    "../artifacts/smoke"
)


def summarize(events: list[dict]) -> dict:
    counts = collections.Counter(
        e.get("type") or e.get("eventType") or "?" for e in events
    )
    seqs = [e["seq"] for e in events if "seq" in e]
    elapsed = [e["elapsedMs"] for e in events if "elapsedMs" in e]

    # sdkTypeRecognized is nested under "payload". Reading it at the top level
    # returns nothing and yields a count of zero, which reads as "the SDK
    # recognised everything" rather than as a broken query.
    unrecognized = sum(
        1 for e in events if (e.get("payload") or {}).get("sdkTypeRecognized") is False
    )

    return {
        "note": NOTE,
        "eventCount": len(events),
        "seqIsDenseAndOrdered": bool(seqs)
        and seqs == list(range(seqs[0], seqs[0] + len(seqs))),
        "elapsedIsMonotonic": all(a <= b for a, b in zip(elapsed, elapsed[1:])),
        "firstElapsedMs": elapsed[0] if elapsed else None,
        "lastElapsedMs": elapsed[-1] if elapsed else None,
        "sdkUnrecognizedTypeCount": unrecognized,
        "countsByType": dict(sorted(counts.items())),
    }


def main(argv: list[str]) -> int:
    if len(argv) > 2:
        print(__doc__, file=sys.stderr)
        return 2
    run_dir = Path(argv[1]) if len(argv) == 2 else DEFAULT_RUN_DIR
    log = run_dir / "events.jsonl"
    if not log.is_file():
        print(f"no events.jsonl in {run_dir}", file=sys.stderr)
        return 1

    events = [json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    summary = summarize(events)
    (run_dir / "events-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    print(
        f"events={summary['eventCount']} "
        f"unrecognized={summary['sdkUnrecognizedTypeCount']} "
        f"dense={summary['seqIsDenseAndOrdered']} "
        f"monotonic={summary['elapsedIsMonotonic']}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
