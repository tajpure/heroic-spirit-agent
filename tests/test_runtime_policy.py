from __future__ import annotations

import asyncio
import json
import sys
import textwrap
import time
import types
from pathlib import Path

import pytest

from hsa_thinktank import hermes_event_bridge
from hsa_thinktank.runtime import (
    AgentInvocation,
    AgentRuntimeError,
    AgentRuntimeTimeout,
    DeterministicRuntime,
    HermesProfileRuntime,
    ScriptedRuntime,
    redact_sensitive,
)
from hsa_thinktank.tool_policy import (
    STANDARD_RESEARCH_POLICY_ID,
    TerminalBackend,
    ToolPolicy,
    ToolRisk,
    get_tool_policy,
)


def invocation(**updates: object) -> AgentInvocation:
    values: dict[str, object] = {
        "invocation_id": "inv-1",
        "hsa_id": "steve-jobs",
        "phase": "evidence",
        "system_prompt": "ephemeral organization overlay",
        "user_prompt": "Compare the options",
        "enabled_toolsets": ("memory", "session_search", "web"),
    }
    values.update(updates)
    return AgentInvocation.model_validate(values)


def policy(*, terminal_backend: TerminalBackend = TerminalBackend.LOCAL) -> ToolPolicy:
    tools = frozenset(
        {"calculator", "web", "memory", "session_search", "terminal", "mcp", "delegation"}
    )
    return ToolPolicy(
        policy_id="test-policy",
        profile_allowlist=tools,
        organization_allowlist=tools,
        phase_allowlists={"research": tools},
        risk_by_tool={
            "calculator": ToolRisk.L0,
            "web": ToolRisk.L1,
            "memory": ToolRisk.L1,
            "session_search": ToolRisk.L1,
            "terminal": ToolRisk.L2,
            "mcp": ToolRisk.L3,
            "delegation": ToolRisk.L1,
        },
        terminal_backend=terminal_backend,
        memory_enabled=True,
        session_search_enabled=True,
        delegation_enabled=True,
    )


def rejection_map(resolution: object) -> dict[str, str]:
    return {
        rejection.toolset: rejection.reason  # type: ignore[attr-defined]
        for rejection in resolution.rejected  # type: ignore[attr-defined]
    }


def test_policy_intersects_profile_organization_and_phase_allowlists() -> None:
    configured = ToolPolicy(
        policy_id="intersection",
        profile_allowlist={"calculator", "web", "memory", "session_search", "delegation"},
        organization_allowlist={"calculator", "web", "memory", "session_search"},
        phase_allowlists={"research": {"calculator", "memory", "session_search"}},
        risk_by_tool={
            "calculator": ToolRisk.L0,
            "web": ToolRisk.L1,
            "memory": ToolRisk.L1,
            "session_search": ToolRisk.L1,
            "delegation": ToolRisk.L1,
        },
        terminal_backend=TerminalBackend.DISABLED,
        delegation_enabled=True,
    )

    result = configured.resolve(
        phase="research",
        requested_toolsets=("calculator", "web", "delegation"),
        user_grants=(),
    )

    # L0/L1 are auto-admitted, while configured context survives phase changes.
    assert result.enabled_toolsets == ("calculator", "memory", "session_search")
    assert rejection_map(result) == {
        "web": "not_in_phase_allowlist",
        "delegation": "not_in_organization_allowlist",
    }


def test_bundled_policy_rejects_toolsets_absent_from_supported_hermes() -> None:
    result = get_tool_policy(STANDARD_RESEARCH_POLICY_ID).resolve(
        phase="independent_ballot",
        requested_toolsets=("calculator", "todo"),
        user_grants=("calculator", "todo"),
    )

    assert result.enabled_toolsets == ("todo",)
    assert rejection_map(result)["calculator"] == "not_in_profile_allowlist"


