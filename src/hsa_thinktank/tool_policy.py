"""Fail-closed tool capability policy for Hermes-backed HSA profiles.

Hermes profiles isolate identity, configuration, memory, and sessions. They do
not isolate the host operating system. Tool admission is therefore computed by
the orchestrator before every invocation, and local execution is never enabled
unless the user grants the corresponding L2 toolset for that run.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator


STANDARD_RESEARCH_POLICY_ID = "standard-research"


class ToolRisk(str, Enum):
    """Coarse risk level used for toolset admission."""

    L0 = "L0"  # pure calculation
    L1 = "L1"  # read-only research and context retrieval
    L2 = "L2"  # mutable profile or host capability requiring a per-run grant
    L3 = "L3"  # external or difficult-to-reverse side effect

    @property
    def rank(self) -> int:
        return int(self.value[1])


class TerminalBackend(str, Enum):
    """Where Hermes terminal commands execute."""

    DISABLED = "disabled"
    LOCAL = "local"


DEFAULT_TOOL_RISKS: dict[str, ToolRisk] = {
    "calculator": ToolRisk.L0,
    "todo": ToolRisk.L0,
    "web": ToolRisk.L1,
    "search": ToolRisk.L1,
    # Hermes delegation may trigger additional model calls and cost. It is a
    # research artifact, not an organization member, and is opt-in per run.
    "delegation": ToolRisk.L2,
    # Hermes' native memory tool can mutate MEMORY.md/USER.md immediately.
    # Reading those files in the profile prompt remains available without this
    # tool; opening the mutating tool therefore requires an explicit run grant.
    "memory": ToolRisk.L2,
    # Session history is mutable profile state and is not part of a frozen
    # decision request. It therefore requires an explicit run grant.
    "session_search": ToolRisk.L2,
    "code_execution": ToolRisk.L2,
    "file": ToolRisk.L2,
    "terminal": ToolRisk.L2,
    # Browser automation can cross from read-only research into external
    # actions, so the MVP keeps it behind the L3 control plane.
    "browser": ToolRisk.L3,
    "mcp": ToolRisk.L3,
    "external_action": ToolRisk.L3,
}


def _normalise_names(values: Iterable[str] | str, *, label: str) -> tuple[str, ...]:
    source = (values,) if isinstance(values, str) else values
    seen: set[str] = set()
    result: list[str] = []
    for value in source:
        if not isinstance(value, str):
            raise TypeError(f"{label} must contain only strings")
        name = value.strip()
        if not name:
            raise ValueError(f"{label} cannot contain an empty toolset name")
        if name not in seen:
            result.append(name)
            seen.add(name)
    return tuple(result)


class ToolRejection(BaseModel):
    """Structured reason that a requested capability was not admitted."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    toolset: str
    reason: str
    risk: ToolRisk | None = None


