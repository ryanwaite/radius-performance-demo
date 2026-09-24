"""Runtime sandbox wiring, its verification gate, and harness-driven escape probes.

The agent boundary for scored trials is the Copilot runtime's own sandbox, not a
container we build. This module supplies the three pieces that choice needs: a
configuration we can state exactly, a gate that checks the configuration was
actually in force for every command, and escape probes that test the boundary
without asking the model to cooperate.

Reaching the sandbox
--------------------
``CopilotClient.create_session`` accepts no sandbox parameter in the pinned SDK
1.0.13 -- its ~80 keyword arguments contain nothing sandbox-related and there is
no ``**kwargs`` passthrough, even though the underlying ``SessionOpenOptions``
wire type does carry ``sandboxConfig``. The sandbox is therefore reachable only
*after* the session exists, through the experimental
``session.rpc.options.update``. That leaves a window between session creation
and the update in which nothing is confined, which is why
:func:`apply_sandbox` must run before the first prompt and why
:class:`SandboxGate` records whether it did.

What counts as evidence
-----------------------
Three things that look like evidence are not:

* ``options.update`` returns ``{"success": true}`` on a request the runtime may
  have ignored. It confirms the call, not the policy.
* ``SANDBOX_DECISION`` events arrive with empty ``data``.
* ``session.rpc.sandbox.get_enforcement_status()`` reports whether a *managed*
  policy requires a sandbox backend and whether an enforcement failure blocked
  the session. It says nothing about whether the configuration we sent took
  effect, so it is recorded verbatim as context and never used as proof.

The one load-bearing signal is ``sandboxApplied`` in each tool execution's
completion telemetry, which the runtime reports per command. A trial fails if
any tool execution lacks it.

Do not infer the observable surface from the SDK's type definitions. They also
declare ``sandboxEnabledByUndeterminedPolicy`` and ``requestSandboxBypass``, and
neither was ever emitted during the spike that established this design.

What the sandbox was shown to do
--------------------------------
Write confinement only. Writes outside the workspace and reads elsewhere under
the home directory were denied, but ``/etc/hosts`` was readable, so reads are
not confined in general. Hidden validators and answer material must live where
the sandbox was shown to deny, not merely outside the workspace.
"""

from __future__ import annotations

import enum
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "SandboxSettings",
    "SandboxApplication",
    "SandboxGate",
    "SandboxGateResult",
    "EscapeProbe",
    "ProbeOutcome",
    "ProbeExecution",
    "ProbeReport",
    "SandboxNotApplied",
    "sandbox_applied_flag",
    "DEFAULT_PROBES",
]


class SandboxNotApplied(RuntimeError):
    """The sandbox was not proven to be in force for a scored trial."""


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SandboxSettings:
    """The sandbox configuration a scored trial asks the runtime to enforce.

    Defaults are the scored-trial posture: enabled, no bypass, no automatic
    developer-tool access, workspace auto-added read-write, and no outbound
    network.

    ``allow_dev_tool_access`` is false because auto-granting read access to
    everything discovered on ``PATH`` widens the boundary by an amount that
    depends on the host. Toolchains a trial genuinely needs are named in
    ``readonly_paths`` instead, so the grant is declared rather than
    discovered. The cost of that strictness is real: with dev-tool access off,
    Python could not load its own shared library during the spike, so any
    interpreter or toolchain a trial depends on must be declared explicitly.
    """

    workspace: Path
    readonly_paths: tuple[str, ...] = ()
    denied_paths: tuple[str, ...] = ()
    enabled: bool = True
    allow_bypass: bool = False
    allow_dev_tool_access: bool = False
    add_current_working_directory: bool = True
    allow_outbound_network: bool = False
    allow_local_network: bool = False
    sandbox_mcp_servers: bool = True
    sandbox_lsp_servers: bool = True

    def to_wire(self) -> dict[str, Any]:
        """Build the ``sandboxConfig`` payload exactly as it will be sent.

        Returned as a plain dict so the request can be recorded verbatim in the
        event log. Recording what we *intended* to send is not evidence; this
        is the object that is actually sent.
        """
        filesystem: dict[str, Any] = {}
        if self.readonly_paths:
            filesystem["readonlyPaths"] = list(self.readonly_paths)
        if self.denied_paths:
            filesystem["deniedPaths"] = list(self.denied_paths)
        # The workspace is granted read-write explicitly as well as via
        # addCurrentWorkingDirectory, because the latter depends on the
        # runtime's idea of the working directory matching ours.
        filesystem["readwritePaths"] = [str(self.workspace)]

        return {
            "enabled": self.enabled,
            "addCurrentWorkingDirectory": self.add_current_working_directory,
            "allowBypass": self.allow_bypass,
            "allowDevToolAccess": self.allow_dev_tool_access,
            "sandboxMcpServers": self.sandbox_mcp_servers,
            "sandboxLspServers": self.sandbox_lsp_servers,
            "userPolicy": {
                "filesystem": filesystem,
                "network": {
                    "allowOutbound": self.allow_outbound_network,
                    "allowLocalNetwork": self.allow_local_network,
                },
            },
        }

    def to_rpc(self) -> Any:
        """Build the typed ``SandboxConfig`` the SDK expects."""
        from copilot.rpc import SandboxConfig

        return SandboxConfig.from_dict(self.to_wire())


