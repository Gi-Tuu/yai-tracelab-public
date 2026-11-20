"""Judge 接口与脚本占位实现（不调用任何模型）。"""

from __future__ import annotations

from typing import Any, Protocol


class JudgeClient(Protocol):
    judge_id: str
    judge_version: str
    model: str
    prompt_version: str
    execution_origin: str

    def evaluate(self, judge_input: dict[str, Any], linkage: dict[str, Any]) -> dict[str, Any]:
        """返回 verdict 字典（verdict/reason/dimensions/evidence 等）。"""
        ...


class ScriptedJudge:
    """流水线占位 Judge：永远 REJECT，绝不伪造 PASS/FAIL 分数。

    只用于在没有模型/API 时验证"白名单输入 → 调用记录 → 扩展结果 → 校验"链路；
    产出的结果必须标 `SCRIPTED_TEST_NOT_EXECUTED`，不能当作参赛真实结果。
    """

    judge_id = "scripted-final-round"
    judge_version = "0.1-scripted"
    model = "scripted"
    prompt_version = "final-round-scripted-v0"
    execution_origin = "SCRIPTED_TEST_NOT_EXECUTED"

    def evaluate(self, judge_input: dict[str, Any], linkage: dict[str, Any]) -> dict[str, Any]:
        return {
            "verdict": "REJECT",
            "agent_task_success": None,
            "business_result_correct": None,
            "reason": (
                "SCRIPTED_TEST_NOT_EXECUTED：当前为脚本占位 Judge，未调用真实模型；"
                "仅用于验证输入白名单、收据链与扩展结果结构。"
            ),
            "dimensions": [
                {
                    "name": "judge_execution",
                    "conclusion": "NOT_APPLICABLE",
                    "reason": "未调用真实模型",
                    "evidence_event_ids": [],
                }
            ],
            "evidence_event_ids": [],
            "first_fault_event_id": None,
            "root_causes": [],
            "uncertainty": {"level": "unknown", "reason": "脚本占位，无真实模型输出"},
        }
