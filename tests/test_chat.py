from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from hsa_thinktank.catalog import Catalog
from hsa_thinktank.chat import ThinkTankChatDriver, preview_chat_context, render_chat_context
from hsa_thinktank.chat_store import ChatContextItem, LocalChatStore
from hsa_thinktank.demo import demo_responder
from hsa_thinktank.models import DecisionProblem
from hsa_thinktank.orchestrator import ThinkTank
from hsa_thinktank.routing import MeetingRouter
from hsa_thinktank.runtime import DeterministicRuntime


class BlockingRuntime:
    name = "blocking"

    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.cancelled = 0

    async def invoke(self, _invocation, *, event_sink=None):
        del event_sink
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled += 1
            raise


def test_chat_driver_runs_meeting_and_persists_only_safe_context(tmp_path: Path) -> None:
    async def scenario() -> None:
        catalog = Catalog.builtin()
        tank = ThinkTank(
            catalog=catalog,
            runtimes=DeterministicRuntime(demo_responder),
        )
        store = LocalChatStore(tmp_path / "chats")
        driver = ThinkTankChatDriver(
            tank=tank,
            chat_store=store,
            persist_runs=False,
            runtime_mode="demo-streaming",
        )
        events = []

        report = await driver.run_turn("如何改善产品的新用户体验？", events.append)

        assert report.status == "decided"
        assert "meeting_selected" in {event.kind for event in events}
        assert "run_finished" in {event.kind for event in events}
        session = store.load(driver.session_id)
        assert [turn.kind for turn in session.turns] == ["user", "decision"]
        context = store.build_context(driver.session_id)
        assert [item.kind for item in context] == ["user_message", "confirmed_decision"]
        assert "如何改善产品" in driver.context_preview()
        rendered_context = render_chat_context(context)
        assert "实时" not in rendered_context
        assert "meeting_selection" not in rendered_context
        assert "organization_id" not in rendered_context
        assert "risk_tier" not in rendered_context

    asyncio.run(scenario())


def test_next_turn_routes_from_semantic_chat_context_without_schema_noise(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        driver = ThinkTankChatDriver(
            tank=ThinkTank(
                catalog=Catalog.builtin(),
                runtimes=DeterministicRuntime(demo_responder),
            ),
            chat_store=LocalChatStore(tmp_path / "chats"),
            persist_runs=False,
        )
        await driver.run_turn(
            "我们应如何进行资本配置，确保估值与安全边际并控制下行风险？",
            lambda _event: None,
        )

        follow_up = await driver.run_turn("下一步怎么做？", lambda _event: None)
        return follow_up

    report = asyncio.run(scenario())

    assert "资本配置" in report.frozen_problem.context
    assert "capital" in report.meeting_selection.matched_signals
    assert "organization_id" not in report.frozen_problem.context


def test_chat_context_wrapper_does_not_invent_routing_signals() -> None:
    context = render_chat_context(
        [
            ChatContextItem(
                role="user",
                kind="user_message",
                content="下一步怎么办？",
            )
        ]
    )

    selection = MeetingRouter(Catalog.builtin()).select(
        DecisionProblem(question="还有吗？", context=context)
    )

    assert selection.matched_signals == []


def test_chat_context_preview_keeps_content_and_hides_internal_identifiers() -> None:
    summary = {
        "question": "是否启动企业版试点？",
        "status": "needs_human",
        "status_reason": "chair requested an override",
        "selected_option_id": "pilot",
        "selected_option": None,
        "options": [
            {"id": "pilot", "description": "先做八周付费试点"},
            {"id": "wait", "description": "暂缓企业版"},
        ],
        "rationale_claims": [{"claim": "可逆试点能换取真实需求证据"}],
        "assumptions": [],
        "unresolved_risks": ["pilot violates steve-jobs:must-pay"],
        "dissent": ["charlie-munger prefers wait: 现金窗口过短"],
        "next_actions": ["两周内获得付费承诺"],
    }
    items = [
        ChatContextItem(
            role="assistant",
            kind="confirmed_decision",
            content=json.dumps(summary, ensure_ascii=False),
            run_id="run-private-123",
        )
    ]

    preview = preview_chat_context(
        items,
        display_name=lambda _member_id: "Charlie Munger 启发式综合体",
    )
    model_context = render_chat_context(items)

    for rendered in (preview, model_context):
        assert "先做八周付费试点" in rendered
        assert "可逆试点能换取真实需求证据" in rendered
        assert "暂缓企业版" in rendered
        assert "现金窗口过短" in rendered
        assert "两周内获得付费承诺" in rendered
        for internal in (
            "run-private-123",
            "needs_human",
            "chair requested",
            "pilot violates",
            "steve-jobs",
            "must-pay",
            "charlie-munger",
            " prefers ",
        ):
            assert internal not in rendered


def test_new_chat_session_starts_with_empty_context(tmp_path: Path) -> None:
    async def scenario() -> None:
        driver = ThinkTankChatDriver(
            tank=ThinkTank(
                catalog=Catalog.builtin(),
                runtimes=DeterministicRuntime(demo_responder),
            ),
            chat_store=LocalChatStore(tmp_path / "chats"),
            persist_runs=False,
        )
        original = driver.session_id

        replacement = await driver.new_session()

        assert replacement != original
        assert driver.context_preview() == ""

    asyncio.run(scenario())


def test_cancelling_turn_joins_owned_run_before_allowing_another_turn(
    tmp_path: Path,
) -> None:
    async def scenario() -> None:
        runtime = BlockingRuntime()
        driver = ThinkTankChatDriver(
            tank=ThinkTank(catalog=Catalog.builtin(), runtimes=runtime),
            chat_store=LocalChatStore(tmp_path / "chats"),
            persist_runs=False,
        )
        turn = asyncio.create_task(driver.run_turn("需要持续研究的问题", lambda _event: None))
        await asyncio.wait_for(runtime.started.wait(), timeout=1)

        turn.cancel()
        with pytest.raises(asyncio.CancelledError):
            await turn

        assert runtime.cancelled > 0
        assert driver._active_handle is None
        assert not any(
            task.get_name().startswith("hsa-thinktank:") and not task.done()
            for task in asyncio.all_tasks()
        )

    asyncio.run(scenario())
