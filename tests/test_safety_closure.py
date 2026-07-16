from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path

import pytest

from hsa_thinktank.audit import AuditTrail
from hsa_thinktank.catalog import Catalog
from hsa_thinktank.demo import demo_responder
from hsa_thinktank.finalization import finalize_approved_decision, sync_memory_outbox
from hsa_thinktank.memory import (
    ApprovalLevel,
    ApprovalStatus,
    ApprovalStore,
    InstitutionalMemoryStore,
    MemoryNotFoundError,
    MemoryStatus,
)
from hsa_thinktank.models import DecisionOption, DecisionProblem, content_hash
from hsa_thinktank.orchestrator import ThinkTank
from hsa_thinktank.run_store import FinalizationRecord, LocalRunStore, RunOutbox
from hsa_thinktank.runtime import DeterministicRuntime, RawAgentResponse


def problem(
    *,
    risk_tier: str = "medium",
    grants: list[str] | None = None,
) -> DecisionProblem:
    return DecisionProblem(
        id="decision-safety-closure",
        question="Should we launch?",
        options=[
            DecisionOption(id="launch", description="Launch with checkpoints"),
            DecisionOption(id="wait", description="Wait for more evidence"),
        ],
        risk_tier=risk_tier,
        user_tool_grants=grants or [],
    )


def make_report():
    return asyncio.run(
        ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=DeterministicRuntime(demo_responder),
        ).decide(
            problem(),
            organization_id="product-roundtable",
            persist=False,
        )
    )


@pytest.mark.parametrize(
    "run_id",
    ["../run-0000000000000000", "run-0000000000000000/..", "/tmp/run-0000000000000000"],
)
def test_run_store_rejects_path_traversal(run_id: str, tmp_path: Path) -> None:
    store = LocalRunStore(tmp_path / "runs")

    with pytest.raises(ValueError, match="invalid run_id"):
        store.load(run_id)


def test_run_store_rejects_audit_chain_from_another_run(tmp_path: Path) -> None:
    report = make_report()
    other = AuditTrail("run-ffffffffffffffff")
    other.append("foreign_event", {"source": "another run"})
    forged = report.model_copy(
        update={
            "audit_events": other.events,
            "trace_root_hash": other.root_hash,
        }
    )

    with pytest.raises(ValueError, match="audit chain is invalid"):
        LocalRunStore(tmp_path / "runs").save(forged)


def test_run_store_does_not_accept_an_incomplete_bundle(tmp_path: Path) -> None:
    report = make_report()
    store = LocalRunStore(tmp_path / "runs")
    store.save(report)

    with pytest.raises(ValueError, match="completion.json"):
        store.verify_bundle(report.run_id, require_completion=True)


def test_run_store_rejects_a_tampered_frozen_problem(tmp_path: Path) -> None:
    report = make_report()
    forged = report.model_copy(
        update={
            "frozen_problem": report.frozen_problem.model_copy(
                update={"question": "A different question"}
            )
        }
    )

    with pytest.raises(ValueError, match="frozen_problem_hash"):
        LocalRunStore(tmp_path / "runs").save(forged)


def test_run_store_rejects_an_empty_audit_lifecycle(tmp_path: Path) -> None:
    report = make_report()
    forged = report.model_copy(
        update={
            "audit_events": [],
            "trace_root_hash": content_hash([]),
        }
    )

    with pytest.raises(ValueError, match="audit_events"):
        LocalRunStore(tmp_path / "runs").save(forged)


def test_run_store_detects_public_summary_tampering(tmp_path: Path) -> None:
    report = make_report()
    store = LocalRunStore(tmp_path / "runs")
    store.save(report)
    summary_path = tmp_path / "runs" / report.run_id / "public-summary.json"
    summary = json.loads(summary_path.read_text(encoding="utf-8"))
    summary["selected_option_id"] = "wait"
    summary_path.write_text(json.dumps(summary), encoding="utf-8")

    with pytest.raises(ValueError, match="public-summary.json"):
        store.verify_bundle(report.run_id)


def test_run_store_detects_rehashed_outbox_operation_tampering(tmp_path: Path) -> None:
    runs = LocalRunStore(tmp_path / "runs")
    with InstitutionalMemoryStore(tmp_path / "memory.sqlite") as memory:
        report = asyncio.run(
            ThinkTank(
                catalog=Catalog.builtin(),
                runtimes=DeterministicRuntime(demo_responder),
                memory_store=memory,
                run_store=runs,
            ).decide(problem(), organization_id="product-roundtable")
        )

    outbox_path = tmp_path / "runs" / report.run_id / "outbox.json"
    payload = json.loads(outbox_path.read_text(encoding="utf-8"))
    payload["memory_operation"]["candidate"]["content"] = "injected"
    payload["memory_operation"]["candidate"]["content_hash"] = ""
    payload["content_hash"] = ""
    forged = RunOutbox.model_validate(payload)
    outbox_path.write_text(forged.model_dump_json(indent=2), encoding="utf-8")

    with pytest.raises(ValueError, match="memory outbox"):
        runs.verify_bundle(report.run_id)


