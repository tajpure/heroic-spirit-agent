from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

from textual.widgets import Input, Markdown

from hsa_thinktank.catalog import Catalog
from hsa_thinktank.tui.app import HSAChatApp, _content_markdown


class FakeChatDriver:
    session_id = "chat-test"
    runtime_mode = "demo-streaming"

    def __init__(self, selected_hsas: list[str] | None = None) -> None:
        self.questions: list[str] = []
        self.cancelled = False
        self.selected_hsas = selected_hsas or ["steve-jobs", "charlie-munger"]

    async def run_turn(self, question: str, emit) -> Any:
        self.questions.append(question)
        emit(
            {
                "kind": "meeting_selected",
                "payload": {
                    "organization_id": "product-roundtable",
                    "protocol": "roundtable",
                    "selected_hsa_ids": self.selected_hsas,
                },
            }
        )
        emit(
            {
                "kind": "options_frozen",
                "payload": {
                    "options": [
                        {"id": "guided", "description": "渐进式引导"},
                        {"id": "full", "description": "完整改版"},
                    ]
                },
            }
        )
        emit(
            {
                "kind": "invocation_started",
                "hsa_id": "steve-jobs",
                "phase": "independent_ballot",
            }
        )
        emit(
            {
                "kind": "output_delta",
                "hsa_id": "steve-jobs",
                "phase": "independent_ballot",
                "payload": {"text": "Focus"},
            }
        )
        await asyncio.sleep(0)
        emit(
            {
                "kind": "agent_output_accepted",
                "hsa_id": "steve-jobs",
                "phase": "independent_ballot",
                "payload": {
                    "value": {
                        "preferred_option_id": "guided",
                        "confidence": 0.8,
                        "claims": [{"claim": "Reduce first-run friction"}],
                    }
                },
            }
        )
        return SimpleNamespace(
            status="decided",
            selected_option="渐进式引导",
            selected_option_id="guided",
            confidence=0.8,
            unresolved_risks=[],
            dissent=[],
        )

    async def cancel(self) -> None:
        self.cancelled = True

    async def new_session(self) -> str:
        self.session_id = "chat-new"
        return self.session_id

    def context_preview(self) -> str:
        return "上一轮已确认决策"


class BlockingChatDriver(FakeChatDriver):
    def __init__(self) -> None:
        super().__init__()
        self.started = asyncio.Event()

    async def run_turn(self, question: str, emit) -> Any:
        del emit
        self.questions.append(question)
        self.started.set()
        await asyncio.Event().wait()


class FailingCancelDriver(BlockingChatDriver):
    def __init__(self) -> None:
        super().__init__()
        self.cancel_attempts = 0

    async def cancel(self) -> None:
        self.cancel_attempts += 1
        raise RuntimeError("backend failed during cancellation")


