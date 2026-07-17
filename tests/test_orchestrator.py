from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from hsa_thinktank.audit import AuditTrail
from hsa_thinktank.catalog import Catalog
from hsa_thinktank.demo import demo_responder
from hsa_thinktank.memory import (
    InstitutionalMemoryStore,
    MemoryCandidate,
    MemoryScope,
    MemoryStatus,
    ApprovalStore,
)
from hsa_thinktank.models import DecisionOption, DecisionProblem
from hsa_thinktank.errors import ProtocolError
from hsa_thinktank.orchestrator import ThinkTank
from hsa_thinktank.run_store import LocalRunStore
from hsa_thinktank.runtime import DeterministicRuntime, RawAgentResponse


def _problem(*, risk_tier: str = "medium", grants: list[str] | None = None) -> DecisionProblem:
    return DecisionProblem(
        id="decision-integration",
        question="Should we launch?",
        options=[
            DecisionOption(id="launch", description="Launch with checkpoints"),
            DecisionOption(id="wait", description="Wait for more evidence"),
        ],
        risk_tier=risk_tier,
        user_tool_grants=grants or [],
    )


@pytest.mark.parametrize(
    ("organization_id", "expected_calls"),
    [
        ("capital-roundtable", 8),
        ("grand-strategy-cabinet", 13),
        ("philosophy-roundtable", 6),
        ("product-roundtable", 6),
        ("launch-red-team", 4),
        ("science-technology-roundtable", 14),
        ("strategy-cabinet", 3),
    ],
)
def test_demo_runs_each_organization_end_to_end(
    tmp_path: Path,
    organization_id: str,
    expected_calls: int,
) -> None:
    catalog = Catalog.builtin()
    runtime = DeterministicRuntime(demo_responder)
    memory_path = tmp_path / f"{organization_id}-memory.sqlite"
    approval_path = tmp_path / f"{organization_id}-approvals.sqlite"
    runs_path = tmp_path / "runs"
    with InstitutionalMemoryStore(memory_path) as memory, ApprovalStore(approval_path) as approvals:
        report = asyncio.run(
            ThinkTank(
                catalog=catalog,
                runtimes=runtime,
                memory_store=memory,
                approval_store=approvals,
                run_store=LocalRunStore(runs_path),
            ).decide(_problem(), organization_id=organization_id)
        )

        assert report.status == "decided"
        assert report.selected_option_id == "launch"
        assert len(report.runtime_calls) == expected_calls
        assert all(call.success for call in report.runtime_calls)
        assert all(call.enabled_toolsets for call in report.runtime_calls)
        assert report.request_snapshot_hash == report.frozen_problem_hash
        assert report.request_snapshot == report.frozen_problem
        assert report.frozen_problem.question == "Should we launch?"
        assert memory.get(f"memory-{report.run_id}").status in {
            MemoryStatus.APPROVED,
            MemoryStatus.STAGED,
        }

    trail = AuditTrail(report.run_id)
    trail.events = report.audit_events
    assert trail.verify()
    run_dir = runs_path / report.run_id
    assert (run_dir / "decision.json").is_file()
    assert (run_dir / "events.jsonl").is_file()
    assert (run_dir / "messages.jsonl").is_file()
    public_summary = json.loads((run_dir / "public-summary.json").read_text(encoding="utf-8"))
    assert public_summary["question"] == "Should we launch?"
    assert "messages" not in public_summary
    assert "request_snapshot" not in public_summary


def test_generated_options_preserve_original_request_hash(tmp_path: Path) -> None:
    problem = DecisionProblem(
        id="decision-generate",
        question="What should we do next?",
    )
    original_hash = problem.snapshot_hash
    runtime = DeterministicRuntime(demo_responder)
    with InstitutionalMemoryStore(tmp_path / "memory.sqlite") as memory:
        report = asyncio.run(
            ThinkTank(
                catalog=Catalog.builtin(),
                runtimes=runtime,
                memory_store=memory,
            ).decide(problem, organization_id="product-roundtable", persist=False)
        )

    assert report.status == "decided"
    assert report.request_snapshot_hash == original_hash
    assert report.frozen_problem_hash != original_hash
    assert report.request_snapshot.options == []
    assert len(report.frozen_problem.options) == 3
    assert len(report.runtime_calls) == 7
    assert report.messages[0].phase == "option_generation"