def test_local_terminal_requires_explicit_l2_grant() -> None:
    local_policy = policy()
    missing_grant = local_policy.resolve(
        phase="research", requested_toolsets=("terminal",), user_grants=()
    )
    assert "terminal" not in missing_grant.enabled_toolsets
    assert rejection_map(missing_grant)["terminal"] == "l2_user_grant_required"

    granted = local_policy.resolve(
        phase="research", requested_toolsets=("terminal",), user_grants=("terminal",)
    )
    assert "terminal" in granted.enabled_toolsets

    disabled = policy(terminal_backend=TerminalBackend.DISABLED).resolve(
        phase="research", requested_toolsets=("terminal",), user_grants=("terminal",)
    )
    assert rejection_map(disabled)["terminal"] == "local_execution_disabled"


def test_l3_always_requires_human_approval() -> None:
    configured = policy()
    pending = configured.resolve(phase="research", requested_toolsets=("mcp",), user_grants=())
    assert rejection_map(pending)["mcp"] == "human_approval_required"

    approved = configured.resolve(
        phase="research",
        requested_toolsets=("mcp",),
        user_grants=(),
        human_approvals=("mcp",),
    )
    assert "mcp" in approved.enabled_toolsets


def test_delegation_is_an_artifact_and_never_an_organization_member() -> None:
    result = policy().resolve(phase="research", requested_toolsets=("delegation",), user_grants=())
    assert "delegation" in result.enabled_toolsets
    assert result.delegation_output == "artifact"
    assert result.delegation_counts_as_member is False


def test_standard_policy_covers_protocol_phases_and_gates_memory_mutation() -> None:
    configured = get_tool_policy(STANDARD_RESEARCH_POLICY_ID)
    assert configured.terminal_backend is TerminalBackend.LOCAL
    phases = (
        "option_generation",
        "independent_ballot",
        "revised_ballot",
        "blue_proposal",
        "red_critique",
        "blue_rebuttal",
        "judge_ballot",
        "portfolio_memo",
        "executive_ballot",
    )
    for phase in phases:
        result = configured.resolve(phase=phase, requested_toolsets=(), user_grants=())
        assert result.enabled_toolsets == ()
        assert rejection_map(result)["memory"] == "l2_user_grant_required"
        assert rejection_map(result)["session_search"] == "l2_user_grant_required"

        writable = configured.resolve(
            phase=phase,
            requested_toolsets=(),
            user_grants=("memory",),
        )
        assert writable.enabled_toolsets == ("memory",)

        searchable = configured.resolve(
            phase=phase,
            requested_toolsets=(),
            user_grants=("session_search",),
        )
        assert searchable.enabled_toolsets == ("session_search",)


def test_standard_policy_requires_an_explicit_grant_for_delegation_cost() -> None:
    configured = get_tool_policy(STANDARD_RESEARCH_POLICY_ID)
    pending = configured.resolve(
        phase="independent_ballot",
        requested_toolsets=("delegation",),
        user_grants=(),
    )
    assert rejection_map(pending)["delegation"] == "l2_user_grant_required"

    granted = configured.resolve(
        phase="independent_ballot",
        requested_toolsets=("delegation",),
        user_grants=("delegation",),
    )
    assert "delegation" in granted.enabled_toolsets
    assert granted.delegation_counts_as_member is False


def test_local_code_execution_requires_explicit_l2_grant() -> None:
    configured = get_tool_policy(STANDARD_RESEARCH_POLICY_ID)
    pending = configured.resolve(
        phase="independent_ballot",
        requested_toolsets=("code_execution",),
        user_grants=(),
    )
    assert rejection_map(pending)["code_execution"] == "l2_user_grant_required"

    granted = configured.resolve(
        phase="independent_ballot",
        requested_toolsets=("code_execution",),
        user_grants=("code_execution",),
    )
    assert "code_execution" in granted.enabled_toolsets

    disabled = get_tool_policy(
        STANDARD_RESEARCH_POLICY_ID,
        terminal_backend=TerminalBackend.DISABLED,
    ).resolve(
        phase="independent_ballot",
        requested_toolsets=("code_execution",),
        user_grants=("code_execution",),
    )
    assert rejection_map(disabled)["code_execution"] == "local_execution_disabled"


