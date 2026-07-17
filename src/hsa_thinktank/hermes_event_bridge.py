"""One-shot Hermes bridge that exposes machine-readable NDJSON events.

The normal Hermes quiet CLI intentionally prints only the final response.  This
bridge runs the same ``AIAgent`` core with explicit, phase-scoped settings and
projects its callbacks onto a small stdout-only protocol.  All incidental
Hermes output is redirected to stderr so one malformed human-facing line can
never be mistaken for a runtime event.

The module is deliberately stdlib-only at import time.  When it is launched by
the HSA virtual environment it re-executes itself with the Hermes virtual
environment before importing Hermes internals.  ``--check`` performs imports
and signature checks only; it never creates an agent or calls a model.
"""

from __future__ import annotations

import argparse
import hashlib
import inspect
import json
import os
import re
import signal
import sys
import threading
from contextlib import redirect_stdout
from datetime import UTC, datetime
from io import StringIO
from pathlib import Path
from typing import Any, TextIO
from uuid import uuid4


BRIDGE_PROTOCOL = "hsa-hermes-ndjson"
BRIDGE_PROTOCOL_VERSION = 1
BRIDGE_VERSION = "1.0"
MAX_REQUEST_BYTES = 16 * 1024 * 1024
_TOOL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")

CAPABILITIES = (
    "response_delta",
    "tool_events",
    "session_resume",
    "profile_context",
    "ephemeral_system_prompt",
    "scoped_toolsets",
    "max_turns",
    "graceful_interrupt",
)

_REQUIRED_AGENT_PARAMETERS = frozenset(
    {
        "api_key",
        "base_url",
        "provider",
        "api_mode",
        "model",
        "max_iterations",
        "enabled_toolsets",
        "quiet_mode",
        "ephemeral_system_prompt",
        "session_id",
        "tool_start_callback",
        "tool_complete_callback",
        "stream_delta_callback",
        "skip_context_files",
        "skip_memory",
        "session_db",
    }
)


def _hermes_root_from_profile_home(profile_home: Path) -> Path:
    if profile_home.parent.name == "profiles":
        return profile_home.parent.parent
    return profile_home


def _project_root_candidates() -> list[Path]:
    candidates: list[Path] = []
    explicit = os.environ.get("HSA_HERMES_PROJECT_ROOT", "").strip()
    if explicit:
        candidates.append(Path(explicit).expanduser())
    profile_home = os.environ.get("HERMES_HOME", "").strip()
    if profile_home:
        root = _hermes_root_from_profile_home(Path(profile_home).expanduser())
        candidates.append(root / "hermes-agent")
    candidates.append(Path.home() / ".hermes" / "hermes-agent")
    result: list[Path] = []
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
        except OSError:
            continue
        if resolved not in result:
            result.append(resolved)
    return result


def _python_candidates(project_root: Path) -> list[Path]:
    explicit = os.environ.get("HSA_HERMES_PYTHON", "").strip()
    candidates = [Path(explicit).expanduser()] if explicit else []
    candidates.extend(
        (
            project_root / "venv" / "bin" / "python",
            project_root / ".venv" / "bin" / "python",
            project_root / "venv" / "Scripts" / "python.exe",
            project_root / ".venv" / "Scripts" / "python.exe",
        )
    )
    return candidates


