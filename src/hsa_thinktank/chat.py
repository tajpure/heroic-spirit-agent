"""Interactive chat-session adapter around the auditable meeting orchestrator."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Sequence
from typing import Any

from .chat_store import ChatContextItem, LocalChatStore
from .errors import CatalogError
from .models import DecisionProblem, DecisionReport
from .orchestrator import ThinkTank
from .routing import AUTO_ORGANIZATION_ID


def render_chat_context(items: list[ChatContextItem]) -> str:
    """Render the explicit, user-inspectable context copied into a new run."""

    if not items:
        return ""
    sections = [
        "以下是同一 HSA 对话中保留的既往内容。它可能已经过时，必须结合当前提问重新判断；"
        "不得把历史结论当作不可推翻的事实。"
    ]
    for index, item in enumerate(items, start=1):
        if item.kind == "user_message":
            sections.append(f"[{index}] 历史提问：\n{item.content}")
            continue
        try:
            summary = json.loads(item.content)
        except json.JSONDecodeError:
            summary = {"summary": item.content}
        sections.append(_render_confirmed_decision(index, summary))
    return "\n\n".join(sections)


def _option_labels(summary: dict[str, Any]) -> dict[str, str]:
    labels: dict[str, str] = {}
    options = summary.get("options")
    if not isinstance(options, list):
        return labels
    for option in options:
        if not isinstance(option, dict):
            continue
        option_id = option.get("id")
        description = option.get("description")
        if isinstance(option_id, str) and isinstance(description, str) and description.strip():
            labels[option_id] = description.strip()
    return labels


def _choice(summary: dict[str, Any], option_labels: dict[str, str]) -> str | None:
    selected = summary.get("selected_option")
    if isinstance(selected, str) and selected.strip():
        return selected.strip()
    selected_id = summary.get("selected_option_id")
    return option_labels.get(selected_id) if isinstance(selected_id, str) else None


def _outcome_line(summary: dict[str, Any], option_labels: dict[str, str]) -> str:
    status = summary.get("status")
    selected = _choice(summary, option_labels)
    if status == "decided" and selected:
        return f"结论：{selected}"
    if status == "needs_human" and selected:
        return f"建议（待确认）：{selected}"
    if status == "inconclusive" and selected:
        return f"暂未形成一致结论；当时的讨论倾向「{selected}」"
    if status == "inconclusive":
        return "暂未形成清晰结论"
    if status == "rejected":
        return "结论：当前不建议推进"
    if status == "budget_exhausted":
        return "讨论尚未完成，未形成结论"
    return "未形成可展示的结论"


def _content_risk(value: Any, option_labels: dict[str, str]) -> str:
    text = str(value).strip()
    severity, separator, statement = text.partition(": ")
    label = {
        "low": "较低风险",
        "medium": "需注意",
        "high": "重要风险",
        "critical": "关键风险",
    }.get(severity)
    body = statement if separator and label else text
    option_id, violates, _references = body.partition(" violates ")
    if violates:
        option = option_labels.get(option_id, "一个候选方案")
        return f"「{option}」未满足至少一项硬性条件"
    return f"{label}：{body}" if label else body


def _content_dissent(
    value: Any,
    option_labels: dict[str, str],
    display_name: Callable[[str], str] | None = None,
) -> str:
    member_id, separator, remainder = str(value).partition(" prefers ")
    option_id, detail_separator, claim = remainder.partition(": ")
    if not separator or not detail_separator:
        return "还有参会者持保留意见"
    member = "一位参会者"
    if display_name is not None:
        try:
            resolved = display_name(member_id)
        except (KeyError, ValueError):
            resolved = ""
        if resolved:
            member = resolved
    option = option_labels.get(option_id, "另一方案")
    return f"{member}倾向「{option}」：{claim}"


def _render_confirmed_decision(index: int, summary: dict[str, Any]) -> str:
    """Render semantic history without router-biasing schema field names."""

    labels = _option_labels(summary)
    lines = [f"[{index}] 已确认的历史讨论"]
    question = summary.get("question")
    if question:
        lines.append(f"历史问题：{question}")
    lines.append("历史" + _outcome_line(summary, labels))

    options = summary.get("options")
    if isinstance(options, list) and options:
        rendered_options = list(labels.values())
        if rendered_options:
            lines.append("当时方案：" + "；".join(rendered_options))

    claims = summary.get("rationale_claims")
    if isinstance(claims, list):
        rendered_claims = [
            str(item.get("claim"))
            for item in claims
            if isinstance(item, dict) and item.get("claim")
        ]
        if rendered_claims:
            lines.append("已确认依据：" + "；".join(rendered_claims))

    assumptions = summary.get("assumptions")
    if isinstance(assumptions, list) and assumptions:
        lines.append("仍需验证的假设：" + "；".join(str(value) for value in assumptions))
    risks = summary.get("unresolved_risks")
    if isinstance(risks, list) and risks:
        lines.append(
            "尚未解决的问题：" + "；".join(_content_risk(value, labels) for value in risks)
        )
    dissent = summary.get("dissent")
    if isinstance(dissent, list) and dissent:
        lines.append(
            "保留意见："
            + "；".join(_content_dissent(value, labels) for value in dissent)
        )
    next_actions = summary.get("next_actions")
    if isinstance(next_actions, list) and next_actions:
        lines.append("后续动作：" + "；".join(str(value) for value in next_actions))
    return "\n".join(lines)


def preview_chat_context(
    items: list[ChatContextItem],
    *,
    display_name: Callable[[str], str] | None = None,
) -> str:
    if not items:
        return ""
    lines: list[str] = []
    for item in items:
        if item.kind == "user_message":
            lines.append(f"- 用户：{item.content}")
            continue
        try:
            summary = json.loads(item.content)
        except json.JSONDecodeError:
            lines.append("- 上一轮讨论已完成，但摘要无法展示")
            continue
        labels = _option_labels(summary)
        question = str(summary.get("question") or "").strip()
        lines.append(f"- 历史问题：{question}" if question else "- 一次已完成的历史讨论")
        lines.append(f"  - {_outcome_line(summary, labels)}")
        claims = summary.get("rationale_claims")
        rendered_claims = [
            str(claim.get("claim"))
            for claim in claims or []
            if isinstance(claim, dict) and claim.get("claim")
        ]
        if rendered_claims:
            lines.append("  - 依据：" + "；".join(rendered_claims))
        dissent = summary.get("dissent")
        if isinstance(dissent, list) and dissent:
            lines.append(
                "  - 保留意见："
                + "；".join(
                    _content_dissent(value, labels, display_name) for value in dissent
                )
            )
        risks = summary.get("unresolved_risks")
        if isinstance(risks, list) and risks:
            lines.append(
                "  - 尚未解决："
                + "；".join(_content_risk(value, labels) for value in risks)
            )
        next_actions = summary.get("next_actions")
        if isinstance(next_actions, list) and next_actions:
            lines.append("  - 下一步：" + "；".join(str(value) for value in next_actions))
    return "\n".join(lines)


class ThinkTankChatDriver:
    """Turn user chat messages into immutable HSA decision runs."""

    def __init__(
        self,
        *,
        tank: ThinkTank,
        chat_store: LocalChatStore,
        session_id: str | None = None,
        organization_id: str = AUTO_ORGANIZATION_ID,
        risk_tier: str = "medium",
        user_id: str | None = None,
        persist_runs: bool = True,
        runtime_mode: str = "hermes-auto-stream",
        tool_grants: Sequence[str] = (),
        max_parallel: int = 4,
    ) -> None:
        self.tank = tank
        self.chat_store = chat_store
        if session_id is None:
            session = chat_store.new(title="HSA Think Tank")
        else:
            session = chat_store.load(session_id)
        self.session_id = session.id
        self.organization_id = organization_id
        self.risk_tier = risk_tier
        self.user_id = user_id
        self.persist_runs = persist_runs
        self.runtime_mode = runtime_mode
        self.tool_grants = tuple(dict.fromkeys(tool_grants))
        self.max_parallel = max_parallel
        self._active_handle: Any | None = None
        self._lock = asyncio.Lock()

    async def run_turn(
        self,
        question: str,
        emit: Callable[[Any], None],
    ) -> DecisionReport:
        """Run one frozen meeting while forwarding observer events to the TUI."""

        async with self._lock:
            if self._active_handle is not None:
                raise RuntimeError("a meeting is already running in this chat session")
            previous_context = self.chat_store.build_context(self.session_id)
            self.chat_store.append_user(self.session_id, question)
            problem = DecisionProblem(
                question=question,
                context=render_chat_context(previous_context),
                risk_tier=self.risk_tier,
                user_tool_grants=list(self.tool_grants),
                max_parallel=self.max_parallel,
            )
            handle = self.tank.start_run(
                problem,
                organization_id=self.organization_id,
                user_id=self.user_id,
                persist=self.persist_runs,
            )
            self._active_handle = handle
            subscription = handle.subscribe()
            pump = asyncio.create_task(self._pump_events(subscription, emit))
            try:
                report = await handle.result()
                await pump
            except asyncio.CancelledError:
                # ``RunHandle.result`` is shielded so that an incidental waiter
                # cancellation cannot kill a run.  This driver, however, owns the
                # run: if its turn task is cancelled it must explicitly cancel and
                # join the underlying Hermes work before releasing the session lock.
                handle.cancel()
                try:
                    await handle.result()
                except BaseException:
                    pass
                if not pump.done():
                    pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
                raise
            except BaseException:
                if not pump.done():
                    pump.cancel()
                await asyncio.gather(pump, return_exceptions=True)
                raise
            finally:
                await subscription.aclose()
                self._active_handle = None
            self.chat_store.append_decision(self.session_id, report)
            return report

    @staticmethod
    async def _pump_events(subscription, emit: Callable[[Any], None]) -> None:
        async for event in subscription:
            emit(event)

    async def cancel(self) -> None:
        handle = self._active_handle
        if handle is None:
            return
        handle.cancel()
        try:
            await handle.result()
        except asyncio.CancelledError:
            pass

    async def new_session(self) -> str:
        if self._active_handle is not None:
            raise RuntimeError("cannot create a new session while a meeting is running")
        session = self.chat_store.new(title="HSA Think Tank")
        self.session_id = session.id
        return session.id

    def context_preview(self) -> str:
        def display_name(hsa_id: str) -> str:
            try:
                return self.tank.catalog.profile(hsa_id).display_name
            except CatalogError:
                return "一位参会者"

        return preview_chat_context(
            self.chat_store.build_context(self.session_id),
            display_name=display_name,
        )


__all__ = ["ThinkTankChatDriver", "preview_chat_context", "render_chat_context"]
