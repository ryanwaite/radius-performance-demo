"""Cold Copilot SDK session lifecycle, isolation, and budget enforcement.

Implements the harness requirements in
``docs/specs/copilot-radius-experiment-plan.md``:

* one fresh cold session per trial with an explicit pinned model (no ``auto``
  routing) -- "Variables held constant";
* memory off, no subagents -- "Parallelism: one agent, no fleet or subagents";
* file and tool access screened against the assigned temporary workspace --
  "Isolation exit criteria";
* wall-clock / model-call / tool-call / AI-credit budgets that terminate the
  session -- "Safety and cost controls";
* every session event captured in order with monotonic timing.

Isolation here is **advisory and in-process**, enforced by a fail-closed
permission handler inside this harness. There is no OS or process boundary:
the SDK spawns its pinned CLI as a host child process, and the workspace is an
ordinary host temporary directory. Anything the handler cannot prove is inside
the workspace is denied, which is a real and useful layer -- but it is the
harness declining a request, not the kernel refusing an operation.

Confinement would require an OS boundary around the agent process, which this
harness does not establish. The Compose increment does not supply it: those
containers bound the application under test, while the agent remains a host
process outside them.

The runtime sandbox that would supply it is reachable only *after* the session
exists, via the experimental ``session.options.update`` -- leaving a window
between session start and that call in which no policy is in force, which any
runner must close or account for. A spike on SDK 1.0.13 / CLI 1.0.83 with one
model denied every escape that executed; this harness does not enable it yet.

The same capability is absent from the session-creation API: the CLI wire
protocol defines a real OS-level ``SandboxConfig`` (an ``enabled`` flag,
``userPolicy.filesystem`` read-only and read-write path lists, a fail-closed
``allowBypass``, and sandboxed MCP/LSP subprocesses). None of it is exposed on
``CopilotClient.create_session`` in the pinned SDK 1.0.13, whose ~80
parameters include nothing sandbox-related. So the gap here is an SDK surface
gap, not a missing runtime feature.
"""

from __future__ import annotations

import asyncio
import re
import shutil
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .usage import CONTEXT_EVENT_TYPES
from .events import EventRecorder, ToolCallTimeline

__all__ = [
    "RUNTIME_MIN_AI_CREDITS",
    "SUBAGENT_TOOLS",
    "BUILTIN_AGENTS",
    "BudgetExceeded",
    "IsolationPolicy",
    "IsolationViolation",
    "SessionBudget",
    "SessionOutcome",
    "SpikeSession",
    "TemporaryWorkspace",
    "fetch_model_catalog",
    "isolated_session_kwargs",
]

#: Built-in tools that spawn or talk to subagents. The benchmark runs exactly
#: one agent, so these are excluded from every scored session.
SUBAGENT_TOOLS: tuple[str, ...] = (
    "task",
    "read_agent",
    "write_agent",
    "list_agents",
)

#: The runtime rejects ``session_limits.max_ai_credits`` below this floor
#: ("Minimum session limit is 30 AI credits"). A per-trial benchmark budget is
#: far smaller than 30 credits, so the runtime limit can only ever be a coarse
#: backstop: the harness-side :class:`SessionBudget` is what actually enforces
#: the trial cap.
RUNTIME_MIN_AI_CREDITS = 30.0

#: The plan's scored-trial budget (plan defaults table): 30 minutes wall clock
#: and 100 tool calls, whichever comes first, at high reasoning effort,
#: identical across arms. Exhaustion scores as a failure.
PLAN_WALL_CLOCK_MS = 30 * 60 * 1000.0
PLAN_MAX_TOOL_CALLS = 100
PLAN_REASONING_EFFORT = "high"

#: Built-in agent types the runtime can spawn.
#:
#: The runtime rejects a ``"*"`` wildcard in ``excludedBuiltinAgents`` and SDK
#: 1.0.13 exposes no RPC that enumerates the valid names, so this list is
#: maintained by hand and verified against the runtime at session creation --
#: an unknown name fails session construction loudly rather than silently.
#: Excluding :data:`SUBAGENT_TOOLS` is the primary control (without the
#: spawning tools there is no path to a subagent); this list is defense in
#: depth. Re-verify it when the pinned CLI version changes.
BUILTIN_AGENTS: tuple[str, ...] = (
    "explore",
    "task",
    "general-purpose",
    "rubber-duck",
    "code-review",
    "research",
    "security-review",
)


class IsolationViolation(RuntimeError):
    """Raised when the agent attempted to escape its assigned workspace."""


class BudgetExceeded(RuntimeError):
    """Raised when a declared budget terminated the session."""


