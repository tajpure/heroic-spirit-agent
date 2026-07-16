"""Owner-only, atomic persistence for editable think-tank chat sessions.

Only user turns and completed decision-report references are durable context.  In
particular, orchestration messages, member drafts, runtime calls, and audit events
are never accepted by this store or copied into the next-round context.
"""

from __future__ import annotations

import fcntl
import json
import os
import re
import stat
import tempfile
import threading
from collections.abc import Callable
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated, Any, Iterator, Literal
from uuid import uuid4

from pydantic import Field, field_validator, model_validator

from .models import (
    Basis,
    DecisionOption,
    DecisionReport,
    DecisionStatus,
    RiskTier,
    StrictModel,
    canonical_json,
    content_hash,
    utc_now,
)


CHAT_ID_PATTERN = r"^chat-[0-9a-f]{16}$"
HASH_PATTERN = r"^[0-9a-f]{64}$"
_CHAT_ID_RE = re.compile(CHAT_ID_PATTERN)
_PROCESS_LOCKS_GUARD = threading.Lock()
_PROCESS_LOCKS: dict[Path, threading.RLock] = {}


def _process_lock(path: Path) -> threading.RLock:
    """Return one process-wide lock shared by every store for ``path``."""

    with _PROCESS_LOCKS_GUARD:
        return _PROCESS_LOCKS.setdefault(path, threading.RLock())


def _aware_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("chat timestamps must be timezone-aware")
    return value.astimezone(UTC)


class PublicMeetingSelection(StrictModel):
    """The non-sensitive routing details retained in a chat transcript."""

    mode: Literal["auto", "explicit"]
    organization_id: str
    protocol: Literal["roundtable", "red_team", "cabinet"]
    selected_hsa_ids: list[str]
    matched_signals: list[str]
    reasons: list[str]


class PublicRationaleClaim(StrictModel):
    claim: str
    basis: Basis
    principle_ids: list[str]
    evidence_ids: list[str]


class PublicDecisionSummary(StrictModel):
    """Strict allow-list of decision fields safe for subsequent chat context."""

    schema_version: str
    run_id: str
    decision_id: str
    question: str
    risk_tier: RiskTier
    options: list[DecisionOption]
    organization_id: str
    protocol_name: str
    meeting_selection: PublicMeetingSelection
    status: DecisionStatus
    status_reason: str
    selected_option_id: str | None
    selected_option: str | None
    option_scores: dict[str, float]
    confidence: float = Field(ge=0.0, le=1.0)
    rationale_claims: list[PublicRationaleClaim]
    assumptions: list[str]
    unresolved_risks: list[str]
    dissent: list[str]
    next_actions: list[str]
    approval_ids: list[str]
    decision_binding_hash: str = Field(pattern=HASH_PATTERN)
    trace_root_hash: str = Field(pattern=HASH_PATTERN)


class UserTurn(StrictModel):
    kind: Literal["user"] = "user"
    id: str = Field(default_factory=lambda: f"turn-{uuid4().hex}")
    content: str = Field(min_length=1)
    created_at: datetime = Field(default_factory=utc_now)

    _normalize_created_at = field_validator("created_at")(_aware_utc)


class DecisionReference(StrictModel):
    kind: Literal["decision"] = "decision"
    id: str = Field(default_factory=lambda: f"decision-ref-{uuid4().hex}")
    run_id: str = Field(min_length=1)
    decision_id: str = Field(min_length=1)
    report_hash: str = Field(pattern=HASH_PATTERN)
    trace_root_hash: str = Field(pattern=HASH_PATTERN)
    decision_binding_hash: str = Field(pattern=HASH_PATTERN)
    confirmed: Literal[True] = True
    public_summary: PublicDecisionSummary
    created_at: datetime = Field(default_factory=utc_now)

    _normalize_created_at = field_validator("created_at")(_aware_utc)

    @model_validator(mode="after")
    def validate_summary_identity(self) -> "DecisionReference":
        if self.public_summary.run_id != self.run_id:
            raise ValueError("public summary run_id does not match decision reference")
        if self.public_summary.decision_id != self.decision_id:
            raise ValueError("public summary decision_id does not match decision reference")
        if self.public_summary.trace_root_hash != self.trace_root_hash:
            raise ValueError("public summary trace root does not match decision reference")
        if self.public_summary.decision_binding_hash != self.decision_binding_hash:
            raise ValueError("public summary binding does not match decision reference")
        return self


