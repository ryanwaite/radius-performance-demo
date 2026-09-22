"""Provenance and package-supply-chain compliance tests.

The plan's "Package supply chain compliance" section makes CFS-only package
consumption a hard constraint, and "Integrity and leakage controls" requires
the CFS-resolved versions to be recorded at freeze time. These tests treat
compliance as a property of the committed files, so a regression that
reintroduces a public registry fails here rather than in a security review.
"""

from __future__ import annotations

import json

import pytest

from radius_perf_eval.versions import (
    PROHIBITED_REGISTRY_HOSTS,
    capture_provenance,
    host_cli_version,
    package_supply_chain,
    pinned_distributions,
    sdk_versions,
    sha256_of_json,
)


# ---------------------------------------------------------------------------
# Digest stability
# ---------------------------------------------------------------------------


def test_digest_is_stable_across_key_order():
    assert sha256_of_json({"a": 1, "b": 2}) == sha256_of_json({"b": 2, "a": 1})


def test_digest_changes_with_content():
    assert sha256_of_json({"a": 1}) != sha256_of_json({"a": 2})


def test_digest_is_prefixed():
    assert sha256_of_json({}).startswith("sha256:")


# ---------------------------------------------------------------------------
# Committed CFS configuration
# ---------------------------------------------------------------------------


def test_exactly_one_index_is_configured():
    """A second index is a dependency-confusion risk and is prohibited."""
    supply_chain = package_supply_chain()
    assert supply_chain["singleIndex"] is True
    assert len(supply_chain["indexUrls"]) == 1


def test_configured_index_is_the_cfs_proxy():
    (url,) = package_supply_chain()["indexUrls"]
    assert url == "https://packagefeedproxy.microsoft.io/pypi/simple"


def test_committed_files_reference_no_public_registry():
    supply_chain = package_supply_chain()
    assert supply_chain["prohibitedRegistryReferences"] == []


def test_lockfile_is_present_and_actually_scanned():
    """Guard against the scan vacuously passing on a missing lockfile."""
    scanned = {
        entry["path"].rsplit("/", 1)[-1]: entry
        for entry in package_supply_chain()["scannedFiles"]
    }
    assert scanned["uv.lock"]["present"] is True
    assert scanned["uv.lock"]["hits"] == []
    assert scanned["pyproject.toml"]["present"] is True


def test_supply_chain_reports_compliant():
    assert package_supply_chain()["compliant"] is True


def test_scan_detects_a_planted_violation(tmp_path):
    """The scanner must be capable of failing, not just of returning []."""
    (tmp_path / "pyproject.toml").write_text(
        '[[tool.uv.index]]\nurl = "https://pypi.org/simple"\ndefault = true\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(
        'url = "https://files.pythonhosted.org/packages/x.whl"\n', encoding="utf-8"
    )
    supply_chain = package_supply_chain(tmp_path)
    assert supply_chain["compliant"] is False
    assert "pypi.org" in supply_chain["prohibitedRegistryReferences"]
    assert "files.pythonhosted.org" in supply_chain["prohibitedRegistryReferences"]


def test_scan_detects_a_second_index(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[[tool.uv.index]]\n"
        'url = "https://packagefeedproxy.microsoft.io/pypi/simple"\n'
        "default = true\n"
        "[[tool.uv.index]]\n"
        'url = "https://example.invalid/simple"\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text("", encoding="utf-8")
    supply_chain = package_supply_chain(tmp_path)
    assert supply_chain["singleIndex"] is False
    assert supply_chain["compliant"] is False


def test_missing_files_do_not_vacuously_pass(tmp_path):
    supply_chain = package_supply_chain(tmp_path)
    assert supply_chain["compliant"] is False


@pytest.mark.parametrize("host", PROHIBITED_REGISTRY_HOSTS)
def test_each_prohibited_host_is_detected(tmp_path, host):
    (tmp_path / "pyproject.toml").write_text(
        '[[tool.uv.index]]\nurl = "https://packagefeedproxy.microsoft.io/pypi/simple"\n',
        encoding="utf-8",
    )
    (tmp_path / "uv.lock").write_text(f"ref = {host}\n", encoding="utf-8")
    assert host in package_supply_chain(tmp_path)["prohibitedRegistryReferences"]


# ---------------------------------------------------------------------------
# Frozen pins
# ---------------------------------------------------------------------------


def test_resolved_versions_match_the_plan_defaults_table():
    resolved = pinned_distributions()
    assert resolved["github-copilot-sdk"] == "1.0.13"
    assert resolved["inspect-ai"] == "0.3.263"


def test_unknown_distribution_is_null_not_absent():
    resolved = pinned_distributions(["definitely-not-installed-xyz"])
    assert resolved == {"definitely-not-installed-xyz": None}


# ---------------------------------------------------------------------------
# Runtime provenance
# ---------------------------------------------------------------------------


def test_sdk_and_pinned_cli_are_recorded_separately():
    """Conflating the two would misattribute a result to the wrong runtime."""
    versions = sdk_versions()
    assert versions["sdkVersion"] == "1.0.13"
    assert versions["pinnedCliVersion"]
    assert versions["sdkProtocolVersion"] is not None


def test_host_cli_is_recorded_but_distinguished_from_pinned():
    host = host_cli_version()
    assert "hostCliPath" in host and "hostCliVersion" in host
    assert "pinnedCliVersion" not in host


def test_provenance_includes_every_required_field():
    payload = capture_provenance(model="gpt-5.4", reasoning_effort="medium").to_json_dict()
    assert payload["model"] == "gpt-5.4"
    assert payload["reasoningEffort"] == "medium"
    assert payload["versions"]["sdkVersion"]
    assert payload["runtime"]["pythonVersion"].startswith("3.12")
    assert payload["supplyChain"]["compliant"] is True
    assert payload["provenanceDigest"].startswith("sha256:")


def test_provenance_is_json_serializable():
    json.dumps(capture_provenance(model="gpt-5.4").to_json_dict())
