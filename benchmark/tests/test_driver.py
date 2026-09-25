"""Docker-free unit tests for the Compose trial driver.

These cover the logic that must be correct before any container starts:
manifest construction and sign-off gating, dynamic port parsing, digest pinning,
teardown-verification parsing, and load statistics.

Run with either ``python -m unittest discover -s benchmark/tests`` or pytest.
"""

from __future__ import annotations

import json
import math
import statistics
import shutil
import subprocess
import sys
import tempfile
import time
import types
import unittest
import unittest.mock
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radius_perf_eval.compose import (  # noqa: E402
    COMPOSE_DIR,
    ComposeError,
    ComposeProject,
    ResidualResources,
    parse_port_mapping,
    parse_resource_lines,
)
from radius_perf_eval import astronomy_shop  # noqa: E402
import pathlib
from radius_perf_eval import cpu_limits  # noqa: E402
import inspect
from radius_perf_eval import checks  # noqa: E402
from radius_perf_eval import shop_readiness  # noqa: E402
from radius_perf_eval import environment as environment_module  # noqa: E402
from radius_perf_eval.environment import (  # noqa: E402
    EGRESS_EXCEPTIONS,
    EnvironmentSpec,
    ServiceResources,
    TrialEnvironment,
)
from radius_perf_eval.checks import (  # noqa: E402
    REDACTED,
    REQUIRED_CHECK_KINDS,
    CheckPlan,
    CheckPlanError,
    ComposeModel,
    NetworkModel,
    ServiceCheck,
    ServiceModel,
    generate_check_plan,
    parse_compose_config,
    parse_memory,
    parse_nano_cpus,
    reconcile,
    redact_env,
    summarise_problems,
)
from radius_perf_eval.hostclass import (  # noqa: E402
    HostClassError,
    HostFacts,
    derive_class_id,
    derive_fingerprint,
    gibibytes,
    observe_host,
)
from radius_perf_eval.images import PinnedImage, is_digest  # noqa: E402
from radius_perf_eval.qualification import (  # noqa: E402
    MIN_REQUALIFICATION_CYCLES,
    QualificationError,
    Requalification,
    compare_fingerprints,
    evaluate_scored_readiness,
    load_requalifications,
    record_requalification,
)
from radius_perf_eval.incidents import (  # noqa: E402
    MYSQL_POOL_DELAY_V1,
    IncidentVerification,
    VerificationCheck,
)
from radius_perf_eval.load import (  # noqa: E402
    LoadProfile,
    Sample,
    detect_stalls,
    percentile,
    run_load,
    stall_events,
)
from radius_perf_eval.manifest import (  # noqa: E402
    CATALOG_APPLICATION_GATES,
    FIXTURE_PATHS,
    LIFECYCLE_GATES,
    EnvironmentManifest,
    canonical_json,
    fixture_files,
    hash_fixture,
    hash_text,
)
from radius_perf_eval.telemetry import HTTP_LATENCY_QUANTILE, TelemetryWindow  # noqa: E402
from radius_perf_eval.trials import (  # noqa: E402
    DECLARED_TOLERANCES,
    DRIFT_SENSITIVE_METRICS,
    FROZEN_TOLERANCE_SETS,
    INCIDENT_PROFILE,
    ToleranceSet,
    LAPTOP_M5_CLASS_ID,
    MAX_ERROR_RATE,
    MAX_HOST_SUSPENSION_SECONDS,
    MAX_STALL_RATE,
    STRUCTURAL_EXPECTATIONS,
    CycleResult,
    build_report,
    UNEXPLAINED_STALL,
    _write_json,
    annotate_power,
    host_power_state,
    parse_power_state,
    host_suspension_seconds,
    resolve_tolerance_set,
    suite_provenance,
    summarise,
)

DIGEST = "sha256:" + "a" * 64


class PortAllocationTests(unittest.TestCase):
    def test_parses_ipv4_mapping(self) -> None:
        self.assertEqual(parse_port_mapping("127.0.0.1:55403"), ("127.0.0.1", 55403))

    def test_parses_bracketed_ipv6_mapping(self) -> None:
        self.assertEqual(parse_port_mapping("[::1]:49222"), ("::1", 49222))

    def test_normalises_wildcard_host_to_loopback(self) -> None:
        self.assertEqual(parse_port_mapping("0.0.0.0:32770"), ("127.0.0.1", 32770))

    def test_uses_last_line_when_docker_emits_several(self) -> None:
        self.assertEqual(parse_port_mapping("\n127.0.0.1:1234\n"), ("127.0.0.1", 1234))

    def test_rejects_unparsable_output(self) -> None:
        for bad in ("", "invalid IP:0", "nonsense"):
            with self.subTest(bad=bad):
                with self.assertRaises(ComposeError):
                    parse_port_mapping(bad)

    def test_ports_are_never_fixed_in_the_compose_template(self) -> None:
        template = (
            Path(__file__).resolve().parents[1] / "radius_perf_eval" / "compose" / "base.yml"
        ).read_text(encoding="utf-8")
        for line in template.splitlines():
            stripped = line.strip()
            if stripped.startswith("- \"127.0.0.1"):
                # Must be host-port-less: "127.0.0.1::<container>", not "127.0.0.1:<host>:<container>".
                self.assertIn("::", stripped, f"fixed host port in template: {stripped}")


class DigestPinningTests(unittest.TestCase):
    def test_recognises_well_formed_digest(self) -> None:
        self.assertTrue(is_digest(DIGEST))

    def test_rejects_malformed_digests(self) -> None:
        for bad in ("", "sha256:xyz", "latest", "sha256:" + "a" * 63, "md5:" + "a" * 64):
            with self.subTest(bad=bad):
                self.assertFalse(is_digest(bad))

    def test_registry_pin_serialises_repo_digest(self) -> None:
        image = PinnedImage(
            service="mysql",
            reference=f"mysql@{DIGEST}",
            image_id=DIGEST,
            kind="registry",
            source="mysql:8.4",
            repo_digest=f"mysql@{DIGEST}",
        )
        payload = image.to_dict()
        self.assertEqual(payload["reference"], f"mysql@{DIGEST}")
        self.assertEqual(payload["kind"], "registry")
        self.assertNotIn(":8.4", payload["reference"])

    def test_spec_pins_no_floating_tag_for_locally_built_image(self) -> None:
        image = PinnedImage(
            service="catalog-api",
            reference=DIGEST,
            image_id=DIGEST,
            kind="local-build",
            source="radius-perf-eval/catalog-api:trial",
            repo_digest=None,
        )
        self.assertTrue(is_digest(image.reference))


class TeardownVerificationTests(unittest.TestCase):
    def test_blank_output_yields_no_resources(self) -> None:
        self.assertEqual(parse_resource_lines("\n  \n"), ())

    def test_strips_and_preserves_order(self) -> None:
        self.assertEqual(parse_resource_lines(" a \nb\n\nc "), ("a", "b", "c"))

    def test_clean_residue_reports_clean(self) -> None:
        residue = ResidualResources()
        self.assertTrue(residue.clean)
        self.assertIn("no residual", residue.describe())

    def test_any_residue_fails_closed(self) -> None:
        for category, value in (
            ("containers", "abc"),
            ("volumes", "radius-eval-x_mysql-data"),
            ("networks", "radius-eval-x_data"),
            ("orphans", "stray"),
        ):
            with self.subTest(category=category):
                residue = ResidualResources(**{category: (value,)})
                self.assertFalse(residue.clean)
                self.assertIn(value, residue.describe())

    def test_residue_serialises_every_category(self) -> None:
        residue = ResidualResources(containers=("c",), volumes=("v",), networks=("n",))
        self.assertEqual(
            json.loads(residue.describe()),
            {"containers": ["c"], "volumes": ["v"], "networks": ["n"], "orphans": []},
        )


class ComposeProjectTests(unittest.TestCase):
    def test_rejects_invalid_project_names(self) -> None:
        for bad in ("Radius-Eval", "-leading", "has space", "a" * 70, ""):
            with self.subTest(bad=bad):
                with self.assertRaises(ComposeError):
                    ComposeProject(project=bad, env={})

    def test_accepts_generated_project_name(self) -> None:
        project = ComposeProject(project="radius-eval-suite-c01", env={})
        self.assertEqual(project.project, "radius-eval-suite-c01")

    def test_rejects_missing_compose_file(self) -> None:
        with self.assertRaises(ComposeError):
            ComposeProject(project="radius-eval-x", env={}, files=[Path("/nope/missing.yml")])


CATALOG_SERVICES = ("catalog-api", "mysql", "prometheus", "valkey")


def catalog_model(extra: dict[str, ServiceModel] | None = None) -> ComposeModel:
    """The catalog stack as `docker compose config --format json` describes it.

    Hand-built so the generation tests need no Docker daemon. A separate test
    asserts these service names still match `compose/base.yml`, so the fixture
    cannot quietly drift away from the file it stands in for.
    """
    services = {
        "mysql": ServiceModel(
            name="mysql",
            image="mysql@sha256:" + "a" * 64,
            environment={"MYSQL_DATABASE": "catalog", "MYSQL_ROOT_PASSWORD": "rootpw"},
            networks=("data",),
            nano_cpus=1_000_000_000,
            memory_bytes=1024**3,
        ),
        "valkey": ServiceModel(
            name="valkey",
            image="valkey@sha256:" + "b" * 64,
            networks=("data",),
            nano_cpus=500_000_000,
            memory_bytes=256 * 1024**2,
        ),
        "catalog-api": ServiceModel(
            name="catalog-api",
            image="sha256:" + "c" * 64,
            environment={"CACHE_ENABLED": "false", "MYSQL_DSN": "catalog:pw@tcp(mysql:3306)/x"},
            networks=("data", "edge"),
            nano_cpus=1_000_000_000,
            memory_bytes=512 * 1024**2,
            published_ports=(8080,),
        ),
        "prometheus": ServiceModel(
            name="prometheus",
            image="prom@sha256:" + "d" * 64,
            networks=("data", "edge"),
            nano_cpus=500_000_000,
            memory_bytes=512 * 1024**2,
            published_ports=(9090,),
        ),
    }
    services.update(extra or {})
    return ComposeModel(
        services=services,
        networks={
            "data": NetworkModel(name="data", internal=True, full_name="proj_data"),
            "edge": NetworkModel(name="edge", internal=False, full_name="proj_edge"),
        },
        volumes=("mysql-data", "prometheus-data"),
    )


def catalog_plan(model: ComposeModel | None = None) -> CheckPlan:
    model = model or catalog_model()
    return generate_check_plan(
        model,
        readiness_probes=set(model.service_names),
        egress_exceptions=EGRESS_EXCEPTIONS,
    )


def required_gates(plan: CheckPlan | None = None) -> frozenset[str]:
    """What the driver requires once it has generated a plan."""
    plan = plan or catalog_plan()
    return LIFECYCLE_GATES | CATALOG_APPLICATION_GATES | plan.gate_names


class ManifestTests(unittest.TestCase):
    def _manifest(self) -> EnvironmentManifest:
        manifest = EnvironmentManifest(run_id="run-1", compose_project="radius-eval-run-1")
        manifest.require_gates(catalog_plan().gate_names | CATALOG_APPLICATION_GATES)
        return manifest

    def test_manifest_exposes_required_schema_keys(self) -> None:
        manifest = self._manifest()
        manifest.add_gate("only", True)
        payload = manifest.to_dict()
        for key in (
            "runId",
            "composeProject",
            "imageDigests",
            "fixtureHash",
            "seedCount",
            "cacheKeys",
            "resourceLimits",
            "ports",
            "incidentActive",
            "readinessVerified",
            "cleanupVerified",
        ):
            self.assertIn(key, payload)

    def _complete(self) -> EnvironmentManifest:
        """A manifest with every required gate recorded and passing."""
        manifest = self._manifest()
        for name in sorted(required_gates()):
            manifest.add_gate(name, True)
        return manifest

    def test_unsigned_without_any_gate(self) -> None:
        self.assertFalse(self._manifest().signed_off)

    def test_signed_only_when_every_gate_passes(self) -> None:
        manifest = self._complete()
        self.assertTrue(manifest.signed_off)
        manifest.add_gate("b", False, "seed rows 9 != 10")
        self.assertFalse(manifest.signed_off)
        self.assertEqual([g.name for g in manifest.failed_gates], ["b"])

    def test_unsigned_when_a_required_gate_was_never_recorded(self) -> None:
        """The defect that seven interrupted cycles signed off through.

        A cycle that died before `compose up` still reached teardown, recorded
        `cleanup-verified`, and signed off on that one gate while reporting
        readinessVerified false and seedCount 0. Nothing had failed, because
        almost nothing had run.
        """
        manifest = self._manifest()
        manifest.add_gate("cleanup-verified", True, "no residue")

        self.assertEqual([g.name for g in manifest.failed_gates], [])
        self.assertFalse(
            manifest.signed_off,
            "a manifest must earn sign-off by recording every verification, "
            "not by avoiding a failed one",
        )
        self.assertNotIn("cleanup-verified", manifest.missing_gates)
        self.assertIn("application-readiness", manifest.missing_gates)
        self.assertEqual(len(manifest.missing_gates), len(required_gates()) - 1)

    def test_every_required_gate_is_individually_load_bearing(self) -> None:
        """Positive control: drop exactly one gate at a time and confirm each
        one alone is enough to withhold sign-off. Without this, the required
        set could name a gate the driver never emits and nobody would notice."""
        for omitted in sorted(required_gates()):
            with self.subTest(omitted=omitted):
                manifest = self._manifest()
                for name in sorted(required_gates() - {omitted}):
                    manifest.add_gate(name, True)
                self.assertFalse(manifest.signed_off)
                self.assertEqual(manifest.missing_gates, [omitted])

    def test_a_manifest_with_no_plan_still_requires_the_lifecycle_gates(self) -> None:
        """The floor before a plan exists.

        If `require_gates` is never called, the requirement is the lifecycle
        set, which includes `check-plan-generated`. A run that died before
        generating a plan therefore cannot sign off on whatever it managed to
        record, which is the failure mode the derived requirement could
        otherwise reintroduce: no plan, no required per-service gates, nothing
        missing.
        """
        manifest = EnvironmentManifest(run_id="r", compose_project="radius-eval-r")
        self.assertEqual(manifest.required_gates, LIFECYCLE_GATES)
        manifest.add_gate("cleanup-verified", True)
        self.assertFalse(manifest.signed_off)
        self.assertIn("check-plan-generated", manifest.missing_gates)

    def test_extra_gates_do_not_block_sign_off(self) -> None:
        """`trial --revert` adds incident-inactive-verified. Required is a
        floor, not an exact set."""
        manifest = self._complete()
        manifest.add_gate("incident-inactive-verified", True)
        self.assertTrue(manifest.signed_off)

    def test_missing_gates_are_reported_in_the_manifest_body(self) -> None:
        manifest = self._manifest()
        manifest.add_gate("cleanup-verified", True)
        payload = manifest.to_dict()
        self.assertFalse(payload["signedOff"])
        self.assertIn("application-readiness", payload["missingGates"])

    def test_required_gates_match_what_the_driver_emits(self) -> None:
        """Pin the derived requirement against the gates a real cycle records.

        The per-service half is now generated, so this no longer guards against
        a hand-written list drifting. It guards against the generator changing
        a gate name, which would leave the driver recording gates nothing
        requires and requiring gates nothing records.
        """
        observed = {
            "check-plan-generated",
            "check-plan-covers-compose-services",
            "image-pinned:mysql",
            "image-pinned:valkey",
            "image-pinned:catalog-api",
            "image-pinned:prometheus",
            "resource-limits:mysql",
            "resource-limits:valkey",
            "resource-limits:catalog-api",
            "resource-limits:prometheus",
            "environment-variables:mysql",
            "environment-variables:valkey",
            "environment-variables:catalog-api",
            "environment-variables:prometheus",
            "egress:mysql",
            "egress:valkey",
            "egress:catalog-api",
            "egress:prometheus",
            "readiness:mysql",
            "readiness:valkey",
            "readiness:catalog-api",
            "readiness:prometheus",
            "application-readiness",
            "mysql-seed-rows",
            "valkey-empty",
            "catalog-api-image-hermetic",
            "incident-active-verified",
            "cleanup-verified",
        }
        self.assertEqual(set(required_gates()), observed)

    def test_digest_changes_when_body_changes(self) -> None:
        manifest = self._manifest()
        manifest.add_gate("a", True)
        first = manifest.to_dict()["manifestDigest"]
        manifest.seed_count = 10
        self.assertNotEqual(first, manifest.to_dict()["manifestDigest"])

    def test_digest_is_stable_for_identical_bodies(self) -> None:
        first, second = self._manifest(), self._manifest()
        for manifest in (first, second):
            manifest.add_gate("a", True)
            manifest.seed_count = 10
            manifest.cache_keys = 0
        self.assertEqual(first.to_dict()["manifestDigest"], second.to_dict()["manifestDigest"])

    def test_canonical_json_is_key_order_independent(self) -> None:
        self.assertEqual(canonical_json({"b": 1, "a": 2}), canonical_json({"a": 2, "b": 1}))

    def test_written_manifest_round_trips(self) -> None:
        manifest = self._complete()
        with tempfile.TemporaryDirectory() as tmp:
            path = manifest.write(Path(tmp) / "nested" / "environment-manifest.json")
            payload = json.loads(path.read_text(encoding="utf-8"))
        self.assertTrue(payload["signedOff"])
        self.assertEqual(payload["runId"], "run-1")


