"""Runtime adapters for invoking persistent Hermes profiles.

The orchestration layer owns deliberation state.  This module deliberately keeps
the runtime contract small: one invocation goes to one fresh subprocess and
returns only the profile's final response plus a session identifier.  Hermes
memory and tools remain available through the explicitly enabled toolsets.
"""

from __future__ import annotations

import asyncio
import hashlib
import inspect
import json
import os
import re
import signal
import subprocess
import sys
from collections import deque
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator


_SESSION_ID_PATTERNS = (
    re.compile(
        r"(?im)^[^\r\n]*?\bsession[ _-]?id\b\s*[:=]\s*[\"']?"
        r"(?P<session>[A-Za-z0-9][A-Za-z0-9._:-]*)"
    ),
    re.compile(
        r"(?im)[\"']session_id[\"']\s*:\s*[\"']"
        r"(?P<session>[A-Za-z0-9][A-Za-z0-9._:-]*)[\"']"
    ),
)
_SENSITIVE_NAME = re.compile(
    r"(?i)(?:api[_-]?key|token|secret|password|passwd|credential|authorization)"
)
_SENSITIVE_ASSIGNMENT = re.compile(
    r"(?i)\b(api[_-]?key|access[_-]?token|refresh[_-]?token|token|secret|"
    r"password|passwd|credential|authorization)\b(\s*[:=]\s*)"
    r"([^\s,;]+)"
)
_BEARER_TOKEN = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/=-]+")
_URL_CREDENTIALS = re.compile(r"(https?://)([^/@\s:]+):([^/@\s]+)@")
_PROFILE_NAME = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_BRIDGE_ERROR_CODE = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{0,63}$")
_BRIDGE_PROTOCOL = "hsa-hermes-ndjson"
_BRIDGE_PROTOCOL_VERSION = 1
_REQUIRED_BRIDGE_CAPABILITIES = frozenset(
    {
        "response_delta",
        "tool_events",
        "session_resume",
        "profile_context",
        "ephemeral_system_prompt",
        "scoped_toolsets",
        "max_turns",
        "graceful_interrupt",
    }
)
_BRIDGE_FRAME_TYPES = Literal[
    "bridge.ready",
    "response.delta",
    "tool.started",
    "tool.completed",
    "response.completed",
    "bridge.error",
]


def redact_sensitive(text: str, *, secrets: Iterable[str] = ()) -> str:
    """Return diagnostics with common credential forms removed.

    Explicit secret values are replaced before pattern-based redaction.  The
    function is intentionally conservative because runtime errors may include
    provider diagnostics or an echoed prompt.
    """

    redacted = text
    values = sorted(
        {value for value in secrets if isinstance(value, str) and value},
        key=len,
        reverse=True,
    )
    for value in values:
        redacted = redacted.replace(value, "[REDACTED]")
    redacted = _BEARER_TOKEN.sub(r"\1[REDACTED]", redacted)
    redacted = _URL_CREDENTIALS.sub(r"\1[REDACTED]@", redacted)
    redacted = _SENSITIVE_ASSIGNMENT.sub(
        lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", redacted
    )
    return redacted


class AgentInvocation(BaseModel):
    """A single, phase-scoped call to an HSA runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    invocation_id: str = Field(min_length=1)
    hsa_id: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    system_prompt: str = ""
    user_prompt: str = Field(min_length=1)
    enabled_toolsets: tuple[str, ...] = ()
    load_profile_context: bool = True
    max_turns: int | None = Field(default=None, ge=1)
    timeout_seconds: float | None = Field(default=None, gt=0)
    resume_session_id: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @field_validator("enabled_toolsets")
    @classmethod
    def _normalise_toolsets(cls, values: tuple[str, ...]) -> tuple[str, ...]:
        seen: set[str] = set()
        normalised: list[str] = []
        for value in values:
            name = value.strip()
            if not name:
                raise ValueError("enabled toolset names cannot be empty")
            if name not in seen:
                normalised.append(name)
                seen.add(name)
        return tuple(normalised)


class RawAgentResponse(BaseModel):
    """The unparsed final response returned by an agent runtime."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    content: str
    session_id: str | None = None
    runtime: str
    profile: str | None = None
    tool_artifacts: tuple[dict[str, Any], ...] = ()
    tool_events: tuple[dict[str, Any], ...] = ()
    raw_messages: tuple[dict[str, Any], ...] = ()
    metadata: dict[str, Any] = Field(default_factory=dict)