class SandboxNotAppliedError(RuntimeError):
    """Raised when the runtime sandbox could not be applied to a session.

    Fails the trial rather than degrading to an unconfined run. A session that
    silently continues without the sandbox produces evidence indistinguishable
    from a confined run, which is the failure the plan's gate exists to catch.
    """


# ---------------------------------------------------------------------------
# Workspace
# ---------------------------------------------------------------------------


class TemporaryWorkspace:
    """A fresh temporary directory that is the agent's only working directory.

    ``root`` is fully resolved (symlinks included) at creation so that later
    containment checks compare real paths against a real path.
    """

    def __init__(self, prefix: str = "radius-perf-ws-") -> None:
        self._dir = tempfile.mkdtemp(prefix=prefix)
        self.root = Path(self._dir).resolve(strict=True)

    def write(self, relative: str, content: str) -> Path:
        target = (self.root / relative).resolve()
        if not _is_within(target, self.root):
            raise IsolationViolation(f"refusing to seed path outside workspace: {relative}")
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return target

    def destroy(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)

    def exists(self) -> bool:
        return Path(self._dir).exists()

    def __enter__(self) -> TemporaryWorkspace:
        return self

    def __exit__(self, *exc: object) -> None:
        self.destroy()


def _is_within(candidate: Path, root: Path) -> bool:
    """True when ``candidate`` is ``root`` or lives beneath it."""
    try:
        candidate.relative_to(root)
        return True
    except ValueError:
        return False


def _resolve_for_check(raw: str, root: Path) -> Path:
    """Resolve a requested path the way the filesystem would.

    ``Path.resolve()`` follows symlinks, which is what defeats a symlink
    escape: a link inside the workspace that points at ``/etc`` resolves to
    ``/etc`` and therefore fails containment. Relative paths are resolved
    against the workspace root, matching the session's working directory.
    """
    path = Path(raw).expanduser()
    if not path.is_absolute():
        path = root / path
    return path.resolve()


def _shell_escape_token(command: str) -> str | None:
    """Return the first escape-shaped token in a shell command, if any.

    Best effort by construction: a static screen cannot see through command
    substitution, variable expansion, or an embedded interpreter. It exists to
    catch the plain cases, not to be a sandbox.
    """
    for token in re.split(r"[\s;|&()<>]+", command):
        token = token.strip("\"'")
        if not token:
            continue
        if token.startswith("~"):
            return token
        if token.startswith("/"):
            return token
        if token == ".." or token.startswith("../") or "/../" in token:
            return token
        if token.endswith("/.."):
            return token
    return None


# ---------------------------------------------------------------------------
# Isolation policy
# ---------------------------------------------------------------------------


