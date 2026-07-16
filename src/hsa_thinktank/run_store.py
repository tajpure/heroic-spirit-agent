"""Atomic, validated local persistence for privileged decision audit bundles."""

from __future__ import annotations

import json
import os
import re
import shutil
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from .audit import AuditTrail
from .memory import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovalStore,
    InstitutionalMemoryStore,
    MemoryCandidate,
    MemoryScope,
    MemoryStatus,
)
from .models import DecisionReport, content_hash


RUN_ID_PATTERN = re.compile(r"^run-[0-9a-f]{16}$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")


class _StrictBundleModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class _HashedBundleModel(_StrictBundleModel):
    content_hash: str = ""

    @model_validator(mode="after")
    def validate_content_hash(self):
        expected = content_hash(self.model_dump(mode="json", exclude={"content_hash"}))
        if self.content_hash not in ("", expected):
            raise ValueError("content_hash does not match bundle content")
        object.__setattr__(self, "content_hash", expected)
        return self


class MemoryOutboxOperation(_StrictBundleModel):
    schema_version: Literal["1.0"] = "1.0"
    operation_id: str = Field(min_length=1)
    run_id: str
    memory_store_id: str = Field(min_length=1)
    action: Literal["commit_final", "stage"]
    candidate: MemoryCandidate
    decision_event_id: str = Field(min_length=1)
    decision_binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ApprovalOutboxOperation(_StrictBundleModel):
    schema_version: Literal["1.0"] = "1.0"
    operation_id: str = Field(min_length=1)
    run_id: str
    approval_store_id: str = Field(min_length=1)
    request: ApprovalRequest


