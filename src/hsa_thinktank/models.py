"""Versioned domain contracts for profiles, organizations and decisions."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from typing import Any, Literal
from uuid import uuid4

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    field_validator,
    model_serializer,
    model_validator,
)

from .provenance import normalize_public_source_url


ID_PATTERN = r"^[a-z0-9][a-z0-9-]{1,63}$"
Basis = Literal["grounded", "inferred", "speculative"]
RiskTier = Literal["low", "medium", "high"]
DecisionStatus = Literal["decided", "inconclusive", "rejected", "budget_exhausted", "needs_human"]


def utc_now() -> datetime:
    return datetime.now(UTC)


def canonical_json(value: Any) -> str:
    if isinstance(value, BaseModel):
        value = value.model_dump(mode="json")
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class SourceRef(StrictModel):
    id: str = Field(pattern=ID_PATTERN)
    title: str = Field(min_length=1)
    url: HttpUrl
    kind: Literal["primary", "secondary"] = "primary"
    note: str = ""


class Principle(StrictModel):
    id: str = Field(pattern=ID_PATTERN)
    rule: str = Field(min_length=1)
    source_ids: list[str] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)
    domains: list[str] = Field(default_factory=list)
    counterexamples: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_source_ids(self) -> "Principle":
        if len(self.source_ids) != len(set(self.source_ids)):
            raise ValueError("principle source_ids must be unique")
        return self


class HSAProfile(StrictModel):
    id: str = Field(pattern=ID_PATTERN)
    display_name: str = Field(min_length=1)
    version: str = Field(min_length=1)
    grounding_mode: Literal["evidence_grounded", "inspired_synthesis", "fictionalized"]
    summary: str = Field(min_length=1)
    principles: list[Principle] = Field(min_length=1)
    domain_limits: list[str] = Field(default_factory=list)
    epistemic_rules: list[str] = Field(min_length=1)
    forbidden_claims: list[str] = Field(min_length=1)
    voice_style: dict[str, str] = Field(default_factory=dict)
    sources: list[SourceRef] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_source_links(self) -> "HSAProfile":
        source_ids = [source.id for source in self.sources]
        if len(source_ids) != len(set(source_ids)):
            raise ValueError("source ids must be unique")
        principle_ids = [principle.id for principle in self.principles]
        if len(principle_ids) != len(set(principle_ids)):
            raise ValueError("principle ids must be unique")
        known_source_ids = set(source_ids)
        missing = sorted(
            source_id
            for principle in self.principles
            for source_id in principle.source_ids
            if source_id not in known_source_ids
        )
        if missing:
            raise ValueError(f"principles reference unknown sources: {', '.join(missing)}")
        return self

    @property
    def fingerprint(self) -> str:
        return content_hash(self)


class OrganizationMember(StrictModel):
    hsa_id: str = Field(pattern=ID_PATTERN)
    role: str = Field(min_length=1)
    weight: float = Field(default=1.0, ge=0.25, le=2.0)
    correlation_group: str = Field(default="hermes-default", min_length=1)


class OrganizationMemoryPolicy(StrictModel):
    private_enabled: bool = True
    shared_read: bool = True
    shared_write_mode: Literal["disabled", "staged", "final_decision_only"] = "final_decision_only"


class OrganizationSpec(StrictModel):
    id: str = Field(pattern=ID_PATTERN)
    name: str = Field(min_length=1)
    protocol: Literal["roundtable", "red_team", "cabinet"]
    version: str = Field(min_length=1)
    chair_id: str = Field(pattern=ID_PATTERN)
    judge_ids: list[str] = Field(default_factory=list)
    min_quorum: int = Field(ge=1)
    min_margin: float = Field(default=0.10, ge=0.0, le=1.0)
    allow_chair_override: bool = False
    auto_selectable: bool = False
    members: list[OrganizationMember] = Field(min_length=2)
    memory_policy: OrganizationMemoryPolicy = Field(default_factory=OrganizationMemoryPolicy)
    tool_policy_id: str = Field(default="standard-research", pattern=ID_PATTERN)
    max_rounds: int = Field(default=2, ge=1, le=10)
    max_invocations: int = Field(default=24, ge=1, le=200)

    @model_validator(mode="after")
    def validate_membership(self) -> "OrganizationSpec":
        ids = [member.hsa_id for member in self.members]
        if len(ids) != len(set(ids)):
            raise ValueError("organization member ids must be unique")
        if self.chair_id not in ids:
            raise ValueError("chair_id must reference a member")
        missing_judges = sorted(set(self.judge_ids) - set(ids))
        if missing_judges:
            raise ValueError(f"judge_ids reference non-members: {', '.join(missing_judges)}")
        if self.min_quorum > len(ids):
            raise ValueError("min_quorum cannot exceed member count")
        if self.protocol == "red_team":
            if not self.judge_ids:
                raise ValueError("red_team requires at least one judge")
            if self.chair_id in self.judge_ids:
                raise ValueError("red_team proposer/chair cannot also judge")
            critics = set(ids) - {self.chair_id, *self.judge_ids}
            if not critics:
                raise ValueError("red_team requires a distinct critic")
        return self

    def member(self, hsa_id: str) -> OrganizationMember:
        for member in self.members:
            if member.hsa_id == hsa_id:
                return member
        raise KeyError(hsa_id)

    @property
    def fingerprint(self) -> str:
        return content_hash(self)


class DecisionOption(StrictModel):
    id: str = Field(pattern=ID_PATTERN)
    description: str = Field(min_length=1)


class Criterion(StrictModel):
    id: str = Field(pattern=ID_PATTERN)
    description: str = Field(min_length=1)
    weight: float = Field(default=1.0, gt=0.0)
    hard_constraint: bool = False


class EvidenceItem(StrictModel):
    id: str = Field(pattern=ID_PATTERN)
    title: str = Field(min_length=1)
    content: str = Field(min_length=1)
    source_url: HttpUrl | None = None
    content_hash: str | None = Field(default=None, pattern=r"^[0-9a-f]{64}$")

    @model_validator(mode="after")
    def fill_hash(self) -> "EvidenceItem":
        expected = content_hash(self.content)
        if self.content_hash is not None and self.content_hash != expected:
            raise ValueError("content_hash does not match evidence content")
        object.__setattr__(self, "content_hash", expected)
        return self


class DecisionProblem(StrictModel):
    id: str = Field(default_factory=lambda: f"decision-{uuid4().hex[:12]}", pattern=ID_PATTERN)
    question: str = Field(min_length=1)
    context: str = ""
    constraints: list[str] = Field(default_factory=list)
    options: list[DecisionOption] = Field(default_factory=list)
    criteria: list[Criterion] = Field(
        default_factory=lambda: [
            Criterion(id="goal-fit", description="目标适配程度"),
            Criterion(id="downside", description="下行风险与可逆性"),
            Criterion(id="execution", description="执行可行性与资源匹配"),
        ]
    )
    evidence: list[EvidenceItem] = Field(default_factory=list)
    risk_tier: RiskTier = "medium"
    max_parallel: int = Field(default=4, ge=1, le=32)
    user_tool_grants: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_unique_ids(self) -> "DecisionProblem":
        for label, values in (
            ("option", self.options),
            ("criterion", self.criteria),
            ("evidence", self.evidence),
        ):
            ids = [value.id for value in values]
            if len(ids) != len(set(ids)):
                raise ValueError(f"{label} ids must be unique")
        if self.options and len(self.options) < 2:
            raise ValueError("provide zero options for generation or at least two options")
        return self

    @property
    def snapshot_hash(self) -> str:
        return content_hash(self)


class MeetingSelection(StrictModel):
    """Auditable result of choosing a meeting topology for one problem."""

    schema_version: Literal["1.0"] = "1.0"
    mode: Literal["auto", "explicit"]
    router_version: str = Field(min_length=1)
    router_policy_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    problem_snapshot_hash: str = Field(pattern=r"^[0-9a-f]{64}$")
    organization_id: str = Field(pattern=ID_PATTERN)
    organization_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    effective_organization: OrganizationSpec
    effective_organization_fingerprint: str = Field(pattern=r"^[0-9a-f]{64}$")
    protocol: Literal["roundtable", "red_team", "cabinet"]
    selected_hsa_ids: list[str] = Field(min_length=2)
    hsa_scores: dict[str, float] = Field(default_factory=dict)
    matched_signals: list[str] = Field(default_factory=list)
    reasons: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_selection(self) -> "MeetingSelection":
        if len(self.selected_hsa_ids) != len(set(self.selected_hsa_ids)):
            raise ValueError("selected_hsa_ids must be unique")
        if len(self.matched_signals) != len(set(self.matched_signals)):
            raise ValueError("matched_signals must be unique")
        invalid_selected_ids = sorted(
            hsa_id for hsa_id in self.selected_hsa_ids if re.fullmatch(ID_PATTERN, hsa_id) is None
        )
        if invalid_selected_ids:
            raise ValueError(
                f"selected_hsa_ids contains invalid ids: {', '.join(invalid_selected_ids)}"
            )
        invalid_score_ids = sorted(
            hsa_id for hsa_id in self.hsa_scores if re.fullmatch(ID_PATTERN, hsa_id) is None
        )
        if invalid_score_ids:
            raise ValueError(f"hsa_scores contains invalid ids: {', '.join(invalid_score_ids)}")
        if any(score < 0 for score in self.hsa_scores.values()):
            raise ValueError("hsa_scores cannot be negative")
        if not set(self.selected_hsa_ids).issubset(self.hsa_scores):
            raise ValueError("hsa_scores must cover every selected HSA")
        effective_member_ids = [member.hsa_id for member in self.effective_organization.members]
        if self.selected_hsa_ids != effective_member_ids:
            raise ValueError("selected_hsa_ids must match effective organization member order")
        if self.effective_organization.id != self.organization_id:
            raise ValueError("effective organization must preserve the base organization id")
        if self.effective_organization.protocol != self.protocol:
            raise ValueError("effective organization protocol does not match selection")
        if self.effective_organization_fingerprint != self.effective_organization.fingerprint:
            raise ValueError("effective organization fingerprint does not match snapshot")
        return self

    @property
    def fingerprint(self) -> str:
        return content_hash(self)


class RationaleClaim(StrictModel):
    model_config = ConfigDict(
        json_schema_extra={
            "allOf": [
                {
                    "if": {
                        "properties": {"basis": {"const": "grounded"}},
                        "required": ["basis"],
                    },
                    "then": {
                        "anyOf": [
                            {
                                "properties": {field: {"minItems": 1}},
                                "required": [field],
                            }
                            for field in (
                                "principle_ids",
                                "evidence_ids",
                                "memory_ids",
                                "tool_artifact_ids",
                                "source_urls",
                            )
                        ]
                    },
                }
            ]
        }
    )

    claim: str = Field(min_length=1)
    basis: Basis
    principle_ids: list[str] = Field(default_factory=list)
    evidence_ids: list[str] = Field(default_factory=list)
    memory_ids: list[str] = Field(default_factory=list)
    tool_artifact_ids: list[str] = Field(default_factory=list)
    source_urls: list[HttpUrl] = Field(default_factory=list, max_length=8)

    @field_validator("source_urls", mode="before")
    @classmethod
    def validate_public_source_urls(cls, values: Any) -> Any:
        if not isinstance(values, (list, tuple)):
            return values
        normalized: list[str] = []
        for value in values:
            source_url = normalize_public_source_url(value)
            if source_url not in normalized:
                normalized.append(source_url)
        return normalized

    @model_validator(mode="after")
    def grounded_claim_requires_provenance(self) -> "RationaleClaim":
        if self.basis == "grounded" and not any(
            (
                self.principle_ids,
                self.evidence_ids,
                self.memory_ids,
                self.tool_artifact_ids,
                self.source_urls,
            )
        ):
            raise ValueError("grounded claims require at least one provenance reference")
        return self

    @model_serializer(mode="wrap")
    def omit_empty_source_urls(self, handler):
        serialized = handler(self)
        if not self.source_urls:
            serialized.pop("source_urls", None)
        return serialized


class GeneratedOptions(StrictModel):
    options: list[DecisionOption] = Field(min_length=2, max_length=6)
    generation_note: str = Field(min_length=1)
    claims: list[RationaleClaim] = Field(default_factory=list, max_length=6)

    @model_validator(mode="after")
    def validate_options(self) -> "GeneratedOptions":
        ids = [option.id for option in self.options]
        if len(ids) != len(set(ids)):
            raise ValueError("generated option ids must be unique")
        return self


class Objection(StrictModel):
    option_id: str = Field(pattern=ID_PATTERN)
    severity: Literal["low", "medium", "high", "critical"]
    statement: str = Field(min_length=1)
    mitigation: str = ""
    resolved: bool = False


class Contribution(StrictModel):
    preferred_option_id: str = Field(pattern=ID_PATTERN)
    option_scores: dict[str, float] = Field(min_length=2)
    criterion_scores: dict[str, dict[str, float]] = Field(default_factory=dict)
    constraint_results: dict[str, dict[str, bool]] = Field(default_factory=dict)
    confidence: float = Field(ge=0.0, le=1.0)
    claims: list[RationaleClaim] = Field(min_length=1, max_length=10)
    assumptions: list[str] = Field(default_factory=list, max_length=10)
    risks: list[Objection] = Field(default_factory=list, max_length=16)
    next_actions: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_scores(self) -> "Contribution":
        values = list(self.option_scores.values()) + [
            value for scores in self.criterion_scores.values() for value in scores.values()
        ]
        if any(not 0.0 <= value <= 1.0 for value in values):
            raise ValueError("all scores must be between 0 and 1")
        highest = max(self.option_scores.values())
        if self.option_scores.get(self.preferred_option_id) != highest:
            raise ValueError("preferred_option_id must have the highest option_score")
        return self


class Attack(StrictModel):
    attack_id: str = Field(pattern=ID_PATTERN)
    option_id: str = Field(pattern=ID_PATTERN)
    severity: Literal["low", "medium", "high", "critical"]
    claim: str = Field(min_length=1)
    evidence_needed: str = ""
    suggested_mitigation: str = ""
    evidence_ids: list[str] = Field(default_factory=list)
    tool_artifact_ids: list[str] = Field(default_factory=list)
    source_urls: list[HttpUrl] = Field(default_factory=list, max_length=8)

    @field_validator("source_urls", mode="before")
    @classmethod
    def validate_public_source_urls(cls, values: Any) -> Any:
        if not isinstance(values, (list, tuple)):
            return values
        normalized: list[str] = []
        for value in values:
            source_url = normalize_public_source_url(value)
            if source_url not in normalized:
                normalized.append(source_url)
        return normalized

    @model_serializer(mode="wrap")
    def omit_empty_source_urls(self, handler):
        serialized = handler(self)
        if not self.source_urls:
            serialized.pop("source_urls", None)
        return serialized


class RedTeamCritique(StrictModel):
    attacks: list[Attack] = Field(min_length=1, max_length=16)
    strongest_alternative_id: str = Field(pattern=ID_PATTERN)

    @model_validator(mode="after")
    def validate_attack_ids(self) -> "RedTeamCritique":
        attack_ids = [attack.attack_id for attack in self.attacks]
        if len(attack_ids) != len(set(attack_ids)):
            raise ValueError("attack ids must be unique")
        return self


class AttackDisposition(StrictModel):
    attack_id: str = Field(pattern=ID_PATTERN)
    status: Literal["accepted", "mitigated", "rejected", "unresolved"]
    response: str = Field(min_length=1)


class RedTeamRebuttal(StrictModel):
    revised_ballot: Contribution
    dispositions: list[AttackDisposition] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_disposition_ids(self) -> "RedTeamRebuttal":
        attack_ids = [disposition.attack_id for disposition in self.dispositions]
        if len(attack_ids) != len(set(attack_ids)):
            raise ValueError("attack disposition ids must be unique")
        return self


class ExecutiveDecision(StrictModel):
    ballot: Contribution
    override_reason: str = ""


class MessageEnvelope(StrictModel):
    id: str
    run_id: str
    ordinal: int = Field(ge=1)
    round: int = Field(ge=0)
    phase: str
    sender_id: str
    kind: str
    visibility: Literal["public", "anonymous", "private", "audit"]
    payload: dict[str, Any]
    parent_message_ids: list[str] = Field(default_factory=list)
    profile_version: str
    content_hash: str


class RuntimeCallRecord(StrictModel):
    invocation_id: str
    hsa_id: str
    phase: str
    backend: str
    success: bool
    session_id: str | None = None
    response_hash: str | None = None
    enabled_toolsets: list[str] = Field(default_factory=list)
    error: str | None = None


class AuditEvent(StrictModel):
    id: str
    run_id: str
    ordinal: int = Field(ge=1)
    event_type: str
    payload: dict[str, Any]
    previous_hash: str
    event_hash: str
    created_at: datetime = Field(default_factory=utc_now)


class DecisionReport(StrictModel):
    schema_version: Literal["1.1"] = "1.1"
    run_id: str
    decision_id: str
    request_snapshot: DecisionProblem
    frozen_problem: DecisionProblem
    request_snapshot_hash: str
    frozen_problem_hash: str
    memory_snapshot_hash: str
    memory_store_id: str | None
    approval_store_id: str | None
    native_memory_fingerprints: dict[str, str]
    native_memory_fingerprints_after: dict[str, str]
    native_memory_changed: list[str]
    meeting_selection: MeetingSelection
    organization_id: str
    organization_version: str
    organization_fingerprint: str
    shared_memory_write_mode: Literal["disabled", "staged", "final_decision_only"]
    protocol_name: str
    protocol_version: Literal["1.0"] = "1.0"
    status: DecisionStatus
    status_reason: str
    selected_option_id: str | None
    selected_option: str | None
    option_scores: dict[str, float]
    confidence: float = Field(ge=0.0, le=1.0)
    raw_member_count: int = Field(ge=0)
    correlation_group_count: int = Field(ge=0)
    effective_sample_size: float = Field(ge=0.0)
    successful_member_ids: list[str]
    rationale_claims: list[RationaleClaim]
    assumptions: list[str]
    unresolved_risks: list[str]
    dissent: list[str]
    next_actions: list[str]
    memory_ids: list[str]
    tool_artifact_ids: list[str]
    approval_ids: list[str]
    decision_binding_hash: str
    chair_override: dict[str, str] | None = None
    profile_fingerprints: dict[str, str]
    runtime_calls: list[RuntimeCallRecord]
    messages: list[MessageEnvelope]
    audit_events: list[AuditEvent] = Field(min_length=1)
    trace_root_hash: str
    started_at: datetime = Field(default_factory=utc_now)
    completed_at: datetime = Field(default_factory=utc_now)

    def decision_binding(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "decision_id": self.decision_id,
            "request_snapshot_hash": self.request_snapshot_hash,
            "frozen_problem_hash": self.frozen_problem_hash,
            "memory_snapshot_hash": self.memory_snapshot_hash,
            "memory_store_id": self.memory_store_id,
            "approval_store_id": self.approval_store_id,
            "native_memory_fingerprints": self.native_memory_fingerprints,
            "native_memory_fingerprints_after": self.native_memory_fingerprints_after,
            "meeting_selection": self.meeting_selection.model_dump(mode="json"),
            "organization_id": self.organization_id,
            "organization_fingerprint": self.organization_fingerprint,
            "shared_memory_write_mode": self.shared_memory_write_mode,
            "protocol_name": self.protocol_name,
            "status": self.status,
            "status_reason": self.status_reason,
            "selected_option_id": self.selected_option_id,
            "option_scores": self.option_scores,
            "confidence": self.confidence,
            "unresolved_risks": self.unresolved_risks,
            "profile_fingerprints": self.profile_fingerprints,
        }

    @model_validator(mode="after")
    def validate_decision_binding(self) -> "DecisionReport":
        if self.decision_id != self.frozen_problem.id:
            raise ValueError("decision_id does not match frozen problem")
        if self.request_snapshot.id != self.frozen_problem.id:
            raise ValueError("request and frozen problem ids do not match")
        if self.request_snapshot_hash != self.request_snapshot.snapshot_hash:
            raise ValueError("request_snapshot_hash does not match request snapshot")
        if self.frozen_problem_hash != self.frozen_problem.snapshot_hash:
            raise ValueError("frozen_problem_hash does not match frozen problem")
        if self.meeting_selection.organization_id != self.organization_id:
            raise ValueError("meeting selection organization does not match decision report")
        if self.meeting_selection.organization_fingerprint != self.organization_fingerprint:
            raise ValueError("meeting selection fingerprint does not match organization")
        if self.meeting_selection.problem_snapshot_hash != self.request_snapshot_hash:
            raise ValueError("meeting selection does not match the original problem snapshot")
        if self.meeting_selection.protocol != self.protocol_name:
            raise ValueError("meeting selection protocol does not match decision report")
        if set(self.meeting_selection.selected_hsa_ids) != set(self.profile_fingerprints):
            raise ValueError("meeting selection HSA set does not match profile fingerprints")
        if (
            self.meeting_selection.effective_organization.memory_policy.shared_write_mode
            != self.shared_memory_write_mode
        ):
            raise ValueError("meeting selection memory policy does not match decision report")
        selected = set(self.meeting_selection.selected_hsa_ids)
        if set(self.native_memory_fingerprints) != selected:
            raise ValueError("native memory fingerprints do not match selected HSAs")
        if set(self.native_memory_fingerprints_after) != selected:
            raise ValueError("post-run memory fingerprints do not match selected HSAs")
        if not set(self.successful_member_ids).issubset(selected):
            raise ValueError("successful members contain an unselected HSA")
        if any(call.hsa_id not in selected for call in self.runtime_calls):
            raise ValueError("runtime calls contain an unselected HSA")
        if any(message.sender_id not in selected for message in self.messages):
            raise ValueError("messages contain an unselected HSA")
        if self.decision_binding_hash != content_hash(self.decision_binding()):
            raise ValueError("decision_binding_hash does not match report decision state")
        return self


def validate_option_references(
    contribution: Contribution,
    option_ids: set[str],
    criterion_ids: set[str] | None = None,
    hard_constraint_ids: set[str] | None = None,
) -> None:
    unknown = set(contribution.option_scores) - option_ids
    missing = option_ids - set(contribution.option_scores)
    if unknown or missing:
        raise ValueError(
            "option_scores must contain exactly frozen options; "
            f"unknown_count={len(unknown)}, missing_count={len(missing)}"
        )
    if contribution.preferred_option_id not in option_ids:
        raise ValueError("preferred_option_id is not frozen")
    for risk in contribution.risks:
        if risk.option_id not in option_ids:
            raise ValueError("risk references an unknown option")
    if contribution.criterion_scores:
        criterion_option_ids = set(contribution.criterion_scores)
        if criterion_option_ids != option_ids:
            raise ValueError(
                "criterion_scores must contain exactly frozen options when provided; "
                f"unknown_count={len(criterion_option_ids - option_ids)}, "
                f"missing_count={len(option_ids - criterion_option_ids)}"
            )
        if criterion_ids is not None:
            for option_id, scores in contribution.criterion_scores.items():
                supplied = set(scores)
                if supplied != criterion_ids:
                    raise ValueError(
                        "criterion_scores entry must contain exactly frozen criteria; "
                        f"unknown_count={len(supplied - criterion_ids)}, "
                        f"missing_count={len(criterion_ids - supplied)}"
                    )
    expected_hard_constraints = hard_constraint_ids or set()
    if expected_hard_constraints:
        supplied_options = set(contribution.constraint_results)
        if supplied_options != option_ids:
            raise ValueError(
                "constraint_results must contain exactly frozen options; "
                f"unknown_count={len(supplied_options - option_ids)}, "
                f"missing_count={len(option_ids - supplied_options)}"
            )
        for option_id, results in contribution.constraint_results.items():
            supplied_constraints = set(results)
            if supplied_constraints != expected_hard_constraints:
                raise ValueError(
                    "constraint_results entry must contain exactly hard constraints; "
                    f"unknown_count={len(supplied_constraints - expected_hard_constraints)}, "
                    f"missing_count={len(expected_hard_constraints - supplied_constraints)}"
                )
    elif contribution.constraint_results:
        raise ValueError("constraint_results were supplied but no hard constraints are frozen")


def extract_json_object(raw: str) -> dict[str, Any]:
    """Extract one JSON object; semantic repair is intentionally not attempted."""

    text = raw.strip()
    fenced = re.fullmatch(r"```(?:json)?\s*(.*?)\s*```", text, re.DOTALL | re.IGNORECASE)
    if fenced:
        text = fenced.group(1).strip()
    try:
        value = json.loads(text)
        if isinstance(value, dict):
            return value
    except json.JSONDecodeError:
        pass
    decoder = json.JSONDecoder()
    for index, character in enumerate(text):
        if character != "{":
            continue
        try:
            value, end = decoder.raw_decode(text[index:])
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict) and not text[index + end :].strip(" \t\r\n`"):
            return value
    raise ValueError("response does not contain exactly one parseable JSON object")
