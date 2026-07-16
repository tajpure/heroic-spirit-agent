"""Deterministic, auditable routing from a decision context to an HSA meeting."""

from __future__ import annotations

import re
from collections.abc import Iterable

from .catalog import Catalog
from .models import (
    DecisionProblem,
    HSAProfile,
    MeetingSelection,
    OrganizationSpec,
    content_hash,
)


AUTO_ORGANIZATION_ID = "auto"
ROUTER_VERSION = "domain-signal-v1"


_SIGNAL_RULES: dict[str, dict[str, tuple[str, ...]]] = {
    "product": {
        "terms": (
            "产品",
            "用户",
            "体验",
            "设计",
            "功能",
            "路线图",
            "品牌",
            "product",
            "user",
            "ux",
            "design",
            "feature",
            "roadmap",
        ),
        "domains": (
            "product",
            "user_experience",
            "design_quality",
            "creative_work",
            "platform_integration",
        ),
    },
    "innovation": {
        "terms": (
            "创新",
            "创意",
            "愿景",
            "新业务",
            "创业",
            "innovation",
            "creative",
            "vision",
            "startup",
        ),
        "domains": ("innovation", "creative", "leadership", "company_direction"),
    },
    "capital": {
        "terms": (
            "投资",
            "资本",
            "预算",
            "成本",
            "收益",
            "定价",
            "商业模式",
            "并购",
            "现金流",
            "investment",
            "capital",
            "budget",
            "cost",
            "revenue",
            "pricing",
            "cash flow",
        ),
        "domains": ("investment", "capital_allocation", "business_strategy"),
    },
    "risk": {
        "terms": (
            "风险",
            "失败",
            "损失",
            "合规",
            "安全",
            "不可逆",
            "审计",
            "激励",
            "偏差",
            "risk",
            "failure",
            "loss",
            "compliance",
            "safety",
            "incentive",
            "bias",
        ),
        "domains": (
            "risk",
            "decision_analysis",
            "expert_judgment",
            "governance",
            "negotiation",
        ),
    },
    "systems": {
        "terms": (
            "系统",
            "反馈",
            "延迟",
            "杠杆",
            "生态",
            "外部性",
            "复杂",
            "长期影响",
            "system",
            "feedback",
            "delay",
            "leverage",
            "ecosystem",
            "externality",
            "complex",
        ),
        "domains": ("systems", "system_", "complex_systems", "adaptive_management"),
    },
    "organization": {
        "terms": (
            "组织",
            "团队",
            "治理",
            "流程",
            "协作",
            "招聘",
            "管理",
            "organization",
            "team",
            "governance",
            "process",
            "hiring",
            "management",
        ),
        "domains": ("organizational", "governance", "leadership", "negotiation"),
    },
    "operations": {
        "terms": (
            "运营",
            "交付",
            "效率",
            "扩容",
            "供应链",
            "可靠性",
            "operations",
            "delivery",
            "efficiency",
            "scale",
            "reliability",
        ),
        "domains": ("operations", "experimentation", "prioritization"),
    },
    "policy": {
        "terms": (
            "政策",
            "公共",
            "监管",
            "社会",
            "环境",
            "公平",
            "policy",
            "regulation",
            "public",
            "social",
            "environment",
            "equity",
        ),
        "domains": ("public_policy", "policy_design", "governance", "system_change"),
    },
    "strategy": {
        "terms": (
            "战略",
            "方向",
            "优先级",
            "资源配置",
            "组合",
            "长期",
            "strategy",
            "priority",
            "allocation",
            "portfolio",
            "long-term",
        ),
        "domains": ("strategy", "prioritization", "portfolio", "organizational_focus"),
    },
}

_RELEASE_TERMS = (
    "上线",
    "发布",
    "投产",
    "部署",
    "迁移",
    "切换",
    "launch",
    "release",
    "ship",
    "deploy",
    "migration",
    "rollout",
)

_ROUTER_POLICY_HASH = content_hash(
    {
        "router_version": ROUTER_VERSION,
        "signal_rules": _SIGNAL_RULES,
        "release_terms": _RELEASE_TERMS,
        "maximum_desired_hsas": 3,
        "minimum_meeting_hsas": 2,
        "high_risk_protocol": "red_team",
    }
)