class FixtureHashTests(unittest.TestCase):
    def test_hash_is_deterministic_and_content_sensitive(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "a.txt").write_text("alpha", encoding="utf-8")
            (root / "b.txt").write_text("beta", encoding="utf-8")
            first, files = hash_fixture(root, ["a.txt", "b.txt"])
            second, _ = hash_fixture(root, ["b.txt", "a.txt"])
            self.assertEqual(first, second, "hash must not depend on input ordering")
            self.assertEqual(set(files), {"a.txt", "b.txt"})

            (root / "b.txt").write_text("beta!", encoding="utf-8")
            changed, _ = hash_fixture(root, ["a.txt", "b.txt"])
            self.assertNotEqual(first, changed)

    def test_missing_fixture_path_fails_closed(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(FileNotFoundError):
                hash_fixture(Path(tmp), ["absent.txt"])

    def test_hash_text_is_prefixed(self) -> None:
        self.assertTrue(hash_text("x").startswith("sha256:"))


class ResourceLimitTests(unittest.TestCase):
    def test_cpu_and_memory_convert_to_daemon_units(self) -> None:
        resources = ServiceResources(cpus="1.0", memory="512m")
        self.assertEqual(resources.nano_cpus, 1_000_000_000)
        self.assertEqual(resources.memory_bytes, 536_870_912)

    def test_fractional_cpu_and_gigabyte_memory(self) -> None:
        resources = ServiceResources(cpus="0.5", memory="1g")
        self.assertEqual(resources.nano_cpus, 500_000_000)
        self.assertEqual(resources.memory_bytes, 1_073_741_824)

    def test_every_service_declares_limits(self) -> None:
        spec = EnvironmentSpec()
        self.assertEqual(
            set(spec.resources), {"mysql", "valkey", "catalog-api", "prometheus"}
        )


class IncidentTests(unittest.TestCase):
    def test_variant_declares_a_constrained_pool_and_delay(self) -> None:
        variant = MYSQL_POOL_DELAY_V1
        self.assertEqual(variant.expected_container_env["DB_MAX_OPEN_CONNS"], "2")
        self.assertEqual(variant.expected_container_env["DB_READ_DELAY"], "250ms")

    def test_baseline_and_incident_states_differ(self) -> None:
        variant = MYSQL_POOL_DELAY_V1
        self.assertNotEqual(variant.expected_container_env, variant.baseline_container_env)

    def test_baseline_matches_the_environment_spec(self) -> None:
        spec = EnvironmentSpec()
        baseline = MYSQL_POOL_DELAY_V1.baseline_container_env
        self.assertEqual(baseline["DB_READ_DELAY"], spec.db_read_delay)
        self.assertEqual(baseline["DB_MAX_OPEN_CONNS"], str(spec.db_max_open_conns))
        self.assertEqual(baseline["DB_MAX_IDLE_CONNS"], str(spec.db_max_idle_conns))

    def test_verification_requires_at_least_one_check(self) -> None:
        self.assertFalse(IncidentVerification(expected_active=True).ok)

    def test_verification_fails_if_any_authority_disagrees(self) -> None:
        checks = (
            VerificationCheck("container-env", "docker-daemon", True, "250ms", "250ms"),
            VerificationCheck("mysql-peak", "mysql-server", False, "<=2", "9"),
        )
        verification = IncidentVerification(expected_active=True, checks=checks)
        self.assertFalse(verification.ok)
        self.assertEqual([c.name for c in verification.failures()], ["mysql-peak"])

    def test_verification_draws_on_independent_authorities(self) -> None:
        checks = (
            VerificationCheck("container-env", "docker-daemon", True, "x", "x"),
            VerificationCheck("mysql-peak", "mysql-server", True, "x", "x"),
            VerificationCheck("latency", "external-probe", True, "x", "x"),
        )
        verification = IncidentVerification(expected_active=True, checks=checks)
        self.assertTrue(verification.ok)
        sources = {c.source for c in checks}
        self.assertNotIn("application", sources, "app self-report must never be an authority")
        self.assertEqual(len(sources), 3)


class LoadStatisticsTests(unittest.TestCase):
    def test_nearest_rank_percentiles(self) -> None:
        values = [float(v) for v in range(1, 101)]
        self.assertEqual(percentile(values, 0.50), 50.0)
        self.assertEqual(percentile(values, 0.95), 95.0)
        self.assertEqual(percentile(values, 0.99), 99.0)
        self.assertEqual(percentile(values, 1.0), 100.0)

    def test_percentile_is_order_independent(self) -> None:
        self.assertEqual(percentile([3.0, 1.0, 2.0], 0.5), percentile([1.0, 2.0, 3.0], 0.5))

    def test_empty_input_is_nan_not_zero(self) -> None:
        self.assertTrue(math.isnan(percentile([], 0.5)))

    def test_rejects_out_of_range_fraction(self) -> None:
        for bad in (0.0, -0.1, 1.5):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    percentile([1.0], bad)

    def test_profile_is_reproducible_and_serialisable(self) -> None:
        profile = LoadProfile(name="incident")
        self.assertEqual(profile.to_dict()["seed"], profile.seed)
        self.assertLess(profile.warmup_seconds, profile.duration_seconds)


class VarianceTests(unittest.TestCase):
    def test_identical_values_have_zero_variation(self) -> None:
        stats = summarise([8.0] * 10)
        self.assertEqual(stats["n"], 10)
        self.assertEqual(stats["cvPercent"], 0.0)
        self.assertEqual(stats["halfRangePercent"], 0.0)

    def test_variation_is_reported_as_a_percentage_of_the_mean(self) -> None:
        stats = summarise([9.0, 11.0])
        self.assertAlmostEqual(stats["mean"], 10.0)
        self.assertAlmostEqual(stats["halfRangePercent"], 10.0)

    def test_nan_samples_are_discarded_not_counted_as_zero(self) -> None:
        stats = summarise([8.0, math.nan, 8.0])
        self.assertEqual(stats["n"], 2)
        self.assertEqual(stats["mean"], 8.0)

    def test_no_samples_reports_n_zero(self) -> None:
        self.assertEqual(summarise([])["n"], 0)

    def test_every_tolerance_is_documented(self) -> None:
        self.assertTrue(DECLARED_TOLERANCES)
        for tolerance in DECLARED_TOLERANCES:
            with self.subTest(metric=tolerance.metric):
                self.assertGreater(tolerance.max_cv_percent, 0.0)
                self.assertTrue(tolerance.rationale.strip(), "tolerance needs a written rationale")


class TelemetryTests(unittest.TestCase):
    def test_canonical_promql_matches_the_telemetry_contract(self) -> None:
        query = HTTP_LATENCY_QUANTILE.format(q="0.95", window="30s")
        self.assertIn("catalog_http_request_duration_seconds_bucket", query)
        self.assertIn('route=~"products.list|products.get"', query)
        self.assertIn('status=~"2.."', query)
        self.assertIn("[30s]", query)

    def test_window_reports_missing_metric_as_none_not_zero(self) -> None:
        window = TelemetryWindow(
            phase="incident", start_epoch=0.0, end_epoch=30.0, window_seconds=30
        )
        self.assertIsNone(window.value("valkeyP95Seconds"))

    def test_window_serialises_the_query_used(self) -> None:
        window = TelemetryWindow(
            phase="incident", start_epoch=0.0, end_epoch=30.0, window_seconds=30
        )
        payload = window.to_dict()
        self.assertEqual(payload["window"]["rangeSeconds"], 30)
        self.assertEqual(payload["phase"], "incident")


if __name__ == "__main__":
    unittest.main()


class DeterminismReportingTests(unittest.TestCase):
    """The report must not let a structurally pinned metric imply harness stability."""

    def test_pinned_metrics_are_flagged_and_excluded_from_drift_set(self) -> None:
        pinned = {t.metric for t in DECLARED_TOLERANCES if t.structurally_pinned}
        self.assertIn("incident.throughputRps", pinned)
        # A metric held constant by the injected arithmetic cannot also be the
        # evidence that the environment is reproducible.
        self.assertFalse(pinned & set(DRIFT_SENSITIVE_METRICS))

    def test_drift_sensitive_metrics_are_all_gated(self) -> None:
        gated = {t.metric for t in DECLARED_TOLERANCES}
        for metric in DRIFT_SENSITIVE_METRICS:
            self.assertIn(metric, gated, f"{metric} can detect drift but is not gated")

    def test_structural_expectations_are_derived_not_fitted(self) -> None:
        expectation = STRUCTURAL_EXPECTATIONS["incident.throughputRps"]
        # pool size 2 / read delay 0.25s
        self.assertEqual(expectation["expected"], 2 / 0.25)
        self.assertTrue(expectation["derivation"])

    def test_error_budget_is_small_but_not_zero(self) -> None:
        # Zero would fail a whole trial on one transient reset; the observed
        # regression class was 1.7%, so the bound must sit between.
        self.assertGreater(MAX_ERROR_RATE, 0.0)
        self.assertLess(MAX_ERROR_RATE, 0.017)

    def test_incident_window_absorbs_the_observed_stall_class(self) -> None:
        """The declared bound must survive the stall that was actually measured.

        A ten-cycle run showed one cycle losing 14 requests to a single 1.85s
        stall. Modelling that loss against the configured window reproduces the
        2.536% CV that was measured at the old 22s window, so the model is
        trusted to size the new one. Two such stalls in ten cycles must still
        fit inside the declared tolerance, otherwise the bound is decorative.
        """
        observed_stall_loss = 14
        bound = next(
            t.max_cv_percent
            for t in DECLARED_TOLERANCES
            if t.metric == "incident.throughputRps"
        )
        window = INCIDENT_PROFILE.duration_seconds - INCIDENT_PROFILE.warmup_seconds
        samples = window * 8.0

        for stalls in (1, 2):
            values = [samples] * (10 - stalls) + [samples - observed_stall_loss] * stalls
            mean = statistics.fmean(values)
            cv = statistics.stdev(values) / mean * 100.0
            with self.subTest(stalls=stalls):
                self.assertLess(cv, bound)

    def test_stall_model_reproduces_the_measured_variance(self) -> None:
        # Sanity check on the model itself: at the original 22s window it must
        # land on the 2.536% CV that was actually observed.
        values = [176.0] * 9 + [176.0 - 14]
        cv = statistics.stdev(values) / statistics.fmean(values) * 100.0
        self.assertAlmostEqual(cv, 2.536, places=2)

    def test_constant_series_reports_zero_cv_not_infinity(self) -> None:
        stats = summarise([0.0, 0.0, 0.0])
        self.assertEqual(stats["cvPercent"], 0.0)
        self.assertFalse(math.isinf(stats["cvPercent"]))


class StallDetectionTests(unittest.TestCase):
    """Stalls are gated directly, so the detector itself needs a real test.

    Each negative assertion below is paired with a positive control that
    proves the detector would have fired had a stall been present. A silent
    detector and a clean environment produce identical output otherwise.
    """

    def test_uniform_sample_has_no_stalls(self) -> None:
        threshold, stalls = detect_stalls([0.5] * 200, 3.0)
        self.assertEqual(stalls, [])
        self.assertAlmostEqual(threshold, 1.5)

        # Positive control: the same detector, same threshold, one bad sample.
        _, controls = detect_stalls([0.5] * 199 + [1.6], 3.0)
        self.assertEqual(controls, [1.6])

    def test_healthy_phase_jitter_is_not_a_stall(self) -> None:
        # Measured healthy phase: median ~29ms, max ~42ms (1.4x the median).
        observed = [0.029] * 400 + [0.042, 0.038, 0.035]
        threshold, stalls = detect_stalls(observed, 3.0)
        self.assertEqual(stalls, [])
        self.assertGreater(threshold, 0.042)

        _, controls = detect_stalls(observed + [0.1], 3.0)
        self.assertEqual(controls, [0.1])

    def test_detects_the_observed_incident_stall(self) -> None:
        # The 1.85s outlier from the first ten-cycle suite, against that
        # phase's measured 0.506s median.
        observed = [0.506] * 175 + [1.85]
        threshold, stalls = detect_stalls(observed, 3.0)
        self.assertEqual(stalls, [1.85])
        self.assertLess(threshold, 1.85)

    def test_stall_threshold_scales_with_phase_median(self) -> None:
        fast, _ = detect_stalls([0.029] * 10, 3.0)
        slow, _ = detect_stalls([0.506] * 10, 3.0)
        self.assertLess(fast, slow)
        # A single absolute threshold could not serve both phases: the
        # incident phase's *normal* latency exceeds the healthy threshold.
        self.assertGreater(0.506, fast)

    def test_stalls_are_ordered_worst_first(self) -> None:
        _, stalls = detect_stalls([0.5] * 50 + [2.0, 5.0, 3.0], 3.0)
        self.assertEqual(stalls, [5.0, 3.0, 2.0])

    def test_empty_sample_is_not_silently_clean(self) -> None:
        threshold, stalls = detect_stalls([], 3.0)
        self.assertTrue(math.isnan(threshold))
        self.assertEqual(stalls, [])

    def test_stall_rate_bound_is_above_observed_baseline(self) -> None:
        # 1 stall in 10 cycles of ~176 measured requests.
        observed_baseline = 1 / (10 * 176)
        self.assertGreater(MAX_STALL_RATE, observed_baseline)
        # ...but still tight enough to fail on a materially worse rate.
        self.assertLess(MAX_STALL_RATE, 0.01)

    def test_uniform_stall_rate_is_invisible_to_throughput_variance(self) -> None:
        """The blind spot the stall gate exists to close.

        Throughput variance only reacts to stalls that fall unevenly across
        cycles. A stall rate that is uniformly elevated degrades every cycle
        equally, so the coefficient of variation reads 0.000% -- perfectly
        stable, and perfectly wrong. This is the same insensitivity that made
        the structurally pinned metrics look reassuring.
        """
        # Each stall costs ~14 requests of a 640-request window (pool=2 is
        # halved for the stall's duration).
        degraded = [640 - 8 * 14] * 10
        throughput = summarise(degraded)
        self.assertEqual(throughput["cvPercent"], 0.0)

        tolerance = next(
            t for t in DECLARED_TOLERANCES if t.metric == "incident.throughputRps"
        )
        self.assertLess(throughput["cvPercent"], tolerance.max_cv_percent)

        # The direct bound catches what the variance bound cannot.
        self.assertGreater(8 / 640, MAX_STALL_RATE)

    def test_single_known_stall_stays_within_both_bounds(self) -> None:
        # The observed baseline -- 1 stall in 1 of 10 cycles -- must not fail
        # the suite, or the gate is merely a tripwire for normal behaviour.
        throughput = summarise([640] * 9 + [640 - 14])
        tolerance = next(
            t for t in DECLARED_TOLERANCES if t.metric == "incident.throughputRps"
        )
        self.assertLess(throughput["cvPercent"], tolerance.max_cv_percent)
        self.assertLess(1 / 640, MAX_STALL_RATE)

    def test_stall_metrics_are_drift_sensitive_not_structurally_pinned(self) -> None:
        pinned = set(STRUCTURAL_EXPECTATIONS)
        self.assertNotIn("incident.stallRate", pinned)
        self.assertNotIn("healthy.stallRate", pinned)


class UnexplainedObservationTests(unittest.TestCase):
    """The suite must keep reporting what we could not explain."""

    def test_unexplained_stall_is_recorded_not_smoothed(self) -> None:
        record = UNEXPLAINED_STALL
        self.assertEqual(record["status"], "unexplained")
        # Magnitude and frequency both present: either alone is unactionable.
        self.assertGreater(record["observedMagnitudeSeconds"], record["phaseNormalMaxSeconds"])
        self.assertIn("10 cycles", record["frequency"])

    def test_unexplained_stall_names_the_experimental_risk(self) -> None:
        # The reason this is not merely a determinism footnote: an agent under
        # test could diagnose a fault we never injected.
        self.assertIn("did not inject", UNEXPLAINED_STALL["openRisk"])


def _cycle(index: int, *, stalls: float = 0.0, excursions: float = 0.0, ok: bool = True) -> CycleResult:
    """A CycleResult with plausible warm steady-state metrics."""
    return CycleResult(
        index=index,
        run_id=f"suite-c{index:02d}",
        ok=ok,
        signed_off=ok,
        cleanup_verified=True,
        incident_verified=ok,
        duration_seconds=175.0,
        metrics={
            "healthy.throughputRps": 273.6,
            "healthy.latencyP50Seconds": 0.029,
            "healthy.latencyP95Seconds": 0.033,
            "healthy.latencyMaxSeconds": 0.044,
            "healthy.errorRate": 0.0,
            "healthy.stallRate": 0.0,
            "healthy.stallCount": 0.0,
            "healthy.excursionRate": 0.0,
            "healthy.excursionCount": 0.0,
            "incident.throughputRps": 7.90,
            "incident.latencyP50Seconds": 0.506,
            "incident.latencyP95Seconds": 0.513,
            "incident.latencyMaxSeconds": 0.513 if not stalls else 1.85,
            "incident.errorRate": 0.0,
            "incident.stallRate": stalls / 640.0,
            "incident.stallCount": stalls,
            "incident.excursionRate": excursions / 640.0,
            "incident.excursionCount": excursions,
        },
    )


def laptop_facts(**overrides) -> HostFacts:
    """The host class the catalog-app tolerances were actually fitted on.

    Built from the values `observe_host` read off that machine, so a test that
    resolves tolerances is resolving the same class a real run would. The
    asserted class id is checked against `derive_class_id` in
    `HostClassTests`, so this fixture cannot drift from the derivation it
    stands in for.
    """
    base = dict(
        os_name="Darwin",
        os_release="27.2.0",
        arch="arm64",
        cpu_model="Apple M5",
        cpu_cores=10,
        memory_bytes=34359738368,
        docker_engine_version="29.8.0",
        docker_operating_system="Docker Desktop",
        docker_kernel="7.0.12-linuxkit",
        docker_arch="aarch64",
        docker_cpus=10,
        docker_memory_bytes=8319504384,
        docker_virtualized=True,
        docker_virtualization_evidence="container runtime reports 'linuxkit'",
        python_version="3.12.14",
    )
    base.update(overrides)
    return HostFacts(**base)


class ReportGenerationTests(unittest.TestCase):
    def test_report_builds_with_a_stall_present(self) -> None:
        cycles = [_cycle(i) for i in range(1, 10)] + [_cycle(10, stalls=1.0)]
        report = build_report(cycles, suite_id="suite", cycles=10, host_facts=laptop_facts())

        observations = report["stallBudget"]["observations"]
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["runId"], "suite-c10")
        self.assertEqual(observations[0]["count"], 1)
        self.assertAlmostEqual(observations[0]["maxLatencySeconds"], 1.85)

    def test_report_is_json_serialisable_with_a_stall(self) -> None:
        # The report is written to disk; a value that cannot be encoded fails
        # just as completely as an AttributeError.
        cycles = [_cycle(i) for i in range(1, 10)] + [_cycle(10, stalls=3.0)]
        report = build_report(cycles, suite_id="suite", cycles=10, host_facts=laptop_facts())
        encoded = json.dumps(report, sort_keys=True, default=str)
        self.assertIn("stallBudget", encoded)

    def test_clean_run_reports_no_observations(self) -> None:
        report = build_report(
            [_cycle(i) for i in range(1, 11)],
            suite_id="suite",
            cycles=10,
            host_facts=laptop_facts(),
        )
        self.assertEqual(report["stallBudget"]["observations"], [])
        self.assertTrue(report["stallBudget"]["withinBudget"])

    def test_stall_budget_fails_when_breached(self) -> None:
        # 8 stalls in a 640-request window is 1.25%, above the 0.5% bound.
        cycles = [_cycle(i) for i in range(1, 10)] + [_cycle(10, stalls=8.0)]
        report = build_report(cycles, suite_id="suite", cycles=10, host_facts=laptop_facts())
        self.assertFalse(report["stallBudget"]["withinBudget"])
        self.assertFalse(report["exitCriterionMet"])

    def test_absolute_excursions_are_reported_alongside_relative_stalls(self) -> None:
        cycles = [_cycle(i) for i in range(1, 10)] + [_cycle(10, stalls=1.0, excursions=1.0)]
        report = build_report(cycles, suite_id="suite", cycles=10, host_facts=laptop_facts())
        totals = report["stallBudget"]["absoluteExcursions"]["totals"]
        self.assertEqual(totals["incident.excursionCount"], 1)
        thresholds = report["stallBudget"]["absoluteExcursions"]["thresholdsSeconds"]
        self.assertEqual(thresholds["incident"], 1.0)
        self.assertEqual(thresholds["healthy"], 0.1)

    def test_report_records_the_unmeasured_phases(self) -> None:
        report = build_report(
            [_cycle(i) for i in range(1, 11)],
            suite_id="suite",
            cycles=10,
            setup_cycles=[{"runId": "suite-setup-c00", "ok": True, "role": "image-acquisition"}],
            warmups=[{"runId": "suite-warmup-c01", "ok": True, "role": "discarded-warmup"}],
            host_facts=laptop_facts(),
        )
        self.assertEqual(len(report["unmeasured"]["setupCycles"]), 1)
        self.assertEqual(len(report["unmeasured"]["warmupCycles"]), 1)

    def test_gate_definition_is_pre_registered_in_the_report(self) -> None:
        report = build_report(
            [_cycle(i) for i in range(1, 11)],
            suite_id="suite",
            cycles=10,
            host_facts=laptop_facts(),
        )
        pre = report["stallBudget"]["preRegistration"]
        self.assertEqual(pre["stallFactor"], 3.0)
        self.assertEqual(pre["maxStallRate"], MAX_STALL_RATE)

    def test_failed_cycle_does_not_meet_exit_criterion(self) -> None:
        cycles = [_cycle(i) for i in range(1, 10)] + [_cycle(10, ok=False)]
        report = build_report(cycles, suite_id="suite", cycles=10, host_facts=laptop_facts())
        self.assertFalse(report["exitCriterionMet"])