def test_secure_final_only_command_keeps_prompt_out_of_os_argv() -> None:
    runtime = HermesProfileRuntime(
        profile="jobs-profile",
        executable="/opt/hermes/jobs",
        bridge_command="/opt/hsa/hermes-bridge",
        max_turns=8,
    )
    call = invocation(
        user_prompt="Choose one",
        phase="independent_ballot",
        enabled_toolsets=("memory", "session_search"),
        max_turns=6,
        resume_session_id="session-previous",
    )

    command = runtime.build_command(call)
    request = runtime.build_request(call)

    assert command == ["/opt/hsa/hermes-bridge", "--final-only"]
    assert "Choose one" not in command
    assert request["prompt"] == "Choose one"
    assert request["toolsets"] == ["memory", "session_search"]
    assert request["max_turns"] == 6
    assert request["resume_session_id"] == "session-previous"
    assert request["load_profile_context"] is True


def test_empty_toolsets_fail_closed_instead_of_using_profile_defaults() -> None:
    runtime = HermesProfileRuntime("jobs-profile")
    with pytest.raises(ValueError, match="cannot be empty"):
        runtime.build_command(invocation(enabled_toolsets=()))


def test_ephemeral_prompt_environment_does_not_disable_profile_memory() -> None:
    runtime = HermesProfileRuntime(
        "jobs-profile", inherit_environment=False, environment={"SAFE": "yes"}
    )
    environment = runtime.build_environment(invocation(system_prompt="roundtable overlay"))
    assert environment == {
        "SAFE": "yes",
        "HERMES_EPHEMERAL_SYSTEM_PROMPT": "roundtable overlay",
    }
    assert "HERMES_IGNORE_RULES" not in environment


def test_private_memory_disabled_is_sent_only_in_stdin_request() -> None:
    runtime = HermesProfileRuntime("jobs-profile")
    command = runtime.build_command(invocation(load_profile_context=False))
    request = runtime.build_request(invocation(load_profile_context=False))
    assert command[-1] == "--final-only"
    assert "--ignore-rules" not in command
    assert request["load_profile_context"] is False
    assert "--ignore-rules" in hermes_event_bridge._build_final_only_argv(request)


def test_redact_sensitive_handles_explicit_bearer_assignment_and_url_secrets() -> None:
    message = (
        "token=my-token Authorization: Bearer abc.def "
        "https://alice:password@example.test explicit-secret"
    )
    redacted = redact_sensitive(message, secrets=("explicit-secret", "my-token"))
    assert "my-token" not in redacted
    assert "abc.def" not in redacted
    assert "alice" not in redacted
    assert "password" not in redacted
    assert "explicit-secret" not in redacted
    assert redacted.count("[REDACTED]") >= 4


def test_bridge_statefully_filters_hidden_reasoning_and_context_tags() -> None:
    scrubber = hermes_event_bridge._SensitiveStreamFilter()
    chunks = [
        "visible ",
        "<thi",
        "nk>private reasoning</TH",
        "INK>",
        "<memory-",
        "context>private memory</memory-con",
        "text>",
        " answer",
    ]

    visible = "".join(scrubber.feed(chunk) for chunk in chunks) + scrubber.flush()

    assert visible == "visible  answer"
    assert "private" not in visible
    assert "think" not in visible.lower()
    assert "memory-context" not in visible.lower()


def test_final_only_helper_filters_content_before_writing_stdout(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    package = types.ModuleType("hermes_cli")
    package.__path__ = []  # type: ignore[attr-defined]
    fake_main = types.ModuleType("hermes_cli.main")

    def run_fake_hermes() -> int:
        assert sys.argv[1:4] == ["chat", "-q", "private prompt"]
        print("visible <reasoning>private chain</reasoning>final", end="")
        return 0

    fake_main.main = run_fake_hermes  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "hermes_cli", package)
    monkeypatch.setitem(sys.modules, "hermes_cli.main", fake_main)
    request = {
        "prompt": "private prompt",
        "toolsets": ["memory"],
        "max_turns": 2,
        "load_profile_context": True,
        "resume_session_id": None,
    }

    status = hermes_event_bridge._run_final_only_request(request)

    output = capsys.readouterr().out
    assert status == 0
    assert output == "visible final"
    assert "private chain" not in output