class HermesBridgeCapabilities(BaseModel):
    """Result of the bridge's no-model compatibility check."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Literal["hsa-hermes-ndjson"]
    protocol_version: Literal[1]
    bridge_version: str = Field(min_length=1)
    available: bool
    capabilities: tuple[str, ...] = ()
    reason: str = ""


class RuntimeStreamEvent(BaseModel):
    """A non-durable, machine-readable event emitted during one invocation.

    The sink is intentionally synchronous and must return immediately.  A UI
    should enqueue these events with ``put_nowait`` and apply its own bounded
    delta coalescing rather than blocking a subprocess pipe reader.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    event_type: Literal[
        "bridge_ready",
        "response_delta",
        "tool_started",
        "tool_completed",
        "response_completed",
    ]
    sequence: int = Field(ge=1)
    invocation_id: str = Field(min_length=1)
    hsa_id: str = Field(min_length=1)
    phase: str = Field(min_length=1)
    content: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


RuntimeEventSink = Callable[[RuntimeStreamEvent], None]


class _BridgeFrame(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    protocol: Literal["hsa-hermes-ndjson"]
    protocol_version: Literal[1]
    sequence: int = Field(ge=1)
    type: _BRIDGE_FRAME_TYPES
    data: dict[str, Any]


@runtime_checkable
class AgentRuntime(Protocol):
    """Asynchronous runtime contract used by the orchestrator."""

    async def invoke(
        self,
        invocation: AgentInvocation,
        *,
        event_sink: RuntimeEventSink | None = None,
    ) -> RawAgentResponse:
        """Execute one invocation without sharing a subprocess."""


class AgentRuntimeError(RuntimeError):
    """A sanitised Hermes process failure safe to include in an audit trace."""

    def __init__(
        self,
        message: str,
        *,
        profile: str | None = None,
        returncode: int | None = None,
    ) -> None:
        super().__init__(message)
        self.profile = profile
        self.returncode = returncode


class AgentRuntimeTimeout(AgentRuntimeError):
    """Raised after a Hermes subprocess has been forcibly killed on timeout."""


class _BridgeHandshakeError(RuntimeError):
    """Raised before an invocation request is dispatched to Hermes."""


@dataclass
class _BridgeAccumulator:
    content: str | None = None
    session_id: str | None = None
    tool_events: list[dict[str, Any]] = field(default_factory=list)
    error_code: str | None = None


def _command_tuple(command: str | os.PathLike[str] | Sequence[str]) -> tuple[str, ...]:
    if isinstance(command, (str, os.PathLike)):
        values = (os.fspath(command),)
    else:
        values = tuple(os.fspath(value) for value in command)
    if not values or any(not value for value in values):
        raise ValueError("profile command cannot be empty")
    return values


def _parse_session_id(stderr: str) -> str | None:
    for pattern in _SESSION_ID_PATTERNS:
        match = pattern.search(stderr)
        if match:
            return match.group("session")
    return None


def _without_terminal_newline(value: str) -> str:
    if value.endswith("\r\n"):
        return value[:-2]
    if value.endswith(("\r", "\n")):
        return value[:-1]
    return value


def _safe_bridge_error_code(value: object) -> str:
    """Collapse untrusted bridge diagnostics to a non-secret category."""

    if isinstance(value, str) and _BRIDGE_ERROR_CODE.fullmatch(value):
        return value
    return "bridge_error"


def _subprocess_isolation() -> dict[str, Any]:
    """Return flags that put one invocation in its own process group."""

    if os.name == "posix":
        return {"start_new_session": True}
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {}


class HermesProfileRuntime:
    """Invoke an isolated Hermes profile wrapper in quiet, single-query mode.

    ``profile`` is normally the wrapper name created by ``hermes profile
    alias``. ``executable`` remains a compatibility source for deriving the
    profile label, while actual calls always launch the project bridge and send
    prompt data over stdin so it never appears in operating-system argv.

    A Hermes profile is a state boundary, not a security sandbox.  High-risk
    terminal gating therefore belongs in :mod:`hsa_thinktank.tool_policy`.
    """

    name = "hermes-profile"

    def __init__(
        self,
        profile: str | os.PathLike[str] | Sequence[str] | None = None,
        *,
        executable: str | os.PathLike[str] | Sequence[str] | None = None,
        max_turns: int = 10,
        timeout_seconds: float = 120.0,
        cwd: str | os.PathLike[str] | None = None,
        environment: Mapping[str, str] | None = None,
        inherit_environment: bool = True,
        profile_home: str | os.PathLike[str] | None = None,
        bridge_command: str | os.PathLike[str] | Sequence[str] | None = None,
        bridge_check_timeout_seconds: float = 10.0,
        shutdown_grace_seconds: float = 2.0,
    ) -> None:
        if profile is None and executable is None:
            raise ValueError("profile or executable is required")
        if max_turns < 1:
            raise ValueError("max_turns must be at least 1")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        if bridge_check_timeout_seconds <= 0:
            raise ValueError("bridge_check_timeout_seconds must be positive")
        if shutdown_grace_seconds < 0:
            raise ValueError("shutdown_grace_seconds cannot be negative")

        command_source = executable if executable is not None else profile
        assert command_source is not None
        self._command = _command_tuple(command_source)
        if profile is None:
            self.profile = Path(self._command[0]).name
        elif isinstance(profile, (str, os.PathLike)):
            self.profile = os.fspath(profile)
        else:
            self.profile = " ".join(os.fspath(part) for part in profile)
        self.max_turns = max_turns
        self.timeout_seconds = timeout_seconds
        self.cwd = os.fspath(cwd) if cwd is not None else None
        self.environment = dict(environment or {})
        self.inherit_environment = inherit_environment
        self.profile_home = os.fspath(profile_home) if profile_home is not None else None
        default_bridge = (
            sys.executable,
            str(Path(__file__).with_name("hermes_event_bridge.py").resolve()),
        )
        self._bridge_command = _command_tuple(bridge_command or default_bridge)
        self.bridge_check_timeout_seconds = bridge_check_timeout_seconds
        self.shutdown_grace_seconds = shutdown_grace_seconds
        self._bridge_capabilities: HermesBridgeCapabilities | None = None

    def build_command(self, invocation: AgentInvocation) -> list[str]:
        """Build the secure final-only helper command without prompt-bearing argv."""

        if not invocation.enabled_toolsets:
            raise ValueError(
                "enabled_toolsets cannot be empty; refusing to fall back to the "
                "profile's unscoped default tools"
            )
        return self.build_bridge_command(final_only=True)

    def build_environment(self, invocation: AgentInvocation) -> dict[str, str]:
        """Construct the child environment, including the ephemeral soul prompt."""

        environment = self._base_environment()
        environment["HERMES_EPHEMERAL_SYSTEM_PROMPT"] = invocation.system_prompt
        return environment

    def build_request(self, invocation: AgentInvocation) -> dict[str, Any]:
        """Build the stdin request shared by streaming and final-only helpers."""

        if not invocation.enabled_toolsets:
            raise ValueError(
                "enabled_toolsets cannot be empty; refusing to fall back to the "
                "profile's unscoped default tools"
            )
        return {
            "protocol": _BRIDGE_PROTOCOL,
            "type": "invoke",
            "protocol_version": _BRIDGE_PROTOCOL_VERSION,
            "invocation_id": invocation.invocation_id,
            "prompt": invocation.user_prompt,
            "toolsets": list(invocation.enabled_toolsets),
            "max_turns": invocation.max_turns or self.max_turns,
            "load_profile_context": invocation.load_profile_context,
            "resume_session_id": invocation.resume_session_id,
        }

    def build_bridge_command(
        self,
        *,
        check: bool = False,
        final_only: bool = False,
    ) -> list[str]:
        """Return the bridge command used for capability checks or one invocation."""

        if check and final_only:
            raise ValueError("bridge command cannot be both check and final-only")
        mode = "--check" if check else "--final-only" if final_only else "--serve-once"
        return [*self._bridge_command, mode]

    async def check_streaming_capability(
        self,
        *,
        force: bool = False,
    ) -> HermesBridgeCapabilities:
        """Check bridge compatibility without constructing an agent or calling a model."""

        if self._bridge_capabilities is not None and not force:
            return self._bridge_capabilities
        profile_home = self._resolve_profile_home()
        if profile_home is None:
            result = self._unavailable_capabilities("Hermes profile home could not be resolved")
            self._bridge_capabilities = result
            return result
        if not profile_home.is_dir():
            result = self._unavailable_capabilities(
                f"Hermes profile home does not exist: {profile_home}"
            )
            self._bridge_capabilities = result
            return result

        environment = self._base_environment()
        environment["HERMES_HOME"] = str(profile_home)
        secrets = tuple(
            value for key, value in environment.items() if value and _SENSITIVE_NAME.search(key)
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *self.build_bridge_command(check=True),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=environment,
                **_subprocess_isolation(),
            )
        except (OSError, ValueError) as exc:
            result = self._unavailable_capabilities(
                f"Hermes bridge check could not start: {redact_sensitive(str(exc), secrets=secrets)}"
            )
            self._bridge_capabilities = result
            return result

        communicate = asyncio.create_task(process.communicate())
        try:
            stdout_bytes, _stderr_bytes = await asyncio.wait_for(
                asyncio.shield(communicate),
                timeout=self.bridge_check_timeout_seconds,
            )
        except TimeoutError:
            await self._terminate_process(process, communicate)
            result = self._unavailable_capabilities("Hermes bridge check timed out")
            self._bridge_capabilities = result
            return result
        except asyncio.CancelledError:
            await self._terminate_process(process, communicate)
            raise

        stdout = stdout_bytes.decode("utf-8", errors="replace").strip()
        try:
            lines = [line for line in stdout.splitlines() if line.strip()]
            if len(lines) != 1:
                raise ValueError("bridge check must return exactly one JSON line")
            result = HermesBridgeCapabilities.model_validate_json(lines[0])
        except Exception as exc:
            result = self._unavailable_capabilities(
                "Hermes bridge check returned an invalid handshake "
                f"({_safe_bridge_error_code(type(exc).__name__)})"
            )
        if not result.available:
            # ``reason`` crosses a child-process trust boundary.  Even a
            # profile-loaded provider error must never become parent audit data.
            result = self._unavailable_capabilities("Hermes bridge reported unavailable")
        if process.returncode != 0 and result.available:
            result = self._unavailable_capabilities(
                f"Hermes bridge check failed with status {process.returncode}"
            )
        missing = (
            sorted(_REQUIRED_BRIDGE_CAPABILITIES - set(result.capabilities))
            if result.available
            else []
        )
        if missing:
            result = self._unavailable_capabilities(
                f"Hermes bridge is missing capabilities: {', '.join(missing)}"
            )
        self._bridge_capabilities = result
        return result

    async def invoke(
        self,
        invocation: AgentInvocation,
        *,
        event_sink: RuntimeEventSink | None = None,
    ) -> RawAgentResponse:
        if not invocation.enabled_toolsets:
            raise ValueError(
                "enabled_toolsets cannot be empty; refusing to fall back to the "
                "profile's unscoped default tools"
            )
        if event_sink is None:
            return await self._invoke_final_only(invocation)

        capabilities = await self.check_streaming_capability()
        if not capabilities.available:
            return await self._invoke_final_only_fallback(
                invocation,
                event_sink=event_sink,
                reason=capabilities.reason or "Hermes bridge is unavailable",
            )
        try:
            return await self._invoke_bridge(invocation, event_sink=event_sink)
        except _BridgeHandshakeError as exc:
            # The bridge waits for a validated request before constructing an
            # agent, so a handshake failure is the only safe place to fall back
            # without risking a duplicate paid model call.
            return await self._invoke_final_only_fallback(
                invocation,
                event_sink=event_sink,
                reason=str(exc),
            )

    async def _invoke_final_only(self, invocation: AgentInvocation) -> RawAgentResponse:
        profile_home = self._resolve_profile_home()
        if profile_home is None:
            raise AgentRuntimeError(
                "Hermes profile home could not be resolved for secure final-only invocation",
                profile=self.profile,
            )
        command = self.build_command(invocation)
        environment = self.build_environment(invocation)
        environment["HERMES_HOME"] = str(profile_home)
        timeout = invocation.timeout_seconds or self.timeout_seconds
        secrets = self._sensitive_values(invocation, environment)
        request_bytes = (
            json.dumps(self.build_request(invocation), ensure_ascii=False, sort_keys=True) + "\n"
        ).encode("utf-8")

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=environment,
                **_subprocess_isolation(),
            )
        except (OSError, ValueError) as exc:
            detail = redact_sensitive(str(exc), secrets=secrets)
            raise AgentRuntimeError(
                f"Hermes profile process could not be started: {detail}",
                profile=self.profile,
            ) from None

        communicate = asyncio.create_task(process.communicate(request_bytes))
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                asyncio.shield(communicate), timeout=timeout
            )
        except TimeoutError:
            await self._terminate_process(process, communicate)
            raise AgentRuntimeTimeout(
                f"Hermes profile invocation timed out after {timeout:g} seconds",
                profile=self.profile,
            ) from None
        except asyncio.CancelledError:
            await self._terminate_process(process, communicate)
            raise

        stdout = stdout_bytes.decode("utf-8", errors="replace")
        stderr = stderr_bytes.decode("utf-8", errors="replace")
        if process.returncode != 0:
            raise AgentRuntimeError(
                f"Hermes secure final-only helper exited with status {process.returncode}",
                profile=self.profile,
                returncode=process.returncode,
            )

        return RawAgentResponse(
            content=_without_terminal_newline(stdout),
            session_id=_parse_session_id(stderr),
            runtime="hermes-profile",
            profile=self.profile,
            metadata={"phase": invocation.phase, "stream_mode": "final-only"},
        )

    async def _invoke_final_only_fallback(
        self,
        invocation: AgentInvocation,
        *,
        event_sink: RuntimeEventSink,
        reason: str,
    ) -> RawAgentResponse:
        response = await self._invoke_final_only(invocation)
        self._emit_stream_event(
            event_sink,
            RuntimeStreamEvent(
                event_type="response_completed",
                sequence=1,
                invocation_id=invocation.invocation_id,
                hsa_id=invocation.hsa_id,
                phase=invocation.phase,
                content=response.content,
                payload={"session_id": response.session_id, "stream_mode": "final-only"},
            ),
        )
        return response.model_copy(
            update={
                "metadata": {
                    **response.metadata,
                    "stream_fallback_reason": reason,
                }
            }
        )

    async def _invoke_bridge(
        self,
        invocation: AgentInvocation,
        *,
        event_sink: RuntimeEventSink,
    ) -> RawAgentResponse:
        profile_home = self._resolve_profile_home()
        if profile_home is None:
            raise _BridgeHandshakeError("Hermes profile home could not be resolved")
        environment = self.build_environment(invocation)
        environment["HERMES_HOME"] = str(profile_home)
        timeout = invocation.timeout_seconds or self.timeout_seconds
        deadline = asyncio.get_running_loop().time() + timeout
        secrets = self._sensitive_values(invocation, environment)
        try:
            process = await asyncio.create_subprocess_exec(
                *self.build_bridge_command(),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=self.cwd,
                env=environment,
                **_subprocess_isolation(),
            )
        except (OSError, ValueError) as exc:
            detail = redact_sensitive(str(exc), secrets=secrets)
            raise _BridgeHandshakeError(f"Hermes bridge could not be started: {detail}") from None

        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        stderr_task = asyncio.create_task(process.stderr.read())
        ready_task = asyncio.create_task(process.stdout.readline())
        try:
            remaining = max(0.001, deadline - asyncio.get_running_loop().time())
            ready_bytes = await asyncio.wait_for(
                asyncio.shield(ready_task),
                timeout=min(self.bridge_check_timeout_seconds, remaining),
            )
            ready = self._decode_bridge_frame(ready_bytes)
            if ready.type != "bridge.ready":
                raise ValueError(f"expected bridge.ready, received {ready.type}")
            if ready.sequence != 1:
                raise ValueError(f"bridge.ready must have sequence 1, received {ready.sequence}")
            ready_capabilities = ready.data.get("capabilities")
            if not isinstance(ready_capabilities, list) or any(
                not isinstance(item, str) for item in ready_capabilities
            ):
                raise ValueError("bridge.ready requires a string capability list")
            missing = sorted(_REQUIRED_BRIDGE_CAPABILITIES - set(ready_capabilities))
            if missing:
                raise ValueError(
                    f"bridge.ready is missing capabilities: {', '.join(missing)}"
                )
        except asyncio.CancelledError:
            await self._terminate_process(process, ready_task, stderr_task)
            raise
        except Exception as exc:
            await self._terminate_process(process, ready_task, stderr_task)
            code = _safe_bridge_error_code(type(exc).__name__)
            raise _BridgeHandshakeError(f"Hermes bridge handshake failed ({code})") from None

        self._emit_stream_event(
            event_sink,
            RuntimeStreamEvent(
                event_type="bridge_ready",
                sequence=ready.sequence,
                invocation_id=invocation.invocation_id,
                hsa_id=invocation.hsa_id,
                phase=invocation.phase,
                payload={"capabilities": sorted(set(ready_capabilities))},
            ),
        )
        request = self.build_request(invocation)
        try:
            process.stdin.write(
                (json.dumps(request, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")
            )
            await process.stdin.drain()
            process.stdin.close()
            await process.stdin.wait_closed()
        except asyncio.CancelledError:
            await self._terminate_process(process, stderr_task)
            raise
        except Exception as exc:
            await self._terminate_process(process, stderr_task)
            # Once writing begins the child may already have accepted the
            # invocation and called a paid model. Never retry via the quiet CLI
            # from this point onward; doing so could duplicate the decision and
            # its cost.
            detail = redact_sensitive(str(exc), secrets=secrets)
            raise AgentRuntimeError(
                f"Hermes bridge failed while dispatching the request: {detail}",
                profile=self.profile,
            ) from None

        accumulator = _BridgeAccumulator()
        stdout_task = asyncio.create_task(
            self._consume_bridge_frames(
                process.stdout,
                invocation=invocation,
                event_sink=event_sink,
                accumulator=accumulator,
                previous_sequence=ready.sequence,
            )
        )
        completion = asyncio.gather(stdout_task, stderr_task, process.wait())
        try:
            remaining = max(0.001, deadline - asyncio.get_running_loop().time())
            _, _stderr_bytes, returncode = await asyncio.wait_for(
                asyncio.shield(completion), timeout=remaining
            )
        except TimeoutError:
            await self._terminate_process(process, completion)
            raise AgentRuntimeTimeout(
                f"Hermes profile invocation timed out after {timeout:g} seconds",
                profile=self.profile,
            ) from None
        except asyncio.CancelledError:
            await self._terminate_process(process, completion)
            raise
        except Exception as exc:
            await self._terminate_process(process, completion)
            code = _safe_bridge_error_code(type(exc).__name__)
            raise AgentRuntimeError(
                f"Hermes bridge protocol failed after dispatch ({code})",
                profile=self.profile,
            ) from None

        if returncode != 0 or accumulator.error_code:
            code = accumulator.error_code or "bridge_process_error"
            raise AgentRuntimeError(
                f"Hermes bridge exited with status {returncode} ({code})",
                profile=self.profile,
                returncode=returncode,
            )
        if accumulator.content is None:
            raise AgentRuntimeError(
                "Hermes bridge completed without response.completed",
                profile=self.profile,
                returncode=returncode,
            )
        return RawAgentResponse(
            content=accumulator.content,
            session_id=accumulator.session_id,
            runtime="hermes-profile",
            profile=self.profile,
            tool_events=tuple(accumulator.tool_events),
            metadata={
                "phase": invocation.phase,
                "stream_mode": "ndjson",
                "bridge_protocol": _BRIDGE_PROTOCOL,
                "bridge_protocol_version": _BRIDGE_PROTOCOL_VERSION,
            },
        )

    async def _consume_bridge_frames(
        self,
        stream: asyncio.StreamReader,
        *,
        invocation: AgentInvocation,
        event_sink: RuntimeEventSink,
        accumulator: _BridgeAccumulator,
        previous_sequence: int,
    ) -> None:
        sequence = previous_sequence
        async for raw_line in stream:
            frame = self._decode_bridge_frame(raw_line)
            if frame.sequence != sequence + 1:
                raise ValueError(
                    f"bridge event sequence jumped from {sequence} to {frame.sequence}"
                )
            sequence = frame.sequence
            if frame.type == "bridge.ready":
                raise ValueError("bridge emitted bridge.ready more than once")
            if frame.type == "response.delta":
                text = frame.data.get("text")
                if not isinstance(text, str) or not text:
                    raise ValueError("response.delta requires non-empty text")
                event = RuntimeStreamEvent(
                    event_type="response_delta",
                    sequence=frame.sequence,
                    invocation_id=invocation.invocation_id,
                    hsa_id=invocation.hsa_id,
                    phase=invocation.phase,
                    content=text,
                )
            elif frame.type in {"tool.started", "tool.completed"}:
                event_name = "tool_started" if frame.type == "tool.started" else "tool_completed"
                accumulator.tool_events.append({"event_type": frame.type, **frame.data})
                event = RuntimeStreamEvent(
                    event_type=event_name,
                    sequence=frame.sequence,
                    invocation_id=invocation.invocation_id,
                    hsa_id=invocation.hsa_id,
                    phase=invocation.phase,
                    payload=frame.data,
                )
            elif frame.type == "response.completed":
                content = frame.data.get("content")
                session_id = frame.data.get("session_id")
                if not isinstance(content, str) or not content:
                    raise ValueError("response.completed requires non-empty content")
                if session_id is not None and not isinstance(session_id, str):
                    raise ValueError("response.completed session_id must be a string")
                if accumulator.content is not None:
                    raise ValueError("bridge emitted response.completed more than once")
                accumulator.content = content
                accumulator.session_id = session_id
                event = RuntimeStreamEvent(
                    event_type="response_completed",
                    sequence=frame.sequence,
                    invocation_id=invocation.invocation_id,
                    hsa_id=invocation.hsa_id,
                    phase=invocation.phase,
                    content=content,
                    payload={"session_id": session_id, "stream_mode": "ndjson"},
                )
            elif frame.type == "bridge.error":
                code = frame.data.get("code")
                accumulator.error_code = _safe_bridge_error_code(code)
                continue
            else:  # pragma: no cover - Pydantic rejects unknown frame types first
                raise ValueError(f"unknown bridge frame type: {frame.type}")
            self._emit_stream_event(event_sink, event)

    @staticmethod
    def _decode_bridge_frame(raw_line: bytes) -> _BridgeFrame:
        if not raw_line:
            raise ValueError("bridge closed before emitting a frame")
        try:
            return _BridgeFrame.model_validate_json(raw_line)
        except Exception as exc:
            raise ValueError(f"invalid bridge NDJSON frame: {exc}") from None

    @staticmethod
    def _emit_stream_event(event_sink: RuntimeEventSink, event: RuntimeStreamEvent) -> None:
        try:
            event_sink(event)
        except Exception:
            # UI telemetry must never turn a valid paid invocation into a
            # runtime failure.  Consumers are responsible for their own logs.
            pass

    def _base_environment(self) -> dict[str, str]:
        environment = dict(os.environ) if self.inherit_environment else {}
        environment.update(self.environment)
        return environment

    def _resolve_profile_home(self) -> Path | None:
        explicit = (
            self.profile_home
            or self.environment.get("HSA_HERMES_PROFILE_HOME")
            or self.environment.get("HERMES_HOME")
        )
        if explicit:
            return Path(explicit).expanduser().resolve()
        if not _PROFILE_NAME.fullmatch(self.profile):
            return None
        configured_home = os.environ.get("HERMES_HOME", "").strip()
        root = Path(configured_home).expanduser() if configured_home else Path.home() / ".hermes"
        try:
            root = root.resolve()
        except OSError:
            return None
        if root.parent.name == "profiles":
            root = root.parent.parent
        if self.profile == "default":
            return root
        candidate = root / "profiles" / self.profile
        return candidate if candidate.is_dir() else None

    @staticmethod
    def _unavailable_capabilities(reason: str) -> HermesBridgeCapabilities:
        return HermesBridgeCapabilities(
            protocol=_BRIDGE_PROTOCOL,
            protocol_version=_BRIDGE_PROTOCOL_VERSION,
            bridge_version="unknown",
            available=False,
            capabilities=(),
            reason=reason,
        )

    async def _terminate_process(
        self,
        process: asyncio.subprocess.Process,
        *waitables: Awaitable[Any],
    ) -> None:
        if process.returncode is None:
            try:
                if os.name == "posix":
                    os.killpg(process.pid, signal.SIGTERM)
                elif os.name == "nt" and hasattr(signal, "CTRL_BREAK_EVENT"):
                    process.send_signal(signal.CTRL_BREAK_EVENT)
                else:
                    process.terminate()
            except ProcessLookupError:
                pass
            try:
                await asyncio.wait_for(
                    asyncio.shield(process.wait()),
                    timeout=self.shutdown_grace_seconds,
                )
            except TimeoutError:
                try:
                    if os.name == "posix":
                        os.killpg(process.pid, signal.SIGKILL)
                    else:
                        process.kill()
                except ProcessLookupError:
                    pass
                await process.wait()
        if waitables:
            cleanup = asyncio.gather(*waitables, return_exceptions=True)
            try:
                await asyncio.wait_for(
                    asyncio.shield(cleanup),
                    timeout=max(1.0, self.shutdown_grace_seconds),
                )
            except TimeoutError:
                cleanup.cancel()
                await asyncio.gather(cleanup, return_exceptions=True)

    @staticmethod
    def _sensitive_values(
        invocation: AgentInvocation, environment: Mapping[str, str]
    ) -> tuple[str, ...]:
        values = {
            value for key, value in environment.items() if value and _SENSITIVE_NAME.search(key)
        }
        values.add(invocation.user_prompt)
        if invocation.system_prompt:
            values.add(invocation.system_prompt)
        explicit = invocation.metadata.get("sensitive_values", ())
        if isinstance(explicit, str):
            values.add(explicit)
        elif isinstance(explicit, Iterable):
            values.update(value for value in explicit if isinstance(value, str) and value)
        return tuple(values)


ScriptedValue = (
    RawAgentResponse
    | str
    | Mapping[str, Any]
    | BaseException
    | Callable[
        [AgentInvocation],
        RawAgentResponse
        | str
        | Mapping[str, Any]
        | Awaitable[RawAgentResponse | str | Mapping[str, Any]],
    ]
)


def _coerce_response(
    value: RawAgentResponse | str | Mapping[str, Any],
    *,
    runtime: str,
    profile: str | None,
) -> RawAgentResponse:
    if isinstance(value, RawAgentResponse):
        return value.model_copy(deep=True)
    if isinstance(value, str):
        return RawAgentResponse(content=value, runtime=runtime, profile=profile)
    payload = dict(value)
    if "content" in payload:
        payload.setdefault("runtime", runtime)
        payload.setdefault("profile", profile)
        return RawAgentResponse.model_validate(payload)
    return RawAgentResponse(
        content=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        runtime=runtime,
        profile=profile,
    )


class ScriptedRuntime:
    """Concurrency-safe queue or keyed script for offline protocol tests."""

    name = "scripted"

    def __init__(
        self,
        responses: Iterable[ScriptedValue] | Mapping[str, ScriptedValue],
        *,
        profile: str = "scripted",
    ) -> None:
        self.profile = profile
        if isinstance(responses, Mapping):
            self._keyed: dict[str, ScriptedValue] | None = dict(responses)
            self._responses: deque[ScriptedValue] | None = None
        else:
            self._keyed = None
            self._responses = deque(responses)
        self._lock = asyncio.Lock()
        self.invocations: list[AgentInvocation] = []

    async def invoke(
        self,
        invocation: AgentInvocation,
        *,
        event_sink: RuntimeEventSink | None = None,
    ) -> RawAgentResponse:
        del event_sink
        async with self._lock:
            self.invocations.append(invocation.model_copy(deep=True))
            if self._keyed is not None:
                key = invocation.metadata.get("script_key")
                if not isinstance(key, str) or key not in self._keyed:
                    raise AgentRuntimeError(
                        "No scripted response for invocation script_key",
                        profile=self.profile,
                    )
                value = self._keyed[key]
            else:
                assert self._responses is not None
                if not self._responses:
                    raise AgentRuntimeError(
                        "Scripted runtime response queue is exhausted",
                        profile=self.profile,
                    )
                value = self._responses.popleft()

        if isinstance(value, BaseException):
            raise value
        if callable(value):
            value = value(invocation)
            if inspect.isawaitable(value):
                value = await value
        return _coerce_response(value, runtime="scripted", profile=self.profile)


class DeterministicRuntime:
    """Pure local runtime whose default response is a stable invocation digest."""

    name = "deterministic"

    def __init__(
        self,
        responder: Callable[
            [AgentInvocation],
            RawAgentResponse
            | str
            | Mapping[str, Any]
            | Awaitable[RawAgentResponse | str | Mapping[str, Any]],
        ]
        | None = None,
        *,
        profile: str = "deterministic",
        namespace: str = "hsa-thinktank",
    ) -> None:
        self.profile = profile
        self.namespace = namespace
        self.responder = responder
        self.invocations: list[AgentInvocation] = []
        self._lock = asyncio.Lock()

    async def invoke(
        self,
        invocation: AgentInvocation,
        *,
        event_sink: RuntimeEventSink | None = None,
    ) -> RawAgentResponse:
        del event_sink
        async with self._lock:
            self.invocations.append(invocation.model_copy(deep=True))

        if self.responder is not None:
            value = self.responder(invocation)
            if inspect.isawaitable(value):
                value = await value
            return _coerce_response(value, runtime="deterministic", profile=self.profile)

        canonical = json.dumps(
            invocation.model_dump(mode="json", exclude_none=True),
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        digest = hashlib.sha256(f"{self.namespace}\0{canonical}".encode("utf-8")).hexdigest()
        content = json.dumps(
            {
                "digest": digest,
                "phase": invocation.phase,
                "runtime": "deterministic",
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return RawAgentResponse(
            content=content,
            session_id=f"det-{digest[:16]}",
            runtime="deterministic",
            profile=self.profile,
            metadata={"digest": digest},
        )


__all__ = [
    "AgentInvocation",
    "AgentRuntime",
    "AgentRuntimeError",
    "AgentRuntimeTimeout",
    "DeterministicRuntime",
    "HermesBridgeCapabilities",
    "HermesProfileRuntime",
    "RawAgentResponse",
    "RuntimeEventSink",
    "RuntimeStreamEvent",
    "ScriptedRuntime",
    "redact_sensitive",
]