class FixtureHashScopeTests(unittest.TestCase):
    """The hash has to cover everything that can change what a trial measures."""

    def setUp(self) -> None:
        self.root = Path(__file__).resolve().parents[2]

    def test_covers_application_source(self) -> None:
        covered = fixture_files(self.root)
        self.assertTrue(any(f.startswith("cmd/") for f in covered))
        self.assertTrue(any(f.startswith("internal/") for f in covered))

    def test_covers_the_driver_and_its_compose_templates(self) -> None:
        covered = fixture_files(self.root)
        # The driver defines the load profiles, the incident and the topology,
        # so a change to it is a change to the experiment.
        self.assertIn("benchmark/radius_perf_eval/trials.py", covered)
        self.assertIn("benchmark/radius_perf_eval/load.py", covered)
        self.assertIn("benchmark/radius_perf_eval/compose/base.yml", covered)
        self.assertIn(
            "benchmark/radius_perf_eval/compose/incident-mysql-pool-delay.yml", covered
        )

    def test_covers_the_build_and_seed_inputs(self) -> None:
        covered = fixture_files(self.root)
        for expected in FIXTURE_PATHS:
            self.assertIn(expected, covered)

    def test_excludes_caches_and_build_output(self) -> None:
        covered = fixture_files(self.root)
        self.assertFalse([f for f in covered if "__pycache__" in f])

    def test_is_materially_wider_than_the_original_six_files(self) -> None:
        # Guards the regression directly: six files was the bug.
        self.assertGreater(len(fixture_files(self.root)), len(FIXTURE_PATHS) * 3)

    def test_hash_changes_when_a_covered_file_changes(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for rel in FIXTURE_PATHS:
                target = root / rel
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text("x")
            for tree in ("cmd", "internal", "benchmark/radius_perf_eval"):
                (root / tree).mkdir(parents=True, exist_ok=True)
            source = root / "internal" / "thing.go"
            source.write_text("package thing")

            before, _ = hash_fixture(root)
            source.write_text("package thing // changed")
            after, _ = hash_fixture(root)
            self.assertNotEqual(before, after)


class StallTimestampTests(unittest.TestCase):
    """Stalls were recorded as bare latencies, which can say that one happened
    and how big it was but nothing about when. Two stalls at a similar elapsed
    time would implicate a periodic host or database event; two at unrelated
    times would not. Recording the timestamps makes that claim testable. It
    changes no gate."""

    EPOCH_OFFSET = 1_700_000_000.0

    def _samples(self) -> list[Sample]:
        # started_at is monotonic; the phase window opens at 100.0.
        return [
            Sample(started_at=100.0, latency_seconds=0.50, status=200, path="/a"),
            Sample(started_at=112.5, latency_seconds=1.85, status=200, path="/b"),
            Sample(started_at=130.0, latency_seconds=0.48, status=200, path="/c"),
        ]

    def _events(self, suite_start_epoch=None):
        return stall_events(
            self._samples(),
            1.5,
            epoch_offset=self.EPOCH_OFFSET,
            phase_start=100.0,
            suite_start_epoch=suite_start_epoch,
        )

    def test_only_stalls_are_timestamped(self) -> None:
        events = self._events()
        self.assertEqual([e.latency_seconds for e in events], [1.85])

    def test_records_wall_clock_and_both_elapsed_clocks(self) -> None:
        suite_start = self.EPOCH_OFFSET + 40.0
        (event,) = self._events(suite_start_epoch=suite_start)

        self.assertEqual(event.epoch, self.EPOCH_OFFSET + 112.5)
        self.assertEqual(event.wall_clock, "2023-11-14T22:15:12.500000+00:00")
        # 12.5s into the measured phase, 72.5s into the suite.
        self.assertAlmostEqual(event.phase_elapsed_seconds, 12.5)
        self.assertAlmostEqual(event.suite_elapsed_seconds, 72.5)

    def test_suite_elapsed_is_null_for_a_standalone_trial(self) -> None:
        (event,) = self._events()
        self.assertIsNone(event.suite_elapsed_seconds)
        self.assertIsNone(event.to_dict()["suiteElapsedSeconds"])

    def test_events_are_ordered_by_occurrence_not_magnitude(self) -> None:
        """Stall *latencies* are sorted worst-first for reading. Stall *events*
        must be chronological, or comparing elapsed times across cycles reads
        the wrong row."""
        samples = [
            Sample(started_at=150.0, latency_seconds=2.0, status=200, path="/a"),
            Sample(started_at=110.0, latency_seconds=3.0, status=200, path="/b"),
        ]
        events = stall_events(
            samples,
            1.5,
            epoch_offset=0.0,
            phase_start=100.0,
            suite_start_epoch=None,
        )
        self.assertEqual([e.phase_elapsed_seconds for e in events], [10.0, 50.0])

    def test_no_events_when_the_threshold_is_undefined(self) -> None:
        self.assertEqual(
            stall_events(
                [],
                float("nan"),
                epoch_offset=0.0,
                phase_start=0.0,
                suite_start_epoch=None,
            ),
            [],
        )

    def test_threshold_agrees_with_the_stall_counter(self) -> None:
        """The count and the timestamps are produced by two functions. If they
        disagree, the report says one stall happened and lists none."""
        samples = self._samples()
        latencies = [s.latency_seconds for s in samples]
        threshold, stalls = detect_stalls(latencies, 3.0)
        events = stall_events(
            samples,
            threshold,
            epoch_offset=0.0,
            phase_start=100.0,
            suite_start_epoch=None,
        )
        self.assertEqual(len(events), len(stalls))


class HostSuspensionTests(unittest.TestCase):
    """A cycle is only a measurement if the host was awake for all of it.

    An earlier holdout attempt entered clamshell sleep 90 seconds into cycle 3
    and alternated sleep and darkwake for 109 minutes. That cycle passed all
    17 gates and reported a throughput computed over a wall-clock window the
    machine had mostly slept through. Nothing noticed.
    """

    def test_no_suspension_when_both_clocks_advance_together(self) -> None:
        self.assertEqual(host_suspension_seconds(1000.0, 1180.0, 50.0, 230.0), 0.0)

    def test_measures_the_gap_between_wall_and_monotonic(self) -> None:
        # 94 minutes of wall clock, 3 minutes of process time.
        self.assertAlmostEqual(
            host_suspension_seconds(1000.0, 1000.0 + 5640.0, 50.0, 50.0 + 180.0),
            5460.0,
        )

    def test_backwards_clock_skew_is_not_reported_as_suspension(self) -> None:
        self.assertEqual(host_suspension_seconds(1000.0, 1100.0, 50.0, 200.0), 0.0)

    def test_scheduling_jitter_stays_under_the_bound(self) -> None:
        jitter = host_suspension_seconds(1000.0, 1180.4, 50.0, 230.0)
        self.assertLess(jitter, MAX_HOST_SUSPENSION_SECONDS)

    def test_suite_fails_when_a_cycle_was_suspended(self) -> None:
        """Positive control for the gate, not just the arithmetic: a suite
        that is otherwise perfect must still fail on a suspended cycle."""
        clean = [_cycle(i) for i in (1, 2)]
        report = build_report(clean, suite_id="s", cycles=2, host_facts=laptop_facts())
        self.assertTrue(report["hostSuspension"]["hostAwakeThroughout"])

        clean[1].host_suspension_seconds = 5460.0
        suspended = build_report(clean, suite_id="s", cycles=2, host_facts=laptop_facts())
        self.assertFalse(suspended["hostSuspension"]["hostAwakeThroughout"])
        self.assertFalse(suspended["exitCriterionMet"])
        self.assertEqual(
            [entry["runId"] for entry in suspended["hostSuspension"]["suspendedCycles"]],
            [clean[1].run_id],
        )


class RunLoadStallWiringTests(unittest.TestCase):
    """Drive run_load against a real server with one deliberately slow reply.

    detect_stalls and stall_events are unit-tested above, but that proves the
    detector, not the caller. run_load is where the sample list, the phase
    start and the epoch offset are handed over, and a wrong argument there
    would produce a plausible-looking count with nonsense timestamps that no
    detector test would catch. This is the positive control for that seam: a
    stall is injected, so the assertions cannot pass on an empty result.
    """

    STALL_SECONDS = 0.9

    @classmethod
    def setUpClass(cls) -> None:
        import http.server
        import threading as _threading

        state = {"served": 0, "stall_on": 6}

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:  # noqa: N802
                state["served"] += 1
                if state["served"] == state["stall_on"]:
                    time.sleep(RunLoadStallWiringTests.STALL_SECONDS)
                body = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, *args: object) -> None:
                pass

        cls._server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls._thread = _threading.Thread(target=cls._server.serve_forever, daemon=True)
        cls._thread.start()
        cls._port = cls._server.server_address[1]
        cls._state = state

    def setUp(self) -> None:
        # Reset per test: the counter is shared by the server thread, and a
        # previous test consuming the trigger would leave this one asserting
        # against a stall that never happened.
        self._state["served"] = 0

    @classmethod
    def tearDownClass(cls) -> None:
        cls._server.shutdown()
        cls._server.server_close()

    def test_injected_stall_is_counted_and_timestamped(self) -> None:
        suite_start = time.time() - 30.0
        before = time.time()
        result = run_load(
            f"http://127.0.0.1:{self._port}",
            LoadProfile(
                name="wiring",
                concurrency=1,
                duration_seconds=2.5,
                warmup_seconds=0.0,
                stall_factor=3.0,
                absolute_excursion_seconds=0.5,
            ),
            suite_start_epoch=suite_start,
        )
        after = time.time()

        # Positive control: the injected stall must actually have been seen,
        # otherwise every assertion below is vacuous.
        self.assertGreaterEqual(result.stall_count, 1)
        self.assertEqual(len(result.stall_events), result.stall_count)

        event = max(result.stall_events, key=lambda e: e.latency_seconds)
        self.assertGreaterEqual(event.latency_seconds, self.STALL_SECONDS)

        # The epoch must land inside the wall-clock bracket of this call. A
        # monotonic value leaked in place of an epoch fails here.
        self.assertGreaterEqual(event.epoch, before)
        self.assertLessEqual(event.epoch, after)
        self.assertTrue(event.wall_clock.endswith("+00:00"))

        # Elapsed clocks must be consistent with each other and with the call.
        self.assertGreaterEqual(event.phase_elapsed_seconds, 0.0)
        self.assertLessEqual(event.phase_elapsed_seconds, 3.0)
        self.assertAlmostEqual(
            event.suite_elapsed_seconds, event.epoch - suite_start, places=6
        )
        self.assertGreater(event.suite_elapsed_seconds, 30.0)

    def test_absolute_excursion_counts_the_same_injected_stall(self) -> None:
        """The absolute bound is frozen at 0.5s here and the stall is 0.9s, so
        a relative threshold that drifted upward could not hide it."""
        result = run_load(
            f"http://127.0.0.1:{self._port}",
            LoadProfile(
                name="wiring-absolute",
                concurrency=1,
                duration_seconds=2.5,
                warmup_seconds=0.0,
                absolute_excursion_seconds=0.5,
            ),
        )
        self.assertGreaterEqual(result.excursion_count, 1)
        self.assertGreaterEqual(result.stall_count, 1)
        self.assertIsNone(result.stall_events[0].suite_elapsed_seconds)


class PowerStateTests(unittest.TestCase):
    """The drift observation that prompted this was unfalsifiable after the
    fact, because nothing recorded whether the host was on battery."""

    AC = (
        "Now drawing from 'AC Power'\n"
        " -InternalBattery-0 (id=36438115)\t100%; finishing charge; "
        "0:00 remaining present: true\n"
    )
    BATTERY = (
        "Now drawing from 'Battery Power'\n"
        " -InternalBattery-0 (id=36438115)\t92%; discharging; "
        "3:41 remaining present: true\n"
    )

    def test_reads_ac(self) -> None:
        state = parse_power_state(self.AC, None)
        self.assertEqual(state["source"], "ac")
        self.assertEqual(state["batteryPercent"], 100)

    def test_reads_battery(self) -> None:
        state = parse_power_state(self.BATTERY, None)
        self.assertEqual(state["source"], "battery")
        self.assertEqual(state["batteryPercent"], 92)

    def test_ac_and_battery_are_distinguished(self) -> None:
        """The whole point is telling these apart; a parser that matched
        'Power' in both would satisfy every other assertion here."""
        self.assertNotEqual(
            parse_power_state(self.AC, None)["source"],
            parse_power_state(self.BATTERY, None)["source"],
        )

    def test_throttling_is_visible_when_the_os_reports_it(self) -> None:
        state = parse_power_state(None, "CPU_Speed_Limit \t= 70\n")
        self.assertEqual(state["cpuSpeedLimitPercent"], 70)
        self.assertTrue(state["thermalWarningsRecorded"])

    def test_absent_thermal_limit_is_none_not_a_hundred(self) -> None:
        """An unthrottled host must not be recorded as a measured 100%: that
        would make 'no data' and 'verified fine' indistinguishable."""
        state = parse_power_state(None, "Note: No thermal warning level has been recorded\n")
        self.assertIsNone(state["cpuSpeedLimitPercent"])
        self.assertFalse(state["thermalWarningsRecorded"])

    def test_missing_probe_output_does_not_raise(self) -> None:
        self.assertEqual(parse_power_state(None, None), {})

    def test_live_capture_reports_this_host(self) -> None:
        state = host_power_state()
        self.assertIn("platform", state)
        if state["platform"] == "Darwin":
            # If pmset is present this must resolve; 'unknown' on a Mac means
            # the parser silently stopped working.
            self.assertIn(state["source"], {"ac", "battery"})

    def test_power_change_during_a_suite_is_flagged(self) -> None:
        report = annotate_power({}, {"source": "ac"}, {"source": "battery"})
        self.assertTrue(report["hostPower"]["changedDuringSuite"])

    def test_stable_power_is_not_flagged(self) -> None:
        report = annotate_power({}, {"source": "ac"}, {"source": "ac"})
        self.assertFalse(report["hostPower"]["changedDuringSuite"])
        self.assertEqual(report["hostPower"]["atStart"]["source"], "ac")

    def test_power_is_not_a_gate(self) -> None:
        """Explicitly pinned: recording power must never fail a suite, or a
        laptop unplugged mid-run would invalidate a valid measurement."""
        report = annotate_power(
            {"exitCriterionMet": True}, {"source": "ac"}, {"source": "battery"}
        )
        self.assertTrue(report["exitCriterionMet"])


