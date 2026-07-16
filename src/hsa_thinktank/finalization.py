"""Human approval and crash recovery for immutable decision bundles."""

from __future__ import annotations

from typing import Any

from .catalog import Catalog
from .memory import (
    ApprovalRequest,
    ApprovalStatus,
    ApprovalStore,
    InstitutionalMemoryStore,
    MemoryCandidate,
    MemoryScope,
)
from .models import canonical_json, content_hash
from .routing import validate_selection_against_catalog
from .run_store import FinalizationRecord, LocalRunStore, MemoryOperationReceipt


def finalize_approved_decision(
    *,
    catalog: Catalog,
    request_id: str,
    approval_store: ApprovalStore,
    memory_store: InstitutionalMemoryStore,
    run_store: LocalRunStore,
) -> dict[str, Any]:
    approval = approval_store.get(request_id)
    _validate_approval(approval)
    report = run_store.verify_bundle(approval.subject_id, require_completion=True)
    if approval_store.store_id != report.approval_store_id:
        raise ValueError("approval store does not match the decision binding")
    if report.shared_memory_write_mode != "disabled":
        if report.memory_store_id is None:
            raise ValueError("decision requires shared memory but no store was bound")
        if memory_store.store_id != report.memory_store_id:
            raise ValueError("memory store does not match the decision binding")
    if approval.id not in report.approval_ids:
        raise ValueError("approval is not referenced by the decision report")
    if report.status != "needs_human":
        raise ValueError("only a needs_human decision can be finalized")
    if report.selected_option_id is None or report.selected_option is None:
        raise ValueError("cannot finalize a decision without a selected option")
    if approval.organization_id != report.organization_id:
        raise ValueError("approval organization does not match decision report")

    binding = report.decision_binding()
    binding_hash = content_hash(binding)
    if binding_hash != report.decision_binding_hash:
        raise ValueError("decision report binding hash is invalid")
    if approval.payload.get("decision_binding_hash") != binding_hash:
        raise ValueError("approval is bound to a different decision digest")
    if approval.payload.get("decision_binding") != binding:
        raise ValueError("approval decision binding payload does not match report")

    organization = validate_selection_against_catalog(catalog, report.meeting_selection)
    if organization.fingerprint != report.organization_fingerprint:
        raise ValueError("catalog organization changed after the decision run")
    current_profile_fingerprints = {
        hsa_id: catalog.profile(hsa_id).fingerprint
        for hsa_id in report.meeting_selection.selected_hsa_ids
    }
    if current_profile_fingerprints != report.profile_fingerprints:
        raise ValueError("catalog HSA profiles changed after the decision run")

    memory_result = _finalize_memory(
        report=report,
        approval=approval,
        write_mode=report.shared_memory_write_mode,
        memory_store=memory_store,
    )
    assert approval.resolved_by is not None and approval.resolved_at is not None
    record = FinalizationRecord(
        run_id=report.run_id,
        approval_id=approval.id,
        decision_binding_hash=binding_hash,
        report_hash=content_hash(report),
        selected_option_id=report.selected_option_id,
        selected_option=report.selected_option,
        finalized_by=approval.resolved_by,
        finalized_at=approval.resolved_at,
        memory_result=memory_result,
    )
    run_store.write_finalization(
        record,
        approval=approval,
    )
    return record.model_dump(mode="json")


def sync_memory_outbox(
    *,
    run_id: str,
    memory_store: InstitutionalMemoryStore,
    run_store: LocalRunStore,
    approval_store: ApprovalStore | None = None,
) -> dict[str, Any] | None:
    """Idempotently reconcile every persisted control-plane outbox operation."""

    report = run_store.verify_bundle(run_id)
    outbox = run_store.load_outbox(run_id)
    approval_plan = outbox.approval_operation
    if approval_plan is not None:
        if approval_store is None or approval_store.store_id != (approval_plan.approval_store_id):
            raise ValueError("approval store does not match the persisted outbox")
        approval_store.pending(approval_plan.request)

    plan = outbox.memory_operation
    result: dict[str, Any] | None = None
    if plan is not None:
        if memory_store.store_id != plan.memory_store_id:
            raise ValueError("memory store does not match the persisted outbox")
        if plan.action == "commit_final":
            record = memory_store.commit_decision_memory(
                plan.candidate,
                decision_event_id=plan.decision_event_id,
                committed_by="hsa-orchestrator",
                decision_is_final=True,
            )
        elif plan.action == "stage":
            record = memory_store.stage_candidate(
                plan.candidate,
                actor_id="hsa-orchestrator",
                origin="decision_outbox",
            )
        else:  # pragma: no cover - Literal plus validation makes this unreachable
            raise ValueError(f"unknown memory outbox action: {plan.action}")
        result = {
            "operation_id": plan.operation_id,
            "store_id": memory_store.store_id,
            "action": plan.action,
            "memory_id": record.id,
            "status": record.status.value,
            "content_hash": record.content_hash,
        }
    run_store.mark_complete(
        report,
        memory_store=memory_store,
        approval_store=approval_store,
    )
    return result


def _validate_approval(approval: ApprovalRequest) -> None:
    if approval.status != ApprovalStatus.APPROVED:
        raise ValueError("approval request must be approved before finalization")
    if approval.action != "approve_decision":
        raise ValueError("approval action is not approve_decision")
    if approval.level.value != "L3":
        raise ValueError("decision finalization requires an L3 approval")
    if approval.resolved_by is None or approval.resolved_at is None:
        raise ValueError("approved request is missing resolution metadata")


def _finalize_memory(
    *,
    report,
    approval: ApprovalRequest,
    write_mode: str,
    memory_store: InstitutionalMemoryStore,
) -> MemoryOperationReceipt | None:
    if write_mode == "disabled":
        return None
    memory_id = f"memory-{report.run_id}"
    if write_mode == "staged":
        record = memory_store.get(memory_id)
        return MemoryOperationReceipt(
            operation_id=f"finalization:{approval.id}",
            store_id=memory_store.store_id,
            action="stage",
            memory_id=record.id,
            status=record.status,
            content_hash=record.content_hash,
        )
    if write_mode != "final_decision_only":
        raise ValueError(f"unsupported shared memory write mode: {write_mode}")

    candidate = MemoryCandidate(
        id=memory_id,
        owner_id="hsa-orchestrator",
        organization_id=report.organization_id,
        scope=MemoryScope.ORGANIZATION,
        content=canonical_json(
            {
                "decision_id": report.decision_id,
                "status": "human_approved",
                "selected_option_id": report.selected_option_id,
                "confidence": report.confidence,
                "decision_binding_hash": report.decision_binding_hash,
                "approval_id": approval.id,
            }
        ),
        source_event_ids=[approval.id],
        confidence=report.confidence,
        created_at=approval.resolved_at,
    )
    record = memory_store.commit_decision_memory(
        candidate,
        decision_event_id=approval.id,
        committed_by=approval.resolved_by or "human-approver",
        decision_is_final=True,
    )
    return MemoryOperationReceipt(
        operation_id=f"finalization:{approval.id}",
        store_id=memory_store.store_id,
        action="commit_final",
        memory_id=record.id,
        status=record.status,
        content_hash=record.content_hash,
    )


__all__ = ["finalize_approved_decision", "sync_memory_outbox"]