class ToolResolution(BaseModel):
    """Auditable result of resolving one phase's effective toolsets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str
    phase: str
    requested_toolsets: tuple[str, ...]
    considered_toolsets: tuple[str, ...]
    enabled_toolsets: tuple[str, ...]
    rejected: tuple[ToolRejection, ...] = ()
    terminal_backend: TerminalBackend
    delegation_output: Literal["artifact"] = "artifact"
    delegation_counts_as_member: Literal[False] = False

    @property
    def allowed(self) -> bool:
        return not self.rejected


class ToolPolicy(BaseModel):
    """Capability intersection and risk gates for one organization policy.

    The base set is the intersection of the profile, organization, and phase
    boundaries.  L0/L1 tools are auto-admitted inside that boundary, L2 tools
    additionally require a per-run user grant, and L3 tools require an explicit
    human approval.  Configured memory context tools are added to the phase
    request before those same gates are applied.
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    policy_id: str = STANDARD_RESEARCH_POLICY_ID
    profile_allowlist: frozenset[str]
    organization_allowlist: frozenset[str]
    phase_allowlists: dict[str, frozenset[str]]
    risk_by_tool: dict[str, ToolRisk] = Field(default_factory=lambda: dict(DEFAULT_TOOL_RISKS))
    terminal_backend: TerminalBackend = TerminalBackend.DISABLED
    terminal_toolsets: frozenset[str] = frozenset({"terminal", "file", "code_execution"})
    memory_enabled: bool = True
    session_search_enabled: bool = True
    delegation_enabled: bool = False
    delegation_output: Literal["artifact"] = "artifact"
    delegation_counts_as_member: Literal[False] = False

    @field_validator("profile_allowlist", "organization_allowlist", "terminal_toolsets")
    @classmethod
    def _validate_allowlist(cls, values: frozenset[str]) -> frozenset[str]:
        return frozenset(_normalise_names(values, label="allowlist"))

    @field_validator("phase_allowlists")
    @classmethod
    def _validate_phases(cls, phases: dict[str, frozenset[str]]) -> dict[str, frozenset[str]]:
        result: dict[str, frozenset[str]] = {}
        for raw_phase, values in phases.items():
            phase = raw_phase.strip()
            if not phase:
                raise ValueError("phase names cannot be empty")
            result[phase] = frozenset(_normalise_names(values, label=f"phase {phase!r} allowlist"))
        return result

    @field_validator("risk_by_tool")
    @classmethod
    def _validate_risks(cls, risks: dict[str, ToolRisk]) -> dict[str, ToolRisk]:
        result: dict[str, ToolRisk] = {}
        for raw_name, risk in risks.items():
            names = _normalise_names((raw_name,), label="risk map")
            result[names[0]] = risk
        return result

    @property
    def context_toolsets(self) -> tuple[str, ...]:
        result: list[str] = []
        if self.memory_enabled:
            result.append("memory")
        if self.session_search_enabled:
            result.append("session_search")
        return tuple(result)

    def resolve(
        self,
        *,
        phase: str,
        requested_toolsets: Iterable[str] | str,
        user_grants: Iterable[str] | str,
        human_approvals: Iterable[str] | str = (),
    ) -> ToolResolution:
        """Resolve effective capabilities and explain every rejected request."""

        phase_name = phase.strip()
        if not phase_name:
            raise ValueError("phase cannot be empty")
        requested = _normalise_names(requested_toolsets, label="requested_toolsets")
        grants = frozenset(_normalise_names(user_grants, label="user_grants"))
        approvals = frozenset(_normalise_names(human_approvals, label="human_approvals"))

        considered = list(requested)
        for toolset in self.context_toolsets:
            if toolset not in considered:
                considered.append(toolset)

        phase_allowlist = self.phase_allowlists.get(phase_name)
        enabled: list[str] = []
        rejected: list[ToolRejection] = []
        for toolset in considered:
            risk = self.risk_by_tool.get(toolset)
            reason = self._rejection_reason(
                toolset=toolset,
                risk=risk,
                phase_allowlist=phase_allowlist,
                user_grants=grants,
                human_approvals=approvals,
            )
            if reason is None:
                enabled.append(toolset)
            else:
                rejected.append(ToolRejection(toolset=toolset, reason=reason, risk=risk))

        return ToolResolution(
            policy_id=self.policy_id,
            phase=phase_name,
            requested_toolsets=requested,
            considered_toolsets=tuple(considered),
            enabled_toolsets=tuple(enabled),
            rejected=tuple(rejected),
            terminal_backend=self.terminal_backend,
            delegation_output=self.delegation_output,
            delegation_counts_as_member=self.delegation_counts_as_member,
        )

    def _rejection_reason(
        self,
        *,
        toolset: str,
        risk: ToolRisk | None,
        phase_allowlist: frozenset[str] | None,
        user_grants: frozenset[str],
        human_approvals: frozenset[str],
    ) -> str | None:
        if risk is None:
            return "unclassified_toolset"
        if toolset not in self.profile_allowlist:
            return "not_in_profile_allowlist"
        if toolset not in self.organization_allowlist:
            return "not_in_organization_allowlist"
        if phase_allowlist is None:
            return "unknown_phase"
        effective_phase_allowlist = phase_allowlist.union(self.context_toolsets)
        if toolset not in effective_phase_allowlist:
            return "not_in_phase_allowlist"
        if risk is ToolRisk.L2 and toolset not in user_grants:
            return "l2_user_grant_required"

        if toolset == "memory" and not self.memory_enabled:
            return "memory_disabled"
        if toolset == "session_search" and not self.session_search_enabled:
            return "session_search_disabled"
        if toolset == "delegation" and not self.delegation_enabled:
            return "delegation_disabled"

        if (
            toolset in self.terminal_toolsets
            and risk.rank >= ToolRisk.L2.rank
            and self.terminal_backend is TerminalBackend.DISABLED
        ):
            return "local_execution_disabled"

        if risk is ToolRisk.L3 and toolset not in human_approvals:
            return "human_approval_required"
        return None

    def allowed_toolsets(
        self,
        *,
        phase: str,
        requested_toolsets: Iterable[str] | str,
        user_grants: Iterable[str] | str,
        human_approvals: Iterable[str] | str = (),
    ) -> tuple[str, ...]:
        """Convenience wrapper for callers that also persist ``resolve`` output."""

        return self.resolve(
            phase=phase,
            requested_toolsets=requested_toolsets,
            user_grants=user_grants,
            human_approvals=human_approvals,
        ).enabled_toolsets