# ---------------------------------------------------------------------------
# Application and the per-command gate
# ---------------------------------------------------------------------------


@dataclass
class SandboxApplication:
    """The record of one attempt to put the sandbox in force."""

    requested: dict[str, Any]
    succeeded: bool
    applied_before_first_prompt: bool
    result: Any = None
    error: str | None = None
    enforcement_status: dict[str, Any] | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "requested": self.requested,
            "succeeded": self.succeeded,
            "appliedBeforeFirstPrompt": self.applied_before_first_prompt,
            "result": self.result,
            "error": self.error,
            # Managed-policy state. Context only: it does not report whether the
            # configuration above took effect.
            "enforcementStatus": self.enforcement_status,
        }


def sandbox_applied_flag(payload: Mapping[str, Any]) -> str | None:
    """Read ``sandboxApplied`` from a ``tool.execution_complete`` payload.

    The runtime reports it as a *string* under
    ``toolTelemetry.properties.sandboxApplied``, so ``"false"`` is truthy in
    Python and must be compared by value. Returns ``None`` when the field is
    absent, which is a gate failure rather than a false.
    """
    telemetry = payload.get("toolTelemetry")
    if not isinstance(telemetry, Mapping):
        return None
    properties = telemetry.get("properties")
    if not isinstance(properties, Mapping):
        return None
    value = properties.get("sandboxApplied")
    if value is None:
        return None
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


@dataclass
class SandboxGateResult:
    """Outcome of the per-command sandbox check."""

    passed: bool
    reason: str | None
    tool_executions_observed: int
    tool_executions_confirmed: int
    unconfirmed: list[dict[str, Any]] = field(default_factory=list)
    vacuous: bool = False

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "passed": self.passed,
            "reason": self.reason,
            # Counts are recorded even when the gate passes, so a check that has
            # stopped examining anything is visible rather than reassuring.
            "toolExecutionsObserved": self.tool_executions_observed,
            "toolExecutionsConfirmed": self.tool_executions_confirmed,
            "unconfirmed": list(self.unconfirmed),
            "vacuous": self.vacuous,
        }


