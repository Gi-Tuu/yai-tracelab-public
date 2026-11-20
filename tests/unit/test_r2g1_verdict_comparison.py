"""R2-G1 P0-C：四态语义、业务布尔与版本比较。"""

from __future__ import annotations

from typing import Any

from tracelab.final_round import run_cases
from tracelab.final_round.results import build_case_result, compare_versions, validate_case_result


def _event(event_id: str, seq: int) -> dict:
    return {
        "event_id": event_id,
        "seq": seq,
        "step_id": "INPUT",
        "step_status": "SUCCEEDED",
        "observed_input": {},
        "observed_output": {},
        "tool_calls": [],
    }


def _case() -> dict:
    return {
        "dataset_version": "dv1",
        "data_origin": "synth",
        "case_id": "CASE-C",
        "business_type": "ai_consultant",
        "domain": "salary",
        "data_purpose": "test",
        "near_duplicate_group": "g",
        "user_request": "测试",
        "context": {"enterprise_id": "E1"},
        "reference_result": {
            "business_goal": "目标",
            "pass_criteria": ["准则"],
            "required_evidence": ["证据"],
            "rule_source": {"x": 1},
            "reference_semantics": "business_goal_only",
        },
        "runs": [
            {
                "run_id": "RUN-A",
                "agent_version": "v1",
                "task_id": "T-A",
                "trace_id": "TR-A",
                "events": [_event("RUN-A-E01", 1), _event("RUN-A-E02", 2)],
                "final_state": "DONE",
                "snapshot_state": "DONE",
                "workflow_terminal": True,
                "observation_complete": True,
            },
            {
                "run_id": "RUN-B",
                "agent_version": "v2",
                "task_id": "T-B",
                "trace_id": "TR-B",
                "events": [_event("RUN-B-E01", 1), _event("RUN-B-E02", 2)],
                "final_state": "DONE",
                "snapshot_state": "DONE",
                "workflow_terminal": True,
                "observation_complete": True,
            },
        ],
    }


def _assessment(verdict: str, run_id: str, **extra: Any) -> dict:
    base = {
        "run_id": run_id,
        "agent_version": "v1",
        "judge_id": "j",
        "judge_version": "jv",
        "verdict": verdict,
        "reason": "理由",
        "dimensions": [{"name": "d", "conclusion": "PASS", "reason": "r", "evidence_event_ids": []}],
        "evidence_event_ids": [],
        "first_fault_event_id": None,
        "root_causes": [],
        "uncertainty": {"level": "low", "reason": "r"},
        "duration_ms": 1.0,
        "call_linkage": {
            "task_id": "t",
            "call_id": "c",
            "attempt_id": "a",
            "input_snapshot_id": "s",
            "judge_input_sha256": "h",
            "model": "m",
            "prompt_version": "p",
            "execution_origin": "TEST",
        },
    }
    base.update(extra)
    return base


def test_compare_versions_four_states():
    assert compare_versions(_assessment("PASS", "A"), _assessment("FAIL", "B"))["direction"] == "baseline_better"
    assert compare_versions(_assessment("FAIL", "A"), _assessment("PASS", "B"))["direction"] == "candidate_better"
    equal = compare_versions(_assessment("PASS", "A"), _assessment("PASS", "B"))
    assert equal["direction"] == "equal" and "过程质量" in equal["reason"]
    fail_both = compare_versions(_assessment("FAIL", "A"), _assessment("FAIL", "B"))
    assert fail_both["direction"] == "insufficient_evidence"
    reject = compare_versions(_assessment("REJECT", "A"), _assessment("PASS", "B"))
    assert reject["direction"] == "insufficient_evidence"


def test_compare_versions_rejects_different_business_standards():
    result = compare_versions(
        _assessment("PASS", "A"),
        _assessment("PASS", "B"),
        baseline_business_type="invoice",
        candidate_business_type="ai_consultant",
    )
    assert result["direction"] == "insufficient_evidence"
    assert "业务目标" in result["reason"]


class _ABJudge:
    judge_id = "fake"
    judge_version = "v1"
    model = "mock"
    prompt_version = "p"
    execution_origin = "TEST"

    def evaluate(self, judge_input, linkage):  # type: ignore[no-untyped-def]
        run_id = str(linkage.get("run_id"))
        if run_id == "RUN-A":
            return {
                "verdict": "PASS",
                "agent_task_success": True,
                "business_result_correct": True,
                "reason": "A 正确",
            }
        return {
            "verdict": "FAIL",
            "agent_task_success": False,
            "business_result_correct": False,
            "reason": "B 失败",
        }


def test_runner_writes_real_version_comparison():
    batch = run_cases([_case()], _ABJudge(), max_workers=2)
    assert batch.issues == []
    comparison = batch.results[0]["version_comparison"]
    assert comparison["direction"] == "baseline_better"
    assert comparison["reason"]
    manifest = batch.to_manifest()
    assert manifest["schema_valid_cases"] == 1
    assert manifest["model_valid_runs"] == 2


def test_finance_boolean_must_be_null_for_reject():
    case = _case()
    result = build_case_result(
        case,
        [
            _assessment("REJECT", "RUN-A", business_invoice_created=True),
            _assessment("REJECT", "RUN-B", business_invoice_created=None),
        ],
        annotation_version="a",
        rubric_version="r",
        config_version="c",
    )
    issues = validate_case_result(result, case)
    assert any("business_invoice_created" in issue and "null" in issue for issue in issues)
