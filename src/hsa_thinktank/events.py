"""Non-blocking, per-run event streams for live meeting observers."""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from copy import deepcopy
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from .audit import AuditTrail
from .models import AuditEvent, DecisionReport, content_hash, utc_now


EventLane = Literal["activity", "audit", "control"]
EventVisibility = Literal["public", "privileged"]


class RunEvent(BaseModel):
    """One immutable observer event; only ``audit`` events are report-authoritative."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal["1.0"] = "1.0"
    run_id: str = Field(min_length=1)
    sequence: int = Field(ge=1)
    lane: EventLane
    kind: str = Field(min_length=1)
    created_at: datetime
    visibility: EventVisibility = "public"
    wave_id: str | None = None
    task_index: int | None = Field(default=None, ge=0)
    phase: str | None = None
    round: int | None = Field(default=None, ge=0)
    hsa_id: str | None = None
    invocation_id: str | None = None
    audit_event_id: str | None = None
    audit_ordinal: int | None = Field(default=None, ge=1)
    audit_event_hash: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


_CLOSED = object()
_TERMINAL_KINDS = frozenset({"run_cancelled", "run_failed", "run_finished"})


class _SubscriberBuffer:
    """Bounded live buffer that sacrifices only response deltas under pressure."""

    def __init__(self, limit: int) -> None:
        self._limit = limit
        self._items: deque[RunEvent] = deque()
        self._wakeup = asyncio.Event()
        self._finished = False
        self._detached = False

    def put(self, event: RunEvent) -> None:
        if self._finished or self._detached:
            return

        if _is_terminal(event):
            self._drop_all_deltas()
        elif _is_delta(event):
            if len(self._items) >= self._limit:
                if self._coalesce_tail(event):
                    self._wakeup.set()
                    return
                if not self._drop_oldest_delta() or len(self._items) >= self._limit:
                    return
        else:
            while len(self._items) >= self._limit and self._drop_oldest_delta():
                pass

        # Critical events may overflow the soft limit when no delta can be
        # displaced. Runs contain finitely many such events and none are lost.
        self._items.append(event)
        self._wakeup.set()

    async def get(self) -> RunEvent | object:
        while True:
            if self._detached:
                return _CLOSED
            if self._items:
                return self._items.popleft()
            if self._finished:
                return _CLOSED
            self._wakeup.clear()
            await self._wakeup.wait()

    def finish(self) -> None:
        """Let a consumer drain critical buffered events, then stop."""

        self._finished = True
        self._wakeup.set()

    def detach(self) -> None:
        """Stop immediately and wake a consumer already blocked in ``get``."""

        self._detached = True
        self._items.clear()
        self._wakeup.set()

    def _coalesce_tail(self, event: RunEvent) -> bool:
        if not self._items or event.invocation_id is None:
            return False
        tail = self._items[-1]
        if not _is_delta(tail) or tail.invocation_id != event.invocation_id:
            return False
        tail_text = tail.payload.get("text")
        event_text = event.payload.get("text")
        if not isinstance(tail_text, str) or not isinstance(event_text, str):
            return False
        payload = deepcopy(event.payload)
        payload["text"] = tail_text + event_text
        payload["coalesced_count"] = _coalesced_count(tail) + 1
        first_runtime_sequence = tail.payload.get(
            "runtime_sequence_start",
            tail.payload.get("runtime_sequence"),
        )
        if isinstance(first_runtime_sequence, int):
            payload["runtime_sequence_start"] = first_runtime_sequence
        self._items[-1] = event.model_copy(update={"payload": payload})
        return True

    def _drop_oldest_delta(self) -> bool:
        for index, event in enumerate(self._items):
            if _is_delta(event):
                del self._items[index]
                return True
        return False

    def _drop_all_deltas(self) -> None:
        self._items = deque(event for event in self._items if not _is_delta(event))


class RunSubscription:
    """Async iterator over one run; closing it never cancels the run."""

    def __init__(
        self,
        stream: RunEventStream,
        buffer: _SubscriberBuffer,
    ) -> None:
        self._stream = stream
        self._buffer = buffer
        self._closed = False

    def __aiter__(self) -> RunSubscription:
        return self

    async def __anext__(self) -> RunEvent:
        if self._closed:
            raise StopAsyncIteration
        item = await self._buffer.get()
        if item is _CLOSED:
            self._closed = True
            self._stream._unsubscribe(self._buffer)
            raise StopAsyncIteration
        assert isinstance(item, RunEvent)
        return item

    async def aclose(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stream._unsubscribe(self._buffer)

    async def __aenter__(self) -> RunSubscription:
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.aclose()


class RunEventStream:
    """In-memory replay plus fan-out with no observer callbacks on the run path."""

    def __init__(
        self,
        run_id: str,
        *,
        clock: Callable[[], datetime] = utc_now,
        subscriber_buffer_limit: int = 256,
    ) -> None:
        if subscriber_buffer_limit < 1:
            raise ValueError("subscriber_buffer_limit must be at least 1")
        self.run_id = run_id
        self.clock = clock
        self.subscriber_buffer_limit = subscriber_buffer_limit
        self._sequence = 0
        self._history: list[RunEvent] = []
        self._subscribers: set[_SubscriberBuffer] = set()
        self._closed = False

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def has_subscribers(self) -> bool:
        """Whether a live observer currently needs runtime-level deltas."""

        return bool(self._subscribers)

    def publish(
        self,
        *,
        lane: EventLane,
        kind: str,
        created_at: datetime | None = None,
        visibility: EventVisibility = "public",
        wave_id: str | None = None,
        task_index: int | None = None,
        phase: str | None = None,
        round: int | None = None,
        hsa_id: str | None = None,
        invocation_id: str | None = None,
        audit_event_id: str | None = None,
        audit_ordinal: int | None = None,
        audit_event_hash: str | None = None,
        payload: dict[str, Any] | None = None,
        retain: bool = True,
    ) -> RunEvent | None:
        """Publish without awaiting subscribers; optionally retain for replay."""

        if self._closed:
            return None
        self._sequence += 1
        event = RunEvent(
            run_id=self.run_id,
            sequence=self._sequence,
            lane=lane,
            kind=kind,
            created_at=created_at or self.clock(),
            visibility=visibility,
            wave_id=wave_id,
            task_index=task_index,
            phase=phase,
            round=round,
            hsa_id=hsa_id,
            invocation_id=invocation_id,
            audit_event_id=audit_event_id,
            audit_ordinal=audit_ordinal,
            audit_event_hash=audit_event_hash,
            payload=deepcopy(payload or {}),
        )
        if retain:
            self._history.append(event)
        for buffer in tuple(self._subscribers):
            buffer.put(event.model_copy(deep=True))
        return event

    def publish_audit(self, event: AuditEvent) -> RunEvent | None:
        payload = event.payload
        event_round = payload.get("round")
        return self.publish(
            lane="audit",
            kind=event.event_type,
            created_at=event.created_at,
            visibility="privileged",
            phase=_optional_string(payload.get("phase")),
            round=event_round if isinstance(event_round, int) and event_round >= 0 else None,
            hsa_id=_optional_string(payload.get("hsa_id") or payload.get("sender_id")),
            invocation_id=_optional_string(payload.get("invocation_id")),
            audit_event_id=event.id,
            audit_ordinal=event.ordinal,
            audit_event_hash=event.event_hash,
            payload=payload,
        )

    def subscribe(self, *, after_sequence: int = 0) -> RunSubscription:
        if after_sequence < 0:
            raise ValueError("after_sequence cannot be negative")
        buffer = _SubscriberBuffer(self.subscriber_buffer_limit)
        for event in self._history:
            if event.sequence > after_sequence:
                buffer.put(event.model_copy(deep=True))
        if self._closed:
            buffer.finish()
        else:
            self._subscribers.add(buffer)
        return RunSubscription(self, buffer)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for buffer in tuple(self._subscribers):
            buffer.finish()
        self._subscribers.clear()

    def _unsubscribe(self, buffer: _SubscriberBuffer) -> None:
        self._subscribers.discard(buffer)
        buffer.detach()


class PublishedAuditTrail(AuditTrail):
    """AuditTrail that mirrors committed events after they enter the hash chain."""

    def __init__(
        self,
        run_id: str,
        *,
        stream: RunEventStream,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        super().__init__(run_id, clock=clock)
        self._stream = stream

    def append(self, event_type: str, payload: dict[str, Any]) -> AuditEvent:
        event = super().append(event_type, payload)
        self._stream.publish_audit(event)
        return event


class RunHandle:
    """A running decision plus a detachable, replayable event subscription."""

    def __init__(
        self,
        *,
        run_id: str,
        stream: RunEventStream,
        task: asyncio.Task[DecisionReport],
        persisted: bool,
    ) -> None:
        self.run_id = run_id
        self._stream = stream
        self._task = task
        self._persisted = persisted
        task.add_done_callback(self._ensure_terminal)

    @property
    def done(self) -> bool:
        return self._task.done()

    def subscribe(self, *, after_sequence: int = 0) -> RunSubscription:
        return self._stream.subscribe(after_sequence=after_sequence)

    async def result(self) -> DecisionReport:
        """Wait without allowing cancellation of this waiter to cancel the run."""

        return await asyncio.shield(self._task)

    def cancel(self) -> bool:
        """Explicitly cancel the run; merely closing a subscription never calls this."""

        if self._task.done():
            return False
        return self._task.cancel()

    def _ensure_terminal(self, task: asyncio.Task[DecisionReport]) -> None:
        """Close runs cancelled before their coroutine executes its terminal wrapper."""

        if task.cancelled():
            if not self._stream.closed:
                publish_run_terminal(
                    self._stream,
                    error=asyncio.CancelledError(),
                    persisted=self._persisted,
                )
            return
        try:
            report = task.result()
        except BaseException as exc:  # defensive fallback for pre-wrapper task failures
            if not self._stream.closed:
                publish_run_terminal(self._stream, error=exc, persisted=self._persisted)
        else:
            if not self._stream.closed:
                publish_run_terminal(self._stream, report=report, persisted=self._persisted)


def publish_run_terminal(
    stream: RunEventStream,
    *,
    report: DecisionReport | None = None,
    error: BaseException | None = None,
    persisted: bool,
) -> None:
    """Publish exactly one terminal control event and close the stream."""

    if isinstance(error, asyncio.CancelledError):
        stream.publish(lane="control", kind="run_cancelled", payload={})
    elif error is not None:
        stream.publish(
            lane="control",
            kind="run_failed",
            payload={"error_type": type(error).__name__},
        )
    else:
        assert report is not None
        stream.publish(
            lane="control",
            kind="run_finished",
            payload={
                "status": report.status,
                "report_hash": content_hash(report),
                "trace_root_hash": report.trace_root_hash,
                "persisted": persisted,
            },
        )
    stream.close()


def _optional_string(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _is_delta(event: RunEvent) -> bool:
    return event.kind == "output_delta"


def _is_terminal(event: RunEvent) -> bool:
    return event.lane == "control" and event.kind in _TERMINAL_KINDS


def _coalesced_count(event: RunEvent) -> int:
    value = event.payload.get("coalesced_count", 1)
    return value if type(value) is int and value >= 1 else 1


__all__ = [
    "PublishedAuditTrail",
    "RunEvent",
    "RunEventStream",
    "RunHandle",
    "RunSubscription",
    "publish_run_terminal",
]