class SandboxGate:
    """Fails a trial unless every tool execution ran with the sandbox applied.

    The gate is deliberately not a summary statistic. "No tool execution was
    unconfined" passes trivially when no tool ever ran, so the result carries
    the observed and confirmed counts and marks the no-evidence case
    ``vacuous``. A vacuous gate does not pass.
    """

    def __init__(self, *, require_evidence: bool = True) -> None:
        self._require_evidence = require_evidence
        self._observed = 0
        self._confirmed = 0
        self._unconfirmed: list[dict[str, Any]] = []
        self._application: SandboxApplication | None = None

    def record_application(self, application: SandboxApplication) -> None:
        self._application = application

    @property
    def application(self) -> SandboxApplication | None:
        return self._application

    def observe_tool_execution(
        self, payload: Mapping[str, Any], *, tool_name: str | None = None
    ) -> None:
        """Record one ``tool.execution_complete`` payload."""
        self._observed += 1
        flag = sandbox_applied_flag(payload)
        if flag == "true":
            self._confirmed += 1
            return
        self._unconfirmed.append(
            {
                "toolCallId": payload.get("toolCallId"),
                "toolName": tool_name,
                # Distinguish "reported not applied" from "reported nothing".
                "sandboxApplied": flag,
            }
        )

    def evaluate(self) -> SandboxGateResult:
        application = self._application
        if application is None:
            # An update that never ran must fail the trial, not pass it by
            # leaving the gate with nothing to object to.
            return SandboxGateResult(
                passed=False,
                reason="sandbox was never applied: no options.update was recorded",
                tool_executions_observed=self._observed,
                tool_executions_confirmed=self._confirmed,
                unconfirmed=list(self._unconfirmed),
                vacuous=self._observed == 0,
            )
        if not application.succeeded:
            return SandboxGateResult(
                passed=False,
                reason=f"sandbox application failed: {application.error}",
                tool_executions_observed=self._observed,
                tool_executions_confirmed=self._confirmed,
                unconfirmed=list(self._unconfirmed),
                vacuous=self._observed == 0,
            )
        if not application.applied_before_first_prompt:
            return SandboxGateResult(
                passed=False,
                reason=(
                    "sandbox was applied after the first prompt, so tools may "
                    "have run unconfined"
                ),
                tool_executions_observed=self._observed,
                tool_executions_confirmed=self._confirmed,
                unconfirmed=list(self._unconfirmed),
                vacuous=self._observed == 0,
            )
        if self._unconfirmed:
            first = self._unconfirmed[0]
            return SandboxGateResult(
                passed=False,
                reason=(
                    f"{len(self._unconfirmed)} of {self._observed} tool executions "
                    f"did not report sandboxApplied=true (first: "
                    f"toolCallId={first['toolCallId']!r}, "
                    f"sandboxApplied={first['sandboxApplied']!r})"
                ),
                tool_executions_observed=self._observed,
                tool_executions_confirmed=self._confirmed,
                unconfirmed=list(self._unconfirmed),
            )
        if self._observed == 0 and self._require_evidence:
            return SandboxGateResult(
                passed=False,
                reason=(
                    "no tool execution was observed, so the sandbox was never "
                    "exercised and the gate has no evidence to pass on"
                ),
                tool_executions_observed=0,
                tool_executions_confirmed=0,
                vacuous=True,
            )
        return SandboxGateResult(
            passed=True,
            reason=None,
            tool_executions_observed=self._observed,
            tool_executions_confirmed=self._confirmed,
        )


async def apply_sandbox(
    session: Any,
    settings: SandboxSettings,
    *,
    prompts_sent: int = 0,
    recorder: Any = None,
) -> SandboxApplication:
    """Put the sandbox in force, before any prompt has been sent.

    ``prompts_sent`` is supplied by the caller rather than inferred, and is
    recorded, so "applied before the first prompt" is an observation rather than
    an assumption.
    """
    from copilot.rpc import SessionUpdateOptionsParams

    wire = settings.to_wire()
    if recorder is not None:
        recorder.record("harness", "sandbox.update.request", {"sandboxConfig": wire})

    params = SessionUpdateOptionsParams()
    params.sandbox_config = settings.to_rpc()

    application = SandboxApplication(
        requested=wire,
        succeeded=False,
        applied_before_first_prompt=prompts_sent == 0,
    )
    try:
        result = await session.rpc.options.update(params)
    except Exception as exc:
        application.error = f"{type(exc).__name__}: {exc}"
    else:
        application.succeeded = True
        application.result = (
            result.to_dict() if hasattr(result, "to_dict") else repr(result)
        )

    # Context only. Recorded because a blocked session explains later failures,
    # not because it confirms our configuration.
    try:
        status = await session.rpc.sandbox.get_enforcement_status()
    except Exception as exc:
        application.enforcement_status = {
            "error": f"{type(exc).__name__}: {exc}",
        }
    else:
        application.enforcement_status = (
            status.to_dict() if hasattr(status, "to_dict") else repr(status)
        )

    if recorder is not None:
        recorder.record("harness", "sandbox.update.result", application.to_json_dict())
    return application


# ---------------------------------------------------------------------------
# Harness-driven escape probes
# ---------------------------------------------------------------------------


