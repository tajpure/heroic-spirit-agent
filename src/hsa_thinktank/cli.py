"""Command-line surface for catalogs, profiles, decisions, memory and approvals."""

from __future__ import annotations

import argparse
import asyncio
import json
import shlex
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Sequence

from pydantic import ValidationError

from . import __version__
from .catalog import Catalog
from .chat import ThinkTankChatDriver
from .chat_store import LocalChatStore
from .demo import demo_responder
from .errors import BackendUnavailable, HSAError
from .finalization import finalize_approved_decision, sync_memory_outbox
from .memory import (
    ApprovalError as ApprovalStoreError,
    ApprovalLevel,
    ApprovalStore,
    InstitutionalMemoryStore,
    MemoryError as InstitutionalMemoryError,
)
from .models import DecisionOption, DecisionProblem, content_hash
from .orchestrator import ThinkTank
from .profile_manager import HermesProfileManager, hermes_profile_name, render_soul
from .run_store import LocalRunStore
from .routing import AUTO_ORGANIZATION_ID, MeetingRouter
from .runtime import DeterministicRuntime, HermesProfileRuntime, ScriptedRuntime


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hsa",
        description="有来源约束、可审计的多 HSA Hermes 智库",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--catalog-dir", help="外部 catalog 根目录（含 hsas/ 与 organizations/）")
    subparsers = parser.add_subparsers(dest="command", required=True)

    catalog = subparsers.add_parser("catalog", help="列出 HSA 与组织")
    catalog.add_argument("--json", action="store_true", help="输出 JSON")

    doctor = subparsers.add_parser("doctor", help="检查 catalog、Hermes 与 Profile")
    doctor.add_argument("--json", action="store_true", help="输出 JSON")

    profiles = subparsers.add_parser("profiles", help="规划或创建独立 Hermes Profiles")
    profile_sub = profiles.add_subparsers(dest="profile_command", required=True)
    plan = profile_sub.add_parser("plan", help="仅显示将执行的 Profile 变更")
    _add_profile_selection(plan)
    bootstrap = profile_sub.add_parser("bootstrap", help="显式创建/更新 Hermes Profiles")
    _add_profile_selection(bootstrap)
    bootstrap.add_argument("--dry-run", action="store_true", help="等价于 profiles plan")
    bootstrap.add_argument("--clone-from", default="default")
    bootstrap.add_argument("--terminal-backend", choices=("local",), default="local")
    bootstrap.add_argument("--overwrite-soul", action="store_true")

    decide = subparsers.add_parser("decide", help="运行一次组织化 HSA 决策")
    decide.add_argument(
        "--organization",
        default=AUTO_ORGANIZATION_ID,
        help="组织 ID；默认 auto，根据当前决策上下文自动选择协议和参会 HSA",
    )
    decision_input = decide.add_mutually_exclusive_group(required=True)
    decision_input.add_argument("--question")
    decision_input.add_argument(
        "--problem-file",
        type=Path,
        help="完整 DecisionProblem JSON；用于传入 criteria、hard constraints 与 evidence",
    )
    decide.add_argument("--context")
    decide.add_argument("--context-file", type=Path)
    decide.add_argument(
        "--option",
        action="append",
        default=[],
        metavar="ID=DESCRIPTION",
        help="重复传入；若省略则由主席先生成并冻结候选方案",
    )
    decide.add_argument("--risk-tier", choices=("low", "medium", "high"))
    decide.add_argument("--backend", choices=("demo", "scripted", "hermes"), default="hermes")
    decide.add_argument("--script", type=Path, help="scripted backend JSON")
    decide.add_argument(
        "--profile-command",
        action="append",
        default=[],
        metavar="HSA_ID=COMMAND",
        help="覆盖 Hermes profile alias",
    )
    decide.add_argument("--tool-grant", action="append", default=[])
    decide.add_argument("--user-id")
    decide.add_argument("--max-parallel", type=int)
    decide.add_argument("--timeout", type=float, default=300.0)
    decide.add_argument("--max-turns", type=int, default=12)
    decide.add_argument("--workspace", type=Path, default=Path(".hsa/workspace"))
    decide.add_argument("--memory-db", type=Path, default=Path(".hsa/memory.sqlite"))
    decide.add_argument("--approval-db", type=Path, default=Path(".hsa/approvals.sqlite"))
    decide.add_argument("--runs-dir", type=Path, default=Path(".hsa/runs"))
    decide.add_argument("--output", type=Path)
    decide.add_argument("--no-persist", action="store_true", help="不保存 run artifact")
    decide.add_argument(
        "--route-only",
        action="store_true",
        help="只输出自动选会结果，不启动 Hermes、不写入 run artifact",
    )

    chat = subparsers.add_parser("chat", help="打开可实时观察多 HSA 会议的 TUI 聊天窗口")
    chat.add_argument(
        "--organization",
        default=AUTO_ORGANIZATION_ID,
        help="组织 ID；默认 auto，每轮根据聊天上下文自动选会",
    )
    chat.add_argument("--risk-tier", choices=("low", "medium", "high"), default="medium")
    chat.add_argument("--backend", choices=("demo", "hermes"), default="hermes")
    chat.add_argument("--session", help="恢复已有 chat session ID")
    chat.add_argument("--chat-dir", type=Path, default=Path(".hsa/chats"))
    chat.add_argument(
        "--profile-command",
        action="append",
        default=[],
        metavar="HSA_ID=COMMAND",
        help="覆盖 Hermes profile alias",
    )
    chat.add_argument("--tool-grant", action="append", default=[])
    chat.add_argument("--user-id")
    chat.add_argument("--max-parallel", type=int, default=4)
    chat.add_argument("--timeout", type=float, default=300.0)
    chat.add_argument("--max-turns", type=int, default=12)
    chat.add_argument("--workspace", type=Path, default=Path(".hsa/workspace"))
    chat.add_argument("--memory-db", type=Path, default=Path(".hsa/memory.sqlite"))
    chat.add_argument("--approval-db", type=Path, default=Path(".hsa/approvals.sqlite"))
    chat.add_argument("--runs-dir", type=Path, default=Path(".hsa/runs"))
    chat.add_argument("--no-persist", action="store_true", help="不保存 DecisionReport run bundle")

    memory = subparsers.add_parser("memory", help="检查或审批组织记忆候选")
    memory.add_argument("--db", type=Path, default=Path(".hsa/memory.sqlite"))
    memory_sub = memory.add_subparsers(dest="memory_command", required=True)
    memory_show = memory_sub.add_parser("show")
    memory_show.add_argument("memory_id")
    memory_approve = memory_sub.add_parser("approve")
    memory_approve.add_argument("memory_id")
    memory_approve.add_argument("--actor", default="human-memory-reviewer")
    memory_reject = memory_sub.add_parser("reject")
    memory_reject.add_argument("memory_id")
    memory_reject.add_argument("--actor", default="human-memory-reviewer")
    memory_reject.add_argument("--reason", default="")
    memory_sync = memory_sub.add_parser("sync-run", help="幂等执行已持久化 run 的 memory outbox")
    memory_sync.add_argument("run_id")
    memory_sync.add_argument("--runs-dir", type=Path, default=Path(".hsa/runs"))
    memory_sync.add_argument("--approval-db", type=Path, default=Path(".hsa/approvals.sqlite"))

    approvals = subparsers.add_parser("approvals", help="检查或处理 L2/L3 审批")
    approvals.add_argument("--db", type=Path, default=Path(".hsa/approvals.sqlite"))
    approval_sub = approvals.add_subparsers(dest="approval_command", required=True)
    approval_show = approval_sub.add_parser("show")
    approval_show.add_argument("request_id")
    approval_approve = approval_sub.add_parser("approve")
    approval_approve.add_argument("request_id")
    approval_approve.add_argument("--actor", required=True)
    approval_approve.add_argument("--level", choices=("L2", "L3"), default="L3")
    approval_approve.add_argument("--reason", default="")
    approval_reject = approval_sub.add_parser("reject")
    approval_reject.add_argument("request_id")
    approval_reject.add_argument("--actor", required=True)
    approval_reject.add_argument("--level", choices=("L2", "L3"), default="L3")
    approval_reject.add_argument("--reason", default="")
    approval_finalize = approval_sub.add_parser(
        "finalize", help="验证已批准请求并写入不可变 finalization record"
    )
    approval_finalize.add_argument("request_id")
    approval_finalize.add_argument("--runs-dir", type=Path, default=Path(".hsa/runs"))
    approval_finalize.add_argument("--memory-db", type=Path, default=Path(".hsa/memory.sqlite"))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        catalog = (
            Catalog.from_directory(args.catalog_dir) if args.catalog_dir else Catalog.builtin()
        )
        if args.command == "catalog":
            return _catalog_command(catalog, args)
        if args.command == "doctor":
            return _doctor_command(catalog, args)
        if args.command == "profiles":
            return _profiles_command(catalog, args)
        if args.command == "decide":
            return asyncio.run(_decide_command(catalog, args))
        if args.command == "chat":
            return _chat_command(catalog, args)
        if args.command == "memory":
            return _memory_command(args)
        if args.command == "approvals":
            return _approval_command(catalog, args)
        parser.error(f"unknown command: {args.command}")
    except (
        ValidationError,
        ValueError,
        HSAError,
        InstitutionalMemoryError,
        ApprovalStoreError,
        OSError,
        json.JSONDecodeError,
    ) as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    return 2