class RunOutbox(_HashedBundleModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    decision_binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    memory_operation: MemoryOutboxOperation | None = None
    approval_operation: ApprovalOutboxOperation | None = None


class MemoryOperationReceipt(_StrictBundleModel):
    operation_id: str = Field(min_length=1)
    store_id: str = Field(min_length=1)
    action: Literal["commit_final", "stage"]
    memory_id: str = Field(min_length=1)
    status: MemoryStatus
    content_hash: str = Field(pattern=r"^[0-9a-f]{64}$")


class ApprovalOperationReceipt(_StrictBundleModel):
    operation_id: str = Field(min_length=1)
    store_id: str = Field(min_length=1)
    approval_id: str = Field(min_length=1)
    status: ApprovalStatus


class CompletionManifest(_HashedBundleModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    status: Literal["complete"] = "complete"
    report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    trace_root_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    outbox_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    memory_receipt: MemoryOperationReceipt | None = None
    approval_receipt: ApprovalOperationReceipt | None = None
    completed_at: datetime

    @field_validator("completed_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("completion timestamp must include a timezone")
        return value


class FinalizationRecord(_HashedBundleModel):
    schema_version: Literal["1.0"] = "1.0"
    run_id: str
    approval_id: str = Field(min_length=1)
    status: Literal["human_approved"] = "human_approved"
    decision_binding_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    report_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    selected_option_id: str = Field(min_length=1)
    selected_option: str = Field(min_length=1)
    finalized_by: str = Field(min_length=1)
    finalized_at: datetime
    memory_result: MemoryOperationReceipt | None = None

    @field_validator("finalized_at")
    @classmethod
    def require_aware_timestamp(cls, value: datetime) -> datetime:
        if value.tzinfo is None or value.utcoffset() is None:
            raise ValueError("finalization timestamp must include a timezone")
        return value


class LocalRunStore:
    """Persist complete owner-only runs and reconcile their control outbox."""

    def __init__(self, root: str | Path = ".hsa/runs") -> None:
        self.root = Path(root)

    def save(
        self,
        report: DecisionReport,
        *,
        outbox: RunOutbox | None = None,
    ) -> Path:
        self._validate_report(report)
        effective_outbox = outbox or RunOutbox(
            run_id=report.run_id,
            report_hash=content_hash(report),
            decision_binding_hash=report.decision_binding_hash,
        )
        self._validate_outbox(report, effective_outbox)
        root = self._secure_root()
        run_dir = self._run_dir(report.run_id, root=root)
        if os.path.lexists(run_dir):
            raise FileExistsError(f"run bundle already exists: {report.run_id}")

        temporary = Path(tempfile.mkdtemp(prefix=f".{report.run_id}.", dir=root))
        os.chmod(temporary, 0o700)
        try:
            _write_new(
                temporary / "decision.json",
                report.model_dump_json(indent=2, exclude_none=False) + "\n",
            )
            _write_new(
                temporary / "events.jsonl",
                "".join(
                    json.dumps(event.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
                    + "\n"
                    for event in report.audit_events
                ),
            )
            _write_new(
                temporary / "messages.jsonl",
                "".join(
                    json.dumps(message.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)
                    + "\n"
                    for message in report.messages
                ),
            )
            _write_new(
                temporary / "public-summary.json",
                json.dumps(_public_summary(report), ensure_ascii=False, indent=2, sort_keys=True)
                + "\n",
            )
            _write_new(
                temporary / "outbox.json",
                effective_outbox.model_dump_json(indent=2, exclude_none=False) + "\n",
            )
            _fsync_directory(temporary)
            os.replace(temporary, run_dir)
            _fsync_directory(root)
        finally:
            if temporary.exists():
                shutil.rmtree(temporary)
        return run_dir / "decision.json"

    def mark_complete(
        self,
        report: DecisionReport,
        *,
        memory_store: InstitutionalMemoryStore | None = None,
        approval_store: ApprovalStore | None = None,
    ) -> Path:
        stored = self.verify_bundle(report.run_id)
        if content_hash(stored) != content_hash(report):
            raise ValueError("stored decision report does not match completion input")
        outbox = self.load_outbox(report.run_id)
        path = self._run_dir(report.run_id) / "completion.json"
        if os.path.lexists(path):
            _require_regular_file(path)
            current = _load_hashed_json(path, CompletionManifest)
            self._validate_completion(report, outbox, current)
            return path
        memory_receipt = self._memory_receipt(outbox.memory_operation, memory_store)
        approval_receipt = self._approval_receipt(outbox.approval_operation, approval_store)
        manifest = CompletionManifest(
            run_id=report.run_id,
            report_hash=content_hash(report),
            trace_root_hash=report.trace_root_hash,
            outbox_hash=outbox.content_hash,
            memory_receipt=memory_receipt,
            approval_receipt=approval_receipt,
            completed_at=datetime.now(UTC),
        )
        self._validate_completion(report, outbox, manifest)
        rendered = manifest.model_dump_json(indent=2, exclude_none=False) + "\n"
        try:
            _atomic_write_new(path, rendered)
        except FileExistsError:
            _require_regular_file(path)
            current = _load_hashed_json(path, CompletionManifest)
            self._validate_completion(report, outbox, current)
        return path

    def load(self, run_id: str) -> DecisionReport:
        run_dir = self._run_dir(run_id)
        path = run_dir / "decision.json"
        _require_regular_file(path)
        report = DecisionReport.model_validate_json(path.read_text(encoding="utf-8"))
        self._validate_report(report)
        if report.run_id != run_id:
            raise ValueError("decision report run_id does not match directory")
        return report

    def load_outbox(self, run_id: str) -> RunOutbox:
        path = self._run_dir(run_id) / "outbox.json"
        _require_regular_file(path)
        outbox = _load_hashed_json(path, RunOutbox)
        if outbox.run_id != run_id:
            raise ValueError("outbox run_id does not match directory")
        return outbox

    def verify_bundle(
        self,
        run_id: str,
        *,
        require_completion: bool = False,
    ) -> DecisionReport:
        report = self.load(run_id)
        run_dir = self._run_dir(run_id)
        event_lines = _read_jsonl(run_dir / "events.jsonl")
        message_lines = _read_jsonl(run_dir / "messages.jsonl")
        if event_lines != [event.model_dump(mode="json") for event in report.audit_events]:
            raise ValueError("events.jsonl does not match decision report")
        if message_lines != [message.model_dump(mode="json") for message in report.messages]:
            raise ValueError("messages.jsonl does not match decision report")
        public_summary_path = run_dir / "public-summary.json"
        _require_regular_file(public_summary_path)
        if json.loads(public_summary_path.read_text(encoding="utf-8")) != _public_summary(report):
            raise ValueError("public-summary.json does not match decision report")
        outbox = self.load_outbox(run_id)
        self._validate_outbox(report, outbox)

        completion_path = run_dir / "completion.json"
        if require_completion or os.path.lexists(completion_path):
            _require_regular_file(completion_path)
            completion = _load_hashed_json(completion_path, CompletionManifest)
            self._validate_completion(report, outbox, completion)

        finalization_path = run_dir / "finalization.json"
        if os.path.lexists(finalization_path):
            _require_regular_file(finalization_path)
            finalization = _load_hashed_json(finalization_path, FinalizationRecord)
            self._validate_finalization_record(report, finalization)
        return report

    def write_finalization(
        self,
        record: FinalizationRecord,
        *,
        approval: ApprovalRequest,
    ) -> Path:
        validated_record = FinalizationRecord.model_validate(record.model_dump(mode="python"))
        validated_approval = ApprovalRequest.model_validate(approval.model_dump(mode="python"))
        report = self.verify_bundle(validated_record.run_id, require_completion=True)
        self._validate_finalization_record(report, validated_record)
        if validated_approval.status != ApprovalStatus.APPROVED:
            raise ValueError("finalization approval is not approved")
        if validated_approval.id != validated_record.approval_id:
            raise ValueError("finalization approval id mismatch")
        if validated_approval.resolved_by != validated_record.finalized_by:
            raise ValueError("finalization actor does not match approval")
        if validated_approval.resolved_at != validated_record.finalized_at:
            raise ValueError("finalization timestamp does not match approval")
        if validated_approval.payload.get("decision_binding_hash") != (
            report.decision_binding_hash
        ):
            raise ValueError("finalization approval binding mismatch")
        self._validate_finalization_memory(report, validated_record)

        path = self._run_dir(report.run_id) / "finalization.json"
        rendered = validated_record.model_dump_json(indent=2, exclude_none=False) + "\n"
        if os.path.lexists(path):
            _require_regular_file(path)
            if path.read_text(encoding="utf-8") != rendered:
                raise ValueError("run already has a different finalization record")
            return path
        try:
            _atomic_write_new(path, rendered)
        except FileExistsError:
            _require_regular_file(path)
            if path.read_text(encoding="utf-8") != rendered:
                raise ValueError("run already has a different finalization record") from None
        return path

    def _secure_root(self) -> Path:
        if self.root.is_symlink():
            raise ValueError("run store root cannot be a symlink")
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(self.root, 0o700)
        return self.root.resolve()

    def _run_dir(self, run_id: str, *, root: Path | None = None) -> Path:
        if not RUN_ID_PATTERN.fullmatch(run_id):
            raise ValueError(f"invalid run_id: {run_id}")
        secure_root = root or self._secure_root()
        run_dir = secure_root / run_id
        if run_dir.is_symlink():
            raise ValueError("run directory cannot be a symlink")
        if run_dir.exists() and not run_dir.is_dir():
            raise ValueError("run path is not a directory")
        return run_dir

    @staticmethod
    def _validate_report(report: DecisionReport) -> None:
        DecisionReport.model_validate(report.model_dump(mode="python"))
        if not RUN_ID_PATTERN.fullmatch(report.run_id):
            raise ValueError(f"invalid run_id: {report.run_id}")
        if not report.audit_events:
            raise ValueError("decision report audit chain cannot be empty")
        trail = AuditTrail(report.run_id)
        trail.events = [event.model_copy(deep=True) for event in report.audit_events]
        if not trail.verify() or trail.root_hash != report.trace_root_hash:
            raise ValueError("decision report audit chain is invalid")
        for index, event in enumerate(report.audit_events, start=1):
            if event.run_id != report.run_id:
                raise ValueError("audit event belongs to another run")
            if event.ordinal != index:
                raise ValueError("audit event ordinals are not contiguous")
            if event.id != f"{report.run_id}-event-{index:04d}":
                raise ValueError("audit event id is not canonical")
        _validate_lifecycle_events(report)

        known_message_ids: set[str] = set()
        for index, message in enumerate(report.messages, start=1):
            if message.run_id != report.run_id or message.ordinal != index:
                raise ValueError("message run or ordinal is invalid")
            if message.id != f"{report.run_id}-message-{index:04d}":
                raise ValueError("message id is not canonical")
            if message.content_hash != content_hash(message.payload):
                raise ValueError("message content hash mismatch")
            if not set(message.parent_message_ids).issubset(known_message_ids):
                raise ValueError("message references an unknown or future parent")
            known_message_ids.add(message.id)

    @staticmethod
    def _validate_outbox(report: DecisionReport, outbox: RunOutbox) -> None:
        outbox = RunOutbox.model_validate(outbox.model_dump(mode="python"))
        if outbox.run_id != report.run_id:
            raise ValueError("outbox run_id does not match report")
        if outbox.report_hash != content_hash(report):
            raise ValueError("outbox report hash mismatch")
        if outbox.decision_binding_hash != report.decision_binding_hash:
            raise ValueError("outbox decision binding mismatch")

        approval_operation = outbox.approval_operation
        approval_should_exist = (
            report.status == "needs_human" and report.approval_store_id is not None
        )
        if approval_should_exist != (approval_operation is not None):
            raise ValueError("approval outbox does not match the report control policy")
        expected_approval_ids = (
            [approval_operation.request.id] if approval_operation is not None else []
        )
        if report.approval_ids != expected_approval_ids:
            raise ValueError("outbox approval intent does not match report approval ids")
        if approval_operation is not None:
            request = approval_operation.request
            if approval_operation.operation_id != f"approval:{report.run_id}":
                raise ValueError("approval outbox operation id is not canonical")
            if approval_operation.run_id != report.run_id:
                raise ValueError("approval outbox run_id mismatch")
            if approval_operation.approval_store_id != report.approval_store_id:
                raise ValueError("approval outbox store identity mismatch")
            if (
                request.status != ApprovalStatus.PENDING
                or request.subject_id != report.run_id
                or request.organization_id != report.organization_id
                or request.action != "approve_decision"
                or request.payload.get("decision_binding_hash") != report.decision_binding_hash
                or request.payload.get("decision_binding") != report.decision_binding()
            ):
                raise ValueError("approval outbox request does not match decision report")
            planned_approvals = [
                event for event in report.audit_events if event.event_type == "approval_planned"
            ]
            if len(planned_approvals) != 1 or planned_approvals[0].payload != {
                "operation_id": approval_operation.operation_id,
                "approval_id": request.id,
                "approval_store_id": approval_operation.approval_store_id,
                "decision_binding_hash": report.decision_binding_hash,
            }:
                raise ValueError("approval outbox does not match approval_planned audit event")
        elif any(event.event_type == "approval_planned" for event in report.audit_events):
            raise ValueError("approval_planned audit event has no outbox operation")

        operation = outbox.memory_operation
        expected_memory_action = None
        if report.memory_store_id is not None:
            if report.shared_memory_write_mode == "staged":
                expected_memory_action = "stage"
            elif (
                report.shared_memory_write_mode == "final_decision_only"
                and report.status == "decided"
            ):
                expected_memory_action = "commit_final"
        if expected_memory_action != (operation.action if operation is not None else None):
            raise ValueError("memory outbox does not match the report control policy")
        if operation is None:
            if any(event.event_type == "decision_memory_planned" for event in report.audit_events):
                raise ValueError("decision_memory_planned event has no outbox operation")
            ready = next(
                event
                for event in report.audit_events
                if event.event_type == "decision_report_ready"
            )
            if ready.payload.get("memory_outbox") is not False:
                raise ValueError("decision_report_ready memory_outbox flag is invalid")
            if ready.payload.get("approval_outbox") != (approval_operation is not None):
                raise ValueError("decision_report_ready approval_outbox flag is invalid")
            return
        if operation.operation_id != f"memory:{report.run_id}":
            raise ValueError("memory outbox operation id is not canonical")
        if operation.run_id != report.run_id:
            raise ValueError("memory outbox run_id mismatch")
        if operation.memory_store_id != report.memory_store_id:
            raise ValueError("memory outbox store identity mismatch")
        if operation.decision_binding_hash != report.decision_binding_hash:
            raise ValueError("memory outbox decision binding mismatch")
        candidate = operation.candidate
        if (
            candidate.id != f"memory-{report.run_id}"
            or candidate.owner_id != "hsa-orchestrator"
            or candidate.organization_id != report.organization_id
            or candidate.scope != MemoryScope.ORGANIZATION
            or candidate.source_event_ids != [operation.decision_event_id]
        ):
            raise ValueError("memory outbox candidate identity or scope mismatch")
        decision_events = {
            event.id: event
            for event in report.audit_events
            if event.event_type == "decision_aggregated"
        }
        if operation.decision_event_id not in decision_events:
            raise ValueError("memory outbox source is not the decision_aggregated event")
        planned = [
            event for event in report.audit_events if event.event_type == "decision_memory_planned"
        ]
        if len(planned) != 1 or planned[0].payload != {
            "action": operation.action,
            "memory_id": candidate.id,
            "content_hash": candidate.content_hash,
            "source_event_id": operation.decision_event_id,
            "operation_id": operation.operation_id,
            "memory_store_id": operation.memory_store_id,
        }:
            raise ValueError("memory outbox does not match decision_memory_planned audit event")
        try:
            memory_payload = json.loads(candidate.content)
        except json.JSONDecodeError as exc:
            raise ValueError("memory outbox candidate content is not canonical JSON") from exc
        expected_payload = {
            "decision_id": report.decision_id,
            "question": report.frozen_problem.question,
            "status": report.status,
            "selected_option_id": report.selected_option_id,
            "confidence": report.confidence,
            "decision_binding_hash": report.decision_binding_hash,
        }
        if memory_payload != expected_payload:
            raise ValueError("memory outbox candidate content does not match decision report")
        ready = next(
            event for event in report.audit_events if event.event_type == "decision_report_ready"
        )
        if ready.payload.get("memory_outbox") is not True or ready.payload.get(
            "approval_outbox"
        ) != (approval_operation is not None):
            raise ValueError("decision_report_ready outbox flags are invalid")

    @staticmethod
    def _memory_receipt(
        operation: MemoryOutboxOperation | None,
        store: InstitutionalMemoryStore | None,
    ) -> MemoryOperationReceipt | None:
        if operation is None:
            return None
        if store is None or store.store_id != operation.memory_store_id:
            raise ValueError("memory store does not match the persisted outbox")
        record = store.get(operation.candidate.id)
        if not _same_memory_definition(record, operation.candidate):
            raise ValueError("persisted memory does not match outbox candidate")
        if operation.action == "commit_final" and record.status != MemoryStatus.APPROVED:
            raise ValueError("commit_final outbox is not approved in the memory store")
        return MemoryOperationReceipt(
            operation_id=operation.operation_id,
            store_id=store.store_id,
            action=operation.action,
            memory_id=record.id,
            status=record.status,
            content_hash=record.content_hash,
        )

    @staticmethod
    def _approval_receipt(
        operation: ApprovalOutboxOperation | None,
        store: ApprovalStore | None,
    ) -> ApprovalOperationReceipt | None:
        if operation is None:
            return None
        if store is None or store.store_id != operation.approval_store_id:
            raise ValueError("approval store does not match the persisted outbox")
        request = store.get(operation.request.id)
        if not _same_approval_definition(request, operation.request):
            raise ValueError("persisted approval does not match outbox request")
        return ApprovalOperationReceipt(
            operation_id=operation.operation_id,
            store_id=store.store_id,
            approval_id=request.id,
            status=request.status,
        )

    @staticmethod
    def _validate_completion(
        report: DecisionReport,
        outbox: RunOutbox,
        manifest: CompletionManifest,
    ) -> None:
        CompletionManifest.model_validate(manifest.model_dump(mode="python"))
        if (
            manifest.run_id != report.run_id
            or manifest.report_hash != content_hash(report)
            or manifest.trace_root_hash != report.trace_root_hash
            or manifest.outbox_hash != outbox.content_hash
        ):
            raise ValueError("completion manifest metadata mismatch")
        operation = outbox.memory_operation
        receipt = manifest.memory_receipt
        if (operation is None) != (receipt is None):
            raise ValueError("completion memory receipt does not match outbox")
        if operation is not None and receipt is not None:
            if (
                receipt.operation_id != operation.operation_id
                or receipt.store_id != operation.memory_store_id
                or receipt.action != operation.action
                or receipt.memory_id != operation.candidate.id
                or receipt.content_hash != operation.candidate.content_hash
                or (operation.action == "commit_final" and receipt.status != MemoryStatus.APPROVED)
            ):
                raise ValueError("completion memory receipt is invalid")
        approval_operation = outbox.approval_operation
        approval_receipt = manifest.approval_receipt
        if (approval_operation is None) != (approval_receipt is None):
            raise ValueError("completion approval receipt does not match outbox")
        if approval_operation is not None and approval_receipt is not None:
            if (
                approval_receipt.operation_id != approval_operation.operation_id
                or approval_receipt.store_id != approval_operation.approval_store_id
                or approval_receipt.approval_id != approval_operation.request.id
            ):
                raise ValueError("completion approval receipt is invalid")

    @staticmethod
    def _validate_finalization_record(report: DecisionReport, record: FinalizationRecord) -> None:
        FinalizationRecord.model_validate(record.model_dump(mode="python"))
        if (
            report.status != "needs_human"
            or record.run_id != report.run_id
            or record.approval_id not in report.approval_ids
            or record.decision_binding_hash != report.decision_binding_hash
            or record.report_hash != content_hash(report)
            or record.selected_option_id != report.selected_option_id
            or record.selected_option != report.selected_option
        ):
            raise ValueError("finalization record does not match decision report")
        if record.memory_result is not None:
            if (
                record.memory_result.memory_id != f"memory-{report.run_id}"
                or record.memory_result.store_id != report.memory_store_id
            ):
                raise ValueError("finalization memory receipt does not match decision report")
        LocalRunStore._validate_finalization_memory(report, record)

    @staticmethod
    def _validate_finalization_memory(
        report: DecisionReport,
        record: FinalizationRecord,
    ) -> None:
        expected_action = {
            "disabled": None,
            "staged": "stage",
            "final_decision_only": "commit_final",
        }.get(report.shared_memory_write_mode)
        if expected_action is None:
            if record.memory_result is not None:
                raise ValueError("disabled memory policy cannot have a finalization receipt")
            return
        if record.memory_result is None or record.memory_result.action != expected_action:
            raise ValueError("finalization memory receipt does not match organization policy")
        if (
            expected_action == "commit_final"
            and record.memory_result.status != MemoryStatus.APPROVED
        ):
            raise ValueError("final decision memory is not approved")


def _validate_lifecycle_events(report: DecisionReport) -> None:
    positions: dict[str, int] = {}
    required = (
        "run_started",
        "meeting_selected",
        "memory_frozen",
        "options_frozen",
        "decision_aggregated",
        "decision_report_ready",
    )
    for event_type in required:
        matches = [
            index
            for index, event in enumerate(report.audit_events)
            if event.event_type == event_type
        ]
        if len(matches) != 1:
            raise ValueError(f"audit lifecycle requires exactly one {event_type} event")
        positions[event_type] = matches[0]
    if [positions[item] for item in required] != sorted(positions.values()):
        raise ValueError("audit lifecycle events are out of order")
    if positions["decision_report_ready"] != len(report.audit_events) - 1:
        raise ValueError("decision_report_ready must be the final audit event")

    started = report.audit_events[positions["run_started"]].payload
    if (
        started.get("decision_id") != report.decision_id
        or started.get("organization_id") != report.organization_id
        or started.get("organization_version") != report.organization_version
        or started.get("request_snapshot_hash") != report.request_snapshot_hash
        or started.get("request_snapshot") != report.request_snapshot.model_dump(mode="json")
    ):
        raise ValueError("run_started audit payload does not match decision report")
    meeting_selected = report.audit_events[positions["meeting_selected"]].payload
    if meeting_selected != report.meeting_selection.model_dump(mode="json"):
        raise ValueError("meeting_selected audit payload does not match decision report")
    aggregated = report.audit_events[positions["decision_aggregated"]].payload
    if aggregated != {
        "status": report.status,
        "selected_option_id": report.selected_option_id,
        "option_scores": report.option_scores,
        "confidence": report.confidence,
        "status_reason": report.status_reason,
    }:
        raise ValueError("decision_aggregated audit payload does not match decision report")
    ready = report.audit_events[positions["decision_report_ready"]].payload
    if ready.get("status") != report.status:
        raise ValueError("decision_report_ready audit payload does not match report")


def _same_memory_definition(record, candidate: MemoryCandidate) -> bool:
    return record.model_dump(mode="json", exclude={"status"}) == candidate.model_dump(
        mode="json", exclude={"status"}
    )


def _same_approval_definition(stored: ApprovalRequest, planned: ApprovalRequest) -> bool:
    fields = (
        "id",
        "idempotency_key",
        "level",
        "action",
        "subject_id",
        "organization_id",
        "requested_by",
        "payload",
        "created_at",
    )
    return all(getattr(stored, field) == getattr(planned, field) for field in fields)


def _public_summary(report: DecisionReport) -> dict[str, Any]:
    return {
        "schema_version": report.schema_version,
        "run_id": report.run_id,
        "decision_id": report.decision_id,
        "question": report.frozen_problem.question,
        "risk_tier": report.frozen_problem.risk_tier,
        "options": [option.model_dump(mode="json") for option in report.frozen_problem.options],
        "organization_id": report.organization_id,
        "protocol_name": report.protocol_name,
        "meeting_selection": {
            "mode": report.meeting_selection.mode,
            "organization_id": report.meeting_selection.organization_id,
            "protocol": report.meeting_selection.protocol,
            "selected_hsa_ids": report.meeting_selection.selected_hsa_ids,
            "matched_signals": report.meeting_selection.matched_signals,
            "reasons": report.meeting_selection.reasons,
        },
        "status": report.status,
        "status_reason": report.status_reason,
        "selected_option_id": report.selected_option_id,
        "selected_option": report.selected_option,
        "option_scores": report.option_scores,
        "confidence": report.confidence,
        "rationale_claims": [
            {
                "claim": claim.claim,
                "basis": claim.basis,
                "principle_ids": claim.principle_ids,
                "evidence_ids": claim.evidence_ids,
            }
            for claim in report.rationale_claims
        ],
        "assumptions": report.assumptions,
        "unresolved_risks": report.unresolved_risks,
        "dissent": report.dissent,
        "next_actions": report.next_actions,
        "approval_ids": report.approval_ids,
        "decision_binding_hash": report.decision_binding_hash,
        "trace_root_hash": report.trace_root_hash,
    }


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    _require_regular_file(path)
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _load_hashed_json(path: Path, model_type):
    payload = json.loads(path.read_text(encoding="utf-8"))
    supplied_hash = payload.get("content_hash") if isinstance(payload, dict) else None
    if not isinstance(supplied_hash, str) or not HASH_PATTERN.fullmatch(supplied_hash):
        raise ValueError(f"{path.name} is missing a valid content_hash")
    return model_type.model_validate(payload)


def _require_regular_file(path: Path) -> None:
    if path.is_symlink() or not path.is_file():
        raise ValueError(f"expected regular bundle file: {path.name}")


def _write_new(path: Path, content: str) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())


def _atomic_write_new(path: Path, content: str) -> None:
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent, text=True)
    installed = False
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.link(temporary, path)
        installed = True
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    if installed:
        _fsync_directory(path.parent)


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = [
    "ApprovalOutboxOperation",
    "CompletionManifest",
    "FinalizationRecord",
    "LocalRunStore",
    "MemoryOperationReceipt",
    "MemoryOutboxOperation",
    "RUN_ID_PATTERN",
    "RunOutbox",
]