class MeetingRouter:
    """Choose a base organization and effective roster without invoking a model."""

    def __init__(self, catalog: Catalog) -> None:
        self.catalog = catalog

    def select(
        self,
        problem: DecisionProblem,
        *,
        requested_organization_id: str | None = None,
    ) -> MeetingSelection:
        text = _problem_text(problem)
        matched_signals = tuple(
            signal for signal, rule in _SIGNAL_RULES.items() if _contains_any(text, rule["terms"])
        )
        scores = {
            profile_id: _profile_score(profile, matched_signals)
            for profile_id, profile in self.catalog.profiles.items()
        }
        rendered_scores = {key: round(value, 3) for key, value in sorted(scores.items())}

        if requested_organization_id not in {None, AUTO_ORGANIZATION_ID}:
            organization = self.catalog.organization(requested_organization_id)
            return _selection(
                organization,
                effective_organization=organization,
                problem=problem,
                mode="explicit",
                scores=rendered_scores,
                matched_signals=matched_signals,
                reasons=[
                    f"用户显式指定组织 {organization.id}；自动路由只记录上下文评分，不覆盖该选择。"
                ],
            )

        desired_hsa_ids = _desired_hsa_ids(
            scores,
            matched_signals=matched_signals,
            risk_tier=problem.risk_tier,
        )
        desired_protocol = _desired_protocol(
            problem,
            text=text,
            matched_signals=matched_signals,
        )
        organization = _choose_base_organization(
            self.catalog.organizations.values(),
            desired_hsa_ids=desired_hsa_ids,
            desired_protocol=desired_protocol,
            scores=scores,
        )
        effective_organization = _effective_organization(
            organization,
            desired_hsa_ids=desired_hsa_ids,
            scores=scores,
        )
        used_fallback = {
            member.hsa_id for member in effective_organization.members
        } != desired_hsa_ids
        signal_reason = (
            f"识别到上下文信号：{', '.join(matched_signals)}。"
            if matched_signals
            else "未识别到足够明确的领域信号，因此扩大会议覆盖面。"
        )
        score_reason = "HSA 领域相关度：" + ", ".join(
            f"{hsa_id}={score:g}" for hsa_id, score in rendered_scores.items()
        )
        protocol_reason = {
            "red_team": "高风险或发布型上下文需要红蓝对抗与独立裁判。",
            "cabinet": "跨系统、组织、资本或长期战略信号需要多领域内阁。",
            "roundtable": "问题边界较集中，采用相关 HSA 的双人或三人圆桌。",
        }[organization.protocol]
        reasons = [signal_reason, score_reason, protocol_reason]
        if used_fallback:
            reasons.append("目录中没有完全匹配的成员组合，已选择覆盖目标 HSA 最完整的现有组织。")
        return _selection(
            organization,
            effective_organization=effective_organization,
            problem=problem,
            mode="auto",
            scores=rendered_scores,
            matched_signals=matched_signals,
            reasons=reasons,
        )


def validate_selection_against_catalog(
    catalog: Catalog,
    selection: MeetingSelection,
) -> OrganizationSpec:
    """Validate a persisted effective roster without rerunning a newer router."""

    base = catalog.organization(selection.organization_id)
    if base.fingerprint != selection.organization_fingerprint:
        raise ValueError("catalog organization changed after meeting selection")
    if selection.mode == "auto" and not base.auto_selectable:
        raise ValueError("base organization is no longer auto-selectable")
    effective = selection.effective_organization
    fixed_fields = (
        "id",
        "protocol",
        "version",
        "min_margin",
        "allow_chair_override",
        "auto_selectable",
        "memory_policy",
        "tool_policy_id",
        "max_rounds",
        "max_invocations",
    )
    if any(getattr(effective, field) != getattr(base, field) for field in fixed_fields):
        raise ValueError("effective organization changes a non-routing base policy")
    base_members = {member.hsa_id: member for member in base.members}
    for member in effective.members:
        if base_members.get(member.hsa_id) != member:
            raise ValueError("effective organization contains a modified or foreign HSA member")
    expected_quorum = min(len(effective.members), max(2, base.min_quorum))
    if selection.mode == "auto" and effective.min_quorum != expected_quorum:
        raise ValueError("effective organization quorum does not match routing policy")
    selected = set(selection.selected_hsa_ids)
    if selection.mode == "auto":
        if base.protocol == "red_team" and selected != set(base_members):
            raise ValueError("red-team routing must preserve proposer, critic and judge roles")
        expected_judges = [judge_id for judge_id in base.judge_ids if judge_id in selected]
        if effective.judge_ids != expected_judges:
            raise ValueError("effective organization judges do not match the selected roster")
        expected_chair = (
            base.chair_id
            if base.chair_id in selected
            else sorted(
                selected,
                key=lambda hsa_id: (-selection.hsa_scores.get(hsa_id, 0.0), hsa_id),
            )[0]
        )
        if effective.chair_id != expected_chair:
            raise ValueError("effective organization chair does not match routing scores")
    if selection.mode == "explicit" and effective != base:
        raise ValueError("explicit meeting selection must preserve the full organization")
    return base


def _problem_text(problem: DecisionProblem) -> str:
    values = [problem.question, problem.context, *problem.constraints]
    values.extend(option.description for option in problem.options)
    for evidence in problem.evidence:
        # Evidence bodies may quote unrelated or adversarial text. Routing uses
        # the user-controlled title while the meeting itself still receives the
        # full frozen evidence item.
        values.append(evidence.title)
    return "\n".join(value for value in values if value).casefold()


def _contains_any(text: str, terms: Iterable[str]) -> bool:
    return any(_contains_term(text, term.casefold()) for term in terms)


def _contains_term(text: str, term: str) -> bool:
    if re.fullmatch(r"[a-z0-9][a-z0-9 -]*", term):
        return re.search(rf"(?<![a-z0-9]){re.escape(term)}(?![a-z0-9])", text) is not None
    return term in text


