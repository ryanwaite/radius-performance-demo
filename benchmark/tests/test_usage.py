"""Unit tests for usage normalization and reconciliation.

These use recorded/synthetic ``assistant.usage`` and ``session.usage.getMetrics``
payloads so the accounting logic is testable without burning model calls.
"""

from __future__ import annotations

import pytest

from radius_perf_eval.usage import (
    NANO_AIU_PER_AI_CREDIT,
    TokenOverlapPolicy,
    build_normalized_record,
    collect_compaction,
    normalize_from_metrics,
    normalize_from_usage_events,
    reconcile,
)


def usage_event(**overrides):
    """A realistic assistant.usage payload, wire-shaped."""
    base = {
        "model": "gpt-5.4",
        "inputTokens": 1000,
        "outputTokens": 200,
        "cost": 1.0,
        "copilotUsage": {"totalNanoAiu": 500_000_000.0, "model": "gpt-5.4"},
    }
    base.update(overrides)
    return base


def metrics_payload(**overrides):
    base = {
        "modelMetrics": {
            "gpt-5.4": {
                "requests": {"count": 2, "cost": 2.0},
                "usage": {
                    "inputTokens": 2000,
                    "outputTokens": 400,
                    "cacheReadTokens": 0,
                    "cacheWriteTokens": 0,
                    "reasoningTokens": 0,
                },
            }
        },
        "totalNanoAiu": 1_000_000_000.0,
        "totalPremiumRequestCost": 2.0,
        "totalApiDurationMs": 4200,
        "totalUserRequests": 1,
        "lastCallInputTokens": 1000,
        "lastCallOutputTokens": 200,
        "sessionStartTime": "2026-09-22T00:00:00Z",
        "codeChanges": {
            "filesModified": [],
            "filesModifiedCount": 0,
            "linesAdded": 0,
            "linesRemoved": 0,
        },
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------------------
# Null handling: a missing field is null, never zero
# ---------------------------------------------------------------------------


def test_unreported_fields_are_null_not_zero():
    result = normalize_from_usage_events([usage_event()])
    # cacheRead/cacheWrite/reasoning were never reported by the provider.
    assert result.totals.input_cached_read_tokens is None
    assert result.totals.input_cache_write_tokens is None
    assert result.totals.output_reasoning_tokens is None
    # ...and are emphatically not zero.
    assert result.totals.input_cached_read_tokens != 0


def test_explicit_zero_is_preserved_as_zero():
    result = normalize_from_usage_events([usage_event(cacheReadTokens=0)])
    assert result.totals.input_cached_read_tokens == 0


def test_empty_event_stream_yields_all_null():
    result = normalize_from_usage_events([])
    assert result.model_requests == 0
    assert result.totals.input_uncached_tokens is None
    assert result.totals.output_visible_tokens is None
    assert result.ai_credits is None


def test_provider_total_is_never_derived():
    # Deriving a grand total would sum overlapping fields.
    result = normalize_from_usage_events([usage_event()])
    assert result.totals.provider_reported_total_tokens is None
    assert any("providerReportedTotalTokens" in a for a in result.ambiguities)


# ---------------------------------------------------------------------------
# Overlap handling
# ---------------------------------------------------------------------------


def test_reasoning_tokens_are_subtracted_from_output():
    result = normalize_from_usage_events(
        [usage_event(outputTokens=500, reasoningTokens=300)]
    )
    assert result.totals.output_visible_tokens == 200
    assert result.totals.output_reasoning_tokens == 300
    # Visible + reasoning reconstructs the provider total; they never double count.
    assert (
        result.totals.output_visible_tokens + result.totals.output_reasoning_tokens == 500
    )


def test_reasoning_greater_than_output_clamps_at_zero():
    result = normalize_from_usage_events(
        [usage_event(outputTokens=100, reasoningTokens=400)]
    )
    assert result.totals.output_visible_tokens == 0


def test_unknown_cache_overlap_yields_null_and_flags_ambiguity():
    result = normalize_from_usage_events(
        [usage_event(inputTokens=1000, cacheReadTokens=800)],
        policy=TokenOverlapPolicy.UNKNOWN,
    )
    assert result.totals.input_uncached_tokens is None
    assert result.totals.input_cached_read_tokens == 800
    assert any("inputUncachedTokens is null" in a for a in result.ambiguities)
    # The raw provider value is still retained for auditing.
    assert result.raw_input_tokens == 1000


def test_inclusive_policy_subtracts_cache_reads():
    result = normalize_from_usage_events(
        [usage_event(inputTokens=1000, cacheReadTokens=800)],
        policy=TokenOverlapPolicy.INPUT_INCLUDES_CACHE_READS,
    )
    assert result.totals.input_uncached_tokens == 200
    assert result.totals.input_cached_read_tokens == 800


def test_exclusive_policy_keeps_input_tokens_as_is():
    result = normalize_from_usage_events(
        [usage_event(inputTokens=1000, cacheReadTokens=800)],
        policy=TokenOverlapPolicy.INPUT_EXCLUDES_CACHE_READS,
    )
    assert result.totals.input_uncached_tokens == 1000


def test_zero_cache_read_needs_no_policy():
    # With no cache read there is no overlap, so UNKNOWN is still unambiguous.
    result = normalize_from_usage_events(
        [usage_event(inputTokens=1000, cacheReadTokens=0)],
        policy=TokenOverlapPolicy.UNKNOWN,
    )
    assert result.totals.input_uncached_tokens == 1000
    assert not any("inputUncachedTokens is null" in a for a in result.ambiguities)


# ---------------------------------------------------------------------------
# Aggregation, including subagents
# ---------------------------------------------------------------------------


def test_events_aggregate_across_calls():
    result = normalize_from_usage_events(
        [
            usage_event(inputTokens=1000, outputTokens=200),
            usage_event(inputTokens=1500, outputTokens=300),
        ]
    )
    assert result.model_requests == 2
    assert result.totals.input_uncached_tokens == 2500
    assert result.totals.output_visible_tokens == 500


def test_subagent_calls_are_included_once():
    # assistant.usage is emitted for sub-agent calls too; they are part of the
    # single event stream and must not be added a second time.
    events = [
        usage_event(inputTokens=1000, outputTokens=100),
        usage_event(inputTokens=500, outputTokens=50, model="claude-sonnet-5"),
    ]
    result = normalize_from_usage_events(events)
    assert result.model_requests == 2
    assert result.totals.input_uncached_tokens == 1500
    assert result.models_seen == ["gpt-5.4", "claude-sonnet-5"]


def test_ai_credits_convert_from_nano_aiu():
    result = normalize_from_usage_events(
        [
            usage_event(copilotUsage={"totalNanoAiu": 500_000_000.0}),
            usage_event(copilotUsage={"totalNanoAiu": 250_000_000.0}),
        ]
    )
    assert result.ai_credits == pytest.approx(0.75)


def test_premium_multiplier_is_summed_not_treated_as_usd():
    result = normalize_from_usage_events([usage_event(cost=1.0), usage_event(cost=0.5)])
    assert result.premium_requests_charged == pytest.approx(1.5)


# ---------------------------------------------------------------------------
# Metrics normalization
# ---------------------------------------------------------------------------


def test_metrics_normalization_reads_model_metrics():
    result = normalize_from_metrics(metrics_payload())
    assert result.model_requests == 2
    assert result.totals.input_uncached_tokens == 2000
    assert result.totals.output_visible_tokens == 400
    assert result.ai_credits == pytest.approx(1.0)
    assert result.premium_requests_charged == pytest.approx(2.0)
    assert result.api_duration_ms == 4200


def test_agent_metrics_are_not_added_to_model_metrics():
    """agentMetrics is a breakdown of the same spend; adding it double counts."""
    payload = metrics_payload(
        agentMetrics={
            "main": {
                "modelMetrics": {
                    "gpt-5.4": {
                        "requests": {"count": 2, "cost": 2.0},
                        "usage": {
                            "inputTokens": 2000,
                            "outputTokens": 400,
                            "cacheReadTokens": 0,
                            "cacheWriteTokens": 0,
                            "reasoningTokens": 0,
                        },
                    }
                },
                "totalApiDurationMs": 4200,
                "totalNanoAiu": 1_000_000_000.0,
            }
        }
    )
    result = normalize_from_metrics(payload)
    # Still 2000, not 4000.
    assert result.totals.input_uncached_tokens == 2000
    assert result.model_requests == 2


def test_metrics_multi_model_sums_across_models():
    payload = metrics_payload(
        modelMetrics={
            "gpt-5.4": {
                "requests": {"count": 1},
                "usage": {"inputTokens": 1000, "outputTokens": 100},
            },
            "claude-sonnet-5": {
                "requests": {"count": 3},
                "usage": {"inputTokens": 500, "outputTokens": 50},
            },
        }
    )
    result = normalize_from_metrics(payload)
    assert result.totals.input_uncached_tokens == 1500
    assert result.model_requests == 4


def test_metrics_missing_nano_aiu_is_null_and_flagged():
    payload = metrics_payload()
    del payload["totalNanoAiu"]
    result = normalize_from_metrics(payload)
    assert result.ai_credits is None
    assert any("totalNanoAiu" in a for a in result.ambiguities)


# ---------------------------------------------------------------------------
# Reconciliation
# ---------------------------------------------------------------------------


def test_reconciliation_matches_when_sources_agree():
    events = normalize_from_usage_events(
        [
            usage_event(inputTokens=1000, outputTokens=200, cacheReadTokens=0),
            usage_event(inputTokens=1000, outputTokens=200, cacheReadTokens=0),
        ]
    )
    metrics = normalize_from_metrics(metrics_payload())
    result = reconcile(events, metrics)
    assert result.agreed, [f.to_json_dict() for f in result.mismatches]


def test_reconciliation_reports_mismatch_without_overwriting():
    events = normalize_from_usage_events(
        [usage_event(inputTokens=999, outputTokens=200, cacheReadTokens=0)]
    )
    metrics = normalize_from_metrics(metrics_payload())
    result = reconcile(events, metrics)
    assert not result.agreed
    mismatched = {f.field for f in result.mismatches}
    assert "inputUncachedTokens" in mismatched
    # Both original values survive the comparison.
    field = next(f for f in result.fields if f.field == "inputUncachedTokens")
    assert field.from_events == 999
    assert field.from_metrics == 2000
    assert field.delta == pytest.approx(1001)


def test_reconciliation_when_metrics_unavailable():
    events = normalize_from_usage_events([usage_event()])
    result = reconcile(events, None)
    assert all(f.status == "metrics-unavailable" for f in result.fields)
    # An unavailable experimental RPC is not a mismatch.
    assert result.agreed


def test_reconciliation_distinguishes_null_from_zero():
    events = normalize_from_usage_events([usage_event()])  # no cacheRead reported
    metrics = normalize_from_metrics(metrics_payload())  # cacheRead == 0
    result = reconcile(events, metrics)
    field = next(f for f in result.fields if f.field == "inputCachedReadTokens")
    assert field.status == "events-null"
    assert field.from_events is None
    assert field.from_metrics == 0


def test_reconciliation_tolerance():
    # Two calls, so modelRequests agrees with the metrics payload; only the
    # token totals differ slightly.
    events = normalize_from_usage_events(
        [usage_event(inputTokens=995, outputTokens=200, cacheReadTokens=0)] * 2
    )
    metrics = normalize_from_metrics(metrics_payload())
    strict = reconcile(events, metrics)
    assert not strict.agreed
    loose = reconcile(events, metrics, relative_tolerance=0.01)
    assert loose.agreed, [f.to_json_dict() for f in loose.mismatches]


# ---------------------------------------------------------------------------
# Normalized record shape
# ---------------------------------------------------------------------------


def test_normalized_record_shape_and_no_invented_cost():
    events = normalize_from_usage_events(
        [usage_event(inputTokens=1000, outputTokens=200, cacheReadTokens=0)] * 2
    )
    metrics = normalize_from_metrics(metrics_payload())
    record = build_normalized_record(
        model="gpt-5.4",
        agent_wall_clock_ms=1234.5,
        logical_tool_calls=3,
        tool_attempts=4,
        from_events=events,
        from_metrics=metrics,
        reconciliation=reconcile(events, metrics),
        premium_request_multiplier=None,
        pricing_version="sha256:abc",
    )

    assert record["runtime"] == "github-copilot-sdk"
    assert record["model"] == "gpt-5.4"
    assert record["agentWallClockMs"] == 1234.5
    assert record["logicalToolCalls"] == 3
    assert record["toolAttempts"] == 4
    assert set(record["usage"]) == {
        "inputUncachedTokens",
        "inputCachedReadTokens",
        "inputCacheWriteTokens",
        "outputVisibleTokens",
        "outputReasoningTokens",
        "providerReportedTotalTokens",
    }
    # Copilot reports credits, not money. USD must never be invented.
    assert record["cost"]["actualUSD"] is None
    assert record["cost"]["estimatedUSD"] is None
    assert record["cost"]["pricingVersion"] == "sha256:abc"
    # Raw sources are retained alongside the normalized view.
    assert record["sources"]["assistantUsageEvents"] is not None
    assert record["sources"]["sessionUsageMetrics"] is not None


def test_normalized_record_falls_back_to_events_when_metrics_missing():
    events = normalize_from_usage_events(
        [usage_event(inputTokens=1000, outputTokens=200, cacheReadTokens=0)]
    )
    record = build_normalized_record(
        model="gpt-5.4",
        agent_wall_clock_ms=10.0,
        logical_tool_calls=0,
        tool_attempts=0,
        from_events=events,
        from_metrics=None,
        reconciliation=reconcile(events, None),
    )
    assert record["usageSource"] == "assistant.usage-events"
    assert record["usage"]["inputUncachedTokens"] == 1000
    assert record["sources"]["sessionUsageMetrics"] is None


def test_normalized_record_prefers_events_and_does_not_merge():
    """Totals come from exactly one source; preference is never a sum."""
    events = normalize_from_usage_events(
        [usage_event(inputTokens=1000, outputTokens=200, cacheReadTokens=0)]
    )
    metrics = normalize_from_metrics(metrics_payload())
    record = build_normalized_record(
        model="gpt-5.4",
        agent_wall_clock_ms=10.0,
        logical_tool_calls=0,
        tool_attempts=0,
        from_events=events,
        from_metrics=metrics,
        reconciliation=reconcile(events, metrics),
    )
    assert record["usageSource"] == "assistant.usage-events"
    # 1000 from events, not 2000 (metrics) and not 3000 (a merge).
    assert record["usage"]["inputUncachedTokens"] == 1000
    # The unused source is still retained verbatim for audit.
    assert record["sources"]["sessionUsageMetrics"]["usage"]["inputUncachedTokens"] == 2000


def test_metrics_disagreement_is_surfaced_not_silently_resolved():
    events = normalize_from_usage_events(
        [usage_event(inputTokens=1000, outputTokens=200, cacheReadTokens=0)]
    )
    metrics = normalize_from_metrics(metrics_payload())
    record = build_normalized_record(
        model="gpt-5.4",
        agent_wall_clock_ms=10.0,
        logical_tool_calls=0,
        tool_attempts=0,
        from_events=events,
        from_metrics=metrics,
        reconciliation=reconcile(events, metrics),
    )
    assert record["reconciliation"]["agreed"] is False
    assert record["reconciliation"]["mismatchCount"] > 0


def test_float_summation_noise_is_not_a_mismatch():
    """Nano-AIU quotient summation must not fabricate a disagreement."""
    events = normalize_from_usage_events(
        [
            usage_event(copilotUsage={"totalNanoAiu": 1_839_000_000.0}),
            usage_event(copilotUsage={"totalNanoAiu": 403_100_000.0}),
        ]
    )
    assert events.ai_credits != 2.2421  # IEEE-754 noise is genuinely present
    metrics = normalize_from_metrics(metrics_payload(totalNanoAiu=2_242_100_000.0))
    field = next(f for f in reconcile(events, metrics).fields if f.field == "aiCredits")
    assert field.status == "match"


def test_integer_token_counts_are_still_compared_exactly():
    events = normalize_from_usage_events(
        [usage_event(inputTokens=1999, outputTokens=200, cacheReadTokens=0)]
    )
    metrics = normalize_from_metrics(metrics_payload())
    field = next(
        f for f in reconcile(events, metrics).fields if f.field == "inputUncachedTokens"
    )
    assert field.status == "mismatch"


def test_nano_aiu_constant_is_explicit():
    assert NANO_AIU_PER_AI_CREDIT == 1_000_000_000.0


# ---------------------------------------------------------------------------
# copilotUsage.tokenDetails: the billed, non-overlapping token partition.
# Shapes below are transcribed from live CLI 1.0.83 assistant.usage payloads.
# ---------------------------------------------------------------------------


def token_details(input_=0, cache_read=0, cache_write=0, output=0):
    return [
        {"batchSize": 1_000_000, "costPerBatch": 250_000_000_000, "tokenCount": input_, "tokenType": "input"},
        {"batchSize": 1_000_000, "costPerBatch": 25_000_000_000, "tokenCount": cache_read, "tokenType": "cache_read"},
        {"batchSize": 1_000_000, "costPerBatch": 0, "tokenCount": cache_write, "tokenType": "cache_write"},
        {"batchSize": 1_000_000, "costPerBatch": 1_500_000_000_000, "tokenCount": output, "tokenType": "output"},
    ]


def test_token_details_supply_authoritative_uncached_input():
    # Live shape: inputTokens 9042 == tokenDetails input 6482 + cache_read 2560.
    event = usage_event(
        inputTokens=9042,
        cacheReadTokens=2560,
        outputTokens=103,
        reasoningTokens=45,
        copilotUsage={
            "totalNanoAiu": 1_839_000_000.0,
            "tokenDetails": token_details(input_=6482, cache_read=2560, output=103),
        },
    )
    result = normalize_from_usage_events([event])
    assert result.totals.input_uncached_tokens == 6482
    assert result.totals.input_cached_read_tokens == 2560


def test_token_details_override_unknown_overlap_policy_and_clear_ambiguity():
    event = usage_event(
        inputTokens=9042,
        cacheReadTokens=2560,
        copilotUsage={"totalNanoAiu": 1.0, "tokenDetails": token_details(input_=6482, cache_read=2560)},
    )
    result = normalize_from_usage_events([event], policy=TokenOverlapPolicy.UNKNOWN)
    assert result.totals.input_uncached_tokens == 6482
    assert not any("inputUncachedTokens is null" in a for a in result.ambiguities)


def test_token_details_confirm_input_tokens_include_cache_reads():
    # The partition must reconstruct the reported inputTokens exactly.
    details = token_details(input_=462, cache_read=8704)
    assert sum(d["tokenCount"] for d in details if d["tokenType"] in {"input", "cache_read"}) == 9166


def test_missing_token_details_still_falls_back_to_policy():
    event = usage_event(inputTokens=9042, cacheReadTokens=2560)
    result = normalize_from_usage_events([event], policy=TokenOverlapPolicy.UNKNOWN)
    assert result.totals.input_uncached_tokens is None
    assert any("inputUncachedTokens is null" in a for a in result.ambiguities)


def test_malformed_token_details_are_ignored_not_fatal():
    for bad in ("not-a-list", [None, 5], [{"tokenType": "input"}], [{"tokenCount": 5}]):
        event = usage_event(
            inputTokens=100,
            cacheReadTokens=0,
            copilotUsage={"totalNanoAiu": 1.0, "tokenDetails": bad},
        )
        result = normalize_from_usage_events([event])
        assert result.totals.input_uncached_tokens == 100


# ---------------------------------------------------------------------------
# Premium requests: only user-initiated calls are charged.
# ---------------------------------------------------------------------------


def test_only_user_initiated_calls_count_as_premium_requests():
    # Live shape: two model calls, one user-initiated and one agent tool-loop
    # turn, each with cost 1.0 -- the runtime charges 1.0, not 2.0.
    events = [
        usage_event(initiator="user", cost=1.0),
        usage_event(initiator="agent", cost=1.0),
    ]
    result = normalize_from_usage_events(events)
    assert result.premium_requests_charged == 1.0
    assert result.premium_multiplier_sum_all_calls == 2.0
    assert result.model_requests == 2


def test_premium_reconciles_with_metrics_when_initiator_is_honoured():
    events = [usage_event(initiator="user", cost=1.0), usage_event(initiator="agent", cost=1.0)]
    from_events = normalize_from_usage_events(events)
    from_metrics = normalize_from_metrics(
        metrics_payload(totalPremiumRequestCost=1.0, totalUserRequests=1)
    )
    field = next(
        f
        for f in reconcile(from_events, from_metrics).fields
        if f.field == "premiumRequestsCharged"
    )
    assert field.status == "match"


def test_absent_initiator_preserves_legacy_summing():
    result = normalize_from_usage_events([usage_event(cost=1.0), usage_event(cost=1.0)])
    assert result.premium_requests_charged == 2.0


# ---------------------------------------------------------------------------
# Context window, compaction, and truncation (increment 3)
# ---------------------------------------------------------------------------


def _usage_event(**overrides):
    payload = {
        "model": "gpt-5.4",
        "inputTokens": 1000,
        "outputTokens": 100,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
        "reasoningTokens": 0,
        "maxPromptTokens": 272000,
        "toolTokenCount": 2569,
        "initiator": "user",
    }
    payload.update(overrides)
    return payload


def test_max_prompt_tokens_is_normalized():
    result = normalize_from_usage_events([_usage_event()])
    assert result.context.max_prompt_tokens == 272000


def test_peak_prompt_tokens_is_the_largest_single_call():
    result = normalize_from_usage_events(
        [
            _usage_event(inputTokens=1000),
            _usage_event(inputTokens=9000),
            _usage_event(inputTokens=4000),
        ]
    )
    # Peak, not sum: prompts replace each other, they do not accumulate.
    assert result.context.peak_prompt_tokens == 9000


def test_headroom_ratio_is_reported_both_ways_under_overlap_ambiguity():
    """Whether `inputTokens` includes cache reads changes the real prompt size.

    Under the UNKNOWN overlap policy the harness reports both the runtime's own
    figure and the upper bound, rather than guessing one.
    """
    result = normalize_from_usage_events(
        [_usage_event(inputTokens=8110, cacheReadTokens=7680)]
    )
    context = result.context
    assert context.peak_prompt_tokens == 8110
    assert context.peak_prompt_tokens_cache_excluded == 15790
    assert context.peak_prompt_token_ratio == pytest.approx(8110 / 272000)
    assert context.peak_prompt_token_ratio_upper_bound == pytest.approx(15790 / 272000)
    assert context.min_headroom_ratio == pytest.approx(1 - 8110 / 272000)


def test_tool_token_count_is_recorded():
    result = normalize_from_usage_events([_usage_event(toolTokenCount=2569)])
    assert result.context.peak_tool_token_count == 2569


def test_a_varying_context_limit_is_reported_at_its_tightest():
    """A mid-run switch to a narrower window must not be reported flatteringly."""
    result = normalize_from_usage_events(
        [_usage_event(maxPromptTokens=272000), _usage_event(maxPromptTokens=200000)]
    )
    assert result.context.max_prompt_tokens == 200000
    assert any("varied across calls" in a for a in result.ambiguities)


def test_missing_max_prompt_tokens_is_an_ambiguity_not_a_zero():
    event = _usage_event()
    del event["maxPromptTokens"]
    result = normalize_from_usage_events([event])
    assert result.context.max_prompt_tokens is None
    assert result.context.peak_prompt_token_ratio is None
    assert any("maxPromptTokens" in a for a in result.ambiguities)


def test_context_is_absent_when_no_events_were_seen():
    assert normalize_from_usage_events([]).context is None


# --- compaction --------------------------------------------------------------


def test_no_compaction_events_is_not_evidence_of_no_compaction():
    record = collect_compaction([])
    payload = record.to_json_dict()
    assert payload["compacted"] is False
    # The flag that stops a zero count being read as a measurement.
    assert payload["emissionVerified"] is False


def test_compaction_events_are_counted():
    record = collect_compaction(
        [
            {"type": "session.compaction_start", "data": {"trigger": "manual"}},
            {
                "type": "session.compaction_complete",
                "data": {
                    "success": True,
                    "trigger": "manual",
                    "preCompactionTokens": 190000,
                    "postCompactionTokens": 42000,
                },
            },
        ]
    )
    assert record.compacted is True
    assert record.compaction_starts == 1
    assert record.compaction_completes == 1
    assert record.manual_compactions == 1
    assert record.pre_compaction_tokens == [190000]
    assert record.post_compaction_tokens == [42000]


def test_a_manual_compaction_is_distinguishable_from_an_organic_one():
    """The forced-compaction control must be separable from real compaction.

    The SDK persists an organically triggered compaction without trigger
    attribution, so an absent trigger is the signal for "not harness-induced".
    """
    record = collect_compaction(
        [
            {"type": "session.compaction_start", "data": {"trigger": "manual"}},
            {"type": "session.compaction_start", "data": {}},
        ]
    )
    assert record.manual_compactions == 1
    assert record.unattributed_compactions == 1


def test_a_failed_compaction_is_counted_as_a_failure():
    record = collect_compaction(
        [{"type": "session.compaction_complete", "data": {"success": False}}]
    )
    assert record.compaction_failures == 1


def test_a_missing_success_field_is_a_failure_not_a_success():
    """`success` is required on the wire type, so its absence is malformed."""
    record = collect_compaction(
        [{"type": "session.compaction_complete", "data": {}}]
    )
    assert record.compaction_failures == 1


def test_compaction_tokens_are_their_own_line_and_are_not_folded_in():
    """Whether the compaction call also emits `assistant.usage` is unknown.

    Folding it into the trial totals would double count if it does; ignoring it
    would undercount if it does not. Reporting it separately does neither.
    """
    record = collect_compaction(
        [
            {
                "type": "session.compaction_complete",
                "data": {
                    "success": True,
                    "compactionTokensUsed": {
                        "inputTokens": 185000,
                        "outputTokens": 3000,
                        "cacheReadTokens": 0,
                    },
                },
            }
        ]
    )
    payload = record.to_json_dict()
    assert payload["compactionTokensUsed"]["inputUncachedTokens"] == 185000
    assert payload["compactionTokensUsed"]["outputVisibleTokens"] == 3000
    assert payload["compactionTokensCountedInTrialTotals"] is False


def test_compaction_without_reported_tokens_is_flagged_as_unaccounted():
    record = collect_compaction(
        [{"type": "session.compaction_complete", "data": {"success": True}}]
    )
    assert any("unaccounted" in note for note in record.notes)


def test_truncation_is_counted_separately_from_compaction():
    record = collect_compaction(
        [
            {
                "type": "session.truncation",
                "data": {"tokensRemovedDuringTruncation": 1200},
            }
        ]
    )
    assert record.truncated is True
    assert record.compacted is False
    assert record.tokens_removed_by_truncation == 1200


def test_unrelated_events_are_ignored():
    record = collect_compaction(
        [{"type": "assistant.usage", "data": {"inputTokens": 1}}]
    )
    assert record.compacted is False
    assert record.truncations == 0