def test_final_only_high_risk_approval_finalizes_decision_memory(tmp_path: Path) -> None:
    runs = LocalRunStore(tmp_path / "runs")
    with (
        InstitutionalMemoryStore(tmp_path / "memory.sqlite") as memory,
        ApprovalStore(tmp_path / "approvals.sqlite") as approvals,
    ):
        report = asyncio.run(
            ThinkTank(
                catalog=Catalog.builtin(),
                runtimes=DeterministicRuntime(demo_responder),
                memory_store=memory,
                approval_store=approvals,
                run_store=runs,
            ).decide(
                problem(risk_tier="high"),
                organization_id="launch-red-team",
            )
        )
        assert report.status == "needs_human"
        assert len(report.approval_ids) == 1
        with pytest.raises(MemoryNotFoundError):
            memory.get(f"memory-{report.run_id}")

        approvals.approve(
            report.approval_ids[0],
            actor_id="human-l3-reviewer",
            approver_level=ApprovalLevel.L3,
            reason="decision binding reviewed",
        )
        approved = approvals.get(report.approval_ids[0])
        forged_finalization = FinalizationRecord(
            run_id=report.run_id,
            approval_id=approved.id,
            decision_binding_hash=report.decision_binding_hash,
            report_hash=content_hash(report),
            selected_option_id="wait",
            selected_option="Wait for more evidence",
            finalized_by=approved.resolved_by or "human-l3-reviewer",
            finalized_at=approved.resolved_at,
            memory_result=None,
        )
        with pytest.raises(ValueError, match="does not match decision report"):
            runs.write_finalization(forged_finalization, approval=approved)
        original_catalog = Catalog.builtin()
        changed_profile = original_catalog.profile("steve-jobs").model_copy(
            update={"version": "1.0.1"}
        )
        changed_catalog = Catalog(
            [
                changed_profile if profile.id == changed_profile.id else profile
                for profile in original_catalog.profiles.values()
            ],
            original_catalog.organizations.values(),
        )
        with pytest.raises(ValueError, match="HSA profiles changed"):
            finalize_approved_decision(
                catalog=changed_catalog,
                request_id=report.approval_ids[0],
                approval_store=approvals,
                memory_store=memory,
                run_store=runs,
            )
        first = finalize_approved_decision(
            catalog=Catalog.builtin(),
            request_id=report.approval_ids[0],
            approval_store=approvals,
            memory_store=memory,
            run_store=runs,
        )
        second = finalize_approved_decision(
            catalog=Catalog.builtin(),
            request_id=report.approval_ids[0],
            approval_store=approvals,
            memory_store=memory,
            run_store=runs,
        )

        assert first == second
        assert first["status"] == "human_approved"
        assert first["decision_binding_hash"] == report.decision_binding_hash
        record = memory.get(f"memory-{report.run_id}")
        assert record.status == MemoryStatus.APPROVED
        assert [event.event for event in memory.audit_events(record.id)] == [
            "staged",
            "approved",
            "decision_committed",
        ]
        assert (tmp_path / "runs" / report.run_id / "finalization.json").is_file()


def test_auto_routed_subset_can_be_approved_and_finalized(tmp_path: Path) -> None:
    runs = LocalRunStore(tmp_path / "runs")
    routed_problem = DecisionProblem(
        id="decision-auto-finalize",
        question="如何改善产品的新用户体验？",
        options=[
            DecisionOption(id="onboarding", description="改善新手引导"),
            DecisionOption(id="navigation", description="简化核心导航"),
        ],
        user_tool_grants=["memory"],
    )
    with (
        InstitutionalMemoryStore(tmp_path / "memory.sqlite") as memory,
        ApprovalStore(tmp_path / "approvals.sqlite") as approvals,
    ):
        report = asyncio.run(
            ThinkTank(
                catalog=Catalog.builtin(),
                runtimes=DeterministicRuntime(demo_responder),
                memory_store=memory,
                approval_store=approvals,
                run_store=runs,
            ).decide(routed_problem)
        )

        assert report.status == "needs_human"
        assert report.meeting_selection.selected_hsa_ids == ["steve-jobs", "charlie-munger"]
        approvals.approve(
            report.approval_ids[0],
            actor_id="human-l3-reviewer",
            approver_level=ApprovalLevel.L3,
            reason="auto-routed decision reviewed",
        )
        finalized = finalize_approved_decision(
            catalog=Catalog.builtin(),
            request_id=report.approval_ids[0],
            approval_store=approvals,
            memory_store=memory,
            run_store=runs,
        )

    assert finalized["status"] == "human_approved"
    assert (tmp_path / "runs" / report.run_id / "finalization.json").is_file()


