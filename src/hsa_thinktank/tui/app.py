"""Textual application for interactive, observable HSA meetings."""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Sequence
from typing import Any, Protocol

from rich.markup import escape
from rich.markdown import Markdown as RichMarkdown
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.containers import Container, Horizontal, Vertical, VerticalScroll
from textual.message import Message
from textual.reactive import reactive
from textual.widgets import Footer, Header, Input, Label, Markdown, RichLog, Static

from ..catalog import Catalog
from ..errors import CatalogError


class ChatDriver(Protocol):
    """UI-independent meeting driver used by :class:`HSAChatApp`."""

    session_id: str
    runtime_mode: str

    async def run_turn(self, question: str, emit: Callable[[Any], None]) -> Any: ...

    async def cancel(self) -> None: ...

    async def new_session(self) -> str: ...

    def context_preview(self) -> str: ...


def _event_value(event: Any, field: str, default: Any = None) -> Any:
    if isinstance(event, dict):
        return event.get(field, default)
    return getattr(event, field, default)


def _normalise_kind(value: str) -> str:
    return value.replace(".", "_").replace("-", "_")


def _payload(event: Any) -> dict[str, Any]:
    value = _event_value(event, "payload", {})
    return value if isinstance(value, dict) else {"value": value}


_PHASE_LABELS = {
    "option_generation": "提出备选方案",
    "independent_ballot": "独立判断",
    "revised_ballot": "修订观点",
    "blue_proposal": "提出主张",
    "red_critique": "提出质疑",
    "blue_rebuttal": "回应质疑",
    "judge_ballot": "给出裁判意见",
    "portfolio_memo": "给出专项意见",
    "executive_ballot": "综合各方意见",
}

_CONTRIBUTION_HEADINGS = {
    "independent_ballot": "当前主张",
    "revised_ballot": "修订后的主张",
    "blue_proposal": "蓝队主张",
    "blue_rebuttal": "修订后的主张",
    "judge_ballot": "裁判意见",
    "portfolio_memo": "专项建议",
    "executive_ballot": "综合建议",
}

_DISPOSITION_LABELS = {
    "accepted": "接受这项质疑",
    "mitigated": "已提出修正",
    "rejected": "不同意这项质疑",
    "unresolved": "仍未解决",
}

_ORGANIZATION_LABELS = {
    "product-roundtable": "产品圆桌",
    "launch-red-team": "发布红队",
    "strategy-cabinet": "战略内阁",
}

_STATUS_REASON_LABELS = {
    "high-risk decision requires human approval": "问题本身属于高风险，这项建议需要你最终确认。",
    "unresolved critical red-team attack": "红队提出的关键反对意见仍未解决，当前不建议推进。",
    "unresolved critical objection": "仍有关键反对意见没有解决，这项建议需要你最终确认。",
    "chair override violates a hard constraint": "主席调整后的方案可能违反硬性条件，需要你最终确认。",
    "chair requested an override": "主席调整了汇总结论，需要你最终确认。",
    "protocol invocation budget exhausted": "讨论尚未完成，暂不下结论。",
    "aggregate compromise has no direct member support": (
        "汇总出的折中方案没有参会者直接支持，暂不下结论。"
    ),
    "all options fail at least one hard constraint": (
        "所有候选方案都未满足至少一项硬性条件，当前不建议推进。"
    ),
    "no valid final ballots": "本轮没有形成可用的最终意见。",
    "native profile memory changed during the run": (
        "本轮使用的历史上下文可能发生变化，这项建议需要你最终确认。"
    ),
    "mutable profile history tools were explicitly enabled": (
        "本轮使用的历史上下文可能发生变化，这项建议需要你最终确认。"
    ),
}