def _add_profile_selection(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--hsa", action="append", default=[], help="仅处理指定 HSA，可重复")


def _catalog_command(catalog: Catalog, args: argparse.Namespace) -> int:
    payload = {
        "hsas": [
            {
                "id": profile.id,
                "display_name": profile.display_name,
                "version": profile.version,
                "grounding_mode": profile.grounding_mode,
            }
            for profile in catalog.profiles.values()
        ],
        "organizations": [
            {
                "id": organization.id,
                "name": organization.name,
                "protocol": organization.protocol,
                "members": [member.hsa_id for member in organization.members],
            }
            for organization in catalog.organizations.values()
        ],
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print("HSA Profiles")
        for item in payload["hsas"]:
            print(f"  {item['id']}: {item['display_name']} ({item['version']})")
        print("Organizations")
        for item in payload["organizations"]:
            print(f"  {item['id']}: {item['name']} [{item['protocol']}]")
    return 0


def _doctor_command(catalog: Catalog, args: argparse.Namespace) -> int:
    hermes = shutil.which("hermes")
    version = None
    if hermes:
        result = subprocess.run([hermes, "--version"], text=True, capture_output=True, check=False)
        version = (
            (result.stdout or result.stderr).splitlines()[0] if result.returncode == 0 else None
        )
    manager = HermesProfileManager(hermes) if hermes else None
    profiles = []
    for profile_id in catalog.profiles:
        if manager is None:
            item = {
                "hsa_id": profile_id,
                "profile_name": hermes_profile_name(profile_id),
                "command": None,
                "profile_home": None,
                "exists": False,
                "soul_present": False,
                "soul_matches_hsa": False,
                "soul_catalog_fingerprint": None,
                "soul_content_hash": None,
                "memory_enabled": False,
                "user_profile_enabled": False,
                "external_memory_provider": "",
                "terminal_backend": "unknown",
            }
        else:
            item = manager.health(profile_id).__dict__
        item["ready"] = bool(
            item["command"]
            and item["exists"]
            and item["soul_present"]
            and item["soul_matches_hsa"]
            and item["soul_catalog_fingerprint"] == catalog.profile(profile_id).fingerprint
            and item["soul_content_hash"] == content_hash(render_soul(catalog.profile(profile_id)))
            and item["memory_enabled"]
            and item["user_profile_enabled"]
            and not item["external_memory_provider"]
            and item["terminal_backend"] == "local"
        )
        profiles.append(item)
    payload = {
        "catalog": "ok",
        "hermes": {"command": hermes, "version": version},
        "profiles": profiles,
        "ready": bool(hermes and all(item["ready"] for item in profiles)),
    }
    if args.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(f"Catalog: ok ({len(catalog.profiles)} HSA, {len(catalog.organizations)} org)")
        print(f"Hermes: {version or 'not found'}")
        for item in profiles:
            state = "ready" if item["ready"] else "not ready"
            print(
                f"Profile {item['profile_name']}: {state}; "
                f"command={item['command'] or 'missing'}; "
                f"memory={item['memory_enabled']}/{item['user_profile_enabled']}; "
                f"terminal={item['terminal_backend']}"
            )
    return 0 if payload["ready"] else 4


def _profiles_command(catalog: Catalog, args: argparse.Namespace) -> int:
    profile_ids = _selected_profiles(catalog, args.hsa)
    if args.profile_command == "plan" or getattr(args, "dry_run", False):
        for profile_id in profile_ids:
            name = hermes_profile_name(profile_id)
            print(
                shlex.join(
                    [
                        "hermes",
                        "profile",
                        "create",
                        name,
                        "--clone-from",
                        getattr(args, "clone_from", "default"),
                        "--clone",
                    ]
                )
            )
            print(f"# write {name}/SOUL.md; enable private memory; terminal=local")
        return 0
    manager = HermesProfileManager()
    for profile_id in profile_ids:
        health = manager.bootstrap(
            catalog.profile(profile_id),
            clone_from=args.clone_from,
            terminal_backend=args.terminal_backend,
            overwrite_soul=args.overwrite_soul,
        )
        print(json.dumps(health.__dict__, ensure_ascii=False))
    return 0


async def _decide_command(catalog: Catalog, args: argparse.Namespace) -> int:
    if args.problem_file is not None:
        if args.context is not None or args.context_file is not None or args.option:
            raise ValueError(
                "--problem-file cannot be combined with --context, --context-file or --option"
            )
        problem = DecisionProblem.model_validate_json(args.problem_file.read_text(encoding="utf-8"))
        updates = {}
        if args.risk_tier is not None:
            updates["risk_tier"] = args.risk_tier
        if args.max_parallel is not None:
            updates["max_parallel"] = args.max_parallel
        if args.tool_grant:
            updates["user_tool_grants"] = list(
                dict.fromkeys([*problem.user_tool_grants, *args.tool_grant])
            )
        if updates:
            problem = DecisionProblem.model_validate(
                {**problem.model_dump(mode="python"), **updates}
            )
    else:
        context = args.context or ""
        if args.context_file:
            context = args.context_file.read_text(encoding="utf-8")
        options = [_parse_option(value) for value in args.option]
        problem = DecisionProblem(
            question=args.question,
            context=context,
            options=options,
            risk_tier=args.risk_tier or "medium",
            max_parallel=args.max_parallel if args.max_parallel is not None else 4,
            user_tool_grants=args.tool_grant,
        )
    meeting_selection = MeetingRouter(catalog).select(
        problem,
        requested_organization_id=args.organization,
    )
    organization = meeting_selection.effective_organization
    if args.route_only:
        print(meeting_selection.model_dump_json(indent=2))
        return 0
    if args.backend == "demo":
        runtimes = DeterministicRuntime(demo_responder)
    elif args.backend == "scripted":
        if args.script is None:
            raise ValueError("--backend scripted requires --script")
        runtimes = ScriptedRuntime(json.loads(args.script.read_text(encoding="utf-8")))
    else:
        runtimes = _build_hermes_runtime_map(
            catalog,
            [member.hsa_id for member in organization.members],
            args,
        )

    args.memory_db.parent.mkdir(parents=True, exist_ok=True)
    args.approval_db.parent.mkdir(parents=True, exist_ok=True)
    with (
        InstitutionalMemoryStore(args.memory_db) as memory_store,
        ApprovalStore(args.approval_db) as approval_store,
    ):
        tank = ThinkTank(
            catalog=catalog,
            runtimes=runtimes,
            memory_store=memory_store,
            approval_store=approval_store,
            run_store=LocalRunStore(args.runs_dir),
        )
        report = await tank.decide(
            problem,
            organization_id=args.organization,
            meeting_selection=meeting_selection,
            user_id=args.user_id,
            persist=not args.no_persist,
        )
    rendered = report.model_dump_json(indent=2) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(rendered, encoding="utf-8")
    print(
        json.dumps(
            {
                "run_id": report.run_id,
                "status": report.status,
                "selected_option_id": report.selected_option_id,
                "selected_option": report.selected_option,
                "confidence": report.confidence,
                "meeting_selection": {
                    "mode": report.meeting_selection.mode,
                    "organization_id": report.meeting_selection.organization_id,
                    "protocol": report.meeting_selection.protocol,
                    "selected_hsa_ids": report.meeting_selection.selected_hsa_ids,
                    "matched_signals": report.meeting_selection.matched_signals,
                    "reasons": report.meeting_selection.reasons,
                },
                "approval_ids": report.approval_ids,
                "trace_root_hash": report.trace_root_hash,
                "output": str(args.output) if args.output else None,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0 if report.status == "decided" else 3


def _chat_command(catalog: Catalog, args: argparse.Namespace) -> int:
    if args.backend == "demo":
        runtimes = DeterministicRuntime(demo_responder)
        runtime_mode = "demo-live"
    else:
        runtimes = _build_hermes_runtime_map(catalog, sorted(catalog.profiles), args)
        runtime_mode = "hermes-auto-stream"

    args.memory_db.parent.mkdir(parents=True, exist_ok=True)
    args.approval_db.parent.mkdir(parents=True, exist_ok=True)
    with (
        InstitutionalMemoryStore(args.memory_db) as memory_store,
        ApprovalStore(args.approval_db) as approval_store,
    ):
        tank = ThinkTank(
            catalog=catalog,
            runtimes=runtimes,
            memory_store=memory_store,
            approval_store=approval_store,
            run_store=LocalRunStore(args.runs_dir),
        )
        driver = ThinkTankChatDriver(
            tank=tank,
            chat_store=LocalChatStore(args.chat_dir),
            session_id=args.session,
            organization_id=args.organization,
            risk_tier=args.risk_tier,
            user_id=args.user_id,
            persist_runs=not args.no_persist,
            runtime_mode=runtime_mode,
            tool_grants=args.tool_grant,
            max_parallel=args.max_parallel,
        )
        _launch_chat_tui(driver, catalog)
    return 0


def _launch_chat_tui(driver: ThinkTankChatDriver, catalog: Catalog) -> None:
    try:
        from .tui import HSAChatApp
    except ModuleNotFoundError as exc:
        if exc.name == "textual":
            raise BackendUnavailable(
                "TUI dependency is missing; install hsa-thinktank[tui]"
            ) from None
        raise
    HSAChatApp(driver=driver, catalog=catalog).run()


def _build_hermes_runtime_map(
    catalog: Catalog,
    hsa_ids: Sequence[str],
    args: argparse.Namespace,
) -> dict[str, HermesProfileRuntime]:
    overrides = _parse_assignments(args.profile_command)
    manager = HermesProfileManager()
    args.workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
    runtime_map: dict[str, HermesProfileRuntime] = {}
    for hsa_id in hsa_ids:
        profile = catalog.profile(hsa_id)
        health = manager.health(hsa_id)
        health_failures = []
        if not health.exists:
            health_failures.append("profile home missing")
        if not health.soul_matches_hsa:
            health_failures.append("SOUL identity mismatch")
        if health.soul_catalog_fingerprint != profile.fingerprint:
            health_failures.append("SOUL catalog fingerprint is stale")
        if health.soul_content_hash != content_hash(render_soul(profile)):
            health_failures.append("SOUL content differs from the catalog rendering")
        if not health.memory_enabled or not health.user_profile_enabled:
            health_failures.append("native memory is disabled")
        if health.external_memory_provider:
            health_failures.append("external memory provider must be disabled")
        if health.terminal_backend != "local":
            health_failures.append("terminal backend is not local")
        if health_failures:
            raise BackendUnavailable(
                f"Hermes profile {health.profile_name} is not ready: "
                + "; ".join(health_failures)
                + "; run `hsa profiles bootstrap` and `hsa doctor`"
            )

        override = overrides.get(hsa_id)
        command: str | list[str] | None
        command = shlex.split(override) if override else health.command
        if not command:
            raise BackendUnavailable(
                f"missing Hermes profile {hermes_profile_name(hsa_id)}; "
                "run `hsa profiles bootstrap` first"
            )
        member_workspace = args.workspace / hsa_id
        member_workspace.mkdir(parents=True, exist_ok=True, mode=0o700)
        runtime_map[hsa_id] = HermesProfileRuntime(
            profile=health.profile_name,
            executable=command,
            profile_home=health.profile_home,
            max_turns=args.max_turns,
            timeout_seconds=args.timeout,
            cwd=member_workspace,
        )
    return runtime_map


def _memory_command(args: argparse.Namespace) -> int:
    if args.memory_command == "sync-run":
        run_store = LocalRunStore(args.runs_dir)
        outbox = run_store.load_outbox(args.run_id)
        with InstitutionalMemoryStore(args.db) as store:
            if outbox.approval_operation is None:
                value = sync_memory_outbox(
                    run_id=args.run_id,
                    memory_store=store,
                    run_store=run_store,
                )
            else:
                with ApprovalStore(args.approval_db) as approval_store:
                    value = sync_memory_outbox(
                        run_id=args.run_id,
                        memory_store=store,
                        run_store=run_store,
                        approval_store=approval_store,
                    )
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    with InstitutionalMemoryStore(args.db) as store:
        if args.memory_command == "show":
            value = store.get(args.memory_id)
        elif args.memory_command == "approve":
            value = store.approve(args.memory_id, approver_id=args.actor)
        else:
            value = store.reject(args.memory_id, approver_id=args.actor, reason=args.reason)
    print(value.model_dump_json(indent=2))
    return 0


def _approval_command(catalog: Catalog, args: argparse.Namespace) -> int:
    if args.approval_command == "finalize":
        with (
            ApprovalStore(args.db) as approval_store,
            InstitutionalMemoryStore(args.memory_db) as memory_store,
        ):
            value = finalize_approved_decision(
                catalog=catalog,
                request_id=args.request_id,
                approval_store=approval_store,
                memory_store=memory_store,
                run_store=LocalRunStore(args.runs_dir),
            )
        print(json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True))
        return 0

    with ApprovalStore(args.db) as store:
        if args.approval_command == "show":
            value = store.get(args.request_id)
        elif args.approval_command == "approve":
            value = store.approve(
                args.request_id,
                actor_id=args.actor,
                approver_level=ApprovalLevel(args.level),
                reason=args.reason,
            )
        else:
            value = store.reject(
                args.request_id,
                actor_id=args.actor,
                approver_level=ApprovalLevel(args.level),
                reason=args.reason,
            )
    print(value.model_dump_json(indent=2))
    return 0


def _selected_profiles(catalog: Catalog, values: list[str]) -> list[str]:
    selected = values or list(catalog.profiles)
    for profile_id in selected:
        catalog.profile(profile_id)
    return selected


def _parse_option(value: str) -> DecisionOption:
    if "=" not in value:
        raise ValueError(f"option must be ID=DESCRIPTION: {value}")
    option_id, description = value.split("=", 1)
    return DecisionOption(id=option_id.strip(), description=description.strip())


def _parse_assignments(values: list[str]) -> dict[str, str]:
    result: dict[str, str] = {}
    for value in values:
        if "=" not in value:
            raise ValueError(f"assignment must be NAME=VALUE: {value}")
        key, item = value.split("=", 1)
        result[key.strip()] = item.strip()
    return result


if __name__ == "__main__":
    raise SystemExit(main())
