from __future__ import annotations

import asyncio
import gc
import json
from datetime import UTC, datetime

import pytest

from hsa_thinktank.catalog import Catalog
from hsa_thinktank.demo import demo_responder
from hsa_thinktank.events import RunEvent, RunEventStream, RunHandle
from hsa_thinktank.models import DecisionOption, DecisionProblem
from hsa_thinktank.orchestrator import ThinkTank
from hsa_thinktank.runtime import AgentInvocation, RawAgentResponse, RuntimeStreamEvent


def _problem() -> DecisionProblem:
    return DecisionProblem(
        id="decision-live-events",
        question="Should we launch?",
        options=[
            DecisionOption(id="launch", description="Launch with checkpoints"),
            DecisionOption(id="wait", description="Wait for more evidence"),
        ],
    )


class DelayedRuntime:
    name = "delayed"

    async def invoke(self, invocation: AgentInvocation) -> RawAgentResponse:
        delay = {
            "charlie-munger": 0.005,
            "donella-meadows": 0.015,
            "steve-jobs": 0.08,
        }[invocation.hsa_id]
        await asyncio.sleep(delay)
        return RawAgentResponse(
            content=json.dumps(demo_responder(invocation), ensure_ascii=False),
            runtime=self.name,
        )


class BlockingRuntime:
    name = "blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def invoke(self, invocation: AgentInvocation) -> RawAgentResponse:
        self.started.set()
        await self.release.wait()
        return RawAgentResponse(
            content=json.dumps(demo_responder(invocation), ensure_ascii=False),
            runtime=self.name,
        )


class StreamingRuntime:
    name = "streaming"

    def __init__(self) -> None:
        self.sink_presence: list[bool] = []

    async def invoke(self, invocation: AgentInvocation, *, event_sink=None) -> RawAgentResponse:
        self.sink_presence.append(event_sink is not None)
        content = json.dumps(demo_responder(invocation), ensure_ascii=False)
        if event_sink is not None:
            common = {
                "invocation_id": invocation.invocation_id,
                "hsa_id": invocation.hsa_id,
                "phase": invocation.phase,
            }
            event_sink(
                RuntimeStreamEvent(
                    event_type="bridge_ready",
                    sequence=1,
                    payload={"capabilities": ["response_delta"]},
                    **common,
                )
            )
            event_sink(
                RuntimeStreamEvent(
                    event_type="response_delta",
                    sequence=2,
                    content="visible fragment",
                    **common,
                )
            )
            event_sink(
                RuntimeStreamEvent(
                    event_type="response_completed",
                    sequence=3,
                    content=content,
                    payload={"stream_mode": "ndjson"},
                    **common,
                )
            )
        return RawAgentResponse(content=content, runtime=self.name)


def test_first_valid_output_is_streamed_before_wave_commit_but_canonical_order_is_stable() -> None:
    async def scenario():
        handle = ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=DelayedRuntime(),
        ).start_run(
            _problem(),
            organization_id="product-roundtable",
            persist=False,
        )
        subscription = handle.subscribe()
        events: list[RunEvent] = []
        first_output: RunEvent | None = None
        while first_output is None:
            event = await anext(subscription)
            events.append(event)
            if event.kind == "agent_output_accepted" and event.phase == "independent_ballot":
                first_output = event

        assert first_output.hsa_id == "charlie-munger"
        assert first_output.visibility == "privileged"
        assert first_output.payload["value"]["preferred_option_id"] == "launch"
        assert not handle.done

        async for event in subscription:
            events.append(event)
        report = await handle.result()
        return handle, report, events

    handle, report, events = asyncio.run(scenario())

    first_wave_outputs = [
        event.hsa_id
        for event in events
        if event.kind == "agent_output_accepted" and event.phase == "independent_ballot"
    ]
    assert first_wave_outputs == ["charlie-munger", "donella-meadows", "steve-jobs"]

    canonical_members = [
        event.payload["hsa_id"]
        for event in report.audit_events
        if event.event_type == "runtime_completed"
        and event.payload["phase"] == "independent_ballot"
    ]
    assert canonical_members == ["steve-jobs", "charlie-munger", "donella-meadows"]
    assert [call.hsa_id for call in report.runtime_calls[:3]] == canonical_members
    assert handle.run_id == report.run_id

    streamed_audit = [event for event in events if event.lane == "audit"]
    assert [
        (event.audit_ordinal, event.audit_event_id, event.audit_event_hash)
        for event in streamed_audit
    ] == [
        (event.ordinal, event.id, event.event_hash) for event in report.audit_events
    ]
    assert events[-1].lane == "control"
    assert events[-1].kind == "run_finished"