def _prepare_hermes_imports() -> tuple[bool, str]:
    """Make Hermes importable, re-executing under its venv when necessary."""

    project_roots = [
        root for root in _project_root_candidates() if (root / "run_agent.py").is_file()
    ]
    for root in project_roots:
        if str(root) not in sys.path:
            sys.path.insert(0, str(root))

    try:
        import run_agent  # noqa: F401

        return True, ""
    except Exception as first_error:
        if os.environ.get("HSA_HERMES_BRIDGE_REEXEC") == "1":
            return False, (
                f"Hermes imports failed after bridge re-exec ({_exception_code(first_error)})"
            )

        # Do not resolve venv interpreter symlinks here.  Multiple virtual
        # environments can point at the same base interpreter while selecting
        # different ``pyvenv.cfg`` files through the invoked symlink path.
        current = Path(sys.executable).absolute()
        for root in project_roots:
            for candidate in _python_candidates(root):
                executable = candidate.expanduser().absolute()
                if not executable.is_file() or executable == current:
                    continue
                environment = dict(os.environ)
                environment["HSA_HERMES_BRIDGE_REEXEC"] = "1"
                environment.setdefault("HSA_HERMES_PROJECT_ROOT", str(root))
                try:
                    os.execve(
                        str(executable),
                        [str(executable), str(Path(__file__).resolve()), *sys.argv[1:]],
                        environment,
                    )
                except OSError:
                    continue
        return False, f"Hermes runtime is unavailable ({_exception_code(first_error)})"


def _exception_code(error: BaseException) -> str:
    """Return a diagnostic category that cannot contain profile-loaded secrets."""

    name = type(error).__name__
    return name if name.isidentifier() else "BridgeError"


def _check_payload(*, available: bool, reason: str = "") -> dict[str, Any]:
    return {
        "protocol": BRIDGE_PROTOCOL,
        "protocol_version": BRIDGE_PROTOCOL_VERSION,
        "bridge_version": BRIDGE_VERSION,
        "available": available,
        "capabilities": list(CAPABILITIES if available else ()),
        "reason": reason,
    }


def _check_runtime() -> dict[str, Any]:
    available, reason = _prepare_hermes_imports()
    if not available:
        return _check_payload(available=False, reason=reason)
    try:
        from run_agent import AIAgent

        constructor_parameters = set(inspect.signature(AIAgent.__init__).parameters)
        missing = sorted(_REQUIRED_AGENT_PARAMETERS - constructor_parameters)
        run_parameters = set(inspect.signature(AIAgent.run_conversation).parameters)
        if missing:
            return _check_payload(
                available=False,
                reason=f"Hermes AIAgent is missing parameters: {', '.join(missing)}",
            )
        if not {"user_message", "conversation_history"}.issubset(run_parameters):
            return _check_payload(
                available=False,
                reason="Hermes run_conversation does not support bounded history input",
            )
        from hermes_cli.config import load_config  # noqa: F401
        from hermes_cli.runtime_provider import resolve_runtime_provider  # noqa: F401
        from hermes_cli.oneshot import (  # noqa: F401
            _create_session_db_for_oneshot,
            _validate_explicit_toolsets,
        )
    except Exception as exc:
        return _check_payload(
            available=False,
            reason=f"Hermes bridge dependency check failed ({_exception_code(exc)})",
        )
    return _check_payload(available=True)


class _Emitter:
    def __init__(self, stream: TextIO) -> None:
        self._stream = stream
        self._lock = threading.Lock()
        self._sequence = 0

    def emit(self, event_type: str, data: dict[str, Any] | None = None) -> None:
        with self._lock:
            self._sequence += 1
            frame = {
                "protocol": BRIDGE_PROTOCOL,
                "protocol_version": BRIDGE_PROTOCOL_VERSION,
                "sequence": self._sequence,
                "type": event_type,
                "data": data or {},
            }
            self._stream.write(
                json.dumps(frame, ensure_ascii=False, sort_keys=True, default=str) + "\n"
            )
            self._stream.flush()