def test_auto_route_invokes_only_selected_hsas(tmp_path: Path) -> None:
    problem = DecisionProblem(
        id="decision-auto-route",
        question="如何改善产品的新用户体验和功能路线图？",
        options=[
            DecisionOption(id="onboarding", description="改善新手引导"),
            DecisionOption(id="navigation", description="简化核心导航"),
        ],
    )
    runtime = DeterministicRuntime(demo_responder)
    report = asyncio.run(
        ThinkTank(catalog=Catalog.builtin(), runtimes=runtime).decide(
            problem,
            persist=False,
        )
    )

    assert report.meeting_selection.mode == "auto"
    assert report.meeting_selection.selected_hsa_ids == ["steve-jobs", "charlie-munger"]
    assert {call.hsa_id for call in report.runtime_calls} == {"steve-jobs", "charlie-munger"}
    assert len(report.runtime_calls) == 4
    meeting_events = [
        event for event in report.audit_events if event.event_type == "meeting_selected"
    ]
    assert len(meeting_events) == 1
    assert meeting_events[0].payload == report.meeting_selection.model_dump(mode="json")


def test_auto_routed_two_hsa_cabinet_runs_end_to_end() -> None:
    problem = DecisionProblem(
        id="decision-auto-cabinet",
        question="如何调整系统反馈回路，减少长期延迟和外部性？",
        options=[
            DecisionOption(id="rules", description="调整系统规则"),
            DecisionOption(id="signals", description="改善反馈信息流"),
        ],
    )
    runtime = DeterministicRuntime(demo_responder)
    report = asyncio.run(
        ThinkTank(catalog=Catalog.builtin(), runtimes=runtime).decide(
            problem,
            persist=False,
        )
    )

    assert report.status == "decided"
    assert report.protocol_name == "cabinet"
    assert report.meeting_selection.organization_id == "grand-strategy-cabinet"
    assert report.meeting_selection.selected_hsa_ids == ["donella-meadows", "elon-musk"]
    assert report.meeting_selection.effective_organization.chair_id == "donella-meadows"
    assert {call.hsa_id for call in report.runtime_calls} == {
        "donella-meadows",
        "elon-musk",
    }
    assert len(report.runtime_calls) == 2


def test_non_persistent_high_risk_decision_has_no_control_plane_side_effects(
    tmp_path: Path,
) -> None:
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
            ).decide(
                _problem(risk_tier="high"),
                organization_id="strategy-cabinet",
                persist=False,
            )
        )

        assert report.status == "needs_human"
        assert report.approval_ids == []
        assert approvals.audit_events() == []
        assert memory.audit_events() == []
        assert any(
            event.event_type == "approval_skipped"
            and event.payload["reason"] == "non_persistent_run"
            for event in report.audit_events
        )


def test_user_tool_grant_cannot_self_approve_l3_toolset(tmp_path: Path) -> None:
    runtime = DeterministicRuntime(demo_responder)
    report = asyncio.run(
        ThinkTank(catalog=Catalog.builtin(), runtimes=runtime).decide(
            _problem(grants=["external_action"]),
            organization_id="product-roundtable",
            persist=False,
        )
    )

    assert report.status == "decided"
    assert all(
        "external_action" not in invocation.enabled_toolsets for invocation in runtime.invocations
    )
    resolutions = [
        event for event in report.audit_events if event.event_type == "tool_policy_resolved"
    ]
    assert resolutions
    assert all(
        any(
            item["toolset"] == "external_action"
            and item["reason"] in {"not_in_phase_allowlist", "human_approval_required"}
            for item in event.payload["rejected"]
        )
        for event in resolutions
    )


def test_persist_true_requires_a_run_store() -> None:
    with pytest.raises(ValueError, match="requires a durable LocalRunStore"):
        asyncio.run(
            ThinkTank(
                catalog=Catalog.builtin(),
                runtimes=DeterministicRuntime(demo_responder),
            ).decide(
                _problem(),
                organization_id="product-roundtable",
            )
        )


def test_private_disabled_excludes_memory_and_profile_context(tmp_path: Path) -> None:
    base = Catalog.builtin()
    source_org = base.organization("product-roundtable")
    private_off = source_org.model_copy(
        update={
            "id": "private-off-roundtable",
            "memory_policy": source_org.memory_policy.model_copy(update={"private_enabled": False}),
        }
    )
    catalog = Catalog(base.profiles.values(), [private_off])
    runtime = DeterministicRuntime(demo_responder)
    with InstitutionalMemoryStore(tmp_path / "memory.sqlite") as memory:
        memory.stage_candidate(
            MemoryCandidate(
                id="private-secret",
                owner_id="steve-jobs",
                scope=MemoryScope.PRIVATE,
                content="do not inject this secret",
                source_event_ids=["source-private"],
                confidence=0.8,
            )
        )
        memory.stage_candidate(
            MemoryCandidate(
                id="shared-fact",
                owner_id="hsa-orchestrator",
                organization_id=private_off.id,
                scope=MemoryScope.ORGANIZATION,
                content="shared evidence is allowed",
                source_event_ids=["source-shared"],
                confidence=0.9,
            )
        )
        memory.approve("private-secret")
        memory.approve("shared-fact")

        report = asyncio.run(
            ThinkTank(
                catalog=catalog,
                runtimes=runtime,
                memory_store=memory,
            ).decide(
                _problem(),
                organization_id=private_off.id,
                persist=False,
            )
        )

    assert report.status == "decided"
    assert runtime.invocations
    assert all(not item.load_profile_context for item in runtime.invocations)
    assert all("memory" not in item.enabled_toolsets for item in runtime.invocations)
    assert all("session_search" not in item.enabled_toolsets for item in runtime.invocations)
    assert all("do not inject this secret" not in item.user_prompt for item in runtime.invocations)
    assert all("shared evidence is allowed" in item.user_prompt for item in runtime.invocations)


