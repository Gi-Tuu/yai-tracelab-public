"""R2-G1.2 P2：Judge 业务类型必须显式且已知。"""

from __future__ import annotations

import pytest

from tracelab.final_round.judge_input import (
    BUSINESS_TYPES,
    JudgeInputContractError,
    normalize_judge_input,
)
from tracelab.final_round.llm_judge import OpenAICompatJudge, build_messages


def _raw(business_type: str) -> dict:
    return {
        "business_type": business_type,
        "domain": "salary",
        "user_request": "查询工资",
        "context": {"enterprise_id": "E1"},
        "business_objective": {"business_goal": "只查询"},
        "observable_run": {
            "events": [
                {
                    "event_id": "R1-E01",
                    "seq": 1,
                    "step_id": "INPUT",
                    "step_status": "SUCCEEDED",
                    "observed_input": {},
                    "observed_output": {},
                    "tool_calls": [],
                }
            ]
        },
    }


def test_supported_types_are_explicit():
    assert BUSINESS_TYPES == frozenset({"invoice", "ai_consultant"})
    for business_type in BUSINESS_TYPES:
        payload = normalize_judge_input(_raw(business_type), linkage={"run_id": "R1"})
        assert payload["judge_input"]["business_type"] == business_type


@pytest.mark.parametrize("business_type", ["unknown", "ANALYST", "", None])
def test_unknown_type_is_rejected_before_judge(business_type):
    with pytest.raises(JudgeInputContractError):
        normalize_judge_input(_raw(business_type), linkage={"run_id": "R1"})


def test_judge_prompt_selection_rejects_unknown_type():
    with pytest.raises(JudgeInputContractError):
        build_messages(_raw("unknown"))
    with pytest.raises(JudgeInputContractError):
        OpenAICompatJudge().prompt_digest("unknown")
