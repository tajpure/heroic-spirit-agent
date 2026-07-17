"""Async run coordinator connecting protocols to persistent Hermes profiles."""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import Callable, Mapping
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ValidationError

from .aggregation import aggregate
from .audit import AuditTrail
from .catalog import Catalog
from .errors import ProtocolError, StructuredOutputError
from .events import PublishedAuditTrail, RunEventStream, RunHandle, publish_run_terminal
from .memory import (
    ApprovalLevel,
    ApprovalRequest,
    ApprovalStore,
    InstitutionalMemoryStore,
    MemoryCandidate,
    MemoryScope,
)
from .models import (
    Attack,
    Contribution,
    DecisionProblem,
    DecisionReport,
    ExecutiveDecision,
    GeneratedOptions,
    MeetingSelection,
    MessageEnvelope,
    RedTeamCritique,
    RedTeamRebuttal,
    RuntimeCallRecord,
    RationaleClaim,
    canonical_json,
    content_hash,
    extract_json_object,
    utc_now,
    validate_option_references,
)
from .profile_manager import native_memory_fingerprint
from .prompting import compile_soul_prompt, compile_task_prompt
from .provenance import normalize_provenance_payload
from .protocols import ProtocolTask, TaskResult, protocol_for
from .run_store import (
    ApprovalOutboxOperation,
    LocalRunStore,
    MemoryOutboxOperation,
    RunOutbox,
)
from .routing import AUTO_ORGANIZATION_ID, MeetingRouter
from .runtime import AgentInvocation, AgentRuntime, RuntimeStreamEvent, redact_sensitive
from .tool_policy import ToolPolicy, get_tool_policy


PHASE_TOOLSETS: dict[str, tuple[str, ...]] = {
    "option_generation": ("memory", "session_search", "search", "web", "code_execution"),
    "independent_ballot": (
        "memory",
        "session_search",
        "search",
        "web",
        "code_execution",
        "delegation",
    ),
    "revised_ballot": ("memory", "session_search", "search", "web", "code_execution"),
    "blue_proposal": ("memory", "session_search", "search", "web", "code_execution"),
    "red_critique": (
        "memory",
        "session_search",
        "search",
        "web",
        "code_execution",
        "delegation",
    ),
    "blue_rebuttal": ("memory", "session_search", "search", "web", "code_execution"),
    "judge_ballot": ("memory", "session_search", "search", "web", "code_execution"),
    "portfolio_memo": (
        "memory",
        "session_search",
        "search",
        "web",
        "code_execution",
        "delegation",
    ),
    "executive_ballot": ("memory", "session_search", "search", "web", "code_execution"),
}


@dataclass
class _RawOutcome:
    task: ProtocolTask
    value: BaseModel | None
    response_text: str | None
    backend: str
    session_id: str | None
    enabled_toolsets: tuple[str, ...]
    tool_events: tuple[dict[str, Any], ...]
    tool_artifacts: tuple[dict[str, Any], ...]
    normalizations: tuple[dict[str, str], ...]
    error: str | None


