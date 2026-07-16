from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from hsa_thinktank.catalog import Catalog
from hsa_thinktank.cli import main
from hsa_thinktank.demo import demo_responder
from hsa_thinktank.models import content_hash
from hsa_thinktank.profile_manager import ProfileHealth, render_soul
from hsa_thinktank.runtime import DeterministicRuntime


class _HealthyProfileManager:
    def __init__(self, *_args, **_kwargs) -> None:
        self.catalog = Catalog.builtin()

    def health(self, hsa_id: str) -> ProfileHealth:
        profile = self.catalog.profile(hsa_id)
        return ProfileHealth(
            hsa_id=hsa_id,
            profile_name=f"hsa-{hsa_id}",
            command=f"/fake/hsa-{hsa_id}",
            profile_home=f"/fake/profiles/hsa-{hsa_id}",
            exists=True,
            soul_present=True,
            soul_matches_hsa=True,
            soul_catalog_fingerprint=profile.fingerprint,
            soul_content_hash=content_hash(render_soul(profile)),
            memory_enabled=True,
            user_profile_enabled=True,
            external_memory_provider="",
            terminal_backend="local",
        )


def test_problem_file_exposes_full_decision_contract_to_cli(tmp_path: Path, capsys) -> None:
    problem_path = tmp_path / "problem.json"
    problem_path.write_text(
        json.dumps(
            {
                "id": "decision-from-file",
                "question": "Should we launch?",
                "options": [
                    {"id": "launch", "description": "Launch with rollback"},
                    {"id": "wait", "description": "Wait for more evidence"},
                ],
                "criteria": [
                    {
                        "id": "legal",
                        "description": "Must be legally permitted",
                        "weight": 1.0,
                        "hard_constraint": True,
                    }
                ],
                "evidence": [
                    {
                        "id": "review",
                        "title": "Legal review",
                        "content": "Counsel approved the scoped pilot.",
                    }
                ],
                "risk_tier": "medium",
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "decide",
            "--organization",
            "product-roundtable",
            "--problem-file",
            str(problem_path),
            "--backend",
            "demo",
            "--memory-db",
            str(tmp_path / "memory.sqlite"),
            "--approval-db",
            str(tmp_path / "approvals.sqlite"),
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    )

    output = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert output["status"] == "decided"
    run_dir = tmp_path / "runs" / output["run_id"]
    assert (run_dir / "completion.json").is_file()


def test_problem_file_rejects_ambiguous_inline_overrides(tmp_path: Path, capsys) -> None:
    problem_path = tmp_path / "problem.json"
    problem_path.write_text(
        json.dumps(
            {
                "question": "Choose",
                "options": [
                    {"id": "one", "description": "First"},
                    {"id": "two", "description": "Second"},
                ],
            }
        ),
        encoding="utf-8",
    )

    exit_code = main(
        [
            "decide",
            "--organization",
            "product-roundtable",
            "--problem-file",
            str(problem_path),
            "--context",
            "ambiguous override",
            "--backend",
            "demo",
        ]
    )

    assert exit_code == 2
    assert "cannot be combined" in capsys.readouterr().err


def test_route_only_selects_hsas_without_running_a_backend(tmp_path: Path, capsys) -> None:
    exit_code = main(
        [
            "decide",
            "--question",
            "如何同时改善产品体验与系统反馈？",
            "--route-only",
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    )

    selection = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert selection["mode"] == "auto"
    assert selection["organization_id"] == "product-roundtable"
    assert selection["selected_hsa_ids"] == ["steve-jobs", "donella-meadows"]
    assert not (tmp_path / "runs").exists()


def test_doctor_can_be_ready_without_docker(monkeypatch: pytest.MonkeyPatch, capsys) -> None:
    monkeypatch.setattr("hsa_thinktank.cli.HermesProfileManager", _HealthyProfileManager)
    monkeypatch.setattr("hsa_thinktank.cli.shutil.which", lambda command: f"/fake/{command}")
    monkeypatch.setattr(
        "hsa_thinktank.cli.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=args,
            returncode=0,
            stdout="Hermes Agent test\n",
            stderr="",
        ),
    )

    exit_code = main(["doctor", "--json"])

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["ready"] is True
    assert "docker" not in payload
    assert {profile["terminal_backend"] for profile in payload["profiles"]} == {"local"}


def test_hermes_backend_runs_without_docker_preflight(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys
) -> None:
    monkeypatch.setattr("hsa_thinktank.cli.HermesProfileManager", _HealthyProfileManager)
    monkeypatch.setattr(
        "hsa_thinktank.cli.HermesProfileRuntime",
        lambda **_kwargs: DeterministicRuntime(demo_responder),
    )

    exit_code = main(
        [
            "decide",
            "--organization",
            "product-roundtable",
            "--question",
            "Choose a direction",
            "--option",
            "one=First",
            "--option",
            "two=Second",
            "--backend",
            "hermes",
            "--workspace",
            str(tmp_path / "workspace"),
            "--memory-db",
            str(tmp_path / "memory.sqlite"),
            "--approval-db",
            str(tmp_path / "approvals.sqlite"),
            "--runs-dir",
            str(tmp_path / "runs"),
        ]
    )

    payload = json.loads(capsys.readouterr().out)
    assert exit_code == 0
    assert payload["status"] == "decided"


def test_chat_command_launches_tui_with_persistent_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = {}

    def fake_launch(driver, catalog) -> None:
        launched["driver"] = driver
        launched["catalog"] = catalog

    monkeypatch.setattr("hsa_thinktank.cli._launch_chat_tui", fake_launch)

    exit_code = main(
        [
            "chat",
            "--backend",
            "demo",
            "--chat-dir",
            str(tmp_path / "chats"),
            "--memory-db",
            str(tmp_path / "memory.sqlite"),
            "--approval-db",
            str(tmp_path / "approvals.sqlite"),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--no-persist",
        ]
    )

    assert exit_code == 0
    assert launched["driver"].runtime_mode == "demo-live"
    assert launched["driver"].session_id.startswith("chat-")
    assert launched["catalog"].profiles
    assert (tmp_path / "chats" / f"{launched['driver'].session_id}.json").is_file()


def test_hermes_chat_passes_profile_home_to_streaming_runtime(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    launched = {}
    runtime_kwargs: list[dict[str, object]] = []

    def fake_runtime(**kwargs):
        runtime_kwargs.append(kwargs)
        return DeterministicRuntime(demo_responder)

    monkeypatch.setattr("hsa_thinktank.cli.HermesProfileManager", _HealthyProfileManager)
    monkeypatch.setattr("hsa_thinktank.cli.HermesProfileRuntime", fake_runtime)
    monkeypatch.setattr(
        "hsa_thinktank.cli._launch_chat_tui",
        lambda driver, _catalog: launched.update(driver=driver),
    )

    exit_code = main(
        [
            "chat",
            "--backend",
            "hermes",
            "--chat-dir",
            str(tmp_path / "chats"),
            "--workspace",
            str(tmp_path / "workspace"),
            "--memory-db",
            str(tmp_path / "memory.sqlite"),
            "--approval-db",
            str(tmp_path / "approvals.sqlite"),
            "--runs-dir",
            str(tmp_path / "runs"),
            "--no-persist",
        ]
    )

    assert exit_code == 0
    assert launched["driver"].runtime_mode == "hermes-auto-stream"
    assert len(runtime_kwargs) == len(Catalog.builtin().profiles)
    assert {
        kwargs["profile_home"] for kwargs in runtime_kwargs
    } == {
        f"/fake/profiles/hsa-{hsa_id}" for hsa_id in Catalog.builtin().profiles
    }