@dataclass
class IsolationPolicy:
    """Fail-closed path and tool screening for one session.

    This is an **advisory, in-process check**. It runs inside the harness and
    decides permission requests the runtime chooses to route through it. There
    is no OS or process boundary beneath it: the agent executes as a host
    process against a host temporary directory. Anything this check cannot
    prove is inside the workspace is denied, which is what makes it useful --
    but a denial is the harness declining a request, not a kernel refusing an
    operation.

    Every permission request is resolved to a real path and checked against the
    workspace root. Unknown request kinds are denied, so a new permission kind
    introduced by a future runtime cannot silently widen access.

    **Shell is denied by default, and this is not a conservative default --
    it is required for correctness.** Three separate fields of
    ``PermissionRequestShell`` are unreliable, all verified against CLI 1.0.83:

    * ``possible_paths`` is empty even for a command that plainly names an
      absolute path (``cat /etc/hosts`` arrives with ``possible_paths=[]``).
    * ``has_write_file_redirection`` is ``False`` for
      ``echo probe > /tmp/x``, a redirection writing outside the workspace.
      A check named for exactly this case does not fire.
    * ``command_segments[*].full_command_text`` truncates at the redirection
      operator, reporting ``echo probe`` for that same command, so a
      segment-based screen sees nothing outside the workspace.

    A handler therefore has no reliable structured signal on which to screen a
    shell command, and approving on any of these bases fails open. This is
    worse than a missing signal: two of the three actively mislead, and the
    third is the representation a careful author is most likely to reach for.

    With ``allow_shell=True`` the policy falls back to screening the top-level
    ``full_command_text`` for escape-shaped tokens. That is **best effort
    only**: command substitution, encoding, or an interpreter can defeat any
    static screen, so it is defence in depth and must never be described as
    confinement.

    Confinement comes from the runtime sandbox, which this harness now applies
    through ``session.rpc.options.update`` before the first prompt. Verified
    live on SDK 1.0.14 / CLI 1.0.87 with ``gpt-5.6-sol``: writes and reads
    outside the workspace fail with ``Operation not permitted`` while an
    in-workspace control write succeeds, and every execution reports
    ``sandboxApplied=true``. Compose containers bound the application under
    test, not the agent, and supply no confinement here.

    One consequence of this screen is worth stating, because it produced a false
    result. ``_shell_escape_token`` rejects **any** absolute path, including one
    inside the workspace. A harness-driven probe must name its target
    absolutely, so with the screen on, every probe -- and the in-workspace
    control -- is denied here and never reaches the sandbox. Both denials leave
    the file absent, so the run reads as perfect confinement while testing
    nothing. ``screen_shell_paths=False`` exists for that case and must never be
    set for a scored run.
    """

    workspace_root: Path
    allow_writes: bool = True
    allow_network: bool = False
    allow_shell: bool = False
    #: Sandbox-probe mode. When False the static path screen is bypassed so the
    #: runtime sandbox is the only thing that can deny a command. Scored runs
    #: must leave this True; it exists so confinement evidence is about the
    #: sandbox rather than about our own filter.
    screen_shell_paths: bool = True
    violations: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)

    def _deny(self, kind: str, reason: str, detail: Mapping[str, Any]) -> tuple[bool, str]:
        record = {"kind": kind, "reason": reason, **dict(detail)}
        self.violations.append(record)
        return False, reason

    def evaluate_paths(self, kind: str, paths: Sequence[str]) -> tuple[bool, str]:
        for raw in paths:
            if not raw:
                continue
            try:
                resolved = _resolve_for_check(raw, self.workspace_root)
            except (OSError, RuntimeError) as exc:
                return self._deny(kind, f"unresolvable path {raw!r}: {exc}", {"path": raw})
            if not _is_within(resolved, self.workspace_root):
                return self._deny(
                    kind,
                    "path escapes assigned workspace",
                    {"path": raw, "resolvedPath": str(resolved)},
                )
        return True, "within workspace"

    def _evaluate_shell(self, request: Any) -> tuple[bool, str]:
        """Screen a shell command. Denies by default; see the class docstring."""
        kind = "shell"
        command = getattr(request, "full_command_text", None) or ""
        detail = {"command": command}

        if getattr(request, "request_sandbox_bypass", None):
            return self._deny(kind, "command requested a sandbox bypass", detail)

        possible_urls = getattr(request, "possible_urls", None) or []
        if possible_urls and not self.allow_network:
            return self._deny(kind, "command would reach the network", detail)

        # Whatever paths the runtime *does* surface must still be contained.
        paths = list(getattr(request, "possible_paths", None) or [])
        if paths:
            ok, reason = self.evaluate_paths(kind, paths)
            if not ok:
                self.violations[-1]["command"] = command
                return ok, reason

        if not self.allow_shell:
            return self._deny(
                kind,
                "shell is disabled: possible_paths is unpopulated, so shell "
                "commands cannot be screened reliably through the permission "
                "API",
                detail,
            )

        # Screen `full_command_text` and nothing else. Two sibling fields look
        # like better inputs and are both unsafe, verified against CLI 1.0.83:
        #
        #   * `command_segments[*].full_command_text` truncates at a
        #     redirection operator. For `echo probe > /tmp/x`, the segment
        #     reads `echo probe`, so a segment-based screen sees no outside
        #     path and approves the write.
        #   * `has_write_file_redirection` is False for that same command, so
        #     a check named for exactly this case does not fire.
        #
        # The segment list is the more natural-looking choice precisely because
        # it appears tokenized and structured. Do not switch to it.
        escape = _shell_escape_token(command)
        if escape is not None:
            if not self.screen_shell_paths:
                # Sandbox-probe mode only. The screen and the sandbox both deny
                # by leaving the file absent, so while the screen is answering
                # no probe can say anything about the sandbox. Turning it off is
                # what makes the sandbox the only thing that can deny. Never set
                # this for a scored run.
                self.decisions.append(
                    {
                        "kind": kind,
                        "approved": True,
                        "reason": "static screen disabled for sandbox probing",
                        "command": command,
                    }
                )
                return True, "static screen disabled for sandbox probing"
            return self._deny(
                kind, f"command references a path outside the workspace: {escape!r}", detail
            )
        return True, "shell command screened as workspace-relative"

    def evaluate(self, request: Any) -> tuple[bool, str]:
        """Return ``(approve, reason)`` for one permission request."""
        kind = getattr(request, "kind", None) or type(request).__name__

        if kind == "read":
            candidates = [
                p
                for p in (getattr(request, "resolved_path", None), getattr(request, "path", None))
                if p
            ]
            if not candidates:
                return self._deny(kind, "read request carried no path", {})
            return self.evaluate_paths(kind, candidates[:1])

        if kind == "write":
            if not self.allow_writes:
                return self._deny(kind, "writes disabled for this session", {})
            candidates = [
                p
                for p in (
                    getattr(request, "resolved_path", None),
                    getattr(request, "file_name", None),
                )
                if p
            ]
            if not candidates:
                return self._deny(kind, "write request carried no path", {})
            return self.evaluate_paths(kind, candidates[:1])

        if kind == "shell":
            return self._evaluate_shell(request)

        if kind == "url":
            if self.allow_network:
                return True, "network allowed"
            return self._deny(kind, "network access denied", {"url": getattr(request, "url", None)})

        if kind == "memory":
            return self._deny(kind, "memory is disabled for benchmark sessions", {})

        # Fail closed: mcp, custom-tool, hook, factory, extension-* and any
        # future kind are denied rather than assumed safe.
        return self._deny(kind, f"permission kind {kind!r} denied by default", {})

    def handler(self, recorder: EventRecorder | None = None):
        """Build the ``on_permission_request`` callback for ``create_session``."""
        from copilot.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

        def on_permission_request(request: Any, invocation: Mapping[str, Any]) -> Any:
            approve, reason = self.evaluate(request)
            decision = {
                "kind": getattr(request, "kind", type(request).__name__),
                "approved": approve,
                "reason": reason,
                # Verbatim request, so a decision can be audited after the run
                # without re-running the session.
                "request": _describe_permission_request(request),
            }
            self.decisions.append(decision)
            if recorder is not None:
                recorder.record("harness", "permission.decision", decision)
            if approve:
                return PermissionDecisionApproveOnce()
            return PermissionDecisionReject(feedback=f"Denied by benchmark isolation: {reason}")

        return on_permission_request


