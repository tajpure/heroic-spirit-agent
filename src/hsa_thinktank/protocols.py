"""Deterministic organization topologies; Hermes only executes leaf turns."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import BaseModel

from .models import (
    Attack,
    Contribution,
    ExecutiveDecision,
    Objection,
    OrganizationSpec,
    RedTeamCritique,
    RedTeamRebuttal,
    content_hash,
)


@dataclass(frozen=True)
class ProtocolTask:
    member_id: str
    phase: str
    round: int
    response_model: type[BaseModel]
    instruction: str
    shared_context: dict[str, Any] = field(default_factory=dict)
    kind: str = "contribution"
    visibility: str = "private"
    parent_message_ids: tuple[str, ...] = ()


@dataclass
class TaskResult:
    task: ProtocolTask
    value: BaseModel | None
    message_id: str | None = None
    error: str | None = None


@dataclass
class ProtocolOutcome:
    ballots: dict[str, Contribution]
    successful_member_ids: set[str]
    forced_objections: list[Objection] = field(default_factory=list)
    chair_override_option_id: str | None = None
    chair_override_reason: str = ""
    status_hint: str | None = None


class ProtocolContext(Protocol):
    organization: OrganizationSpec

    async def invoke_wave(self, tasks: list[ProtocolTask]) -> list[TaskResult]: ...


class OrganizationProtocol(Protocol):
    name: str

    async def run(self, context: ProtocolContext) -> ProtocolOutcome: ...


class RoundtableProtocol:
    name = "roundtable"

    async def run(self, context: ProtocolContext) -> ProtocolOutcome:
        organization = context.organization
        first_tasks = [
            ProtocolTask(
                member_id=member.hsa_id,
                phase="independent_ballot",
                round=1,
                response_model=Contribution,
                instruction=(
                    "在看不到其他成员观点的前提下独立评估全部冻结方案。使用可用记忆与工具，"
                    "但不要假定记忆或检索结果自动可信。提交完整评分、依据、风险和下一步。"
                ),
            )
            for member in organization.members
        ]
        first = await context.invoke_wave(first_tasks)
        first_valid = {
            result.task.member_id: result for result in first if result.value is not None
        }
        if organization.max_rounds < 2:
            return ProtocolOutcome(
                ballots={},
                successful_member_ids=set(first_valid),
                status_hint="inconclusive",
            )
        anonymous_results = sorted(
            first_valid.values(),
            key=lambda result: content_hash(result.value),
        )
        digest = [
            {
                "anonymous_member": f"member-{index + 1}",
                "ballot": _public_contribution_projection(result.value),
            }
            for index, result in enumerate(anonymous_results)
        ]
        parent_ids = tuple(
            result.message_id for result in first_valid.values() if result.message_id is not None
        )
        second_tasks = [
            ProtocolTask(
                member_id=member.hsa_id,
                phase="revised_ballot",
                round=2,
                response_model=Contribution,
                instruction=(
                    "审阅匿名首轮意见，指出最重要的分歧后重新提交完整评分。不得根据人物名气"
                    "改变判断；只有新证据或更强论证才应改变分数。"
                ),
                shared_context={"anonymous_first_round": digest},
                kind="revised_ballot",
                parent_message_ids=parent_ids,
            )
            for member in organization.members
            if member.hsa_id in first_valid
        ]
        second = await context.invoke_wave(second_tasks)
        revised = {
            result.task.member_id: result.value
            for result in second
            if isinstance(result.value, Contribution)
        }
        return ProtocolOutcome(
            ballots=revised,
            successful_member_ids=set(revised),
            status_hint=("inconclusive" if len(revised) < organization.min_quorum else None),
        )


class RedTeamProtocol:
    name = "red_team"

    async def run(self, context: ProtocolContext) -> ProtocolOutcome:
        organization = context.organization
        proposer_id = organization.chair_id
        judge_ids = set(organization.judge_ids)
        critic_ids = [
            member.hsa_id
            for member in organization.members
            if member.hsa_id != proposer_id and member.hsa_id not in judge_ids
        ]
        proposal_result = (
            await context.invoke_wave(
                [
                    ProtocolTask(
                        member_id=proposer_id,
                        phase="blue_proposal",
                        round=1,
                        response_model=Contribution,
                        instruction=(
                            "作为蓝队提出首选方案及可执行论证，同时诚实列出薄弱假设。"
                            "对全部方案评分，不得只为首选方案辩护。"
                        ),
                        kind="proposal",
                    )
                ]
            )
        )[0]
        if not isinstance(proposal_result.value, Contribution):
            return ProtocolOutcome({}, set(), status_hint="inconclusive")
        if organization.max_rounds < 2:
            return ProtocolOutcome({}, {proposer_id}, status_hint="inconclusive")

        critiques = await context.invoke_wave(
            [
                ProtocolTask(
                    member_id=critic_id,
                    phase="red_critique",
                    round=1,
                    response_model=RedTeamCritique,
                    instruction=(
                        "作为红队按失败模式攻击蓝队方案。critical 只用于违反硬约束或可能造成"
                        "不可接受损失的风险；每个攻击必须说明需要什么证据或缓解措施。"
                    ),
                    shared_context={
                        "blue_proposal": _public_contribution_projection(proposal_result.value)
                    },
                    kind="critique",
                    parent_message_ids=(proposal_result.message_id,)
                    if proposal_result.message_id
                    else (),
                )
                for critic_id in critic_ids
            ]
        )
        valid_critiques = [
            result for result in critiques if isinstance(result.value, RedTeamCritique)
        ]
        if not valid_critiques:
            return ProtocolOutcome(
                {},
                {proposer_id},
                status_hint="inconclusive",
            )
        attacks = [attack for result in valid_critiques for attack in result.value.attacks]
        attack_ids = [attack.attack_id for attack in attacks]
        if len(attack_ids) != len(set(attack_ids)):
            return ProtocolOutcome(
                {},
                {
                    proposer_id,
                    *(result.task.member_id for result in valid_critiques),
                },
                status_hint="inconclusive",
            )
        critique_message_ids = tuple(
            result.message_id for result in valid_critiques if result.message_id is not None
        )
        rebuttal_result = (
            await context.invoke_wave(
                [
                    ProtocolTask(
                        member_id=proposer_id,
                        phase="blue_rebuttal",
                        round=2,
                        response_model=RedTeamRebuttal,
                        instruction=(
                            "逐项处理红队攻击，只能标为 accepted、mitigated、rejected 或 unresolved。"
                            "随后提交修订后的完整评分；不得假装未解决风险已经消失。"
                        ),
                        shared_context={
                            "proposal": _public_contribution_projection(proposal_result.value),
                            "attacks": [_public_attack_projection(attack) for attack in attacks],
                        },
                        kind="rebuttal",
                        parent_message_ids=critique_message_ids,
                    )
                ]
            )
        )[0]
        if not isinstance(rebuttal_result.value, RedTeamRebuttal):
            return ProtocolOutcome(
                {},
                {
                    proposer_id,
                    *(result.task.member_id for result in valid_critiques),
                },
                status_hint="inconclusive",
            )

        disposition_by_id = {
            disposition.attack_id: disposition for disposition in rebuttal_result.value.dispositions
        }
        unresolved = _unresolved_attacks(attacks, disposition_by_id)
        judge_context = {
            "revised_blue_ballot": _public_contribution_projection(
                rebuttal_result.value.revised_ballot
            ),
            "attacks": [_public_attack_projection(attack) for attack in attacks],
            "dispositions": [
                disposition.model_dump(mode="json")
                for disposition in rebuttal_result.value.dispositions
            ],
        }
        judge_results = await context.invoke_wave(
            [
                ProtocolTask(
                    member_id=judge_id,
                    phase="judge_ballot",
                    round=2,
                    response_model=Contribution,
                    instruction=(
                        "作为独立裁判依据冻结准则评估所有方案。红队提出风险不等于自动否决，"
                        "但未解决 critical attack 必须保留。提交最终完整评分。"
                    ),
                    shared_context=judge_context,
                    kind="judge_ballot",
                    parent_message_ids=(rebuttal_result.message_id,)
                    if rebuttal_result.message_id
                    else (),
                )
                for judge_id in organization.judge_ids
            ]
        )
        ballots = {
            result.task.member_id: result.value
            for result in judge_results
            if isinstance(result.value, Contribution)
        }
        successful = {proposer_id}
        successful.update(result.task.member_id for result in valid_critiques)
        successful.update(ballots)
        status_hint = (
            "rejected" if any(item.severity == "critical" for item in unresolved) else None
        )
        return ProtocolOutcome(
            ballots=ballots,
            successful_member_ids=successful,
            forced_objections=unresolved,
            status_hint=status_hint,
        )


class CabinetProtocol:
    name = "cabinet"

    async def run(self, context: ProtocolContext) -> ProtocolOutcome:
        organization = context.organization
        advisers = [
            member for member in organization.members if member.hsa_id != organization.chair_id
        ]
        memo_results = await context.invoke_wave(
            [
                ProtocolTask(
                    member_id=member.hsa_id,
                    phase="portfolio_memo",
                    round=1,
                    response_model=Contribution,
                    instruction=(
                        f"以 {member.role} 职责提交领域 memo 和全部方案评分。只在预先声明的硬约束"
                        "上提出 critical objection，不得临时扩张否决权。"
                    ),
                    kind="portfolio_memo",
                )
                for member in advisers
            ]
        )
        valid_memos = [result for result in memo_results if isinstance(result.value, Contribution)]
        if organization.max_rounds < 2:
            return ProtocolOutcome(
                {},
                {result.task.member_id for result in valid_memos},
                status_hint="inconclusive",
            )
        parent_ids = tuple(
            result.message_id for result in valid_memos if result.message_id is not None
        )
        executive_result = (
            await context.invoke_wave(
                [
                    ProtocolTask(
                        member_id=organization.chair_id,
                        phase="executive_ballot",
                        round=2,
                        response_model=ExecutiveDecision,
                        instruction=(
                            "综合各领域 memo 后提交自己的完整评分。若要覆盖按组织权重计算的结果，"
                            "必须填写 override_reason；否则留空。覆盖不能绕过硬约束或人工风险门。"
                        ),
                        shared_context={
                            "portfolio_memos": [
                                {
                                    "portfolio": result.task.member_id,
                                    "memo": _public_contribution_projection(result.value),
                                }
                                for result in valid_memos
                            ]
                        },
                        kind="executive_ballot",
                        parent_message_ids=parent_ids,
                    )
                ]
            )
        )[0]
        ballots = {
            result.task.member_id: result.value
            for result in valid_memos
            if isinstance(result.value, Contribution)
        }
        override_option = None
        override_reason = ""
        if isinstance(executive_result.value, ExecutiveDecision):
            ballots[organization.chair_id] = executive_result.value.ballot
            if organization.allow_chair_override and executive_result.value.override_reason.strip():
                override_option = executive_result.value.ballot.preferred_option_id
                override_reason = executive_result.value.override_reason.strip()
        successful = set(ballots)
        return ProtocolOutcome(
            ballots=ballots,
            successful_member_ids=successful,
            chair_override_option_id=override_option,
            chair_override_reason=override_reason,
        )


def protocol_for(name: str) -> OrganizationProtocol:
    protocols: dict[str, OrganizationProtocol] = {
        "roundtable": RoundtableProtocol(),
        "red_team": RedTeamProtocol(),
        "cabinet": CabinetProtocol(),
    }
    try:
        return protocols[name]
    except KeyError as exc:
        raise ValueError(f"unknown organization protocol: {name}") from exc


def _unresolved_attacks(attacks: list[Attack], dispositions: dict[str, Any]) -> list[Objection]:
    unresolved: list[Objection] = []
    for attack in attacks:
        disposition = dispositions.get(attack.attack_id)
        is_resolved = disposition is not None and disposition.status in {"mitigated", "rejected"}
        if not is_resolved:
            unresolved.append(
                Objection(
                    option_id=attack.option_id,
                    severity=attack.severity,
                    statement=attack.claim,
                    mitigation=attack.suggested_mitigation,
                    resolved=False,
                )
            )
    return unresolved


def _public_contribution_projection(contribution: Contribution) -> dict[str, Any]:
    """Return the only ballot fields another HSA is allowed to observe."""

    return {
        "preferred_option_id": contribution.preferred_option_id,
        "option_scores": contribution.option_scores,
        "criterion_scores": contribution.criterion_scores,
        "constraint_results": contribution.constraint_results,
        "confidence": contribution.confidence,
        "claims": [
            {
                "claim": claim.claim,
                "basis": claim.basis,
                "evidence_ids": claim.evidence_ids,
            }
            for claim in contribution.claims
        ],
        "assumptions": contribution.assumptions,
        "risks": [
            {
                "option_id": risk.option_id,
                "severity": risk.severity,
                "statement": risk.statement,
                "mitigation": risk.mitigation,
                "resolved": risk.resolved,
            }
            for risk in contribution.risks
        ],
        "next_actions": contribution.next_actions,
    }


def _public_attack_projection(attack: Attack) -> dict[str, Any]:
    return {
        "attack_id": attack.attack_id,
        "option_id": attack.option_id,
        "severity": attack.severity,
        "claim": attack.claim,
        "evidence_needed": attack.evidence_needed,
        "suggested_mitigation": attack.suggested_mitigation,
        "evidence_ids": attack.evidence_ids,
    }