class SuiteProvenanceTests(unittest.TestCase):
    """A holdout that cannot name the commit it ran against is not a holdout."""

    def _repo(self, tmp: str) -> Path:
        root = Path(tmp)
        env = {"GIT_CONFIG_GLOBAL": str(root / "gitconfig"), "HOME": tmp, "PATH": "/usr/bin:/bin"}
        subprocess.run(["git", "init", "-q", str(root)], check=True, env=env)
        (root / "a.txt").write_text("one\n", encoding="utf-8")
        for args in (
            ["config", "user.email", "t@example.com"],
            ["config", "user.name", "t"],
            ["add", "-A"],
            ["commit", "-qm", "init"],
        ):
            subprocess.run(["git", "-C", str(root), *args], check=True, env=env)
        return root

    def test_clean_tree_records_a_commit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            prov = suite_provenance(root, "suite-x", root / "out")
            self.assertTrue(prov["worktreeClean"])
            self.assertEqual(prov["worktreeDiff"], "")
            self.assertEqual(len(prov["commit"]), 40)
            self.assertEqual(prov["suiteId"], "suite-x")

    def test_dirty_tree_is_reported_with_the_diff(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = self._repo(tmp)
            (root / "a.txt").write_text("two\n", encoding="utf-8")
            prov = suite_provenance(root, "suite-x", root / "out")
            self.assertFalse(prov["worktreeClean"])
            self.assertIn("a.txt", prov["worktreeDiff"])

    def test_a_non_repository_is_not_reported_clean(self) -> None:
        """Absent git output must not read as 'no changes'. Defaulting to
        clean would let an unknown tree pass as a verified one."""
        with tempfile.TemporaryDirectory() as tmp:
            prov = suite_provenance(Path(tmp), "suite-x", Path(tmp))
            self.assertFalse(prov["worktreeClean"])
            self.assertIsNone(prov["commit"])


class ReportPersistenceTests(unittest.TestCase):
    """The README documented a report path the code did not write. The suite's
    own report survived only because an external harness wrote it."""

    def test_write_json_creates_parents_and_round_trips(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "suite-1" / "determinism-report.json"
            _write_json(target, {"exitCriterionMet": True, "b": 1, "a": 2})
            self.assertTrue(target.exists())
            self.assertEqual(
                json.loads(target.read_text(encoding="utf-8"))["exitCriterionMet"], True
            )

    def test_written_report_is_stable_across_writes(self) -> None:
        """Sorted keys, so two reports of the same suite diff on content
        rather than on dict ordering."""
        with tempfile.TemporaryDirectory() as tmp:
            first, second = Path(tmp) / "one.json", Path(tmp) / "two.json"
            _write_json(first, {"b": 1, "a": 2})
            _write_json(second, {"a": 2, "b": 1})
            self.assertEqual(first.read_text(), second.read_text())

    def test_non_serialisable_values_do_not_lose_the_report(self) -> None:
        """A report is written after ten cycles of real work; a stray Path in
        it must not raise and discard the run."""
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "r.json"
            _write_json(target, {"dir": Path("/tmp/x")})
            self.assertIn("/tmp/x", target.read_text(encoding="utf-8"))

    def test_run_suite_writes_the_path_the_readme_documents(self) -> None:
        """Pins the contract rather than the prose: the README tells operators
        to look in <suite-id>/determinism-report.json."""
        readme = (Path(__file__).resolve().parents[1] / "README.md").read_text(
            encoding="utf-8"
        )
        self.assertIn("<suite-id>/determinism-report.json", readme)
        source = (
            Path(__file__).resolve().parents[1]
            / "radius_perf_eval"
            / "trials.py"
        ).read_text(encoding="utf-8")
        self.assertIn('suite_dir / "determinism-report.json"', source)
        self.assertIn('suite_dir / "provenance.json"', source)


class DaemonVersionCaptureTests(unittest.TestCase):
    """build_report runs after every cycle is finished. Raising there would
    discard a completed suite over a version string."""

    def test_an_unreachable_daemon_is_recorded_not_raised(self) -> None:
        import radius_perf_eval.trials as trials_module

        original = trials_module.daemon_info
        trials_module.daemon_info = lambda: (_ for _ in ()).throw(
            RuntimeError("Docker daemon is not reachable.\nsecond line")
        )
        try:
            versions = trials_module._daemon_versions()
        finally:
            trials_module.daemon_info = original
        self.assertIn("error", versions)
        self.assertNotIn("second line", versions["error"])

    def test_report_is_still_produced_without_a_daemon(self) -> None:
        import radius_perf_eval.trials as trials_module

        original = trials_module.daemon_info
        trials_module.daemon_info = lambda: (_ for _ in ()).throw(RuntimeError("down"))
        try:
            report = build_report(
                [_cycle(i) for i in range(1, 11)],
                suite_id="s",
                cycles=10,
                host_facts=laptop_facts(),
            )
        finally:
            trials_module.daemon_info = original
        self.assertTrue(report["exitCriterionMet"])
        self.assertEqual(report["dockerVersions"], {"error": "down"})


class ComposeModelParsingTests(unittest.TestCase):
    """Turning `docker compose config --format json` into a model."""

    SAMPLE = json.dumps(
        {
            "name": "proj",
            "networks": {
                "data": {"name": "proj_data", "internal": True},
                "edge": {"name": "proj_edge"},
            },
            "volumes": {"mysql-data": {"name": "proj_mysql-data"}},
            "services": {
                "mysql": {
                    "image": "mysql@sha256:" + "a" * 64,
                    "networks": {"data": None},
                    "environment": {"MYSQL_ROOT_PASSWORD": "rootpw"},
                    "deploy": {"resources": {"limits": {"cpus": 1, "memory": "1073741824"}}},
                },
                "api": {
                    "image": "sha256:" + "c" * 64,
                    "networks": {"data": None, "edge": None},
                    "ports": [{"mode": "ingress", "host_ip": "127.0.0.1", "target": 8080}],
                    "deploy": {"resources": {"limits": {"cpus": 0.5, "memory": "536870912"}}},
                },
            },
        }
    )

    def test_parses_services_networks_and_volumes(self) -> None:
        model = parse_compose_config(self.SAMPLE)
        self.assertEqual(model.service_names, ("api", "mysql"))
        self.assertEqual(model.volumes, ("mysql-data",))
        self.assertTrue(model.networks["data"].internal)
        self.assertFalse(model.networks["edge"].internal)

    def test_normalises_limits_to_nanocpus_and_bytes(self) -> None:
        model = parse_compose_config(self.SAMPLE)
        self.assertEqual(model.services["mysql"].nano_cpus, 1_000_000_000)
        self.assertEqual(model.services["mysql"].memory_bytes, 1024**3)
        self.assertEqual(model.services["api"].nano_cpus, 500_000_000)

    def test_published_ports_are_container_targets(self) -> None:
        model = parse_compose_config(self.SAMPLE)
        self.assertEqual(model.services["api"].published_ports, (8080,))
        self.assertEqual(model.services["mysql"].published_ports, ())

    def test_internal_only_service_is_not_egress_reachable(self) -> None:
        model = parse_compose_config(self.SAMPLE)
        self.assertFalse(model.egress_reachable("mysql"))
        self.assertTrue(model.egress_reachable("api"))

    def test_a_service_with_no_network_is_treated_as_reachable(self) -> None:
        """Compose puts an unattached service on the default bridge, which
        routes. Assuming the opposite would report egress blocked for the one
        service most likely to have it."""
        payload = json.loads(self.SAMPLE)
        payload["services"]["stray"] = {"image": "x@sha256:" + "e" * 64}
        model = parse_compose_config(json.dumps(payload))
        self.assertTrue(model.egress_reachable("stray"))

    def test_observed_network_names_use_the_project_prefixed_names(self) -> None:
        model = parse_compose_config(self.SAMPLE)
        self.assertEqual(model.observed_network_names("api"), ("proj_data", "proj_edge"))

    def test_missing_limits_are_none_not_zero(self) -> None:
        """Zero would compare equal to an unlimited container's reported limit,
        so an undeclared limit must be distinguishable from a declared one."""
        payload = json.loads(self.SAMPLE)
        payload["services"]["mysql"]["deploy"] = {"placement": {}}
        model = parse_compose_config(json.dumps(payload))
        self.assertIsNone(model.services["mysql"].nano_cpus)
        self.assertFalse(model.services["mysql"].limits_declared)

    def test_rejects_non_json_and_empty_stacks(self) -> None:
        with self.assertRaises(CheckPlanError):
            parse_compose_config("services:\n  mysql: {}")
        with self.assertRaises(CheckPlanError):
            parse_compose_config(json.dumps({"services": {}}))

    def test_memory_accepts_byte_strings_and_suffixes(self) -> None:
        self.assertEqual(parse_memory("536870912"), 536870912)
        self.assertEqual(parse_memory(536870912), 536870912)
        self.assertEqual(parse_memory("512m"), 512 * 1024**2)
        self.assertEqual(parse_memory("1g"), 1024**3)
        self.assertEqual(parse_memory("1gib"), 1024**3)
        with self.assertRaises(CheckPlanError):
            parse_memory("lots")
        with self.assertRaises(CheckPlanError):
            parse_memory(True)

    def test_nano_cpus_accepts_numbers_and_strings(self) -> None:
        self.assertEqual(parse_nano_cpus(1), 1_000_000_000)
        self.assertEqual(parse_nano_cpus("0.5"), 500_000_000)
        with self.assertRaises(CheckPlanError):
            parse_nano_cpus("half")


class CheckPlanGenerationTests(unittest.TestCase):
    def test_every_service_gets_every_required_kind(self) -> None:
        plan = catalog_plan()
        for service in CATALOG_SERVICES:
            for kind in REQUIRED_CHECK_KINDS:
                self.assertIn(f"{kind}:{service}", plan.gate_names)
        self.assertEqual(
            len(plan.checks), len(CATALOG_SERVICES) * len(REQUIRED_CHECK_KINDS)
        )

    def test_the_catalog_stack_generates_a_complete_plan(self) -> None:
        plan = catalog_plan()
        self.assertTrue(plan.complete, plan.problems)

    def test_adding_a_service_grows_the_check_set(self) -> None:
        """Positive control for the whole point of this module.

        The hand-written version of these checks could not fail this way: a
        fifth service simply acquired no gates and the manifest signed off on
        the four it knew about.
        """
        before = catalog_plan()
        model = catalog_model(
            extra={
                "worker": ServiceModel(
                    name="worker",
                    image="worker@sha256:" + "f" * 64,
                    networks=("data",),
                    nano_cpus=250_000_000,
                    memory_bytes=128 * 1024**2,
                )
            }
        )
        after = generate_check_plan(
            model,
            readiness_probes=set(model.service_names),
            egress_exceptions=EGRESS_EXCEPTIONS,
        )
        self.assertEqual(
            len(after.checks) - len(before.checks),
            len(REQUIRED_CHECK_KINDS),
            "a new service must add one check of every required kind",
        )
        for kind in REQUIRED_CHECK_KINDS:
            self.assertNotIn(f"{kind}:worker", before.gate_names)
            self.assertIn(f"{kind}:worker", after.gate_names)
        self.assertTrue(after.complete, after.problems)

    def test_a_new_service_without_a_readiness_probe_fails_the_plan(self) -> None:
        model = catalog_model(
            extra={
                "worker": ServiceModel(
                    name="worker",
                    image="worker@sha256:" + "f" * 64,
                    networks=("data",),
                    nano_cpus=250_000_000,
                    memory_bytes=128 * 1024**2,
                )
            }
        )
        plan = generate_check_plan(
            model,
            readiness_probes=set(CATALOG_SERVICES),
            egress_exceptions=EGRESS_EXCEPTIONS,
        )
        self.assertFalse(plan.complete)
        self.assertTrue(
            any("readiness probe" in p and "worker" in p for p in plan.problems), plan.problems
        )

    def test_a_new_reachable_service_must_declare_an_egress_exception(self) -> None:
        model = catalog_model(
            extra={
                "worker": ServiceModel(
                    name="worker",
                    image="worker@sha256:" + "f" * 64,
                    networks=("edge",),
                    nano_cpus=250_000_000,
                    memory_bytes=128 * 1024**2,
                )
            }
        )
        plan = generate_check_plan(
            model,
            readiness_probes=set(model.service_names),
            egress_exceptions=EGRESS_EXCEPTIONS,
        )
        self.assertFalse(plan.complete)
        self.assertTrue(
            any("egress exception" in p and "worker" in p for p in plan.problems), plan.problems
        )

    def test_a_service_without_limits_fails_the_plan(self) -> None:
        model = catalog_model(
            extra={
                "worker": ServiceModel(
                    name="worker", image="worker@sha256:" + "f" * 64, networks=("data",)
                )
            }
        )
        plan = generate_check_plan(
            model,
            readiness_probes=set(model.service_names),
            egress_exceptions=EGRESS_EXCEPTIONS,
        )
        self.assertFalse(plan.complete)
        self.assertTrue(
            any("no cpu and memory limits" in p for p in plan.problems), plan.problems
        )

    def test_an_unpinned_image_fails_the_plan(self) -> None:
        model = catalog_model(
            extra={
                "worker": ServiceModel(
                    name="worker",
                    image="worker:latest",
                    networks=("data",),
                    nano_cpus=250_000_000,
                    memory_bytes=128 * 1024**2,
                )
            }
        )
        plan = generate_check_plan(
            model,
            readiness_probes=set(model.service_names),
            egress_exceptions=EGRESS_EXCEPTIONS,
        )
        self.assertFalse(plan.complete)
        self.assertTrue(any("unpinned image" in p for p in plan.problems), plan.problems)

    def test_a_check_naming_an_absent_service_fails_reconciliation(self) -> None:
        """The mirror of a missing check. Left unchecked it would sit in the
        required set forever, never be recorded, and read as an unrelated bug."""
        model = catalog_model()
        checks = list(catalog_plan(model).checks)
        checks += [ServiceCheck(kind=kind, service="ghost") for kind in REQUIRED_CHECK_KINDS]
        problems = reconcile(
            checks,
            model,
            readiness_probes=set(model.service_names),
            egress_exceptions=EGRESS_EXCEPTIONS,
        )
        self.assertTrue(
            any("not in the compose file" in p and "ghost" in p for p in problems), problems
        )

    def test_egress_exceptions_are_filtered_to_services_in_the_file(self) -> None:
        plan = generate_check_plan(
            catalog_model(),
            readiness_probes=set(CATALOG_SERVICES),
            egress_exceptions={**EGRESS_EXCEPTIONS, "gone": "stale entry"},
        )
        self.assertNotIn("gone", plan.egress_exceptions)

    def test_the_fixture_matches_the_compose_file_service_list(self) -> None:
        """Anti-drift: this fixture stands in for `compose/base.yml` in every
        test above, so it has to keep naming the same services the file does.
        Parsed with a narrow reader rather than a YAML dependency, because the
        only thing needed is the set of top-level keys under `services:`."""
        text = (COMPOSE_DIR / "base.yml").read_text(encoding="utf-8")
        in_services = False
        found: list[str] = []
        for line in text.splitlines():
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            if not line.startswith(" "):
                in_services = line.rstrip().startswith("services:")
                continue
            if in_services and line.startswith("  ") and not line.startswith("   "):
                found.append(line.strip().rstrip(":"))
        self.assertEqual(sorted(found), sorted(CATALOG_SERVICES))


class SuppressedServiceControlTests(unittest.TestCase):
    """Positive control: a plan that drops a service must not sign off.

    This is the failure the derived requirement could otherwise create. The
    required gate set comes from the plan, so suppressing a service's checks
    also removes its gates from the requirement, and `missing_gates` sees
    nothing wrong. Only reconciliation against the Compose file notices, which
    makes `check-plan-covers-compose-services` the gate holding this closed.
    """

    def _manifest_from(self, plan: CheckPlan) -> EnvironmentManifest:
        manifest = EnvironmentManifest(run_id="r", compose_project="radius-eval-r")
        manifest.require_gates(plan.gate_names | CATALOG_APPLICATION_GATES)
        for name in sorted(manifest.required_gates):
            if name == "check-plan-covers-compose-services":
                manifest.add_gate(name, plan.complete, summarise_problems(plan.problems))
            else:
                manifest.add_gate(name, True)
        return manifest

    def test_the_complete_plan_signs_off(self) -> None:
        """Without this the suppression result below would prove nothing: a
        manifest that never signs off cannot show that suppression is what
        stopped it."""
        manifest = self._manifest_from(catalog_plan())
        self.assertTrue(manifest.signed_off, manifest.failed_gates)

    def test_suppressing_a_service_withholds_sign_off(self) -> None:
        model = catalog_model()
        suppressed = catalog_plan(model).without_service("valkey")
        suppressed = CheckPlan(
            checks=suppressed.checks,
            services=suppressed.services,
            egress_exceptions=suppressed.egress_exceptions,
            problems=reconcile(
                suppressed.checks,
                model,
                readiness_probes=set(model.service_names),
                egress_exceptions=EGRESS_EXCEPTIONS,
            ),
        )
        manifest = self._manifest_from(suppressed)

        self.assertEqual(
            manifest.missing_gates,
            [],
            "the derived requirement shrank with the plan, which is exactly why "
            "missing_gates cannot be what catches this",
        )
        self.assertFalse(manifest.signed_off)
        self.assertEqual(
            [g.name for g in manifest.failed_gates], ["check-plan-covers-compose-services"]
        )

    def test_suppression_names_every_missing_kind(self) -> None:
        model = catalog_model()
        problems = reconcile(
            catalog_plan(model).without_service("valkey").checks,
            model,
            readiness_probes=set(model.service_names),
            egress_exceptions=EGRESS_EXCEPTIONS,
        )
        for kind in REQUIRED_CHECK_KINDS:
            self.assertTrue(
                any(f"no {kind} check" in p and "valkey" in p for p in problems),
                f"{kind} not reported: {problems}",
            )


class EnvironmentRedactionTests(unittest.TestCase):
    def test_secret_shaped_keys_are_redacted(self) -> None:
        redacted = redact_env(
            {
                "MYSQL_ROOT_PASSWORD": "s0mesecret",
                "MYSQL_DSN": "catalog:pw@tcp(mysql:3306)/catalog",
                "CACHE_ENABLED": "false",
                "API_TOKEN": "t",
            }
        )
        self.assertEqual(redacted["MYSQL_ROOT_PASSWORD"], REDACTED)
        self.assertEqual(redacted["MYSQL_DSN"], REDACTED)
        self.assertEqual(redacted["API_TOKEN"], REDACTED)
        self.assertEqual(redacted["CACHE_ENABLED"], "false")

    def test_no_per_run_credential_reaches_the_manifest(self) -> None:
        """Manifests are written to artifact directories that outlive the
        trial, and the generated environment check now reads every service's
        environment rather than catalog-api's seven declared variables."""
        secret = "s" + "f" * 32
        model = catalog_model()
        env = dict(model.services["mysql"].environment)
        env["MYSQL_ROOT_PASSWORD"] = secret
        payload = json.dumps(redact_env(env))
        self.assertNotIn(secret, payload)

    def test_redaction_is_by_key_not_by_value(self) -> None:
        """A value-matching redactor would miss a credential it had not been
        told about. Matching on key shape catches the whole class."""
        self.assertEqual(redact_env({"SOME_SECRET": "x"})["SOME_SECRET"], REDACTED)
        self.assertEqual(redact_env({"DSN": "x"})["DSN"], REDACTED)


class EnvironmentCheckCoverageTests(unittest.TestCase):
    """`environment-variables:<svc>` passes vacuously for a service that
    declares nothing. That is the correct result, but a bare pass would read
    as a verification that happened, so the check has to say how much it
    examined. These tests exercise the check itself with the daemon read
    stubbed out, so they run with Docker unreachable.
    """

    def _run_check(self, declared: dict[str, str], observed: dict[str, str]):
        model = catalog_model()
        model.services["valkey"].environment.clear()
        model.services["valkey"].environment.update(declared)
        manifest = EnvironmentManifest(run_id="r", compose_project="p")
        holder = types.SimpleNamespace(manifest=manifest)
        original = environment_module.container_env
        environment_module.container_env = lambda _container: dict(observed)
        try:
            payload = TrialEnvironment._check_environment(
                holder, "valkey", "container", model
            )
        finally:
            environment_module.container_env = original
        gate = next(g for g in manifest.gates if g.name == "environment-variables:valkey")
        return gate, payload

    def test_a_service_declaring_nothing_records_explicit_zero_coverage(self) -> None:
        gate, payload = self._run_check({}, {"UNRELATED": "x"})
        self.assertTrue(gate.passed)
        self.assertEqual(payload["declaredCount"], 0)
        self.assertIn("coverage 0", gate.detail)

    def test_a_service_declaring_variables_records_how_many_it_checked(self) -> None:
        gate, payload = self._run_check({"A": "1", "B": "2"}, {"A": "1", "B": "2"})
        self.assertTrue(gate.passed)
        self.assertEqual(payload["declaredCount"], 2)
        self.assertNotIn("coverage 0", gate.detail)
        self.assertIn("2 declared", gate.detail)

    def test_a_mismatched_value_fails_the_gate(self) -> None:
        """Positive control for the two above: the check is capable of
        failing, so a pass with zero declared variables is a statement about
        the service and not about the check being inert."""
        gate, payload = self._run_check({"A": "1"}, {"A": "2"})
        self.assertFalse(gate.passed)
        self.assertEqual(payload["mismatched"], ["A"])


class HostClassTests(unittest.TestCase):
    """The host class must come from observation and must refuse the unknown.

    The mechanism being tested is not "can we build a string". It is that a
    machine nobody fitted tolerances on cannot obtain a verdict, and that no
    caller can talk its way into one.
    """

    def test_the_fixture_matches_the_derivation(self) -> None:
        """Without this the laptop fixture could drift from `derive_class_id`
        and every tolerance-resolution test below would be testing a class id
        that no real run ever produces."""
        self.assertEqual(derive_class_id(laptop_facts()), LAPTOP_M5_CLASS_ID)

    def test_the_fitted_class_has_a_frozen_set(self) -> None:
        self.assertIn(LAPTOP_M5_CLASS_ID, FROZEN_TOLERANCE_SETS)

    def test_a_different_machine_is_a_different_class(self) -> None:
        """Each envelope fact must move the class, because each one moves the
        performance envelope. A class that ignored core count would let bounds
        fitted on ten cores judge a two-core machine."""
        for field, value in (
            ("os_name", "Linux"),
            ("arch", "x86_64"),
            ("cpu_model", "Intel Xeon Platinum 8370C"),
            ("cpu_cores", 8),
            ("memory_bytes", 68719476736),
            ("docker_cpus", 8),
            ("docker_memory_bytes", 34359738368),
            ("docker_virtualized", False),
        ):
            with self.subTest(field=field):
                other = derive_class_id(laptop_facts(**{field: value}))
                self.assertNotEqual(other, LAPTOP_M5_CLASS_ID)

    def test_patch_versions_do_not_change_the_class(self) -> None:
        """Deliberate, and backed by measurement: the engine moved 29.7.2 to
        29.8.0 and Python 3.12.13 to 3.12.14 between the holdout and a later
        check, and the numbers did not move. Patch drift is reported through
        the fingerprint instead, so it stays visible without crying wolf."""
        drifted = laptop_facts(
            os_release="27.3.0", docker_engine_version="30.0.1", python_version="3.12.20"
        )
        self.assertEqual(derive_class_id(drifted), LAPTOP_M5_CLASS_ID)
        self.assertNotEqual(derive_fingerprint(drifted), derive_fingerprint(laptop_facts()))

    def test_the_fingerprint_contains_the_patch_versions(self) -> None:
        fingerprint = derive_fingerprint(laptop_facts())
        self.assertIn("docker29.8.0", fingerprint)
        self.assertIn("py3.12.14", fingerprint)
        self.assertIn("os27.2.0", fingerprint)

    def test_an_unreadable_fact_refuses_classification(self) -> None:
        """A missing fact must not become a default. An unread core count
        silently becoming 0 would produce a stable, meaningless class id that
        someone could then freeze tolerances against."""
        for field in ("cpu_model", "cpu_cores", "memory_bytes", "docker_memory_bytes"):
            with self.subTest(field=field):
                with self.assertRaises(HostClassError):
                    derive_class_id(laptop_facts(**{field: None}))

    def test_virtualization_is_detected_with_evidence(self) -> None:
        facts = observe_host(
            docker_info={
                "ServerVersion": "29.8.0",
                "OperatingSystem": "Docker Desktop",
                "KernelVersion": "7.0.12-linuxkit",
                "Architecture": "aarch64",
                "NCPU": "10",
                "MemTotal": "8319504384",
            }
        )
        self.assertTrue(facts.docker_virtualized)
        self.assertIn("linuxkit", facts.docker_virtualization_evidence)

    def test_a_linux_engine_on_a_linux_host_is_not_virtualized(self) -> None:
        """The Azure case. It must land in a different class from the laptop,
        which is the entire point of the mechanism."""
        facts = observe_host(
            docker_info={
                "ServerVersion": "27.1.1",
                "OperatingSystem": "Ubuntu 22.04.4 LTS",
                "KernelVersion": "6.5.0-1018-azure",
                "Architecture": "x86_64",
                "NCPU": "8",
                "MemTotal": "34359738368",
            }
        )
        if facts.os_name == "Linux":
            self.assertIs(facts.docker_virtualized, False)
        else:
            # Observed from macOS, where a virtual machine is always present
            # even when the engine did not advertise one, so the honest answer
            # is unknown rather than False.
            self.assertIsNone(facts.docker_virtualized)

    def test_gibibytes_rounds_for_display_only(self) -> None:
        self.assertEqual(gibibytes(34359738368), 32.0)
        self.assertEqual(gibibytes(8319504384), 7.7)
        self.assertIsNone(gibibytes(None))


class HostQualificationRefusalTests(unittest.TestCase):
    """An unknown host class must fail, not pass and not skip.

    This is the positive control the brief asked for. The failure it guards
    against is the quiet one: bounds fitted on a laptop being applied to a
    cloud virtual machine and producing a verdict that looks exactly like a
    real one.
    """

    def test_a_known_host_class_resolves(self) -> None:
        """Control for every test below. Without it, they could all pass
        because resolution never works rather than because it refuses."""
        resolved, audit = resolve_tolerance_set(laptop_facts())
        self.assertIsNotNone(resolved)
        self.assertTrue(audit["resolved"])
        self.assertIsNone(audit["refusal"])
        self.assertEqual(audit["observedClass"], LAPTOP_M5_CLASS_ID)

    def test_an_unknown_host_class_refuses(self) -> None:
        resolved, audit = resolve_tolerance_set(
            laptop_facts(cpu_model="Intel Xeon Platinum 8370C", cpu_cores=8)
        )
        self.assertIsNone(resolved)
        self.assertFalse(audit["resolved"])
        self.assertIn("no frozen tolerance set", audit["refusal"])

    def test_an_unknown_host_class_fails_the_exit_criterion(self) -> None:
        """The refusal has to reach the verdict, not just the audit block."""
        cycles = [_cycle(i) for i in range(1, 11)]
        unknown = laptop_facts(cpu_model="Neoverse-N2", cpu_cores=16)

        known_report = build_report(
            cycles, suite_id="s", cycles=10, host_facts=laptop_facts()
        )
        unknown_report = build_report(cycles, suite_id="s", cycles=10, host_facts=unknown)

        self.assertTrue(known_report["exitCriterionMet"])
        self.assertFalse(unknown_report["exitCriterionMet"])
        self.assertIn("no frozen tolerance set", unknown_report["hostQualification"]["refusal"])

    def test_an_unobserved_host_fails_the_exit_criterion(self) -> None:
        report = build_report(
            [_cycle(i) for i in range(1, 11)], suite_id="s", cycles=10, host_facts=None
        )
        self.assertFalse(report["exitCriterionMet"])
        self.assertIn("not observed", report["hostQualification"]["refusal"])

    def test_refusal_does_not_leave_an_empty_tolerance_list_passing(self) -> None:
        """`all([])` is True, so a refused run would have satisfied the
        tolerance clause vacuously and failed only by luck elsewhere. The
        report must contain no tolerance entries and the verdict must be false
        for that reason, not despite it."""
        report = build_report(
            [_cycle(i) for i in range(1, 11)], suite_id="s", cycles=10, host_facts=None
        )
        self.assertEqual(report["tolerances"], [])
        self.assertTrue(all(entry["withinTolerance"] for entry in report["tolerances"]))
        self.assertFalse(report["hostQualification"]["verdictPossible"])
        self.assertFalse(report["exitCriterionMet"])

    def test_a_registered_class_with_no_bounds_still_refuses(self) -> None:
        """This is the case the second half of `verdict_possible` exists for,
        and the only case that distinguishes it from the first half.

        A class present in the registry but carrying an empty tolerance tuple
        resolves successfully and then checks nothing. Without this test both
        halves of the condition could be deleted one at a time with every test
        still green, which is how the guard was written the first time.
        """
        empty_class = "test-only-empty-set"
        FROZEN_TOLERANCE_SETS[empty_class] = ToleranceSet(
            host_class=empty_class,
            description="a registry entry with no bounds in it",
            fitted_on="never fitted",
            fitted_at_commit="none",
            tolerances=(),
            max_error_rate=MAX_ERROR_RATE,
            max_stall_rate=MAX_STALL_RATE,
        )
        try:
            with unittest.mock.patch(
                "radius_perf_eval.trials.derive_class_id", return_value=empty_class
            ):
                report = build_report(
                    [_cycle(i) for i in range(1, 11)],
                    suite_id="s",
                    cycles=10,
                    host_facts=laptop_facts(),
                )
        finally:
            del FROZEN_TOLERANCE_SETS[empty_class]

        self.assertTrue(report["hostQualification"]["resolved"])
        self.assertFalse(report["hostQualification"]["verdictPossible"])
        self.assertIn("nothing to check", report["hostQualification"]["refusal"])
        self.assertFalse(report["exitCriterionMet"])

    def test_the_audit_names_the_classes_it_would_have_accepted(self) -> None:
        _, audit = resolve_tolerance_set(laptop_facts(cpu_cores=4))
        self.assertIn(LAPTOP_M5_CLASS_ID, audit["knownHostClasses"])
        self.assertIn("facts", audit)

    def test_every_frozen_set_is_keyed_by_its_own_host_class(self) -> None:
        """A set filed under the wrong key would be applied to the wrong
        machine, which is the exact failure this module exists to prevent."""
        for class_id, tolerance_set in FROZEN_TOLERANCE_SETS.items():
            with self.subTest(class_id=class_id):
                self.assertEqual(tolerance_set.host_class, class_id)
                self.assertTrue(tolerance_set.tolerances)
                self.assertTrue(tolerance_set.fitted_at_commit)


class FingerprintDriftTests(unittest.TestCase):
    """The fingerprint is recorded, compared, and acted on.

    An identifier that is written to a report and read by nothing is
    decoration. These tests pin the three things that stop it being that: a
    mismatch is named component by component, a mismatch blocks a scored
    start, and a recorded re-qualification clears it.
    """

    def test_an_identical_fingerprint_matches_with_no_drift(self) -> None:
        printed = derive_fingerprint(laptop_facts())
        comparison = compare_fingerprints(printed, printed)
        self.assertTrue(comparison.matches)
        self.assertTrue(comparison.comparable)
        self.assertEqual(comparison.drift, ())

    def test_drift_names_the_component_that_moved(self) -> None:
        fitted = derive_fingerprint(laptop_facts(python_version="3.12.13"))
        observed = derive_fingerprint(laptop_facts(python_version="3.12.14"))
        comparison = compare_fingerprints(observed, fitted)

        self.assertFalse(comparison.matches)
        self.assertTrue(comparison.comparable)
        self.assertEqual(len(comparison.drift), 1)
        entry = comparison.drift[0]
        self.assertEqual(entry["component"], "pythonVersion")
        self.assertEqual(entry["fittedOn"], "py3.12.13")
        self.assertEqual(entry["observed"], "py3.12.14")

    def test_several_components_can_drift_at_once(self) -> None:
        fitted = derive_fingerprint(
            laptop_facts(python_version="3.12.13", docker_engine_version="29.7.2")
        )
        observed = derive_fingerprint(laptop_facts())
        comparison = compare_fingerprints(observed, fitted)
        moved = {entry["component"] for entry in comparison.drift}
        self.assertEqual(moved, {"dockerEngine", "pythonVersion"})

    def test_an_unknown_fitted_fingerprint_is_a_mismatch_not_a_match(self) -> None:
        """The dangerous default. Treating "we never recorded one" as "it has
        not changed" would let the gate pass on exactly the hosts nobody has
        ever checked."""
        comparison = compare_fingerprints(derive_fingerprint(laptop_facts()), None)
        self.assertFalse(comparison.matches)
        self.assertFalse(comparison.comparable)


class ScoredStartGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = Path(tempfile.mkdtemp()) / "qualifications.json"
        self.addCleanup(shutil.rmtree, self.store.parent, ignore_errors=True)

    def test_an_unchanged_fingerprint_allows_a_scored_start(self) -> None:
        facts = laptop_facts()
        readiness = evaluate_scored_readiness(
            facts,
            fitted_fingerprint=derive_fingerprint(facts),
            tolerances_resolved=True,
            records=[],
        )
        self.assertTrue(readiness.allowed)
        self.assertIn("unchanged", readiness.reason)

    def test_a_changed_fingerprint_with_no_requalification_blocks(self) -> None:
        """The positive control. If this ever passes, the gate is decoration."""
        facts = laptop_facts()
        readiness = evaluate_scored_readiness(
            facts,
            fitted_fingerprint=derive_fingerprint(
                laptop_facts(python_version="3.12.13")
            ),
            tolerances_resolved=True,
            records=[],
        )
        self.assertFalse(readiness.allowed)
        self.assertIn("not re-qualified", readiness.reason)
        self.assertIn("pythonVersion", readiness.reason)

    def test_a_recorded_requalification_clears_the_block(self) -> None:
        facts = laptop_facts()
        record_requalification(
            host_class=derive_class_id(facts),
            fingerprint=derive_fingerprint(facts),
            cycles=3,
            suite_id="requal",
            driver_commit="abc1234",
            tolerances_fitted_at_commit="0407638",
            path=self.store,
        )
        readiness = evaluate_scored_readiness(
            facts,
            fitted_fingerprint=derive_fingerprint(
                laptop_facts(python_version="3.12.13")
            ),
            tolerances_resolved=True,
            path=self.store,
        )
        self.assertTrue(readiness.allowed)
        self.assertIn("re-qualified", readiness.reason)
        self.assertIsNotNone(readiness.requalification)

    def test_a_requalification_for_a_different_fingerprint_does_not_clear(self) -> None:
        """A record is for one fingerprint. Matching on host class alone would
        let a check of 3.12.13 vouch for 3.12.14."""
        facts = laptop_facts()
        record_requalification(
            host_class=derive_class_id(facts),
            fingerprint=derive_fingerprint(laptop_facts(python_version="3.12.13")),
            cycles=3,
            suite_id="requal",
            driver_commit="abc1234",
            tolerances_fitted_at_commit="0407638",
            path=self.store,
        )
        readiness = evaluate_scored_readiness(
            facts,
            fitted_fingerprint=None,
            tolerances_resolved=True,
            path=self.store,
        )
        self.assertFalse(readiness.allowed)

    def test_a_short_requalification_is_refused_at_write_time(self) -> None:
        with self.assertRaises(QualificationError):
            record_requalification(
                host_class="anything",
                fingerprint="anything",
                cycles=MIN_REQUALIFICATION_CYCLES - 1,
                suite_id="s",
                driver_commit="c",
                tolerances_fitted_at_commit="f",
                path=self.store,
            )

    def test_an_unresolved_host_class_blocks_regardless_of_fingerprint(self) -> None:
        facts = laptop_facts()
        readiness = evaluate_scored_readiness(
            facts,
            fitted_fingerprint=derive_fingerprint(facts),
            tolerances_resolved=False,
            records=[],
        )
        self.assertFalse(readiness.allowed)
        self.assertIn("no frozen tolerance set", readiness.reason)

    def test_an_unobserved_host_blocks(self) -> None:
        readiness = evaluate_scored_readiness(
            None, fitted_fingerprint="x", tolerances_resolved=True, records=[]
        )
        self.assertFalse(readiness.allowed)

    def test_records_survive_a_round_trip_through_the_store(self) -> None:
        record_requalification(
            host_class="class-a",
            fingerprint="class-a/os1/docker2/py3",
            cycles=3,
            suite_id="s1",
            driver_commit="c1",
            tolerances_fitted_at_commit="f1",
            path=self.store,
        )
        record_requalification(
            host_class="class-b",
            fingerprint="class-b/os1/docker2/py3",
            cycles=4,
            suite_id="s2",
            driver_commit="c2",
            tolerances_fitted_at_commit="f2",
            path=self.store,
        )
        loaded = load_requalifications(self.store)
        self.assertEqual([r.suite_id for r in loaded], ["s1", "s2"])
        self.assertEqual(loaded[1].cycles, 4)

    def test_a_corrupt_store_raises_rather_than_reading_as_empty(self) -> None:
        """An unreadable store that degrades to "no records" is indistinguishable
        from a clean machine, and would block rather than mislead -- but a
        store holding a malformed record could otherwise be silently skipped."""
        self.store.parent.mkdir(parents=True, exist_ok=True)
        self.store.write_text('{"requalifications": [{"hostClass": "a"}]}')
        with self.assertRaises(QualificationError):
            load_requalifications(self.store)

    def test_an_absent_store_reads_as_no_records(self) -> None:
        self.assertEqual(load_requalifications(self.store), [])


class ScoredGateInReportTests(unittest.TestCase):
    """The gate has to reach the report, and must not change the verdict."""

    def test_a_drifted_fingerprint_still_gets_a_verdict(self) -> None:
        report = build_report(
            [_cycle(i) for i in range(1, 11)],
            suite_id="s",
            cycles=10,
            host_facts=laptop_facts(),
            prior_requalifications=[],
        )
        self.assertTrue(report["exitCriterionMet"])
        self.assertFalse(report["hostQualification"]["scoredTrials"]["allowed"])

    def test_the_report_lists_the_drift(self) -> None:
        report = build_report(
            [_cycle(i) for i in range(1, 11)],
            suite_id="s",
            cycles=10,
            host_facts=laptop_facts(),
            prior_requalifications=[],
        )
        comparison = report["hostQualification"]["fingerprintComparison"]
        self.assertEqual(comparison["observed"], derive_fingerprint(laptop_facts()))
        self.assertIsNone(comparison["fittedOn"])
        self.assertFalse(comparison["comparable"])

    def test_a_prior_requalification_reaches_the_report(self) -> None:
        facts = laptop_facts()
        report = build_report(
            [_cycle(i) for i in range(1, 11)],
            suite_id="s",
            cycles=10,
            host_facts=facts,
            prior_requalifications=[
                Requalification(
                    host_class=derive_class_id(facts),
                    fingerprint=derive_fingerprint(facts),
                    recorded_at="2026-09-24T00:00:00+00:00",
                    cycles=3,
                    suite_id="requal",
                    driver_commit="abc1234",
                    tolerances_fitted_at_commit="0407638",
                )
            ],
        )
        scored = report["hostQualification"]["scoredTrials"]
        self.assertTrue(scored["allowed"])
        self.assertEqual(scored["requalification"]["suiteId"], "requal")

    def test_the_catalog_set_declares_its_fitted_fingerprint_as_unknown(self) -> None:
        """Pins the honest value. If someone later writes today's fingerprint
        in here to make the gate pass, this fails and says why."""
        self.assertIsNone(
            FROZEN_TOLERANCE_SETS[LAPTOP_M5_CLASS_ID].fitted_fingerprint
        )


# ---------------------------------------------------------------------------
# Astronomy Shop: vendored upstream, declared transforms, pinned digests
# ---------------------------------------------------------------------------


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[2]


def _vendored_compose_text() -> str:
    return "\n".join(
        path.read_text() for path in astronomy_shop.compose_file_paths(_repo_root())
    )


def _fake_config() -> dict:
    """A config shaped like the merged upstream one, built by hand.

    These tests run with the Docker daemon unreachable, so they cannot call
    `docker compose config`. The fixture carries one instance of everything
    the transforms are supposed to act on, and the separate `...OnRealUpstream`
    tests below check that the vendored files still contain those constructs,
    so the fixture cannot drift into describing a stack that no longer exists.
    """
    return {
        "name": "opentelemetry-demo",
        "networks": {"default": {"name": "opentelemetry-demo", "driver": "bridge"}},
        "services": {
            "otel-collector": {
                "container_name": "otel-collector",
                "image": "otel/opentelemetry-collector-contrib:0.119.0",
                "user": "0:0",
                "volumes": [
                    {"source": "/", "target": "/hostfs", "read_only": True},
                    {
                        "source": "/var/run/docker.sock",
                        "target": "/var/run/docker.sock",
                        "read_only": True,
                    },
                    {
                        "source": str(
                            astronomy_shop.upstream_dir(_repo_root())
                            / "src/otel-collector/otelcol-config.yml"
                        ),
                        "target": "/etc/otelcol-config.yml",
                    },
                ],
            },
            "frontend-proxy": {
                "container_name": "frontend-proxy",
                "image": "ghcr.io/open-telemetry/demo:latest-frontend-proxy",
                "ports": [
                    {"target": 8080, "published": "8080"},
                    {"target": 10000, "published": "10000"},
                ],
            },
            "flagd": {
                "container_name": "flagd",
                "image": "ghcr.io/open-feature/flagd:v0.12.2",
                "ports": [{"target": 8013, "published": "8013"}],
            },
            "flagd-ui": {
                "container_name": "flagd-ui",
                "image": "ghcr.io/open-telemetry/demo:latest-flagd-ui",
                "ports": [{"target": 4000, "published": "4000"}],
            },
            "cart": {
                "container_name": "cart",
                "image": "ghcr.io/open-telemetry/demo:latest-cart",
                "ports": [{"target": 8080}],
            },
        },
    }


class AstronomyShopPinTests(unittest.TestCase):
    """The vendored tree is pinned to one release and one commit."""

    def test_the_upstream_pin_names_a_tag_and_a_full_commit(self):
        self.assertEqual(astronomy_shop.UPSTREAM_TAG, "3.1.0")
        self.assertRegex(astronomy_shop.UPSTREAM_COMMIT, r"^[0-9a-f]{40}$")

    def test_the_vendored_tree_is_present_and_has_the_three_compose_files(self):
        paths = astronomy_shop.compose_file_paths(_repo_root())
        self.assertEqual(len(paths), 3)
        for path in paths:
            self.assertTrue(path.is_file(), f"missing vendored file: {path}")

    def test_the_upstream_directory_is_absolute(self):
        """Every caller runs `docker compose` with cwd set to this directory,
        so a relative path would be resolved twice and land nowhere."""
        self.assertTrue(astronomy_shop.upstream_dir(_repo_root()).is_absolute())

    def test_the_apache_licence_notice_is_kept(self):
        licence = astronomy_shop.upstream_dir(_repo_root()) / "LICENSE"
        self.assertTrue(licence.is_file())
        self.assertIn("Apache License", licence.read_text())


class AstronomyShopTransformTests(unittest.TestCase):
    """Each transform removes what it claims, and nothing else."""

    def test_the_docker_socket_mount_is_removed(self):
        config = _fake_config()
        changed = astronomy_shop.apply_transforms(config).applied
        self.assertEqual(changed["remove-docker-socket"], ["otel-collector"])
        sources = [
            v["source"] for v in config["services"]["otel-collector"]["volumes"]
        ]
        self.assertNotIn("/var/run/docker.sock", sources)

    def test_the_host_filesystem_mount_is_removed(self):
        config = _fake_config()
        changed = astronomy_shop.apply_transforms(config).applied
        self.assertEqual(changed["remove-host-filesystem"], ["otel-collector"])
        targets = [
            v["target"] for v in config["services"]["otel-collector"]["volumes"]
        ]
        self.assertNotIn("/hostfs", targets)

    def test_every_container_name_is_removed(self):
        config = _fake_config()
        changed = astronomy_shop.apply_transforms(config).applied
        self.assertEqual(len(changed["scope-container-names"]), 5)
        for name, service in config["services"].items():
            self.assertNotIn("container_name", service, name)

    def test_the_fixed_network_name_is_removed(self):
        config = _fake_config()
        changed = astronomy_shop.apply_transforms(config).applied
        self.assertEqual(changed["scope-network-names"], ["default"])
        self.assertNotIn("name", config["networks"]["default"])
        self.assertEqual(config["networks"]["default"]["driver"], "bridge")

    def test_published_ports_are_dropped_but_container_ports_are_kept(self):
        config = _fake_config()
        astronomy_shop.apply_transforms(config)
        proxy = config["services"]["frontend-proxy"]["ports"]
        self.assertEqual([p["target"] for p in proxy], [8080, 10000])
        for port in proxy:
            self.assertNotIn("published", port)

    def test_the_flag_services_lose_their_ports_entirely(self):
        config = _fake_config()
        changed = astronomy_shop.apply_transforms(config).applied
        self.assertEqual(sorted(changed["hide-flag-services"]), ["flagd", "flagd-ui"])
        for name in astronomy_shop.FLAG_SERVICES:
            self.assertEqual(config["services"][name]["ports"], [])

    def test_a_service_the_transforms_do_not_target_is_untouched(self):
        config = _fake_config()
        before = json.dumps(config["services"]["cart"]["ports"])
        astronomy_shop.apply_transforms(config)
        self.assertEqual(json.dumps(config["services"]["cart"]["ports"]), before)

    def test_a_transform_with_nothing_to_remove_reports_an_empty_list(self):
        """An upstream bump that drops the socket mount itself must show up as
        a transform that changed nothing, not as one that silently became a
        no-op while still claiming to protect something."""
        config = _fake_config()
        astronomy_shop.apply_transforms(config)
        again = astronomy_shop.apply_transforms(config).applied
        self.assertEqual(again["remove-docker-socket"], [])
        self.assertEqual(again["scope-container-names"], [])
        self.assertEqual(again["scope-network-names"], [])

    def test_every_declared_transform_is_reported_even_when_it_changes_nothing(self):
        report = astronomy_shop.apply_transforms(_fake_config())
        self.assertEqual(
            sorted(report.applied),
            sorted(t.name for t in astronomy_shop.TRANSFORMS),
        )

    def test_every_transform_carries_a_rationale(self):
        for transform in astronomy_shop.TRANSFORMS:
            self.assertGreater(len(transform.rationale), 80, transform.name)


class AstronomyShopTransformsOnRealUpstreamTests(unittest.TestCase):
    """Positive controls: the vendored files still contain what we remove.

    Without these the transform tests above could keep passing against a
    fixture describing constructs upstream no longer has, which is the shape
    of a test that proves nothing.
    """

    def test_upstream_really_does_bind_the_docker_socket(self):
        self.assertIn("DOCKER_SOCK", _vendored_compose_text())

    def test_upstream_really_does_bind_the_host_filesystem(self):
        self.assertIn("HOST_FILESYSTEM", _vendored_compose_text())

    def test_upstream_really_does_set_container_name_on_every_service(self):
        text = _vendored_compose_text()
        self.assertGreaterEqual(text.count("container_name:"), 28)

    def test_upstream_really_does_pin_the_network_name(self):
        self.assertIn("name: opentelemetry-demo", _vendored_compose_text())


class AstronomyShopCollectorConfigTests(unittest.TestCase):
    """The derived collector configs match the mounts the stack actually has."""

    def _derived_dir(self):
        return _repo_root() / "benchmark/apps/astronomy-shop/derived/otel-collector"

    def test_the_derived_configs_exist(self):
        self.assertTrue(self._derived_dir().is_dir())
        self.assertTrue((self._derived_dir() / "otelcol-config.yml").is_file())

    def test_the_removed_receivers_are_absent_from_every_derived_config(self):
        for path in self._derived_dir().glob("otelcol-config*.yml"):
            text = path.read_text()
            self.assertNotIn("docker_stats", text, path.name)
            self.assertNotIn("host_metrics", text, path.name)

    def test_upstream_really_does_configure_those_receivers(self):
        """Positive control. If upstream stops shipping them, the derivation
        is a no-op and this test says so instead of passing quietly."""
        source = (
            astronomy_shop.upstream_dir(_repo_root())
            / "src/otel-collector/otelcol-config.yml"
        ).read_text()
        self.assertIn("docker_stats", source)
        self.assertIn("host_metrics", source)

    def test_the_collector_mounts_are_repointed_at_the_derived_copies(self):
        config = _fake_config()
        changed = astronomy_shop.apply_transforms(config).applied
        self.assertEqual(changed["use-derived-collector-config"], ["otel-collector"])
        source = config["services"]["otel-collector"]["volumes"][-1]["source"]
        self.assertIn("derived/otel-collector/", source)
        self.assertNotIn("upstream/src/", source)

    def test_a_missing_derived_config_is_a_hard_error(self):
        """Docker would create an empty directory at the mount point and the
        collector would start against a config nobody reviewed."""
        config = _fake_config()
        config["services"]["otel-collector"]["volumes"] = [
            {
                "source": "/nowhere/upstream/src/otel-collector/otelcol-config.yml",
                "target": "/etc/otelcol-config.yml",
            }
        ]
        with self.assertRaises(FileNotFoundError):
            astronomy_shop.apply_transforms(config)

    def test_the_collector_config_manifest_records_what_was_removed(self):
        manifest = json.loads(
            (
                _repo_root()
                / "benchmark/apps/astronomy-shop/derived/collector-config-manifest.json"
            ).read_text()
        )
        self.assertEqual(manifest["upstreamTag"], astronomy_shop.UPSTREAM_TAG)
        self.assertEqual(manifest["upstreamCommit"], astronomy_shop.UPSTREAM_COMMIT)
        self.assertRegex(manifest["manifestHash"], r"^sha256:[0-9a-f]{64}$")
        removed = manifest["files"]["otelcol-config.yml"]["removed"]
        self.assertIn("receivers::docker_stats", removed)
        self.assertIn("receivers::host_metrics", removed)


class AstronomyShopFlagTests(unittest.TestCase):
    """The flags AIOpsLab drives are present, and the baseline is off."""

    def test_the_vendored_release_declares_every_flag_aiopslab_uses(self):
        flags = astronomy_shop.declared_flags(_repo_root())
        missing = sorted(set(astronomy_shop.AIOPSLAB_REQUIRED_FLAGS) - set(flags))
        self.assertEqual(missing, [], f"3.1.0 is missing {missing}")

    def test_the_required_flag_list_is_not_empty(self):
        """Guards the test above against passing because it compared nothing."""
        self.assertGreaterEqual(len(astronomy_shop.AIOPSLAB_REQUIRED_FLAGS), 11)

    def test_default_variants_are_read_from_the_definition_not_assumed_off(self):
        """Graded flags have numeric or duration neutral variants, so assuming
        the string "off" would mislabel them."""
        flags = astronomy_shop.declared_flags(_repo_root())
        defaults = astronomy_shop.default_off_flags(flags)
        self.assertEqual(sorted(defaults), sorted(flags))
        for name, variant in defaults.items():
            self.assertIn(variant, flags[name]["variants"], name)

    def test_the_flag_services_are_named(self):
        self.assertIn("flagd", astronomy_shop.FLAG_SERVICES)
        self.assertIn("flagd-ui", astronomy_shop.FLAG_SERVICES)


class AstronomyShopDigestTests(unittest.TestCase):
    """Every image is pinned by digest, and the manifest covers every service."""

    def _manifest(self) -> dict:
        return json.loads(
            (
                _repo_root() / "benchmark/apps/astronomy-shop/image-digests.json"
            ).read_text()
        )

    def test_the_manifest_hash_is_recorded(self):
        self.assertRegex(self._manifest()["manifestHash"], r"^sha256:[0-9a-f]{64}$")

    def test_every_pinned_reference_is_a_digest_not_a_tag(self):
        for service, reference in self._manifest()["images"].items():
            self.assertIn("@sha256:", reference, service)
            self.assertNotIn(":latest", reference, service)

    def test_the_manifest_covers_all_twenty_eight_services(self):
        self.assertEqual(len(self._manifest()["images"]), 28)

    def test_upstream_references_really_are_floating(self):
        """Positive control for the pinning step: if upstream ever ships
        digests itself, pinning is a no-op and this says so."""
        floating = astronomy_shop.floating_references(_fake_config())
        self.assertGreater(len(floating), 0)
        self.assertIn("cart", floating)

    def test_a_digest_reference_is_not_reported_as_floating(self):
        config = _fake_config()
        config["services"]["cart"]["image"] = "ghcr.io/x/demo@sha256:" + "a" * 64
        self.assertNotIn("cart", astronomy_shop.floating_references(config))


# ---------------------------------------------------------------------------
# Astronomy Shop readiness and the flag gate
# ---------------------------------------------------------------------------


def _shop_services() -> list[str]:
    """Service names taken from the vendored compose files, not hard-coded."""
    text = _vendored_compose_text()
    names = set()
    for line in text.splitlines():
        if line.startswith("  ") and line.endswith(":") and not line.startswith("    "):
            name = line.strip().rstrip(":")
            if name and not name.startswith("#"):
                names.add(name)
    return sorted(names)


class ShopReadinessCoverageTests(unittest.TestCase):
    """Every service is probed, and every probe names a real service."""

    def test_there_is_one_probe_for_every_service_in_the_compose_file(self):
        config = json.loads(
            (
                _repo_root()
                / "benchmark/apps/astronomy-shop/image-digests.json"
            ).read_text()
        )
        gaps = shop_readiness.missing_probes(sorted(config["images"]))
        self.assertEqual(gaps["servicesWithoutProbe"], [])
        self.assertEqual(gaps["probesWithoutService"], [])

    def test_the_probe_count_matches_the_service_count(self):
        self.assertEqual(len(shop_readiness.build_probes()), 28)

    def test_a_service_with_no_probe_is_reported(self):
        """Positive control for the reconciliation itself."""
        gaps = shop_readiness.missing_probes(["frontend", "a-new-service"])
        self.assertIn("a-new-service", gaps["servicesWithoutProbe"])

    def test_a_probe_naming_an_absent_service_is_reported(self):
        gaps = shop_readiness.missing_probes(["frontend"])
        self.assertIn("kafka", gaps["probesWithoutService"])

    def test_the_two_portless_services_are_probed_through_the_broker(self):
        """`accounting` and `fraud-detection` publish nothing, so the only
        externally observable readiness is consumer-group membership."""
        by_service = {p.service: p for p in shop_readiness.build_probes()}
        for name in shop_readiness.KAFKA_CONSUMERS:
            self.assertEqual(by_service[name].kind, "consumer-group", name)

    def test_the_flag_services_are_probed_from_inside_the_network(self):
        """They are unpublished by `hide-flag-services`, so a host probe could
        not reach them, and both images are distroless so exec is impossible."""
        by_service = {p.service: p for p in shop_readiness.build_probes()}
        for name in ("flagd", "flagd-ui"):
            self.assertEqual(by_service[name].kind, "internal-http", name)

    def test_every_probe_has_a_detail_describing_what_it_asks(self):
        for probe in shop_readiness.build_probes():
            self.assertTrue(probe.detail.strip(), probe.service)


class ShopReadinessResultTests(unittest.TestCase):
    """An unevaluable probe is a failure, never a skip."""

    def test_an_unevaluable_probe_is_recorded_as_not_ready_with_a_reason(self):
        context = shop_readiness.ProbeContext(
            project="p", compose_file="none.json", ports={}, timeout=0.1
        )
        with self.assertRaises(shop_readiness.ReadinessError):
            context.host_port("frontend", 8080)

    def test_a_missing_port_does_not_abort_the_whole_sweep(self):
        """A probe that raises must not hide the state of every probe after
        it, and must not be silently dropped from the result set."""
        probe = shop_readiness.Probe(
            service="x", kind="http", detail="d",
            evaluate=lambda _: (_ for _ in ()).throw(
                shop_readiness.ReadinessError("no port")
            ),
        )
        with unittest.mock.patch.object(
            shop_readiness, "build_probes", return_value=(probe,)
        ):
            results, ready = shop_readiness.evaluate_all(
                shop_readiness.ProbeContext(
                    project="p", compose_file="f", ports={}
                )
            )
        self.assertFalse(ready)
        self.assertEqual(len(results), 1)
        self.assertIn("no port", results[0].error)

    def test_the_probe_image_is_pinned_by_digest(self):
        self.assertIn("@sha256:", shop_readiness.PROBE_IMAGE)


class ShopFlagGateTests(unittest.TestCase):
    """The baseline gate reads flagd, and fails closed."""

    def _baseline(self) -> dict:
        return json.loads(
            (
                _repo_root() / "benchmark/apps/astronomy-shop/flag-baseline.json"
            ).read_text()
        )

    def test_the_recorded_baseline_covers_every_declared_flag(self):
        baseline = self._baseline()
        declared = astronomy_shop.declared_flags(_repo_root())
        self.assertEqual(sorted(baseline["resolved"]), sorted(declared))

    def test_the_recorded_baseline_agrees_with_the_shipped_default_variants(self):
        """Pins today's agreement. A flag whose targeting rules make it
        resolve to something other than its defaultVariant would show up here
        rather than silently weakening the gate."""
        self.assertEqual(self._baseline()["disagreements"], {})

    def test_unreadable_flag_state_fails_the_gate(self):
        """A gate that cannot see the flags has observed nothing, which is not
        the same as having observed a clean baseline."""
        with unittest.mock.patch.object(
            shop_readiness, "read_flag_state", return_value=(False, {}, "boom")
        ):
            state = shop_readiness.evaluate_flag_gate("p", {"adFailure": "off"})
        self.assertFalse(state.readable)
        self.assertFalse(state.baseline_clean)
        self.assertIn("boom", state.error)

    def test_a_flag_that_is_on_fails_the_gate(self):
        with unittest.mock.patch.object(
            shop_readiness, "read_flag_state",
            return_value=(True, {"adFailure": "on", "cartFailure": "off"}, ""),
        ):
            state = shop_readiness.evaluate_flag_gate(
                "p", {"adFailure": "off", "cartFailure": "off"}
            )
        self.assertFalse(state.baseline_clean)
        self.assertEqual(state.unexpected_on, ("adFailure",))

    def test_a_clean_baseline_passes(self):
        with unittest.mock.patch.object(
            shop_readiness, "read_flag_state",
            return_value=(True, {"adFailure": "off", "cartFailure": "off"}, ""),
        ):
            state = shop_readiness.evaluate_flag_gate(
                "p", {"adFailure": "off", "cartFailure": "off"}
            )
        self.assertTrue(state.baseline_clean)
        self.assertEqual(state.unexpected_on, ())

    def test_a_flag_missing_from_the_response_fails_the_gate(self):
        """flagd answering with a subset must not read as the subset being
        clean; the unreported flag's state is unknown, not off."""
        with unittest.mock.patch.object(
            shop_readiness, "read_flag_state",
            return_value=(True, {"adFailure": "off"}, ""),
        ):
            state = shop_readiness.evaluate_flag_gate(
                "p", {"adFailure": "off", "cartFailure": "off"}
            )
        self.assertFalse(state.baseline_clean)
        self.assertEqual(state.missing, ("cartFailure",))

    def test_a_graded_flag_is_compared_by_variant_not_by_truthiness(self):
        """Several flags carry numeric or duration payloads whose neutral
        setting is not boolean false, so comparing values would mislabel
        them."""
        with unittest.mock.patch.object(
            shop_readiness, "read_flag_state",
            return_value=(True, {"aiRunawayAgent": "high"}, ""),
        ):
            state = shop_readiness.evaluate_flag_gate("p", {"aiRunawayAgent": "off"})
        self.assertEqual(state.unexpected_on, ("aiRunawayAgent",))

    def test_unreadable_state_fails_even_when_nothing_else_could_fail(self):
        """Isolates the `readable` term.

        The other unreadable-state test passes because the early return also
        populates `missing`, so it would still pass if `readable` were dropped
        from the verdict entirely. With no expected flags there is nothing for
        `missing` or `unexpectedOn` to catch, so only `readable` can fail this.
        Found by mutation: removing `self.readable` from `baseline_clean` left
        the whole suite green.
        """
        with unittest.mock.patch.object(
            shop_readiness, "read_flag_state", return_value=(False, {}, "unreachable")
        ):
            state = shop_readiness.evaluate_flag_gate("p", {})
        self.assertEqual(state.missing, ())
        self.assertEqual(state.unexpected_on, ())
        self.assertFalse(state.baseline_clean)


class ShopCheckPlanTests(unittest.TestCase):
    """The shop's checks are generated, and its known gaps stay visible.

    The rendered 28-service stack is run-specific (project name, dynamically
    allocated ports), so committing one would pin a single run's ports as
    though they were a property of the fixture. These tests therefore exercise
    the machinery on a synthetic config that reproduces the two upstream
    properties that matter, and coverage of the real 28 services is asserted
    against the vendored digest manifest in `ShopReadinessCoverageTests`.
    """

    def _upstream_shaped_config(self, services: int = 3) -> str:
        """A config shaped like upstream: memory limits, no cpu, routing bridge."""
        return json.dumps(
            {
                "networks": {"default": {"driver": "bridge", "ipam": {}}},
                "services": {
                    f"svc{i}": {
                        "image": f"ghcr.io/x/svc{i}@sha256:{'0' * 64}",
                        "deploy": {"resources": {"limits": {"memory": "314572800"}}},
                        "networks": {"default": None},
                        "environment": {"OTEL_SERVICE_NAME": f"svc{i}"},
                    }
                    for i in range(services)
                },
            }
        )

    def test_every_service_gets_one_check_of_each_required_kind(self):
        plan = astronomy_shop.shop_check_plan(
            self._upstream_shaped_config(3),
            readiness_probes=("svc0", "svc1", "svc2"),
        )
        self.assertEqual(len(plan.checks), 3 * len(checks.REQUIRED_CHECK_KINDS))
        for service in ("svc0", "svc1", "svc2"):
            kinds = {c.kind for c in plan.checks if c.service == service}
            self.assertEqual(kinds, set(checks.REQUIRED_CHECK_KINDS), service)

    def test_adding_a_service_grows_the_check_set(self):
        """Positive control: the plan is derived, not enumerated."""
        small = astronomy_shop.shop_check_plan(self._upstream_shaped_config(3))
        large = astronomy_shop.shop_check_plan(self._upstream_shaped_config(4))
        self.assertEqual(
            len(large.checks) - len(small.checks), len(checks.REQUIRED_CHECK_KINDS)
        )

    def test_a_memory_only_limit_does_not_count_as_a_limit(self):
        """Upstream sets `deploy.resources.limits.memory` and no `cpus`.

        Half a limit must not read as a limit, because an unbounded CPU share
        across 28 services is the variance this check exists to catch.
        """
        plan = astronomy_shop.shop_check_plan(
            self._upstream_shaped_config(1), readiness_probes=("svc0",)
        )
        self.assertIn(
            "service 'svc0' declares no cpu and memory limits", plan.problems
        )

    def test_the_routing_default_bridge_is_reported_as_egress(self):
        plan = astronomy_shop.shop_check_plan(
            self._upstream_shaped_config(1), readiness_probes=("svc0",)
        )
        self.assertIn(
            "service 'svc0' can reach the network and declares no egress exception",
            plan.problems,
        )

    def test_the_shop_plan_takes_no_egress_exceptions(self):
        """The generator deliberately exposes no suppression parameter.

        `generate_check_plan` accepts `egress_exceptions`, which would silence
        the egress family for all 28 services while changing nothing about the
        environment. `shop_check_plan` does not forward it, so the gap cannot
        be made to disappear from sign-off by a caller.
        """
        signature = inspect.signature(astronomy_shop.shop_check_plan)
        self.assertNotIn("egress_exceptions", signature.parameters)

    def test_the_known_gaps_are_the_only_ones_on_an_otherwise_sound_service(self):
        """Pins the gap count, so a third family cannot appear unnoticed."""
        plan = astronomy_shop.shop_check_plan(
            self._upstream_shaped_config(1), readiness_probes=("svc0",)
        )
        self.assertEqual(len(plan.problems), 2)
        self.assertFalse(plan.complete)


# ---------------------------------------------------------------------------
# CPU limits: fitting, and the throttling acceptance test
# ---------------------------------------------------------------------------


class DemandStreamParseTests(unittest.TestCase):
    """The parser that turns kernel counters into per-service demand.

    These matter more than they look. The limits that throttled 19 of 28
    services were fitted from ``docker stats`` averages, and this parser
    exists to replace that basis, so a defect here reproduces the original
    fault with better provenance.
    """

    IDS = {"aaa": "payment", "bbb": "frontend"}

    def test_rate_uses_observed_elapsed_time(self):
        # 1.0s apart, 500_000us consumed -> 0.5 cores.
        text = "@100.0\naaa 1000000\n@101.0\naaa 1500000\n"
        got = cpu_limits.parse_demand_stream(text, {"aaa": "payment"})
        self.assertAlmostEqual(got["payment"].peak_cores, 0.5, places=6)

    def test_rate_reflects_real_interval_not_requested_one(self):
        # Same usage delta over 2s is half the rate of the same delta over 1s.
        slow = cpu_limits.parse_demand_stream(
            "@100.0\naaa 0\n@102.0\naaa 1000000\n", {"aaa": "payment"}
        )
        self.assertAlmostEqual(slow["payment"].peak_cores, 0.5, places=6)

    def test_peak_is_the_burst_not_the_average(self):
        """The defect that produced the bad fit, in miniature.

        A service idle for three intervals and then briefly at a full core
        averages 0.25 cores. Fitting 2x the average gives 0.5 and the burst
        needs 1.0, which is how payment ended up throttled at 17%.
        """
        # The burst sits in the middle deliberately. With it last, "the
        # maximum rate" and "the most recent rate" are the same number, and a
        # parser that reported the latter would pass while losing every burst
        # that is not the final one.
        text = (
            "@0.0\naaa 0\n"
            "@1.0\naaa 0\n"
            "@2.0\naaa 1000000\n"
            "@3.0\naaa 1000000\n"
            "@4.0\naaa 1000000\n"
        )
        got = cpu_limits.parse_demand_stream(text, {"aaa": "payment"})
        self.assertAlmostEqual(got["payment"].peak_cores, 1.0, places=6)
        self.assertAlmostEqual(got["payment"].mean_cores, 0.25, places=6)
        self.assertLess(got["payment"].mean_cores, got["payment"].peak_cores)
        # And the final interval really is quiet, so the assertion above can
        # only be satisfied by remembering the earlier burst.
        self.assertEqual(got["payment"].intervals, 4)

    def test_single_frame_is_rejected_rather_than_reported_as_zero(self):
        with self.assertRaises(cpu_limits.CpuLimitError) as ctx:
            cpu_limits.parse_demand_stream("@100.0\naaa 5\n", {"aaa": "payment"})
        self.assertIn("at least two", str(ctx.exception))

    def test_empty_stream_is_rejected(self):
        with self.assertRaises(cpu_limits.CpuLimitError):
            cpu_limits.parse_demand_stream("", {"aaa": "payment"})

    def test_non_advancing_clock_is_skipped_not_divided_by(self):
        """10ms clock resolution means consecutive frames can tie."""
        text = (
            "@100.0\naaa 0\n"
            "@100.0\naaa 500000\n"
            "@101.0\naaa 1000000\n"
        )
        got = cpu_limits.parse_demand_stream(text, {"aaa": "payment"})
        self.assertAlmostEqual(got["payment"].peak_cores, 0.5, places=6)
        self.assertEqual(got["payment"].intervals, 1)

    def test_counter_going_backwards_is_skipped(self):
        """usage_usec is monotonic, so a drop means the container was replaced."""
        text = (
            "@100.0\naaa 9000000\n"
            "@101.0\naaa 10\n"
            "@102.0\naaa 200010\n"
        )
        got = cpu_limits.parse_demand_stream(text, {"aaa": "payment"})
        self.assertAlmostEqual(got["payment"].peak_cores, 0.2, places=6)
        self.assertEqual(got["payment"].intervals, 1)

    def test_service_absent_from_a_frame_skips_only_that_interval(self):
        text = (
            "@100.0\naaa 0\nbbb 0\n"
            "@101.0\naaa 100000\n"
            "@102.0\naaa 200000\nbbb 400000\n"
        )
        got = cpu_limits.parse_demand_stream(
            text, self.IDS, expected_interval=1.0
        )
        self.assertEqual(got["payment"].intervals, 2)
        # Measured across the gap rather than dropped: 400_000us over 2.0s.
        self.assertEqual(got["frontend"].intervals, 1)
        self.assertAlmostEqual(got["frontend"].peak_cores, 0.2, places=6)
        self.assertEqual(got["frontend"].gap_intervals, 1)
        self.assertEqual(got["payment"].gap_intervals, 0)

    def test_unknown_container_ids_are_ignored(self):
        text = "@100.0\nzzz 0\naaa 0\n@101.0\nzzz 9000000\naaa 100000\n"
        got = cpu_limits.parse_demand_stream(text, {"aaa": "payment"})
        self.assertEqual(sorted(got), ["payment"])

    def test_malformed_lines_do_not_abort_the_parse(self):
        text = (
            "@100.0\naaa 0\ngarbage line here\n"
            "@not-a-clock\n"
            "@101.0\naaa 100000\naaa notanumber\n"
        )
        got = cpu_limits.parse_demand_stream(text, {"aaa": "payment"})
        self.assertAlmostEqual(got["payment"].peak_cores, 0.1, places=6)

    def test_mean_is_time_weighted_across_uneven_intervals(self):
        # 0.1 cores for 1s, then 1.0 core for 4s -> 4.1 core-seconds / 5s.
        text = (
            "@0.0\naaa 0\n"
            "@1.0\naaa 100000\n"
            "@5.0\naaa 4100000\n"
        )
        got = cpu_limits.parse_demand_stream(text, {"aaa": "payment"})
        self.assertAlmostEqual(got["payment"].mean_cores, 0.82, places=6)
        self.assertAlmostEqual(got["payment"].peak_cores, 1.0, places=6)

    def test_a_gap_understates_a_burst_so_it_is_counted(self):
        """A rate averaged over a gap hides the burst inside it.

        This is the same error as fitting from a multi-second average, so a
        gap is recorded rather than silently folded into the result.
        """
        text = (
            "@0.0\naaa 0\n"
            "@1.0\n"
            "@2.0\naaa 1000000\n"
        )
        got = cpu_limits.parse_demand_stream(
            text, {"aaa": "payment"}, expected_interval=1.0
        )
        self.assertEqual(got["payment"].gap_intervals, 1)
        # 1.0 core-second over 2.0s reads as 0.5, though the burst was 1.0.
        self.assertAlmostEqual(got["payment"].peak_cores, 0.5, places=6)

    def test_clean_run_reports_no_gaps(self):
        text = "@0.0\naaa 0\n@1.0\naaa 100000\n@2.0\naaa 200000\n"
        got = cpu_limits.parse_demand_stream(
            text, {"aaa": "payment"}, expected_interval=1.0
        )
        self.assertEqual(got["payment"].gap_intervals, 0)
        self.assertEqual(got["payment"].intervals, 2)


class DemandSamplerGuardTests(unittest.TestCase):
    """Argument guards, verified without a daemon."""

    def test_non_positive_duration_is_rejected(self):
        for bad in (0, -1.0):
            with self.assertRaises(cpu_limits.CpuLimitError):
                cpu_limits.sample_cpu_demand("p", duration_seconds=bad)

    def test_non_positive_interval_is_rejected(self):
        for bad in (0, -0.5):
            with self.assertRaises(cpu_limits.CpuLimitError):
                cpu_limits.sample_cpu_demand(
                    "p", duration_seconds=10, interval_seconds=bad
                )

    def test_sampler_does_not_use_date_percent_n(self):
        """busybox date ignores %N and returns whole seconds.

        That collapses every sub-second interval to a zero time delta, which
        the parser then skips, so the sampler would return almost no data
        while appearing to work. The clock must be /proc/uptime.
        """
        source = inspect.getsource(cpu_limits.sample_cpu_demand)
        body = source.split('"""')[-1]
        self.assertNotIn("date +", body)
        self.assertIn("/proc/uptime", body)

    def test_sidecar_is_unprivileged_and_has_no_docker_socket(self):
        source = inspect.getsource(cpu_limits.sample_cpu_demand)
        self.assertNotIn("--privileged", source)
        self.assertNotIn("docker.sock", source)
        self.assertIn("/hostcg:ro", source)


class CpuLimitFittingTests(unittest.TestCase):
    """The rule, and the manifest that records what it produced."""

    def test_the_rule_is_twice_the_peak(self):
        """Above half the floor, the multiplier is what decides the limit.

        The probe value used to be 0.60, which stopped exercising the
        multiplier the moment the floor rose to 8.0: the assertion still
        passed as a floor test while claiming to test the multiplier.
        """
        self.assertGreater(cpu_limits.MULTIPLIER * 5.0, cpu_limits.FLOOR_CORES)
        self.assertAlmostEqual(cpu_limits.fit_limit(5.0), 10.0)

    def test_the_floor_dominates_every_service_in_the_committed_fit(self):
        """Stated rather than left to be noticed from the numbers.

        No measured peak on this host class reaches half the floor, so every
        committed limit is the floor and the set is uniform. That uniformity
        is wanted: the agent under test reads this Compose file, and a limit
        fitted per service would tell it which service we expect to strain.
        It also means the multiplier does not bind here, which is why the
        test above picks a peak where it does.
        """
        payload = cpu_limits.load_fitted_limits(_repo_root())
        limits = set(payload["limitCores"].values())
        self.assertEqual(limits, {cpu_limits.FLOOR_CORES})
        self.assertLess(
            max(payload["peakCores"].values()),
            cpu_limits.FLOOR_CORES / cpu_limits.MULTIPLIER,
        )

    def test_a_small_service_gets_the_floor_not_a_tiny_limit(self):
        """Twice a 0.01-core peak is 0.02 cores, which would throttle the
        service constantly for no measurement benefit."""
        self.assertAlmostEqual(cpu_limits.fit_limit(0.01), cpu_limits.FLOOR_CORES)

    def test_rounding_never_lands_below_the_rule(self):
        for peak in (0.333, 0.1234, 0.9999, 1.005):
            self.assertGreaterEqual(
                cpu_limits.fit_limit(peak) + 1e-9,
                max(cpu_limits.MULTIPLIER * peak, cpu_limits.FLOOR_CORES),
                peak,
            )

    def test_fitting_nothing_is_an_error(self):
        with self.assertRaises(cpu_limits.CpuLimitError):
            cpu_limits.fit_limits({})

    def test_a_stale_hash_is_rejected_on_load(self):
        """The positive control for the guard, not just for the function.

        Mutation testing showed the previous tests pinned only that the hash
        changes when contents change. Disabling the guard in
        ``load_fitted_limits`` still passed them all, so the check that
        actually protects a run was itself unchecked.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = cpu_limits.cpu_limits_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.loads(
                cpu_limits.cpu_limits_path(_repo_root()).read_text()
            )
            payload["manifestHash"] = "sha256:" + "0" * 64
            path.write_text(json.dumps(payload))
            with self.assertRaises(cpu_limits.CpuLimitError) as caught:
                cpu_limits.load_fitted_limits(root)
            self.assertIn("manifestHash", str(caught.exception))

    def test_a_faithful_copy_loads(self):
        """So the test above fails for the hash, not for the copying."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = cpu_limits.cpu_limits_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                cpu_limits.cpu_limits_path(_repo_root()).read_text()
            )
            self.assertEqual(
                cpu_limits.load_fitted_limits(root)["manifestHash"],
                json.loads(path.read_text())["manifestHash"],
            )

    def test_the_committed_hash_matches_the_committed_contents(self):
        """The hash reaches provenance, so it has to be recomputable.

        An earlier revision carried a hash matching no basis that could be
        reconstructed from the file, which is provenance proving nothing.
        """
        payload = json.loads(
            cpu_limits.cpu_limits_path(_repo_root()).read_text()
        )
        self.assertEqual(
            payload["manifestHash"], cpu_limits.fitted_limits_hash(payload)
        )

    def test_an_edited_limit_invalidates_the_hash(self):
        """The positive control for the check above."""
        payload = json.loads(
            cpu_limits.cpu_limits_path(_repo_root()).read_text()
        )
        before = cpu_limits.fitted_limits_hash(payload)
        payload["limitCores"]["kafka"] = 99.0
        self.assertNotEqual(cpu_limits.fitted_limits_hash(payload), before)

    def test_the_hash_ignores_remeasured_peaks_that_change_no_limit(self):
        """Re-measuring moves the last decimal without moving a quota.

        If that counted as a fixture change, every re-measurement would
        look like one and the signal would stop meaning anything.
        """
        payload = json.loads(
            cpu_limits.cpu_limits_path(_repo_root()).read_text()
        )
        before = cpu_limits.fitted_limits_hash(payload)
        payload["peakCores"]["kafka"] = payload["peakCores"]["kafka"] + 0.0001
        self.assertEqual(cpu_limits.fitted_limits_hash(payload), before)

    def test_a_limit_that_does_not_follow_the_rule_is_rejected(self):
        """Otherwise the rule is a comment and the numbers are magic."""
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = cpu_limits.cpu_limits_path(root)
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = json.loads(
                cpu_limits.cpu_limits_path(_repo_root()).read_text()
            )
            payload["limitCores"]["kafka"] = cpu_limits.FLOOR_CORES / 2
            payload["manifestHash"] = cpu_limits.fitted_limits_hash(payload)
            path.write_text(json.dumps(payload))
            with self.assertRaises(cpu_limits.CpuLimitError) as caught:
                cpu_limits.load_fitted_limits(root)
            self.assertIn("kafka", str(caught.exception))

    def test_every_shop_service_has_a_fitted_limit(self):
        payload = cpu_limits.load_fitted_limits(_repo_root())
        digests = json.loads(
            (_repo_root() / "benchmark/apps/astronomy-shop/image-digests.json").read_text()
        )
        self.assertEqual(
            sorted(payload["limitCores"]), sorted(digests["images"])
        )

    def test_the_committed_limits_reproduce_from_the_committed_peaks(self):
        """The manifest is not free to drift from the rule that made it."""
        payload = cpu_limits.load_fitted_limits(_repo_root())
        refitted = cpu_limits.fit_limits(payload["peakCores"])
        self.assertEqual(refitted, payload["limitCores"])

    def test_a_manifest_fitted_under_a_different_rule_is_rejected(self):
        """Positive control for the rule check.

        Without it, changing the multiplier would leave 28 committed numbers
        describing a fitting nobody could reproduce.
        """
        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            path = cpu_limits.cpu_limits_path(root)
            path.parent.mkdir(parents=True)
            path.write_text(
                json.dumps(
                    {
                        "rule": {"multiplier": 99.0, "floorCores": 0.25},
                        "limitCores": {"a": 1.0},
                    }
                )
            )
            with self.assertRaises(cpu_limits.CpuLimitError) as caught:
                cpu_limits.load_fitted_limits(root)
        self.assertIn("frozen rule", str(caught.exception))

    def test_a_missing_manifest_is_an_error_not_an_empty_set(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(cpu_limits.CpuLimitError):
                cpu_limits.load_fitted_limits(pathlib.Path(tmp))


class CpuLimitTransformTests(unittest.TestCase):
    """Every service gets a limit, including the ones that barely use CPU."""

    def _config(self, *names: str) -> dict:
        return {
            "services": {
                n: {"image": "x@sha256:" + "0" * 64,
                    "deploy": {"resources": {"limits": {"memory": "1000"}}}}
                for n in names
            }
        }

    def test_the_transform_is_declared(self):
        self.assertIn(
            "apply-cpu-limits", {t.name for t in astronomy_shop.TRANSFORMS}
        )

    def test_a_service_with_no_fitted_limit_is_an_error(self):
        """Not a service left unlimited.

        An unlimited service in an otherwise limited stack is both a variance
        source and a tell: it is the one service whose shape differs.
        """
        with self.assertRaises(cpu_limits.CpuLimitError) as caught:
            astronomy_shop._apply_cpu_limits(self._config("not-a-real-service"))
        self.assertIn("not-a-real-service", str(caught.exception))

    def test_the_memory_limit_survives_the_transform(self):
        config = self._config("ad")
        astronomy_shop._apply_cpu_limits(config)
        limits = config["services"]["ad"]["deploy"]["resources"]["limits"]
        self.assertEqual(limits["memory"], "1000")
        self.assertIn("cpus", limits)

    def test_the_lightest_service_is_limited_too(self):
        """The uniformity property, stated as a test.

        `shipping` peaks at 0.01 cores and needs no limit for its own sake. It
        is limited so that a CPU-limit incident on some other service cannot
        be spotted by noticing which service has a limit at all.
        """
        payload = cpu_limits.load_fitted_limits(_repo_root())
        self.assertIn("shipping", payload["limitCores"])
        self.assertEqual(payload["limitCores"]["shipping"], cpu_limits.FLOOR_CORES)


class ThrottleVerdictTests(unittest.TestCase):
    """The acceptance test, which is the part that counts as evidence."""

    def _reading(self, service, periods, throttled, quota=1.0):
        return cpu_limits.ThrottleReading(
            service=service, nr_periods=periods, nr_throttled=throttled,
            throttled_usec=throttled * 1000, quota_cores=quota,
        )

    def test_a_clean_window_is_accepted(self):
        opened = {"a": self._reading("a", 100, 0)}
        closed = {"a": self._reading("a", 200, 0)}
        verdict = cpu_limits.verdict_from_readings(opened, closed, {"a": 1.0})
        self.assertTrue(verdict.accepted)
        # A service that never throttled must not be listed as bound. A
        # check that fires on everything is as useless as one that never
        # fires, and mutation testing showed nothing else pinned this.
        self.assertEqual(verdict.lifetime_bound, ())

    def test_throttling_during_the_window_fails(self):
        opened = {"a": self._reading("a", 100, 0)}
        closed = {"a": self._reading("a", 200, 5)}
        verdict = cpu_limits.verdict_from_readings(opened, closed, {"a": 1.0})
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.lifetime_bound, ("a",))
        # The window figure survives as diagnostic detail: it localises when
        # the throttling happened, which the lifetime total cannot.
        self.assertEqual(verdict.services[0].throttled, 5)

    def test_startup_throttling_fails_even_though_the_window_is_clean(self):
        """This assertion was inverted by measurement, deliberately.

        It used to assert that startup throttling was harmless, on the
        reasoning that only throttling during measurement can corrupt a
        measurement. A probe disproved the premise: throttling lengthens
        bring-up, so readiness timing becomes a function of host contention,
        which is run-to-run variance in the environment itself. The window
        subtraction it checked is still correct and still checked.
        """
        opened = {"a": self._reading("a", 100, 40)}
        closed = {"a": self._reading("a", 200, 40)}
        verdict = cpu_limits.verdict_from_readings(opened, closed, {"a": 1.0})
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.lifetime_bound, ("a",))
        self.assertEqual(verdict.services[0].cumulative_throttled, 40)
        self.assertEqual(verdict.services[0].throttled, 0)

    def test_a_service_absent_from_the_reading_fails(self):
        """A verdict that passed because a service went unread would be a
        check that examined nothing and reported success."""
        verdict = cpu_limits.verdict_from_readings({}, {}, {"a": 1.0})
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.missing, ("a",))

    def test_a_window_with_no_scheduling_periods_is_unmeasured_not_clean(self):
        opened = {"a": self._reading("a", 100, 0)}
        closed = {"a": self._reading("a", 100, 0)}
        verdict = cpu_limits.verdict_from_readings(opened, closed, {"a": 1.0})
        self.assertFalse(verdict.accepted)
        self.assertEqual(verdict.unmeasured, ("a",))

    def test_an_unfitted_service_present_in_the_project_is_reported(self):
        opened = {"a": self._reading("a", 100, 0), "b": self._reading("b", 100, 0)}
        closed = {"a": self._reading("a", 200, 0), "b": self._reading("b", 200, 0)}
        verdict = cpu_limits.verdict_from_readings(opened, closed, {"a": 1.0})
        self.assertFalse(verdict.accepted)
        self.assertTrue(any("b" in m for m in verdict.missing))

    def test_lifetime_throttling_is_reported_even_when_the_window_is_clean(self):
        """Measurement showed most throttling happens before any window opens.

        Kafka spent 131 throttled periods starting up and one in a 300s
        steady window. A verdict that reported only the window would call
        that stack clean, so the lifetime counter is surfaced separately.
        """
        opened = {"a": self._reading("a", 100, 40)}
        closed = {"a": self._reading("a", 200, 40)}
        verdict = cpu_limits.verdict_from_readings(opened, closed, {"a": 1.0})
        self.assertEqual(verdict.lifetime_bound, ("a",))
        self.assertEqual(verdict.to_dict()["lifetimeBound"], ["a"])

    def test_window_throttling_always_implies_lifetime_throttling(self):
        """Why the window budget was removed rather than kept alongside.

        Throttled periods counted inside a window are a subset of those
        counted since container start, so the lifetime criterion subsumes a
        window threshold entirely and that threshold could never decide a
        verdict. Keeping both would have been a dead conjunct. This pins the
        implication so the removal cannot be quietly undone.
        """
        opened = {"a": self._reading("a", 100, 0)}
        closed = {"a": self._reading("a", 200, 5)}
        verdict = cpu_limits.verdict_from_readings(opened, closed, {"a": 1.0})
        self.assertGreater(verdict.services[0].throttled, 0)
        self.assertEqual(verdict.lifetime_bound, ("a",))

    def test_the_verdict_exposes_no_tunable_throttling_threshold(self):
        """A gate with a knob invites the knob being turned until it passes.

        The criterion is zero throttled periods, which needs no threshold.
        """
        import inspect as _inspect
        sig = _inspect.signature(cpu_limits.verdict_from_readings)
        self.assertEqual(
            [p for p in sig.parameters if "budget" in p or "threshold" in p],
            [],
        )
        self.assertFalse(
            [n for n in dir(cpu_limits) if "BUDGET" in n or "THRESHOLD" in n]
        )

