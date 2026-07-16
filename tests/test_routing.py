from __future__ import annotations

import pytest

from hsa_thinktank.catalog import Catalog
from hsa_thinktank.models import DecisionProblem, EvidenceItem
from hsa_thinktank.routing import MeetingRouter, validate_selection_against_catalog


def route(question: str, *, risk_tier: str = "medium"):
    problem = DecisionProblem(question=question, risk_tier=risk_tier)
    return problem, MeetingRouter(Catalog.builtin()).select(problem)


def test_product_context_selects_product_and_risk_hsas() -> None:
    problem, selection = route("如何改善产品的新用户体验和功能路线图？")

    assert selection.mode == "auto"
    assert selection.problem_snapshot_hash == problem.snapshot_hash
    assert selection.organization_id == "product-roundtable"
    assert selection.protocol == "roundtable"
    assert selection.selected_hsa_ids == ["steve-jobs", "charlie-munger"]
    assert selection.matched_signals == ["product"]
    assert selection.effective_organization.chair_id == "steve-jobs"


def test_system_strategy_context_selects_a_two_hsa_cabinet() -> None:
    _, selection = route("如何调整系统反馈回路，减少长期延迟和外部性？")

    assert selection.organization_id == "strategy-cabinet"
    assert selection.protocol == "cabinet"
    assert selection.selected_hsa_ids == ["charlie-munger", "donella-meadows"]
    assert selection.effective_organization.chair_id == "donella-meadows"
    assert selection.effective_organization.min_quorum == 2


def test_product_and_system_context_selects_complementary_pair() -> None:
    _, selection = route("如何重构产品体验，同时改善系统反馈和生态外部性？")

    assert selection.organization_id == "product-roundtable"
    assert selection.selected_hsa_ids == ["steve-jobs", "donella-meadows"]


@pytest.mark.parametrize(
    ("question", "risk_tier"),
    [
        ("本周是否全量发布新计费系统？", "medium"),
        ("是否改变定价？", "high"),
    ],
)
def test_release_or_high_risk_uses_full_red_team(question: str, risk_tier: str) -> None:
    _, selection = route(question, risk_tier=risk_tier)

    assert selection.organization_id == "launch-red-team"
    assert selection.protocol == "red_team"
    assert selection.selected_hsa_ids == [
        "steve-jobs",
        "charlie-munger",
        "donella-meadows",
    ]
    assert selection.effective_organization.judge_ids == ["donella-meadows"]


def test_high_risk_fails_closed_without_auto_selectable_red_team() -> None:
    builtin = Catalog.builtin()
    catalog = Catalog(
        builtin.profiles.values(),
        [builtin.organization("product-roundtable")],
    )
    problem = DecisionProblem(question="是否改变定价？", risk_tier="high")

    with pytest.raises(ValueError, match="auto-selectable red_team"):
        MeetingRouter(catalog).select(problem)


def test_ambiguous_context_falls_back_to_full_roundtable() -> None:
    _, selection = route("我们应该选择 A 还是 B？")

    assert selection.organization_id == "product-roundtable"
    assert selection.selected_hsa_ids == [
        "steve-jobs",
        "charlie-munger",
        "donella-meadows",
    ]
    assert selection.matched_signals == []


def test_explicit_organization_is_never_overridden() -> None:
    catalog = Catalog.builtin()
    problem = DecisionProblem(question="改善产品体验")
    selection = MeetingRouter(catalog).select(
        problem,
        requested_organization_id="strategy-cabinet",
    )

    assert selection.mode == "explicit"
    assert selection.organization_id == "strategy-cabinet"
    assert selection.effective_organization == catalog.organization("strategy-cabinet")
    assert len(selection.selected_hsa_ids) == 3


def test_evidence_body_cannot_silently_change_the_route() -> None:
    catalog = Catalog.builtin()
    baseline = DecisionProblem(question="改善产品体验")
    quoted = baseline.model_copy(
        update={
            "evidence": [
                EvidenceItem(
                    id="quoted-text",
                    title="用户研究",
                    content="Quoted unrelated text: launch deploy policy capital system risk.",
                )
            ]
        }
    )

    first = MeetingRouter(catalog).select(baseline)
    second = MeetingRouter(catalog).select(quoted)

    assert first.selected_hsa_ids == second.selected_hsa_ids
    assert first.protocol == second.protocol


def test_persisted_effective_roster_rejects_foreign_member() -> None:
    catalog = Catalog.builtin()
    _, selection = route("改善产品体验")
    foreign = catalog.organization("product-roundtable").model_copy(
        update={
            "members": [
                catalog.organization("product-roundtable").member("steve-jobs"),
                catalog.organization("strategy-cabinet").member("donella-meadows"),
            ],
            "min_quorum": 2,
        }
    )
    tampered = selection.model_copy(
        update={
            "effective_organization": foreign,
            "effective_organization_fingerprint": foreign.fingerprint,
            "selected_hsa_ids": ["steve-jobs", "donella-meadows"],
        }
    )

    with pytest.raises(ValueError, match="modified or foreign"):
        validate_selection_against_catalog(catalog, tampered)