class _RunContext:
    def __init__(
        self,
        *,
        run_id: str,
        problem: DecisionProblem,
        organization,
        catalog: Catalog,
        runtimes: AgentRuntime | Mapping[str, AgentRuntime],
        memory_snapshots: dict[str, list[dict[str, Any]]],
        tool_policy: ToolPolicy,
        audit: AuditTrail,
        events: RunEventStream,
    ) -> None:
        self.run_id = run_id
        self.problem = problem
        self.organization = organization
        self.catalog = catalog
        self.runtimes = runtimes
        self.memory_snapshots = memory_snapshots
        self.tool_policy = tool_policy
        self.audit = audit
        self.events = events
        self.messages: list[MessageEnvelope] = []
        self.runtime_calls: list[RuntimeCallRecord] = []
        self.invocation_count = 0
        self.budget_exhausted = False
        self.wave_count = 0
        self._semaphore = asyncio.Semaphore(problem.max_parallel)

    async def invoke_wave(self, tasks: list[ProtocolTask]) -> list[TaskResult]:
        if not tasks:
            return []
        self.wave_count += 1
        wave_id = f"wave-{self.wave_count:04d}"
        remaining = self.organization.max_invocations - self.invocation_count
        runnable = tasks[: max(0, remaining)]
        skipped = tasks[len(runnable) :]
        self.events.publish(
            lane="activity",
            kind="wave_scheduled",
            wave_id=wave_id,
            phase=tasks[0].phase,
            round=tasks[0].round,
            payload={
                "member_ids": [task.member_id for task in tasks],
                "runnable_count": len(runnable),
                "skipped_count": len(skipped),
            },
        )
        for task_index, task in enumerate(runnable):
            self.events.publish(
                lane="activity",
                kind="invocation_queued",
                wave_id=wave_id,
                task_index=task_index,
                phase=task.phase,
                round=task.round,
                hsa_id=task.member_id,
                invocation_id=self._invocation_id(task),
            )
        if skipped:
            self.budget_exhausted = True
            self.audit.append(
                "budget_exhausted",
                {"phase": tasks[0].phase, "skipped_members": [task.member_id for task in skipped]},
            )
            for task_index, task in enumerate(skipped, start=len(runnable)):
                self.events.publish(
                    lane="activity",
                    kind="invocation_skipped",
                    wave_id=wave_id,
                    task_index=task_index,
                    phase=task.phase,
                    round=task.round,
                    hsa_id=task.member_id,
                    invocation_id=self._invocation_id(task),
                    payload={"reason": "invocation_budget_exhausted"},
                )
        self.invocation_count += len(runnable)
        raw_results = await asyncio.gather(
            *(
                self._invoke(task, wave_id=wave_id, task_index=task_index)
                for task_index, task in enumerate(runnable)
            )
        )
        results: list[TaskResult] = []
        for raw in raw_results:
            message_id = None
            if raw.value is not None:
                message_id = self._record_message(raw.task, raw.value)
            self.runtime_calls.append(
                RuntimeCallRecord(
                    invocation_id=self._invocation_id(raw.task),
                    hsa_id=raw.task.member_id,
                    phase=raw.task.phase,
                    backend=raw.backend,
                    success=raw.value is not None,
                    session_id=raw.session_id,
                    response_hash=content_hash(raw.response_text) if raw.response_text else None,
                    enabled_toolsets=list(raw.enabled_toolsets),
                    error=raw.error,
                )
            )
            self.audit.append(
                "runtime_completed" if raw.value is not None else "runtime_failed",
                {
                    "invocation_id": self._invocation_id(raw.task),
                    "hsa_id": raw.task.member_id,
                    "phase": raw.task.phase,
                    "backend": raw.backend,
                    "session_id": raw.session_id,
                    "enabled_toolsets": list(raw.enabled_toolsets),
                    "tool_event_hashes": [content_hash(item) for item in raw.tool_events],
                    "tool_artifacts": [
                        {
                            "id": _tool_artifact_id(item),
                            "content_hash": content_hash(item),
                        }
                        for item in raw.tool_artifacts
                    ],
                    "normalizations": list(raw.normalizations),
                    "response_hash": content_hash(raw.response_text) if raw.response_text else None,
                    "error": raw.error,
                },
            )
            results.append(
                TaskResult(task=raw.task, value=raw.value, message_id=message_id, error=raw.error)
            )
        results.extend(
            TaskResult(task=task, value=None, error="invocation budget exhausted")
            for task in skipped
        )
        self.events.publish(
            lane="activity",
            kind="wave_committed",
            wave_id=wave_id,
            phase=tasks[0].phase,
            round=tasks[0].round,
            payload={
                "accepted_count": sum(raw.value is not None for raw in raw_results),
                "failed_count": sum(raw.value is None for raw in raw_results),
                "skipped_count": len(skipped),
            },
        )
        return results

    async def _invoke(
        self,
        task: ProtocolTask,
        *,
        wave_id: str,
        task_index: int,
    ) -> _RawOutcome:
        profile = self.catalog.profile(task.member_id)
        member = self.organization.member(task.member_id)
        resolution = self.tool_policy.resolve(
            phase=task.phase,
            requested_toolsets=(
                *PHASE_TOOLSETS.get(task.phase, ()),
                *self.problem.user_tool_grants,
            ),
            user_grants=self.problem.user_tool_grants,
            # A CLI grant is not a human approval. The MVP does not dispatch
            # L3 tools; it only records an approval request for the final
            # advisory decision when a risk gate requires one.
            human_approvals=(),
        )
        enabled_toolsets = tuple(resolution.enabled_toolsets)
        self.audit.append(
            "tool_policy_resolved",
            {
                "invocation_id": self._invocation_id(task),
                "hsa_id": task.member_id,
                "phase": task.phase,
                "policy_id": resolution.policy_id,
                "requested_toolsets": list(resolution.requested_toolsets),
                "enabled_toolsets": list(enabled_toolsets),
                "rejected": [item.model_dump(mode="json") for item in resolution.rejected],
                "terminal_backend": resolution.terminal_backend.value,
            },
        )
        system_prompt = compile_soul_prompt(profile, member, self.organization, self.problem)
        user_prompt = compile_task_prompt(
            problem=self.problem,
            phase=task.phase,
            instruction=task.instruction,
            response_model=task.response_model,
            shared_context=task.shared_context,
            memory_snapshot=self.memory_snapshots.get(task.member_id, []),
            enabled_toolsets=list(enabled_toolsets),
        )
        invocation = AgentInvocation(
            invocation_id=self._invocation_id(task),
            hsa_id=task.member_id,
            phase=task.phase,
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            enabled_toolsets=enabled_toolsets,
            metadata={
                "response_type": task.response_model.__name__,
                "option_ids": [option.id for option in self.problem.options],
                "hard_constraint_ids": [
                    criterion.id for criterion in self.problem.criteria if criterion.hard_constraint
                ],
                "run_id": self.run_id,
                "organization_id": self.organization.id,
                "script_key": f"{task.phase}:{task.member_id}",
            },
            load_profile_context=self.organization.memory_policy.private_enabled,
        )
        runtime = self._runtime_for(task.member_id)
        try:
            async with self._semaphore:
                self.events.publish(
                    lane="activity",
                    kind="invocation_started",
                    wave_id=wave_id,
                    task_index=task_index,
                    phase=task.phase,
                    round=task.round,
                    hsa_id=task.member_id,
                    invocation_id=self._invocation_id(task),
                )
                response = await self._invoke_runtime(
                    runtime,
                    invocation,
                    wave_id=wave_id,
                    task_index=task_index,
                    task=task,
                )
        except Exception as exc:  # runtime boundary: one member failure becomes quorum input
            return self._publish_returned(
                _RawOutcome(
                    task=task,
                    value=None,
                    response_text=None,
                    backend=getattr(runtime, "name", type(runtime).__name__),
                    session_id=None,
                    enabled_toolsets=enabled_toolsets,
                    tool_events=(),
                    tool_artifacts=(),
                    normalizations=(),
                    error=str(exc),
                ),
                wave_id=wave_id,
                task_index=task_index,
            )

        normalizations: tuple[dict[str, str], ...] = ()
        try:
            parsed = extract_json_object(response.content)
            normalized = normalize_provenance_payload(parsed)
            normalizations = normalized.normalizations
            value = task.response_model.model_validate(normalized.payload)
            tool_artifact_ids = {
                artifact_id
                for item in response.tool_artifacts
                if (artifact_id := _tool_artifact_id(item)) is not None
            }
            _validate_references(
                value,
                task=task,
                problem=self.problem,
                principle_ids={item.id for item in profile.principles},
                visible_memory_ids={
                    str(item["id"])
                    for item in self.memory_snapshots.get(task.member_id, [])
                    if "id" in item
                },
                tool_artifact_ids=tool_artifact_ids,
            )
            return self._publish_returned(
                _RawOutcome(
                    task=task,
                    value=value,
                    response_text=response.content,
                    backend=response.runtime,
                    session_id=response.session_id,
                    enabled_toolsets=enabled_toolsets,
                    tool_events=tuple(getattr(response, "tool_events", ())),
                    tool_artifacts=tuple(getattr(response, "tool_artifacts", ())),
                    normalizations=normalizations,
                    error=None,
                ),
                wave_id=wave_id,
                task_index=task_index,
            )
        except (ValidationError, ValueError, StructuredOutputError) as exc:
            return self._publish_returned(
                _RawOutcome(
                    task=task,
                    value=None,
                    response_text=response.content,
                    backend=response.runtime,
                    session_id=response.session_id,
                    enabled_toolsets=enabled_toolsets,
                    tool_events=tuple(response.tool_events),
                    tool_artifacts=tuple(response.tool_artifacts),
                    normalizations=normalizations,
                    error=_structured_output_error(exc),
                ),
                wave_id=wave_id,
                task_index=task_index,
            )

    async def _invoke_runtime(
        self,
        runtime: AgentRuntime,
        invocation: AgentInvocation,
        *,
        wave_id: str,
        task_index: int,
        task: ProtocolTask,
    ):
        """Use runtime streaming only while an observer is actually attached."""

        if not self.events.has_subscribers:
            return await runtime.invoke(invocation)
        try:
            parameters = inspect.signature(runtime.invoke).parameters.values()
        except (TypeError, ValueError):
            parameters = ()
        supports_events = any(
            parameter.name == "event_sink" or parameter.kind is inspect.Parameter.VAR_KEYWORD
            for parameter in parameters
        )
        if not supports_events:
            return await runtime.invoke(invocation)

        def publish(event: RuntimeStreamEvent) -> None:
            self._publish_runtime_stream_event(
                event,
                wave_id=wave_id,
                task_index=task_index,
                task=task,
            )

        return await runtime.invoke(invocation, event_sink=publish)

    def _publish_runtime_stream_event(
        self,
        event: RuntimeStreamEvent,
        *,
        wave_id: str,
        task_index: int,
        task: ProtocolTask,
    ) -> None:
        kind_by_type = {
            "bridge_ready": "runtime_stream_ready",
            "response_delta": "output_delta",
            "tool_started": "tool_started",
            "tool_completed": "tool_completed",
            "response_completed": "response_received",
        }
        payload = {**event.payload, "runtime_sequence": event.sequence}
        if event.event_type == "response_delta":
            payload["text"] = event.content or ""
        elif event.event_type == "response_completed" and event.content is not None:
            payload["content_hash"] = content_hash(event.content)
            payload["content_size"] = len(event.content.encode("utf-8"))
        invocation_id = self._invocation_id(task)
        self.events.publish(
            lane="activity",
            kind=kind_by_type[event.event_type],
            visibility="privileged",
            wave_id=wave_id,
            task_index=task_index,
            phase=task.phase,
            round=task.round,
            hsa_id=task.member_id,
            invocation_id=invocation_id,
            payload={**payload, "invocation_id": invocation_id},
            retain=event.event_type != "response_delta",
        )

    def _publish_returned(
        self,
        outcome: _RawOutcome,
        *,
        wave_id: str,
        task_index: int,
    ) -> _RawOutcome:
        common = {
            "lane": "activity",
            "wave_id": wave_id,
            "task_index": task_index,
            "phase": outcome.task.phase,
            "round": outcome.task.round,
            "hsa_id": outcome.task.member_id,
            "invocation_id": self._invocation_id(outcome.task),
        }
        if outcome.value is not None:
            self.events.publish(
                **common,
                kind="agent_output_accepted",
                visibility="privileged",
                payload={
                    "backend": outcome.backend,
                    "value": outcome.value.model_dump(mode="json"),
                },
            )
        else:
            self.events.publish(
                **common,
                kind="agent_output_rejected",
                visibility="privileged",
                payload={
                    "backend": outcome.backend,
                    "error": outcome.error or "unknown output rejection",
                },
            )
        self.events.publish(
            **common,
            kind="invocation_returned",
            payload={
                "accepted": outcome.value is not None,
                "backend": outcome.backend,
            },
        )
        return outcome

    def _runtime_for(self, member_id: str) -> AgentRuntime:
        if isinstance(self.runtimes, Mapping):
            try:
                return self.runtimes[member_id]
            except KeyError as exc:
                raise ProtocolError(f"no runtime configured for HSA {member_id}") from exc
        return self.runtimes

    def _record_message(self, task: ProtocolTask, value: BaseModel) -> str:
        ordinal = len(self.messages) + 1
        message_id = f"{self.run_id}-message-{ordinal:04d}"
        payload = value.model_dump(mode="json")
        message = MessageEnvelope(
            id=message_id,
            run_id=self.run_id,
            ordinal=ordinal,
            round=task.round,
            phase=task.phase,
            sender_id=task.member_id,
            kind=task.kind,
            visibility=task.visibility,
            payload=payload,
            parent_message_ids=list(task.parent_message_ids),
            profile_version=self.catalog.profile(task.member_id).version,
            content_hash=content_hash(payload),
        )
        self.messages.append(message)
        self.audit.append("message_accepted", message.model_dump(mode="json"))
        return message_id

    def _invocation_id(self, task: ProtocolTask) -> str:
        return f"{self.run_id}:{task.member_id}:{task.phase}:r{task.round}"


