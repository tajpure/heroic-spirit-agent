from __future__ import annotations

import asyncio

import pytest

from hsa_thinktank.catalog import Catalog
from hsa_thinktank.models import (
    Contribution,
    ExecutiveDecision,
    OrganizationMember,
    RationaleClaim,
    RedTeamCritique,
    RedTeamRebuttal,
)
from hsa_thinktank.protocols import (
    CabinetProtocol,
    ProtocolTask,
    RedTeamProtocol,
    RoundtableProtocol,
    TaskResult,
)


def contribution(preferred: str = "go", *, sensitive_provenance: bool = False) -> Contribution:
    claim = RationaleClaim(claim=f"choose {preferred}", basis="inferred")
    if sensitive_provenance:
        claim = RationaleClaim(
            claim=f"choose {preferred}",
            basis="grounded",
            principle_ids=["private-principle"],
            evidence_ids=["public-evidence"],
            memory_ids=["private-memory"],
            tool_artifact_ids=["private-tool-artifact"],
        )
    return Contribution(
        preferred_option_id=preferred,
        option_scores={
            "go": 0.8 if preferred == "go" else 0.4,
            "stop": 0.4 if preferred == "go" else 0.8,
        },
        confidence=0.7,
        claims=[claim],
    )


class FakeContext:
    def __init__(self, organization, *, sensitive_provenance: bool = False) -> None:
        self.organization = organization
        self.sensitive_provenance = sensitive_provenance
        self.waves: list[list[ProtocolTask]] = []

    async def invoke_wave(self, tasks: list[ProtocolTask]) -> list[TaskResult]:
        self.waves.append(tasks)
        results = []
        for index, task in enumerate(tasks):
            if task.response_model is Contribution:
                value = contribution("go", sensitive_provenance=self.sensitive_provenance)
            elif task.response_model is RedTeamCritique:
                value = RedTeamCritique(
                    attacks=[
                        {
                            "attack_id": "risk-one",
                            "option_id": "go",
                            "severity": "high",
                            "claim": "pilot missing",
                            "evidence_ids": ["public-evidence"]
                            if self.sensitive_provenance
                            else [],
                            "tool_artifact_ids": ["private-tool-artifact"]
                            if self.sensitive_provenance
                            else [],
                        }
                    ],
                    strongest_alternative_id="stop",
                )
            elif task.response_model is RedTeamRebuttal:
                value = RedTeamRebuttal(
                    revised_ballot=contribution(
                        "go", sensitive_provenance=self.sensitive_provenance
                    ),
                    dispositions=[
                        {
                            "attack_id": "risk-one",
                            "status": "mitigated",
                            "response": "run pilot first",
                        }
                    ],
                )
            elif task.response_model is ExecutiveDecision:
                value = ExecutiveDecision(
                    ballot=contribution("go", sensitive_provenance=self.sensitive_provenance),
                    override_reason="",
                )
            else:
                raise AssertionError(task.response_model)
            results.append(
                TaskResult(task=task, value=value, message_id=f"message-{len(self.waves)}-{index}")
            )
        return results


def test_roundtable_is_independent_then_anonymous() -> None:
    context = FakeContext(Catalog.builtin().organization("product-roundtable"))
    outcome = asyncio.run(RoundtableProtocol().run(context))
    assert len(context.waves) == 2
    assert all(not task.shared_context for task in context.waves[0])
    assert all("anonymous_first_round" in task.shared_context for task in context.waves[1])
    assert set(outcome.ballots) == {"steve-jobs", "charlie-munger", "donella-meadows"}