def test_subscription_can_detach_without_cancelling_and_late_subscribers_replay() -> None:
    async def scenario():
        handle = ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=DelayedRuntime(),
        ).start_run(
            _problem(),
            organization_id="product-roundtable",
            persist=False,
        )
        subscription = handle.subscribe()
        first = await anext(subscription)
        await subscription.aclose()
        report = await handle.result()
        replay = [event async for event in handle.subscribe(after_sequence=first.sequence)]
        return handle, report, first, replay

    handle, report, first, replay = asyncio.run(scenario())

    assert first.kind == "run_created"
    assert handle.done
    assert report.status == "decided"
    assert replay
    assert all(event.sequence > first.sequence for event in replay)
    assert replay[-1].kind == "run_finished"


def test_closing_subscription_wakes_a_blocked_consumer() -> None:
    async def scenario():
        stream = RunEventStream("run-blocked-consumer")
        subscription = stream.subscribe()
        pending = asyncio.create_task(anext(subscription))
        await asyncio.sleep(0)

        await subscription.aclose()

        with pytest.raises(StopAsyncIteration):
            await asyncio.wait_for(pending, timeout=0.1)

    asyncio.run(scenario())


def test_non_retained_deltas_are_live_only_and_durable_events_still_replay() -> None:
    async def scenario():
        stream = RunEventStream("run-live-only")
        live = stream.subscribe()
        delta = stream.publish(
            lane="activity",
            kind="output_delta",
            payload={"text": "token"},
            retain=False,
        )
        durable = stream.publish(lane="control", kind="checkpoint")
        stream.close()
        live_events = [event async for event in live]
        replay_events = [event async for event in stream.subscribe()]
        return delta, durable, live_events, replay_events

    delta, durable, live_events, replay_events = asyncio.run(scenario())

    assert delta is not None
    assert durable is not None
    assert [event.kind for event in live_events] == ["output_delta", "checkpoint"]
    assert [event.kind for event in replay_events] == ["checkpoint"]
    assert replay_events[0].sequence == durable.sequence


def test_slow_subscriber_coalesces_consecutive_deltas_at_its_soft_limit() -> None:
    async def scenario():
        stream = RunEventStream("run-coalesced-deltas", subscriber_buffer_limit=2)
        subscription = stream.subscribe()
        checkpoint = stream.publish(lane="activity", kind="checkpoint")
        stream.publish(
            lane="activity",
            kind="output_delta",
            invocation_id="invocation-a",
            payload={"text": "first ", "runtime_sequence": 10},
            retain=False,
        )
        latest = stream.publish(
            lane="activity",
            kind="output_delta",
            invocation_id="invocation-a",
            payload={"text": "second", "runtime_sequence": 11},
            retain=False,
        )
        stream.close()
        return checkpoint, latest, [event async for event in subscription]

    checkpoint, latest, events = asyncio.run(scenario())

    assert checkpoint is not None
    assert latest is not None
    assert [event.kind for event in events] == ["checkpoint", "output_delta"]
    assert [event.sequence for event in events] == [checkpoint.sequence, latest.sequence]
    assert events[-1].payload == {
        "text": "first second",
        "runtime_sequence": 11,
        "coalesced_count": 2,
        "runtime_sequence_start": 10,
    }


def test_terminal_discards_delta_backlog_without_losing_critical_events() -> None:
    async def scenario():
        stream = RunEventStream("run-terminal-priority", subscriber_buffer_limit=3)
        subscription = stream.subscribe()
        accepted = stream.publish(
            lane="activity",
            kind="agent_output_accepted",
            invocation_id="invocation-a",
        )
        for sequence in range(20):
            stream.publish(
                lane="activity",
                kind="output_delta",
                invocation_id=f"invocation-{sequence % 2}",
                payload={"text": str(sequence)},
                retain=False,
            )
        audit = stream.publish(lane="audit", kind="runtime_completed")
        terminal = stream.publish(lane="control", kind="run_finished")
        stream.close()
        return accepted, audit, terminal, [event async for event in subscription]

    accepted, audit, terminal, events = asyncio.run(scenario())

    assert accepted is not None
    assert audit is not None
    assert terminal is not None
    assert [event.kind for event in events] == [
        "agent_output_accepted",
        "runtime_completed",
        "run_finished",
    ]
    assert [event.sequence for event in events] == [
        accepted.sequence,
        audit.sequence,
        terminal.sequence,
    ]


def test_critical_events_may_overflow_soft_limit_but_are_never_dropped() -> None:
    async def scenario():
        stream = RunEventStream("run-critical-overflow", subscriber_buffer_limit=1)
        subscription = stream.subscribe()
        stream.publish(lane="activity", kind="agent_output_accepted")
        stream.publish(lane="audit", kind="message_accepted")
        stream.publish(lane="control", kind="run_failed")
        stream.close()
        return [event async for event in subscription]

    events = asyncio.run(scenario())

    assert [event.kind for event in events] == [
        "agent_output_accepted",
        "message_accepted",
        "run_failed",
    ]
    assert [event.sequence for event in events] == sorted(event.sequence for event in events)


