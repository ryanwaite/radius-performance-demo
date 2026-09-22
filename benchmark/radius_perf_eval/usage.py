"""Raw and normalized Copilot usage accounting.

The experiment plan (``docs/specs/copilot-radius-experiment-plan.md``,
"Copilot usage") imposes four hard rules that this module implements:

1. Retain raw provider payloads verbatim alongside any normalized record.
2. A field a provider does not report is ``null``, never ``0``.
3. Never sum overlapping fields.
4. Reconcile the accumulated-usage RPC against the summed per-call events, and
   keep a mismatch as an artifact instead of overwriting either source.

Known overlap semantics for the GitHub Copilot SDK 1.0.14 wire schema:

* ``reasoningTokens`` is documented as a **subset of** ``outputTokens``, so
  visible output is ``outputTokens - reasoningTokens``.
* The relationship between ``inputTokens`` and ``cacheReadTokens`` is **not**
  documented. Rather than guess, :class:`TokenOverlapPolicy` defaults to
  ``UNKNOWN``, which yields ``null`` for ``inputUncachedTokens`` whenever a
  non-zero cache read is reported, and flags the ambiguity. A pilot that
  establishes the true semantics can switch the policy without changing
  callers.

This module deliberately has no SDK import so it can be unit tested against
recorded payloads without burning model calls.
"""

from __future__ import annotations

import enum
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

__all__ = [
    "NANO_AIU_PER_AI_CREDIT",
    "FieldReconciliation",
    "NormalizedUsage",
    "Reconciliation",
    "TokenOverlapPolicy",
    "UsageTotals",
    "build_normalized_record",
    "normalize_from_metrics",
    "normalize_from_usage_events",
    "reconcile",
]

#: ``totalNanoAiu`` is expressed in nano-AI units. The SDK documentation notes
#: the SI ``nano`` prefix as a convenience conversion and tells integrators to
#: confirm against GitHub billing before surfacing currency-like values, so the
#: divisor lives here as a named, auditable constant.
NANO_AIU_PER_AI_CREDIT = 1_000_000_000.0

#: Floating-point summation noise floor used when reconciling float-valued
#: fields. Integer token counts are always compared exactly.
_FLOAT_SUMMATION_EPSILON = 1e-9


class TokenOverlapPolicy(enum.Enum):
    """How ``inputTokens`` relates to ``cacheReadTokens`` for a given provider.

    This policy is only consulted when the call has no
    ``copilotUsage.tokenDetails`` breakdown. Where that breakdown is present it
    is authoritative and the policy is bypassed entirely.
    """

    UNKNOWN = "unknown"
    """Relationship undocumented. Fail closed: emit ``null`` rather than guess."""

    INPUT_EXCLUDES_CACHE_READS = "input-excludes-cache-reads"
    """``inputTokens`` already counts only uncached prompt tokens."""

    INPUT_INCLUDES_CACHE_READS = "input-includes-cache-reads"
    """``inputTokens`` is a total; uncached input is the difference."""