def make_executable(tmp_path: Path, body: str) -> Path:
    executable = tmp_path / "fake-profile"
    executable.write_text(f"#!{sys.executable}\n" + textwrap.dedent(body), encoding="utf-8")
    executable.chmod(0o700)
    return executable


def test_hermes_runtime_returns_only_stdout_and_parses_stderr_session(
    tmp_path: Path,
) -> None:
    helper = make_executable(
        tmp_path,
        """
        import json
        import os
        import sys

        assert sys.argv[1:] == ["--final-only"]
        request = json.loads(sys.stdin.readline())
        assert sys.stdin.read() == ""
        assert request["prompt"] == "Compare the options"
        assert request["load_profile_context"] is True
        print(os.environ["HERMES_EPHEMERAL_SYSTEM_PROMPT"], end="")
        print("Session ID: session-123", file=sys.stderr)
        """,
    )
    runtime = HermesProfileRuntime(
        profile="jobs-profile",
        executable="/bin/false",
        bridge_command=helper,
        profile_home=tmp_path,
        inherit_environment=True,
    )

    response = asyncio.run(runtime.invoke(invocation(system_prompt="private memory stays on")))

    assert response.content == "private memory stays on"
    assert response.session_id == "session-123"
    assert response.profile == "jobs-profile"
    assert response.tool_events == ()
    assert response.raw_messages == ()
    assert response.metadata["stream_mode"] == "final-only"


def test_hermes_bridge_streams_machine_readable_events_with_scoped_settings(
    tmp_path: Path,
) -> None:
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    final_executable = make_executable(
        tmp_path,
        """
        print("final-only path must not run")
        raise SystemExit(90)
        """,
    )
    bridge = tmp_path / "fake-bridge"
    bridge.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            """
            import json
            import os
            import sys

            protocol = "hsa-hermes-ndjson"
            capabilities = [
                "response_delta",
                "tool_events",
                "session_resume",
                "profile_context",
                "ephemeral_system_prompt",
                "scoped_toolsets",
                "max_turns",
                "graceful_interrupt",
            ]
            if "--check" in sys.argv:
                print(json.dumps({
                    "protocol": protocol,
                    "protocol_version": 1,
                    "bridge_version": "test",
                    "available": True,
                    "capabilities": capabilities,
                    "reason": "",
                }))
                raise SystemExit(0)

            assert sys.argv[-1] == "--serve-once"
            def emit(sequence, event_type, data):
                print(json.dumps({
                    "protocol": protocol,
                    "protocol_version": 1,
                    "sequence": sequence,
                    "type": event_type,
                    "data": data,
                }), flush=True)

            emit(1, "bridge.ready", {"capabilities": capabilities})
            request = json.loads(sys.stdin.readline())
            assert sys.stdin.read() == ""
            assert request["protocol"] == protocol
            assert request["type"] == "invoke"
            assert request["toolsets"] == ["memory", "web"]
            assert request["max_turns"] == 6
            assert request["load_profile_context"] is False
            assert request["resume_session_id"] == "previous-session"
            assert os.environ["HERMES_EPHEMERAL_SYSTEM_PROMPT"] == "streamed soul"
            assert os.environ["HERMES_HOME"].endswith("profile")
            emit(2, "response.delta", {"text": "{\\\"answer\\\":"})
            emit(3, "tool.started", {
                "tool_call_id": "tool-1",
                "name": "web_search",
                "argument_keys": ["query"],
            })
            emit(4, "tool.completed", {
                "tool_call_id": "tool-1",
                "name": "web_search",
                "result_sha256": "0" * 64,
            })
            emit(5, "response.delta", {"text": "true}"})
            emit(6, "response.completed", {
                "content": "{\\\"answer\\\":true}",
                "session_id": "bridge-session",
            })
            """
        ),
        encoding="utf-8",
    )
    bridge.chmod(0o700)
    runtime = HermesProfileRuntime(
        profile="jobs-profile",
        executable=final_executable,
        profile_home=profile_home,
        bridge_command=bridge,
    )

    capabilities = asyncio.run(runtime.check_streaming_capability())
    events = []
    response = asyncio.run(
        runtime.invoke(
            invocation(
                system_prompt="streamed soul",
                enabled_toolsets=("memory", "web"),
                max_turns=6,
                load_profile_context=False,
                resume_session_id="previous-session",
            ),
            event_sink=events.append,
        )
    )

    assert capabilities.available is True
    assert response.content == '{"answer":true}'
    assert response.session_id == "bridge-session"
    assert response.metadata["stream_mode"] == "ndjson"
    assert [event.event_type for event in events] == [
        "bridge_ready",
        "response_delta",
        "tool_started",
        "tool_completed",
        "response_delta",
        "response_completed",
    ]
    assert [item["event_type"] for item in response.tool_events] == [
        "tool.started",
        "tool.completed",
    ]


