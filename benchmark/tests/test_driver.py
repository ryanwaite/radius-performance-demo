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
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from radius_perf_eval.compose import (  # noqa: E402
    ComposeError,
    ComposeProject,
    ResidualResources,
    parse_port_mapping,
    parse_resource_lines,
)
from radius_perf_eval.environment import EnvironmentSpec, ServiceResources  # noqa: E402
from radius_perf_eval.images import PinnedImage, is_digest  # noqa: E402
from radius_perf_eval.incidents import (  # noqa: E402
    MYSQL_POOL_DELAY_V1,
    IncidentVerification,
    VerificationCheck,
)
from radius_perf_eval.load import (  # noqa: E402
    LoadProfile,
    detect_stalls,
    percentile,
)
from radius_perf_eval.manifest import (  # noqa: E402
    FIXTURE_PATHS,
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
    INCIDENT_PROFILE,
    MAX_ERROR_RATE,
    MAX_STALL_RATE,
    STRUCTURAL_EXPECTATIONS,
    CycleResult,
    build_report,
    UNEXPLAINED_STALL,
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


class ManifestTests(unittest.TestCase):
    def _manifest(self) -> EnvironmentManifest:
        return EnvironmentManifest(run_id="run-1", compose_project="radius-eval-run-1")

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

    def test_unsigned_without_any_gate(self) -> None:
        self.assertFalse(self._manifest().signed_off)

    def test_signed_only_when_every_gate_passes(self) -> None:
        manifest = self._manifest()
        manifest.add_gate("a", True)
        self.assertTrue(manifest.signed_off)
        manifest.add_gate("b", False, "seed rows 9 != 10")
        self.assertFalse(manifest.signed_off)
        self.assertEqual([g.name for g in manifest.failed_gates], ["b"])

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
        manifest = self._manifest()
        manifest.add_gate("a", True)
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


class ReportGenerationTests(unittest.TestCase):
    """Exercise the whole report path, including the branches only a stall reaches.

    The stall block crashed on a field name that CycleResult does not define,
    and every unit test passed anyway, because none of them built a cycle with
    a nonzero stall count. Testing the detector was not the same as testing
    the thing that reports it.
    """

    def test_report_builds_with_a_stall_present(self) -> None:
        cycles = [_cycle(i) for i in range(1, 10)] + [_cycle(10, stalls=1.0)]
        report = build_report(cycles, suite_id="suite", cycles=10)

        observations = report["stallBudget"]["observations"]
        self.assertEqual(len(observations), 1)
        self.assertEqual(observations[0]["runId"], "suite-c10")
        self.assertEqual(observations[0]["count"], 1)
        self.assertAlmostEqual(observations[0]["maxLatencySeconds"], 1.85)

    def test_report_is_json_serialisable_with_a_stall(self) -> None:
        # The report is written to disk; a value that cannot be encoded fails
        # just as completely as an AttributeError.
        cycles = [_cycle(i) for i in range(1, 10)] + [_cycle(10, stalls=3.0)]
        report = build_report(cycles, suite_id="suite", cycles=10)
        encoded = json.dumps(report, sort_keys=True, default=str)
        self.assertIn("stallBudget", encoded)

    def test_clean_run_reports_no_observations(self) -> None:
        report = build_report(
            [_cycle(i) for i in range(1, 11)], suite_id="suite", cycles=10
        )
        self.assertEqual(report["stallBudget"]["observations"], [])
        self.assertTrue(report["stallBudget"]["withinBudget"])

    def test_stall_budget_fails_when_breached(self) -> None:
        # 8 stalls in a 640-request window is 1.25%, above the 0.5% bound.
        cycles = [_cycle(i) for i in range(1, 10)] + [_cycle(10, stalls=8.0)]
        report = build_report(cycles, suite_id="suite", cycles=10)
        self.assertFalse(report["stallBudget"]["withinBudget"])
        self.assertFalse(report["exitCriterionMet"])

    def test_absolute_excursions_are_reported_alongside_relative_stalls(self) -> None:
        cycles = [_cycle(i) for i in range(1, 10)] + [_cycle(10, stalls=1.0, excursions=1.0)]
        report = build_report(cycles, suite_id="suite", cycles=10)
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
        )
        self.assertEqual(len(report["unmeasured"]["setupCycles"]), 1)
        self.assertEqual(len(report["unmeasured"]["warmupCycles"]), 1)

    def test_gate_definition_is_pre_registered_in_the_report(self) -> None:
        report = build_report(
            [_cycle(i) for i in range(1, 11)], suite_id="suite", cycles=10
        )
        pre = report["stallBudget"]["preRegistration"]
        self.assertEqual(pre["stallFactor"], 3.0)
        self.assertEqual(pre["maxStallRate"], MAX_STALL_RATE)

    def test_failed_cycle_does_not_meet_exit_criterion(self) -> None:
        cycles = [_cycle(i) for i in range(1, 10)] + [_cycle(10, ok=False)]
        report = build_report(cycles, suite_id="suite", cycles=10)
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