class ThinkTank:
    def __init__(
        self,
        *,
        catalog: Catalog,
        runtimes: AgentRuntime | Mapping[str, AgentRuntime],
        memory_store: InstitutionalMemoryStore | None = None,
        approval_store: ApprovalStore | None = None,
        run_store: LocalRunStore | None = None,
        tool_policy_factory: Callable[[str], ToolPolicy] = get_tool_policy,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.catalog = catalog
        self.runtimes = runtimes
        self.memory_store = memory_store
        self.approval_store = approval_store
        self.run_store = run_store
        self.tool_policy_factory = tool_policy_factory
        self.clock = clock

    def start_run(
        self,
        problem: DecisionProblem,
        *,
        organization_id: str | None = AUTO_ORGANIZATION_ID,
        meeting_selection: MeetingSelection | None = None,
        user_id: str | None = None,
        persist: bool = True,
    ) -> RunHandle:
        """Start one run and return a detachable event/result handle."""

        if persist and self.run_store is None:
            raise ValueError("persist=True requires a durable LocalRunStore")
        run_id = f"run-{uuid4().hex[:16]}"
        events = RunEventStream(run_id, clock=self.clock)
        problem_snapshot = problem.model_copy(deep=True)
        selection_snapshot = (
            meeting_selection.model_copy(deep=True) if meeting_selection is not None else None
        )
        events.publish(
            lane="activity",
            kind="run_created",
            payload={"decision_id": problem_snapshot.id, "persisted": persist},
        )
        task = asyncio.get_running_loop().create_task(
            self._run_with_events(
                problem_snapshot,
                organization_id=organization_id,
                meeting_selection=selection_snapshot,
                user_id=user_id,
                persist=persist,
                run_id=run_id,
                events=events,
            ),
            name=f"hsa-thinktank:{run_id}",
        )
        return RunHandle(run_id=run_id, stream=events, task=task, persisted=persist)

    async def decide(
        self,
        problem: DecisionProblem,
        *,
        organization_id: str | None = AUTO_ORGANIZATION_ID,
        meeting_selection: MeetingSelection | None = None,
        user_id: str | None = None,
        persist: bool = True,
    ) -> DecisionReport:
        """Compatibility wrapper around :meth:`start_run`."""

        handle = self.start_run(
            problem,
            organization_id=organization_id,
            meeting_selection=meeting_selection,
            user_id=user_id,
            persist=persist,
        )
        try:
            return await handle._task
        except asyncio.CancelledError:
            handle.cancel()
            raise

    async def _run_with_events(
        self,
        problem: DecisionProblem,
        *,
        organization_id: str | None,
        meeting_selection: MeetingSelection | None,
        user_id: str | None,
        persist: bool,
        run_id: str,
        events: RunEventStream,
    ) -> DecisionReport:
        try:
            report = await self._decide_run(
                problem,
                organization_id=organization_id,
                meeting_selection=meeting_selection,
                user_id=user_id,
                persist=persist,
                run_id=run_id,
                events=events,
            )
        except asyncio.CancelledError as exc:
            publish_run_terminal(events, error=exc, persisted=persist)
            raise
        except BaseException as exc:
            publish_run_terminal(events, error=exc, persisted=persist)
            raise
        publish_run_terminal(events, report=report, persisted=persist)
        return report

    async def _decide_run(
        self,
        problem: DecisionProblem,
        *,
        organization_id: str | None,
        meeting_selection: MeetingSelection | None,
        user_id: str | None,
        persist: bool,
        run_id: str,
        events: RunEventStream,
    ) -> DecisionReport:
        expected_selection = MeetingRouter(self.catalog).select(
            problem,
            requested_organization_id=organization_id,
        )
        if (
            meeting_selection is not None
            and meeting_selection.fingerprint != expected_selection.fingerprint
        ):
            raise ValueError("meeting selection does not match the current routing policy")
        meeting_selection = meeting_selection or expected_selection
        base_organization = self.catalog.organization(meeting_selection.organization_id)
        organization = meeting_selection.effective_organization
        started_at = self.clock()
        request_snapshot = problem.model_copy(deep=True)
        request_snapshot_hash = problem.snapshot_hash
        audit = PublishedAuditTrail(run_id, stream=events, clock=self.clock)
        audit.append(
            "run_started",
            {
                "decision_id": problem.id,
                "organization_id": base_organization.id,
                "organization_version": base_organization.version,
                "request_snapshot_hash": request_snapshot_hash,
                "request_snapshot": request_snapshot.model_dump(mode="json"),
            },
        )
        audit.append(
            "meeting_selected",
            meeting_selection.model_dump(mode="json"),
        )
        memory_snapshots, memory_snapshot_hash = self._freeze_memories(
            organization, user_id=user_id
        )
        native_fingerprints = {
            member.hsa_id: (
                native_memory_fingerprint(member.hsa_id)
                if organization.memory_policy.private_enabled
                else content_hash({"profile_context": "disabled"})
            )
            for member in organization.members
        }
        audit.append(
            "memory_frozen",
            {
                "snapshot_hash": memory_snapshot_hash,
                "native_memory_fingerprints": native_fingerprints,
            },
        )
        tool_policy = self.tool_policy_factory(organization.tool_policy_id)
        if not organization.memory_policy.private_enabled:
            tool_policy = tool_policy.model_copy(
                update={"memory_enabled": False, "session_search_enabled": False}
            )
        context = _RunContext(
            run_id=run_id,
            problem=problem,
            organization=organization,
            catalog=self.catalog,
            runtimes=self.runtimes,
            memory_snapshots=memory_snapshots,
            tool_policy=tool_policy,
            audit=audit,
            events=events,
        )
        if not problem.options:
            generated = await context.invoke_wave(
                [
                    ProtocolTask(
                        member_id=organization.chair_id,
                        phase="option_generation",
                        round=0,
                        response_model=GeneratedOptions,
                        instruction=(
                            "生成 2 到 6 个互斥、可执行、覆盖保守选项的候选方案。不要在此阶段"
                            "选择胜者；方案 ID 使用简短 kebab-case。"
                        ),
                        kind="option_set",
                    )
                ]
            )
            value = generated[0].value if generated else None
            if not isinstance(value, GeneratedOptions):
                raise ProtocolError("failed to generate a valid frozen option set")
            problem = problem.model_copy(update={"options": value.options})
            context.problem = problem
            audit.append(
                "options_frozen",
                {"options": [option.model_dump(mode="json") for option in problem.options]},
            )
        else:
            audit.append(
                "options_frozen",
                {"options": [option.model_dump(mode="json") for option in problem.options]},
            )

        outcome = await protocol_for(organization.protocol).run(context)
        if context.budget_exhausted and outcome.status_hint is None:
            outcome.status_hint = "budget_exhausted"
        events.publish(
            lane="activity",
            kind="aggregation_started",
            payload={"protocol": organization.protocol},
        )
        decision = aggregate(problem, organization, outcome)
        native_fingerprints_after = {
            member.hsa_id: (
                native_memory_fingerprint(member.hsa_id)
                if organization.memory_policy.private_enabled
                else content_hash({"profile_context": "disabled"})
            )
            for member in organization.members
        }
        native_memory_changed = sorted(
            hsa_id
            for hsa_id, before in native_fingerprints.items()
            if native_fingerprints_after[hsa_id] != before
        )
        volatile_profile_grants = sorted(
            {"memory", "session_search"}.intersection(problem.user_tool_grants)
        )
        audit.append(
            "native_memory_checked",
            {
                "fingerprints_after": native_fingerprints_after,
                "changed_hsa_ids": native_memory_changed,
                "volatile_profile_grants": volatile_profile_grants,
            },
        )
        if decision.status == "decided" and (native_memory_changed or volatile_profile_grants):
            reason = (
                "native profile memory changed during the run"
                if native_memory_changed
                else "mutable profile history tools were explicitly enabled"
            )
            decision = replace(decision, status="needs_human", status_reason=reason)

        profile_fingerprints = {
            member.hsa_id: self.catalog.profile(member.hsa_id).fingerprint
            for member in organization.members
        }
        memory_store_id = self.memory_store.store_id if self.memory_store is not None else None
        approval_store_id = (
            self.approval_store.store_id if self.approval_store is not None else None
        )
        decision_binding = {
            "run_id": run_id,
            "decision_id": problem.id,
            "request_snapshot_hash": request_snapshot_hash,
            "frozen_problem_hash": problem.snapshot_hash,
            "memory_snapshot_hash": memory_snapshot_hash,
            "memory_store_id": memory_store_id,
            "approval_store_id": approval_store_id,
            "native_memory_fingerprints": native_fingerprints,
            "native_memory_fingerprints_after": native_fingerprints_after,
            "meeting_selection": meeting_selection.model_dump(mode="json"),
            "organization_id": base_organization.id,
            "organization_fingerprint": base_organization.fingerprint,
            "shared_memory_write_mode": organization.memory_policy.shared_write_mode,
            "protocol_name": organization.protocol,
            "status": decision.status,
            "status_reason": decision.status_reason,
            "selected_option_id": decision.selected_option_id,
            "option_scores": decision.option_scores,
            "confidence": decision.confidence,
            "unresolved_risks": decision.unresolved_risks,
            "profile_fingerprints": profile_fingerprints,
        }
        decision_binding_hash = content_hash(decision_binding)
        decision_event = audit.append(
            "decision_aggregated",
            {
                "status": decision.status,
                "selected_option_id": decision.selected_option_id,
                "option_scores": decision.option_scores,
                "confidence": decision.confidence,
                "status_reason": decision.status_reason,
            },
        )
        approval_plan = self._plan_approval_if_needed(
            run_id,
            base_organization.id,
            decision.status,
            decision_binding,
            decision_binding_hash,
            audit,
            allow_writes=persist,
        )
        memory_plan = self._plan_decision_memory(
            run_id=run_id,
            organization_id=base_organization.id,
            problem=problem,
            status=decision.status,
            selected_option_id=decision.selected_option_id,
            confidence=decision.confidence,
            write_mode=organization.memory_policy.shared_write_mode,
            source_event_id=decision_event.id,
            decision_binding_hash=decision_binding_hash,
            audit=audit,
            allow_writes=persist,
        )
        audit.append(
            "decision_report_ready",
            {
                "status": decision.status,
                "memory_outbox": memory_plan is not None,
                "approval_outbox": approval_plan is not None,
            },
        )
        selected_description = next(
            (
                option.description
                for option in problem.options
                if option.id == decision.selected_option_id
            ),
            None,
        )
        report = DecisionReport(
            run_id=run_id,
            decision_id=problem.id,
            request_snapshot=request_snapshot,
            frozen_problem=problem,
            request_snapshot_hash=request_snapshot_hash,
            frozen_problem_hash=problem.snapshot_hash,
            memory_snapshot_hash=memory_snapshot_hash,
            memory_store_id=memory_store_id,
            approval_store_id=approval_store_id,
            native_memory_fingerprints=native_fingerprints,
            native_memory_fingerprints_after=native_fingerprints_after,
            native_memory_changed=native_memory_changed,
            meeting_selection=meeting_selection,
            organization_id=base_organization.id,
            organization_version=base_organization.version,
            organization_fingerprint=base_organization.fingerprint,
            shared_memory_write_mode=organization.memory_policy.shared_write_mode,
            protocol_name=organization.protocol,
            status=decision.status,
            status_reason=decision.status_reason,
            selected_option_id=decision.selected_option_id,
            selected_option=selected_description,
            option_scores=decision.option_scores,
            confidence=decision.confidence,
            raw_member_count=len(outcome.ballots),
            correlation_group_count=decision.correlation_group_count,
            effective_sample_size=decision.effective_sample_size,
            successful_member_ids=sorted(outcome.successful_member_ids),
            rationale_claims=decision.rationale_claims,
            assumptions=decision.assumptions,
            unresolved_risks=decision.unresolved_risks,
            dissent=decision.dissent,
            next_actions=decision.next_actions,
            memory_ids=decision.memory_ids,
            tool_artifact_ids=decision.tool_artifact_ids,
            approval_ids=[approval_plan.request.id] if approval_plan is not None else [],
            decision_binding_hash=decision_binding_hash,
            chair_override=decision.chair_override,
            profile_fingerprints=profile_fingerprints,
            runtime_calls=context.runtime_calls,
            messages=context.messages,
            audit_events=audit.events,
            trace_root_hash=audit.root_hash,
            started_at=started_at,
            completed_at=self.clock(),
        )
        if persist:
            assert self.run_store is not None
            outbox = RunOutbox(
                run_id=run_id,
                report_hash=content_hash(report),
                decision_binding_hash=decision_binding_hash,
                memory_operation=memory_plan,
                approval_operation=approval_plan,
            )
            self.run_store.save(report, outbox=outbox)
            events.publish(
                lane="control",
                kind="bundle_persisted",
                payload={"report_hash": content_hash(report)},
            )
            self._execute_approval_plan(approval_plan)
            self._execute_memory_plan(memory_plan)
            self.run_store.mark_complete(
                report,
                memory_store=self.memory_store,
                approval_store=self.approval_store,
            )
            events.publish(
                lane="control",
                kind="completion_written",
                payload={"trace_root_hash": report.trace_root_hash},
            )
        return report

    def _freeze_memories(self, organization, *, user_id: str | None):
        snapshots: dict[str, list[dict[str, Any]]] = {}
        hashes: dict[str, str] = {}
        for member in organization.members:
            if self.memory_store is None:
                records: list[dict[str, Any]] = []
                snapshot_hash = content_hash(records)
            else:
                snapshot = self.memory_store.snapshot(
                    requester_id=member.hsa_id,
                    organization_id=organization.id
                    if organization.memory_policy.shared_read
                    else None,
                    user_id=user_id,
                    include_private=organization.memory_policy.private_enabled,
                )
                records = [record.model_dump(mode="json") for record in snapshot.records]
                snapshot_hash = snapshot.snapshot_hash
            snapshots[member.hsa_id] = records
            hashes[member.hsa_id] = snapshot_hash
        return snapshots, content_hash(hashes)

    def _plan_approval_if_needed(
        self,
        run_id: str,
        organization_id: str,
        status: str,
        decision_binding: dict[str, Any],
        decision_binding_hash: str,
        audit: AuditTrail,
        *,
        allow_writes: bool,
    ) -> ApprovalOutboxOperation | None:
        if status != "needs_human" or self.approval_store is None:
            return None
        if not allow_writes:
            audit.append(
                "approval_skipped",
                {"reason": "non_persistent_run", "status": status},
            )
            return None
        request = ApprovalRequest(
            idempotency_key=f"decision:{run_id}",
            level=ApprovalLevel.L3,
            action="approve_decision",
            subject_id=run_id,
            organization_id=organization_id,
            requested_by="hsa-orchestrator",
            payload={
                "decision_binding_hash": decision_binding_hash,
                "decision_binding": decision_binding,
            },
        )
        plan = ApprovalOutboxOperation(
            operation_id=f"approval:{run_id}",
            run_id=run_id,
            approval_store_id=self.approval_store.store_id,
            request=request,
        )
        audit.append(
            "approval_planned",
            {
                "operation_id": plan.operation_id,
                "approval_id": request.id,
                "approval_store_id": plan.approval_store_id,
                "decision_binding_hash": decision_binding_hash,
            },
        )
        return plan

    def _plan_decision_memory(
        self,
        *,
        run_id: str,
        organization_id: str,
        problem: DecisionProblem,
        status: str,
        selected_option_id: str | None,
        confidence: float,
        write_mode: str,
        source_event_id: str,
        decision_binding_hash: str,
        audit: AuditTrail,
        allow_writes: bool,
    ) -> MemoryOutboxOperation | None:
        if self.memory_store is None or write_mode == "disabled":
            audit.append(
                "decision_memory_skipped",
                {"reason": "store_or_policy_disabled", "status": status},
            )
            return None
        if not allow_writes:
            audit.append(
                "decision_memory_skipped",
                {"reason": "non_persistent_run", "status": status},
            )
            return None
        if write_mode == "final_decision_only" and status != "decided":
            audit.append(
                "decision_memory_skipped",
                {"reason": "decision_not_final", "status": status},
            )
            return None
        candidate = MemoryCandidate(
            id=f"memory-{run_id}",
            owner_id="hsa-orchestrator",
            organization_id=organization_id,
            scope=MemoryScope.ORGANIZATION,
            content=canonical_json(
                {
                    "decision_id": problem.id,
                    "question": problem.question,
                    "status": status,
                    "selected_option_id": selected_option_id,
                    "confidence": confidence,
                    "decision_binding_hash": decision_binding_hash,
                }
            ),
            source_event_ids=[source_event_id],
            confidence=confidence,
        )
        action = "commit_final" if write_mode == "final_decision_only" else "stage"
        plan = MemoryOutboxOperation(
            operation_id=f"memory:{run_id}",
            run_id=run_id,
            memory_store_id=self.memory_store.store_id,
            action=action,
            candidate=candidate,
            decision_event_id=source_event_id,
            decision_binding_hash=decision_binding_hash,
        )
        audit.append(
            "decision_memory_planned",
            {
                "action": action,
                "memory_id": candidate.id,
                "content_hash": candidate.content_hash,
                "source_event_id": source_event_id,
                "operation_id": plan.operation_id,
                "memory_store_id": plan.memory_store_id,
            },
        )
        return plan

    def _execute_memory_plan(
        self,
        plan: MemoryOutboxOperation | None,
    ) -> None:
        if plan is None:
            return
        if self.memory_store is None or self.memory_store.store_id != plan.memory_store_id:
            raise RuntimeError("memory outbox exists without a memory store")
        if plan.action == "commit_final":
            self.memory_store.commit_decision_memory(
                plan.candidate,
                decision_event_id=plan.decision_event_id,
                committed_by="hsa-orchestrator",
                decision_is_final=True,
            )
        elif plan.action == "stage":
            self.memory_store.stage_candidate(
                plan.candidate,
                actor_id="hsa-orchestrator",
                origin="decision_outbox",
            )
        else:
            raise ValueError(f"unknown memory outbox action: {plan.action}")

    def _execute_approval_plan(self, plan: ApprovalOutboxOperation | None) -> None:
        if plan is None:
            return
        if self.approval_store is None or self.approval_store.store_id != (plan.approval_store_id):
            raise RuntimeError("approval outbox exists without its bound approval store")
        self.approval_store.pending(plan.request)


def _structured_output_error(error: Exception) -> str:
    if isinstance(error, ValidationError):
        try:
            errors = error.errors(include_input=False, include_url=False)
        except TypeError:  # Pydantic 2.8 compatibility
            errors = error.errors()
        error_types = sorted(
            {
                value
                for item in errors
                if isinstance((value := item.get("type")), str)
                and value.replace("_", "").replace(".", "").isalnum()
            }
        )
        rendered_types = ",".join(error_types[:8]) or "validation_error"
        return (
            "structured output rejected: validation failed; "
            f"error_count={len(errors)}; types={rendered_types}"
        )
    detail = redact_sensitive(str(error)).strip() or type(error).__name__
    return f"structured output rejected: {detail}"


def _validate_references(
    value: BaseModel,
    *,
    task: ProtocolTask,
    problem: DecisionProblem,
    principle_ids: set[str],
    visible_memory_ids: set[str],
    tool_artifact_ids: set[str],
) -> None:
    option_ids = {option.id for option in problem.options}
    criterion_ids = {criterion.id for criterion in problem.criteria}
    hard_constraint_ids = {
        criterion.id for criterion in problem.criteria if criterion.hard_constraint
    }
    if isinstance(value, GeneratedOptions):
        _validate_claim_references(
            value.claims,
            principle_ids=principle_ids,
            evidence_ids={item.id for item in problem.evidence},
            visible_memory_ids=visible_memory_ids,
            tool_artifact_ids=tool_artifact_ids,
        )
    elif isinstance(value, Contribution):
        validate_option_references(
            value,
            option_ids,
            criterion_ids,
            hard_constraint_ids,
        )
        _validate_claim_references(
            value.claims,
            principle_ids=principle_ids,
            evidence_ids={item.id for item in problem.evidence},
            visible_memory_ids=visible_memory_ids,
            tool_artifact_ids=tool_artifact_ids,
        )
    elif isinstance(value, RedTeamRebuttal):
        validate_option_references(
            value.revised_ballot,
            option_ids,
            criterion_ids,
            hard_constraint_ids,
        )
        _validate_claim_references(
            value.revised_ballot.claims,
            principle_ids=principle_ids,
            evidence_ids={item.id for item in problem.evidence},
            visible_memory_ids=visible_memory_ids,
            tool_artifact_ids=tool_artifact_ids,
        )
        expected_attack_ids = {
            str(item["attack_id"])
            for item in task.shared_context.get("attacks", [])
            if isinstance(item, dict) and "attack_id" in item
        }
        supplied_attack_ids = {item.attack_id for item in value.dispositions}
        if supplied_attack_ids != expected_attack_ids:
            raise ValueError(
                "rebuttal dispositions must contain exactly the received attacks; "
                f"unknown_count={len(supplied_attack_ids - expected_attack_ids)}, "
                f"missing_count={len(expected_attack_ids - supplied_attack_ids)}"
            )
    elif isinstance(value, ExecutiveDecision):
        validate_option_references(
            value.ballot,
            option_ids,
            criterion_ids,
            hard_constraint_ids,
        )
        _validate_claim_references(
            value.ballot.claims,
            principle_ids=principle_ids,
            evidence_ids={item.id for item in problem.evidence},
            visible_memory_ids=visible_memory_ids,
            tool_artifact_ids=tool_artifact_ids,
        )
    elif isinstance(value, RedTeamCritique):
        if value.strongest_alternative_id not in option_ids:
            raise ValueError("strongest_alternative_id is not frozen")
        for attack in value.attacks:
            _validate_attack(attack, option_ids)
            unknown_evidence = set(attack.evidence_ids) - {item.id for item in problem.evidence}
            if unknown_evidence:
                raise ValueError(
                    f"attack references unknown evidence: count={len(unknown_evidence)}"
                )
            unknown_artifacts = set(attack.tool_artifact_ids) - tool_artifact_ids
            if unknown_artifacts:
                raise ValueError(
                    f"attack references unavailable tool artifacts: count={len(unknown_artifacts)}"
                )


def _validate_attack(attack: Attack, option_ids: set[str]) -> None:
    if attack.option_id not in option_ids:
        raise ValueError("attack references an unknown option")


def _validate_claim_references(
    claims: list[RationaleClaim],
    *,
    principle_ids: set[str],
    evidence_ids: set[str],
    visible_memory_ids: set[str],
    tool_artifact_ids: set[str],
) -> None:
    for claim in claims:
        unknown_principles = set(claim.principle_ids) - principle_ids
        unknown_evidence = set(claim.evidence_ids) - evidence_ids
        unknown_memory = set(claim.memory_ids) - visible_memory_ids
        unknown_artifacts = set(claim.tool_artifact_ids) - tool_artifact_ids
        if unknown_principles:
            raise ValueError(
                f"claim references unknown principles: count={len(unknown_principles)}"
            )
        if unknown_evidence:
            raise ValueError(f"claim references unknown evidence: count={len(unknown_evidence)}")
        if unknown_memory:
            raise ValueError(
                f"claim references memory outside the frozen snapshot: count={len(unknown_memory)}"
            )
        if unknown_artifacts:
            raise ValueError(
                f"claim references unavailable tool artifacts: count={len(unknown_artifacts)}"
            )


def _tool_artifact_id(artifact: dict[str, Any]) -> str | None:
    value = artifact.get("id", artifact.get("artifact_id"))
    return value if isinstance(value, str) and value else None
