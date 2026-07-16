"""Auditable institutional memory and human approval stores.

The module deliberately keeps tool-facing writes and durable shared memory apart:
tool output can only create a staged :class:`MemoryCandidate`; promotion requires
an explicit approval, while the only automatic shared write path is a final
decision commit.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from enum import StrEnum
from pathlib import Path
from typing import Any, Iterator, Mapping
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _memory_id() -> str:
    return f"memory-{uuid4().hex}"


def _approval_id() -> str:
    return f"approval-{uuid4().hex}"


def _canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _sha256(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timestamps must include a timezone")
    return value.astimezone(timezone.utc)


def _dump_datetime(value: datetime | None) -> str | None:
    return None if value is None else _as_utc(value).isoformat()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MemoryScope(StrEnum):
    PRIVATE = "private"
    ORGANIZATION = "organization"
    USER = "user"


class MemoryStatus(StrEnum):
    STAGED = "staged"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class SharedWriteMode(StrEnum):
    FINAL_DECISION_ONLY = "final_decision_only"


class ApprovalLevel(StrEnum):
    L2 = "L2"
    L3 = "L3"


class ApprovalStatus(StrEnum):
    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"


class MemoryError(RuntimeError):
    """Base error for institutional memory operations."""


class MemoryNotFoundError(MemoryError):
    pass


class MemoryConflictError(MemoryError):
    pass


class MemoryPolicyError(MemoryError):
    pass


class ApprovalError(RuntimeError):
    """Base error for approval operations."""


class ApprovalNotFoundError(ApprovalError):
    pass


class ApprovalConflictError(ApprovalError):
    pass


class ApprovalPolicyError(ApprovalError):
    pass


class _MemoryBase(StrictModel):
    id: str = Field(default_factory=_memory_id, min_length=1)
    owner_id: str = Field(min_length=1)
    organization_id: str | None = None
    scope: MemoryScope
    content: str = Field(min_length=1)
    source_event_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    created_at: datetime = Field(default_factory=_utc_now)
    expires_at: datetime | None = None
    supersedes: str | None = None
    content_hash: str = ""
    status: MemoryStatus = MemoryStatus.STAGED

    @model_validator(mode="before")
    @classmethod
    def derive_content_hash(cls, value: Any) -> Any:
        if isinstance(value, BaseModel):
            value = value.model_dump(mode="python")
        if not isinstance(value, Mapping):
            return value
        payload = dict(value)
        content = payload.get("content")
        if isinstance(content, str):
            expected = _sha256(content)
            supplied = payload.get("content_hash")
            if supplied not in (None, "", expected):
                raise ValueError("content_hash does not match content")
            payload["content_hash"] = expected
        return payload

    @field_validator("created_at", "expires_at")
    @classmethod
    def normalize_datetime(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _as_utc(value)

    @model_validator(mode="after")
    def validate_scope_and_lifetime(self) -> "_MemoryBase":
        if self.scope == MemoryScope.ORGANIZATION and not self.organization_id:
            raise ValueError("organization scope requires organization_id")
        if self.expires_at is not None and self.expires_at <= self.created_at:
            raise ValueError("expires_at must be later than created_at")
        if self.supersedes == self.id:
            raise ValueError("a memory cannot supersede itself")
        if len(set(self.source_event_ids)) != len(self.source_event_ids):
            raise ValueError("source_event_ids must be unique")
        return self


class MemoryCandidate(_MemoryBase):
    """A non-visible memory awaiting promotion."""

    @model_validator(mode="after")
    def candidate_must_be_staged(self) -> "MemoryCandidate":
        if self.status != MemoryStatus.STAGED:
            raise ValueError("memory candidates must have staged status")
        return self


class MemoryRecord(_MemoryBase):
    """Persisted memory, including its current lifecycle status."""


class MemorySnapshot(StrictModel):
    records: list[MemoryRecord]
    snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class MemoryAuditEvent(StrictModel):
    id: int
    memory_id: str
    event: str
    actor_id: str
    created_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(StrictModel):
    id: str = Field(default_factory=_approval_id, min_length=1)
    idempotency_key: str = Field(min_length=1)
    level: ApprovalLevel
    action: str = Field(min_length=1)
    subject_id: str = Field(min_length=1)
    organization_id: str | None = None
    requested_by: str = Field(min_length=1)
    payload: dict[str, Any] = Field(default_factory=dict)
    status: ApprovalStatus = ApprovalStatus.PENDING
    created_at: datetime = Field(default_factory=_utc_now)
    resolved_at: datetime | None = None
    resolved_by: str | None = None
    resolution_reason: str = ""

    @field_validator("created_at", "resolved_at")
    @classmethod
    def normalize_datetime(cls, value: datetime | None) -> datetime | None:
        return None if value is None else _as_utc(value)

    @model_validator(mode="after")
    def validate_resolution(self) -> "ApprovalRequest":
        if self.status == ApprovalStatus.PENDING:
            if self.resolved_at is not None or self.resolved_by is not None:
                raise ValueError("pending approval cannot have resolution metadata")
        elif self.resolved_at is None or not self.resolved_by:
            raise ValueError("resolved approval requires resolved_at and resolved_by")
        return self


class ApprovalAuditEvent(StrictModel):
    id: int
    request_id: str
    event: str
    actor_id: str
    created_at: datetime
    details: dict[str, Any] = Field(default_factory=dict)


class _SQLiteStore:
    def __init__(self, database: str | Path = ":memory:") -> None:
        self.database = str(database)
        self._lock = threading.RLock()
        self._connection = sqlite3.connect(
            self.database,
            isolation_level=None,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA busy_timeout = 5000")
        if self.database != ":memory:":
            self._connection.execute("PRAGMA journal_mode = WAL")
            database_path = Path(self.database)
            os.chmod(database_path, 0o600)
            for suffix in ("-wal", "-shm"):
                sidecar = Path(f"{database_path}{suffix}")
                if sidecar.exists():
                    os.chmod(sidecar, 0o600)
        self.store_id = self._initialize_store_identity()

    def _initialize_store_identity(self) -> str:
        with self._transaction() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS hsa_store_metadata (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                )
                """
            )
            row = connection.execute(
                "SELECT value FROM hsa_store_metadata WHERE key = 'store_id'"
            ).fetchone()
            if row is None:
                value = f"store-{uuid4().hex}"
                connection.execute(
                    "INSERT INTO hsa_store_metadata(key, value) VALUES ('store_id', ?)",
                    (value,),
                )
                return value
            return str(row[0])

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            self._connection.execute("BEGIN IMMEDIATE")
            try:
                yield self._connection
            except BaseException:
                self._connection.rollback()
                raise
            else:
                self._connection.commit()

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def __enter__(self) -> "_SQLiteStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class InstitutionalMemoryStore(_SQLiteStore):
    """SQLite-backed memory with strict visibility and immutable history.

    ``search`` trusts its caller to supply authenticated requester, organization,
    and user identifiers. It never infers membership and never returns staged,
    rejected, superseded, or expired records.

    Only ``stage_tool_output`` should be exposed to agent tools. ``approve``,
    ``reject`` and ``commit_decision_memory`` are control-plane operations.
    """

    def __init__(
        self,
        database: str | Path = ":memory:",
        *,
        shared_write_mode: SharedWriteMode | str = SharedWriteMode.FINAL_DECISION_ONLY,
    ) -> None:
        self.shared_write_mode = SharedWriteMode(shared_write_mode)
        super().__init__(database)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS institutional_memory (
                id TEXT PRIMARY KEY,
                owner_id TEXT NOT NULL,
                organization_id TEXT,
                scope TEXT NOT NULL CHECK(scope IN ('private', 'organization', 'user')),
                content TEXT NOT NULL,
                source_event_ids TEXT NOT NULL,
                confidence REAL NOT NULL CHECK(confidence >= 0 AND confidence <= 1),
                created_at TEXT NOT NULL,
                expires_at TEXT,
                supersedes TEXT,
                content_hash TEXT NOT NULL,
                status TEXT NOT NULL
                    CHECK(status IN ('staged', 'approved', 'rejected', 'superseded'))
            )
            """,
            """
            CREATE INDEX IF NOT EXISTS institutional_memory_visibility
            ON institutional_memory(status, scope, owner_id, organization_id, expires_at)
            """,
            """
            CREATE TABLE IF NOT EXISTS memory_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                memory_id TEXT NOT NULL,
                event TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                details TEXT NOT NULL
            )
            """,
        )
        with self._transaction() as connection:
            for statement in statements:
                connection.execute(statement)

    @staticmethod
    def _row_to_record(row: sqlite3.Row) -> MemoryRecord:
        return MemoryRecord.model_validate(
            {
                "id": row["id"],
                "owner_id": row["owner_id"],
                "organization_id": row["organization_id"],
                "scope": row["scope"],
                "content": row["content"],
                "source_event_ids": json.loads(row["source_event_ids"]),
                "confidence": row["confidence"],
                "created_at": row["created_at"],
                "expires_at": row["expires_at"],
                "supersedes": row["supersedes"],
                "content_hash": row["content_hash"],
                "status": row["status"],
            }
        )

    @staticmethod
    def _same_definition(record: MemoryRecord, candidate: MemoryCandidate) -> bool:
        return record.model_dump(mode="json", exclude={"status"}) == candidate.model_dump(
            mode="json", exclude={"status"}
        )

    @staticmethod
    def _insert_candidate(connection: sqlite3.Connection, candidate: MemoryCandidate) -> None:
        connection.execute(
            """
            INSERT INTO institutional_memory (
                id, owner_id, organization_id, scope, content, source_event_ids,
                confidence, created_at, expires_at, supersedes, content_hash, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                candidate.id,
                candidate.owner_id,
                candidate.organization_id,
                candidate.scope.value,
                candidate.content,
                _canonical_json(candidate.source_event_ids),
                candidate.confidence,
                _dump_datetime(candidate.created_at),
                _dump_datetime(candidate.expires_at),
                candidate.supersedes,
                candidate.content_hash,
                candidate.status.value,
            ),
        )

    @staticmethod
    def _record_locked(connection: sqlite3.Connection, memory_id: str) -> MemoryRecord | None:
        row = connection.execute(
            "SELECT * FROM institutional_memory WHERE id = ?", (memory_id,)
        ).fetchone()
        return None if row is None else InstitutionalMemoryStore._row_to_record(row)

    @staticmethod
    def _write_audit_locked(
        connection: sqlite3.Connection,
        *,
        memory_id: str,
        event: str,
        actor_id: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO memory_audit_events(memory_id, event, actor_id, created_at, details)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                memory_id,
                event,
                actor_id,
                _dump_datetime(_utc_now()),
                _canonical_json(dict(details or {})),
            ),
        )

    def stage_candidate(
        self,
        candidate: MemoryCandidate | Mapping[str, Any],
        *,
        actor_id: str = "memory-tool",
        origin: str = "tool_output",
    ) -> MemoryRecord:
        staged = MemoryCandidate.model_validate(candidate)
        with self._transaction() as connection:
            existing = self._record_locked(connection, staged.id)
            if existing is not None:
                # The stage operation is already durable even if a later human
                # action advanced the candidate to approved/rejected/superseded.
                # Never resurrect or conflict with an identical progressed row.
                if self._same_definition(existing, staged):
                    return existing
                raise MemoryConflictError(f"memory id already exists: {staged.id}")
            self._insert_candidate(connection, staged)
            self._write_audit_locked(
                connection,
                memory_id=staged.id,
                event="staged",
                actor_id=actor_id,
                details={"origin": origin},
            )
            record = self._record_locked(connection, staged.id)
            assert record is not None
            return record

    def stage_tool_output(
        self,
        candidate: MemoryCandidate | Mapping[str, Any],
        *,
        tool_id: str,
    ) -> MemoryRecord:
        """The sole tool-facing entry point; it can never approve a record."""

        return self.stage_candidate(candidate, actor_id=tool_id, origin="tool_output")

    def _approve_locked(
        self,
        connection: sqlite3.Connection,
        record: MemoryRecord,
        *,
        approver_id: str,
    ) -> MemoryRecord:
        if record.status == MemoryStatus.APPROVED:
            return record
        if record.status != MemoryStatus.STAGED:
            raise MemoryConflictError(f"cannot approve memory in {record.status.value} status")
        if record.expires_at is not None and record.expires_at <= _utc_now():
            raise MemoryPolicyError("cannot approve an expired memory candidate")

        if record.supersedes is not None:
            previous = self._record_locked(connection, record.supersedes)
            if previous is None:
                raise MemoryNotFoundError(f"superseded memory does not exist: {record.supersedes}")
            if previous.status != MemoryStatus.APPROVED:
                raise MemoryConflictError("only an approved memory can be superseded")
            if (
                previous.scope != record.scope
                or previous.owner_id != record.owner_id
                or previous.organization_id != record.organization_id
            ):
                raise MemoryPolicyError(
                    "a replacement must keep the same scope, owner and organization"
                )
            connection.execute(
                "UPDATE institutional_memory SET status = ? WHERE id = ?",
                (MemoryStatus.SUPERSEDED.value, previous.id),
            )
            self._write_audit_locked(
                connection,
                memory_id=previous.id,
                event="superseded",
                actor_id=approver_id,
                details={"replacement_id": record.id},
            )

        connection.execute(
            "UPDATE institutional_memory SET status = ? WHERE id = ?",
            (MemoryStatus.APPROVED.value, record.id),
        )
        self._write_audit_locked(
            connection,
            memory_id=record.id,
            event="approved",
            actor_id=approver_id,
        )
        approved = self._record_locked(connection, record.id)
        assert approved is not None
        return approved

    def approve(self, memory_id: str, *, approver_id: str = "memory-approver") -> MemoryRecord:
        with self._transaction() as connection:
            record = self._record_locked(connection, memory_id)
            if record is None:
                raise MemoryNotFoundError(memory_id)
            return self._approve_locked(connection, record, approver_id=approver_id)

    def reject(
        self,
        memory_id: str,
        *,
        approver_id: str = "memory-approver",
        reason: str = "",
    ) -> MemoryRecord:
        with self._transaction() as connection:
            record = self._record_locked(connection, memory_id)
            if record is None:
                raise MemoryNotFoundError(memory_id)
            if record.status == MemoryStatus.REJECTED:
                return record
            if record.status != MemoryStatus.STAGED:
                raise MemoryConflictError(f"cannot reject memory in {record.status.value} status")
            connection.execute(
                "UPDATE institutional_memory SET status = ? WHERE id = ?",
                (MemoryStatus.REJECTED.value, memory_id),
            )
            self._write_audit_locked(
                connection,
                memory_id=memory_id,
                event="rejected",
                actor_id=approver_id,
                details={"reason": reason},
            )
            rejected = self._record_locked(connection, memory_id)
            assert rejected is not None
            return rejected

    def commit_decision_memory(
        self,
        candidate: MemoryCandidate | Mapping[str, Any],
        *,
        decision_event_id: str,
        committed_by: str,
        decision_is_final: bool,
    ) -> MemoryRecord:
        """Atomically stage and promote memory emitted by a final decision.

        Shared scopes cannot use this automatic path for drafts. A previously
        staged, byte-equivalent candidate may be committed idempotently.
        """

        staged = MemoryCandidate.model_validate(candidate)
        if not decision_event_id:
            raise MemoryPolicyError("decision_event_id is required")
        if not decision_is_final:
            raise MemoryPolicyError(
                "shared_write_mode=final_decision_only rejects non-final decisions"
            )
        if self.shared_write_mode != SharedWriteMode.FINAL_DECISION_ONLY:
            raise MemoryPolicyError("unsupported shared_write_mode")

        with self._transaction() as connection:
            existing = self._record_locked(connection, staged.id)
            if existing is None:
                self._insert_candidate(connection, staged)
                self._write_audit_locked(
                    connection,
                    memory_id=staged.id,
                    event="staged",
                    actor_id=committed_by,
                    details={
                        "origin": "final_decision",
                        "decision_event_id": decision_event_id,
                    },
                )
                existing = self._record_locked(connection, staged.id)
                assert existing is not None
            elif not self._same_definition(existing, staged):
                raise MemoryConflictError(f"memory id already exists: {staged.id}")

            if existing.status == MemoryStatus.APPROVED:
                return existing

            approved = self._approve_locked(connection, existing, approver_id=committed_by)
            self._write_audit_locked(
                connection,
                memory_id=staged.id,
                event="decision_committed",
                actor_id=committed_by,
                details={"decision_event_id": decision_event_id},
            )
            return approved

    def get(self, memory_id: str) -> MemoryRecord:
        with self._lock:
            record = self._record_locked(self._connection, memory_id)
        if record is None:
            raise MemoryNotFoundError(memory_id)
        return record

    def search(
        self,
        *,
        requester_id: str,
        organization_id: str | None = None,
        user_id: str | None = None,
        include_private: bool = True,
        query: str = "",
        now: datetime | None = None,
        limit: int = 100,
    ) -> list[MemoryRecord]:
        """Return only records visible to the authenticated caller context.

        ``owner_id`` means HSA owner for private memory and user owner for user
        memory. User-scoped records therefore require an explicit ``user_id``;
        merely knowing the user memory's owner value is not enough.
        """

        if not requester_id:
            raise ValueError("requester_id is required")
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        effective_now = _as_utc(now or _utc_now())

        visibility: list[str] = []
        parameters: list[Any] = [
            MemoryStatus.APPROVED.value,
            _dump_datetime(effective_now),
        ]
        if include_private:
            visibility.append("(scope = 'private' AND owner_id = ?)")
            parameters.append(requester_id)
        if organization_id is not None:
            visibility.append("(scope = 'organization' AND organization_id = ?)")
            parameters.append(organization_id)
        if user_id is not None:
            visibility.append("(scope = 'user' AND owner_id = ?)")
            parameters.append(user_id)

        sql = f"""
            SELECT * FROM institutional_memory
            WHERE status = ?
              AND (expires_at IS NULL OR expires_at > ?)
              AND ({" OR ".join(visibility) if visibility else "0"})
        """
        if query:
            sql += " AND instr(lower(content), lower(?)) > 0"
            parameters.append(query)
        sql += """
            ORDER BY scope ASC, COALESCE(organization_id, '') ASC,
                     owner_id ASC, created_at ASC, id ASC
            LIMIT ?
        """
        parameters.append(limit)

        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return [self._row_to_record(row) for row in rows]

    def snapshot(
        self,
        *,
        requester_id: str,
        organization_id: str | None = None,
        user_id: str | None = None,
        include_private: bool = True,
        query: str = "",
        now: datetime | None = None,
        limit: int = 1000,
    ) -> MemorySnapshot:
        records = self.search(
            requester_id=requester_id,
            organization_id=organization_id,
            user_id=user_id,
            include_private=include_private,
            query=query,
            now=now,
            limit=limit,
        )
        digest = _sha256(_canonical_json([record.model_dump(mode="json") for record in records]))
        return MemorySnapshot(records=records, snapshot_hash=digest)

    def audit_events(self, memory_id: str | None = None) -> list[MemoryAuditEvent]:
        sql = "SELECT * FROM memory_audit_events"
        parameters: tuple[Any, ...] = ()
        if memory_id is not None:
            sql += " WHERE memory_id = ?"
            parameters = (memory_id,)
        sql += " ORDER BY id ASC"
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return [
            MemoryAuditEvent(
                id=row["id"],
                memory_id=row["memory_id"],
                event=row["event"],
                actor_id=row["actor_id"],
                created_at=row["created_at"],
                details=json.loads(row["details"]),
            )
            for row in rows
        ]


