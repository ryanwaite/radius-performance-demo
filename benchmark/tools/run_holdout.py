"""Holdout runner for the Compose trial driver.

A holdout is not just a suite run. It is a suite run against gates that were
frozen *before* it started, which makes two things load-bearing:

1. The tree must be unmodified. Editing the driver between freeze and
   execution turns a test of the gates back into a fitting of them, and the
   report would not say so. This refuses to run against a dirty worktree.

2. The artifacts must outlive the session that produced them. An earlier
   holdout was killed mid-suite by host sleep, and the one artifact that could
   have explained an unexplained 1.85s stall had already been cleaned up from
   under the worktree. Pass an ``artifact-dir`` outside the repository.

This lived outside the repository while the tree was frozen, for reason 1.
That is not a property of the file, though -- it is enforced by the dirty-tree
refusal below, which works wherever the file sits. Keeping it in the
repository means the harness that produced the evidence ships with the code
the evidence is about.

``run_suite`` writes ``determinism-report.json`` and ``provenance.json`` into
the suite directory itself, so this adds only the freeze check and a non-zero
exit status when the exit criterion is not met -- the part a CI job or a shell
loop needs.

Usage:

    python3.12 benchmark/tools/run_holdout.py <repo-root> <artifact-dir> <suite-id>

Exit status: 0 met, 1 not met, 2 dirty worktree, 3 the suite raised.
"""

from __future__ import annotations

import json
import subprocess
import sys
import traceback
from pathlib import Path


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print(__doc__, file=sys.stderr)
        return 64
    repo_root = Path(argv[0]).resolve()
    artifact_dir = Path(argv[1]).resolve()
    suite_id = argv[2]

    sys.path.insert(0, str(repo_root / "benchmark"))
    from radius_perf_eval.incidents import MYSQL_POOL_DELAY_V1
    from radius_perf_eval.trials import run_suite

    artifact_dir.mkdir(parents=True, exist_ok=True)

    dirty = subprocess.run(
        ["git", "-C", str(repo_root), "status", "--porcelain"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    commit = subprocess.run(
        ["git", "-C", str(repo_root), "rev-parse", "HEAD"],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()
    print(f"[holdout] commit {commit} clean={not dirty}", flush=True)
    if dirty:
        print("[holdout] REFUSING: worktree is dirty\n" + dirty, flush=True)
        return 2

    try:
        report = run_suite(
            repo_root=repo_root,
            cycles=10,
            suite_id=suite_id,
            incident=MYSQL_POOL_DELAY_V1,
            results_dir=artifact_dir,
            pull=True,
        )
    except Exception:
        (artifact_dir / "FAILED.txt").write_text(
            traceback.format_exc(), encoding="utf-8"
        )
        print("[holdout] suite raised:", flush=True)
        traceback.print_exc()
        return 3

    suite_dir = artifact_dir / suite_id
    power = report.get("hostPower", {})
    print(
        f"[holdout] exitCriterionMet={report['exitCriterionMet']} "
        f"successful={report['successfulCycles']}/{report['cycles']} "
        f"in {report['wallClockSeconds']}s "
        f"power={power.get('atStart', {}).get('source')}"
        f"->{power.get('atEnd', {}).get('source')}\n"
        f"[holdout] report: {suite_dir / 'determinism-report.json'}",
        flush=True,
    )
    if not (suite_dir / "determinism-report.json").exists():
        # The report is the deliverable. A suite that ran and left nothing
        # behind is indistinguishable from one that never ran.
        print("[holdout] REPORT MISSING despite a completed suite", flush=True)
        (artifact_dir / "FAILED.txt").write_text(
            json.dumps(report, indent=2, default=str), encoding="utf-8"
        )
        return 3
    return 0 if report["exitCriterionMet"] else 1


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