def test_runtime_stream_events_reach_live_observer_without_replaying_deltas() -> None:
    async def scenario():
        runtime = StreamingRuntime()
        handle = ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=runtime,
        ).start_run(
            _problem(),
            organization_id="product-roundtable",
            persist=False,
        )
        live = [event async for event in handle.subscribe()]
        report = await handle.result()
        replay = [event async for event in handle.subscribe()]
        return runtime, report, live, replay

    runtime, report, live, replay = asyncio.run(scenario())

    assert report.status == "decided"
    assert runtime.sink_presence and all(runtime.sink_presence)
    assert any(event.kind == "runtime_stream_ready" for event in live)
    delta = next(event for event in live if event.kind == "output_delta")
    assert delta.payload["text"] == "visible fragment"
    received = next(event for event in live if event.kind == "response_received")
    assert "content" not in received.payload
    assert received.payload["content_size"] > 0
    assert not any(event.kind == "output_delta" for event in replay)
    assert any(event.kind == "agent_output_accepted" for event in replay)


def test_headless_decide_does_not_enable_runtime_streaming() -> None:
    async def scenario():
        runtime = StreamingRuntime()
        report = await ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=runtime,
        ).decide(
            _problem(),
            organization_id="product-roundtable",
            persist=False,
        )
        return runtime, report

    runtime, report = asyncio.run(scenario())

    assert report.status == "decided"
    assert runtime.sink_presence and not any(runtime.sink_presence)


def test_start_run_uses_the_think_tank_clock_for_activity_events() -> None:
    async def scenario():
        fixed_time = datetime(2035, 6, 7, 8, 9, tzinfo=UTC)
        handle = ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=DelayedRuntime(),
            clock=lambda: fixed_time,
        ).start_run(
            _problem(),
            organization_id="product-roundtable",
            persist=False,
        )
        subscription = handle.subscribe()
        created = await anext(subscription)
        assert handle.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handle.result()
        events = [created, *[event async for event in subscription]]
        return fixed_time, events

    fixed_time, events = asyncio.run(scenario())

    assert events
    assert all(event.created_at == fixed_time for event in events)


def test_closed_stream_done_callback_retrieves_background_task_exception() -> None:
    async def scenario():
        contexts: list[dict[str, object]] = []
        loop = asyncio.get_running_loop()
        previous_handler = loop.get_exception_handler()
        loop.set_exception_handler(lambda _loop, context: contexts.append(context))

        async def fail():
            raise RuntimeError("background failure")

        stream = RunEventStream("run-failed-in-background")
        task = asyncio.create_task(fail())
        handle = RunHandle(
            run_id=stream.run_id,
            stream=stream,
            task=task,
            persisted=False,
        )
        stream.close()
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert handle.done

        del handle
        del task
        gc.collect()
        await asyncio.sleep(0)
        loop.set_exception_handler(previous_handler)
        return contexts

    contexts = asyncio.run(scenario())

    assert not any(
        context.get("message") == "Task exception was never retrieved" for context in contexts
    )


def test_explicit_cancel_emits_terminal_event_without_a_decision_report() -> None:
    async def scenario():
        runtime = BlockingRuntime()
        handle = ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=runtime,
        ).start_run(
            _problem(),
            organization_id="product-roundtable",
            persist=False,
        )
        subscription = handle.subscribe()
        await runtime.started.wait()
        assert handle.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handle.result()
        events = [event async for event in subscription]
        return handle, events

    handle, events = asyncio.run(scenario())

    assert handle.done
    assert events[-1].lane == "control"
    assert events[-1].kind == "run_cancelled"
    assert not any(event.kind == "run_finished" for event in events)


def test_cancel_before_runner_starts_still_closes_the_event_stream() -> None:
    async def scenario():
        handle = ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=DelayedRuntime(),
        ).start_run(
            _problem(),
            organization_id="product-roundtable",
            persist=False,
        )
        subscription = handle.subscribe()
        assert handle.cancel()
        with pytest.raises(asyncio.CancelledError):
            await handle.result()
        await asyncio.sleep(0)
        return [event async for event in subscription]

    events = asyncio.run(scenario())

    assert [event.kind for event in events] == ["run_created", "run_cancelled"]


def test_failed_run_closes_stream_with_sanitized_terminal_event() -> None:
    async def scenario():
        problem = DecisionProblem(
            id="decision-live-events-failure",
            question="Generate options first",
        )

        class InvalidRuntime:
            name = "invalid"

            async def invoke(self, _invocation: AgentInvocation) -> RawAgentResponse:
                return RawAgentResponse(content="not-json", runtime=self.name)

        handle = ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=InvalidRuntime(),
        ).start_run(
            problem,
            organization_id="product-roundtable",
            persist=False,
        )
        subscription = handle.subscribe()
        with pytest.raises(Exception, match="failed to generate"):
            await handle.result()
        return [event async for event in subscription]

    events = asyncio.run(scenario())

    rejected = [event for event in events if event.kind == "agent_output_rejected"]
    assert len(rejected) == 1
    assert rejected[0].visibility == "privileged"
    assert "structured output rejected" in rejected[0].payload["error"]
    assert events[-1].kind == "run_failed"
    assert events[-1].payload == {"error_type": "ProtocolError"}
