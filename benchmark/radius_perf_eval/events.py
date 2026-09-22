"""Append-only session event capture with a monotonic clock.

The experiment plan requires an ordered JSONL event log whose ordering is
preserved and whose durations come from a monotonic clock, never wall time
(``docs/specs/copilot-radius-experiment-plan.md``, "Wall-clock timing" and
"Artifacts and run record").

Design constraints:

* Every record carries a strictly increasing ``seq`` assigned under a lock, so
  ordering survives concurrent producers and equal timestamps.
* Durations derive from :func:`time.monotonic_ns`. UTC timestamps are recorded
  alongside for human correlation only.
* The log is append-only: records are flushed as they arrive so a crashed or
  budget-terminated run still yields a usable prefix.
* ``assistant.usage`` events are ephemeral (not replayed on resume), so the
  recorder is the only durable copy. It stores them verbatim.
"""

from __future__ import annotations

import json
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

__all__ = [
    "EventRecord",
    "EventRecorder",
    "MonotonicClock",
    "ToolCallTimeline",
    "read_events",
]


class MonotonicClock:
    """A monotonic clock anchored to a single UTC reference instant.

    ``time.monotonic_ns`` has an undefined epoch, so an anchor is captured once
    at construction. All derived UTC timestamps are computed from the anchor
    plus the monotonic delta, which keeps timestamps internally consistent even
    if the host wall clock is stepped mid-run.
    """

    def __init__(self) -> None:
        self._start_monotonic_ns = time.monotonic_ns()
        self._anchor_utc = datetime.now(timezone.utc)

    @property
    def anchor_utc(self) -> datetime:
        return self._anchor_utc

    @property
    def started_at_utc(self) -> str:
        return self._anchor_utc.isoformat().replace("+00:00", "Z")

    def elapsed_ns(self) -> int:
        return time.monotonic_ns() - self._start_monotonic_ns

    def elapsed_ms(self) -> float:
        return self.elapsed_ns() / 1_000_000

    def now_utc_iso(self) -> str:
        micros = self.elapsed_ns() / 1_000
        stamp = self._anchor_utc.timestamp() + micros / 1_000_000
        return datetime.fromtimestamp(stamp, tz=timezone.utc).isoformat()


@dataclass(frozen=True)
class EventRecord:
    """One line of ``events.jsonl``."""

    seq: int
    elapsed_ns: int
    recorded_at: str
    source: str
    type: str
    payload: Mapping[str, Any]

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "seq": self.seq,
            "elapsedNs": self.elapsed_ns,
            "elapsedMs": self.elapsed_ns / 1_000_000,
            "recordedAt": self.recorded_at,
            "source": self.source,
            "type": self.type,
            "payload": self.payload,
        }


