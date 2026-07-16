from __future__ import annotations

from datetime import UTC, datetime

from hsa_thinktank.aggregation import aggregate
from hsa_thinktank.audit import AuditTrail
from hsa_thinktank.catalog import Catalog
from hsa_thinktank.models import (
    Contribution,
    Criterion,
    DecisionOption,
    DecisionProblem,
    Objection,
    RationaleClaim,
    validate_option_references,
)
from hsa_thinktank.protocols import ProtocolOutcome


def ballot(preferred: str, a: float, b: float, *, critical: bool = False) -> Contribution:
    return Contribution(
        preferred_option_id=preferred,
        option_scores={"launch": a, "wait": b},
        confidence=0.8,
        claims=[
            RationaleClaim(
                claim=f"prefer {preferred}",
                basis="grounded",
                evidence_ids=["market-study"],
                memory_ids=["memory-one"],
                tool_artifact_ids=["tool-one"],
            )
        ],
        risks=[
            Objection(
                option_id="launch",
                severity="critical" if critical else "medium",
                statement="unresolved launch risk",
            )
        ]
        if critical
        else [],
        next_actions=["run a pilot"],
    )


def problem(risk_tier: str = "medium") -> DecisionProblem:
    return DecisionProblem(
        id="decision-test",
        question="launch?",
        risk_tier=risk_tier,
        options=[
            DecisionOption(id="launch", description="Launch now"),
            DecisionOption(id="wait", description="Wait"),
        ],
    )


def hard_constraint_ballot(
    preferred: str,
    launch_score: float,
    wait_score: float,
    *,
    launch_allowed: bool,
    wait_allowed: bool,
) -> Contribution:
    return Contribution(
        preferred_option_id=preferred,
        option_scores={"launch": launch_score, "wait": wait_score},
        constraint_results={
            "launch": {"legal": launch_allowed},
            "wait": {"legal": wait_allowed},
        },
        confidence=0.8,
        claims=[
            RationaleClaim(
                claim=f"prefer {preferred}",
                basis="grounded",
                evidence_ids=["legal-review"],
            )
        ],
    )


def hard_constraint_problem() -> DecisionProblem:
    return DecisionProblem(
        id="decision-hard-constraint",
        question="Which legally permitted option should we take?",
        options=[
            DecisionOption(id="launch", description="Launch now"),
            DecisionOption(id="wait", description="Wait"),
        ],
        criteria=[
            Criterion(
                id="legal",
                description="Must satisfy the legal gate",
                hard_constraint=True,
            )
        ],
    )


def test_builtin_catalog_is_referentially_valid() -> None:
    catalog = Catalog.builtin()
    assert sorted(catalog.profiles) == ["charlie-munger", "donella-meadows", "steve-jobs"]
    assert sorted(catalog.organizations) == [
        "launch-red-team",
        "product-roundtable",
        "strategy-cabinet",
    ]


def test_aggregation_discounts_shared_runtime_correlation() -> None:
    organization = Catalog.builtin().organization("product-roundtable")
    outcome = ProtocolOutcome(
        ballots={
            "steve-jobs": ballot("launch", 0.85, 0.4),
            "charlie-munger": ballot("wait", 0.55, 0.65),
            "donella-meadows": ballot("launch", 0.8, 0.5),
        },
        successful_member_ids={"steve-jobs", "charlie-munger", "donella-meadows"},
    )
    decision = aggregate(problem(), organization, outcome)
    assert decision.status == "decided"
    assert decision.selected_option_id == "launch"
    assert decision.effective_sample_size == 1.0
    assert decision.correlation_group_count == 1
    assert decision.memory_ids == ["memory-one"]
    assert decision.tool_artifact_ids == ["tool-one"]
    assert any("charlie-munger" in item for item in decision.dissent)


def test_high_risk_and_critical_risk_do_not_silently_decide() -> None:
    catalog = Catalog.builtin()
    roundtable = catalog.organization("product-roundtable")
    common = ProtocolOutcome(
        ballots={
            "steve-jobs": ballot("launch", 0.9, 0.2),
            "charlie-munger": ballot("launch", 0.8, 0.3),
        },
        successful_member_ids={"steve-jobs", "charlie-munger"},
    )
    assert aggregate(problem("high"), roundtable, common).status == "needs_human"

    red_team = catalog.organization("launch-red-team")
    critical = ProtocolOutcome(
        ballots={"donella-meadows": ballot("launch", 0.9, 0.2)},
        successful_member_ids={"steve-jobs", "charlie-munger", "donella-meadows"},
        forced_objections=[
            Objection(
                option_id="launch",
                severity="critical",
                statement="hard constraint violated",
            )
        ],
        status_hint="rejected",
    )
    assert aggregate(problem(), red_team, critical).status == "rejected"


def test_audit_hash_chain_detects_tampering() -> None:
    instant = datetime(2026, 7, 16, tzinfo=UTC)
    trail = AuditTrail("run-test", clock=lambda: instant)
    trail.append("one", {"value": 1})
    trail.append("two", {"value": 2})
    assert trail.verify()
    trail.events[0].payload["value"] = 999
    assert not trail.verify()


def test_partial_criterion_scores_cannot_override_complete_option_scores() -> None:
    contribution = ballot("wait", 0.1, 0.9).model_copy(
        update={
            "criterion_scores": {
                "launch": {"goal-fit": 1.0},
                "wait": {"goal-fit": 0.0},
            }
        }
    )

    try:
        validate_option_references(
            contribution,
            {"launch", "wait"},
            {"goal-fit", "downside", "execution"},
        )
    except ValueError as exc:
        assert "exactly frozen criteria" in str(exc)
    else:
        raise AssertionError("partial criterion scores were accepted")


def test_hard_constraint_veto_excludes_the_higher_scoring_option() -> None:
    organization = Catalog.builtin().organization("product-roundtable")
    outcome = ProtocolOutcome(
        ballots={
            "steve-jobs": hard_constraint_ballot(
                "launch",
                0.99,
                0.10,
                launch_allowed=True,
                wait_allowed=True,
            ),
            "charlie-munger": hard_constraint_ballot(
                "wait",
                0.89,
                0.90,
                launch_allowed=False,
                wait_allowed=True,
            ),
        },
        successful_member_ids={"steve-jobs", "charlie-munger"},
    )

    decision = aggregate(hard_constraint_problem(), organization, outcome)

    assert decision.status == "decided"
    assert decision.selected_option_id == "wait"
    assert decision.option_scores["launch"] > decision.option_scores["wait"]


def test_missing_hard_constraint_matrix_fails_closed() -> None:
    frozen = hard_constraint_problem()
    missing = ballot("launch", 0.9, 0.2)

    try:
        validate_option_references(
            missing,
            {"launch", "wait"},
            {"legal"},
            {"legal"},
        )
    except ValueError as exc:
        assert "constraint_results must contain exactly frozen options" in str(exc)
    else:
        raise AssertionError("missing hard-constraint matrix was accepted")

    # Aggregation also treats an unvalidated/missing matrix as a failed gate.
    outcome = ProtocolOutcome(
        ballots={
            "steve-jobs": missing,
            "charlie-munger": ballot("launch", 0.8, 0.3),
        },
        successful_member_ids={"steve-jobs", "charlie-munger"},
    )
    decision = aggregate(
        frozen,
        Catalog.builtin().organization("product-roundtable"),
        outcome,
    )
    assert decision.status == "inconclusive"
    assert decision.selected_option_id is None