def _as_int(value: Any) -> int | None:
    """Coerce to ``int``, preserving ``None``. Booleans are rejected."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    return None


def _add(left: int | None, right: int | None) -> int | None:
    """Null-preserving addition.

    ``None + None`` stays ``None`` so an unreported field never collapses to
    ``0``. ``None + 5`` yields ``5`` because a per-call absence inside an
    otherwise-reporting stream is a genuine zero contribution for that call.
    """
    if left is None and right is None:
        return None
    return (left or 0) + (right or 0)


def _add_float(left: float | None, right: float | None) -> float | None:
    if left is None and right is None:
        return None
    return (left or 0.0) + (right or 0.0)


@dataclass
class UsageTotals:
    """Non-overlapping normalized token totals. Every field is nullable."""

    input_uncached_tokens: int | None = None
    input_cached_read_tokens: int | None = None
    input_cache_write_tokens: int | None = None
    output_visible_tokens: int | None = None
    output_reasoning_tokens: int | None = None
    provider_reported_total_tokens: int | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "inputUncachedTokens": self.input_uncached_tokens,
            "inputCachedReadTokens": self.input_cached_read_tokens,
            "inputCacheWriteTokens": self.input_cache_write_tokens,
            "outputVisibleTokens": self.output_visible_tokens,
            "outputReasoningTokens": self.output_reasoning_tokens,
            "providerReportedTotalTokens": self.provider_reported_total_tokens,
        }


@dataclass
class NormalizedUsage:
    """Normalized usage derived from exactly one source.

    ``source`` is either ``"assistant.usage-events"`` or
    ``"session.usage.getMetrics"``. The two are never merged; they are
    reconciled.
    """

    source: str
    totals: UsageTotals = field(default_factory=UsageTotals)
    model_requests: int | None = None
    ai_credits: float | None = None
    premium_requests_charged: float | None = None
    premium_multiplier_sum_all_calls: float | None = None
    api_duration_ms: int | None = None
    models_seen: list[str] = field(default_factory=list)
    ambiguities: list[str] = field(default_factory=list)
    raw_input_tokens: int | None = None
    raw_output_tokens: int | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "source": self.source,
            "usage": self.totals.to_json_dict(),
            "modelRequests": self.model_requests,
            "aiCredits": self.ai_credits,
            "premiumRequestsCharged": self.premium_requests_charged,
            "premiumMultiplierSumAllCalls": self.premium_multiplier_sum_all_calls,
            "apiDurationMs": self.api_duration_ms,
            "modelsSeen": list(self.models_seen),
            "ambiguities": list(self.ambiguities),
            "providerReportedInputTokens": self.raw_input_tokens,
            "providerReportedOutputTokens": self.raw_output_tokens,
        }


def _split_input(
    input_tokens: int | None,
    cache_read_tokens: int | None,
    policy: TokenOverlapPolicy,
    ambiguities: list[str],
) -> int | None:
    """Derive uncached input tokens without ever summing overlapping fields."""
    if input_tokens is None:
        return None
    if not cache_read_tokens:
        # No cache read reported (absent or zero): no overlap is possible.
        return input_tokens
    if policy is TokenOverlapPolicy.INPUT_EXCLUDES_CACHE_READS:
        return input_tokens
    if policy is TokenOverlapPolicy.INPUT_INCLUDES_CACHE_READS:
        return max(input_tokens - cache_read_tokens, 0)
    ambiguities.append(
        "inputUncachedTokens is null: cacheReadTokens is non-zero and the "
        "inputTokens/cacheReadTokens overlap is undocumented under "
        f"TokenOverlapPolicy.{policy.name}"
    )
    return None


def _split_output(
    output_tokens: int | None,
    reasoning_tokens: int | None,
) -> int | None:
    """``reasoningTokens`` is a documented subset of ``outputTokens``."""
    if output_tokens is None:
        return None
    if reasoning_tokens is None:
        return output_tokens
    return max(output_tokens - reasoning_tokens, 0)


def _token_details(data: Mapping[str, Any]) -> dict[str, int]:
    """Extract the billing token breakdown from ``copilotUsage.tokenDetails``.

    This breakdown is what the provider actually bills, and it partitions the
    call's tokens into non-overlapping buckets (``input``, ``cache_read``,
    ``cache_write``, ``output``). When present it is authoritative and removes
    the need to guess at field overlap.
    """
    copilot_usage = data.get("copilotUsage")
    if not isinstance(copilot_usage, Mapping):
        return {}
    details = copilot_usage.get("tokenDetails")
    if not isinstance(details, Sequence) or isinstance(details, (str, bytes)):
        return {}
    out: dict[str, int] = {}
    for entry in details:
        if not isinstance(entry, Mapping):
            continue
        token_type = entry.get("tokenType")
        count = _as_int(entry.get("tokenCount"))
        if isinstance(token_type, str) and count is not None:
            out[token_type] = out.get(token_type, 0) + count
    return out


def normalize_from_usage_events(
    events: Iterable[Mapping[str, Any]],
    *,
    policy: TokenOverlapPolicy = TokenOverlapPolicy.UNKNOWN,
) -> NormalizedUsage:
    """Aggregate raw ``assistant.usage`` payloads into normalized totals.

    Accepts the wire-shaped ``data`` object of each ``assistant.usage`` event.
    The SDK emits one such event per model API call **including sub-agent
    calls**, so the aggregate already covers subagents and must not be added to
    any separate per-agent total.

    Two accounting subtleties, both confirmed against live CLI 1.0.83 payloads:

    * ``copilotUsage.tokenDetails`` carries the billed, non-overlapping token
      partition. Where it is present, ``input`` is the genuinely uncached
      prompt token count and no overlap guess is needed.
    * ``cost`` is a per-call premium-request multiplier, but only calls with
      ``initiator == "user"`` are charged as premium requests. Summing ``cost``
      across every call overcounts; the runtime's own
      ``totalPremiumRequestCost`` counts user-initiated calls only.
    """
    ambiguities: list[str] = []
    totals = UsageTotals()
    raw_input: int | None = None
    raw_output: int | None = None
    ai_credits: float | None = None
    premium: float | None = None
    premium_all_calls: float | None = None
    duration_ms: float | None = None
    models: list[str] = []
    count = 0
    used_token_details = False

    for data in events:
        count += 1
        model = data.get("model")
        if isinstance(model, str) and model not in models:
            models.append(model)

        input_tokens = _as_int(data.get("inputTokens"))
        output_tokens = _as_int(data.get("outputTokens"))
        cache_read = _as_int(data.get("cacheReadTokens"))
        cache_write = _as_int(data.get("cacheWriteTokens"))
        reasoning = _as_int(data.get("reasoningTokens"))

        raw_input = _add(raw_input, input_tokens)
        raw_output = _add(raw_output, output_tokens)

        details = _token_details(data)
        if "input" in details:
            # Authoritative: the billed uncached prompt tokens for this call.
            used_token_details = True
            uncached_input = details["input"]
        else:
            uncached_input = _split_input(input_tokens, cache_read, policy, ambiguities)

        totals.input_uncached_tokens = _add(totals.input_uncached_tokens, uncached_input)
        totals.input_cached_read_tokens = _add(
            totals.input_cached_read_tokens,
            details.get("cache_read", cache_read),
        )
        totals.input_cache_write_tokens = _add(
            totals.input_cache_write_tokens,
            details.get("cache_write", cache_write),
        )
        totals.output_visible_tokens = _add(
            totals.output_visible_tokens, _split_output(output_tokens, reasoning)
        )
        totals.output_reasoning_tokens = _add(totals.output_reasoning_tokens, reasoning)

        copilot_usage = data.get("copilotUsage")
        if isinstance(copilot_usage, Mapping):
            nano = _as_float(copilot_usage.get("totalNanoAiu"))
            if nano is not None:
                ai_credits = _add_float(ai_credits, nano / NANO_AIU_PER_AI_CREDIT)

        call_cost = _as_float(data.get("cost"))
        premium_all_calls = _add_float(premium_all_calls, call_cost)
        # Only a user-initiated call is charged as a premium request; an
        # agent-initiated tool-loop turn is not. Summing every call's `cost`
        # double counts against the runtime's own accumulation.
        if data.get("initiator") == "user":
            premium = _add_float(premium, call_cost)
        elif "initiator" not in data:
            premium = _add_float(premium, call_cost)

        duration_ms = _add_float(duration_ms, _as_float(data.get("duration")))

    if used_token_details:
        # Any overlap warning raised before token details were seen is moot.
        ambiguities = [a for a in ambiguities if "inputUncachedTokens is null" not in a]

    if input_tokens_unreported := (count > 0 and raw_input is None):
        ambiguities.append("no assistant.usage event reported inputTokens")
    if count > 0 and raw_output is None and not input_tokens_unreported:
        ambiguities.append("no assistant.usage event reported outputTokens")

    # Deliberately left null: the SDK exposes no provider-reported grand total
    # on assistant.usage, and deriving one would sum overlapping fields.
    totals.provider_reported_total_tokens = None
    if count > 0:
        ambiguities.append(
            "providerReportedTotalTokens is null: assistant.usage exposes no "
            "provider grand total, and deriving one would sum overlapping fields"
        )

    return NormalizedUsage(
        source="assistant.usage-events",
        totals=totals,
        model_requests=count,
        ai_credits=ai_credits,
        premium_requests_charged=premium,
        premium_multiplier_sum_all_calls=premium_all_calls,
        api_duration_ms=int(duration_ms) if duration_ms is not None else None,
        models_seen=models,
        ambiguities=_dedupe(ambiguities),
        raw_input_tokens=raw_input,
        raw_output_tokens=raw_output,
    )


def normalize_from_metrics(
    metrics: Mapping[str, Any],
    *,
    policy: TokenOverlapPolicy = TokenOverlapPolicy.UNKNOWN,
) -> NormalizedUsage:
    """Normalize a raw ``session.usage.getMetrics`` response.

    Only ``modelMetrics`` contributes to totals. ``agentMetrics`` is a
    *breakdown* of the same spend keyed by agent instance (the main
    conversation uses the key ``main``); adding both would double count.
    """
    ambiguities: list[str] = []
    totals = UsageTotals()
    raw_input: int | None = None
    raw_output: int | None = None
    requests: int | None = None
    models: list[str] = []

    model_metrics = metrics.get("modelMetrics")
    if isinstance(model_metrics, Mapping):
        for model_id, metric in model_metrics.items():
            if not isinstance(metric, Mapping):
                continue
            models.append(str(model_id))
            usage = metric.get("usage") if isinstance(metric.get("usage"), Mapping) else {}
            input_tokens = _as_int(usage.get("inputTokens"))
            output_tokens = _as_int(usage.get("outputTokens"))
            cache_read = _as_int(usage.get("cacheReadTokens"))
            cache_write = _as_int(usage.get("cacheWriteTokens"))
            reasoning = _as_int(usage.get("reasoningTokens"))

            raw_input = _add(raw_input, input_tokens)
            raw_output = _add(raw_output, output_tokens)
            totals.input_uncached_tokens = _add(
                totals.input_uncached_tokens,
                _split_input(input_tokens, cache_read, policy, ambiguities),
            )
            totals.input_cached_read_tokens = _add(totals.input_cached_read_tokens, cache_read)
            totals.input_cache_write_tokens = _add(totals.input_cache_write_tokens, cache_write)
            totals.output_visible_tokens = _add(
                totals.output_visible_tokens, _split_output(output_tokens, reasoning)
            )
            totals.output_reasoning_tokens = _add(totals.output_reasoning_tokens, reasoning)

            reqs = metric.get("requests")
            if isinstance(reqs, Mapping):
                requests = _add(requests, _as_int(reqs.get("count")))

    nano = _as_float(metrics.get("totalNanoAiu"))
    ai_credits = nano / NANO_AIU_PER_AI_CREDIT if nano is not None else None
    if nano is None:
        ambiguities.append("session metrics did not report totalNanoAiu")

    totals.provider_reported_total_tokens = None
    ambiguities.append(
        "providerReportedTotalTokens is null: session.usage.getMetrics exposes "
        "no provider grand total"
    )

    if requests is None:
        # `totalUserRequests` counts only user-initiated requests, so it is not
        # interchangeable with the per-model request count that includes
        # tool-loop turns. Record it, but do not silently substitute it.
        ambiguities.append(
            "modelMetrics reported no request count; totalUserRequests counts "
            "only user-initiated requests and is not a substitute"
        )

    return NormalizedUsage(
        source="session.usage.getMetrics",
        totals=totals,
        model_requests=requests,
        ai_credits=ai_credits,
        premium_requests_charged=_as_float(metrics.get("totalPremiumRequestCost")),
        api_duration_ms=_as_int(metrics.get("totalApiDurationMs")),
        models_seen=models,
        ambiguities=_dedupe(ambiguities),
        raw_input_tokens=raw_input,
        raw_output_tokens=raw_output,
    )


def _dedupe(values: Sequence[str]) -> list[str]:
    seen: list[str] = []
    for value in values:
        if value not in seen:
            seen.append(value)
    return seen


@dataclass(frozen=True)
class FieldReconciliation:
    field: str
    from_events: float | int | None
    from_metrics: float | int | None
    status: str
    delta: float | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Reconciliation:
    """Comparison of two independent usage sources. Never a merge."""

    fields: list[FieldReconciliation] = field(default_factory=list)

    @property
    def mismatches(self) -> list[FieldReconciliation]:
        return [f for f in self.fields if f.status == "mismatch"]

    @property
    def agreed(self) -> bool:
        return not self.mismatches

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "agreed": self.agreed,
            "mismatchCount": len(self.mismatches),
            "fields": [f.to_json_dict() for f in self.fields],
        }


_RECONCILED_FIELDS: tuple[tuple[str, str], ...] = (
    ("inputUncachedTokens", "input_uncached_tokens"),
    ("inputCachedReadTokens", "input_cached_read_tokens"),
    ("inputCacheWriteTokens", "input_cache_write_tokens"),
    ("outputVisibleTokens", "output_visible_tokens"),
    ("outputReasoningTokens", "output_reasoning_tokens"),
)


def reconcile(
    from_events: NormalizedUsage,
    from_metrics: NormalizedUsage | None,
    *,
    relative_tolerance: float = 0.0,
) -> Reconciliation:
    """Cross-check the per-call event stream against the accumulated RPC.

    A mismatch is recorded, not resolved. ``session.usage.getMetrics`` is an
    experimental RPC, so neither source is treated as authoritative.
    """
    result = Reconciliation()
    if from_metrics is None:
        for name, _ in _RECONCILED_FIELDS:
            value = getattr(from_events.totals, dict(_RECONCILED_FIELDS)[name])
            result.fields.append(
                FieldReconciliation(
                    field=name,
                    from_events=value,
                    from_metrics=None,
                    status="metrics-unavailable",
                )
            )
        return result

    pairs: list[tuple[str, Any, Any]] = [
        (name, getattr(from_events.totals, attr), getattr(from_metrics.totals, attr))
        for name, attr in _RECONCILED_FIELDS
    ]
    pairs.append(("modelRequests", from_events.model_requests, from_metrics.model_requests))
    pairs.append(("aiCredits", from_events.ai_credits, from_metrics.ai_credits))
    pairs.append(
        (
            "premiumRequestsCharged",
            from_events.premium_requests_charged,
            from_metrics.premium_requests_charged,
        )
    )

    for name, left, right in pairs:
        result.fields.append(_compare(name, left, right, relative_tolerance))
    return result


def _compare(
    name: str,
    left: float | int | None,
    right: float | int | None,
    relative_tolerance: float,
) -> FieldReconciliation:
    if left is None and right is None:
        return FieldReconciliation(name, None, None, "both-null")
    if left is None:
        return FieldReconciliation(name, None, right, "events-null")
    if right is None:
        return FieldReconciliation(name, left, None, "metrics-null")
    delta = float(right) - float(left)
    scale = max(abs(float(left)), abs(float(right)), 1.0)
    tolerance = relative_tolerance
    if isinstance(left, float) or isinstance(right, float):
        # AI credits are accumulated by summing nano-AIU quotients, so the two
        # sources can differ by IEEE-754 rounding noise alone. Token counts are
        # integers and stay exact.
        tolerance = max(tolerance, _FLOAT_SUMMATION_EPSILON)
    status = "match" if abs(delta) <= tolerance * scale else "mismatch"
    return FieldReconciliation(name, left, right, status, delta)


def build_normalized_record(
    *,
    model: str,
    agent_wall_clock_ms: float,
    logical_tool_calls: int,
    tool_attempts: int,
    from_events: NormalizedUsage,
    from_metrics: NormalizedUsage | None,
    reconciliation: Reconciliation,
    premium_request_multiplier: float | None = None,
    pricing_version: str | None = None,
    runtime: str = "github-copilot-sdk",
) -> dict[str, Any]:
    """Build the run record's normalized usage block.

    The summed ``assistant.usage`` event stream is preferred for token totals.
    It is the per-call ground truth and carries the provider's own billed token
    partition (``copilotUsage.tokenDetails``), which yields a real
    ``inputUncachedTokens`` figure under prompt caching.
    ``session.usage.getMetrics`` reports no such breakdown and is an
    experimental RPC, so it serves as the independent cross-check.

    Preference never means merge: a single source supplies every token field,
    the choice is recorded in ``usageSource``, and any disagreement is surfaced
    through ``reconciliation`` rather than being silently resolved.
    """
    primary = from_events

    return {
        "runtime": runtime,
        "model": model,
        "agentWallClockMs": agent_wall_clock_ms,
        "modelRequests": primary.model_requests,
        "logicalToolCalls": logical_tool_calls,
        "toolAttempts": tool_attempts,
        "usage": primary.totals.to_json_dict(),
        "credits": {
            "aiCredits": primary.ai_credits,
            "premiumRequestMultiplier": premium_request_multiplier,
        },
        "cost": {
            # Copilot reports AI credits and premium-request multipliers, not
            # money. Inventing a USD figure here would be a fabricated measure.
            "actualUSD": None,
            "estimatedUSD": None,
            "pricingVersion": pricing_version,
        },
        "usageSource": primary.source,
        "premiumRequestsCharged": primary.premium_requests_charged,
        "modelApiDurationMs": primary.api_duration_ms,
        "ambiguities": primary.ambiguities,
        "sources": {
            "assistantUsageEvents": from_events.to_json_dict(),
            "sessionUsageMetrics": (
                from_metrics.to_json_dict() if from_metrics is not None else None
            ),
        },
        "reconciliation": reconciliation.to_json_dict(),
    }