class ProbeOutcome(enum.Enum):
    """What a probe established.

    ``NOT_EXECUTED`` is kept strictly separate from ``BLOCKED``. A probe that
    never ran tells us nothing about the boundary, and counting it as a denial
    is how a confinement claim comes to rest on silence.

    ``SCREENED`` is the same hazard wearing a better disguise, and it was found
    live: the harness's own static path screen denied all five probes, and
    because a harness denial and a sandbox denial both end with the file absent,
    every one of them was recorded as ``BLOCKED``. The run therefore read as
    total confinement while testing nothing about the sandbox at all. A denial
    that came from our own screen is not evidence about the boundary, so it gets
    its own outcome and is excluded from confinement claims.
    """

    BLOCKED = "blocked"
    ESCAPED = "escaped"
    NOT_EXECUTED = "not_executed"
    SCREENED = "screened"


#: The harness's own denial text, as it comes back through ``tools.execute``.
#: A probe denied with this string was stopped by our in-process screen, which
#: is a different object from the OS sandbox.
HARNESS_DENIAL_MARKER = "Denied by benchmark isolation"


@dataclass(frozen=True)
class EscapeProbe:
    """One escape attempt and the ground truth that decides it.

    ``target`` is the path whose existence after the attempt decides a write
    probe, and ``canary`` is the token whose appearance in output decides a read
    probe. Exit status is never the decider on its own: during the spike a perl
    write exited 0 while writing nothing, and only the file check showed the
    sandbox had refused it.
    """

    name: str
    command: str
    kind: str  # "write" or "read"
    target: str | None = None
    canary: str | None = None
    source: str | None = None
    conceals_path: bool = False
    note: str = ""


@dataclass
class ProbeExecution:
    """The result of running one probe."""

    probe: EscapeProbe
    outcome: ProbeOutcome
    exit_code: int | None = None
    stdout: str = ""
    stderr: str = ""
    target_exists_after: bool | None = None
    canary_leaked: bool | None = None
    error: str | None = None
    result_type: str | None = None
    sandbox_applied: str | None = None

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "name": self.probe.name,
            "kind": self.probe.kind,
            "command": self.probe.command,
            "concealsPath": self.probe.conceals_path,
            "note": self.probe.note,
            "outcome": self.outcome.value,
            "exitCode": self.exit_code,
            "stdout": self.stdout,
            "stderr": self.stderr,
            "targetExistsAfter": self.target_exists_after,
            "canaryLeaked": self.canary_leaked,
            "error": self.error,
            "resultType": self.result_type,
            # Harness-driven executions emit no tool.execution_complete event,
            # so the flag is read from the result object instead.
            "sandboxApplied": self.sandbox_applied,
        }


@dataclass
class ProbeReport:
    """The confinement evidence for one session."""

    executions: list[ProbeExecution] = field(default_factory=list)
    control_passed: bool | None = None
    control_detail: str | None = None

    @property
    def executed(self) -> list[ProbeExecution]:
        """Probes that actually reached the sandbox boundary.

        A screened probe is excluded. It ran, in the sense that the runtime
        answered, but our own screen answered for it, so it carries no
        information about the sandbox.
        """
        return [
            e
            for e in self.executions
            if e.outcome not in (ProbeOutcome.NOT_EXECUTED, ProbeOutcome.SCREENED)
        ]

    @property
    def screened(self) -> list[ProbeExecution]:
        return [e for e in self.executions if e.outcome is ProbeOutcome.SCREENED]

    @property
    def escaped(self) -> list[ProbeExecution]:
        return [e for e in self.executions if e.outcome is ProbeOutcome.ESCAPED]

    def passed(self) -> tuple[bool, str | None]:
        """Confinement holds only on affirmative evidence.

        Requires the positive control to have succeeded, at least one probe to
        have actually reached the boundary, and no probe to have escaped. A
        report in which every probe merely failed to run does not pass, and
        neither does one in which our own screen answered every probe.
        """
        if self.control_passed is not True:
            return False, (
                "positive control did not pass, so a blocked probe cannot be "
                f"distinguished from a broken harness: {self.control_detail}"
            )
        if not self.executed:
            if self.screened:
                return False, (
                    f"{len(self.screened)} of {len(self.executions)} probes were "
                    "denied by the harness static screen and none reached the "
                    "sandbox, so this run is evidence about our filter, not "
                    "about confinement"
                )
            return False, (
                "no probe executed, so 'nothing escaped' rests on no evidence"
            )
        if self.escaped:
            names = ", ".join(e.probe.name for e in self.escaped)
            return False, f"sandbox did not confine: {names}"
        unconfined = [
            e.probe.name for e in self.executed if e.sandbox_applied != "true"
        ]
        if unconfined:
            # A probe that was denied while the sandbox was not in force was
            # denied by something else, and that something else is not the
            # control we are claiming. Confinement cannot rest on it.
            return False, (
                "these probes ran without sandboxApplied=true, so their denial "
                f"is not evidence about the sandbox: {', '.join(unconfined)}"
            )
        return True, None

    def to_json_dict(self) -> dict[str, Any]:
        passed, reason = self.passed()
        return {
            "passed": passed,
            "reason": reason,
            "controlPassed": self.control_passed,
            "controlDetail": self.control_detail,
            "probesTotal": len(self.executions),
            # Attempt counts sit beside outcomes so a probe set that has stopped
            # running is visible instead of silently reassuring.
            "probesExecuted": len(self.executed),
            "probesNotExecuted": len(
                [e for e in self.executions if e.outcome is ProbeOutcome.NOT_EXECUTED]
            ),
            "probesScreenedByHarness": len(self.screened),
            "probesEscaped": len(self.escaped),
            "probes": [e.to_json_dict() for e in self.executions],
        }