def test_unavailable_bridge_falls_back_before_dispatch(tmp_path: Path) -> None:
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    bridge = tmp_path / "unavailable-bridge"
    bridge.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            """
            import json
            import sys

            if "--serve-once" in sys.argv:
                raise SystemExit(91)
            if "--final-only" in sys.argv:
                request = json.loads(sys.stdin.readline())
                assert request["prompt"] == "Compare the options"
                print('{"answer":"final-only"}')
                print("session_id: fallback-session", file=sys.stderr)
                raise SystemExit(0)
            print(json.dumps({
                "protocol": "hsa-hermes-ndjson",
                "protocol_version": 1,
                "bridge_version": "test",
                "available": False,
                "capabilities": [],
                "reason": "unsupported Hermes version sk-child-only-secret",
            }))
            raise SystemExit(1)
            """
        ),
        encoding="utf-8",
    )
    bridge.chmod(0o700)
    runtime = HermesProfileRuntime(
        profile="jobs-profile",
        executable="/bin/false",
        profile_home=profile_home,
        bridge_command=bridge,
    )
    events = []

    response = asyncio.run(runtime.invoke(invocation(), event_sink=events.append))

    assert response.content == '{"answer":"final-only"}'
    assert response.session_id == "fallback-session"
    assert response.metadata["stream_mode"] == "final-only"
    assert response.metadata["stream_fallback_reason"] == "Hermes bridge reported unavailable"
    assert [event.event_type for event in events] == ["response_completed"]


def test_post_dispatch_bridge_failure_never_runs_final_only_fallback(
    tmp_path: Path,
) -> None:
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    final_marker = tmp_path / "final-only-ran"
    request_marker = tmp_path / "bridge-read-request"
    bridge = tmp_path / "post-dispatch-failure-bridge"
    bridge.write_text(
        f"#!{sys.executable}\n"
        + textwrap.dedent(
            f"""
            import json
            import sys
            from pathlib import Path

            protocol = "hsa-hermes-ndjson"
            capabilities = [
                "response_delta",
                "tool_events",
                "session_resume",
                "profile_context",
                "ephemeral_system_prompt",
                "scoped_toolsets",
                "max_turns",
                "graceful_interrupt",
            ]
            if "--check" in sys.argv:
                print(json.dumps({{
                    "protocol": protocol,
                    "protocol_version": 1,
                    "bridge_version": "test",
                    "available": True,
                    "capabilities": capabilities,
                    "reason": "",
                }}))
                raise SystemExit(0)
            if "--final-only" in sys.argv:
                Path({str(final_marker)!r}).write_text("ran", encoding="utf-8")
                print('{{"answer":"duplicate"}}')
                raise SystemExit(0)

            print(json.dumps({{
                "protocol": protocol,
                "protocol_version": 1,
                "sequence": 1,
                "type": "bridge.ready",
                "data": {{"capabilities": capabilities}},
            }}), flush=True)
            request = json.loads(sys.stdin.readline())
            assert request["type"] == "invoke"
            Path({str(request_marker)!r}).write_text("read", encoding="utf-8")
            print("this is not NDJSON", flush=True)
            raise SystemExit(7)
            """
        ),
        encoding="utf-8",
    )
    bridge.chmod(0o700)
    runtime = HermesProfileRuntime(
        profile="jobs-profile",
        executable="/bin/false",
        profile_home=profile_home,
        bridge_command=bridge,
    )

    with pytest.raises(AgentRuntimeError, match="protocol failed after dispatch"):
        asyncio.run(runtime.invoke(invocation(), event_sink=lambda _event: None))

    assert request_marker.read_text(encoding="utf-8") == "read"
    assert not final_marker.exists()