def test_roundtable_does_not_decide_from_stale_first_round_ballots() -> None:
    class FailedRevisionContext(FakeContext):
        async def invoke_wave(self, tasks: list[ProtocolTask]) -> list[TaskResult]:
            if tasks and tasks[0].phase == "revised_ballot":
                self.waves.append(tasks)
                return [
                    TaskResult(task=task, value=None, error="invalid revision") for task in tasks
                ]
            return await super().invoke_wave(tasks)

    context = FailedRevisionContext(Catalog.builtin().organization("product-roundtable"))
    outcome = asyncio.run(RoundtableProtocol().run(context))

    assert outcome.ballots == {}
    assert outcome.successful_member_ids == set()
    assert outcome.status_hint == "inconclusive"


def test_red_team_keeps_roles_distinct() -> None:
    context = FakeContext(Catalog.builtin().organization("launch-red-team"))
    outcome = asyncio.run(RedTeamProtocol().run(context))
    assert [wave[0].phase for wave in context.waves] == [
        "blue_proposal",
        "red_critique",
        "blue_rebuttal",
        "judge_ballot",
    ]
    assert set(outcome.ballots) == {"donella-meadows"}
    assert outcome.successful_member_ids == {"steve-jobs", "charlie-munger", "donella-meadows"}
    assert outcome.forced_objections == []


def test_cabinet_requires_explicit_override_reason() -> None:
    context = FakeContext(Catalog.builtin().organization("strategy-cabinet"))
    outcome = asyncio.run(CabinetProtocol().run(context))
    assert len(context.waves[0]) == 2
    assert context.waves[1][0].member_id == "steve-jobs"
    assert outcome.chair_override_option_id is None


@pytest.mark.parametrize(
    ("organization_id", "protocol"),
    [
        ("product-roundtable", RoundtableProtocol()),
        ("launch-red-team", RedTeamProtocol()),
        ("strategy-cabinet", CabinetProtocol()),
    ],
)
def test_max_rounds_one_never_enters_a_second_round(
    organization_id: str,
    protocol,
) -> None:
    organization = (
        Catalog.builtin().organization(organization_id).model_copy(update={"max_rounds": 1})
    )
    context = FakeContext(organization)

    outcome = asyncio.run(protocol.run(context))

    assert len(context.waves) == 1
    assert all(task.round == 1 for task in context.waves[0])
    assert outcome.status_hint == "inconclusive"
    assert outcome.ballots == {}


def test_red_team_rejects_duplicate_attack_ids_across_critics() -> None:
    source = Catalog.builtin().organization("launch-red-team")
    organization = source.model_copy(
        update={
            "id": "duplicate-attack-red-team",
            "members": [
                *source.members,
                OrganizationMember(
                    hsa_id="critic-two",
                    role="second-red-critic",
                    correlation_group="hermes-default",
                ),
            ],
        }
    )
    context = FakeContext(organization)

    outcome = asyncio.run(RedTeamProtocol().run(context))

    assert [wave[0].phase for wave in context.waves] == ["blue_proposal", "red_critique"]
    assert len(context.waves[1]) == 2
    assert outcome.status_hint == "inconclusive"
    assert outcome.ballots == {}


@pytest.mark.parametrize(
    ("organization_id", "protocol"),
    [
        ("product-roundtable", RoundtableProtocol()),
        ("launch-red-team", RedTeamProtocol()),
        ("strategy-cabinet", CabinetProtocol()),
    ],
)
def test_cross_hsa_public_projections_strip_private_provenance(
    organization_id: str,
    protocol,
) -> None:
    context = FakeContext(
        Catalog.builtin().organization(organization_id),
        sensitive_provenance=True,
    )

    asyncio.run(protocol.run(context))

    shared_contexts = [
        task.shared_context for wave in context.waves for task in wave if task.shared_context
    ]
    assert shared_contexts
    keys = set().union(*(_nested_keys(value) for value in shared_contexts))
    assert keys.isdisjoint({"principle_ids", "memory_ids", "tool_artifact_ids"})


def _nested_keys(value) -> set[str]:
    if isinstance(value, dict):
        return set(value).union(*(_nested_keys(item) for item in value.values()), set())
    if isinstance(value, list):
        return set().union(*(_nested_keys(item) for item in value), set())
    return set()