_CONTENT_EVENT_KINDS = frozenset(
    {
        "meeting_selected",
        "meeting_selection",
        "options_frozen",
        "invocation_queued",
        "runtime_queued",
        "invocation_started",
        "runtime_started",
        "agent_output_accepted",
        "output_accepted",
        "agent_output_rejected",
        "invocation_failed",
        "runtime_failed",
        "invocation_timed_out",
        "decision_completed",
        "decision_aggregated",
        "run_cancelled",
        "run_failed",
    }
)


def _phase_label(phase: Any) -> str:
    return _PHASE_LABELS.get(str(phase or ""), "发表观点")


def _option_label(option_id: Any, option_labels: dict[str, str]) -> str | None:
    if not isinstance(option_id, str) or not option_id:
        return None
    return option_labels.get(option_id)


def _append_section(lines: list[str], title: str, items: Sequence[str]) -> None:
    unique = list(dict.fromkeys(item.strip() for item in items if item and item.strip()))
    if not unique:
        return
    lines.append(f"\n**{title}**")
    lines.extend(f"- {item}" for item in unique)


def _status_explanation(status: str, reason: str) -> str | None:
    if status == "decided":
        return None
    if reason in _STATUS_REASON_LABELS:
        return _STATUS_REASON_LABELS[reason]
    if reason.startswith("score margin "):
        return "各方意见接近，尚未形成清晰结论。"
    if reason.startswith("quorum not met:"):
        return "完成发言的参会者不足，暂未形成结论。"
    if reason.startswith("invalid final ballot from "):
        return "参会意见未能形成一致可用的结论。"
    return {
        "needs_human": "这项建议需要你最终确认。",
        "inconclusive": "现有分歧或信息不足，建议补充关键事实后再讨论。",
        "rejected": "关键风险尚未解决，当前不建议推进。",
        "budget_exhausted": "讨论尚未完成，暂不下结论。",
    }.get(status)


def _claim_text(item: Any) -> str | None:
    if isinstance(item, dict):
        value = item.get("claim") or item.get("statement")
    else:
        value = getattr(item, "claim", None) or getattr(item, "statement", None)
    return str(value).strip() if value else None


def _contribution_markdown(
    contribution: dict[str, Any],
    *,
    heading: str,
    option_labels: dict[str, str],
) -> list[str]:
    lines: list[str] = []
    preferred = _option_label(contribution.get("preferred_option_id"), option_labels)
    if preferred:
        lines.extend((f"**{heading}**", preferred))

    claims = contribution.get("claims")
    if isinstance(claims, list) and claims:
        _append_section(lines, "理由", [text for item in claims if (text := _claim_text(item))])

    risks = contribution.get("risks")
    if isinstance(risks, list) and risks:
        rendered_risks: list[str] = []
        for item in risks:
            if isinstance(item, dict):
                statement = str(item.get("statement") or "").strip()
                mitigation = str(item.get("mitigation") or "").strip()
            else:
                statement = str(getattr(item, "statement", "")).strip()
                mitigation = str(getattr(item, "mitigation", "")).strip()
            if statement:
                rendered_risks.append(
                    f"{statement}；建议：{mitigation}" if mitigation else statement
                )
        _append_section(lines, "担忧", rendered_risks)

    assumptions = contribution.get("assumptions")
    if isinstance(assumptions, list):
        _append_section(lines, "判断前提", [str(item) for item in assumptions])

    next_actions = contribution.get("next_actions")
    if isinstance(next_actions, list):
        _append_section(lines, "建议下一步", [str(item) for item in next_actions])
    return lines


