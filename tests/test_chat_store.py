from __future__ import annotations

import asyncio
import json
import multiprocessing
import stat
from pathlib import Path
from threading import Barrier, BrokenBarrierError, Thread

import pytest
from pydantic import ValidationError

import hsa_thinktank.chat_store as chat_store_module
from hsa_thinktank.catalog import Catalog
from hsa_thinktank.chat_store import LocalChatStore
from hsa_thinktank.demo import demo_responder
from hsa_thinktank.models import DecisionOption, DecisionProblem, DecisionReport
from hsa_thinktank.orchestrator import ThinkTank
from hsa_thinktank.runtime import DeterministicRuntime


def _append_chat_in_child(root: str, session_id: str, ready, done) -> None:
    store = LocalChatStore(root)
    ready.set()
    store.append_user(session_id, "from-child-process")
    done.set()


@pytest.fixture(scope="module")
def completed_report() -> DecisionReport:
    problem = DecisionProblem(
        id="decision-chat-store",
        question="Should the team launch the pilot?",
        context="Internal deliberation context must not leak through member drafts.",
        options=[
            DecisionOption(id="launch", description="Launch with checkpoints"),
            DecisionOption(id="wait", description="Wait for more evidence"),
        ],
    )
    return asyncio.run(
        ThinkTank(
            catalog=Catalog.builtin(),
            runtimes=DeterministicRuntime(demo_responder),
        ).decide(problem, organization_id="product-roundtable", persist=False)
    )


def test_new_list_and_load_are_owner_only(tmp_path: Path) -> None:
    root = tmp_path / "chats"
    store = LocalChatStore(root)

    session = store.new(title="Launch discussion")
    path = root / f"{session.id}.json"

    assert stat.S_IMODE(root.stat().st_mode) == 0o700
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert store.load(session.id) == session
    assert [item.model_dump() for item in store.list()] == [
        {
            "id": session.id,
            "title": "Launch discussion",
            "turn_count": 0,
            "context_turn_count": 0,
            "created_at": session.created_at,
            "updated_at": session.updated_at,
        }
    ]

    with pytest.raises(ValueError, match="invalid chat session id"):
        store.load("../outside")


def test_context_contains_only_users_and_confirmed_public_decisions(
    tmp_path: Path,
    completed_report: DecisionReport,
) -> None:
    store = LocalChatStore(tmp_path / "chats")
    session = store.new()

    store.append(session.id, "Please decide whether we should launch.")
    updated = store.append(session.id, completed_report)
    context = store.build_context(session.id)

    assert [item.kind for item in context] == ["user_message", "confirmed_decision"]
    assert [item.role for item in context] == ["user", "assistant"]
    assert context[0].content == "Please decide whether we should launch."
    summary = json.loads(context[1].content)
    assert summary["run_id"] == completed_report.run_id
    assert summary["question"] == completed_report.frozen_problem.question
    assert summary["selected_option_id"] == completed_report.selected_option_id
    assert context[1].run_id == completed_report.run_id

    forbidden = {
        "messages",
        "runtime_calls",
        "audit_events",
        "request_snapshot",
        "frozen_problem",
        "memory_ids",
        "tool_artifact_ids",
        "native_memory_fingerprints",
    }
    assert forbidden.isdisjoint(summary)
    assert forbidden.isdisjoint(updated.turns[-1].public_summary.model_dump())

    with pytest.raises(TypeError, match="accepts only"):
        store.append(session.id, {"kind": "draft", "content": "private draft"})  # type: ignore[arg-type]


def test_clear_context_retains_history_and_starts_a_new_window(
    tmp_path: Path,
    completed_report: DecisionReport,
) -> None:
    store = LocalChatStore(tmp_path / "chats")
    session = store.new()
    store.append_user(session.id, "Old question")
    store.append_decision(session.id, completed_report)

    cleared = store.clear_context(session.id)

    assert len(cleared.turns) == 2
    assert cleared.context_start == 2
    assert store.build_context(session.id) == []
    assert store.list()[0].turn_count == 2
    assert store.list()[0].context_turn_count == 0

    store.append_user(session.id, "New question")
    context = store.context(session.id)
    assert [(item.kind, item.content) for item in context] == [
        ("user_message", "New question")
    ]
    assert len(store.load(session.id).turns) == 3