def isolated_session_kwargs(
    *,
    workspace_root: Path,
    model: str,
    reasoning_effort: str | None = None,
    max_ai_credits: float | None = None,
    tools: list[Any] | None = None,
) -> dict[str, Any]:
    """Session options that disable memory, subagents, and auto routing.

    Each flag is set explicitly rather than relying on a runtime default, so a
    default change in a future SDK cannot quietly alter the benchmark
    condition.
    """
    if model.strip().lower() == "auto":
        raise ValueError(
            "auto routing is prohibited: the benchmark pins one explicit model per trial"
        )

    from copilot import ToolSet

    # NOTE: two distinct `MemoryConfiguration` types ship in SDK 1.0.13 --
    # a dataclass in `copilot.rpc` and a TypedDict in `copilot.session`.
    # `create_session` subscripts its argument, so it requires the TypedDict
    # (a plain mapping); passing the dataclass raises TypeError.
    from copilot.session import MemoryConfiguration

    # Defense in depth: subagent spawning tools by name, plus every MCP and
    # custom tool, are excluded.
    excluded = ToolSet()
    excluded.add_builtin(list(SUBAGENT_TOOLS))
    excluded.add_mcp("*")
    excluded.add_custom("*")

    memory: MemoryConfiguration = {"enabled": False}
    kwargs: dict[str, Any] = {
        "model": model,
        "working_directory": str(workspace_root),
        # No directory beyond the assigned workspace.
        "additional_directories": [],
        # Cold context: no cross-trial memory.
        "memory": memory,
        # One agent. No subagent spawning.
        "excluded_tools": excluded.to_list(),
        "excluded_builtin_agents": list(BUILTIN_AGENTS),
        "custom_agents": [],
        "custom_agents_local_only": True,
        # No repository or user instructions leaking into the condition.
        "skip_custom_instructions": True,
        "enable_config_discovery": False,
        "enable_on_demand_instruction_discovery": False,
        "instruction_directories": [],
        # Skills are a *treatment* surface, supplied by the fixture, never by
        # the host machine.
        "enable_skills": False,
        "skill_directories": [],
        "plugin_directories": [],
        # No cross-session state or embedding retrieval.
        "enable_session_store": False,
        "skip_embedding_retrieval": True,
        "enable_file_hooks": False,
        "enable_host_git_operations": False,
        # No MCP servers in the scored sandbox.
        "mcp_servers": {},
        "mcp_oauth_token_storage": "in-memory",
        "embedding_cache_storage": "in-memory",
        # Deterministic accounting surfaces.
        "enable_session_telemetry": False,
        "enable_file_change_tracking": True,
        "streaming": True,
        "include_sub_agent_streaming_events": True,
    }
    if reasoning_effort is not None:
        kwargs["reasoning_effort"] = reasoning_effort
    if max_ai_credits is not None:
        # The runtime refuses a limit below RUNTIME_MIN_AI_CREDITS, so a tight
        # per-trial budget cannot be expressed here. Raise it to the floor as a
        # runaway backstop and let SessionBudget enforce the real cap.
        kwargs["session_limits"] = {
            "max_ai_credits": max(max_ai_credits, RUNTIME_MIN_AI_CREDITS)
        }
    if tools:
        kwargs["tools"] = list(tools)
    return kwargs