def _content_markdown(
    payload: dict[str, Any],
    *,
    phase: Any,
    option_labels: dict[str, str],
    attack_labels: dict[str, str],
) -> str:
    """Project an authoritative payload into user-facing discussion content."""

    phase_name = str(phase or "")
    if phase_name not in _PHASE_LABELS:
        return "本轮没有形成可直接展示的观点。"

    options = payload.get("options")
    if phase_name == "option_generation" and isinstance(options, list):
        descriptions = [
            str(item.get("description") or "").strip()
            for item in options
            if isinstance(item, dict) and item.get("description")
        ]
        lines = ["**提出的备选方案**"]
        lines.extend(f"- {description}" for description in descriptions)
        note = str(payload.get("generation_note") or "").strip()
        if note:
            _append_section(lines, "说明", [note])
        return "\n".join(lines) if descriptions else "本轮没有形成可展示的方案。"

    attacks = payload.get("attacks")
    if phase_name == "red_critique" and isinstance(attacks, list):
        lines = ["**反对意见**"]
        for attack in attacks:
            if not isinstance(attack, dict):
                continue
            claim = str(attack.get("claim") or "").strip()
            if not claim:
                continue
            option = _option_label(attack.get("option_id"), option_labels)
            lines.append(f"- 针对「{option}」：{claim}" if option else f"- {claim}")
            evidence_needed = str(attack.get("evidence_needed") or "").strip()
            mitigation = str(attack.get("suggested_mitigation") or "").strip()
            if evidence_needed:
                lines.append(f"  - 还需确认：{evidence_needed}")
            if mitigation:
                lines.append(f"  - 建议修正：{mitigation}")
        alternative = _option_label(payload.get("strongest_alternative_id"), option_labels)
        if alternative:
            lines.extend(("\n**更倾向的备选方案**", alternative))
        return "\n".join(lines)

    lines: list[str] = []
    dispositions = payload.get("dispositions")
    if phase_name == "blue_rebuttal" and isinstance(dispositions, list):
        lines.append("**对质疑的回应**")
        for disposition in dispositions:
            if not isinstance(disposition, dict):
                continue
            response = str(disposition.get("response") or "").strip()
            if not response:
                continue
            attack = attack_labels.get(str(disposition.get("attack_id") or ""))
            status = _DISPOSITION_LABELS.get(
                str(disposition.get("status") or ""), "回应"
            )
            if attack:
                lines.append(f"- 关于“{attack}”：{status}——{response}")
            else:
                lines.append(f"- {status}：{response}")

    contribution: dict[str, Any] = {}
    if phase_name == "executive_ballot" and isinstance(payload.get("ballot"), dict):
        contribution = payload["ballot"]
    elif phase_name == "blue_rebuttal" and isinstance(
        payload.get("revised_ballot"), dict
    ):
        contribution = payload["revised_ballot"]
    elif phase_name in {
        "independent_ballot",
        "revised_ballot",
        "blue_proposal",
        "judge_ballot",
        "portfolio_memo",
    }:
        contribution = payload
    if contribution:
        lines.extend(
            _contribution_markdown(
                contribution,
                heading=_CONTRIBUTION_HEADINGS.get(phase_name, "当前主张"),
                option_labels=option_labels,
            )
        )

    override_reason = str(payload.get("override_reason") or "").strip()
    if phase_name == "executive_ballot" and override_reason:
        _append_section(lines, "主席调整说明", [override_reason])

    return "\n".join(lines) if lines else "本轮没有形成可直接展示的观点。"


def _statement_summary(
    payload: dict[str, Any],
    *,
    phase: Any,
    option_labels: dict[str, str],
) -> str:
    phase_name = str(phase or "")
    if phase_name not in _PHASE_LABELS:
        return "完成了一轮发言"
    options = payload.get("options")
    if phase_name == "option_generation" and isinstance(options, list):
        return f"提出了 {len(options)} 个备选方案"
    attacks = payload.get("attacks")
    if (
        phase_name == "red_critique"
        and isinstance(attacks, list)
        and attacks
        and isinstance(attacks[0], dict)
    ):
        claim = str(attacks[0].get("claim") or "").strip()
        if claim:
            return f"质疑：{claim}"
    dispositions = payload.get("dispositions")
    if (
        phase_name == "blue_rebuttal"
        and isinstance(dispositions, list)
        and dispositions
        and isinstance(dispositions[0], dict)
    ):
        response = str(dispositions[0].get("response") or "").strip()
        if response:
            return f"回应：{response}"
    contribution = (
        payload.get("ballot")
        if phase_name == "executive_ballot"
        else payload.get("revised_ballot")
        if phase_name == "blue_rebuttal"
        else payload
    )
    if isinstance(contribution, dict):
        claims = contribution.get("claims")
        if isinstance(claims, list) and claims:
            claim = _claim_text(claims[0])
            if claim:
                return claim
        preferred = _option_label(contribution.get("preferred_option_id"), option_labels)
        if preferred:
            return f"主张：{preferred}"
    return _phase_label(phase)