def test_failed_replace_preserves_previous_session(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "chats"
    store = LocalChatStore(root)
    session = store.new()
    store.append_user(session.id, "Persisted turn")
    path = root / f"{session.id}.json"
    before = path.read_bytes()

    def fail_replace(_source: Path, _target: Path) -> None:
        raise OSError("simulated replace failure")

    monkeypatch.setattr(chat_store_module.os, "replace", fail_replace)
    with pytest.raises(OSError, match="simulated replace failure"):
        store.append_user(session.id, "Must not partially persist")

    assert path.read_bytes() == before
    assert [turn.content for turn in store.load(session.id).turns] == ["Persisted turn"]
    assert not list(root.glob(".*.tmp"))


def test_symlinks_and_tampered_public_summaries_are_rejected(
    tmp_path: Path,
    completed_report: DecisionReport,
) -> None:
    real_root = tmp_path / "real"
    real_root.mkdir()
    linked_root = tmp_path / "linked"
    linked_root.symlink_to(real_root, target_is_directory=True)
    with pytest.raises(ValueError, match="root cannot be a symlink"):
        LocalChatStore(linked_root)

    root = tmp_path / "chats"
    store = LocalChatStore(root)
    session = store.new()
    store.append_decision(session.id, completed_report)
    path = root / f"{session.id}.json"
    payload = json.loads(path.read_text(encoding="utf-8"))
    payload["turns"][0]["public_summary"]["messages"] = ["live private draft"]
    path.write_text(json.dumps(payload), encoding="utf-8")

    with pytest.raises(ValidationError, match="Extra inputs are not permitted"):
        store.load(session.id)


def test_two_store_instances_do_not_lose_concurrent_turns(tmp_path: Path) -> None:
    gate = Barrier(2)

    class RacingStore(LocalChatStore):
        synchronize_reads = False

        def _load_path(self, path: Path, *, expected_id: str):
            session = super()._load_path(path, expected_id=expected_id)
            if self.synchronize_reads:
                try:
                    gate.wait(timeout=0.15)
                except BrokenBarrierError:
                    pass
            return session

    root = tmp_path / "chats"
    first = RacingStore(root)
    session = first.new()
    second = RacingStore(root)
    first.synchronize_reads = True
    second.synchronize_reads = True
    errors: list[BaseException] = []

    def append(store: LocalChatStore, content: str) -> None:
        try:
            store.append_user(session.id, content)
        except BaseException as exc:  # pragma: no cover - asserted below
            errors.append(exc)

    threads = [
        Thread(target=append, args=(first, "from-first")),
        Thread(target=append, args=(second, "from-second")),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=2)

    assert not errors
    assert all(not thread.is_alive() for thread in threads)
    first.synchronize_reads = False
    assert sorted(turn.content for turn in first.load(session.id).turns) == [
        "from-first",
        "from-second",
    ]
    lock_path = root / f".{session.id}.lock"
    assert stat.S_IMODE(lock_path.stat().st_mode) == 0o600


def test_session_lock_blocks_a_second_process(tmp_path: Path) -> None:
    root = tmp_path / "chats"
    store = LocalChatStore(root)
    session = store.new()
    context = multiprocessing.get_context("spawn")
    ready = context.Event()
    done = context.Event()
    process = context.Process(
        target=_append_chat_in_child,
        args=(str(root), session.id, ready, done),
    )
    started = False

    try:
        with store._exclusive_session(session.id):
            process.start()
            started = True
            assert ready.wait(timeout=5)
            assert not done.wait(timeout=0.2)
        assert done.wait(timeout=5)
        process.join(timeout=5)
        assert process.exitcode == 0
    finally:
        if started:
            if process.is_alive():
                process.terminate()
            process.join(timeout=5)

    assert [turn.content for turn in store.load(session.id).turns] == ["from-child-process"]