def test_bridge_error_message_cannot_leak_child_only_secret(tmp_path: Path) -> None:
    profile_home = tmp_path / "profile"
    profile_home.mkdir()
    bridge = make_executable(
        tmp_path,
        """
        import json
        import sys

        protocol = "hsa-hermes-ndjson"
        capabilities = [
            "response_delta", "tool_events", "session_resume", "profile_context",
            "ephemeral_system_prompt", "scoped_toolsets", "max_turns",
            "graceful_interrupt",
        ]
        if "--check" in sys.argv:
            print(json.dumps({
                "protocol": protocol,
                "protocol_version": 1,
                "bridge_version": "test",
                "available": True,
                "capabilities": capabilities,
                "reason": "",
            }))
            raise SystemExit(0)
        print(json.dumps({
            "protocol": protocol,
            "protocol_version": 1,
            "sequence": 1,
            "type": "bridge.ready",
            "data": {"capabilities": capabilities},
        }), flush=True)
        json.loads(sys.stdin.readline())
        print(json.dumps({
            "protocol": protocol,
            "protocol_version": 1,
            "sequence": 2,
            "type": "bridge.error",
            "data": {
                "code": "provider_error",
                "message": "provider rejected sk-child-only-secret",
            },
        }), flush=True)
        raise SystemExit(1)
        """,
    )
    runtime = HermesProfileRuntime(
        profile="jobs-profile",
        executable="/bin/false",
        profile_home=profile_home,
        bridge_command=bridge,
    )

    with pytest.raises(AgentRuntimeError) as caught:
        asyncio.run(runtime.invoke(invocation(), event_sink=lambda _event: None))

    diagnostic = str(caught.value)
    assert "provider_error" in diagnostic
    assert "sk-child-only-secret" not in diagnostic


def test_runtime_error_diagnostics_are_sanitized(tmp_path: Path) -> None:
    helper = make_executable(
        tmp_path,
        """
        import os
        import sys

        assert sys.argv[1:] == ["--final-only"]
        sys.stdin.read()
        print("api_key=" + os.environ["PROVIDER_API_KEY"], file=sys.stderr)
        raise SystemExit(7)
        """,
    )
    runtime = HermesProfileRuntime(
        profile="jobs-profile",
        executable="/bin/false",
        bridge_command=helper,
        profile_home=tmp_path,
        environment={"PROVIDER_API_KEY": "sk-do-not-leak"},
    )

    with pytest.raises(AgentRuntimeError) as caught:
        asyncio.run(runtime.invoke(invocation()))

    assert caught.value.returncode == 7
    assert "sk-do-not-leak" not in str(caught.value)
    assert "status 7" in str(caught.value)