class _SensitiveStreamFilter:
    """Statefully remove hidden-reasoning and private-context XML spans.

    This filter is deliberately local to the bridge.  Upstream Hermes also
    scrubs its normal display path, but some provider/tool branches call the
    raw stream callback directly.  A bridge protocol must therefore enforce
    its own visibility boundary across arbitrary delta splits.
    """

    _TAGS = (
        ("<think>", "think", False),
        ("</think>", "think", True),
        ("<thinking>", "thinking", False),
        ("</thinking>", "thinking", True),
        ("<reasoning>", "reasoning", False),
        ("</reasoning>", "reasoning", True),
        ("<thought>", "thought", False),
        ("</thought>", "thought", True),
        ("<analysis>", "analysis", False),
        ("</analysis>", "analysis", True),
        ("<reasoning_scratchpad>", "reasoning_scratchpad", False),
        ("</reasoning_scratchpad>", "reasoning_scratchpad", True),
        ("<memory-context>", "memory-context", False),
        ("</memory-context>", "memory-context", True),
        ("<context>", "context", False),
        ("</context>", "context", True),
    )

    def __init__(self) -> None:
        self._buffer = ""
        self._stack: list[str] = []

    def feed(self, text: str) -> str:
        if not text:
            return ""
        buffer = self._buffer + text
        self._buffer = ""
        visible: list[str] = []
        while buffer:
            match = self._first_tag(buffer)
            if match is not None:
                index, tag, name, closing = match
                if not self._stack and index:
                    visible.append(buffer[:index])
                if closing:
                    if name in self._stack:
                        last_match = len(self._stack) - 1 - self._stack[::-1].index(name)
                        del self._stack[last_match:]
                else:
                    self._stack.append(name)
                buffer = buffer[index + len(tag) :]
                continue

            held = self._partial_tag_suffix(buffer)
            emit = buffer[:-held] if held else buffer
            if emit and not self._stack:
                visible.append(emit)
            self._buffer = buffer[-held:] if held else ""
            break
        return "".join(visible)

    def flush(self) -> str:
        # The buffer can only be a prefix of a protected tag.  Discarding it is
        # safer than exposing a split marker or content from an unterminated
        # sensitive span.
        self._buffer = ""
        self._stack.clear()
        return ""

    @classmethod
    def scrub_complete(cls, text: str) -> str:
        scrubber = cls()
        return scrubber.feed(text) + scrubber.flush()

    @classmethod
    def _first_tag(cls, text: str) -> tuple[int, str, str, bool] | None:
        lowered = text.lower()
        found: tuple[int, str, str, bool] | None = None
        for tag, name, closing in cls._TAGS:
            index = lowered.find(tag)
            if index >= 0 and (found is None or index < found[0]):
                found = (index, tag, name, closing)
        return found

    @classmethod
    def _partial_tag_suffix(cls, text: str) -> int:
        lowered = text.lower()
        held = 0
        for tag, _name, _closing in cls._TAGS:
            max_length = min(len(lowered), len(tag) - 1)
            for length in range(max_length, held, -1):
                if lowered.endswith(tag[:length]):
                    held = length
                    break
        return held


def _read_request() -> dict[str, Any]:
    raw = sys.stdin.buffer.readline(MAX_REQUEST_BYTES + 1)
    if not raw:
        raise ValueError("bridge request is missing")
    if len(raw) > MAX_REQUEST_BYTES:
        raise ValueError("bridge request exceeds the 16 MiB limit")
    if sys.stdin.buffer.read(1):
        raise ValueError("bridge accepts exactly one NDJSON request")
    try:
        value = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"bridge request is not valid UTF-8 JSON: {exc}") from None
    if not isinstance(value, dict):
        raise ValueError("bridge request must be a JSON object")
    if (
        value.get("protocol") != BRIDGE_PROTOCOL
        or value.get("type") != "invoke"
        or value.get("protocol_version") != BRIDGE_PROTOCOL_VERSION
    ):
        raise ValueError("bridge request protocol does not match")
    prompt = value.get("prompt")
    invocation_id = value.get("invocation_id")
    toolsets = value.get("toolsets")
    max_turns = value.get("max_turns")
    load_profile_context = value.get("load_profile_context")
    if not isinstance(prompt, str) or not prompt:
        raise ValueError("bridge prompt must be a non-empty string")
    if not isinstance(invocation_id, str) or not invocation_id.strip():
        raise ValueError("bridge invocation_id must be a non-empty string")
    if (
        not isinstance(toolsets, list)
        or not toolsets
        or any(not isinstance(item, str) or not item.strip() for item in toolsets)
    ):
        raise ValueError("bridge toolsets must be a non-empty string list")
    if any(item != item.strip() for item in toolsets) or len(set(toolsets)) != len(toolsets):
        raise ValueError("bridge toolsets must be stripped and unique")
    if not isinstance(max_turns, int) or isinstance(max_turns, bool) or max_turns < 1:
        raise ValueError("bridge max_turns must be a positive integer")
    if not isinstance(load_profile_context, bool):
        raise ValueError("bridge load_profile_context must be a boolean")
    resume = value.get("resume_session_id")
    if resume is not None and (not isinstance(resume, str) or not resume.strip()):
        raise ValueError("bridge resume_session_id must be a non-empty string")
    return value