_STANDARD_TOOLSETS = frozenset(DEFAULT_TOOL_RISKS).difference(
    {
        # Hermes 0.16 exposes no standalone ``calculator`` toolset.  Keep its
        # generic risk classification above for custom policies, but reject it
        # at the bundled profile boundary instead of letting Hermes silently
        # narrow an explicitly scoped invocation.
        "calculator",
    }
)
_RESEARCH_TOOLSETS = frozenset(
    {
        "todo",
        "web",
        "search",
        "memory",
        "session_search",
        "delegation",
        "browser",
        "code_execution",
    }
)
_ANALYSIS_TOOLSETS = frozenset(
    {
        "todo",
        "search",
        "web",
        "memory",
        "session_search",
        "delegation",
        "file",
        "terminal",
        "code_execution",
    }
)
_DELIBERATION_TOOLSETS = frozenset(
    {
        "todo",
        "search",
        "web",
        "memory",
        "session_search",
        "delegation",
        "code_execution",
    }
)
_AGGREGATION_TOOLSETS = frozenset({"todo", "memory", "session_search"})


def standard_research_policy(**overrides: Any) -> ToolPolicy:
    """Build the bundled memory-capable research policy.

    Hermes uses its local terminal backend, but host execution tools remain
    unavailable unless the user grants each L2 capability for the current run.
    L3 tools require explicit human approval. Deployments may narrow any
    allowlist through ``overrides``.
    """

    values: dict[str, Any] = {
        "policy_id": STANDARD_RESEARCH_POLICY_ID,
        "profile_allowlist": _STANDARD_TOOLSETS,
        "organization_allowlist": _STANDARD_TOOLSETS,
        "phase_allowlists": {
            "evidence": _RESEARCH_TOOLSETS,
            "evidence_collection": _RESEARCH_TOOLSETS,
            "option_generation": _RESEARCH_TOOLSETS,
            "independent": _ANALYSIS_TOOLSETS,
            "independent_draft": _ANALYSIS_TOOLSETS,
            "independent_ballot": _ANALYSIS_TOOLSETS,
            "blue_proposal": _ANALYSIS_TOOLSETS,
            "portfolio_memo": _ANALYSIS_TOOLSETS,
            "deliberation": _DELIBERATION_TOOLSETS,
            "critique": _DELIBERATION_TOOLSETS,
            "revision": _DELIBERATION_TOOLSETS,
            "judge": _DELIBERATION_TOOLSETS,
            "synthesis": _DELIBERATION_TOOLSETS,
            "revised_ballot": _DELIBERATION_TOOLSETS,
            "red_critique": _DELIBERATION_TOOLSETS,
            "blue_rebuttal": _DELIBERATION_TOOLSETS,
            "judge_ballot": _DELIBERATION_TOOLSETS,
            "executive_ballot": _DELIBERATION_TOOLSETS,
            "aggregation": _AGGREGATION_TOOLSETS,
            "final": _AGGREGATION_TOOLSETS,
            "execution": _STANDARD_TOOLSETS,
        },
        "risk_by_tool": DEFAULT_TOOL_RISKS,
        "terminal_backend": TerminalBackend.LOCAL,
        "memory_enabled": True,
        "session_search_enabled": True,
        "delegation_enabled": True,
    }
    values.update(overrides)
    return ToolPolicy.model_validate(values)


def get_tool_policy(policy_id: str, **overrides: Any) -> ToolPolicy:
    """Resolve a bundled policy by catalog identifier."""

    if policy_id != STANDARD_RESEARCH_POLICY_ID:
        raise KeyError(f"unknown tool policy: {policy_id}")
    return standard_research_policy(**overrides)


TOOL_POLICY_REGISTRY: Mapping[str, Any] = {
    STANDARD_RESEARCH_POLICY_ID: standard_research_policy,
}


__all__ = [
    "DEFAULT_TOOL_RISKS",
    "STANDARD_RESEARCH_POLICY_ID",
    "TOOL_POLICY_REGISTRY",
    "TerminalBackend",
    "ToolPolicy",
    "ToolRejection",
    "ToolResolution",
    "ToolRisk",
    "get_tool_policy",
    "standard_research_policy",
]