# ---------------------------------------------------------------------------
# Budgets
# ---------------------------------------------------------------------------


@dataclass
class SessionBudget:
    """Hard caps that terminate a session.

    ``max_ai_credits`` is additionally passed to the runtime as a session
    limit; the harness still enforces it locally so termination does not depend
    on an experimental runtime feature.
    """

    wall_clock_ms: float | None = None
    max_model_requests: int | None = None
    max_tool_calls: int | None = None
    max_ai_credits: float | None = None

    @classmethod
    def plan_default(cls) -> "SessionBudget":
        """The plan's scored-trial budget: 30 minutes or 100 tool calls.

        Deliberately leaves ``max_model_requests`` unset. The plan caps tool
        calls, not model calls, and adding an undeclared third cap would let a
        trial terminate for a reason no arm agreed to -- which would show up as
        a between-arm difference in exhaustion rate that reflects the harness
        rather than the treatment.
        """
        return cls(
            wall_clock_ms=PLAN_WALL_CLOCK_MS,
            max_tool_calls=PLAN_MAX_TOOL_CALLS,
        )

    def check(
        self,
        *,
        elapsed_ms: float,
        model_requests: int,
        tool_calls: int,
        ai_credits: float | None,
    ) -> str | None:
        if self.wall_clock_ms is not None and elapsed_ms >= self.wall_clock_ms:
            return f"wall-clock budget exhausted: {elapsed_ms:.0f}ms >= {self.wall_clock_ms:.0f}ms"
        if self.max_model_requests is not None and model_requests >= self.max_model_requests:
            return (
                f"model-request budget exhausted: {model_requests} >= {self.max_model_requests}"
            )
        if self.max_tool_calls is not None and tool_calls >= self.max_tool_calls:
            return f"tool-call budget exhausted: {tool_calls} >= {self.max_tool_calls}"
        if (
            self.max_ai_credits is not None
            and ai_credits is not None
            and ai_credits >= self.max_ai_credits
        ):
            return f"AI-credit budget exhausted: {ai_credits} >= {self.max_ai_credits}"
        return None


@dataclass
class SessionOutcome:
    """Everything a trial needs from one agent execution."""

    session_id: str | None
    model: str
    terminal_class: str
    agent_wall_clock_ms: float
    assistant_usage_events: list[dict[str, Any]] = field(default_factory=list)
    session_metrics: dict[str, Any] | None = None
    session_metrics_error: str | None = None
    tool_timeline: dict[str, Any] = field(default_factory=dict)
    model_request_durations_ms: list[float] = field(default_factory=list)
    budget_stop_reason: str | None = None
    isolation_violations: list[dict[str, Any]] = field(default_factory=list)
    permission_decisions: list[dict[str, Any]] = field(default_factory=list)
    subagent_events: int = 0
    final_message: str | None = None
    error: str | None = None


# ---------------------------------------------------------------------------
# Model catalog
# ---------------------------------------------------------------------------


async def fetch_model_catalog(client: Any) -> dict[str, Any]:
    """Capture the account's model catalog verbatim plus a normalized view.

    Records per model: id, display name, availability/policy, supported
    reasoning efforts, billing multiplier, and token pricing, as required by
    the plan's model-selection and cost-control sections.
    """
    from copilot.rpc import ModelsListRequest

    result = await client.rpc.models.list(ModelsListRequest())
    raw = [model.to_dict() for model in result.models]

    normalized = []
    for model in raw:
        billing = model.get("billing") or {}
        policy = model.get("policy") or {}
        normalized.append(
            {
                "id": model.get("id"),
                "displayName": model.get("name"),
                "policyState": policy.get("state"),
                "available": policy.get("state") in (None, "enabled"),
                "supportedReasoningEfforts": model.get("supportedReasoningEfforts"),
                "defaultReasoningEffort": model.get("defaultReasoningEffort"),
                "billingMultiplier": billing.get("multiplier"),
                "billingDiscountPercent": billing.get("discountPercent"),
                "tokenPrices": billing.get("tokenPrices"),
                "modelPickerCategory": model.get("modelPickerCategory"),
                "modelPickerPriceCategory": model.get("modelPickerPriceCategory"),
                "supportedContextTiers": model.get("supportedContextTiers"),
                "capabilities": model.get("capabilities"),
            }
        )

    return {"raw": raw, "normalized": normalized, "modelCount": len(raw)}


