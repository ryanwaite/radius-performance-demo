"""Unit tests for the append-only event recorder and tool-call timeline."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone

import pytest

from radius_perf_eval.events import (
    EventRecorder,
    MonotonicClock,
    ToolCallTimeline,
    read_events,
)


def test_monotonic_clock_never_decreases():
    clock = MonotonicClock()
    samples = [clock.elapsed_ms() for _ in range(200)]
    assert samples == sorted(samples)
    assert samples[0] >= 0.0


def test_monotonic_clock_wall_clock_anchor_is_utc():
    clock = MonotonicClock()
    anchor = datetime.fromisoformat(clock.started_at_utc.replace("Z", "+00:00"))
    assert anchor.tzinfo is not None
    assert anchor.utcoffset() == timezone.utc.utcoffset(None)


def test_recorder_writes_append_only_jsonl_in_order(tmp_path):
    path = tmp_path / "events.jsonl"
    recorder = EventRecorder(path)
    for i in range(10):
        recorder.record("test", f"event.{i}", {"i": i})
    recorder.close()

    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert len(rows) == 10
    assert [r["seq"] for r in rows] == list(range(10))
    assert [r["type"] for r in rows] == [f"event.{i}" for i in range(10)]
    assert [r["elapsedMs"] for r in rows] == sorted(r["elapsedMs"] for r in rows)


def test_recorder_flushes_each_record_so_a_crash_preserves_history(tmp_path):
    path = tmp_path / "events.jsonl"
    recorder = EventRecorder(path)
    recorder.record("test", "first", {})
    # Read before close: the record must already be durable on disk.
    assert len(path.read_text().splitlines()) == 1
    recorder.close()


def test_recorder_sequence_is_unique_under_concurrency(tmp_path):
    path = tmp_path / "events.jsonl"
    recorder = EventRecorder(path)

    def worker(n: int) -> None:
        for i in range(50):
            recorder.record("test", "concurrent", {"worker": n, "i": i})

    threads = [threading.Thread(target=worker, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    recorder.close()

    rows = read_events(path)
    seqs = [r["seq"] for r in rows]
    assert len(seqs) == 200
    assert len(set(seqs)) == 200
    assert seqs == sorted(seqs)


def test_recorder_coerces_non_serializable_payloads(tmp_path):
    path = tmp_path / "events.jsonl"
    recorder = EventRecorder(path)

    class Opaque:
        def __repr__(self) -> str:
            return "<opaque>"

    recorder.record("test", "weird", {"obj": Opaque(), "when": datetime(2026, 1, 1)})
    recorder.close()
    rows = read_events(path)
    assert rows[0]["payload"]["obj"] == "<opaque>"
    assert "2026" in rows[0]["payload"]["when"]


def test_recorder_rejects_writes_after_close(tmp_path):
    recorder = EventRecorder(tmp_path / "events.jsonl")
    recorder.close()
    with pytest.raises(RuntimeError):
        recorder.record("test", "late", {})


# ---------------------------------------------------------------------------
# Tool call timeline
# ---------------------------------------------------------------------------


def test_logical_tool_calls_count_distinct_ids():
    timeline = ToolCallTimeline()
    timeline.start("call-1", "bash", at_ms=0.0)
    timeline.complete("call-1", at_ms=100.0, success=True)
    timeline.start("call-2", "str_replace_editor", at_ms=150.0)
    timeline.complete("call-2", at_ms=200.0, success=True)

    summary = timeline.summary()
    assert summary["logicalToolCalls"] == 2
    assert summary["toolAttempts"] == 2


def test_retried_tool_call_is_one_logical_call_but_many_attempts():
    timeline = ToolCallTimeline()
    timeline.start("call-1", "bash", at_ms=0.0)
    timeline.complete("call-1", at_ms=50.0, success=False)
    timeline.start("call-1", "bash", at_ms=60.0)
    timeline.complete("call-1", at_ms=110.0, success=True)

    summary = timeline.summary()
    assert summary["logicalToolCalls"] == 1
    assert summary["toolAttempts"] == 2


def test_tool_durations_are_recorded_per_call():
    timeline = ToolCallTimeline()
    timeline.start("call-1", "bash", at_ms=10.0)
    timeline.complete("call-1", at_ms=60.0, success=True)
    summary = timeline.summary()
    assert summary["durationsMs"][0]["durationMs"] == pytest.approx(50.0)
    assert summary["durationsMs"][0]["toolName"] == "bash"


def test_incomplete_tool_call_is_reported_not_silently_dropped():
    timeline = ToolCallTimeline()
    timeline.start("call-1", "bash", at_ms=0.0)
    summary = timeline.summary()
    assert summary["logicalToolCalls"] == 1
    assert summary["incomplete"] == ["call-1"]


def test_completion_without_start_is_still_counted():
    # Defensive: never lose a tool call because the start event was missed.
    timeline = ToolCallTimeline()
    timeline.complete("orphan", at_ms=10.0, success=True)
    summary = timeline.summary()
    assert summary["logicalToolCalls"] == 1
    assert "orphan" in summary["orphanCompletions"]


def test_by_tool_breakdown():
    timeline = ToolCallTimeline()
    timeline.start("a", "bash", at_ms=0.0)
    timeline.complete("a", at_ms=10.0, success=True)
    timeline.start("b", "bash", at_ms=20.0)
    timeline.complete("b", at_ms=25.0, success=True)
    timeline.start("c", "view", at_ms=30.0)
    timeline.complete("c", at_ms=31.0, success=True)

    summary = timeline.summary()
    assert summary["byTool"]["bash"] == 2
    assert summary["byTool"]["view"] == 1