class AgentPanel(Vertical):
    """One selected HSA's live activity and accepted output."""

    status = reactive("等待")

    def __init__(self, hsa_id: str, display_name: str) -> None:
        super().__init__(classes="agent-panel", id=f"agent-{hsa_id}")
        self.hsa_id = hsa_id
        self.display_name = display_name

    def compose(self) -> ComposeResult:
        yield Label(self.display_name, classes="agent-name")
        yield Static(self.status, classes="agent-status", markup=False)
        yield RichLog(highlight=False, markup=False, wrap=True, classes="agent-output")

    def watch_status(self, value: str) -> None:
        if self.is_mounted:
            self.query_one(".agent-status", Static).update(value)

    def set_status(self, value: str) -> None:
        self.status = value

    def begin_invocation(self) -> None:
        """Keep earlier statements visible when the same HSA responds again."""

    def show_statement(self, phase_label: str, markdown: str) -> None:
        log = self.query_one(".agent-output", RichLog)
        log.write(Text(f"— {phase_label} —", style="bold cyan"))
        log.write(RichMarkdown(markdown))

    def show_unavailable(self) -> None:
        log = self.query_one(".agent-output", RichLog)
        log.write(Text("本轮没有形成可展示的观点。", style="yellow"))


class RunEventArrived(Message):
    def __init__(self, event: Any) -> None:
        super().__init__()
        self.event = event


class TurnFinished(Message):
    def __init__(self, report: Any) -> None:
        super().__init__()
        self.report = report


class TurnFailed(Message):
    def __init__(self, error: str) -> None:
        super().__init__()
        self.error = error