ChatTurn = Annotated[UserTurn | DecisionReference, Field(discriminator="kind")]


class ChatSession(StrictModel):
    schema_version: Literal["1.0"] = "1.0"
    id: str = Field(pattern=CHAT_ID_PATTERN)
    title: str = Field(min_length=1)
    turns: list[ChatTurn] = Field(default_factory=list)
    context_start: int = Field(default=0, ge=0)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    _normalize_created_at = field_validator("created_at")(_aware_utc)
    _normalize_updated_at = field_validator("updated_at")(_aware_utc)

    @model_validator(mode="after")
    def validate_session(self) -> "ChatSession":
        if self.context_start > len(self.turns):
            raise ValueError("context_start cannot exceed the number of turns")
        turn_ids = [turn.id for turn in self.turns]
        if len(turn_ids) != len(set(turn_ids)):
            raise ValueError("chat turn ids must be unique")
        if self.updated_at < self.created_at:
            raise ValueError("updated_at cannot precede created_at")
        return self


class ChatSessionSummary(StrictModel):
    id: str = Field(pattern=CHAT_ID_PATTERN)
    title: str
    turn_count: int = Field(ge=0)
    context_turn_count: int = Field(ge=0)
    created_at: datetime
    updated_at: datetime


class ChatContextItem(StrictModel):
    """A model-ready context item; no live HSA draft kind exists by design."""

    role: Literal["user", "assistant"]
    kind: Literal["user_message", "confirmed_decision"]
    content: str = Field(min_length=1)
    run_id: str | None = None
    decision_id: str | None = None


def public_decision_summary(report: DecisionReport) -> PublicDecisionSummary:
    """Project a completed report through an explicit public-field allow-list."""

    return PublicDecisionSummary(
        schema_version=report.schema_version,
        run_id=report.run_id,
        decision_id=report.decision_id,
        question=report.frozen_problem.question,
        risk_tier=report.frozen_problem.risk_tier,
        options=report.frozen_problem.options,
        organization_id=report.organization_id,
        protocol_name=report.protocol_name,
        meeting_selection=PublicMeetingSelection(
            mode=report.meeting_selection.mode,
            organization_id=report.meeting_selection.organization_id,
            protocol=report.meeting_selection.protocol,
            selected_hsa_ids=report.meeting_selection.selected_hsa_ids,
            matched_signals=report.meeting_selection.matched_signals,
            reasons=report.meeting_selection.reasons,
        ),
        status=report.status,
        status_reason=report.status_reason,
        selected_option_id=report.selected_option_id,
        selected_option=report.selected_option,
        option_scores=report.option_scores,
        confidence=report.confidence,
        rationale_claims=[
            PublicRationaleClaim(
                claim=claim.claim,
                basis=claim.basis,
                principle_ids=claim.principle_ids,
                evidence_ids=claim.evidence_ids,
            )
            for claim in report.rationale_claims
        ],
        assumptions=report.assumptions,
        unresolved_risks=report.unresolved_risks,
        dissent=report.dissent,
        next_actions=report.next_actions,
        approval_ids=report.approval_ids,
        decision_binding_hash=report.decision_binding_hash,
        trace_root_hash=report.trace_root_hash,
    )