def test_tui_shows_only_selected_hsas_and_final_decision() -> None:
    async def scenario() -> None:
        driver = FakeChatDriver()
        app = HSAChatApp(driver=driver, catalog=Catalog.builtin())
        async with app.run_test(size=(150, 45)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "如何改善新用户体验？"
            await pilot.press("enter")
            await pilot.pause(0.2)

            assert driver.questions == ["如何改善新用户体验？"]
            assert set(app._panels) == {"steve-jobs", "charlie-munger"}
            assert app._panels["steve-jobs"].status.startswith("已发言")
            decision = app.query_one("#decision", Markdown).source
            assert "渐进式引导" in decision
            assert "最终结论" in decision
            assert "置信度" not in decision
            screenshot = app.export_screenshot()
            assert "结构校验" not in screenshot
            assert "confidence" not in screenshot
            assert "runtime=" not in screenshot

    asyncio.run(scenario())


def test_tui_context_command_does_not_start_a_meeting() -> None:
    async def scenario() -> None:
        driver = FakeChatDriver()
        app = HSAChatApp(driver=driver, catalog=Catalog.builtin())
        async with app.run_test(size=(100, 35)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "/context"
            await pilot.press("enter")
            await pilot.pause()

            assert not driver.questions
            assert "上一轮已确认决策" in app.query_one("#decision", Markdown).source
            assert app.has_class("narrow")

    asyncio.run(scenario())


def test_quitting_tui_cancels_an_active_meeting() -> None:
    async def scenario() -> None:
        driver = BlockingChatDriver()
        app = HSAChatApp(driver=driver, catalog=Catalog.builtin())
        async with app.run_test(size=(120, 35)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "需要长时间研究的问题"
            await pilot.press("enter")
            await asyncio.wait_for(driver.started.wait(), timeout=0.5)

            await app.action_quit()

            assert driver.cancelled is True

    asyncio.run(scenario())


def test_narrow_tui_scrolls_three_panels_without_covering_decision() -> None:
    async def scenario() -> None:
        driver = FakeChatDriver(["steve-jobs", "charlie-munger", "donella-meadows"])
        app = HSAChatApp(driver=driver, catalog=Catalog.builtin())
        async with app.run_test(size=(80, 24)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "如何综合判断？"
            await pilot.press("enter")
            await pilot.pause(0.2)

            agent_scroll = app.query_one("#agent-scroll")
            decision = app.query_one("#decision", Markdown)
            timeline = app.query_one("#timeline-column")
            assert timeline.display is False
            assert agent_scroll.region.bottom <= decision.region.y
            assert agent_scroll.max_scroll_y > 0
            assert len(app._panels) == 3
            assert all(panel.region.height >= 10 for panel in app._panels.values())
            assert all(
                panel.query_one(".agent-output").region.height >= 4
                for panel in app._panels.values()
            )

    asyncio.run(scenario())


def test_cancel_failure_is_rendered_without_crashing_tui() -> None:
    async def scenario() -> None:
        driver = FailingCancelDriver()
        app = HSAChatApp(driver=driver, catalog=Catalog.builtin())
        async with app.run_test(size=(100, 35)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "需要取消的问题"
            await pilot.press("enter")
            await asyncio.wait_for(driver.started.wait(), timeout=0.5)

            await pilot.press("ctrl+c")
            await pilot.pause(0.1)

            assert driver.cancel_attempts == 1
            assert app._busy is False
            assert "本轮讨论已取消" in app.query_one("#decision", Markdown).source

    asyncio.run(scenario())


def test_quit_failure_still_cleans_workers_and_exits() -> None:
    async def scenario() -> None:
        driver = FailingCancelDriver()
        app = HSAChatApp(driver=driver, catalog=Catalog.builtin())
        async with app.run_test(size=(100, 35)) as pilot:
            prompt = app.query_one("#prompt", Input)
            prompt.value = "退出时仍在运行的问题"
            await pilot.press("enter")
            await asyncio.wait_for(driver.started.wait(), timeout=0.5)

            await pilot.press("ctrl+q")
            await pilot.pause(0.1)

            assert driver.cancel_attempts == 1
            assert not app.workers._workers

    asyncio.run(scenario())


def test_tui_drops_structured_delta_burst_before_textual_message_queue() -> None:
    async def scenario() -> None:
        app = HSAChatApp(driver=FakeChatDriver(), catalog=Catalog.builtin())
        async with app.run_test(size=(120, 35)) as pilot:
            await app._set_selected_hsas(["steve-jobs"])
            await pilot.pause()
            baseline = app.message_queue_size

            for _ in range(2_000):
                app._emit_event(
                    {
                        "kind": "output_delta",
                        "hsa_id": "steve-jobs",
                        "phase": "independent_ballot",
                        "invocation_id": "inv-burst",
                        "payload": {"text": "RAW-DELTA-SENTINEL"},
                    }
                )

            assert app.message_queue_size - baseline < 2

            app._emit_event(
                {
                    "kind": "agent_output_accepted",
                    "hsa_id": "steve-jobs",
                    "phase": "independent_ballot",
                    "invocation_id": "inv-burst",
                    "payload": {
                        "value": {
                            "preferred_option_id": "guided",
                            "confidence": 0.8,
                            "claims": [{"claim": "validated"}],
                        }
                    },
                }
            )
            assert app.message_queue_size - baseline < 10
            await pilot.pause(0.1)
            assert app._panels["steve-jobs"].status.startswith("已发言")
            assert "RAW-DELTA-SENTINEL" not in app.export_screenshot()

    asyncio.run(scenario())


def test_content_projection_keeps_arguments_and_hides_internal_metrics() -> None:
    rendered = _content_markdown(
        {
            "preferred_option_id": "guided",
            "option_scores": {"guided": 0.9, "full": 0.4},
            "confidence": 0.99,
            "criterion_scores": {"guided": {"fit": 0.9}},
            "claims": [{"claim": "先降低首次使用的理解成本", "basis": "speculative"}],
            "assumptions": ["用户流失主要发生在首次体验"],
            "risks": [
                {
                    "option_id": "guided",
                    "severity": "medium",
                    "statement": "引导可能遮蔽核心价值",
                    "mitigation": "只保留一个关键步骤",
                }
            ],
            "next_actions": ["观察一周后的激活率"],
        },
        phase="portfolio_memo",
        option_labels={"guided": "渐进式引导", "full": "完整改版"},
        attack_labels={},
    )

    assert "渐进式引导" in rendered
    assert "先降低首次使用的理解成本" in rendered
    assert "引导可能遮蔽核心价值" in rendered
    assert "用户流失主要发生在首次体验" in rendered
    assert "观察一周后的激活率" in rendered
    for internal in (
        "置信度",
        "0.99",
        "confidence",
        "option_scores",
        "criterion_scores",
        "preferred_option_id",
        "speculative",
        "```json",
    ):
        assert internal not in rendered


def test_content_projection_makes_red_team_dispute_readable() -> None:
    options = {"pilot": "做八周付费试点", "wait": "暂缓企业版"}
    critique = _content_markdown(
        {
            "attacks": [
                {
                    "attack_id": "risk-1",
                    "option_id": "pilot",
                    "severity": "high",
                    "claim": "企业意向还没有转化为付费承诺",
                    "evidence_needed": "至少一家签署付费试点",
                    "suggested_mitigation": "把付费签约设为继续投入的门槛",
                }
            ],
            "strongest_alternative_id": "wait",
        },
        phase="red_critique",
        option_labels=options,
        attack_labels={},
    )
    rebuttal = _content_markdown(
        {
            "dispositions": [
                {
                    "attack_id": "risk-1",
                    "status": "mitigated",
                    "response": "未签付费试点就不扩编",
                }
            ],
            "revised_ballot": {
                "preferred_option_id": "pilot",
                "claims": [{"claim": "先用可逆试点验证企业需求"}],
                "assumptions": [],
                "risks": [],
                "next_actions": [],
            },
        },
        phase="blue_rebuttal",
        option_labels=options,
        attack_labels={"risk-1": "企业意向还没有转化为付费承诺"},
    )

    assert "企业意向还没有转化为付费承诺" in critique
    assert "至少一家签署付费试点" in critique
    assert "把付费签约设为继续投入的门槛" in critique
    assert "暂缓企业版" in critique
    assert "企业意向还没有转化为付费承诺" in rebuttal
    assert "未签付费试点就不扩编" in rebuttal
    assert "risk-1" not in critique + rebuttal
    assert "mitigated" not in rebuttal


def test_final_conclusion_uses_plain_language_without_confidence_or_internal_status() -> None:
    app = HSAChatApp(driver=FakeChatDriver(), catalog=Catalog.builtin())
    report = SimpleNamespace(
        status="needs_human",
        status_reason="high-risk decision requires human approval",
        selected_option_id="pilot",
        selected_option="先做八周付费试点",
        confidence=0.934567,
        frozen_problem=SimpleNamespace(
            options=[
                SimpleNamespace(id="pilot", description="先做八周付费试点"),
                SimpleNamespace(id="wait", description="暂缓企业版"),
            ]
        ),
        rationale_claims=[SimpleNamespace(claim="试点可以保留可逆性")],
        assumptions=["至少一家客户愿意付费"],
        unresolved_risks=["high: 集成工作量仍未验证"],
        dissent=["charlie-munger prefers wait: 现金窗口过短"],
        next_actions=["两周内拿到付费承诺"],
    )

    rendered = app._report_markdown(report)
    assert "建议（待你确认）：先做八周付费试点" in rendered
    assert "问题本身属于高风险" in rendered
    assert "试点可以保留可逆性" in rendered
    assert "Charlie Munger 启发式综合体主张「暂缓企业版」：现金窗口过短" in rendered
    assert "重要风险：集成工作量仍未验证" in rendered
    assert "至少一家客户愿意付费" in rendered
    assert "两周内拿到付费承诺" in rendered
    for internal in (
        "0.934567",
        "confidence",
        "置信度",
        "needs_human",
        "high-risk decision requires human approval",
        "charlie-munger",
        " prefers ",
    ):
        assert internal not in rendered


def test_final_conclusion_does_not_recommend_rejected_or_unfinished_choice() -> None:
    app = HSAChatApp(driver=FakeChatDriver(), catalog=Catalog.builtin())
    base = {
        "selected_option_id": "pilot",
        "selected_option": "先做八周付费试点",
        "frozen_problem": SimpleNamespace(
            options=[SimpleNamespace(id="pilot", description="先做八周付费试点")],
            criteria=[],
        ),
        "rationale_claims": [],
        "assumptions": [],
        "unresolved_risks": [],
        "dissent": [],
        "next_actions": [],
        "chair_override": None,
    }

    rejected = app._report_markdown(
        SimpleNamespace(
            **base,
            status="rejected",
            status_reason="unresolved critical red-team attack",
        )
    )
    unfinished = app._report_markdown(
        SimpleNamespace(
            **base,
            status="budget_exhausted",
            status_reason="protocol invocation budget exhausted",
        )
    )
    inconclusive = app._report_markdown(
        SimpleNamespace(
            **base,
            status="inconclusive",
            status_reason="score margin 0.004 below threshold",
        )
    )

    assert "当前不建议推进" in rejected
    assert "建议：先做八周付费试点" not in rejected
    assert "暂不下结论" in unfinished
    assert "建议：先做八周付费试点" not in unfinished
    assert "当前讨论倾向「先做八周付费试点」" in inconclusive
    assert "建议：先做八周付费试点" not in inconclusive
    assert "0.004" not in inconclusive


def test_final_conclusion_humanizes_hard_constraint_risk_and_chair_reason() -> None:
    app = HSAChatApp(driver=FakeChatDriver(), catalog=Catalog.builtin())
    report = SimpleNamespace(
        status="needs_human",
        status_reason="chair requested an override",
        selected_option_id="pilot",
        selected_option="先做八周付费试点",
        frozen_problem=SimpleNamespace(
            options=[SimpleNamespace(id="pilot", description="先做八周付费试点")],
            criteria=[SimpleNamespace(id="must-pay", description="必须先获得付费承诺")],
        ),
        rationale_claims=[],
        assumptions=[],
        unresolved_risks=["pilot violates steve-jobs:must-pay"],
        dissent=[],
        next_actions=[],
        chair_override={
            "aggregated_option_id": "wait",
            "override_option_id": "pilot",
            "reason": "先用两周可逆试点换取真实证据",
        },
    )

    rendered = app._report_markdown(report)

    assert "「先做八周付费试点」未满足硬性条件：必须先获得付费承诺" in rendered
    assert "主席调整说明" in rendered
    assert "先用两周可逆试点换取真实证据" in rendered
    for internal in ("pilot violates", "steve-jobs", "must-pay", "chair requested"):
        assert internal not in rendered


def test_content_projection_keeps_all_bounded_objections_and_fails_closed() -> None:
    attacks = [
        {
            "attack_id": f"risk-{index}",
            "option_id": "pilot",
            "claim": f"反对意见 {index}",
        }
        for index in range(1, 17)
    ]
    rendered = _content_markdown(
        {"attacks": attacks, "strongest_alternative_id": "wait"},
        phase="red_critique",
        option_labels={"pilot": "先试点", "wait": "暂缓"},
        attack_labels={},
    )
    unknown = _content_markdown(
        {"claim": "RAW-UNKNOWN-PHASE", "confidence": 0.99},
        phase="future_internal_phase",
        option_labels={},
        attack_labels={},
    )

    assert "反对意见 16" in rendered
    assert "risk-16" not in rendered
    assert "RAW-UNKNOWN-PHASE" not in unknown
    assert "0.99" not in unknown


def test_tui_keeps_the_dispute_and_ignores_duplicate_audit_events() -> None:
    async def scenario() -> None:
        app = HSAChatApp(driver=FakeChatDriver(), catalog=Catalog.builtin())
        async with app.run_test(size=(150, 45)) as pilot:
            await app._set_selected_hsas(
                ["steve-jobs", "charlie-munger", "donella-meadows"]
            )
            app._emit_event(
                {
                    "kind": "options_frozen",
                    "payload": {
                        "options": [
                            {"id": "pilot", "description": "先做八周付费试点"},
                            {"id": "wait", "description": "暂缓企业版"},
                        ]
                    },
                }
            )
            app._emit_event(
                {
                    "kind": "agent_output_accepted",
                    "hsa_id": "steve-jobs",
                    "phase": "blue_proposal",
                    "payload": {
                        "value": {
                            "preferred_option_id": "pilot",
                            "claims": [{"claim": "用可逆试点换取真实需求证据"}],
                            "assumptions": [],
                            "risks": [],
                            "next_actions": [],
                        }
                    },
                }
            )
            app._emit_event(
                {
                    "kind": "agent_output_accepted",
                    "hsa_id": "charlie-munger",
                    "phase": "red_critique",
                    "payload": {
                        "value": {
                            "attacks": [
                                {
                                    "attack_id": "risk-sentinel",
                                    "option_id": "pilot",
                                    "claim": "企业意向还没有转化为付费承诺",
                                    "evidence_needed": "签署付费试点",
                                    "suggested_mitigation": "未付费就不扩编",
                                }
                            ],
                            "strongest_alternative_id": "wait",
                        }
                    },
                }
            )
            app._emit_event(
                {
                    "kind": "agent_output_accepted",
                    "hsa_id": "steve-jobs",
                    "phase": "blue_rebuttal",
                    "payload": {
                        "value": {
                            "dispositions": [
                                {
                                    "attack_id": "risk-sentinel",
                                    "status": "mitigated",
                                    "response": "未签付费试点就停止投入",
                                }
                            ],
                            "revised_ballot": {
                                "preferred_option_id": "pilot",
                                "claims": [{"claim": "保留试点，但加入明确止损线"}],
                                "assumptions": [],
                                "risks": [],
                                "next_actions": [],
                            },
                        }
                    },
                }
            )
            for hidden_event in (
                {
                    "kind": "message_accepted",
                    "hsa_id": "steve-jobs",
                    "phase": "blue_rebuttal",
                    "payload": {"payload": {"claim": "MESSAGE-AUDIT-SENTINEL"}},
                },
                {
                    "kind": "tool_started",
                    "hsa_id": "steve-jobs",
                    "payload": {"name": "TOOL-AUDIT-SENTINEL"},
                },
                {
                    "kind": "runtime_stream_ready",
                    "hsa_id": "steve-jobs",
                    "payload": {"backend": "RUNTIME-SENTINEL"},
                },
            ):
                app._emit_event(hidden_event)

            await pilot.pause(0.2)
            surface = "\n".join(
                line.text
                for panel in app._panels.values()
                for line in panel.query_one(".agent-output").lines
            )
            assert "用可逆试点换取真实需求证据" in surface
            assert "企业意向还没有转化为付费承诺" in surface
            assert "未签付费试点就停止投入" in surface
            assert "保留试点，但加入明确止损线" in surface
            for hidden in (
                "risk-sentinel",
                "mitigated",
                "MESSAGE-AUDIT-SENTINEL",
                "TOOL-AUDIT-SENTINEL",
                "RUNTIME-SENTINEL",
            ):
                assert hidden not in surface

    asyncio.run(scenario())
