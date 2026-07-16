from __future__ import annotations

import stat
from datetime import datetime, timedelta, timezone

import pytest

from hsa_thinktank.memory import (
    ApprovalConflictError,
    ApprovalLevel,
    ApprovalPolicyError,
    ApprovalRequest,
    ApprovalStatus,
    ApprovalStore,
    InstitutionalMemoryStore,
    MemoryCandidate,
    MemoryNotFoundError,
    MemoryPolicyError,
    MemoryScope,
    MemoryStatus,
    SharedWriteMode,
)


NOW = datetime(2026, 7, 16, 9, 0, tzinfo=timezone.utc)


def test_sqlite_memory_database_is_owner_only(tmp_path) -> None:
    database = tmp_path / "memory.sqlite"
    with InstitutionalMemoryStore(database):
        pass

    assert stat.S_IMODE(database.stat().st_mode) == 0o600


def candidate(
    memory_id: str,
    *,
    owner_id: str = "hsa-alpha",
    scope: MemoryScope = MemoryScope.PRIVATE,
    organization_id: str | None = None,
    content: str | None = None,
    supersedes: str | None = None,
    created_at: datetime = NOW,
) -> MemoryCandidate:
    return MemoryCandidate(
        id=memory_id,
        owner_id=owner_id,
        organization_id=organization_id,
        scope=scope,
        content=content or f"content for {memory_id}",
        source_event_ids=[f"event-{memory_id}"],
        confidence=0.85,
        created_at=created_at,
        supersedes=supersedes,
    )


def test_private_memory_is_staged_then_isolated_between_hsas(tmp_path) -> None:
    with InstitutionalMemoryStore(tmp_path / "memory.sqlite") as store:
        staged = store.stage_tool_output(candidate("private-a"), tool_id="hermes-tool")

        assert staged.status == MemoryStatus.STAGED
        assert store.search(requester_id="hsa-alpha", now=NOW) == []

        approved = store.approve("private-a", approver_id="human-reviewer")
        assert approved.status == MemoryStatus.APPROVED
        assert [record.id for record in store.search(requester_id="hsa-alpha", now=NOW)] == [
            "private-a"
        ]
        assert store.search(requester_id="hsa-beta", organization_id="org-one", now=NOW) == []


def test_organization_and_user_scopes_require_matching_authenticated_context(
    tmp_path,
) -> None:
    with InstitutionalMemoryStore(tmp_path / "memory.sqlite") as store:
        organization_memory = candidate(
            "org-memory",
            scope=MemoryScope.ORGANIZATION,
            organization_id="org-one",
        )
        user_memory = candidate(
            "user-memory",
            owner_id="user-42",
            scope=MemoryScope.USER,
        )
        store.stage_candidate(organization_memory)
        store.stage_candidate(user_memory)
        store.approve("org-memory")
        store.approve("user-memory")

        same_org = store.search(requester_id="hsa-beta", organization_id="org-one", now=NOW)
        assert [record.id for record in same_org] == ["org-memory"]

        assert store.search(requester_id="hsa-beta", organization_id="org-two", now=NOW) == []
        assert store.search(requester_id="hsa-beta", now=NOW) == []

        same_user = store.search(requester_id="hsa-beta", user_id="user-42", now=NOW)
        assert [record.id for record in same_user] == ["user-memory"]
        assert store.search(requester_id="hsa-beta", user_id="user-99", now=NOW) == []


def test_private_visibility_can_be_disabled_without_hiding_shared_memory(
    tmp_path,
) -> None:
    with InstitutionalMemoryStore(tmp_path / "memory.sqlite") as store:
        store.stage_candidate(candidate("private-secret", content="private"))
        store.stage_candidate(
            candidate(
                "shared-fact",
                scope=MemoryScope.ORGANIZATION,
                organization_id="org-one",
                content="shared",
            )
        )
        store.approve("private-secret")
        store.approve("shared-fact")

        visible = store.search(
            requester_id="hsa-alpha",
            organization_id="org-one",
            include_private=False,
            now=NOW,
        )

        assert [record.id for record in visible] == ["shared-fact"]


def test_rejected_and_unapproved_candidates_are_never_searchable(tmp_path) -> None:
    with InstitutionalMemoryStore(tmp_path / "memory.sqlite") as store:
        store.stage_candidate(candidate("pending-memory"))
        store.stage_candidate(candidate("rejected-memory"))
        rejected = store.reject(
            "rejected-memory", approver_id="reviewer", reason="unsupported claim"
        )

        assert rejected.status == MemoryStatus.REJECTED
        assert store.search(requester_id="hsa-alpha", now=NOW) == []
        assert store.get("pending-memory").status == MemoryStatus.STAGED


def test_approving_replacement_supersedes_without_deleting_history(tmp_path) -> None:
    with InstitutionalMemoryStore(tmp_path / "memory.sqlite") as store:
        store.stage_candidate(candidate("old-policy", content="Use policy A"))
        store.approve("old-policy")
        store.stage_candidate(
            candidate(
                "new-policy",
                content="Use policy B",
                supersedes="old-policy",
            )
        )
        store.approve("new-policy", approver_id="memory-board")

        assert store.get("old-policy").status == MemoryStatus.SUPERSEDED
        assert store.get("new-policy").status == MemoryStatus.APPROVED
        assert [record.id for record in store.search(requester_id="hsa-alpha", now=NOW)] == [
            "new-policy"
        ]
        assert [event.event for event in store.audit_events("old-policy")] == [
            "staged",
            "approved",
            "superseded",
        ]