def _load_profile_environment() -> None:
    from hermes_cli.env_loader import load_hermes_dotenv

    project_root = next(
        (root for root in _project_root_candidates() if (root / "run_agent.py").is_file()),
        None,
    )
    load_hermes_dotenv(project_env=(project_root / ".env") if project_root else None)


def _effective_runtime() -> tuple[dict[str, Any], str, dict[str, Any]]:
    from hermes_cli.config import load_config
    from hermes_cli.fallback_config import get_fallback_chain
    from hermes_cli.runtime_provider import resolve_runtime_provider

    config = load_config()
    model_config = config.get("model") or {}
    if isinstance(model_config, str):
        configured_model = model_config
        configured_provider = None
    else:
        configured_model = model_config.get("default") or model_config.get("model") or ""
        configured_provider = model_config.get("provider")
    effective_model = os.getenv("HERMES_INFERENCE_MODEL", "").strip() or configured_model
    runtime = resolve_runtime_provider(
        requested=os.getenv("HERMES_INFERENCE_PROVIDER", "").strip() or configured_provider or None,
        target_model=effective_model or None,
    )
    fallback = get_fallback_chain(config)
    return runtime, effective_model, fallback


def _new_session_id() -> str:
    timestamp = datetime.now(UTC).strftime("%Y%m%d_%H%M%S")
    return f"{timestamp}_{uuid4().hex[:8]}"


def _safe_size_and_hash(value: Any) -> tuple[int, str]:
    try:
        rendered = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        rendered = str(value)
    encoded = rendered.encode("utf-8", errors="replace")
    return len(encoded), hashlib.sha256(encoded).hexdigest()


def _safe_tool_name(value: Any) -> str:
    """Return a bounded tool label that cannot carry arbitrary child data."""

    raw = str(value)
    if _TOOL_NAME.fullmatch(raw):
        return raw
    return f"tool-{hashlib.sha256(raw.encode('utf-8', errors='replace')).hexdigest()[:16]}"


