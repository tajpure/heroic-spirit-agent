"""Schema-valid deterministic responses for zero-cost end-to-end demonstrations."""

from __future__ import annotations

from typing import Any

from .runtime import AgentInvocation


def demo_responder(invocation: AgentInvocation) -> dict[str, Any]:
    response_type = invocation.metadata.get("response_type")
    option_ids = list(invocation.metadata.get("option_ids") or [])
    hard_constraint_ids = list(invocation.metadata.get("hard_constraint_ids") or [])
    if response_type == "GeneratedOptions":
        return {
            "options": [
                {"id": "proceed", "description": "推进，并设置可逆的阶段检查点"},
                {"id": "defer", "description": "暂缓，先补齐关键证据"},
                {"id": "stop", "description": "停止该方向并重新分配资源"},
            ],
            "generation_note": "确定性离线演示生成的通用候选方案",
        }
    if not option_ids:
        option_ids = ["proceed", "defer"]
    preferred = option_ids[0]
    scores = {
        option_id: round(max(0.1, 0.82 - index * 0.22), 2)
        for index, option_id in enumerate(option_ids)
    }
    ballot = {
        "preferred_option_id": preferred,
        "option_scores": scores,
        "criterion_scores": {},
        "constraint_results": (
            {
                option_id: {constraint_id: True for constraint_id in hard_constraint_ids}
                for option_id in option_ids
            }
            if hard_constraint_ids
            else {}
        ),
        "confidence": 0.68,
        "claims": [
            {
                "claim": f"离线演示按 {invocation.hsa_id} 的组织角色偏向 {preferred}",
                "basis": "speculative",
                "principle_ids": [],
                "evidence_ids": [],
                "memory_ids": [],
                "tool_artifact_ids": [],
            }
        ],
        "assumptions": ["这是确定性演示，不代表真实 Hermes/HSA 判断"],
        "risks": [],
        "next_actions": ["切换 --backend hermes 进行真实有记忆、有工具的评估"],
    }
    if response_type == "RedTeamCritique":
        alternative = option_ids[1] if len(option_ids) > 1 else preferred
        return {
            "attacks": [
                {
                    "attack_id": "demo-risk",
                    "option_id": preferred,
                    "severity": "medium",
                    "claim": "执行假设尚未经过真实数据验证",
                    "evidence_needed": "小规模实验结果",
                    "suggested_mitigation": "设置可逆里程碑",
                    "evidence_ids": [],
                    "tool_artifact_ids": [],
                }
            ],
            "strongest_alternative_id": alternative,
        }
    if response_type == "RedTeamRebuttal":
        return {
            "revised_ballot": ballot,
            "dispositions": [
                {
                    "attack_id": "demo-risk",
                    "status": "mitigated",
                    "response": "加入阶段检查点并在证据不足时停止",
                }
            ],
        }
    if response_type == "ExecutiveDecision":
        return {"ballot": ballot, "override_reason": ""}
    return ballot