def test_failed_supersede_rolls_back_the_whole_approval_transaction(tmp_path) -> None:
    with InstitutionalMemoryStore(tmp_path / "memory.sqlite") as store:
        store.stage_candidate(candidate("replacement", supersedes="missing-memory"))

        with pytest.raises(MemoryNotFoundError):
            store.approve("replacement")

        assert store.get("replacement").status == MemoryStatus.STAGED
        assert [event.event for event in store.audit_events("replacement")] == ["staged"]


def test_snapshot_order_and_hash_are_stable_across_repeated_reads(tmp_path) -> None:
    with InstitutionalMemoryStore(tmp_path / "memory.sqlite") as store:
        for memory_id in ("z-record", "a-record"):
            store.stage_candidate(
                candidate(
                    memory_id,
                    owner_id="user-42",
                    scope=MemoryScope.USER,
                    created_at=NOW,
                )
            )
            store.approve(memory_id)

        first = store.snapshot(requester_id="hsa-alpha", user_id="user-42", now=NOW)
        second = store.snapshot(requester_id="hsa-alpha", user_id="user-42", now=NOW)

        assert [record.id for record in first.records] == ["a-record", "z-record"]
        assert first.snapshot_hash == second.snapshot_hash
        assert len(first.snapshot_hash) == 64


def test_final_decision_is_the_only_automatic_shared_write_path(tmp_path) -> None:
    shared = candidate(
        "decision-memory",
        scope=MemoryScope.ORGANIZATION,
        organization_id="org-one",
    )
    with InstitutionalMemoryStore(
        tmp_path / "memory.sqlite",
        shared_write_mode=SharedWriteMode.FINAL_DECISION_ONLY,
    ) as store:
        with pytest.raises(MemoryPolicyError, match="final_decision_only"):
            store.commit_decision_memory(
                shared,
                decision_event_id="decision-draft",
                committed_by="orchestrator",
                decision_is_final=False,
            )
        with pytest.raises(MemoryNotFoundError):
            store.get("decision-memory")

        committed = store.commit_decision_memory(
            shared,
            decision_event_id="decision-final",
            committed_by="orchestrator",
            decision_is_final=True,
        )

        assert committed.status == MemoryStatus.APPROVED
        assert [
            record.id
            for record in store.search(requester_id="hsa-beta", organization_id="org-one", now=NOW)
        ] == ["decision-memory"]
        assert [event.event for event in store.audit_events("decision-memory")] == [
            "staged",
            "approved",
            "decision_committed",
        ]


def test_expired_approved_memory_is_not_visible(tmp_path) -> None:
    expiring = candidate("expires")
    expires_at = NOW + timedelta(days=365)
    expiring = expiring.model_copy(update={"expires_at": expires_at})
    with InstitutionalMemoryStore(tmp_path / "memory.sqlite") as store:
        store.stage_candidate(expiring)
        store.approve("expires")

        assert [
            record.id
            for record in store.search(
                requester_id="hsa-alpha", now=expires_at - timedelta(minutes=1)
            )
        ] == ["expires"]
        assert store.search(requester_id="hsa-alpha", now=expires_at + timedelta(minutes=1)) == []


def test_approval_store_enforces_idempotency_level_and_audit(tmp_path) -> None:
    with ApprovalStore(tmp_path / "approval.sqlite") as store:
        request = ApprovalRequest(
            id="approval-one",
            idempotency_key="promote:memory-one:v1",
            level=ApprovalLevel.L3,
            action="promote_memory",
            subject_id="memory-one",
            organization_id="org-one",
            requested_by="hsa-alpha",
            payload={"content_hash": "abc"},
            created_at=NOW,
        )
        pending = store.pending(request)
        replay = store.pending(request.model_copy(update={"id": "approval-replay"}))

        assert pending.status == ApprovalStatus.PENDING
        assert replay.id == pending.id
        assert len(store.audit_events(pending.id)) == 1

        with pytest.raises(ApprovalConflictError, match="idempotency key"):
            store.pending(
                request.model_copy(update={"id": "approval-conflict", "action": "delete_memory"})
            )

        with pytest.raises(ApprovalPolicyError, match="cannot resolve"):
            store.approve(
                pending.id,
                actor_id="l2-reviewer",
                approver_level=ApprovalLevel.L2,
            )
        assert store.get(pending.id).status == ApprovalStatus.PENDING

        approved = store.approve(
            pending.id,
            actor_id="l3-reviewer",
            approver_level=ApprovalLevel.L3,
            reason="sources verified",
        )
        assert approved.status == ApprovalStatus.APPROVED
        assert approved.resolved_by == "l3-reviewer"
        assert [event.event for event in store.audit_events(pending.id)] == [
            "pending",
            "approved",
        ]


def test_l2_approval_can_be_rejected_and_is_audited(tmp_path) -> None:
    with ApprovalStore(tmp_path / "approval.sqlite") as store:
        request = store.create_pending(
            ApprovalRequest(
                id="approval-two",
                idempotency_key="publish:memory-two:v1",
                level=ApprovalLevel.L2,
                action="publish_memory",
                subject_id="memory-two",
                requested_by="hsa-alpha",
                created_at=NOW,
            )
        )
        rejected = store.reject(
            request.id,
            actor_id="l2-reviewer",
            approver_level=ApprovalLevel.L2,
            reason="insufficient evidence",
        )

        assert rejected.status == ApprovalStatus.REJECTED
        assert rejected.resolution_reason == "insufficient evidence"
        assert [event.event for event in store.audit_events(request.id)] == [
            "pending",
            "rejected",
        ]