def test_tool_artifact_ids_require_runtime_provenance(tmp_path: Path) -> None:
    def hallucinating_responder(invocation):
        payload = demo_responder(invocation)
        payload["claims"][0]["tool_artifact_ids"] = ["made-up-artifact"]
        return payload

    rejected = asyncio.run(
        ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=DeterministicRuntime(hallucinating_responder),
        ).decide(
            _problem(),
            organization_id="product-roundtable",
            persist=False,
        )
    )
    assert rejected.status == "inconclusive"
    assert all(not call.success for call in rejected.runtime_calls)
    assert all(
        "unavailable tool artifacts" in (call.error or "") for call in rejected.runtime_calls
    )

    def sourced_responder(invocation):
        payload = demo_responder(invocation)
        payload["claims"][0]["tool_artifact_ids"] = ["artifact-one"]
        return RawAgentResponse(
            content=json.dumps(payload),
            runtime="artifact-test",
            tool_artifacts=({"id": "artifact-one", "kind": "search"},),
        )

    accepted = asyncio.run(
        ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=DeterministicRuntime(sourced_responder),
        ).decide(
            _problem(),
            organization_id="product-roundtable",
            persist=False,
        )
    )
    assert accepted.status == "decided"
    assert accepted.tool_artifact_ids == ["artifact-one"]
    assert any(
        event.payload.get("tool_artifacts")
        for event in accepted.audit_events
        if event.event_type == "runtime_completed"
    )


def test_public_url_in_artifact_ids_is_normalized_without_fabricating_tool_evidence() -> None:
    source_url = "https://example.com/public-filing"

    def responder(invocation):
        payload = demo_responder(invocation)
        payload["claims"][0]["basis"] = "grounded"
        payload["claims"][0]["tool_artifact_ids"] = [source_url]
        return payload

    report = asyncio.run(
        ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=DeterministicRuntime(responder),
        ).decide(
            _problem(),
            organization_id="product-roundtable",
            persist=False,
        )
    )

    assert report.status == "decided"
    assert all(call.success for call in report.runtime_calls)
    assert report.tool_artifact_ids == []
    assert all(
        [str(url) for url in claim.source_urls] == [source_url] for claim in report.rationale_claims
    )
    runtime_events = [
        event for event in report.audit_events if event.event_type == "runtime_completed"
    ]
    assert runtime_events
    assert all(
        event.payload["normalizations"][0]["code"] == "url_artifact_moved_to_source"
        for event in runtime_events
    )
    assert all(source_url not in str(event.payload["normalizations"]) for event in runtime_events)


def test_empty_grounded_claim_is_downgraded_and_counted_in_quorum() -> None:
    def responder(invocation):
        payload = demo_responder(invocation)
        payload["claims"][0].update(
            {
                "basis": "grounded",
                "principle_ids": [],
                "evidence_ids": [],
                "memory_ids": [],
                "tool_artifact_ids": [],
            }
        )
        return payload

    report = asyncio.run(
        ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=DeterministicRuntime(responder),
        ).decide(
            _problem(),
            organization_id="product-roundtable",
            persist=False,
        )
    )

    assert report.status == "decided"
    assert set(report.successful_member_ids) == set(report.meeting_selection.selected_hsa_ids)
    assert all(call.success for call in report.runtime_calls)
    assert all(claim.basis == "inferred" for claim in report.rationale_claims)
    assert all(
        event.payload["normalizations"][0]["code"] == "grounded_without_provenance_downgraded"
        for event in report.audit_events
        if event.event_type == "runtime_completed"
    )


