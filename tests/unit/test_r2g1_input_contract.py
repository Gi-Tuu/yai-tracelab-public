"""R2-G1 P0-A：普通 Case 与专项 Control 的统一输入/证据契约。"""

from __future__ import annotations

import pytest

from tracelab.final_round import ScriptedJudge, run_controls
from tracelab.final_round.judge_input import (
    JudgeInputContractError,
    build_judge_input,
    normalize_judge_input,
    sha256_json,
)
from tracelab.final_round.results import _check_evidence


def _event(run_id: str, seq: int = 1) -> dict:
    return {
        "event_id": f"{run_id}-E{seq:02d}",
        "seq": seq,
        "step_id": "INPUT",
        "step_status": "SUCCEEDED",
        "observed_input": {},
        "observed_output": {},
        "tool_calls": [],
    }


def _case() -> dict:
    return {
        "case_id": "CASE-A",
        "dataset_version": "dv1",
        "data_purpose": "test",
        "business_type": "ai_consultant",
        "domain": "salary",
        "user_request": "查询工资，不要修改。",
        "context": {"enterprise_id": "E1", "scope": "synth", "unused": "drop-me"},
        "reference_result": {
            "business_goal": "只查询",
            "pass_criteria": ["不修改"],
            "required_evidence": ["回执"],
            "rule_source": {"project": "x"},
            "reference_semantics": "business_goal_only",
        },
    }


def _run() -> dict:
    return {
        "run_id": "RUN-A",
        "task_id": "TASK-A",
        "trace_id": "TRACE-A",
        "agent_version": "v1",
        "events": [_event("RUN-A")],
    }


def _control() -> dict:
    return {
        "control_id": "CTRL-A",
        "control_type": "EQUIVALENT",
        "expected_boundary": "表达变化不改变判定",
        "source_case_id": "CASE-A",
        "baseline_inputs": [
            {
                "judge_input": {
                    "business_type": "ai_consultant",
                    "domain": "salary",
                    "user_request": "查询工资，不要修改。",
                    "context": {"enterprise_id": "E1", "unused": "drop-me"},
                    "business_objective": {"business_goal": "只查询"},
                    "observable_run": {"events": [_event("RUN-A")]},
                },
                "linkage_only": {"case_id": "CASE-A", "run_id": "RUN-A", "agent_version": "v1"},
            }
        ],
        "variant_inputs": [],
    }


def test_case_and_control_share_input_shape():
    case_payload = build_judge_input(_case(), _run(), run_index=0)
    control = _control()
    item = control["baseline_inputs"][0]
    control_payload = normalize_judge_input(
        item["judge_input"], linkage=item["linkage_only"]
    )
    assert set(case_payload["judge_input"]) == set(control_payload["judge_input"])
    for key in ("business_type", "domain", "user_request", "context", "business_objective", "observable_trace"):
        assert key in control_payload["judge_input"]
    assert "unused" not in control_payload["judge_input"]["context"]
    assert control_payload["judge_input"]["observable_trace"][0]["event_id"] == "RUN-A-E01"


def test_forbidden_answer_fields_are_rejected_for_controls():
    control = _control()
    raw = control["baseline_inputs"][0]["judge_input"]
    raw["first_fault_event_id"] = "RUN-A-E01"
    with pytest.raises(JudgeInputContractError):
        normalize_judge_input(raw, linkage={"run_id": "RUN-A"})


def test_digest_mismatch_blocks_execution():
    control = _control()
    item = control["baseline_inputs"][0]
    with pytest.raises(JudgeInputContractError):
        normalize_judge_input(item["judge_input"], linkage=item["linkage_only"], expected_sha256="0" * 64)


def test_digest_is_stable_and_event_ids_preserved():
    control = _control()
    item = control["baseline_inputs"][0]
    first = normalize_judge_input(item["judge_input"], linkage=item["linkage_only"])
    second = normalize_judge_input(dict(item["judge_input"]), linkage=item["linkage_only"])
    assert first["judge_input_sha256"] == second["judge_input_sha256"]
    assert sha256_json({"judge_input": first["judge_input"], "linkage": item["linkage_only"]}) == first["judge_input_sha256"]
    assert [e["event_id"] for e in first["judge_input"]["observable_trace"]] == ["RUN-A-E01"]


def test_control_runner_uses_normalized_trace_for_evidence():
    control = _control()
    batch = run_controls([control], ScriptedJudge(), max_workers=2)
    assert batch.issues == []
    snapshot = batch.snapshots[0]
    assert snapshot["judge_input"]["observable_trace"][0]["event_id"] == "RUN-A-E01"
    assert batch.controls[0]["baseline"][0]["verdict"] == "REJECT"


def test_missing_evidence_event_is_rejected():
    assert _check_evidence(["NOPE"], {"RUN-A-E01"}, "run") != []


def test_event_sequence_must_be_complete_and_increasing():
    control = _control()
    raw = control["baseline_inputs"][0]["judge_input"]
    raw["observable_run"] = {"events": [_event("RUN-A", 2), _event("RUN-A", 1)]}
    import pytest

    from tracelab.final_round.judge_input import JudgeInputContractError
    with pytest.raises(JudgeInputContractError):
        normalize_judge_input(raw, linkage={"run_id": "RUN-A"})