def test_timeout_forcibly_stops_subprocess(tmp_path: Path) -> None:
    executable = make_executable(
        tmp_path,
        """
        import time
        time.sleep(5)
        """,
    )
    runtime = HermesProfileRuntime(
        profile="jobs-profile",
        executable="/bin/false",
        bridge_command=executable,
        profile_home=tmp_path,
        timeout_seconds=0.05,
    )

    with pytest.raises(AgentRuntimeTimeout):
        asyncio.run(runtime.invoke(invocation()))


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group semantics")
def test_cancellation_gracefully_signals_the_entire_process_group(tmp_path: Path) -> None:
    parent_marker = tmp_path / "parent-terminated"
    child_marker = tmp_path / "child-terminated"
    ready_marker = tmp_path / "child-ready"
    child_code = textwrap.dedent(
        f"""
        import signal
        import time
        from pathlib import Path

        marker = Path({str(child_marker)!r})
        ready = Path({str(ready_marker)!r})

        def stop(_signum, _frame):
            marker.write_text("term", encoding="utf-8")
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, stop)
        ready.write_text("ready", encoding="utf-8")
        while True:
            time.sleep(1)
        """
    )
    executable = make_executable(
        tmp_path,
        f"""
        import signal
        import subprocess
        import sys
        import time
        from pathlib import Path

        child_code = {child_code!r}
        child = subprocess.Popen([sys.executable, "-c", child_code])

        def stop(_signum, _frame):
            Path({str(parent_marker)!r}).write_text("term", encoding="utf-8")
            try:
                child.wait(timeout=1)
            except Exception:
                pass
            raise SystemExit(0)

        signal.signal(signal.SIGTERM, stop)
        deadline = time.monotonic() + 2
        while not Path({str(ready_marker)!r}).exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        while True:
            time.sleep(1)
        """,
    )
    runtime = HermesProfileRuntime(
        profile="jobs-profile",
        executable="/bin/false",
        bridge_command=executable,
        profile_home=tmp_path,
        timeout_seconds=5,
        shutdown_grace_seconds=1,
    )

    async def cancel_after_start() -> None:
        task = asyncio.create_task(runtime.invoke(invocation()))
        for _ in range(200):
            if ready_marker.exists():
                break
            await asyncio.sleep(0.01)
        assert ready_marker.exists()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(cancel_after_start())

    assert parent_marker.read_text(encoding="utf-8") == "term"
    assert child_marker.read_text(encoding="utf-8") == "term"


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX process-group semantics")
def test_timeout_force_kills_a_process_that_ignores_graceful_shutdown(tmp_path: Path) -> None:
    ready_marker = tmp_path / "stubborn-ready"
    executable = make_executable(
        tmp_path,
        f"""
        import signal
        import time
        from pathlib import Path

        signal.signal(signal.SIGTERM, signal.SIG_IGN)
        Path({str(ready_marker)!r}).write_text("ready", encoding="utf-8")
        time.sleep(30)
        """,
    )
    runtime = HermesProfileRuntime(
        profile="jobs-profile",
        executable="/bin/false",
        bridge_command=executable,
        profile_home=tmp_path,
        # Leave enough startup headroom for a loaded CI host; the 30-second
        # child sleep still proves that timeout escalation force-kills it.
        timeout_seconds=1.0,
        shutdown_grace_seconds=0.05,
    )

    started = time.monotonic()
    with pytest.raises(AgentRuntimeTimeout):
        asyncio.run(runtime.invoke(invocation()))

    assert ready_marker.exists()
    assert time.monotonic() - started < 3


def test_offline_runtimes_are_async_and_deterministic() -> None:
    call = invocation()
    scripted = ScriptedRuntime(["first", {"content": "second", "session_id": "s2"}])
    first = asyncio.run(scripted.invoke(call))
    second = asyncio.run(scripted.invoke(call.model_copy(update={"invocation_id": "inv-2"})))
    assert (first.content, second.content, second.session_id) == ("first", "second", "s2")

    deterministic = DeterministicRuntime()
    response_one = asyncio.run(deterministic.invoke(call))
    response_two = asyncio.run(deterministic.invoke(call))
    assert json.loads(response_one.content) == json.loads(response_two.content)
    assert response_one.session_id == response_two.session_id
