"""Code-owned aggregation, correlation discount and risk gates."""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass

from .models import (
    DecisionProblem,
    Objection,
    OrganizationSpec,
    RationaleClaim,
    validate_option_references,
)
from .protocols import ProtocolOutcome


@dataclass(frozen=True)
class AggregatedDecision:
    status: str
    status_reason: str
    selected_option_id: str | None
    option_scores: dict[str, float]
    confidence: float
    effective_sample_size: float
    correlation_group_count: int
    rationale_claims: list[RationaleClaim]
    assumptions: list[str]
    unresolved_risks: list[str]
    dissent: list[str]
    next_actions: list[str]
    memory_ids: list[str]
    tool_artifact_ids: list[str]
    chair_override: dict[str, str] | None


def aggregate(
    problem: DecisionProblem,
    organization: OrganizationSpec,
    outcome: ProtocolOutcome,
) -> AggregatedDecision:
    option_ids = [option.id for option in problem.options]
    if len(outcome.successful_member_ids) < organization.min_quorum:
        return _empty(
            "inconclusive",
            f"quorum not met: {len(outcome.successful_member_ids)}/{organization.min_quorum}",
            option_ids,
        )
    if not outcome.ballots:
        return _empty("inconclusive", "no valid final ballots", option_ids)

    criterion_ids = {criterion.id for criterion in problem.criteria}
    hard_constraint_ids = {
        criterion.id for criterion in problem.criteria if criterion.hard_constraint
    }
    for member_id, ballot in outcome.ballots.items():
        try:
            validate_option_references(
                ballot,
                set(option_ids),
                criterion_ids,
                hard_constraint_ids,
            )
        except ValueError as exc:
            return _empty(
                "inconclusive",
                f"invalid final ballot from {member_id}: {exc}",
                option_ids,
            )

    group_sizes = Counter(
        organization.member(member_id).correlation_group for member_id in outcome.ballots
    )
    effective_weights = {
        member_id: organization.member(member_id).weight
        / group_sizes[organization.member(member_id).correlation_group]
        for member_id in outcome.ballots
    }
    total_weight = sum(effective_weights.values())
    scores: dict[str, float] = {}
    for option_id in option_ids:
        weighted = sum(
            effective_weights[member_id] * ballot.option_scores[option_id]
            for member_id, ballot in outcome.ballots.items()
        )
        scores[option_id] = round(weighted / total_weight, 6)

    hard_failures = {
        option_id: [
            f"{member_id}:{constraint_id}"
            for member_id, ballot in outcome.ballots.items()
            for constraint_id in hard_constraint_ids
            if not ballot.constraint_results.get(option_id, {}).get(constraint_id, False)
        ]
        for option_id in option_ids
    }
    feasible_option_ids = [option_id for option_id in option_ids if not hard_failures[option_id]]
    if not feasible_option_ids:
        failures = [
            f"{option_id} violates {', '.join(values)}"
            for option_id, values in hard_failures.items()
            if values
        ]
        return _empty(
            "rejected",
            "all options fail at least one hard constraint",
            option_ids,
            unresolved_risks=failures,
        )

    ranked = sorted(
        feasible_option_ids,
        key=lambda item: (-scores[item], option_ids.index(item)),
    )
    selected = ranked[0]
    original_selected = selected
    margin = scores[ranked[0]] - scores[ranked[1]] if len(ranked) > 1 else 1.0

    critical = [
        risk
        for risk in _all_objections(outcome)
        if risk.option_id == selected and risk.severity == "critical" and not risk.resolved
    ]
    chair_override = None
    override_violates_hard_constraint = False
    if outcome.chair_override_option_id and outcome.chair_override_option_id != selected:
        chair_override = {
            "aggregated_option_id": original_selected,
            "override_option_id": outcome.chair_override_option_id,
            "reason": outcome.chair_override_reason,
        }
        if outcome.chair_override_option_id in feasible_option_ids:
            selected = outcome.chair_override_option_id
        else:
            override_violates_hard_constraint = True

    if outcome.status_hint == "budget_exhausted":
        status, reason = "budget_exhausted", "protocol invocation budget exhausted"
    elif critical and organization.protocol == "red_team":
        status, reason = "rejected", "unresolved critical red-team attack"
    elif critical:
        status, reason = "needs_human", "unresolved critical objection"
    elif override_violates_hard_constraint:
        status, reason = "needs_human", "chair override violates a hard constraint"
    elif margin < organization.min_margin:
        status, reason = "inconclusive", f"score margin {margin:.3f} below threshold"
    elif chair_override is not None:
        status, reason = "needs_human", "chair requested an override"
    elif problem.risk_tier == "high":
        status, reason = "needs_human", "high-risk decision requires human approval"
    else:
        status, reason = "decided", "quorum, margin and risk gates passed"

    raw_n = len(outcome.ballots)
    group_totals = defaultdict(float)
    for member_id in outcome.ballots:
        member = organization.member(member_id)
        group_totals[member.correlation_group] += member.weight
    denominator = sum(value * value for value in group_totals.values())
    effective_sample_size = (sum(group_totals.values()) ** 2) / denominator if denominator else 0.0
    agreement = (
        sum(
            effective_weights[member_id]
            for member_id, ballot in outcome.ballots.items()
            if ballot.preferred_option_id == selected
        )
        / total_weight
    )
    mean_confidence = (
        sum(
            effective_weights[member_id] * ballot.confidence
            for member_id, ballot in outcome.ballots.items()
        )
        / total_weight
    )
    correlation_factor = 0.5 + 0.5 * math.sqrt(min(1.0, effective_sample_size / raw_n))
    confidence = round(mean_confidence * (0.5 + 0.5 * agreement) * correlation_factor, 6)

    supporting = [
        ballot for ballot in outcome.ballots.values() if ballot.preferred_option_id == selected
    ]
    if not supporting and status not in {"rejected", "budget_exhausted"}:
        status = "inconclusive"
        reason = "aggregate compromise has no direct member support"
    claims = _unique_claims(claim for ballot in supporting for claim in ballot.claims)
    assumptions = _unique_text(
        item for ballot in outcome.ballots.values() for item in ballot.assumptions
    )
    next_actions = _unique_text(item for ballot in supporting for item in ballot.next_actions)
    risks = _unique_text(
        f"{risk.severity}: {risk.statement}"
        for risk in _all_objections(outcome)
        if risk.option_id == selected and not risk.resolved
    )
    dissent = [
        f"{member_id} prefers {ballot.preferred_option_id}: {ballot.claims[0].claim}"
        for member_id, ballot in outcome.ballots.items()
        if ballot.preferred_option_id != selected
    ]
    memory_ids = sorted({item for claim in claims for item in claim.memory_ids})
    artifact_ids = sorted({item for claim in claims for item in claim.tool_artifact_ids})
    return AggregatedDecision(
        status=status,
        status_reason=reason,
        selected_option_id=selected,
        option_scores=scores,
        confidence=confidence,
        effective_sample_size=round(effective_sample_size, 6),
        correlation_group_count=len(group_totals),
        rationale_claims=claims[:12],
        assumptions=assumptions[:12],
        unresolved_risks=risks[:16],
        dissent=dissent,
        next_actions=next_actions[:12],
        memory_ids=memory_ids,
        tool_artifact_ids=artifact_ids,
        chair_override=chair_override,
    )


def _all_objections(outcome: ProtocolOutcome) -> list[Objection]:
    return outcome.forced_objections + [
        risk for ballot in outcome.ballots.values() for risk in ballot.risks
    ]


def _unique_text(values) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = value.strip()
        if normalized and normalized not in seen:
            seen.add(normalized)
            result.append(normalized)
    return result


def _unique_claims(values) -> list[RationaleClaim]:
    seen: set[str] = set()
    result: list[RationaleClaim] = []
    for value in values:
        if value.claim not in seen:
            seen.add(value.claim)
            result.append(value)
    return result


def _empty(
    status: str,
    reason: str,
    option_ids: list[str],
    *,
    unresolved_risks: list[str] | None = None,
) -> AggregatedDecision:
    return AggregatedDecision(
        status=status,
        status_reason=reason,
        selected_option_id=None,
        option_scores={option_id: 0.0 for option_id in option_ids},
        confidence=0.0,
        effective_sample_size=0.0,
        correlation_group_count=0,
        rationale_claims=[],
        assumptions=[],
        unresolved_risks=unresolved_risks or [],
        dissent=[],
        next_actions=[],
        memory_ids=[],
        tool_artifact_ids=[],
        chair_override=None,
    )