def test_generated_option_claims_share_provenance_normalization_and_prompt_rules() -> None:
    source_url = "https://example.com/market-data"

    def responder(invocation):
        payload = demo_responder(invocation)
        if invocation.metadata["response_type"] == "GeneratedOptions":
            payload["claims"] = [
                {
                    "claim": "The public data motivates these candidate actions",
                    "basis": "grounded",
                    "tool_artifact_ids": [source_url],
                }
            ]
        return payload

    runtime = DeterministicRuntime(responder)
    report = asyncio.run(
        ThinkTank(catalog=Catalog.builtin(), runtimes=runtime).decide(
            DecisionProblem(id="decision-option-claims", question="What should we do?"),
            organization_id="product-roundtable",
            persist=False,
        )
    )

    generated_message = report.messages[0]
    assert generated_message.phase == "option_generation"
    assert generated_message.payload["claims"][0]["tool_artifact_ids"] == []
    assert generated_message.payload["claims"][0]["source_urls"] == [source_url]
    assert "source_urls" in runtime.invocations[0].user_prompt
    assert "禁止把 URL 放入该字段" in runtime.invocations[0].user_prompt


def test_generated_option_claim_with_unknown_opaque_artifact_fails_closed() -> None:
    def responder(invocation):
        payload = demo_responder(invocation)
        if invocation.metadata["response_type"] == "GeneratedOptions":
            payload["claims"] = [
                {
                    "claim": "Unsupported option-generation claim",
                    "basis": "grounded",
                    "tool_artifact_ids": ["made-up-artifact"],
                }
            ]
        return payload

    with pytest.raises(ProtocolError, match="failed to generate a valid frozen option set"):
        asyncio.run(
            ThinkTank(
                catalog=Catalog.builtin(),
                runtimes=DeterministicRuntime(responder),
            ).decide(
                DecisionProblem(id="decision-bad-option-claim", question="What should we do?"),
                organization_id="product-roundtable",
                persist=False,
            )
        )


def test_red_team_attack_url_uses_public_source_without_a_grounded_downgrade() -> None:
    source_with_secret_query = "https://example.com/risk?access_token=private#details"

    def responder(invocation):
        payload = demo_responder(invocation)
        if invocation.metadata["response_type"] == "RedTeamCritique":
            payload["attacks"][0]["tool_artifact_ids"] = [source_with_secret_query]
        return payload

    report = asyncio.run(
        ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=DeterministicRuntime(responder),
        ).decide(
            _problem(),
            organization_id="launch-red-team",
            persist=False,
        )
    )

    critique = next(message for message in report.messages if message.phase == "red_critique")
    attack = critique.payload["attacks"][0]
    assert attack["tool_artifact_ids"] == []
    assert attack["source_urls"] == ["https://example.com/risk"]
    assert "basis" not in attack
    critique_call = next(call for call in report.runtime_calls if call.phase == "red_critique")
    assert critique_call.success is True


def test_mixed_three_member_provenance_shapes_all_count_toward_quorum() -> None:
    def responder(invocation):
        payload = demo_responder(invocation)
        if invocation.hsa_id == "charlie-munger":
            payload["claims"][0]["basis"] = "grounded"
            payload["claims"][0]["tool_artifact_ids"] = ["https://example.com/public-source"]
        elif invocation.hsa_id == "donella-meadows":
            payload["claims"][0].update(
                {
                    "basis": "grounded",
                    "principle_ids": [],
                    "evidence_ids": [],
                    "memory_ids": [],
                    "tool_artifact_ids": [],
                }
            )
        return payload

    report = asyncio.run(
        ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=DeterministicRuntime(responder),
        ).decide(
            _problem(),
            organization_id="product-roundtable",
            persist=False,
        )
    )

    assert report.status == "decided"
    assert set(report.successful_member_ids) == {
        "steve-jobs",
        "charlie-munger",
        "donella-meadows",
    }
    assert len(report.runtime_calls) == 6
    assert all(call.success for call in report.runtime_calls)


def test_invalid_source_url_shape_cannot_leak_input_into_runtime_or_audit() -> None:
    secret = "https://example.com/source?access_token=do-not-persist"

    def responder(invocation):
        payload = demo_responder(invocation)
        payload["claims"][0].update(
            {
                "basis": "grounded",
                "source_urls": secret,
                "tool_artifact_ids": [],
            }
        )
        return payload

    report = asyncio.run(
        ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=DeterministicRuntime(responder),
        ).decide(
            _problem(),
            organization_id="product-roundtable",
            persist=False,
        )
    )

    assert report.status == "inconclusive"
    assert all(not call.success for call in report.runtime_calls)
    private_record = json.dumps(
        {
            "runtime_calls": [call.model_dump(mode="json") for call in report.runtime_calls],
            "audit_events": [event.model_dump(mode="json") for event in report.audit_events],
        },
        ensure_ascii=False,
        sort_keys=True,
    )
    assert "do-not-persist" not in private_record
    assert "access_token" not in private_record
    assert all("types=list_type" in (call.error or "") for call in report.runtime_calls)