# ---------------------------------------------------------------------------
# Session driver
# ---------------------------------------------------------------------------


class SpikeSession:
    """Drives one cold session end to end, recording everything."""

    def __init__(
        self,
        *,
        client: Any,
        workspace: TemporaryWorkspace,
        recorder: EventRecorder,
        model: str,
        budget: SessionBudget | None = None,
        reasoning_effort: str | None = None,
        allow_writes: bool = True,
        allow_shell: bool = False,
        screen_shell_paths: bool = True,
        tools: list[Any] | None = None,
        sandbox_settings: Any = None,
    ) -> None:
        self._client = client
        self._workspace = workspace
        self._recorder = recorder
        self._model = model
        self._budget = budget or SessionBudget()
        self._reasoning_effort = reasoning_effort
        self._tools = list(tools or [])
        self._sandbox_settings = sandbox_settings
        self.sandbox_application: Any = None
        self.prompts_sent = 0
        self.context_events: list[dict[str, Any]] = []
        self.tool_executions: list[dict[str, Any]] = []
        # Starts are kept alongside completions because the sandbox gate has to
        # see a command that began and never finished. Keeping only completions
        # would hide exactly the execution the gate exists to object to.
        self.tool_execution_starts: list[dict[str, Any]] = []
        self.policy = IsolationPolicy(
            workspace_root=workspace.root,
            allow_writes=allow_writes,
            allow_shell=allow_shell,
            screen_shell_paths=screen_shell_paths,
        )
        self.timeline = ToolCallTimeline()
        self.assistant_usage: list[dict[str, Any]] = []
        self.model_request_durations_ms: list[float] = []
        self.subagent_events = 0
        self._budget_stop_reason: str | None = None
        self._final_message: str | None = None
        self._session: Any = None
        self._turn_starts: dict[str, int] = {}
        self._stop_requested = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

    # -- accounting -------------------------------------------------------

    @property
    def model_requests(self) -> int:
        return len(self.assistant_usage)

    @property
    def ai_credits(self) -> float | None:
        total: float | None = None
        for data in self.assistant_usage:
            usage = data.get("copilotUsage")
            if isinstance(usage, Mapping):
                nano = usage.get("totalNanoAiu")
                if isinstance(nano, (int, float)):
                    total = (total or 0.0) + float(nano) / 1_000_000_000.0
        return total

    def _check_budget(self) -> None:
        if self._budget_stop_reason is not None:
            return
        reason = self._budget.check(
            elapsed_ms=self._recorder.clock.elapsed_ms(),
            model_requests=self.model_requests,
            tool_calls=len(self.timeline.logical_tool_calls),
            ai_credits=self.ai_credits,
        )
        if reason is not None:
            self._budget_stop_reason = reason
            self._recorder.record("harness", "budget.exhausted", {"reason": reason})
            if self._loop is not None:
                self._loop.call_soon_threadsafe(self._stop_requested.set)

    # -- event capture ----------------------------------------------------

    def _on_event(self, event: Any) -> None:
        """Record every session event verbatim, in order."""
        try:
            payload = event.data.to_dict() if hasattr(event.data, "to_dict") else {}
        except Exception as exc:  # pragma: no cover - defensive
            payload = {"_serializationError": repr(exc)}

        event_type = getattr(event.type, "value", str(event.type))
        # The SDK maps any event type it does not model onto "unknown", which
        # collapses genuinely distinct events (turn_started, model_call_started,
        # ...) into one bucket. The runtime still carries a discriminator in
        # `data.kind`, so surface it under an explicit `unknown:` prefix. The
        # prefix keeps these clearly separate from SDK-modelled types, so no
        # downstream consumer can mistake one for a supported event.
        sdk_type_recognized = event_type != "unknown"
        if not sdk_type_recognized:
            kind = payload.get("kind")
            if isinstance(kind, str) and kind:
                event_type = f"unknown:{kind}"

        elapsed_ms = self._recorder.clock.elapsed_ms()
        self._recorder.record(
            "copilot",
            event_type,
            {
                "eventId": str(getattr(event, "id", "")),
                "agentId": getattr(event, "agent_id", None),
                "parentId": str(getattr(event, "parent_id", "") or "") or None,
                "runtimeTimestamp": getattr(event, "timestamp", None),
                "sdkTypeRecognized": sdk_type_recognized,
                "data": payload,
            },
        )

        if event_type == "assistant.usage":
            # Verbatim, including sub-agent calls. This is the only durable
            # copy: assistant.usage is ephemeral and never replayed.
            self.assistant_usage.append(payload)
            duration = payload.get("duration")
            if isinstance(duration, (int, float)):
                self.model_request_durations_ms.append(float(duration))
            self._check_budget()
        elif event_type == "tool.execution_start":
            self.timeline.start(
                payload.get("toolCallId"),
                payload.get("toolName"),
                at_ms=elapsed_ms,
                agent_id=getattr(event, "agent_id", None),
            )
            # Verbatim, for the same reason completions are: the gate reads the
            # runtime's own payloads rather than a harness re-derivation.
            self.tool_execution_starts.append(payload)
            self._check_budget()
        elif event_type == "tool.execution_complete":
            # `tool.execution_complete` carries no toolName; the timeline
            # resolves it from the matching start.
            self.timeline.complete(
                payload.get("toolCallId"),
                at_ms=elapsed_ms,
                success=payload.get("success"),
                agent_id=getattr(event, "agent_id", None),
            )
            # Kept verbatim so the sandbox gate reads the runtime's own
            # `sandboxApplied` telemetry rather than a harness re-derivation.
            self.tool_executions.append(payload)
            self._check_budget()
        elif event_type in CONTEXT_EVENT_TYPES:
            # Emission unverified: no trial has yet filled a context window, so
            # an empty list here is not evidence that compaction did not occur.
            self.context_events.append({"type": event_type, "data": payload})
        elif event_type.startswith("subagent."):
            self.subagent_events += 1
            self._recorder.record(
                "harness",
                "isolation.unexpected-subagent",
                {"eventType": event_type},
            )
        elif event_type == "assistant.message":
            content = payload.get("content")
            if isinstance(content, str) and content.strip():
                self._final_message = content

    # -- lifecycle --------------------------------------------------------

    async def start(self) -> Any:
        self._loop = asyncio.get_running_loop()
        kwargs = isolated_session_kwargs(
            workspace_root=self._workspace.root,
            model=self._model,
            reasoning_effort=self._reasoning_effort,
            max_ai_credits=self._budget.max_ai_credits,
            tools=self._tools,
        )
        self._recorder.record("harness", "session.create.request", _redact(kwargs))
        self._session = await self._client.create_session(
            on_permission_request=self.policy.handler(self._recorder),
            on_event=self._on_event,
            **kwargs,
        )
        self._recorder.record(
            "harness",
            "session.create.result",
            {"sessionId": self._session.session_id},
        )
        if self._sandbox_settings is not None:
            # Applied here, between create and the first prompt, because
            # `create_session` exposes no `sandbox_config` parameter. The
            # session exists unconfined for this window; nothing is prompted
            # into it before the update lands, and `prompts_sent` is passed so
            # the ordering is recorded as an observation rather than a claim.
            from .sandbox import apply_sandbox

            self.sandbox_application = await apply_sandbox(
                self._session,
                self._sandbox_settings,
                recorder=self._recorder,
                prompts_sent=self.prompts_sent,
            )
            if not self.sandbox_application.succeeded:
                raise SandboxNotAppliedError(
                    "sandbox configuration was not applied: "
                    f"{self.sandbox_application.error}"
                )
        return self._session

    async def run_prompt(self, prompt: str, *, timeout_s: float) -> SessionOutcome:
        """Send one prompt and wait for idle, budget stop, or timeout."""
        assert self._session is not None, "start() must be called first"
        started_ns = self._recorder.clock.elapsed_ns()
        self.prompts_sent += 1
        self._recorder.record("harness", "agent.prompt", {"prompt": prompt})

        terminal_class = "validated_success"
        error: str | None = None

        send_task = asyncio.create_task(
            self._session.send_and_wait(prompt, timeout=timeout_s)
        )
        stop_task = asyncio.create_task(self._stop_requested.wait())
        # Independent wall-clock guard: a runtime that never emits another
        # event must still be terminated.
        try:
            done, pending = await asyncio.wait(
                {send_task, stop_task},
                timeout=self._wall_clock_timeout(timeout_s),
                return_when=asyncio.FIRST_COMPLETED,
            )
        finally:
            pass

        if stop_task in done and send_task not in done:
            terminal_class = "budget_exhaustion"
            await self._abort()
        elif not done:
            self._budget_stop_reason = self._budget_stop_reason or (
                "wall-clock budget exhausted: no completion within "
                f"{self._wall_clock_timeout(timeout_s):.0f}s"
            )
            terminal_class = "budget_exhaustion"
            self._recorder.record(
                "harness", "budget.exhausted", {"reason": self._budget_stop_reason}
            )
            await self._abort()
        else:
            try:
                await send_task
            except TimeoutError:
                terminal_class = "budget_exhaustion"
                self._budget_stop_reason = (
                    self._budget_stop_reason or f"send_and_wait timed out after {timeout_s}s"
                )
                await self._abort()
            except Exception as exc:
                terminal_class = "copilot_sdk_or_adapter_failure"
                error = repr(exc)
                self._recorder.record("harness", "agent.error", {"error": error})

        for task in (send_task, stop_task):
            if not task.done():
                task.cancel()
        await asyncio.gather(send_task, stop_task, return_exceptions=True)

        if self._budget_stop_reason and terminal_class == "validated_success":
            terminal_class = "budget_exhaustion"
        if self.policy.violations and terminal_class == "validated_success":
            # An escape attempt is its own classification per the plan.
            terminal_class = "isolation_violation_attempt"

        elapsed_ms = (self._recorder.clock.elapsed_ns() - started_ns) / 1_000_000
        metrics, metrics_error = await self._fetch_metrics()

        outcome = SessionOutcome(
            session_id=getattr(self._session, "session_id", None),
            model=self._model,
            terminal_class=terminal_class,
            agent_wall_clock_ms=elapsed_ms,
            assistant_usage_events=list(self.assistant_usage),
            session_metrics=metrics,
            session_metrics_error=metrics_error,
            tool_timeline=self.timeline.summary(),
            model_request_durations_ms=list(self.model_request_durations_ms),
            budget_stop_reason=self._budget_stop_reason,
            isolation_violations=list(self.policy.violations),
            permission_decisions=list(self.policy.decisions),
            subagent_events=self.subagent_events,
            final_message=self._final_message,
            error=error,
        )
        self._recorder.record(
            "harness",
            "agent.finished",
            {
                "terminalClass": outcome.terminal_class,
                "agentWallClockMs": outcome.agent_wall_clock_ms,
                "modelRequests": self.model_requests,
                "logicalToolCalls": len(self.timeline.logical_tool_calls),
                "budgetStopReason": outcome.budget_stop_reason,
            },
        )
        return outcome

    def _wall_clock_timeout(self, fallback_s: float) -> float:
        if self._budget.wall_clock_ms is None:
            return fallback_s
        remaining_ms = self._budget.wall_clock_ms - self._recorder.clock.elapsed_ms()
        return max(remaining_ms / 1000.0, 0.1)

    async def _abort(self) -> None:
        try:
            await self._session.abort()
            self._recorder.record("harness", "session.aborted", {})
        except Exception as exc:  # pragma: no cover - defensive
            self._recorder.record("harness", "session.abort.failed", {"error": repr(exc)})

    async def _fetch_metrics(self) -> tuple[dict[str, Any] | None, str | None]:
        """Read the experimental accumulated-usage RPC, tolerating failure."""
        try:
            metrics = await self._session.rpc.usage.get_metrics()
            payload = metrics.to_dict()
            self._recorder.record("harness", "session.usage.getMetrics", payload)
            return payload, None
        except Exception as exc:
            message = repr(exc)
            self._recorder.record(
                "harness", "session.usage.getMetrics.failed", {"error": message}
            )
            return None, message

    async def close(self) -> None:
        if self._session is not None:
            try:
                await self._session.disconnect()
            except Exception:  # pragma: no cover - defensive
                pass


def _describe_permission_request(request: Any) -> dict[str, Any]:
    """Flatten a permission request into recordable, JSON-safe fields."""
    out: dict[str, Any] = {"class": type(request).__name__}
    for name in dir(request):
        if name.startswith("_"):
            continue
        try:
            value = getattr(request, name)
        except Exception:  # pragma: no cover - defensive
            continue
        if callable(value):
            continue
        out[name] = (
            value
            if isinstance(value, (str, int, float, bool, type(None)))
            else repr(value)[:500]
        )
    return out


_REDACTED_KEYS = {"github_token", "githubToken", "token", "authorization"}


def _redact(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Strip credential-shaped values before they reach an artifact.

    Config objects (``MemoryConfiguration``, ``SessionLimitsConfig``) are
    rendered as plain data so the recorded request stays reproducible.
    """
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if key in _REDACTED_KEYS:
            out[key] = "[redacted]"
        elif isinstance(value, Mapping):
            out[key] = _redact(value)
        elif hasattr(value, "__dataclass_fields__"):
            out[key] = {
                f: getattr(value, f) for f in value.__dataclass_fields__
            }
        elif isinstance(value, (str, int, float, bool, type(None), list)):
            out[key] = value
        else:
            out[key] = repr(value)
    return out