def test_pending_approval_cannot_finalize(tmp_path: Path) -> None:
    runs = LocalRunStore(tmp_path / "runs")
    with (
        InstitutionalMemoryStore(tmp_path / "memory.sqlite") as memory,
        ApprovalStore(tmp_path / "approvals.sqlite") as approvals,
    ):
        report = asyncio.run(
            ThinkTank(
                catalog=Catalog.builtin(),
                runtimes=DeterministicRuntime(demo_responder),
                memory_store=memory,
                approval_store=approvals,
                run_store=runs,
            ).decide(
                problem(risk_tier="high"),
                organization_id="launch-red-team",
            )
        )

        with pytest.raises(ValueError, match="must be approved"):
            finalize_approved_decision(
                catalog=Catalog.builtin(),
                request_id=report.approval_ids[0],
                approval_store=approvals,
                memory_store=memory,
                run_store=runs,
            )


def test_tampered_decision_binding_cannot_finalize(tmp_path: Path) -> None:
    approval_path = tmp_path / "approvals.sqlite"
    runs = LocalRunStore(tmp_path / "runs")
    with (
        InstitutionalMemoryStore(tmp_path / "memory.sqlite") as memory,
        ApprovalStore(approval_path) as approvals,
    ):
        report = asyncio.run(
            ThinkTank(
                catalog=Catalog.builtin(),
                runtimes=DeterministicRuntime(demo_responder),
                memory_store=memory,
                approval_store=approvals,
                run_store=runs,
            ).decide(
                problem(risk_tier="high"),
                organization_id="launch-red-team",
            )
        )
        request_id = report.approval_ids[0]
        approvals.approve(
            request_id,
            actor_id="human-l3-reviewer",
            approver_level=ApprovalLevel.L3,
        )

        with sqlite3.connect(approval_path) as connection:
            row = connection.execute(
                "SELECT payload FROM approval_requests WHERE id = ?", (request_id,)
            ).fetchone()
            assert row is not None
            payload = json.loads(row[0])
            payload["decision_binding_hash"] = "0" * 64
            connection.execute(
                "UPDATE approval_requests SET payload = ? WHERE id = ?",
                (json.dumps(payload, sort_keys=True), request_id),
            )

        with pytest.raises(ValueError, match="different decision digest"):
            finalize_approved_decision(
                catalog=Catalog.builtin(),
                request_id=request_id,
                approval_store=approvals,
                memory_store=memory,
                run_store=runs,
            )


class CrashBeforeMemoryCommitStore(InstitutionalMemoryStore):
    def commit_decision_memory(self, *args, **kwargs):
        raise RuntimeError("simulated crash before memory commit")


class CrashAfterMemoryStageStore(InstitutionalMemoryStore):
    def stage_candidate(self, *args, **kwargs):
        super().stage_candidate(*args, **kwargs)
        raise RuntimeError("simulated crash after durable stage")


class CrashBeforeApprovalPublishStore(ApprovalStore):
    def pending(self, *args, **kwargs):
        raise RuntimeError("simulated crash before approval publish")


def test_memory_outbox_sync_recovers_and_is_idempotent(tmp_path: Path) -> None:
    memory_path = tmp_path / "memory.sqlite"
    runs_path = tmp_path / "runs"
    runs = LocalRunStore(runs_path)
    with CrashBeforeMemoryCommitStore(memory_path) as crashing_memory:
        with pytest.raises(RuntimeError, match="simulated crash"):
            asyncio.run(
                ThinkTank(
                    catalog=Catalog.builtin(),
                    runtimes=DeterministicRuntime(demo_responder),
                    memory_store=crashing_memory,
                    run_store=runs,
                ).decide(
                    problem(),
                    organization_id="product-roundtable",
                )
            )

    run_ids = sorted(path.name for path in runs_path.iterdir() if path.name.startswith("run-"))
    assert len(run_ids) == 1
    run_id = run_ids[0]
    assert not (runs_path / run_id / "completion.json").exists()

    with InstitutionalMemoryStore(tmp_path / "wrong-memory.sqlite") as wrong_memory:
        with pytest.raises(ValueError, match="memory store does not match"):
            sync_memory_outbox(
                run_id=run_id,
                memory_store=wrong_memory,
                run_store=runs,
            )

    with InstitutionalMemoryStore(memory_path) as memory:
        with pytest.raises(MemoryNotFoundError):
            runs.mark_complete(runs.load(run_id), memory_store=memory)
        first = sync_memory_outbox(run_id=run_id, memory_store=memory, run_store=runs)
        second = sync_memory_outbox(run_id=run_id, memory_store=memory, run_store=runs)

        assert first == second
        assert first is not None
        assert first["status"] == MemoryStatus.APPROVED.value
        memory_id = f"memory-{run_id}"
        assert [event.event for event in memory.audit_events(memory_id)] == [
            "staged",
            "approved",
            "decision_committed",
        ]
    assert (runs_path / run_id / "completion.json").is_file()