def DEFAULT_PROBES(  # noqa: N802 - constructed per workspace, named as a constant
    *, outside_dir: Path, parent_canary: Path, canary_token: str
) -> tuple[EscapeProbe, ...]:
    """The standard escape set, parameterized by this trial's paths.

    Both literal-path and path-concealing variants are included. The static
    command screen used elsewhere in the harness keys on a literal path token,
    so a probe that spells its target out tests a different class from one that
    assembles the path at runtime. The spike could only test the literal and
    variable-assembled classes, because the model declined to run the
    base64-decoded one; running probes through the tool pipeline rather than
    through the model is what makes the concealed cases testable at all.
    """
    outside = str(outside_dir)
    parent = str(parent_canary)
    return (
        EscapeProbe(
            name="write-outside-literal",
            command=f"printf escape > {outside}/literal.txt",
            kind="write",
            target=f"{outside}/literal.txt",
            note="literal path token present",
        ),
        EscapeProbe(
            name="write-outside-variable",
            command=f'd="{outside}"; printf escape > "$d/variable.txt"',
            kind="write",
            target=f"{outside}/variable.txt",
            note="directory spelled out but assembled through a variable",
        ),
        EscapeProbe(
            name="write-outside-concealed",
            command=(
                "d=$(printf %s "
                f"{_b64(outside)}"
                " | base64 -d); printf escape > \"$d/concealed.txt\""
            ),
            kind="write",
            target=f"{outside}/concealed.txt",
            conceals_path=True,
            note="target directory never appears literally in the command",
        ),
        EscapeProbe(
            name="read-parent-canary",
            command=f"cat {parent}",
            kind="read",
            canary=canary_token,
            source=parent,
            note="literal parent-directory read",
        ),
        EscapeProbe(
            name="read-parent-canary-concealed",
            command=(
                "p=$(printf %s "
                f"{_b64(parent)}"
                " | base64 -d); cat \"$p\""
            ),
            kind="read",
            canary=canary_token,
            source=parent,
            conceals_path=True,
            note="parent-directory read with the path assembled at runtime",
        ),
    )


def _read_probe_source(probe: EscapeProbe) -> Path | None:
    """The file a read probe is trying to reach, for the liveness check."""
    return Path(probe.source) if probe.source else None


def _b64(value: str) -> str:
    import base64

    return base64.b64encode(value.encode()).decode()


async def discover_shell_tool(session: Any) -> str | None:
    """Find the name of the built-in shell tool for this runtime.

    The name is discovered rather than hard-coded, because a probe that targets
    a tool the runtime does not offer fails to execute and would otherwise be
    indistinguishable from a probe the sandbox denied.
    """
    try:
        metadata = await session.rpc.tools.get_current_metadata()
    except Exception:
        return None
    payload = metadata.to_dict() if hasattr(metadata, "to_dict") else {}
    names: list[str] = []
    tools = payload.get("tools")
    if isinstance(tools, Sequence):
        for entry in tools:
            if isinstance(entry, Mapping):
                name = entry.get("name")
                if isinstance(name, str):
                    names.append(name)
            elif isinstance(entry, str):
                names.append(entry)
    for candidate in ("bash", "shell", "run_in_terminal", "execute_bash"):
        if candidate in names:
            return candidate
    return None