class LocalChatStore:
    """Filesystem-backed chat store with owner-only permissions and atomic writes."""

    def __init__(
        self,
        root: str | Path,
        *,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.root = Path(root)
        self._clock = clock
        self._lock = threading.RLock()
        self._secure_root()

    def new(self, title: str = "New chat") -> ChatSession:
        """Create and atomically persist an empty chat session."""

        with self._lock:
            now = _aware_utc(self._clock())
            for _ in range(32):
                session = ChatSession(
                    id=f"chat-{uuid4().hex[:16]}",
                    title=title,
                    created_at=now,
                    updated_at=now,
                )
                try:
                    self._atomic_create(self._session_path(session.id), session)
                except FileExistsError:
                    continue
                return session
        raise RuntimeError("could not allocate a unique chat session id")

    def list(self) -> list[ChatSessionSummary]:
        """List sessions newest-first without exposing their transcript contents."""

        with self._lock:
            sessions: list[ChatSessionSummary] = []
            for path in self.root.glob("chat-*.json"):
                session_id = path.stem
                if _CHAT_ID_RE.fullmatch(session_id) is None:
                    continue
                session = self._load_path(path, expected_id=session_id)
                sessions.append(
                    ChatSessionSummary(
                        id=session.id,
                        title=session.title,
                        turn_count=len(session.turns),
                        context_turn_count=len(session.turns) - session.context_start,
                        created_at=session.created_at,
                        updated_at=session.updated_at,
                    )
                )
            return sorted(sessions, key=lambda item: (item.updated_at, item.id), reverse=True)

    def load(self, session_id: str) -> ChatSession:
        with self._lock, self._exclusive_session(session_id):
            return self._load_path(self._session_path(session_id), expected_id=session_id)

    def append(
        self,
        session_id: str,
        value: str | UserTurn | DecisionReport,
    ) -> ChatSession:
        """Append a user turn or a completed decision report.

        Arbitrary dictionaries and orchestration messages are intentionally rejected
        so real-time HSA drafts cannot leak into durable chat context.
        """

        if isinstance(value, str):
            return self.append_user(session_id, value)
        if isinstance(value, UserTurn):
            return self._append_turn(session_id, value)
        if isinstance(value, DecisionReport):
            return self.append_decision(session_id, value)
        raise TypeError("chat append accepts only str, UserTurn, or DecisionReport")

    def append_user(self, session_id: str, content: str) -> ChatSession:
        turn = UserTurn(content=content, created_at=_aware_utc(self._clock()))
        return self._append_turn(session_id, turn)

    def append_decision(self, session_id: str, report: DecisionReport) -> ChatSession:
        """Append a reference and public projection of a finalized report."""

        # Revalidate callers' model copies before deriving hashes or public context.
        report = DecisionReport.model_validate(report.model_dump(mode="python"))
        summary = public_decision_summary(report)
        turn = DecisionReference(
            run_id=report.run_id,
            decision_id=report.decision_id,
            report_hash=content_hash(report),
            trace_root_hash=report.trace_root_hash,
            decision_binding_hash=report.decision_binding_hash,
            public_summary=summary,
            created_at=_aware_utc(self._clock()),
        )
        return self._append_turn(session_id, turn)

    def clear_context(self, session_id: str) -> ChatSession:
        """Start a fresh context window while retaining the editable transcript."""

        with self._lock, self._exclusive_session(session_id):
            session = self._load_path(self._session_path(session_id), expected_id=session_id)
            updated = self._updated_session(session, context_start=len(session.turns))
            self._atomic_replace(self._session_path(session_id), updated)
            return updated

    def build_context(self, session_id: str) -> list[ChatContextItem]:
        """Build next-round context from users and confirmed public summaries only."""

        session = self.load(session_id)
        context: list[ChatContextItem] = []
        for turn in session.turns[session.context_start :]:
            if isinstance(turn, UserTurn):
                context.append(
                    ChatContextItem(
                        role="user",
                        kind="user_message",
                        content=turn.content,
                    )
                )
            elif turn.confirmed:
                context.append(
                    ChatContextItem(
                        role="assistant",
                        kind="confirmed_decision",
                        content=canonical_json(turn.public_summary),
                        run_id=turn.run_id,
                        decision_id=turn.decision_id,
                    )
                )
        return context

    # Readable aliases for callers that prefer explicit resource names.
    new_session = new
    list_sessions = list
    context = build_context

    def _append_turn(self, session_id: str, turn: ChatTurn) -> ChatSession:
        with self._lock, self._exclusive_session(session_id):
            session = self._load_path(self._session_path(session_id), expected_id=session_id)
            updated = self._updated_session(session, turns=[*session.turns, turn])
            self._atomic_replace(self._session_path(session_id), updated)
            return updated

    @contextmanager
    def _exclusive_session(self, session_id: str) -> Iterator[None]:
        """Serialize one session across store instances and local processes."""

        self._session_path(session_id)  # Validate before deriving a lock filename.
        lock_path = self.root.resolve() / f".{session_id}.lock"
        process_lock = _process_lock(lock_path)
        with process_lock:
            flags = os.O_RDWR | os.O_CREAT
            flags |= getattr(os, "O_CLOEXEC", 0)
            flags |= getattr(os, "O_NOFOLLOW", 0)
            descriptor = os.open(lock_path, flags, 0o600)
            locked = False
            try:
                metadata = os.fstat(descriptor)
                if not stat.S_ISREG(metadata.st_mode):
                    raise ValueError("chat session lock must be a regular file")
                os.fchmod(descriptor, 0o600)
                fcntl.flock(descriptor, fcntl.LOCK_EX)
                locked = True
                yield
            finally:
                if locked:
                    fcntl.flock(descriptor, fcntl.LOCK_UN)
                os.close(descriptor)

    def _updated_session(self, session: ChatSession, **changes: Any) -> ChatSession:
        now = _aware_utc(self._clock())
        if now < session.updated_at:
            now = session.updated_at
        payload = session.model_dump(mode="python")
        payload.update(changes)
        payload["updated_at"] = now
        return ChatSession.model_validate(payload)

    def _secure_root(self) -> None:
        if self.root.is_symlink():
            raise ValueError("chat store root cannot be a symlink")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if not self.root.is_dir():
            raise ValueError("chat store root must be a directory")
        os.chmod(self.root, 0o700)

    def _session_path(self, session_id: str) -> Path:
        if _CHAT_ID_RE.fullmatch(session_id) is None:
            raise ValueError("invalid chat session id")
        return self.root / f"{session_id}.json"

    def _load_path(self, path: Path, *, expected_id: str) -> ChatSession:
        if path.is_symlink():
            raise ValueError("chat session file cannot be a symlink")
        if not path.exists():
            raise FileNotFoundError(expected_id)
        if not path.is_file():
            raise ValueError("chat session path must be a regular file")
        os.chmod(path, 0o600)
        payload = json.loads(path.read_text(encoding="utf-8"))
        session = ChatSession.model_validate(payload)
        if session.id != expected_id:
            raise ValueError("chat session id does not match its filename")
        return session

    def _atomic_create(self, path: Path, session: ChatSession) -> None:
        temp = self._write_temp(path, session)
        try:
            # Hard-link publication has create-if-absent semantics and is atomic.
            os.link(temp, path)
            temp.unlink()
            self._fsync_directory()
        finally:
            if temp.exists():
                temp.unlink()

    def _atomic_replace(self, path: Path, session: ChatSession) -> None:
        if path.is_symlink():
            raise ValueError("chat session file cannot be a symlink")
        temp = self._write_temp(path, session)
        try:
            os.replace(temp, path)
            self._fsync_directory()
        finally:
            if temp.exists():
                temp.unlink()

    def _write_temp(self, target: Path, session: ChatSession) -> Path:
        fd, raw_path = tempfile.mkstemp(
            dir=self.root,
            prefix=f".{target.name}.",
            suffix=".tmp",
        )
        temp = Path(raw_path)
        try:
            os.fchmod(fd, 0o600)
            handle = os.fdopen(fd, "w", encoding="utf-8")
            fd = -1  # Ownership moved to ``handle``; it closes the descriptor.
            with handle:
                json.dump(
                    session.model_dump(mode="json"),
                    handle,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                )
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            if fd >= 0:
                os.close(fd)
            if temp.exists():
                temp.unlink()
            raise
        return temp

    def _fsync_directory(self) -> None:
        directory_fd = os.open(self.root, os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


ChatStore = LocalChatStore

__all__ = [
    "ChatContextItem",
    "ChatSession",
    "ChatSessionSummary",
    "ChatStore",
    "DecisionReference",
    "LocalChatStore",
    "PublicDecisionSummary",
    "UserTurn",
    "public_decision_summary",
]