class ApprovalStore(_SQLiteStore):
    """SQLite approval queue with idempotency and append-only audit events."""

    _LEVEL_RANK = {ApprovalLevel.L2: 2, ApprovalLevel.L3: 3}

    def __init__(self, database: str | Path = ":memory:") -> None:
        super().__init__(database)
        self._initialize_schema()

    def _initialize_schema(self) -> None:
        statements = (
            """
            CREATE TABLE IF NOT EXISTS approval_requests (
                id TEXT PRIMARY KEY,
                idempotency_key TEXT NOT NULL UNIQUE,
                level TEXT NOT NULL CHECK(level IN ('L2', 'L3')),
                action TEXT NOT NULL,
                subject_id TEXT NOT NULL,
                organization_id TEXT,
                requested_by TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL CHECK(status IN ('pending', 'approved', 'rejected')),
                created_at TEXT NOT NULL,
                resolved_at TEXT,
                resolved_by TEXT,
                resolution_reason TEXT NOT NULL
            )
            """,
            """
            CREATE TABLE IF NOT EXISTS approval_audit_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                request_id TEXT NOT NULL,
                event TEXT NOT NULL,
                actor_id TEXT NOT NULL,
                created_at TEXT NOT NULL,
                details TEXT NOT NULL
            )
            """,
        )
        with self._transaction() as connection:
            for statement in statements:
                connection.execute(statement)

    @staticmethod
    def _row_to_request(row: sqlite3.Row) -> ApprovalRequest:
        return ApprovalRequest.model_validate(
            {
                "id": row["id"],
                "idempotency_key": row["idempotency_key"],
                "level": row["level"],
                "action": row["action"],
                "subject_id": row["subject_id"],
                "organization_id": row["organization_id"],
                "requested_by": row["requested_by"],
                "payload": json.loads(row["payload"]),
                "status": row["status"],
                "created_at": row["created_at"],
                "resolved_at": row["resolved_at"],
                "resolved_by": row["resolved_by"],
                "resolution_reason": row["resolution_reason"],
            }
        )

    @staticmethod
    def _same_idempotent_action(existing: ApprovalRequest, requested: ApprovalRequest) -> bool:
        fields = {
            "level",
            "action",
            "subject_id",
            "organization_id",
            "requested_by",
            "payload",
        }
        return all(getattr(existing, field) == getattr(requested, field) for field in fields)

    @staticmethod
    def _request_locked(connection: sqlite3.Connection, request_id: str) -> ApprovalRequest | None:
        row = connection.execute(
            "SELECT * FROM approval_requests WHERE id = ?", (request_id,)
        ).fetchone()
        return None if row is None else ApprovalStore._row_to_request(row)

    @staticmethod
    def _write_audit_locked(
        connection: sqlite3.Connection,
        *,
        request_id: str,
        event: str,
        actor_id: str,
        details: Mapping[str, Any] | None = None,
    ) -> None:
        connection.execute(
            """
            INSERT INTO approval_audit_events(request_id, event, actor_id, created_at, details)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                request_id,
                event,
                actor_id,
                _dump_datetime(_utc_now()),
                _canonical_json(dict(details or {})),
            ),
        )

    def pending(self, request: ApprovalRequest | Mapping[str, Any]) -> ApprovalRequest:
        pending = ApprovalRequest.model_validate(request)
        if pending.status != ApprovalStatus.PENDING:
            raise ApprovalPolicyError("new approval request must be pending")

        with self._transaction() as connection:
            row = connection.execute(
                "SELECT * FROM approval_requests WHERE idempotency_key = ?",
                (pending.idempotency_key,),
            ).fetchone()
            if row is not None:
                existing = self._row_to_request(row)
                if not self._same_idempotent_action(existing, pending):
                    raise ApprovalConflictError(
                        "idempotency key was reused for a different approval action"
                    )
                return existing

            if self._request_locked(connection, pending.id) is not None:
                raise ApprovalConflictError(f"approval id already exists: {pending.id}")

            connection.execute(
                """
                INSERT INTO approval_requests (
                    id, idempotency_key, level, action, subject_id,
                    organization_id, requested_by, payload, status, created_at,
                    resolved_at, resolved_by, resolution_reason
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    pending.id,
                    pending.idempotency_key,
                    pending.level.value,
                    pending.action,
                    pending.subject_id,
                    pending.organization_id,
                    pending.requested_by,
                    _canonical_json(pending.payload),
                    pending.status.value,
                    _dump_datetime(pending.created_at),
                    None,
                    None,
                    "",
                ),
            )
            self._write_audit_locked(
                connection,
                request_id=pending.id,
                event="pending",
                actor_id=pending.requested_by,
                details={"level": pending.level.value, "action": pending.action},
            )
            stored = self._request_locked(connection, pending.id)
            assert stored is not None
            return stored

    create_pending = pending

    @classmethod
    def _authorize_level(
        cls,
        required: ApprovalLevel,
        supplied: ApprovalLevel | str,
    ) -> ApprovalLevel:
        approver_level = ApprovalLevel(supplied)
        if cls._LEVEL_RANK[approver_level] < cls._LEVEL_RANK[required]:
            raise ApprovalPolicyError(
                f"{approver_level.value} approver cannot resolve {required.value} request"
            )
        return approver_level

    def _resolve(
        self,
        request_id: str,
        *,
        status: ApprovalStatus,
        actor_id: str,
        approver_level: ApprovalLevel | str,
        reason: str,
    ) -> ApprovalRequest:
        with self._transaction() as connection:
            request = self._request_locked(connection, request_id)
            if request is None:
                raise ApprovalNotFoundError(request_id)
            self._authorize_level(request.level, approver_level)
            if request.status == status:
                return request
            if request.status != ApprovalStatus.PENDING:
                raise ApprovalConflictError(f"approval is already {request.status.value}")
            resolved_at = _utc_now()
            connection.execute(
                """
                UPDATE approval_requests
                SET status = ?, resolved_at = ?, resolved_by = ?, resolution_reason = ?
                WHERE id = ?
                """,
                (
                    status.value,
                    _dump_datetime(resolved_at),
                    actor_id,
                    reason,
                    request_id,
                ),
            )
            self._write_audit_locked(
                connection,
                request_id=request_id,
                event=status.value,
                actor_id=actor_id,
                details={
                    "approver_level": ApprovalLevel(approver_level).value,
                    "reason": reason,
                },
            )
            resolved = self._request_locked(connection, request_id)
            assert resolved is not None
            return resolved

    def approve(
        self,
        request_id: str,
        *,
        actor_id: str,
        approver_level: ApprovalLevel | str,
        reason: str = "",
    ) -> ApprovalRequest:
        return self._resolve(
            request_id,
            status=ApprovalStatus.APPROVED,
            actor_id=actor_id,
            approver_level=approver_level,
            reason=reason,
        )

    def reject(
        self,
        request_id: str,
        *,
        actor_id: str,
        approver_level: ApprovalLevel | str,
        reason: str = "",
    ) -> ApprovalRequest:
        return self._resolve(
            request_id,
            status=ApprovalStatus.REJECTED,
            actor_id=actor_id,
            approver_level=approver_level,
            reason=reason,
        )

    def get(self, request_id: str) -> ApprovalRequest:
        with self._lock:
            request = self._request_locked(self._connection, request_id)
        if request is None:
            raise ApprovalNotFoundError(request_id)
        return request

    def audit_events(self, request_id: str | None = None) -> list[ApprovalAuditEvent]:
        sql = "SELECT * FROM approval_audit_events"
        parameters: tuple[Any, ...] = ()
        if request_id is not None:
            sql += " WHERE request_id = ?"
            parameters = (request_id,)
        sql += " ORDER BY id ASC"
        with self._lock:
            rows = self._connection.execute(sql, parameters).fetchall()
        return [
            ApprovalAuditEvent(
                id=row["id"],
                request_id=row["request_id"],
                event=row["event"],
                actor_id=row["actor_id"],
                created_at=row["created_at"],
                details=json.loads(row["details"]),
            )
            for row in rows
        ]


__all__ = [
    "ApprovalAuditEvent",
    "ApprovalConflictError",
    "ApprovalError",
    "ApprovalLevel",
    "ApprovalNotFoundError",
    "ApprovalPolicyError",
    "ApprovalRequest",
    "ApprovalStatus",
    "ApprovalStore",
    "InstitutionalMemoryStore",
    "MemoryAuditEvent",
    "MemoryCandidate",
    "MemoryConflictError",
    "MemoryError",
    "MemoryNotFoundError",
    "MemoryPolicyError",
    "MemoryRecord",
    "MemoryScope",
    "MemorySnapshot",
    "MemoryStatus",
    "SharedWriteMode",
]