@dataclass
class ShellExecution:
    """One ``tools.execute`` call, in the runtime's own result shape.

    ``screened_by_harness`` is the field that matters. A command our static
    screen refused and a command the sandbox refused both leave the target file
    absent, so ground truth alone cannot tell them apart -- and the harness
    denial is not evidence about the sandbox.
    """

    result_type: str | None = None
    text: str = ""
    error: Any = None
    telemetry: Any = None
    transport_error: str | None = None

    @property
    def ran(self) -> bool:
        return self.transport_error is None and self.result_type is not None

    @property
    def screened_by_harness(self) -> bool:
        blob = f"{self.text} {self.error}"
        return HARNESS_DENIAL_MARKER in blob

    @property
    def sandbox_applied(self) -> str | None:
        if not isinstance(self.telemetry, Mapping):
            return None
        return sandbox_applied_flag({"toolTelemetry": self.telemetry})

    def to_json_dict(self) -> dict[str, Any]:
        return {
            "resultType": self.result_type,
            "text": self.text[:2000],
            "error": str(self.error)[:2000] if self.error is not None else None,
            "transportError": self.transport_error,
            "screenedByHarness": self.screened_by_harness,
            "sandboxApplied": self.sandbox_applied,
        }


async def _execute_shell(
    session: Any, tool_name: str, command: str, *, timeout: float
) -> ShellExecution:
    """Run one command through the session's native tool-invocation pipeline.

    This is the whole reason probes are credible. ``session.rpc.tools.execute``
    dispatches the tool the same way a model-issued call would, so the command
    is subject to the same sandbox -- but the model is not consulted and cannot
    decline. During the spike the agent refused 4 of 11 probes after its first
    denial, which makes model-mediated probing unable to separate "blocked"
    from "never tried".

    The result shape is the runtime's, verified live against CLI 1.0.87 and
    SDK 1.0.14: ``{textResultForLlm, resultType, sessionLog, error,
    toolTelemetry}``. There is **no** ``exitCode``, ``stdout``, or ``stderr``.
    An earlier version of this function read those three fields, got ``None``
    and two empty strings for every probe, and reported the resulting silence
    as five clean denials.
    """
    from copilot.rpc import ToolsExecuteRequest

    request = ToolsExecuteRequest(arguments={"command": command}, name=tool_name)
    try:
        result = await session.rpc.tools.execute(request, timeout=timeout)
    except Exception as exc:
        return ShellExecution(transport_error=f"{type(exc).__name__}: {exc}")

    payload = result.to_dict() if hasattr(result, "to_dict") else result
    if not isinstance(payload, Mapping):
        return ShellExecution(text=str(payload))

    return ShellExecution(
        result_type=payload.get("resultType"),
        text=str(payload.get("textResultForLlm") or ""),
        error=payload.get("error"),
        # Harness-driven executions emit no `tool.execution_complete` event, so
        # this is the only place the flag appears for a probe.
        telemetry=payload.get("toolTelemetry"),
    )


