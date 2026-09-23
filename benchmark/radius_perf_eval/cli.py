"""Command line entry point for the Compose trial driver."""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from .docker_cli import DockerError, daemon_info
from .environment import EnvironmentSpec, TrialEnvironment
from .incidents import INCIDENTS, MYSQL_POOL_DELAY_V1
from .trials import HEALTHY_PROFILE, INCIDENT_PROFILE, run_suite

DEFAULT_REPO_ROOT = Path(__file__).resolve().parents[2]


def _default_run_id() -> str:
    return f"run-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}"


def _add_common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--repo-root", type=Path, default=DEFAULT_REPO_ROOT)
    parser.add_argument("--results-dir", type=Path, default=None)
    parser.add_argument("--scenario", default=MYSQL_POOL_DELAY_V1.scenario_id, choices=sorted(INCIDENTS))
    parser.add_argument("--no-pull", action="store_true", help="skip registry pulls, use local images")


def cmd_doctor(args: argparse.Namespace) -> int:
    info = daemon_info()
    print(json.dumps(info, indent=2, sort_keys=True))
    return 0


def cmd_trial(args: argparse.Namespace) -> int:
    incident = INCIDENTS[args.scenario]
    environment = TrialEnvironment(
        args.run_id,
        repo_root=args.repo_root,
        spec=EnvironmentSpec(),
        incident=incident,
        results_dir=args.results_dir,
        pull=not args.no_pull,
    )
    try:
        environment.create()
        environment.verify_start_state()
        environment.measure(HEALTHY_PROFILE.name, HEALTHY_PROFILE)
        environment.inject_incident()
        environment.measure(INCIDENT_PROFILE.name, INCIDENT_PROFILE)
        if args.revert:
            environment.revert_incident()
    finally:
        environment.destroy()
        run_dir = environment.write_artifacts()

    manifest = environment.manifest
    print(json.dumps(manifest.to_dict(), indent=2, sort_keys=True))
    print(f"\nartifacts: {run_dir}", file=sys.stderr)
    if not manifest.signed_off:
        print(
            "manifest NOT signed off; failed gates: "
            + (", ".join(gate.name for gate in manifest.failed_gates) or "none")
            + "; gates never recorded: "
            + (", ".join(manifest.missing_gates) or "none"),
            file=sys.stderr,
        )
        return 1
    return 0


def cmd_determinism(args: argparse.Namespace) -> int:
    report = run_suite(
        repo_root=args.repo_root,
        cycles=args.cycles,
        suite_id=args.suite_id,
        incident=INCIDENTS[args.scenario],
        results_dir=args.results_dir,
        pull=not args.no_pull,
    )
    print(json.dumps(report, indent=2, sort_keys=True, default=str))
    return 0 if report["exitCriterionMet"] else 1


def cmd_cleanup(args: argparse.Namespace) -> int:
    """Remove any stray radius-eval-* projects left by an interrupted run."""
    from .compose import ComposeProject, parse_resource_lines
    from .docker_cli import docker

    names = parse_resource_lines(
        docker("ps", "--all", "--format", "{{.Label \"com.docker.compose.project\"}}").stdout
    )
    projects = sorted({name for name in names if name.startswith("radius-eval-")})
    volume_names = parse_resource_lines(docker("volume", "ls", "--format", "{{.Name}}").stdout)
    projects += sorted(
        {
            name.split("_", 1)[0]
            for name in volume_names
            if name.startswith("radius-eval-") and "_" in name
        }
    )
    removed = []
    for project in sorted(set(projects)):
        residue = ComposeProject(project=project, env={}).destroy()
        removed.append({"project": project, "clean": residue.clean, "residue": residue.to_dict()})
    print(json.dumps({"removed": removed}, indent=2, sort_keys=True))
    return 0 if all(entry["clean"] for entry in removed) else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="radius-perf-eval-env",
        description="Deterministic Docker Compose trial driver for the Radius performance benchmark.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    doctor = subparsers.add_parser("doctor", help="check that the Docker daemon is reachable")
    doctor.set_defaults(func=cmd_doctor)

    trial = subparsers.add_parser("trial", help="run one create/inject/load/measure/destroy cycle")
    _add_common(trial)
    trial.add_argument("--run-id", default=None)
    trial.add_argument("--revert", action="store_true", help="also verify incident deactivation")
    trial.set_defaults(func=cmd_trial)

    determinism = subparsers.add_parser(
        "determinism", help="run N cycles and report run-to-run variance"
    )
    _add_common(determinism)
    determinism.add_argument("--cycles", type=int, default=10)
    determinism.add_argument("--suite-id", default=None)
    determinism.set_defaults(func=cmd_determinism)

    cleanup = subparsers.add_parser("cleanup", help="remove stray radius-eval-* Compose projects")
    cleanup.set_defaults(func=cmd_cleanup)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "run_id", "sentinel") is None:
        args.run_id = _default_run_id()
    try:
        return int(args.func(args))
    except DockerError as exc:
        print(f"docker error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
