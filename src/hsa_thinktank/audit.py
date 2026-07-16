"""In-memory hash-chained audit trail persisted with every decision report."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime
from typing import Any

from .models import AuditEvent, canonical_json, content_hash, utc_now


class AuditTrail:
    def __init__(
        self,
        run_id: str,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.run_id = run_id
        self.clock = clock
        self.events: list[AuditEvent] = []

    @property
    def root_hash(self) -> str:
        return self.events[-1].event_hash if self.events else content_hash([])

    def append(self, event_type: str, payload: dict[str, Any]) -> AuditEvent:
        ordinal = len(self.events) + 1
        previous = self.events[-1].event_hash if self.events else "0" * 64
        created_at = self.clock()
        digest_payload = {
            "run_id": self.run_id,
            "ordinal": ordinal,
            "event_type": event_type,
            "payload": payload,
            "previous_hash": previous,
            "created_at": created_at.isoformat(),
        }
        event = AuditEvent(
            id=f"{self.run_id}-event-{ordinal:04d}",
            run_id=self.run_id,
            ordinal=ordinal,
            event_type=event_type,
            payload=payload,
            previous_hash=previous,
            event_hash=content_hash(canonical_json(digest_payload)),
            created_at=created_at,
        )
        self.events.append(event)
        return event

    def verify(self) -> bool:
        if not self.events:
            return False
        previous = "0" * 64
        for ordinal, event in enumerate(self.events, start=1):
            if event.run_id != self.run_id:
                return False
            if event.ordinal != ordinal:
                return False
            if event.id != f"{self.run_id}-event-{ordinal:04d}":
                return False
            digest_payload = {
                "run_id": event.run_id,
                "ordinal": event.ordinal,
                "event_type": event.event_type,
                "payload": event.payload,
                "previous_hash": previous,
                "created_at": event.created_at.isoformat(),
            }
            expected = content_hash(canonical_json(digest_payload))
            if event.previous_hash != previous or event.event_hash != expected:
                return False
            previous = event.event_hash
        return True