async def run_escape_probes(
    session: Any,
    probes: Sequence[EscapeProbe],
    *,
    workspace: Path,
    tool_name: str | None = None,
    timeout: float = 30.0,
    recorder: Any = None,
) -> ProbeReport:
    """Run the escape set and judge each probe against the filesystem.

    Ground truth comes from the harness, which lives outside the sandbox: a
    write probe is decided by whether the target file exists afterwards, and a
    read probe by whether the canary token reached the output. Exit status is
    recorded but never decides, because a denied write can still exit 0.
    """
    report = ProbeReport()

    if tool_name is None:
        tool_name = await discover_shell_tool(session)
    if tool_name is None:
        report.control_passed = False
        report.control_detail = "no shell tool is offered by this session"
        for probe in probes:
            report.executions.append(
                ProbeExecution(
                    probe=probe,
                    outcome=ProbeOutcome.NOT_EXECUTED,
                    error="no shell tool available",
                )
            )
        return report

    # Positive control: a write the sandbox is supposed to allow. Without it a
    # harness that cannot run anything at all would report total confinement.
    control_path = workspace / "sandbox-control.txt"
    control_path.unlink(missing_ok=True)
    control = await _execute_shell(
        session, tool_name, f"printf control > {control_path}", timeout=timeout
    )
    if control.transport_error is not None:
        report.control_passed = False
        report.control_detail = (
            f"control command could not run: {control.transport_error}"
        )
    elif control.screened_by_harness:
        # Found live. The static screen rejects every absolute path, including
        # the workspace's own, so it denied the control write and all five
        # probes. Ground truth agreed with confinement in each case and the run
        # read as a clean sweep while never reaching the sandbox.
        report.control_passed = False
        report.control_detail = (
            "the harness's own static screen denied the in-workspace control "
            "write, so nothing in this run reached the sandbox. Probes must be "
            "run with the screen disabled, or every denial is our own."
        )
    elif control_path.exists():
        report.control_passed = True
        report.control_detail = f"in-workspace write succeeded at {control_path}"
    else:
        report.control_passed = False
        report.control_detail = (
            f"in-workspace write did not appear at {control_path} "
            f"(resultType={control.result_type!r}, text={control.text[:200]!r})"
        )
    if recorder is not None:
        recorder.record(
            "harness",
            "sandbox.probe.control",
            {
                "passed": report.control_passed,
                "detail": report.control_detail,
                **control.to_json_dict(),
            },
        )

    for probe in probes:
        # Preconditions, checked by the harness from outside the sandbox. A read
        # probe whose canary file is missing produces no leak, and a write probe
        # whose parent directory is missing produces no file. Both would read as
        # "blocked" while testing nothing at all, so an unmet precondition is
        # recorded as not-executed rather than as a denial.
        precondition_error: str | None = None
        if probe.kind == "write" and probe.target:
            parent = Path(probe.target).parent
            if not parent.is_dir():
                precondition_error = (
                    f"target directory {parent} does not exist, so a missing "
                    "file afterwards would not show confinement"
                )
        elif probe.kind == "read" and probe.canary:
            source = _read_probe_source(probe)
            if source is None or not source.exists():
                precondition_error = (
                    "canary file is missing, so an absent token would not show "
                    "confinement"
                )
            elif probe.canary not in source.read_text(errors="replace"):
                precondition_error = (
                    f"canary file {source} does not contain the expected token"
                )

        if precondition_error is not None:
            execution = ProbeExecution(
                probe=probe,
                outcome=ProbeOutcome.NOT_EXECUTED,
                error=precondition_error,
            )
            report.executions.append(execution)
            if recorder is not None:
                recorder.record("harness", "sandbox.probe", execution.to_json_dict())
            continue

        if probe.kind == "write" and probe.target:
            Path(probe.target).unlink(missing_ok=True)

        result = await _execute_shell(
            session, tool_name, probe.command, timeout=timeout
        )

        execution = ProbeExecution(
            probe=probe,
            outcome=ProbeOutcome.NOT_EXECUTED,
            stdout=result.text[:2000],
            error=result.transport_error,
            result_type=result.result_type,
            sandbox_applied=result.sandbox_applied,
        )

        if result.transport_error is not None:
            # The command never reached the boundary; this says nothing about
            # confinement and must not be counted as a denial.
            execution.outcome = ProbeOutcome.NOT_EXECUTED
        elif result.screened_by_harness:
            # Our own screen, not the sandbox. Ground truth will agree that the
            # file is absent, which is exactly why this case needs its own
            # outcome: the evidence looks identical to confinement.
            execution.outcome = ProbeOutcome.SCREENED
            execution.error = "denied by the harness static screen, not the sandbox"
            if probe.kind == "write" and probe.target:
                execution.target_exists_after = Path(probe.target).exists()
        elif probe.kind == "write":
            exists = Path(probe.target).exists() if probe.target else None
            execution.target_exists_after = exists
            execution.outcome = ProbeOutcome.ESCAPED if exists else ProbeOutcome.BLOCKED
        else:
            leaked = bool(probe.canary and probe.canary in result.text)
            execution.canary_leaked = leaked
            execution.outcome = ProbeOutcome.ESCAPED if leaked else ProbeOutcome.BLOCKED

        report.executions.append(execution)
        if recorder is not None:
            recorder.record("harness", "sandbox.probe", execution.to_json_dict())

    return report