def _profile_score(profile: HSAProfile, matched_signals: Iterable[str]) -> float:
    domains = {
        domain.casefold() for principle in profile.principles for domain in principle.domains
    }
    score = 0.0
    for signal in matched_signals:
        fragments = _SIGNAL_RULES[signal]["domains"]
        matches = sum(1 for domain in domains if any(fragment in domain for fragment in fragments))
        if matches:
            score += 1.0 + min(matches, 4) * 0.5
    return score


def _desired_hsa_ids(
    scores: dict[str, float],
    *,
    matched_signals: tuple[str, ...],
    risk_tier: str,
) -> set[str]:
    ranked = sorted(scores, key=lambda hsa_id: (-scores[hsa_id], hsa_id))
    maximum = min(3, len(ranked))
    if risk_tier == "high" or not matched_signals:
        return set(ranked[:maximum])
    count = 2
    ambiguous_top = maximum >= 3 and scores[ranked[0]] == scores[ranked[2]]
    if maximum >= 3 and scores[ranked[2]] > 0 and (len(matched_signals) >= 3 or ambiguous_top):
        count = 3
    return set(ranked[: min(count, maximum)])


def _desired_protocol(
    problem: DecisionProblem,
    *,
    text: str,
    matched_signals: tuple[str, ...],
) -> str:
    if problem.risk_tier == "high" or _contains_any(text, _RELEASE_TERMS):
        return "red_team"
    strategic = {"systems", "policy", "organization", "capital", "strategy"}
    if len(strategic.intersection(matched_signals)) >= 2:
        return "cabinet"
    return "roundtable"


def _choose_base_organization(
    organizations: Iterable[OrganizationSpec],
    *,
    desired_hsa_ids: set[str],
    desired_protocol: str,
    scores: dict[str, float],
) -> OrganizationSpec:
    all_organizations = [
        organization for organization in organizations if organization.auto_selectable
    ]
    if not all_organizations:
        raise ValueError(
            "catalog has no auto-selectable organizations; pass --organization explicitly"
        )
    protocol_matches = [
        organization
        for organization in all_organizations
        if organization.protocol == desired_protocol
    ]
    if not protocol_matches:
        raise ValueError(
            f"catalog has no auto-selectable {desired_protocol} organization; "
            "pass --organization explicitly"
        )
    candidates = protocol_matches
    return sorted(
        candidates,
        key=lambda organization: (
            -len(_member_ids(organization).intersection(desired_hsa_ids)),
            -sum(scores.get(member.hsa_id, 0.0) for member in organization.members),
            len(organization.members),
            organization.id,
        ),
    )[0]


def _effective_organization(
    organization: OrganizationSpec,
    *,
    desired_hsa_ids: set[str],
    scores: dict[str, float],
) -> OrganizationSpec:
    base_member_ids = _member_ids(organization)
    selected = desired_hsa_ids.intersection(base_member_ids)
    if organization.protocol == "red_team" or len(selected) < 2:
        selected = base_member_ids
    members = [member for member in organization.members if member.hsa_id in selected]
    if len(members) < 2:
        raise ValueError("auto-routed meetings require at least two HSA members")
    if organization.chair_id in selected:
        chair_id = organization.chair_id
    else:
        chair_id = sorted(selected, key=lambda hsa_id: (-scores.get(hsa_id, 0.0), hsa_id))[0]
    judge_ids = [judge_id for judge_id in organization.judge_ids if judge_id in selected]
    return OrganizationSpec.model_validate(
        {
            **organization.model_dump(mode="python"),
            "name": f"{organization.name}（自动选席）",
            "chair_id": chair_id,
            "judge_ids": judge_ids,
            "members": members,
            "min_quorum": min(len(members), max(2, organization.min_quorum)),
        }
    )


def _member_ids(organization: OrganizationSpec) -> set[str]:
    return {member.hsa_id for member in organization.members}


def _selection(
    organization: OrganizationSpec,
    *,
    effective_organization: OrganizationSpec,
    problem: DecisionProblem,
    mode: str,
    scores: dict[str, float],
    matched_signals: tuple[str, ...],
    reasons: list[str],
) -> MeetingSelection:
    return MeetingSelection(
        mode=mode,
        router_version=ROUTER_VERSION,
        router_policy_hash=_ROUTER_POLICY_HASH,
        problem_snapshot_hash=problem.snapshot_hash,
        organization_id=organization.id,
        organization_fingerprint=organization.fingerprint,
        effective_organization=effective_organization,
        effective_organization_fingerprint=effective_organization.fingerprint,
        protocol=effective_organization.protocol,
        selected_hsa_ids=[member.hsa_id for member in effective_organization.members],
        hsa_scores=scores,
        matched_signals=list(matched_signals),
        reasons=reasons,
    )


__all__ = [
    "AUTO_ORGANIZATION_ID",
    "ROUTER_VERSION",
    "MeetingRouter",
    "validate_selection_against_catalog",
]