def _tool_completion_data(
    *,
    invocation_id: str,
    tool_call_id: Any,
    name: Any,
    result: Any,
) -> dict[str, Any]:
    """Build a progress frame with a result-free, stable artifact record."""

    raw_call_id = str(tool_call_id)
    raw_name = str(name)
    safe_name = _safe_tool_name(raw_name)
    result_size, result_digest = _safe_size_and_hash(result)
    identity = json.dumps(
        [invocation_id, raw_call_id, raw_name, result_digest],
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8", errors="replace")
    artifact_id = f"hta_{hashlib.sha256(identity).hexdigest()[:32]}"
    return {
        "tool_call_id": raw_call_id,
        "name": raw_name,
        "result_size": result_size,
        "result_sha256": result_digest,
        "artifact": {
            "id": artifact_id,
            "kind": "hermes-tool-result",
            "tool_name": safe_name,
            "result_size": result_size,
            "result_sha256": result_digest,
        },
    }


def _run_request(request: dict[str, Any], emitter: _Emitter) -> int:
    # Load profile credentials/config before importing modules that cache
    # provider environment at import time. ``run_agent`` performs this load as
    # well, but doing it explicitly keeps the bridge correct if that behaviour
    # changes upstream.
    _load_profile_environment()
    from hermes_cli.oneshot import (
        _create_session_db_for_oneshot,
        _oneshot_clarify_callback,
        _validate_explicit_toolsets,
    )
    from run_agent import AIAgent

    requested_toolsets = [str(item) for item in request["toolsets"]]
    validated_toolsets, toolset_error = _validate_explicit_toolsets(requested_toolsets)
    if toolset_error:
        raise ValueError(toolset_error.strip())
    if (
        validated_toolsets is None
        or len(validated_toolsets) != len(requested_toolsets)
        or set(validated_toolsets) != set(requested_toolsets)
    ):
        raise ValueError("Hermes did not accept the exact scoped toolsets")

    runtime, effective_model, fallback = _effective_runtime()
    session_db = _create_session_db_for_oneshot()
    resume_session_id = request.get("resume_session_id")
    if resume_session_id and session_db is None:
        raise RuntimeError("Hermes session storage is unavailable; cannot resume safely")
    session_id = resume_session_id or _new_session_id()
    history: list[dict[str, Any]] = []
    if resume_session_id:
        history = session_db.get_messages_as_conversation(session_id) or []

    visible_stream = _SensitiveStreamFilter()

    def stream_delta(text: Any) -> None:
        if isinstance(text, str) and text:
            visible = visible_stream.feed(text)
            if visible:
                emitter.emit("response.delta", {"text": visible})

    def tool_started(tool_call_id: str, name: str, arguments: Any) -> None:
        size, digest = _safe_size_and_hash(arguments)
        argument_keys = sorted(arguments) if isinstance(arguments, dict) else []
        emitter.emit(
            "tool.started",
            {
                "tool_call_id": str(tool_call_id),
                "name": str(name),
                "argument_keys": argument_keys,
                "arguments_size": size,
                "arguments_sha256": digest,
            },
        )

    def tool_completed(tool_call_id: str, name: str, arguments: Any, result: Any) -> None:
        emitter.emit(
            "tool.completed",
            _tool_completion_data(
                invocation_id=request["invocation_id"],
                tool_call_id=tool_call_id,
                name=name,
                result=result,
            ),
        )

    agent = AIAgent(
        api_key=runtime.get("api_key"),
        base_url=runtime.get("base_url"),
        provider=runtime.get("provider"),
        api_mode=runtime.get("api_mode"),
        acp_command=runtime.get("command"),
        acp_args=list(runtime.get("args") or []),
        credential_pool=runtime.get("credential_pool"),
        model=effective_model,
        max_iterations=request["max_turns"],
        enabled_toolsets=requested_toolsets,
        quiet_mode=True,
        tool_progress_mode="off",
        ephemeral_system_prompt=os.getenv("HERMES_EPHEMERAL_SYSTEM_PROMPT", ""),
        session_id=session_id,
        platform="cli",
        session_db=session_db,
        clarify_callback=_oneshot_clarify_callback,
        fallback_model=fallback or None,
        skip_context_files=not request["load_profile_context"],
        skip_memory=not request["load_profile_context"],
        stream_delta_callback=stream_delta,
        tool_start_callback=tool_started,
        tool_complete_callback=tool_completed,
    )
    agent.suppress_status_output = True

    interrupted = threading.Event()

    def interrupt(signum: int, _frame: Any) -> None:
        interrupted.set()
        try:
            agent.interrupt(f"received signal {signum}")
        except TypeError:
            agent.interrupt()

    for signal_name in ("SIGINT", "SIGTERM", "SIGHUP"):
        signum = getattr(signal, signal_name, None)
        if signum is not None:
            signal.signal(signum, interrupt)

    result = agent.run_conversation(
        user_message=request["prompt"],
        conversation_history=history,
    )
    if interrupted.is_set() or (isinstance(result, dict) and result.get("interrupted")):
        emitter.emit(
            "bridge.error",
            {"code": "cancelled", "message": "Hermes invocation was cancelled"},
        )
        return 130
    response = result.get("final_response", "") if isinstance(result, dict) else str(result)
    if not isinstance(response, str) or not response:
        emitter.emit(
            "bridge.error",
            {
                "code": "empty_response",
                "message": "Hermes produced no final response",
            },
        )
        return 1
    response = _SensitiveStreamFilter.scrub_complete(response)
    if not response:
        emitter.emit(
            "bridge.error",
            {
                "code": "sensitive_only_response",
                "message": "Hermes produced no public final response",
            },
        )
        return 1
    emitter.emit(
        "response.completed",
        {"content": response, "session_id": getattr(agent, "session_id", session_id)},
    )
    return 0


def _build_final_only_argv(request: dict[str, Any]) -> list[str]:
    """Build in-process Hermes CLI arguments; prompt never enters OS argv."""

    command = [
        "hermes",
        "chat",
        "-q",
        request["prompt"],
        "-Q",
        "--source",
        "tool",
        "-t",
        ",".join(request["toolsets"]),
        "--max-turns",
        str(request["max_turns"]),
    ]
    if request.get("resume_session_id"):
        command.extend(("--resume", request["resume_session_id"]))
    if not request["load_profile_context"]:
        command.append("--ignore-rules")
    return command


def _run_final_only_request(request: dict[str, Any]) -> int:
    """Run Hermes quiet chat in this helper process with an stdin-supplied prompt."""

    sys.argv = _build_final_only_argv(request)
    captured = StringIO()
    with redirect_stdout(captured):
        from hermes_cli.main import main as hermes_main

        try:
            result = hermes_main()
        except SystemExit as exc:
            if exc.code is None:
                status = 0
            else:
                status = exc.code if isinstance(exc.code, int) else 1
        else:
            status = result if isinstance(result, int) else 0
    if status != 0:
        return status
    visible = _SensitiveStreamFilter.scrub_complete(captured.getvalue())
    if not visible:
        print("hsa-final-only-error:sensitive_only_response", file=sys.stderr)
        return 1
    sys.stdout.write(visible)
    sys.stdout.flush()
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="HSA Hermes NDJSON event bridge")
    parser.add_argument(
        "--check", action="store_true", help="check compatibility without a model call"
    )
    parser.add_argument(
        "--serve-once",
        action="store_true",
        help="read one invocation request from stdin and emit NDJSON events",
    )
    parser.add_argument(
        "--final-only",
        action="store_true",
        help="read one invocation from stdin and run quiet Hermes without OS-argv prompt data",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if sum((args.check, args.serve_once, args.final_only)) != 1:
        print(
            "exactly one of --check, --serve-once or --final-only is required",
            file=sys.stderr,
        )
        return 2

    if args.check:
        payload = _check_runtime()
        print(json.dumps(payload, ensure_ascii=False, sort_keys=True), flush=True)
        return 0 if payload["available"] else 1

    available, reason = _prepare_hermes_imports()
    if not available:
        if args.final_only:
            print("hsa-final-only-error:unavailable", file=sys.stderr)
            return 1
        emitter = _Emitter(sys.stdout)
        emitter.emit("bridge.error", {"code": "unavailable", "message": reason})
        return 1
    if args.final_only:
        try:
            request = _read_request()
            return _run_final_only_request(request)
        except BaseException as exc:
            print(f"hsa-final-only-error:{_exception_code(exc)}", file=sys.stderr)
            return 130 if isinstance(exc, KeyboardInterrupt) else 1

    emitter = _Emitter(sys.stdout)
    emitter.emit("bridge.ready", {"capabilities": list(CAPABILITIES)})
    try:
        request = _read_request()
        # Hermes and third-party tools occasionally print directly to stdout.
        # Keep protocol stdout in ``emitter`` and divert all such output.
        with redirect_stdout(sys.stderr):
            return _run_request(request, emitter)
    except BaseException as exc:
        if isinstance(exc, (KeyboardInterrupt, SystemExit)):
            raise
        emitter.emit(
            "bridge.error",
            {
                "code": _exception_code(exc),
                "message": "Hermes bridge request failed",
            },
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