class HSAChatApp(App[None]):
    """Full-screen chat client for sequential, auditable HSA meetings."""

    TITLE = "HSA Think Tank"
    SUB_TITLE = "多 Hero Soul Agent 会议"

    CSS = """
    Screen {
        layout: vertical;
        background: $surface;
    }
    #meeting-bar {
        height: auto;
        min-height: 3;
        padding: 0 1;
        border-bottom: solid $primary;
    }
    #question {
        color: $text-muted;
    }
    #workspace {
        height: 1fr;
    }
    #main-column {
        width: 1fr;
    }
    #agent-scroll {
        height: 1fr;
    }
    #timeline-column {
        width: 34;
        min-width: 24;
        border-left: solid $primary-background-lighten-2;
        padding: 0 1;
    }
    #agent-grid {
        height: 1fr;
        layout: grid;
        grid-size: 3 1;
        grid-gutter: 1;
        padding: 1;
    }
    .agent-panel {
        height: 1fr;
        min-width: 28;
        border: round $primary-background-lighten-2;
        padding: 0 1;
    }
    .agent-name {
        height: 1;
        text-style: bold;
        color: $accent;
    }
    .agent-status {
        height: 1;
        color: $text-muted;
    }
    .agent-output {
        height: 1fr;
    }
    #decision {
        height: auto;
        max-height: 14;
        border-top: solid $primary-background-lighten-2;
        padding: 0 1;
        overflow-y: auto;
    }
    #prompt {
        dock: bottom;
        margin: 0 1;
    }
    .narrow #workspace {
        layout: vertical;
    }
    .narrow #timeline-column {
        display: none;
    }
    .narrow #agent-grid {
        layout: vertical;
        height: auto;
        grid-size: 1;
        padding: 0 1 1 1;
    }
    .narrow .agent-panel {
        height: 10;
        min-height: 10;
        margin-bottom: 1;
    }
    .narrow #decision {
        max-height: 7;
    }
    """

    BINDINGS = [
        ("ctrl+c", "cancel_meeting", "取消会议"),
        ("ctrl+n", "new_session", "新会话"),
        ("ctrl+l", "show_context", "查看上下文"),
        ("ctrl+q", "quit", "退出"),
    ]

    def __init__(self, *, driver: ChatDriver, catalog: Catalog) -> None:
        super().__init__()
        self.driver = driver
        self.catalog = catalog
        self._busy = False
        self._queued_questions: list[str] = []
        self._panels: dict[str, AgentPanel] = {}
        self._option_labels: dict[str, str] = {}
        self._attack_labels: dict[str, str] = {}
        self._cancel_failure: str | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=True)
        with Vertical(id="meeting-bar"):
            yield Static(
                "等待问题",
                id="meeting-title",
                markup=False,
            )
            yield Static("输入问题开始自动选会", id="question", markup=False)
        with Horizontal(id="workspace"):
            with Vertical(id="main-column"):
                with VerticalScroll(id="agent-scroll"):
                    yield Container(id="agent-grid")
                yield Markdown("### 最终结论\n等待新问题。", id="decision")
            with Vertical(id="timeline-column"):
                yield Label("会议讨论", classes="agent-name")
                yield RichLog(id="timeline", wrap=True, highlight=False, markup=True)
        yield Input(placeholder="输入问题；/context 查看上下文，/new 开新会话", id="prompt")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#prompt", Input).focus()
        self._apply_responsive_layout(self.size.width)

    def on_resize(self, event) -> None:  # Textual's Resize type is intentionally duck-typed.
        self._apply_responsive_layout(event.size.width)

    def _apply_responsive_layout(self, width: int) -> None:
        self.set_class(width < 110, "narrow")
        grid = self.query_one("#agent-grid", Container)
        grid.styles.grid_size_columns = 1 if width < 110 else max(1, len(self._panels))

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        value = event.value.strip()
        event.input.value = ""
        if not value:
            return
        if value == "/cancel":
            await self.action_cancel_meeting()
            return
        if value == "/context":
            self.action_show_context()
            return
        if value == "/new":
            await self.action_new_session()
            return
        if self._busy:
            self._queued_questions.append(value)
            self._timeline(f"[yellow]已排队下一轮：[/] {escape(value)}")
            return
        self._start_turn(value)

    def _start_turn(self, question: str) -> None:
        self._option_labels.clear()
        self._attack_labels.clear()
        self._busy = True
        self.query_one("#question", Static).update(question)
        self.query_one("#decision", Markdown).update("### 最终结论\n讨论进行中……")
        self._timeline(f"[bold cyan]用户：[/] {escape(question)}")
        self.run_meeting(question)

    @work(group="meeting", exclusive=True)
    async def run_meeting(self, question: str) -> None:
        try:
            report = await self.driver.run_turn(question, self._emit_event)
        except asyncio.CancelledError:
            self.post_message(TurnFailed("会议已取消"))
            raise
        except Exception as exc:
            self.post_message(TurnFailed(str(exc)))
        else:
            self.post_message(TurnFinished(report))

    def _emit_event(self, event: Any) -> None:
        kind = _normalise_kind(str(_event_value(event, "kind", "unknown")))
        if kind not in _CONTENT_EVENT_KINDS:
            return
        self.post_message(RunEventArrived(event))

    async def on_run_event_arrived(self, message: RunEventArrived) -> None:
        event = message.event
        kind = _normalise_kind(str(_event_value(event, "kind", "unknown")))
        if kind not in _CONTENT_EVENT_KINDS:
            return
        payload = _payload(event)
        hsa_id = _event_value(event, "hsa_id") or payload.get("hsa_id")
        phase = _event_value(event, "phase") or payload.get("phase")

        if kind in {"meeting_selected", "meeting_selection"}:
            selection = payload.get("meeting_selection", payload)
            selected = selection.get("selected_hsa_ids", [])
            await self._set_selected_hsas(selected)
            organization_id = str(selection.get("organization_id") or "")
            effective = selection.get("effective_organization")
            organization = (
                str(effective.get("name"))
                if isinstance(effective, dict) and effective.get("name")
                else _ORGANIZATION_LABELS.get(organization_id, "专题会议")
            )
            participant_names = [self._display_name(str(item)) for item in selected]
            self.query_one("#meeting-title", Static).update(
                f"{organization} · 参会：{'、'.join(participant_names)}"
            )
            self._timeline(
                f"[green]{escape('、'.join(participant_names))} 开始讨论[/]"
            )
            return

        if kind == "options_frozen":
            self._remember_options(payload)
            return

        panel = self._panels.get(str(hsa_id)) if hsa_id else None
        display_name = self._display_name(str(hsa_id)) if hsa_id else "会议"
        human_phase = _phase_label(phase)
        if kind in {"invocation_queued", "runtime_queued"}:
            if panel:
                panel.set_status(f"准备发言 · {human_phase}")
        elif kind in {"invocation_started", "runtime_started"}:
            if panel:
                panel.begin_invocation()
                panel.set_status(f"正在{human_phase}")
            self._timeline(
                f"[cyan]{escape(display_name)}[/]：{escape(human_phase)}"
            )
        elif kind in {"agent_output_accepted", "output_accepted"}:
            value = (
                payload.get("value")
                or payload.get("output")
                or payload
            )
            if isinstance(value, dict):
                self._remember_options(value)
                self._remember_attacks(value)
                if panel:
                    panel.set_status(f"已发言 · {human_phase}")
                    panel.show_statement(
                        human_phase,
                        _content_markdown(
                            value,
                            phase=phase,
                            option_labels=self._option_labels,
                            attack_labels=self._attack_labels,
                        )
                    )
                summary = _statement_summary(
                    value,
                    phase=phase,
                    option_labels=self._option_labels,
                )
                self._timeline(
                    f"[green]{escape(display_name)}：[/] {escape(summary)}"
                )
        elif kind in {
            "agent_output_rejected",
            "invocation_failed",
            "runtime_failed",
            "invocation_timed_out",
        }:
            if panel:
                panel.set_status("本轮未能发言")
                panel.show_unavailable()
            self._timeline(f"[yellow]{escape(display_name)} 本轮未能完成发言[/]")
        elif kind in {"decision_completed", "decision_aggregated"}:
            self._timeline("[bold green]正在整理最终结论[/]")
        elif kind in {"run_cancelled", "run_failed"}:
            self._timeline("[yellow]本轮讨论没有形成结论[/]")

    def _display_name(self, hsa_id: str) -> str:
        try:
            return self.catalog.profile(hsa_id).display_name
        except CatalogError:
            return "参会者"

    def _remember_options(self, payload: dict[str, Any]) -> None:
        options = payload.get("options")
        if not isinstance(options, list):
            return
        for item in options:
            if not isinstance(item, dict):
                continue
            option_id = item.get("id")
            description = item.get("description")
            if isinstance(option_id, str) and isinstance(description, str):
                self._option_labels[option_id] = description

    def _remember_attacks(self, payload: dict[str, Any]) -> None:
        attacks = payload.get("attacks")
        if not isinstance(attacks, list):
            return
        for item in attacks:
            if not isinstance(item, dict):
                continue
            attack_id = item.get("attack_id")
            claim = item.get("claim")
            if isinstance(attack_id, str) and isinstance(claim, str):
                self._attack_labels[attack_id] = claim

    async def _set_selected_hsas(self, hsa_ids: Sequence[str]) -> None:
        grid = self.query_one("#agent-grid", Container)
        await grid.remove_children()
        self._panels.clear()
        panels: list[AgentPanel] = []
        for hsa_id in hsa_ids:
            display_name = self._display_name(hsa_id)
            panel = AgentPanel(hsa_id, display_name)
            self._panels[hsa_id] = panel
            panels.append(panel)
        if panels:
            await grid.mount(*panels)
        self._apply_responsive_layout(self.size.width)

    async def on_turn_finished(self, message: TurnFinished) -> None:
        self._busy = False
        self._cancel_failure = None
        self.query_one("#decision", Markdown).update(self._report_markdown(message.report))
        self._timeline("[bold green]最终结论已形成[/]")
        self._dequeue_next_turn()

    async def on_turn_failed(self, message: TurnFailed) -> None:
        self._busy = False
        cancelled = "取消" in (self._cancel_failure or message.error)
        self._cancel_failure = None
        explanation = "本轮讨论已取消。" if cancelled else "本轮讨论未完成，尚未形成结论。"
        self.query_one("#decision", Markdown).update(f"### 最终结论\n\n{explanation}")
        self._timeline(f"[yellow]{escape(explanation)}[/]")
        self._dequeue_next_turn()

    def _dequeue_next_turn(self) -> None:
        if self._queued_questions:
            question = self._queued_questions.pop(0)
            self.call_after_refresh(self._start_turn, question)

    def _report_markdown(self, report: Any) -> str:
        status = str(getattr(report, "status", "unknown"))
        option_labels: dict[str, str] = {}
        constraint_labels: dict[str, str] = {}
        frozen_problem = getattr(report, "frozen_problem", None)
        for option in list(getattr(frozen_problem, "options", []) or []):
            option_id = getattr(option, "id", None)
            description = getattr(option, "description", None)
            if option_id and description:
                option_labels[str(option_id)] = str(description)
        for criterion in list(getattr(frozen_problem, "criteria", []) or []):
            criterion_id = getattr(criterion, "id", None)
            description = getattr(criterion, "description", None)
            if criterion_id and description:
                constraint_labels[str(criterion_id)] = str(description)
        selected_id = getattr(report, "selected_option_id", None)
        selected = getattr(report, "selected_option", None) or option_labels.get(
            str(selected_id or "")
        )
        rationale = list(getattr(report, "rationale_claims", []) or [])
        assumptions = list(getattr(report, "assumptions", []) or [])
        risks = list(getattr(report, "unresolved_risks", []) or [])
        dissent = list(getattr(report, "dissent", []) or [])
        next_actions = list(getattr(report, "next_actions", []) or [])
        lines = ["### 最终结论"]
        if status == "decided" and selected:
            lines.append(f"**建议：{selected}**")
        elif status == "needs_human" and selected:
            lines.append(f"**建议（待你确认）：{selected}**")
        elif status == "inconclusive" and selected:
            lines.append(f"**暂未形成一致结论；当前讨论倾向「{selected}」。**")
        elif status == "inconclusive":
            lines.append("**暂未形成清晰结论。**")
        elif status == "rejected":
            lines.append("**当前不建议推进。**")
        elif status == "budget_exhausted":
            lines.append("**讨论尚未完成，暂不下结论。**")
        else:
            lines.append("**本轮没有形成可执行结论。**")

        explanation = _status_explanation(
            status,
            str(getattr(report, "status_reason", "") or ""),
        )
        if explanation and explanation not in lines[-1]:
            lines.append(explanation)
        if rationale:
            _append_section(
                lines,
                "为什么",
                [text for item in rationale if (text := _claim_text(item))],
            )
        if dissent:
            _append_section(
                lines,
                "仍有分歧",
                [self._humanize_dissent(str(item), option_labels) for item in dissent],
            )
        if risks:
            _append_section(
                lines,
                "需要留意",
                [
                    self._humanize_risk(
                        str(item),
                        option_labels=option_labels,
                        constraint_labels=constraint_labels,
                    )
                    for item in risks
                ],
            )
        if assumptions:
            _append_section(lines, "判断前提", [str(item) for item in assumptions])
        if next_actions:
            _append_section(lines, "下一步", [str(item) for item in next_actions])
        chair_override = getattr(report, "chair_override", None)
        if isinstance(chair_override, dict):
            override_reason = str(chair_override.get("reason") or "").strip()
            if override_reason:
                _append_section(lines, "主席调整说明", [override_reason])
        return "\n".join(lines)

    def _humanize_dissent(self, value: str, option_labels: dict[str, str]) -> str:
        member_id, separator, remainder = value.partition(" prefers ")
        option_id, detail_separator, claim = remainder.partition(": ")
        if not separator or not detail_separator:
            return "还有参会者持保留意见。"
        member = self._display_name(member_id)
        option = option_labels.get(option_id, "另一方案")
        return f"{member}主张「{option}」：{claim}"

    @staticmethod
    def _humanize_risk(
        value: str,
        *,
        option_labels: dict[str, str],
        constraint_labels: dict[str, str],
    ) -> str:
        severity, separator, statement = value.partition(": ")
        label = {
            "low": "较低风险",
            "medium": "需注意",
            "high": "重要风险",
            "critical": "关键风险",
        }.get(severity)
        body = statement if separator and label else value
        option_id, violates, references = body.partition(" violates ")
        if violates:
            option = option_labels.get(option_id, "一个候选方案")
            constraints = []
            for reference in references.split(", "):
                _, marker, constraint_id = reference.rpartition(":")
                if not marker:
                    continue
                description = constraint_labels.get(constraint_id)
                if description and description not in constraints:
                    constraints.append(description)
            if constraints:
                return f"「{option}」未满足硬性条件：{'；'.join(constraints)}"
            return f"「{option}」未满足至少一项硬性条件"
        return f"{label}：{body}" if label else body

    async def action_cancel_meeting(self) -> None:
        if not self._busy:
            self._timeline("当前没有运行中的会议")
            return
        self._timeline("[yellow]正在取消会议……[/]")
        try:
            await self.driver.cancel()
        except asyncio.CancelledError:
            pass
        except Exception:
            self._cancel_failure = "取消未能立即完成，请稍后重试。"
            self.query_one("#decision", Markdown).update(
                f"### 最终结论\n\n{self._cancel_failure}"
            )
            self._timeline(f"[bold red]{escape(self._cancel_failure)}[/]")
        finally:
            self.workers.cancel_group(self, "meeting")

    async def action_quit(self) -> None:
        try:
            if self._busy:
                await self.driver.cancel()
        except asyncio.CancelledError:
            pass
        except Exception:
            self._timeline("[yellow]当前会议未能立即结束[/]")
        finally:
            self._queued_questions.clear()
            self.workers.cancel_group(self, "meeting")
            self.exit()

    async def action_new_session(self) -> None:
        if self._busy:
            self._timeline("[yellow]请先完成或取消当前会议[/]")
            return
        await self.driver.new_session()
        self._option_labels.clear()
        self._attack_labels.clear()
        self.query_one("#meeting-title", Static).update("等待问题")
        self.query_one("#question", Static).update("输入问题开始自动选会")
        self.query_one("#decision", Markdown).update("### 最终结论\n等待新问题。")
        await self._set_selected_hsas([])
        self._timeline("[green]已创建新会话[/]")

    def action_show_context(self) -> None:
        preview = self.driver.context_preview() or "（当前上下文为空）"
        self.query_one("#decision", Markdown).update("### 下一轮将纳入的上下文\n\n" + preview)

    def _timeline(self, message: str) -> None:
        self.query_one("#timeline", RichLog).write(message)


__all__ = ["AgentPanel", "ChatDriver", "HSAChatApp"]