class ThrottleReadingParseTests(unittest.TestCase):
    """Parsing the kernel's files, with no daemon involved."""

    def test_cpu_stat_is_parsed(self):
        values = cpu_limits._parse_cpu_stat(
            "usage_usec 31557\nnr_periods 12\nnr_throttled 3\nthrottled_usec 99\n"
        )
        self.assertEqual(values["nr_periods"], 12)
        self.assertEqual(values["nr_throttled"], 3)

    def test_an_unset_cpu_max_reads_as_no_limit_not_as_zero(self):
        """`max 100000` means unlimited. Reading it as 0.0 would make an
        unlimited service look like the most constrained one."""
        self.assertIsNone(cpu_limits._parse_cpu_max("max 100000"))

    def test_a_quota_is_converted_to_cores(self):
        self.assertAlmostEqual(cpu_limits._parse_cpu_max("25000 100000"), 0.25)

    def test_a_project_with_no_containers_raises(self):
        """Nothing to read is not the same as nothing throttled."""
        def runner(*args, **kwargs):
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        with self.assertRaises(cpu_limits.CpuLimitError):
            cpu_limits.read_throttling("empty", runner=runner)

    def test_the_sidecar_image_is_pinned_by_digest(self):
        self.assertIn("@sha256:", cpu_limits.SIDECAR_IMAGE)