def _json_default(value: Any) -> Any:
    """Best-effort JSON coercion that never drops data silently."""
    if isinstance(value, datetime):
        return value.isoformat()
    if hasattr(value, "to_dict"):
        try:
            return value.to_dict()
        except Exception:  # pragma: no cover - defensive
            pass
    if hasattr(value, "value") and type(value).__name__.endswith(("Enum", "Type")):
        return value.value
    if isinstance(value, (set, frozenset, tuple)):
        return list(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return repr(value)


@dataclass
class _OpenToolCall:
    tool_call_id: str
    tool_name: str | None
    started_ms: float
    agent_id: str | None


@dataclass
class ToolCallTimeline:
    """Per-tool-call durations derived from the ordered event stream.

    "Logical tool calls" are distinct ``toolCallId`` values. "Tool attempts"
    count every ``tool.execution_start`` observed, so a retried call inflates
    attempts but not logical calls, matching the "Tools and treatment use"
    measures in the experiment plan.
    """

    logical_tool_calls: set[str] = field(default_factory=set)
    tool_attempts: int = 0
    completed: list[dict[str, Any]] = field(default_factory=list)
    orphan_completions: list[str] = field(default_factory=list)
    _open: dict[str, _OpenToolCall] = field(default_factory=dict, repr=False)
    _names: dict[str, str | None] = field(default_factory=dict, repr=False)
    _anonymous_attempts: int = field(default=0, repr=False)

    def start(
        self,
        tool_call_id: str | None,
        tool_name: str | None = None,
        *,
        at_ms: float,
        agent_id: str | None = None,
    ) -> None:
        self.tool_attempts += 1
        if tool_call_id is None:
            # Never lose an attempt just because the runtime omitted an id.
            self._anonymous_attempts += 1
            return
        self.logical_tool_calls.add(tool_call_id)
        self._names.setdefault(tool_call_id, tool_name)
        self._open[tool_call_id] = _OpenToolCall(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            started_ms=at_ms,
            agent_id=agent_id,
        )

    def complete(
        self,
        tool_call_id: str | None,
        tool_name: str | None = None,
        *,
        at_ms: float,
        success: bool | None = None,
        agent_id: str | None = None,
    ) -> None:
        open_call = self._open.pop(tool_call_id, None) if tool_call_id else None
        if tool_call_id is not None:
            if tool_call_id not in self.logical_tool_calls:
                # A completion with no observed start still represents a real
                # tool call; count it rather than silently dropping it.
                self.orphan_completions.append(tool_call_id)
                self.logical_tool_calls.add(tool_call_id)
            if tool_name:
                self._names.setdefault(tool_call_id, tool_name)
        resolved_name = tool_name or (open_call.tool_name if open_call else None)
        self.completed.append(
            {
                "toolCallId": tool_call_id,
                "toolName": resolved_name,
                "agentId": agent_id or (open_call.agent_id if open_call else None),
                "startedMs": open_call.started_ms if open_call else None,
                "completedMs": at_ms,
                "durationMs": (at_ms - open_call.started_ms) if open_call else None,
                "success": success,
            }
        )

    def summary(self) -> dict[str, Any]:
        by_tool: dict[str, int] = {}
        for call_id in self.logical_tool_calls:
            name = self._names.get(call_id) or "<unknown>"
            by_tool[name] = by_tool.get(name, 0) + 1
        return {
            "logicalToolCalls": len(self.logical_tool_calls),
            "toolAttempts": self.tool_attempts,
            "anonymousAttempts": self._anonymous_attempts,
            "incomplete": sorted(self._open),
            "orphanCompletions": list(self.orphan_completions),
            "byTool": by_tool,
            "durationsMs": [
                c for c in self.completed if c["durationMs"] is not None
            ],
            "calls": list(self.completed),
        }


class EventRecorder:
    """Append-only JSONL writer with a monotonic sequence counter."""

    def __init__(self, path: str | Path, *, clock: MonotonicClock | None = None) -> None:
        self._path = Path(path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self.clock = clock or MonotonicClock()
        self._lock = threading.Lock()
        self._seq = 0
        self._handle = self._path.open("a", encoding="utf-8")
        self._closed = False

    @property
    def path(self) -> Path:
        return self._path

    @property
    def count(self) -> int:
        with self._lock:
            return self._seq

    def record(self, source: str, event_type: str, payload: Mapping[str, Any]) -> EventRecord:
        """Append one record. Safe to call from any thread."""
        with self._lock:
            if self._closed:
                raise RuntimeError("EventRecorder is closed")
            seq = self._seq
            self._seq += 1
            record = EventRecord(
                seq=seq,
                elapsed_ns=self.clock.elapsed_ns(),
                recorded_at=self.clock.now_utc_iso(),
                source=source,
                type=event_type,
                payload=payload,
            )
            self._handle.write(
                json.dumps(record.to_json_dict(), default=_json_default, sort_keys=True) + "\n"
            )
            self._handle.flush()
        return record

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            self._handle.close()

    def __enter__(self) -> EventRecorder:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


def read_events(path: str | Path) -> list[dict[str, Any]]:
    """Read back an ``events.jsonl`` file in recorded order."""
    rows: list[dict[str, Any]] = []
    with Path(path).open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line:
                rows.append(json.loads(line))
    return rows