def test_stage_outbox_reconciles_after_candidate_was_human_approved(tmp_path: Path) -> None:
    memory_path = tmp_path / "memory.sqlite"
    runs_path = tmp_path / "runs"
    runs = LocalRunStore(runs_path)
    with CrashAfterMemoryStageStore(memory_path) as crashing_memory:
        with pytest.raises(RuntimeError, match="after durable stage"):
            asyncio.run(
                ThinkTank(
                    catalog=Catalog.builtin(),
                    runtimes=DeterministicRuntime(demo_responder),
                    memory_store=crashing_memory,
                    run_store=runs,
                ).decide(problem(), organization_id="strategy-cabinet")
            )

    run_id = next(path.name for path in runs_path.iterdir() if path.name.startswith("run-"))
    with InstitutionalMemoryStore(memory_path) as memory:
        memory.approve(f"memory-{run_id}", approver_id="human-reviewer")
        result = sync_memory_outbox(
            run_id=run_id,
            memory_store=memory,
            run_store=runs,
        )

    assert result is not None
    assert result["status"] == MemoryStatus.APPROVED.value
    assert (runs_path / run_id / "completion.json").is_file()


def test_approval_outbox_recovers_before_completion(tmp_path: Path) -> None:
    memory_path = tmp_path / "memory.sqlite"
    approval_path = tmp_path / "approvals.sqlite"
    runs_path = tmp_path / "runs"
    runs = LocalRunStore(runs_path)
    with (
        InstitutionalMemoryStore(memory_path) as memory,
        CrashBeforeApprovalPublishStore(approval_path) as crashing_approvals,
    ):
        with pytest.raises(RuntimeError, match="before approval publish"):
            asyncio.run(
                ThinkTank(
                    catalog=Catalog.builtin(),
                    runtimes=DeterministicRuntime(demo_responder),
                    memory_store=memory,
                    approval_store=crashing_approvals,
                    run_store=runs,
                ).decide(
                    problem(risk_tier="high"),
                    organization_id="launch-red-team",
                )
            )

    run_id = next(path.name for path in runs_path.iterdir() if path.name.startswith("run-"))
    report = runs.load(run_id)
    with (
        InstitutionalMemoryStore(memory_path) as memory,
        ApprovalStore(approval_path) as approvals,
    ):
        assert (
            sync_memory_outbox(
                run_id=run_id,
                memory_store=memory,
                approval_store=approvals,
                run_store=runs,
            )
            is None
        )
        assert approvals.get(report.approval_ids[0]).status == ApprovalStatus.PENDING

    assert (runs_path / run_id / "completion.json").is_file()


def test_structured_output_failure_keeps_runtime_forensics() -> None:
    def invalid_response(_invocation):
        return RawAgentResponse(
            content="this is not valid structured output",
            runtime="invalid-structured-runtime",
            session_id="session-forensic-123",
            tool_events=({"tool": "search", "status": "completed"},),
            tool_artifacts=({"id": "artifact-forensic", "kind": "search-result"},),
        )

    report = asyncio.run(
        ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=DeterministicRuntime(invalid_response),
        ).decide(
            problem(),
            organization_id="product-roundtable",
            persist=False,
        )
    )

    assert report.status == "inconclusive"
    assert report.messages == []
    assert report.runtime_calls
    assert all(not call.success for call in report.runtime_calls)
    assert all(call.session_id == "session-forensic-123" for call in report.runtime_calls)
    assert all(call.response_hash for call in report.runtime_calls)
    failed_events = [event for event in report.audit_events if event.event_type == "runtime_failed"]
    assert len(failed_events) == len(report.runtime_calls)
    assert all(event.payload["tool_event_hashes"] for event in failed_events)
    assert all(event.payload["tool_artifacts"] for event in failed_events)
    assert all(event.payload["response_hash"] for event in failed_events)


@pytest.mark.parametrize("grant", ["memory", "session_search"])
def test_mutable_profile_grants_force_human_review(grant: str) -> None:
    report = asyncio.run(
        ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=DeterministicRuntime(demo_responder),
        ).decide(
            problem(grants=[grant]),
            organization_id="product-roundtable",
            persist=False,
        )
    )

    assert report.status == "needs_human"
    assert report.status_reason == "mutable profile history tools were explicitly enabled"
    assert report.runtime_calls
    assert all(grant in call.enabled_toolsets for call in report.runtime_calls)
